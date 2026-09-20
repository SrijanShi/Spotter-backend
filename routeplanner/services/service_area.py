"""Is a location inside the area this API serves?

The brief says both endpoints are within the USA, so inputs outside it are
rejected rather than silently routed. Two cheap offline checks, no API call:

1. Point-in-polygon against a 1:50m US outline (Natural Earth, public domain).
2. If that fails - and it does for border towns like El Paso and Brownsville,
   where a 50m coastline is a few kilometres out - accept the point when a US
   truck stop from the price file sits within 25 miles of it. Canadian cities
   fail both: the nearest US truck stop to Toronto is 43 miles away.
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from .geo import US_BOUNDS

# A US truck stop this close means the point is in (or effectively on) US soil.
NEAR_STATION_MILES = 25.0

_lock = threading.Lock()
_rings: list[list[tuple[float, float]]] | None = None
_ring_boxes: list[tuple[float, float, float, float]] = []


def _load_rings() -> None:
    """Load the boundary once, with a bounding box per ring to skip most work."""
    global _rings, _ring_boxes
    if _rings is not None:
        return
    with _lock:
        if _rings is not None:
            return
        path = Path(settings.DATA_DIR) / "us_boundary.json"
        try:
            payload = json.loads(path.read_text())
            rings = [[(point[0], point[1]) for point in ring] for ring in payload["rings"]]
        except (OSError, ValueError, KeyError):
            rings = []
        boxes = []
        for ring in rings:
            longitudes = [point[0] for point in ring]
            latitudes = [point[1] for point in ring]
            boxes.append((min(longitudes), min(latitudes), max(longitudes), max(latitudes)))
        _ring_boxes = boxes
        _rings = rings


def in_bounding_box(latitude: float, longitude: float) -> bool:
    return any(
        min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon
        for min_lat, min_lon, max_lat, max_lon in US_BOUNDS
    )


def in_us_outline(latitude: float, longitude: float) -> bool:
    """Ray casting against the national outline."""
    _load_rings()
    if not _rings:
        return False
    for ring, (min_lon, min_lat, max_lon, max_lat) in zip(_rings, _ring_boxes):
        if not (min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon):
            continue
        inside = False
        count = len(ring)
        previous = count - 1
        for current in range(count):
            x1, y1 = ring[current]
            x2, y2 = ring[previous]
            if (y1 > latitude) != (y2 > latitude):
                crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
                if longitude < crossing:
                    inside = not inside
            previous = current
        if inside:
            return True
    return False


def near_a_us_truck_stop(latitude: float, longitude: float) -> bool:
    from .corridor import get_station_index

    index = get_station_index()
    distance = index.nearest_station_miles(latitude, longitude, NEAR_STATION_MILES)
    return distance is not None


def in_service_area(latitude: float, longitude: float) -> bool:
    if not in_bounding_box(latitude, longitude):
        return False
    if in_us_outline(latitude, longitude):
        return True
    return near_a_us_truck_stop(latitude, longitude)


@lru_cache(maxsize=1)
def boundary_vertex_count() -> int:
    _load_rings()
    return sum(len(ring) for ring in (_rings or []))
