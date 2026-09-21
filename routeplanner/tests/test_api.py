"""API contract tests.

The routing provider is mocked, so the suite never touches the network and runs
in well under a second. Locations are passed as `lat,lon` so geocoding is a
no-op too.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from routeplanner.models import FuelStation
from routeplanner.services import geo
from routeplanner.services.corridor import reset_station_index
from routeplanner.services.routing import RouteResult, RoutingError

# A straight line due east along latitude 35, from -100 to -88 (~680 miles).
ROUTE_POINTS = [(35.0, -100.0 + i / 10) for i in range(121)]
ROUTE_MILES = geo.cumulative_miles(ROUTE_POINTS)[-1]

START = "35.0,-100.0"
FINISH = "35.0,-88.0"


def fake_route(*args, **kwargs):
    return RouteResult(
        points=list(ROUTE_POINTS),
        distance_miles=ROUTE_MILES,
        duration_hours=ROUTE_MILES / 60,
        provider="test",
    )


class RoutePlanApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        prices = [3.90, 3.10, 3.60, 2.95, 3.40, 3.80]
        for index, price in enumerate(prices):
            FuelStation.objects.create(
                opis_id=str(index),
                name=f"Truck Stop {index}",
                address=f"I-40, EXIT {index}",
                city=f"Town {index}",
                state="OK",
                latitude=35.02,
                longitude=-99.0 + index * 2,
                retail_price=f"{price:.3f}",
            )

    def setUp(self):
        cache.clear()
        reset_station_index()
        self.addCleanup(cache.clear)
        self.addCleanup(reset_station_index)
        self.url = reverse("api:route")

    def plan(self, **params):
        query = {"start": START, "finish": FINISH, **params}
        return self.client.get(self.url, query)

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_happy_path(self, fetch):
        response = self.plan()
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertAlmostEqual(body["route"]["distance_miles"], round(ROUTE_MILES, 1), places=1)
        self.assertEqual(body["vehicle"]["mpg"], 10.0)
        self.assertEqual(body["vehicle"]["range_miles"], 500.0)

        # Every mile of the trip is paid for: gallons == distance / mpg.
        self.assertAlmostEqual(body["totals"]["gallons"], ROUTE_MILES / 10, places=1)
        self.assertGreater(body["totals"]["fuel_cost"], 0)
        self.assertEqual(body["totals"]["stops"], len(body["fuel_stops"]))

        # Stops run forward along the route and never more than a tank apart.
        miles = [stop["mile_marker"] for stop in body["fuel_stops"]]
        self.assertEqual(miles, sorted(miles))
        self.assertTrue(all(b - a <= 500 for a, b in zip(miles, miles[1:])))

        # A map of the route is part of every answer.
        self.assertIn("/map/?", body["map_url"])
        self.assertTrue(body["map_url"].startswith("http"))

        # One routing call, no geocoding calls (coordinates were supplied).
        self.assertEqual(body["meta"]["external_api_calls"], 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(body["meta"]["cached"])
        self.assertIn("compute_ms", body["meta"])

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_it_buys_at_the_cheapest_stations_in_reach(self, fetch):
        body = self.plan().json()
        prices = [stop["price_per_gallon"] for stop in body["fuel_stops"]]
        # The $3.90 station is the most expensive on the line and should not be
        # where the bulk of the fuel is bought.
        most_gallons = max(body["fuel_stops"], key=lambda stop: stop["gallons"])
        self.assertLess(most_gallons["price_per_gallon"], max(prices) + 0.001)
        self.assertLessEqual(most_gallons["price_per_gallon"], 3.10)

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_second_identical_request_is_served_from_cache(self, fetch):
        self.assertEqual(self.plan().status_code, 200)
        body = self.plan().json()
        self.assertTrue(body["meta"]["cached"])
        self.assertEqual(body["meta"]["external_api_calls"], 0)
        self.assertEqual(body["meta"]["routing_api_ms"], 0.0)
        self.assertEqual(fetch.call_count, 1)  # no second call to the router

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_refresh_bypasses_the_cache(self, fetch):
        self.plan()
        self.plan(refresh="true")
        self.assertEqual(fetch.call_count, 2)

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_geometry_can_be_omitted(self, fetch):
        body = self.plan(include_geometry="false").json()
        self.assertNotIn("geometry", body["route"])
        full = self.plan(include_geometry="true", refresh="true").json()
        self.assertEqual(full["route"]["geometry"]["type"], "LineString")
        self.assertGreater(len(full["route"]["geometry"]["coordinates"]), 1)

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_post_works_too(self, fetch):
        response = self.client.post(
            self.url, {"start": START, "finish": FINISH}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)

    def test_missing_parameters_are_rejected(self):
        response = self.client.get(self.url, {"start": START})
        self.assertEqual(response.status_code, 400)
        self.assertIn("finish", response.json())

    def test_detour_over_the_limit_is_rejected(self):
        response = self.plan(max_detour_miles="500")
        self.assertEqual(response.status_code, 400)

    def test_locations_outside_the_usa_are_rejected(self):
        response = self.plan(start="43.65,-79.38")  # Toronto
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "outside_united_states")

    @mock.patch(
        "routeplanner.services.planner.fetch_route",
        side_effect=RoutingError("all providers down"),
    )
    def test_routing_failure_is_surfaced_as_502(self, fetch):
        response = self.plan()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "routing_unavailable")

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_a_range_no_station_can_bridge_returns_422(self, fetch):
        response = self.plan(range_miles="60", max_detour_miles="5")
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"], "route_not_fuelable")
        self.assertIn("reason", body)
        self.assertIn("gap_miles", body)

    @mock.patch("routeplanner.services.planner.fetch_route", side_effect=fake_route)
    def test_corridor_widens_before_giving_up(self, fetch):
        # At 1 mile the stations (2 miles off the line) are invisible; the
        # planner should widen the corridor and still return a plan.
        response = self.plan(max_detour_miles="1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreater(body["meta"]["max_detour_miles"], 1)
        self.assertTrue(body["meta"]["warnings"])


class EmptyDatasetTests(TestCase):
    def setUp(self):
        cache.clear()
        reset_station_index()
        self.addCleanup(reset_station_index)

    def test_api_reports_missing_fuel_data(self):
        response = self.client.get(reverse("api:route"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "no_fuel_data")


class HealthTests(TestCase):
    def setUp(self):
        reset_station_index()
        self.addCleanup(reset_station_index)

    def test_health(self):
        response = self.client.get(reverse("api:health"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("osrm", body["routing_providers"])


class MapPageTests(TestCase):
    def test_map_page_renders(self):
        response = self.client.get(reverse("map"), {"start": "Dallas, TX", "finish": "Tulsa, OK"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fuel-Optimal Route Planner")
        self.assertContains(response, "Dallas, TX")
