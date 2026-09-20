from django.test import SimpleTestCase

from routeplanner.services import geo, service_area


class GeoTests(SimpleTestCase):
    def test_haversine_matches_a_known_distance(self):
        # Dallas -> Houston is about 225 miles as the crow flies.
        miles = geo.haversine_miles(32.7767, -96.7970, 29.7604, -95.3698)
        self.assertAlmostEqual(miles, 225, delta=5)

    def test_decode_polyline_precision_5(self):
        # The canonical Google example.
        points = geo.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[0][0], 38.5, places=5)
        self.assertAlmostEqual(points[0][1], -120.2, places=5)
        self.assertAlmostEqual(points[2][0], 43.252, places=5)
        self.assertAlmostEqual(points[2][1], -126.453, places=5)

    def test_cumulative_miles_is_monotonic_and_starts_at_zero(self):
        points = [(32.0, -96.0), (33.0, -96.0), (34.0, -96.0)]
        cumulative = geo.cumulative_miles(points)
        self.assertEqual(cumulative[0], 0.0)
        self.assertAlmostEqual(cumulative[-1], 138, delta=2)
        self.assertEqual(cumulative, sorted(cumulative))

    def test_sample_points_keeps_the_ends(self):
        points = [(32.0 + i / 100, -96.0) for i in range(1000)]
        cumulative = geo.cumulative_miles(points)
        sampled = geo.sample_points(points, cumulative, 50)
        self.assertLessEqual(len(sampled), 51)
        self.assertEqual(sampled[0][:2], points[0])
        self.assertEqual(sampled[-1][:2], points[-1])

    def test_simplify_path_collapses_a_straight_line(self):
        points = [(32.0 + i / 100, -96.0) for i in range(200)]
        self.assertEqual(len(geo.simplify_path(points)), 2)

    def test_simplify_path_keeps_corners(self):
        points = [(32.0, -96.0), (32.0, -95.0), (33.0, -95.0), (33.0, -94.0)]
        self.assertEqual(len(geo.simplify_path(points)), 4)

    def test_bounding_box_is_a_coarse_first_filter(self):
        self.assertTrue(service_area.in_bounding_box(32.77, -96.79))  # Dallas
        self.assertTrue(service_area.in_bounding_box(61.2, -149.9))  # Anchorage
        self.assertTrue(service_area.in_bounding_box(21.3, -157.8))  # Honolulu
        self.assertFalse(service_area.in_bounding_box(19.43, -99.13))  # Mexico City
        self.assertFalse(service_area.in_bounding_box(51.5, -0.12))  # London
