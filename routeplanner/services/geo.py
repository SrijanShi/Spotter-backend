"""Small geospatial helpers: distances, polyline decoding, US bounds."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344

# Generous bounding boxes covering the contiguous US, Alaska and Hawaii. Used as
# a cheap first filter; `service_area.py` does the precise test.
US_BOUNDS = (
    (24.3, -125.1, 49.6, -66.8),  # contiguous
    (51.0, -180.0, 71.6, -129.0),  # Alaska
    (18.8, -160.4, 22.3, -154.7),  # Hawaii
)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def cumulative_miles(points: Sequence[tuple[float, float]]) -> list[float]:
    """Running distance along a (lat, lon) path, starting at 0."""
    cumulative = [0.0]
    for i in range(1, len(points)):
        lat1, lon1 = points[i - 1]
        lat2, lon2 = points[i]
        cumulative.append(cumulative[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return cumulative


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode a Google/OSRM encoded polyline into (lat, lon) pairs.

    OSRM's ``polyline6`` geometry is ~5x smaller than GeoJSON over the wire, which
    matters a lot on the free routing servers.
    """
    coordinates: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = float(10**precision)
    length = len(encoded)

    while index < length:
        for is_longitude in (False, True):
            result = 1
            shift = 0
            while True:
                byte = ord(encoded[index]) - 63 - 1
                index += 1
                result += byte << shift
                shift += 5
                if byte < 0x1F:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_longitude:
                lon += delta
            else:
                lat += delta
        coordinates.append((lat / factor, lon / factor))
    return coordinates


def bounding_box(points: Iterable[tuple[float, float]]) -> list[float]:
    """[min_lon, min_lat, max_lon, max_lat] - GeoJSON bbox order."""
    lats = []
    lons = []
    for lat, lon in points:
        lats.append(lat)
        lons.append(lon)
    return [min(lons), min(lats), max(lons), max(lats)]


def simplify_path(
    points: Sequence[tuple[float, float]], tolerance_miles: float = 0.05
) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker, iterative so long routes cannot blow the stack.

    A 1,500 mile route comes back from the router as ~21,000 points; thinning it
    to the shape the eye can actually see keeps the JSON response small.
    """
    if len(points) < 3:
        return list(points)

    # Work in degrees: latitude is ~69 miles/degree, longitude shrinks with latitude.
    mean_lat = sum(p[0] for p in points) / len(points)
    lat_scale = 69.0
    lon_scale = 69.0 * max(math.cos(math.radians(mean_lat)), 0.1)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ax, ay = points[start][1] * lon_scale, points[start][0] * lat_scale
        bx, by = points[end][1] * lon_scale, points[end][0] * lat_scale
        dx, dy = bx - ax, by - ay
        segment_length = math.hypot(dx, dy)
        farthest_index = -1
        farthest_distance = 0.0
        for i in range(start + 1, end):
            px, py = points[i][1] * lon_scale, points[i][0] * lat_scale
            if segment_length == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dy * px - dx * py + bx * ay - by * ax) / segment_length
            if distance > farthest_distance:
                farthest_distance = distance
                farthest_index = i
        if farthest_distance > tolerance_miles and farthest_index > 0:
            keep[farthest_index] = True
            stack.append((start, farthest_index))
            stack.append((farthest_index, end))

    return [point for point, keeper in zip(points, keep) if keeper]


def sample_points(
    points: Sequence[tuple[float, float]], cumulative: Sequence[float], max_points: int
) -> list[tuple[float, float, float]]:
    """Thin a route down to at most ``max_points`` (lat, lon, mile) triples.

    The last point is always kept so the corridor scan covers the full route.
    """
    total = len(points)
    if total <= max_points:
        return [(points[i][0], points[i][1], cumulative[i]) for i in range(total)]
    step = math.ceil(total / max_points)
    indexes = list(range(0, total, step))
    if indexes[-1] != total - 1:
        indexes.append(total - 1)
    return [(points[i][0], points[i][1], cumulative[i]) for i in indexes]
