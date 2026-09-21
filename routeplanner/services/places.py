"""Resolve "City, ST" locations offline, so the common case costs no API call.

The brief calls one call to the map/routing API ideal. With place names geocoded
online, a request for "Dallas, TX" to "New York, NY" costs three: two geocodes
and the route. But the Census place gazetteer that already geocodes the truck
stops also covers every US city and town, so "City, ST" and "City, State" inputs
are answered from a 440 KB file in the repo, and only the route goes over the
network. Anything this module cannot parse with confidence - a street address,
a landmark, a ZIP code - is left to the online geocoder.
"""

from __future__ import annotations

import csv
import gzip
import re
import threading
from pathlib import Path

from django.conf import settings

from .gazetteer import US_STATES, name_aliases

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR",
}

# Canadian provinces and territories - answered offline with a clear "not in the
# USA" rather than spending a geocoding call to find that out.
CANADIAN_PROVINCES = frozenset(
    {
        "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
        "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK",
        "NEWFOUNDLAND AND LABRADOR", "NORTHWEST TERRITORIES", "NOVA SCOTIA", "NUNAVUT",
        "ONTARIO", "PRINCE EDWARD ISLAND", "QUEBEC", "SASKATCHEWAN", "YUKON",
    }
)

_COUNTRY_SUFFIX = re.compile(r"\s*,?\s*(USA|U\.S\.A\.|US|U\.S\.|UNITED STATES( OF AMERICA)?)\s*$")
_ZIP_SUFFIX = re.compile(r"\s+\d{5}(-\d{4})?\s*$")

_lock = threading.Lock()
_places: dict[tuple[str, str], tuple[float, float]] | None = None


def places_path() -> Path:
    return Path(settings.DATA_DIR) / "us_places.csv.gz"


def _load() -> dict[tuple[str, str], tuple[float, float]]:
    global _places
    if _places is None:
        with _lock:
            if _places is None:
                places: dict[tuple[str, str], tuple[float, float]] = {}
                path = places_path()
                if path.exists():
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        for row in csv.DictReader(handle):
                            places[(row["name"], row["state"])] = (
                                float(row["latitude"]),
                                float(row["longitude"]),
                            )
                _places = places
    return _places


def split_city_state(query: str) -> tuple[str, str] | None:
    """'Dallas, TX' -> ('DALLAS', 'TX'). None when the input is not that shape.

    Accepts a state code or a full state name, an optional ZIP code and an
    optional trailing country ("Dallas, Texas 75201, USA").
    """
    text = _COUNTRY_SUFFIX.sub("", query.strip().upper()).strip().rstrip(",")
    text = _ZIP_SUFFIX.sub("", text).strip()
    if text.count(",") != 1:
        return None
    city, region = (part.strip() for part in text.split(","))
    if not city or not region or any(char.isdigit() for char in city):
        return None
    region = region.rstrip(".")
    return city, region


def is_canadian_region(region: str) -> bool:
    return region in CANADIAN_PROVINCES


def lookup(query: str) -> tuple[float, float, str] | None:
    """Offline coordinate for a 'City, ST' query, or None to fall back online."""
    parts = split_city_state(query)
    if parts is None:
        return None
    city, region = parts
    state = region if region in US_STATES else STATE_NAMES.get(region)
    if state is None:
        return None
    places = _load()
    for alias in name_aliases(city):
        hit = places.get((alias, state))
        if hit:
            return hit[0], hit[1], f"{city.title()}, {state}"
    return None


def reset_places() -> None:
    global _places
    with _lock:
        _places = None
