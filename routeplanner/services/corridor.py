"""Find the fuel stations that sit close to a route.

The whole station list (~6.5k rows) is held in a grid-hashed in-memory index that
is built once per process, so matching a 1,500-mile route takes tens of
milliseconds and touches the database zero times per request.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Iterable, Sequence

from django.conf import settings

from .geo import haversine_miles

logger = logging.getLogger(__name__)

MILES_PER_LAT_DEGREE = 69.0


@dataclass(frozen=True)
class Station:
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: float


@dataclass
class CandidateStop:
    """A station projected onto the route."""

    station: Station
    mile_marker: float
    detour_miles: float


class StationIndex:
    def __init__(self, stations: Iterable[Station], cell_degrees: float | None = None):
        self.cell_degrees = cell_degrees or settings.ROUTE_PLANNER["GRID_CELL_DEGREES"]
        self.stations: list[Station] = list(stations)
        self._grid: dict[tuple[int, int], list[int]] = {}
        for position, station in enumerate(self.stations):
            self._grid.setdefault(self._cell(station.latitude, station.longitude), []).append(
                position
            )

    def _cell(self, latitude: float, longitude: float) -> tuple[int, int]:
        size = self.cell_degrees
        return (math.floor(latitude / size), math.floor(longitude / size))

    def __len__(self) -> int:
        return len(self.stations)

    @classmethod
    def from_database(cls) -> "StationIndex":
        from ..models import FuelStation

        rows = FuelStation.objects.values_list(
            "opis_id", "name", "address", "city", "state", "latitude", "longitude", "retail_price"
        )
        stations = [
            Station(
                opis_id=opis_id,
                name=name,
                address=address,
                city=city,
                state=state,
                latitude=latitude,
                longitude=longitude,
                price=float(price),
            )
            for opis_id, name, address, city, state, latitude, longitude, price in rows.iterator(
                chunk_size=2000
            )
        ]
        return cls(stations)

    def candidates_along(
        self,
        sampled_points: Sequence[tuple[float, float, float]],
        max_detour_miles: float,
    ) -> list[CandidateStop]:
        """Stations within ``max_detour_miles`` of the sampled route.

        Each station is reported once, at the point of its closest approach, so a
        route that passes a town twice does not produce a duplicate stop.
        """
        best: dict[int, tuple[float, float]] = {}  # index -> (mile_marker, detour)
        size = self.cell_degrees
        lat_span = int(max_detour_miles / (MILES_PER_LAT_DEGREE * size)) + 1
        grid_get = self._grid.get
        stations = self.stations

        for latitude, longitude, mile in sampled_points:
            miles_per_lon_degree = MILES_PER_LAT_DEGREE * max(math.cos(math.radians(latitude)), 0.1)
            lon_span = int(max_detour_miles / (miles_per_lon_degree * size)) + 1
            cell_lat, cell_lon = self._cell(latitude, longitude)
            for i in range(cell_lat - lat_span, cell_lat + lat_span + 1):
                for j in range(cell_lon - lon_span, cell_lon + lon_span + 1):
                    for position in grid_get((i, j), ()):
                        station = stations[position]
                        detour = haversine_miles(
                            latitude, longitude, station.latitude, station.longitude
                        )
                        if detour > max_detour_miles:
                            continue
                        current = best.get(position)
                        if current is None or detour < current[1]:
                            best[position] = (mile, detour)

        candidates = [
            CandidateStop(station=stations[position], mile_marker=mile, detour_miles=detour)
            for position, (mile, detour) in best.items()
        ]
        candidates.sort(key=lambda candidate: (candidate.mile_marker, candidate.station.price))
        return candidates

    def nearest_station_miles(
        self, latitude: float, longitude: float, max_miles: float
    ) -> float | None:
        """Distance to the closest station, or None if none is within ``max_miles``."""
        size = self.cell_degrees
        lat_span = int(max_miles / (MILES_PER_LAT_DEGREE * size)) + 1
        miles_per_lon_degree = MILES_PER_LAT_DEGREE * max(math.cos(math.radians(latitude)), 0.1)
        lon_span = int(max_miles / (miles_per_lon_degree * size)) + 1
        cell_lat, cell_lon = self._cell(latitude, longitude)

        best: float | None = None
        for i in range(cell_lat - lat_span, cell_lat + lat_span + 1):
            for j in range(cell_lon - lon_span, cell_lon + lon_span + 1):
                for position in self._grid.get((i, j), ()):
                    station = self.stations[position]
                    distance = haversine_miles(
                        latitude, longitude, station.latitude, station.longitude
                    )
                    if distance <= max_miles and (best is None or distance < best):
                        best = distance
        return best


_index: StationIndex | None = None
_lock = threading.Lock()


def get_station_index(force_reload: bool = False) -> StationIndex:
    """Process-wide station index, built on first use (or at startup)."""
    global _index
    if _index is None or force_reload:
        with _lock:
            if _index is None or force_reload:
                _index = StationIndex.from_database()
                logger.info("Station index built with %s stations", len(_index))
    return _index


def reset_station_index() -> None:
    """Drop the cached index - used by tests and after a data import."""
    global _index
    with _lock:
        _index = None
