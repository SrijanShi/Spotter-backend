from unittest import mock

from django.test import TestCase

from routeplanner.models import FuelStation, GeocodeCache
from routeplanner.services.corridor import reset_station_index
from routeplanner.services.geocoding import (
    GeocodingError,
    OutsideUnitedStatesError,
    geocode,
)


def nominatim_response(results):
    response = mock.Mock()
    response.json.return_value = results
    response.raise_for_status.return_value = None
    return response


DALLAS = [
    {
        "lat": "32.7767",
        "lon": "-96.7970",
        "display_name": "Dallas, Texas, United States",
        "address": {"country_code": "us"},
    }
]
TORONTO = [
    {
        "lat": "43.6532",
        "lon": "-79.3832",
        "display_name": "Toronto, Ontario, Canada",
        "address": {"country_code": "ca"},
    }
]


class GeocodeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        FuelStation.objects.create(
            opis_id="1",
            name="Dallas Truck Stop",
            city="Dallas",
            state="TX",
            latitude=32.78,
            longitude=-96.80,
            retail_price="3.100",
        )

    def setUp(self):
        reset_station_index()
        self.addCleanup(reset_station_index)

    def test_coordinates_are_used_directly_with_no_api_call(self):
        with mock.patch("routeplanner.services.geocoding.requests.get") as get:
            place = geocode("32.7767,-96.7970")
        get.assert_not_called()
        self.assertEqual(place.api_calls, 0)
        self.assertAlmostEqual(place.latitude, 32.7767)

    def test_city_state_input_is_answered_offline(self):
        with mock.patch("routeplanner.services.geocoding.requests.get") as get:
            for query in ("Dallas, TX", "dallas, texas", "Dallas, TX 75201, USA"):
                place = geocode(query)
                self.assertEqual(place.api_calls, 0, query)
                self.assertAlmostEqual(place.latitude, 32.79, delta=0.1)
                self.assertAlmostEqual(place.longitude, -96.77, delta=0.1)
        get.assert_not_called()

    def test_offline_lookup_handles_census_naming(self):
        # "Oklahoma City city" and "St. Louis city" in the gazetteer.
        for query in ("Oklahoma City, OK", "St. Louis, MO", "Saint Louis, MO", "New York, NY"):
            self.assertEqual(geocode(query).api_calls, 0, query)

    def test_canadian_province_is_rejected_without_a_call(self):
        with mock.patch("routeplanner.services.geocoding.requests.get") as get:
            with self.assertRaises(OutsideUnitedStatesError):
                geocode("Toronto, ON")
        get.assert_not_called()

    def test_other_inputs_are_geocoded_online_once_then_cached(self):
        with mock.patch(
            "routeplanner.services.geocoding.requests.get",
            return_value=nominatim_response(DALLAS),
        ) as get:
            first = geocode("Dallas Love Field")
            second = geocode("dallas love field")  # case-insensitive cache hit
        self.assertEqual(get.call_count, 1)
        self.assertEqual(first.api_calls, 1)
        self.assertEqual(second.api_calls, 0)
        self.assertEqual(GeocodeCache.objects.count(), 1)

    def test_a_foreign_place_is_rejected_rather_than_mapped_to_a_namesake(self):
        # Searching the world and filtering to US results is what stops
        # "Toronto, ON" from quietly becoming Toronto, Ohio.
        with mock.patch(
            "routeplanner.services.geocoding.requests.get",
            return_value=nominatim_response(TORONTO),
        ):
            with self.assertRaises(OutsideUnitedStatesError):
                geocode("CN Tower")
        self.assertEqual(GeocodeCache.objects.count(), 0)

    def test_coordinates_outside_the_usa_are_rejected(self):
        with self.assertRaises(OutsideUnitedStatesError):
            geocode("43.6532,-79.3832")

    def test_unknown_place_raises(self):
        with mock.patch(
            "routeplanner.services.geocoding.requests.get",
            return_value=nominatim_response([]),
        ):
            with self.assertRaises(GeocodingError):
                geocode("Zzzz Nowhere Town")

    def test_empty_input_raises(self):
        with self.assertRaises(GeocodingError):
            geocode("   ")
