"""Check the dynamic program against exhaustive search.

On small random instances - with distances chosen so that every decision lands
on a whole gallon - a search can enumerate every possible way to buy fuel,
including the choice of where to stop. The planner must find exactly the same
minimum, with and without a per-stop cost and detour costs.
"""

import random

from django.test import SimpleTestCase

from routeplanner.services.optimizer import build_nodes, check_reachability, plan_fuel_stops
from routeplanner.services.optimizer import InfeasibleRouteError
from routeplanner.tests.test_optimizer import stop

MPG = 10.0
RANGE_MILES = 500.0
TANK = int(RANGE_MILES / MPG)  # 50 whole gallons


def brute_force_minimum(nodes, route_miles: float, stop_penalty: float, count_detours: bool) -> float:
    """Cheapest (fuel + stop costs) way to finish, trying every whole-gallon purchase."""

    def stop_cost(node) -> float:
        if node.is_origin_fill:
            return 0.0
        detour = 2 * node.candidate.detour_miles / MPG * node.price if count_detours else 0.0
        return stop_penalty + detour

    best = [[float("inf")] * (TANK + 1) for _ in nodes]
    best[0][0] = 0.0
    answer = float("inf")

    for i, node in enumerate(nodes):
        for fuel in range(TANK + 1):
            cost = best[i][fuel]
            if cost == float("inf"):
                continue
            for bought in range(0, TANK - fuel + 1):
                total = cost + bought * node.price + (stop_cost(node) if bought else 0.0)
                on_board = fuel + bought
                if route_miles - node.mile_marker <= on_board * MPG:
                    answer = min(answer, total)
                for j in range(i + 1, len(nodes)):
                    needed = (nodes[j].mile_marker - node.mile_marker) / MPG
                    if needed > on_board:
                        break
                    left = int(round(on_board - needed))
                    best[j][left] = min(best[j][left], total)
    return answer


class PlannerMatchesBruteForceTests(SimpleTestCase):
    def check(self, stop_penalty: float, count_detours: bool, trials: int = 30):
        rng = random.Random(f"{stop_penalty}-{count_detours}")
        compared = 0
        for trial in range(trials):
            count = rng.randint(3, 7)
            # Mile markers are multiples of 10 so a leg always costs whole gallons.
            miles = sorted(rng.sample(range(10, 900, 10), count))
            route_miles = float(miles[-1] + rng.randrange(10, 400, 10))
            candidates = [
                stop(float(m), round(rng.uniform(2.5, 5.0), 2), detour=rng.choice([0.0, 1.0, 5.0, 12.0]))
                for m in miles
            ]
            nodes = build_nodes(candidates, route_miles)
            try:
                check_reachability(nodes, route_miles, RANGE_MILES)
            except InfeasibleRouteError:
                continue  # not drivable; covered by the infeasibility tests

            plan = plan_fuel_stops(
                candidates,
                route_miles,
                mpg=MPG,
                range_miles=RANGE_MILES,
                stop_penalty=stop_penalty,
                count_detours=count_detours,
            )
            optimum = brute_force_minimum(nodes, route_miles, stop_penalty, count_detours)
            self.assertAlmostEqual(
                plan.objective,
                optimum,
                places=6,
                msg=f"trial {trial}: planner {plan.objective} != optimum {optimum} "
                f"for miles={miles} route={route_miles}",
            )
            compared += 1
        self.assertGreater(compared, trials // 3)

    def test_cheapest_fuel_only(self):
        self.check(stop_penalty=0.0, count_detours=False)

    def test_with_a_stop_penalty(self):
        self.check(stop_penalty=5.0, count_detours=False)

    def test_with_a_stop_penalty_and_detours(self):
        self.check(stop_penalty=5.0, count_detours=True)

    def test_with_a_large_stop_penalty(self):
        self.check(stop_penalty=40.0, count_detours=True)
