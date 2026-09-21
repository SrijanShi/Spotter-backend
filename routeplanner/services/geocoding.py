"""Turn user-supplied locations into coordinates.

Costs zero API calls for ``lat,lon`` input, for "City, ST" input (answered from
the offline Census place file - see ``places.py``), and for any place name that
has been resolved before (cached in the database permanently). Only addresses
and landmarks the offline data cannot answer reach an online geocoder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests
from django.conf import settings

from ..models import GeocodeCache
from . import places
from .service_area import in_service_area

logger = logging.getLogger(__name__)

COORDINATE_RE = re.compile(
    r"^\s*(?P<lat>-?\d{1,2}(?:\.\d+)?)\s*,\s*(?P<lon>-?\d{1,3}(?:\.\d+)?)\s*$"
)


class GeocodingError(ValueError):
    """The location could not be resolved."""


class OutsideUnitedStatesError(ValueError):
    """The location resolved to a point outside the area this API serves."""


@dataclass
class Place:
    query: str
    latitude: float
    longitude: float
    display_name: str = ""
    provider: str = "coordinates"
    api_calls: int = 0

    @property
    def coordinates(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


def _config(key: str):
    return settings.ROUTE_PLANNER[key]


def _ors_geocode(query: str) -> Place | None:
    """OpenRouteService's Pelias geocoder - same free key as the directions API.

    Searched worldwide, then filtered to US results: restricting the search to
    the USA up front would quietly turn "Toronto, ON" into Toronto, Ohio.
    """
    api_key = _config("ORS_API_KEY")
    if not api_key:
        return None
    response = requests.get(
        "https://api.openrouteservice.org/geocode/search",
        params={"api_key": api_key, "text": query, "size": 5},
        headers={"User-Agent": _config("USER_AGENT")},
        timeout=_config("HTTP_TIMEOUT_SECONDS"),
    )
    response.raise_for_status()
    features = response.json().get("features") or []
    if not features:
        return None
    us_features = [
        feature
        for feature in features
        if (feature.get("properties") or {}).get("country_a") == "USA"
    ]
    if not us_features:
        raise OutsideUnitedStatesError(
            f"'{query}' is not in the United States. Both locations must be within the USA."
        )
    feature = us_features[0]
    lon, lat = feature["geometry"]["coordinates"]
    return Place(
        query=query,
        latitude=lat,
        longitude=lon,
        display_name=feature.get("properties", {}).get("label", ""),
        provider="openrouteservice",
        api_calls=1,
    )


def _nominatim_geocode(query: str) -> Place | None:
    """OpenStreetMap Nominatim - keyless fallback, same worldwide-then-filter rule."""
    response = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 5, "addressdetails": 1},
        headers={"User-Agent": _config("USER_AGENT")},
        timeout=_config("HTTP_TIMEOUT_SECONDS"),
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        return None
    us_results = [
        result
        for result in results
        if (result.get("address") or {}).get("country_code", "").lower() == "us"
    ]
    if not us_results:
        raise OutsideUnitedStatesError(
            f"'{query}' is not in the United States. Both locations must be within the USA."
        )
    result = us_results[0]
    return Place(
        query=query,
        latitude=float(result["lat"]),
        longitude=float(result["lon"]),
        display_name=result.get("display_name", ""),
        provider="nominatim",
        api_calls=1,
    )


def geocode(raw: str) -> Place:
    """Resolve ``raw`` to a US coordinate.

    Accepts either ``"lat,lon"`` (free) or a place name such as ``"Dallas, TX"``.
    """
    if raw is None or not str(raw).strip():
        raise GeocodingError("Location must not be empty.")

    query = str(raw).strip()

    match = COORDINATE_RE.match(query)
    if match:
        latitude = float(match.group("lat"))
        longitude = float(match.group("lon"))
        if not in_service_area(latitude, longitude):
            raise OutsideUnitedStatesError(
                f"'{query}' is outside the United States. Both locations must be within the USA."
            )
        return Place(query=query, latitude=latitude, longitude=longitude)

    parts = places.split_city_state(query)
    if parts and places.is_canadian_region(parts[1]):
        raise OutsideUnitedStatesError(
            f"'{query}' is in Canada. Both locations must be within the USA."
        )

    offline = places.lookup(query)
    if offline:
        latitude, longitude, label = offline
        return Place(
            query=query,
            latitude=latitude,
            longitude=longitude,
            display_name=label,
            provider="census-gazetteer (offline)",
            api_calls=0,
        )

    cached = GeocodeCache.objects.filter(query__iexact=query).first()
    if cached:
        return Place(
            query=query,
            latitude=cached.latitude,
            longitude=cached.longitude,
            display_name=cached.display_name,
            provider=f"{cached.provider} (cached)",
            api_calls=0,
        )

    place: Place | None = None
    errors: list[str] = []
    calls = 0  # every HTTP request sent, so the count reported to clients is exact
    geocoders = [_nominatim_geocode]
    if _config("ORS_API_KEY"):
        geocoders.insert(0, _ors_geocode)
    for geocoder in geocoders:
        calls += 1
        try:
            place = geocoder(query)
        except OutsideUnitedStatesError:
            raise  # a definite answer, not a failure to answer
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f"{geocoder.__name__}: {exc}")
            continue
        if place:
            place.api_calls = calls
            break

    if place is None:
        detail = f" ({'; '.join(errors)})" if errors else ""
        raise GeocodingError(f"Could not find '{query}' in the United States.{detail}")

    if not in_service_area(place.latitude, place.longitude):
        raise OutsideUnitedStatesError(
            f"'{query}' resolved outside the United States. Both locations must be within the USA."
        )

    GeocodeCache.objects.update_or_create(
        query=query,
        defaults={
            "latitude": place.latitude,
            "longitude": place.longitude,
            "display_name": place.display_name,
            "provider": place.provider,
        },
    )
    return place
