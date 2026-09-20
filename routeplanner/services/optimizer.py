"""Cost-optimal fuel purchasing along a route.

This is the classic "gas station problem" with a uniform tank and fuel that can
be bought by the fraction of a gallon. The greedy rule below is provably
optimal for that model:

    At the station you are standing at, look ahead as far as the tank can carry
    you.
      * If a cheaper station is in reach, buy exactly enough to get to the
        first cheaper one - never pay today's higher price for fuel you can buy
        cheaper down the road.
      * Otherwise this is the cheapest fuel you will see for a full tank, so
        fill up and drive to the cheapest station in reach.
    Near the destination, never buy more than the fuel needed to finish.

Cost model: the tank starts empty and the origin is treated as a fill-up at the
cheapest truck stop near the start, so the gallons purchased come to exactly
``distance / mpg`` and the returned total covers every mile of the trip.
Callers that prefer the "starts with a full tank" reading can pass
``start_fuel_gallons``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .corridor import CandidateStop

EPSILON = 1e-9

# A station this far along the route still counts as "near the start" when
# pricing the initial tank.
ORIGIN_WINDOW_MILES = 50.0

# Stations whose mile markers fall in the same bucket are interchangeable stops;
# only the cheapest is kept. City-centroid geocoding produces a lot of these.
MILE_BUCKET = 0.1


class InfeasibleRouteError(RuntimeError):
    """The route cannot be driven with the given range and station coverage."""

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class FuelNode:
    mile_marker: float
    price: float
    candidate: CandidateStop | None = None  # None only for the virtual origin
    is_origin_fill: bool = False


@dataclass
class FuelPurchase:
    node: FuelNode
    gallons: float
    cost: float


@dataclass
class FuelPlan:
    purchases: list[FuelPurchase]
    total_gallons: float
    total_cost: float
    tank_gallons: float
    fuel_remaining_gallons: float


def build_nodes(
    candidates: Sequence[CandidateStop], route_miles: float
) -> list[FuelNode]:
    """Collapse candidates into fuel nodes and prepend the virtual origin."""
    if not candidates:
        raise InfeasibleRouteError(
            "No fuel stations were found near this route.",
            detail={"reason": "no_stations_in_corridor"},
        )

    cheapest_per_bucket: dict[int, CandidateStop] = {}
    for candidate in candidates:
        if candidate.mile_marker > route_miles:
            continue
        bucket = int(candidate.mile_marker / MILE_BUCKET)
        current = cheapest_per_bucket.get(bucket)
        if current is None or candidate.station.price < current.station.price:
            cheapest_per_bucket[bucket] = candidate

    ordered = sorted(cheapest_per_bucket.values(), key=lambda c: c.mile_marker)
    nodes = [
        FuelNode(mile_marker=c.mile_marker, price=c.station.price, candidate=c) for c in ordered
    ]

    near_start = [node for node in nodes if node.mile_marker <= ORIGIN_WINDOW_MILES]
    reference = min(near_start, key=lambda n: n.price) if near_start else nodes[0]
    origin = FuelNode(
        mile_marker=0.0,
        price=reference.price,
        candidate=reference.candidate,
        is_origin_fill=True,
    )
    # Drop any real node sitting on mile 0 - the origin fill already covers it.
    nodes = [node for node in nodes if node.mile_marker > EPSILON]
    return [origin] + nodes


def check_reachability(nodes: Sequence[FuelNode], route_miles: float, range_miles: float) -> None:
    """Every consecutive hop - and the run to the destination - must fit the tank."""
    for previous, following in zip(nodes, nodes[1:]):
        gap = following.mile_marker - previous.mile_marker
        if gap > range_miles + EPSILON:
            raise InfeasibleRouteError(
                f"No fuel station within {range_miles:.0f} miles between mile "
                f"{previous.mile_marker:.1f} and mile {following.mile_marker:.1f}.",
                detail={
                    "reason": "range_gap",
                    "gap_miles": round(gap, 1),
                    "from_mile": round(previous.mile_marker, 1),
                    "to_mile": round(following.mile_marker, 1),
                    "from_station": previous.candidate.station.name if previous.candidate else None,
                    "to_station": following.candidate.station.name if following.candidate else None,
                    "hint": "Increase max_detour_miles to consider stations further off the route.",
                },
            )

    final_leg = route_miles - nodes[-1].mile_marker
    if final_leg > range_miles + EPSILON:
        raise InfeasibleRouteError(
            f"The last {final_leg:.1f} miles to the destination exceed the "
            f"{range_miles:.0f} mile range of the vehicle.",
            detail={
                "reason": "range_gap_to_destination",
                "gap_miles": round(final_leg, 1),
                "from_mile": round(nodes[-1].mile_marker, 1),
                "hint": "Increase max_detour_miles to consider stations further off the route.",
            },
        )


def plan_fuel_stops(
    candidates: Sequence[CandidateStop],
    route_miles: float,
    *,
    mpg: float,
    range_miles: float,
    start_fuel_gallons: float = 0.0,
) -> FuelPlan:
    tank_gallons = range_miles / mpg
    nodes = build_nodes(candidates, route_miles)
    check_reachability(nodes, route_miles, range_miles)

    fuel = min(max(start_fuel_gallons, 0.0), tank_gallons)
    purchases: list[FuelPurchase] = []
    index = 0
    # Each iteration moves strictly forward, so this cannot spin.
    for _ in range(len(nodes) + 1):
        node = nodes[index]
        remaining_miles = route_miles - node.mile_marker
        if remaining_miles <= fuel * mpg + EPSILON:
            break

        reach_limit = node.mile_marker + range_miles + EPSILON
        window = []
        j = index + 1
        while j < len(nodes) and nodes[j].mile_marker <= reach_limit:
            window.append(j)
            j += 1

        if not window:
            # check_reachability already proved the destination is in range.
            gallons_needed = remaining_miles / mpg
            purchase = min(gallons_needed - fuel, tank_gallons - fuel)
            if purchase > EPSILON:
                purchases.append(
                    FuelPurchase(node=node, gallons=purchase, cost=purchase * node.price)
                )
                fuel += purchase
            break

        cheaper = next((j for j in window if nodes[j].price < node.price - EPSILON), None)
        if cheaper is not None:
            target = cheaper
            gallons_needed = (nodes[target].mile_marker - node.mile_marker) / mpg
            purchase = max(gallons_needed - fuel, 0.0)
        else:
            # Cheapest fuel within a tank's reach: fill up, but never buy more
            # than it takes to finish the trip.
            purchase = min(tank_gallons, remaining_miles / mpg) - fuel
            purchase = max(purchase, 0.0)
            target = min(window, key=lambda j: (nodes[j].price, -nodes[j].mile_marker))

        if purchase > EPSILON:
            purchases.append(FuelPurchase(node=node, gallons=purchase, cost=purchase * node.price))
            fuel += purchase

        leg_miles = nodes[target].mile_marker - node.mile_marker
        if leg_miles > fuel * mpg + EPSILON:
            # Not enough fuel for the cheapest option - stop at the furthest
            # station we can actually reach instead.
            reachable = [j for j in window if nodes[j].mile_marker - node.mile_marker <= fuel * mpg]
            if not reachable:
                raise InfeasibleRouteError(
                    "Ran out of range before the next reachable station.",
                    detail={"reason": "range_gap", "from_mile": round(node.mile_marker, 1)},
                )
            target = max(reachable, key=lambda j: nodes[j].mile_marker)
            leg_miles = nodes[target].mile_marker - node.mile_marker

        fuel -= leg_miles / mpg
        index = target

    total_gallons = sum(p.gallons for p in purchases)
    total_cost = sum(p.cost for p in purchases)
    return FuelPlan(
        purchases=purchases,
        total_gallons=total_gallons,
        total_cost=total_cost,
        tank_gallons=tank_gallons,
        fuel_remaining_gallons=max(fuel, 0.0),
    )
