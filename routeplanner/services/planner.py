"""Orchestration: geocode -> route -> corridor match -> optimise -> response."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from . import geo
from .corridor import CandidateStop, get_station_index
from .geocoding import Place, geocode
from .optimizer import FuelPlan, InfeasibleRouteError, plan_fuel_stops
from .routing import RouteResult, fetch_route

logger = logging.getLogger(__name__)


@dataclass
class PlanRequest:
    start: str
    finish: str
    mpg: float
    range_miles: float
    max_detour_miles: float
    start_fuel_gallons: float = 0.0
    include_geometry: bool = True
    refresh: bool = False


@dataclass
class PlanContext:
    api_calls: int = 0
    routing_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _config(key: str):
    return settings.ROUTE_PLANNER[key]


def _average_price() -> float:
    index = get_station_index()
    if not len(index):
        return 0.0
    return sum(station.price for station in index.stations) / len(index)


def _cache_key(start: Place, finish: Place, request: PlanRequest) -> str:
    raw = "|".join(
        str(part)
        for part in (
            round(start.latitude, 4),
            round(start.longitude, 4),
            round(finish.latitude, 4),
            round(finish.longitude, 4),
            request.mpg,
            request.range_miles,
            request.max_detour_miles,
            request.start_fuel_gallons,
            request.include_geometry,
        )
    )
    return "routeplan:" + hashlib.sha1(raw.encode()).hexdigest()


def _match_corridor(
    route: RouteResult, max_detour_miles: float
) -> tuple[list[CandidateStop], list[float]]:
    cumulative = geo.cumulative_miles(route.points)
    sampled = geo.sample_points(route.points, cumulative, _config("ROUTE_SAMPLE_POINTS"))
    candidates = get_station_index().candidates_along(sampled, max_detour_miles)
    return candidates, cumulative


def _merge_repeat_visits(plan: FuelPlan):
    """Fold consecutive purchases at the same truck stop into one stop.

    The origin fill is priced at a nearby station, so the algorithm can
    legitimately buy at mile 0 and top up a few miles later at the very same
    forecourt. That is one stop to a driver, so it is one stop in the response.
    """
    merged: list[tuple] = []  # (node, gallons, cost)
    for purchase in plan.purchases:
        station = purchase.node.candidate.station if purchase.node.candidate else None
        if merged:
            previous_node, gallons, cost = merged[-1]
            previous_station = (
                previous_node.candidate.station if previous_node.candidate else None
            )
            same_stop = (
                station is not None
                and previous_station is not None
                and station.opis_id == previous_station.opis_id
            )
            if same_stop:
                merged[-1] = (previous_node, gallons + purchase.gallons, cost + purchase.cost)
                continue
        merged.append((purchase.node, purchase.gallons, purchase.cost))
    return merged


def _serialize_stops(plan: FuelPlan) -> list[dict]:
    stops = []
    for node, gallons, cost in _merge_repeat_visits(plan):
        candidate = node.candidate
        station = candidate.station if candidate else None
        stops.append(
            {
                "sequence": len(stops) + 1,
                "is_origin_fill": node.is_origin_fill,
                "name": station.name if station else "Start of route",
                "address": station.address if station else "",
                "city": station.city if station else "",
                "state": station.state if station else "",
                "opis_id": station.opis_id if station else None,
                "latitude": station.latitude if station else None,
                "longitude": station.longitude if station else None,
                "price_per_gallon": round(node.price, 3),
                "mile_marker": round(node.mile_marker, 1),
                "detour_miles": round(candidate.detour_miles, 1) if candidate else 0.0,
                "gallons": round(gallons, 2),
                "cost": round(cost, 2),
                "note": (
                    "Initial tank, priced at the cheapest truck stop near the start"
                    if node.is_origin_fill
                    else None
                ),
            }
        )
    return stops


def _build_response(
    request: PlanRequest,
    start: Place,
    finish: Place,
    route: RouteResult,
    plan: FuelPlan | None,
    context: PlanContext,
    candidate_count: int,
    detour_used: float,
) -> dict:
    average_price = _average_price()
    gallons_for_trip = route.distance_miles / request.mpg
    baseline_cost = gallons_for_trip * average_price

    if plan is not None:
        stops = _serialize_stops(plan)
        total_gallons = plan.total_gallons
        total_cost = plan.total_cost
    else:
        stops = []
        total_gallons = gallons_for_trip
        total_cost = baseline_cost

    payload = {
        "start": {
            "query": start.query,
            "resolved_name": start.display_name,
            "latitude": round(start.latitude, 6),
            "longitude": round(start.longitude, 6),
            "geocoder": start.provider,
        },
        "finish": {
            "query": finish.query,
            "resolved_name": finish.display_name,
            "latitude": round(finish.latitude, 6),
            "longitude": round(finish.longitude, 6),
            "geocoder": finish.provider,
        },
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_hours, 2),
            "provider": route.provider,
            "bbox": [round(value, 6) for value in geo.bounding_box(route.points)],
        },
        "vehicle": {
            "mpg": request.mpg,
            "range_miles": request.range_miles,
            "tank_gallons": round(request.range_miles / request.mpg, 2),
            "start_fuel_gallons": request.start_fuel_gallons,
        },
        "fuel_stops": stops,
        "totals": {
            "stops": len(stops),
            "gallons": round(total_gallons, 2),
            "fuel_cost": round(total_cost, 2),
            "average_price_paid": round(total_cost / total_gallons, 3) if total_gallons else 0.0,
            "cost_at_national_average": round(baseline_cost, 2),
            "savings_vs_national_average": round(baseline_cost - total_cost, 2),
            "national_average_price": round(average_price, 3),
        },
        "meta": {
            "external_api_calls": context.api_calls,
            "cached": False,
            "stations_considered": candidate_count,
            "max_detour_miles": detour_used,
            "route_points": len(route.points),
            # How much of the wall clock belonged to the free routing service
            # rather than to this API.
            "routing_api_ms": context.routing_ms,
            "warnings": context.warnings,
        },
    }

    if request.include_geometry:
        simplified = geo.simplify_path(route.points)
        payload["route"]["geometry"] = {
            "type": "LineString",
            "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in simplified],
        }
        payload["route"]["geometry_points"] = len(simplified)

    return payload


def plan_route(request: PlanRequest) -> dict:
    """Plan a route and its fuel stops. Returns a JSON-ready dict."""
    started = time.perf_counter()
    context = PlanContext()

    start = geocode(request.start)
    finish = geocode(request.finish)
    context.api_calls += start.api_calls + finish.api_calls

    key = _cache_key(start, finish, request)
    if not request.refresh:
        cached = cache.get(key)
        if cached is not None:
            payload = {**cached, "meta": {**cached["meta"], "cached": True}}
            payload["meta"]["external_api_calls"] = context.api_calls
            payload["meta"]["routing_api_ms"] = 0.0
            payload["meta"]["compute_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return payload

    routing_started = time.perf_counter()
    route = fetch_route(start.coordinates, finish.coordinates)
    context.routing_ms = round((time.perf_counter() - routing_started) * 1000, 1)
    context.api_calls += route.api_calls
    context.warnings.extend(route.warnings)

    detour = request.max_detour_miles
    limit = _config("MAX_DETOUR_MILES_LIMIT")
    plan: FuelPlan | None = None
    candidates: list[CandidateStop] = []
    last_error: InfeasibleRouteError | None = None

    # Widen the corridor once before giving up - sparse stretches of the country
    # simply need a longer look off the highway.
    while True:
        candidates, _ = _match_corridor(route, detour)
        try:
            plan = plan_fuel_stops(
                candidates,
                route.distance_miles,
                mpg=request.mpg,
                range_miles=request.range_miles,
                start_fuel_gallons=request.start_fuel_gallons,
            )
            break
        except InfeasibleRouteError as exc:
            last_error = exc
            if detour >= limit:
                break
            detour = min(detour * 2, limit)
            context.warnings.append(
                f"Widened the search corridor to {detour:.0f} miles to find reachable fuel stops."
            )

    if plan is None:
        assert last_error is not None
        if route.distance_miles <= request.range_miles:
            # The trip fits in one tank, so it is still drivable - just report an
            # estimate instead of failing.
            context.warnings.append(
                "No fuel stations were found near this route; the trip is within one tank, "
                "so the cost below is estimated at the national average price."
            )
        else:
            raise last_error

    payload = _build_response(
        request, start, finish, route, plan, context, len(candidates), detour
    )
    cache.set(key, payload, _config("PLAN_CACHE_SECONDS"))
    payload["meta"]["compute_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return payload
