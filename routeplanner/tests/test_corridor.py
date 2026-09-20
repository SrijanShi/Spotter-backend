from django.test import SimpleTestCase

from routeplanner.services import geo
from routeplanner.services.corridor import Station, StationIndex


def station(name: str, latitude: float, longitude: float, price: float = 3.0) -> Station:
    return Station(
        opis_id=name,
        name=name,
        address="",
        city=name,
        state="TX",
        latitude=latitude,
        longitude=longitude,
        price=price,
    )


class StationIndexTests(SimpleTestCase):
    def setUp(self):
        # A due-west to due-east line along latitude 32, roughly 350 miles long.
        self.points = [(32.0, -96.0 + i / 100) for i in range(700)]
        self.cumulative = geo.cumulative_miles(self.points)
        self.sampled = geo.sample_points(self.points, self.cumulative, 4000)

    def test_finds_stations_in_the_corridor_and_ignores_the_rest(self):
        index = StationIndex(
            [
                station("on-route", 32.0, -95.0),
                station("just-off", 32.1, -94.0),  # ~7 miles north
                station("far-away", 35.0, -95.0),  # ~200 miles north
            ]
        )
        found = {c.station.name for c in index.candidates_along(self.sampled, 15.0)}
        self.assertEqual(found, {"on-route", "just-off"})

    def test_mile_marker_matches_the_distance_along_the_route(self):
        index = StationIndex([station("halfway", 32.0, -92.5)])
        candidate = index.candidates_along(self.sampled, 15.0)[0]
        expected = geo.haversine_miles(32.0, -96.0, 32.0, -92.5)
        self.assertAlmostEqual(candidate.mile_marker, expected, delta=1.0)
        self.assertAlmostEqual(candidate.detour_miles, 0.0, delta=0.5)

    def test_a_station_is_reported_once_at_its_closest_approach(self):
        # A route that doubles back past the same town.
        there_and_back = self.points + list(reversed(self.points))
        cumulative = geo.cumulative_miles(there_and_back)
        sampled = geo.sample_points(there_and_back, cumulative, 4000)
        index = StationIndex([station("passed-twice", 32.05, -93.0)])
        candidates = index.candidates_along(sampled, 15.0)
        self.assertEqual(len(candidates), 1)
        self.assertLess(candidates[0].detour_miles, 5.0)

    def test_candidates_come_back_sorted_by_mile_marker(self):
        index = StationIndex(
            [station(f"s{i}", 32.0, -96.0 + i / 2, price=3.0 + i / 10) for i in range(8)]
        )
        candidates = index.candidates_along(self.sampled, 20.0)
        self.assertEqual(
            [c.mile_marker for c in candidates],
            sorted(c.mile_marker for c in candidates),
        )

    def test_corridor_width_is_respected(self):
        index = StationIndex([station("nine-miles-north", 32.13, -94.5)])
        self.assertEqual(index.candidates_along(self.sampled, 5.0), [])
        self.assertEqual(len(index.candidates_along(self.sampled, 15.0)), 1)
