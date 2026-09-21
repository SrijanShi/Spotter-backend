"""Where to stop for fuel, and how much to buy at each stop.

The plan minimises

    fuel bought  +  a fixed cost for every stop made

where a stop's fixed cost is ``stop_penalty`` dollars (the driver's time, ~$5
for a 10 minute stop) plus, optionally, the fuel to drive out to the station
and back. With neither, this is the classic "gas station problem" and the answer
is simply the cheapest fuel - but that answer is impractical: it happily pulls a
truck off the interstate to buy 1.2 gallons that are 3 cents cheaper. Miami to
Seattle takes 27 stops that way; with a $5 stop cost and detours priced it takes
9, for 1% more fuel money. The planner reports both plans, so the trade-off is
visible rather than hidden.

Method: dynamic programming over (station, fuel in the tank). Walking the
stations in order along the route, keep for every fuel level the cheapest way to
arrive with that much fuel. At a station the truck either drives past, or stops
and fills to any level. The fill step is a prefix minimum, so each station
costs O(tank) and a cross-country route solves in a few milliseconds. Fuel is
tracked in whole miles of range (0.1 gallon at 10 mpg), which is the only
approximation, and the result is optimal at that resolution.
``test_optimizer_optimality.py`` checks it against brute force.

Cost model: the tank starts empty and the origin is treated as a fill-up at the
cheapest truck stop near the start, so the gallons purchased cover every mile of
the trip. Callers that prefer the "starts with a full tank" reading can pass
``start_fuel_gallons``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .corridor import CandidateStop

EPSILON = 1e-9
INFINITY = float("inf")

# Fuel is tracked in miles of range at this resolution.
RESOLUTION_MILES = 1.0

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
    # Fuel cost plus stop costs - the quantity actually minimised.
    objective: float = 0.0


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
        rank = (candidate.station.price, candidate.detour_miles)
        if current is None or rank < (current.station.price, current.detour_miles):
            cheapest_per_bucket[bucket] = candidate

    if not cheapest_per_bucket:
        raise InfeasibleRouteError(
            "No fuel stations were found near this route.",
            detail={"reason": "no_stations_in_corridor"},
        )

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


def _stop_cost(node: FuelNode, *, mpg: float, stop_penalty: float, count_detours: bool) -> float:
    """Fixed cost of stopping at ``node``, whatever the amount bought."""
    if node.is_origin_fill or node.candidate is None:
        return 0.0  # the trip starts here anyway
    detour_fuel = 2 * node.candidate.detour_miles / mpg * node.price if count_detours else 0.0
    return stop_penalty + detour_fuel


def plan_fuel_stops(
    candidates: Sequence[CandidateStop],
    route_miles: float,
    *,
    mpg: float,
    range_miles: float,
    start_fuel_gallons: float = 0.0,
    stop_penalty: float = 0.0,
    count_detours: bool = False,
) -> FuelPlan:
    """Cheapest plan under the stop-cost model described in the module docstring.

    ``stop_penalty`` is dollars per stop; ``count_detours`` adds the fuel to reach
    each station and come back. Both zero gives the pure cheapest-fuel plan.
    """
    tank_gallons = range_miles / mpg
    nodes = build_nodes(candidates, route_miles)
    check_reachability(nodes, route_miles, range_miles)

    tank = int(math.floor(range_miles / RESOLUTION_MILES + EPSILON))
    levels = np.arange(tank + 1, dtype=float)  # fuel on board, in grid units of range
    indexes = np.arange(tank + 1)

    # cost[level] = cheapest spend to be here with `level` units of range on board.
    cost = np.full(tank + 1, INFINITY)
    cost[min(tank, int(round(max(start_fuel_gallons, 0.0) * mpg / RESOLUTION_MILES)))] = 0.0

    decisions: list[tuple[np.ndarray, np.ndarray, int]] = []
    position = 0
    for node in nodes:
        mile = int(round(node.mile_marker / RESOLUTION_MILES))
        advance = mile - position
        position = mile
        if advance:
            # Driving `advance` units burns that much range.
            if advance > tank:
                cost = np.full(tank + 1, INFINITY)
            else:
                cost = np.concatenate((cost[advance:], np.full(advance, INFINITY)))

        per_unit = node.price * RESOLUTION_MILES / mpg
        fixed = _stop_cost(node, mpg=mpg, stop_penalty=stop_penalty, count_detours=count_detours)

        # Stopping here and filling up to `level` from any lower level `start`:
        #   cost[start] + (level - start) * per_unit + fixed
        # = fixed + level * per_unit + min over start <= level of (cost[start] - start * per_unit)
        adjusted = cost - levels * per_unit
        prefix_best = np.minimum.accumulate(adjusted)
        is_new_best = adjusted <= np.concatenate(([INFINITY], prefix_best[:-1]))
        best_start = np.maximum.accumulate(np.where(is_new_best, indexes, 0))
        if_stopping = fixed + levels * per_unit + prefix_best

        stopped = if_stopping < cost - EPSILON
        cost = np.where(stopped, if_stopping, cost)
        decisions.append((stopped, best_start, advance))

    remaining = route_miles - position * RESOLUTION_MILES
    needed = max(0, int(math.ceil(remaining / RESOLUTION_MILES - EPSILON)))
    if needed > tank or not np.isfinite(cost[needed:]).any():
        raise InfeasibleRouteError(
            "No combination of stops covers this route within the vehicle's range.",
            detail={
                "reason": "range_gap",
                "hint": "Increase max_detour_miles to consider stations further off the route.",
            },
        )
    final_level = needed + int(np.argmin(cost[needed:]))
    objective = float(cost[final_level])

    purchases: list[FuelPurchase] = []
    level = final_level
    for node, (stopped, best_start, advance) in zip(reversed(nodes), reversed(decisions)):
        if stopped[level]:
            start = int(best_start[level])
            gallons = (level - start) * RESOLUTION_MILES / mpg
            if gallons > EPSILON:
                purchases.append(FuelPurchase(node=node, gallons=gallons, cost=gallons * node.price))
            level = start
        level += advance
    purchases.reverse()

    return FuelPlan(
        purchases=purchases,
        total_gallons=sum(p.gallons for p in purchases),
        total_cost=sum(p.cost for p in purchases),
        tank_gallons=tank_gallons,
        fuel_remaining_gallons=max(final_level * RESOLUTION_MILES - remaining, 0.0) / mpg,
        objective=objective,
    )
