"""Routing provider behaviour: retries, errors and exact call counting."""

from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings
from django.conf import settings

from routeplanner.services.routing import NoRouteError, RoutingError, fetch_route

DALLAS = (32.7767, -96.797)
HOUSTON = (29.7604, -95.3698)


def response(status: int, payload: dict) -> mock.Mock:
    reply = mock.Mock(status_code=status)
    reply.json.return_value = payload
    return reply


OSRM_OK = {
    "code": "Ok",
    "routes": [{"geometry": "_p~iF~ps|U_ulLnnqC_mqNvxq`@", "distance": 362_000.0, "duration": 13_000.0}],
}


def planner_settings(**overrides):
    return override_settings(ROUTE_PLANNER={**settings.ROUTE_PLANNER, **overrides})


@planner_settings(ORS_API_KEY="")
class OsrmOnlyTests(SimpleTestCase):
    @mock.patch("routeplanner.services.routing.requests.request")
    def test_a_normal_route_is_exactly_one_call(self, request):
        request.return_value = response(200, OSRM_OK)
        result = fetch_route(DALLAS, HOUSTON)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.api_calls, 1)
        self.assertEqual(result.provider, "osrm")

    @mock.patch("routeplanner.services.routing.requests.request")
    def test_a_5xx_is_retried_once_and_both_calls_are_counted(self, request):
        request.side_effect = [response(503, {}), response(200, OSRM_OK)]
        result = fetch_route(DALLAS, HOUSTON)
        self.assertEqual(result.api_calls, 2)

    @mock.patch("routeplanner.services.routing.requests.request")
    def test_no_route_is_not_retried(self, request):
        request.return_value = response(400, {"code": "NoRoute", "message": "Impossible route"})
        with self.assertRaises(NoRouteError) as caught:
            fetch_route(DALLAS, HOUSTON)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(caught.exception.api_calls, 1)

    @mock.patch("routeplanner.services.routing.requests.request")
    def test_a_network_failure_is_an_outage_not_a_missing_road(self, request):
        request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(RoutingError) as caught:
            fetch_route(DALLAS, HOUSTON)
        self.assertNotIsInstance(caught.exception, NoRouteError)
        self.assertEqual(request.call_count, 2)


@planner_settings(ORS_API_KEY="test-key")
class OrsWithFallbackTests(SimpleTestCase):
    @mock.patch("routeplanner.services.routing.requests.request")
    def test_an_ors_client_error_falls_back_to_osrm_without_a_retry(self, request):
        request.side_effect = [
            response(400, {"error": {"code": 2004, "message": "distance limit exceeded"}}),
            response(200, OSRM_OK),
        ]
        result = fetch_route(DALLAS, HOUSTON)
        self.assertEqual(result.provider, "osrm")
        self.assertEqual(result.api_calls, 2)  # one ORS, one OSRM - no retry of the 400
        self.assertTrue(result.warnings)

    @mock.patch("routeplanner.services.routing.requests.request")
    def test_no_road_anywhere_is_reported_as_no_route(self, request):
        request.side_effect = [
            response(404, {"error": {"code": 2009, "message": "Route could not be found"}}),
            response(400, {"code": "NoRoute", "message": "Impossible route"}),
        ]
        with self.assertRaises(NoRouteError) as caught:
            fetch_route(DALLAS, HOUSTON)
        self.assertEqual(caught.exception.api_calls, 2)
