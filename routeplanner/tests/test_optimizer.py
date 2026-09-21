"""Known-answer tests for the fuel purchasing algorithm.

Every expected number below is worked out by hand in the comments, so a failure
points at the algorithm rather than at a fixture.
"""

from django.test import SimpleTestCase

from routeplanner.services.corridor import CandidateStop, Station
from routeplanner.services.optimizer import InfeasibleRouteError, plan_fuel_stops


def stop(mile: float, price: float, name: str = "", detour: float = 1.0) -> CandidateStop:
    return CandidateStop(
        station=Station(
            opis_id=f"{name or mile}",
            name=name or f"Station @{mile}",
            address="",
            city="Town",
            state="TX",
            latitude=32.0,
            longitude=-96.0,
            price=price,
        ),
        mile_marker=mile,
        detour_miles=detour,
    )


class PlanFuelStopsTests(SimpleTestCase):
    def plan(self, candidates, route_miles, **kwargs):
        kwargs.setdefault("mpg", 10.0)
        kwargs.setdefault("range_miles", 500.0)
        return plan_fuel_stops(candidates, route_miles, **kwargs)

    def test_buys_exactly_the_fuel_the_trip_needs(self):
        # 100 miles at 10 mpg = 10 gallons, and nothing more.
        plan = self.plan([stop(10, 3.00), stop(50, 4.00)], 100)
        self.assertAlmostEqual(plan.total_gallons, 10.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 30.0, places=6)

    def test_skips_expensive_stations_to_reach_a_cheaper_one(self):
        # $5 at mile 10, $2 at mile 100, 200 mile route:
        # buy 10 gal at $5 to reach the cheap station, then 10 gal at $2.
        plan = self.plan([stop(10, 5.00, "pricey"), stop(100, 2.00, "cheap")], 200)
        self.assertAlmostEqual(plan.total_cost, 70.0, places=6)
        self.assertAlmostEqual(plan.total_gallons, 20.0, places=6)
        names = [purchase.node.candidate.station.name for purchase in plan.purchases]
        self.assertNotIn("pricey", names[1:])

    def test_fills_up_when_prices_only_rise(self):
        # $3 @1, $4 @300, $5 @600 on a 900 mile route.
        # 50.1 gal at $3 (a full tank at the start, topped up at mile 1, reaches
        # mile 501), 29.9 at $4 (fills up at mile 300, reaches 800), 10 at $5.
        plan = self.plan([stop(1, 3.00), stop(300, 4.00), stop(600, 5.00)], 900)
        self.assertAlmostEqual(plan.total_gallons, 90.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 150.3 + 119.6 + 50.0, places=6)
        # Never more than a tank at a time.
        self.assertTrue(all(p.gallons <= plan.tank_gallons + 1e-9 for p in plan.purchases))

    def test_never_exceeds_the_tank_or_the_range(self):
        candidates = [stop(mile, 3.0 + (mile % 7) / 10) for mile in range(20, 1400, 40)]
        plan = self.plan(candidates, 1400)
        self.assertAlmostEqual(plan.total_gallons, 140.0, places=6)
        miles = [0.0] + [p.node.mile_marker for p in plan.purchases] + [1400.0]
        self.assertTrue(all(b - a <= 500 + 1e-6 for a, b in zip(miles, miles[1:])))

    def test_a_full_tank_at_the_start_buys_nothing_on_a_short_hop(self):
        plan = self.plan([stop(10, 3.00)], 100, start_fuel_gallons=50.0)
        self.assertEqual(plan.purchases, [])
        self.assertAlmostEqual(plan.total_cost, 0.0)

    def test_origin_fill_is_priced_at_the_cheapest_stop_near_the_start(self):
        plan = self.plan([stop(5, 4.00), stop(20, 3.10), stop(400, 2.00)], 800)
        first = plan.purchases[0].node
        self.assertTrue(first.is_origin_fill)
        self.assertAlmostEqual(first.price, 3.10)

    def test_gap_longer_than_the_range_is_rejected(self):
        with self.assertRaises(InfeasibleRouteError) as caught:
            self.plan([stop(10, 3.00), stop(900, 3.00)], 1000)
        self.assertEqual(caught.exception.detail["reason"], "range_gap")
        self.assertAlmostEqual(caught.exception.detail["gap_miles"], 890.0)

    def test_final_leg_longer_than_the_range_is_rejected(self):
        with self.assertRaises(InfeasibleRouteError) as caught:
            self.plan([stop(10, 3.00)], 800)
        self.assertEqual(caught.exception.detail["reason"], "range_gap_to_destination")

    def test_no_candidates_is_rejected(self):
        with self.assertRaises(InfeasibleRouteError) as caught:
            self.plan([], 100)
        self.assertEqual(caught.exception.detail["reason"], "no_stations_in_corridor")

    def test_duplicate_mile_markers_collapse_to_the_cheapest(self):
        # Ten stations share a city centroid; only the cheapest should be used.
        candidates = [stop(40.0, 4.50 - i / 10, f"s{i}") for i in range(10)]
        candidates.append(stop(300.0, 5.00))
        plan = self.plan(candidates, 400)
        prices = {round(p.node.price, 2) for p in plan.purchases}
        self.assertIn(3.60, prices)  # 4.50 - 0.9
        self.assertNotIn(4.50, prices)

    def test_stops_are_ordered_along_the_route(self):
        candidates = [stop(mile, 3.5 - (mile % 300) / 1000) for mile in range(50, 1500, 50)]
        plan = self.plan(candidates, 1500)
        miles = [p.node.mile_marker for p in plan.purchases]
        self.assertEqual(miles, sorted(miles))


class StopCostTests(SimpleTestCase):
    """The practical plan: a fixed cost per stop, and the detour to reach it."""

    def test_a_stop_penalty_trades_a_little_fuel_for_far_fewer_stops(self):
        # Prices drift down a cent every 25 miles: the pure cheapest plan tops
        # up at almost every station, which no driver would do.
        candidates = [stop(mile, 4.00 - mile / 2500) for mile in range(25, 1500, 25)]
        cheapest = plan_fuel_stops(candidates, 1500, mpg=10, range_miles=500)
        practical = plan_fuel_stops(
            candidates, 1500, mpg=10, range_miles=500, stop_penalty=5.0
        )
        self.assertGreater(len(cheapest.purchases), 10)
        self.assertLessEqual(len(practical.purchases), 5)
        # It pays a little more for fuel...
        self.assertGreaterEqual(practical.total_cost, cheapest.total_cost - 1e-9)
        # ...and is the better plan once each stop is priced at $5.
        def with_stop_costs(plan):
            return plan.total_cost + 5.0 * sum(1 for p in plan.purchases if not p.node.is_origin_fill)
        self.assertLess(with_stop_costs(practical), with_stop_costs(cheapest))
        self.assertAlmostEqual(practical.total_gallons, 150.0, places=6)

    def test_a_cheap_station_far_off_the_route_can_lose_to_a_close_one(self):
        # Both need ~30 gallons. 2 cents a gallon saves $0.60; a 14 mile detour
        # each way burns 2.8 gallons ($8.40). The close station should win.
        far = stop(200, 3.00, "far", detour=14.0)
        close = stop(210, 3.02, "close", detour=0.5)
        origin_only = stop(1, 3.50, "start", detour=0.0)
        candidates = [origin_only, far, close]

        naive = plan_fuel_stops(candidates, 500 + 200, mpg=10, range_miles=500)
        aware = plan_fuel_stops(
            candidates, 500 + 200, mpg=10, range_miles=500, count_detours=True
        )
        naive_names = {p.node.candidate.station.name for p in naive.purchases if p.node.candidate}
        aware_names = {p.node.candidate.station.name for p in aware.purchases if p.node.candidate}
        self.assertIn("far", naive_names)
        self.assertIn("close", aware_names)
        self.assertNotIn("far", aware_names)

    def test_the_objective_is_fuel_plus_stop_costs(self):
        candidates = [stop(mile, 3.0 + (mile % 7) / 20, detour=2.0) for mile in range(40, 1200, 40)]
        plan = plan_fuel_stops(
            candidates, 1200, mpg=10, range_miles=500, stop_penalty=5.0, count_detours=True
        )
        real_stops = [p for p in plan.purchases if not p.node.is_origin_fill]
        stop_costs = sum(5.0 + 2 * 2.0 / 10 * p.node.price for p in real_stops)
        self.assertAlmostEqual(plan.objective, plan.total_cost + stop_costs, places=6)
