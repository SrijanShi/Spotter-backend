from django.test import TestCase

from routeplanner.models import FuelStation
from routeplanner.services import service_area
from routeplanner.services.corridor import reset_station_index


class ServiceAreaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A truck stop right on the Rio Grande in El Paso: the 1:50m coastline
        # puts the city marginally on the wrong side of the border, and this is
        # what rescues it.
        FuelStation.objects.create(
            opis_id="1",
            name="El Paso Travel Center",
            city="El Paso",
            state="TX",
            latitude=31.7619,
            longitude=-106.485,
            retail_price="3.100",
        )

    def setUp(self):
        reset_station_index()
        self.addCleanup(reset_station_index)

    def test_outline_accepts_us_points(self):
        self.assertTrue(service_area.in_us_outline(32.7767, -96.797))  # Dallas
        self.assertTrue(service_area.in_us_outline(39.739, -104.99))  # Denver
        self.assertTrue(service_area.in_us_outline(24.5551, -81.78))  # Key West

    def test_outline_rejects_canada_and_mexico(self):
        self.assertFalse(service_area.in_us_outline(43.65, -79.38))  # Toronto
        self.assertFalse(service_area.in_us_outline(49.28, -123.12))  # Vancouver
        self.assertFalse(service_area.in_us_outline(19.43, -99.13))  # Mexico City

    def test_border_town_is_rescued_by_a_nearby_truck_stop(self):
        self.assertFalse(service_area.in_us_outline(31.7619, -106.485))
        self.assertTrue(service_area.in_service_area(31.7619, -106.485))

    def test_coastal_cities_are_rescued_the_same_way(self):
        # A 1:50m coastline clips Manhattan; the truck stops around it do not.
        self.assertFalse(service_area.in_us_outline(40.7128, -74.006))
        FuelStation.objects.create(
            opis_id="2",
            name="Newark Truck Plaza",
            city="Newark",
            state="NJ",
            latitude=40.735,
            longitude=-74.172,
            retail_price="3.500",
        )
        reset_station_index()
        self.assertTrue(service_area.in_service_area(40.7128, -74.006))

    def test_canadian_cities_are_outside_the_service_area(self):
        for latitude, longitude in ((43.65, -79.38), (45.5, -73.57), (49.28, -123.12)):
            self.assertFalse(service_area.in_service_area(latitude, longitude))

    def test_far_away_points_fail_the_bounding_box_first(self):
        self.assertFalse(service_area.in_service_area(51.5, -0.12))  # London

    def test_the_boundary_file_actually_loaded(self):
        self.assertGreater(service_area.boundary_vertex_count(), 1000)
