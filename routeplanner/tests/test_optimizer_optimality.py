"""Cross-check the greedy against an exhaustive search.

The greedy rule in `optimizer.py` is provably optimal, but a proof in a docstring
is worth little without evidence. On small random instances - with distances
chosen so that every decision lands on a whole gallon - a dynamic program can
enumerate every possible way to buy fuel. The greedy must match it exactly.
"""

import random

from django.test import SimpleTestCase

from routeplanner.services.optimizer import build_nodes, plan_fuel_stops
from routeplanner.tests.test_optimizer import stop

MPG = 10.0
RANGE_MILES = 500.0
TANK = int(RANGE_MILES / MPG)  # 50 whole gallons


def brute_force_minimum_cost(nodes, route_miles: float) -> float:
    """Cheapest way to finish, searching every whole-gallon purchase."""
    best = [[float("inf")] * (TANK + 1) for _ in nodes]
    best[0][0] = 0.0
    answer = float("inf")

    for i, node in enumerate(nodes):
        for fuel in range(TANK + 1):
            cost = best[i][fuel]
            if cost == float("inf"):
                continue
            for bought in range(0, TANK - fuel + 1):
                total_cost = cost + bought * node.price
                on_board = fuel + bought
                if route_miles - node.mile_marker <= on_board * MPG:
                    answer = min(answer, total_cost)
                for j in range(i + 1, len(nodes)):
                    leg = nodes[j].mile_marker - node.mile_marker
                    needed = leg / MPG
                    if needed > on_board:
                        break
                    left = int(round(on_board - needed))
                    if total_cost < best[j][left]:
                        best[j][left] = total_cost
    return answer


class GreedyMatchesBruteForceTests(SimpleTestCase):
    def test_random_instances(self):
        rng = random.Random(20260920)
        for trial in range(40):
            count = rng.randint(3, 7)
            # Mile markers are multiples of 10 so a leg always costs whole gallons.
            miles = sorted(rng.sample(range(10, 900, 10), count))
            route_miles = float(miles[-1] + rng.randrange(10, 400, 10))
            candidates = [stop(float(m), round(rng.uniform(2.5, 5.0), 2)) for m in miles]

            nodes = build_nodes(candidates, route_miles)
            gaps = [b.mile_marker - a.mile_marker for a, b in zip(nodes, nodes[1:])]
            if (gaps and max(gaps) > RANGE_MILES) or (
                route_miles - nodes[-1].mile_marker > RANGE_MILES
            ):
                continue  # not drivable; covered by the infeasibility tests

            plan = plan_fuel_stops(
                candidates, route_miles, mpg=MPG, range_miles=RANGE_MILES
            )
            optimum = brute_force_minimum_cost(nodes, route_miles)
            self.assertAlmostEqual(
                plan.total_cost,
                optimum,
                places=6,
                msg=f"trial {trial}: greedy {plan.total_cost} != optimum {optimum} "
                f"for miles={miles} route={route_miles}",
            )
