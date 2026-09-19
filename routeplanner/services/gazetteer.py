"""Offline geocoding source: the US Census Bureau place gazetteer.

The supplied fuel price CSV has no coordinates - only a city, a state and a
highway-exit description. The Census gazetteer lists every incorporated place
and CDP in the country with a centroid, is free, needs no API key and downloads
in a few seconds, which makes it the right base layer. Name normalisation is the
whole game: the gazetteer says "Oklahoma City city" and "Indianapolis city
(balance)" where the price file says "Oklahoma City" and "Indianapolis".
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
import zipfile
from pathlib import Path

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2023_Gazetteer/2023_Gaz_place_national.zip"
)

US_STATES = frozenset(
    """AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
    NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR""".split()
)

# Trailing legal designators in Census place names. Only ever strip ONE of these:
# stripping repeatedly turns "Oklahoma City city" into "Oklahoma".
_DESIGNATOR = re.compile(
    r"\s+(CDP|city|town|village|borough|municipality|township|comunidad|zona urbana|"
    r"plantation|consolidated government|metro government|metropolitan government|"
    r"unified government|government|corporation)$",
    re.IGNORECASE,
)
_BALANCE = re.compile(r"\s*\(balance\)\s*$", re.IGNORECASE)


def normalize_place_name(name: str) -> str:
    """'Indianapolis city (balance)' -> 'INDIANAPOLIS'."""
    cleaned = _BALANCE.sub("", (name or "").strip())
    cleaned = _DESIGNATOR.sub("", cleaned).strip()
    return cleaned.upper()


def name_aliases(name: str) -> set[str]:
    """Spelling variants that let the two files meet in the middle."""
    base = (name or "").strip().upper()
    if not base:
        return set()
    aliases = {base}
    aliases.add(re.sub(r"^ST\.? ", "SAINT ", base))
    aliases.add(re.sub(r"^SAINT ", "ST. ", base))
    aliases.add(re.sub(r"^MT\.? ", "MOUNT ", base))
    aliases.add(re.sub(r"^MOUNT ", "MT. ", base))
    # "Mc Calla" / "McCalla", "De Forest" / "DeForest", "La Salle" / "LaSalle"
    aliases.add(re.sub(r"^(MC|DE|LA|LE|O|VAN) (?=[A-Z])", r"\1", base))
    if "-" in base:
        aliases.add(base.split("-")[0].strip())
    aliases.add(re.sub(r"\s+(COUNTY|PARISH|CITY)$", "", base).strip())
    aliases.add(re.sub(r"\s*\(.*\)$", "", base).strip())
    return {alias for alias in aliases if alias}


def download_gazetteer(destination: Path) -> Path:
    """Fetch and unzip the gazetteer, unless it is already on disk."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    with urllib.request.urlopen(GAZETTEER_URL, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    member = next(name for name in archive.namelist() if name.endswith(".txt"))
    destination.write_bytes(archive.read(member))
    return destination


def load_gazetteer(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """Map every (name alias, state) to a centroid.

    Where several places share a name in one state, the one with the largest land
    area wins - that is almost always the town the truck stop is named after.
    """
    best_area: dict[tuple[str, str], float] = {}
    places: dict[tuple[str, str], tuple[float, float]] = {}

    with open(path, encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            row = {key.strip(): (value.strip() if value else "") for key, value in row.items()}
            state = row.get("USPS", "")
            if state not in US_STATES:
                continue
            try:
                latitude = float(row["INTPTLAT"])
                longitude = float(row["INTPTLONG"])
                land_area = float(row.get("ALAND") or 0)
            except (KeyError, ValueError):
                continue
            for alias in name_aliases(normalize_place_name(row.get("NAME", ""))):
                key = (alias, state)
                if key not in places or land_area > best_area.get(key, -1.0):
                    places[key] = (latitude, longitude)
                    best_area[key] = land_area
    return places


def lookup(
    places: dict[tuple[str, str], tuple[float, float]], city: str, state: str
) -> tuple[float, float] | None:
    for alias in name_aliases(city):
        hit = places.get((alias, state.strip().upper()))
        if hit:
            return hit
    return None
