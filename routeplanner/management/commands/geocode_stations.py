"""Offline build step: resolve every truck stop in the price CSV to a coordinate.

Run once; the output (``data/station_coordinates.csv``) is committed, so the API
never geocodes a station at request time and a fresh clone needs no network.

    python manage.py geocode_stations

Two sources, in order:
  1. US Census place gazetteer - free, offline, instant, covers ~94% of rows.
  2. OpenStreetMap Nominatim - only for what is left (a few hundred small towns),
     rate limited to one request per second per their usage policy.
"""

from __future__ import annotations

import csv
import time
import urllib.parse
import urllib.request
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routeplanner.services.gazetteer import (
    US_STATES,
    download_gazetteer,
    load_gazetteer,
    lookup,
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FIELDNAMES = ["city", "state", "latitude", "longitude", "source"]


class Command(BaseCommand):
    help = "Geocode the fuel price CSV's cities into data/station_coordinates.csv"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=str(settings.FUEL_PRICES_CSV))
        parser.add_argument("--out", default=str(settings.STATION_COORDINATES_CSV))
        parser.add_argument(
            "--gazetteer",
            default=str(settings.DATA_DIR / "census_gazetteer_places.txt"),
            help="Local copy of the Census gazetteer (downloaded if missing).",
        )
        parser.add_argument(
            "--skip-nominatim",
            action="store_true",
            help="Census gazetteer only - no network geocoding of the remainder.",
        )
        parser.add_argument(
            "--sleep", type=float, default=1.1, help="Seconds between Nominatim calls."
        )

    def handle(self, *args, **options):
        source_csv = Path(options["csv"])
        if not source_csv.exists():
            raise CommandError(f"Fuel price CSV not found at {source_csv}")
        out_path = Path(options["out"])

        with open(source_csv, encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        wanted: dict[tuple[str, str], int] = {}
        skipped_non_us = 0
        for row in rows:
            state = (row.get("State") or "").strip().upper()
            city = (row.get("City") or "").strip().upper()
            if state not in US_STATES:
                skipped_non_us += 1
                continue
            wanted[(city, state)] = wanted.get((city, state), 0) + 1

        self.stdout.write(
            f"{len(rows)} rows, {len(wanted)} distinct US city/state pairs "
            f"({skipped_non_us} non-US rows skipped)"
        )

        resolved = self._load_existing(out_path)
        reused = sum(1 for key in wanted if key in resolved)
        if reused:
            self.stdout.write(f"Reusing {reused} coordinates already in {out_path.name}")

        gazetteer_path = download_gazetteer(Path(options["gazetteer"]))
        places = load_gazetteer(gazetteer_path)
        self.stdout.write(f"Census gazetteer loaded: {len(places)} name/state keys")

        census_hits = 0
        for key in wanted:
            if key in resolved:
                continue
            hit = lookup(places, key[0], key[1])
            if hit:
                resolved[key] = (hit[0], hit[1], "census")
                census_hits += 1

        missing = [key for key in wanted if key not in resolved]
        covered_rows = sum(count for key, count in wanted.items() if key in resolved)
        total_us_rows = sum(wanted.values())
        self.stdout.write(
            f"Census matched {census_hits} new pairs; "
            f"{covered_rows}/{total_us_rows} rows covered "
            f"({100 * covered_rows / max(total_us_rows, 1):.1f}%), {len(missing)} pairs missing"
        )

        if missing and not options["skip_nominatim"]:
            self.stdout.write(f"Geocoding {len(missing)} remaining towns via Nominatim...")
            found = 0
            for position, (city, state) in enumerate(sorted(missing), start=1):
                coordinates = self._nominatim(city, state)
                if coordinates:
                    resolved[(city, state)] = (*coordinates, "nominatim")
                    found += 1
                if position % 25 == 0 or position == len(missing):
                    self.stdout.write(f"  {position}/{len(missing)} ({found} found)")
                time.sleep(options["sleep"])
            self.stdout.write(f"Nominatim resolved {found}/{len(missing)}")

        self._write(out_path, resolved)
        final_rows = sum(count for key, count in wanted.items() if key in resolved)
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(resolved)} city coordinates to {out_path} - "
                f"{final_rows}/{total_us_rows} price rows geocodable "
                f"({100 * final_rows / max(total_us_rows, 1):.1f}%)"
            )
        )

    def _load_existing(self, path: Path) -> dict[tuple[str, str], tuple[float, float, str]]:
        if not path.exists():
            return {}
        existing: dict[tuple[str, str], tuple[float, float, str]] = {}
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    existing[(row["city"].strip().upper(), row["state"].strip().upper())] = (
                        float(row["latitude"]),
                        float(row["longitude"]),
                        row.get("source", "unknown"),
                    )
                except (KeyError, ValueError):
                    continue
        return existing

    def _nominatim(self, city: str, state: str) -> tuple[float, float] | None:
        params = urllib.parse.urlencode(
            {
                "city": city,
                "state": state,
                "country": "USA",
                "format": "json",
                "limit": 1,
            }
        )
        request = urllib.request.Request(
            f"{NOMINATIM_URL}?{params}",
            headers={"User-Agent": settings.ROUTE_PLANNER["USER_AGENT"]},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:  # noqa: BLE001 - one town failing must not stop the run
            self.stderr.write(f"  ! {city}, {state}: {exc}")
            return None
        if not payload:
            return None
        return float(payload[0]["lat"]), float(payload[0]["lon"])

    def _write(self, path: Path, resolved: dict[tuple[str, str], tuple[float, float, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for (city, state), (latitude, longitude, source) in sorted(resolved.items()):
                writer.writerow(
                    {
                        "city": city,
                        "state": state,
                        "latitude": f"{latitude:.6f}",
                        "longitude": f"{longitude:.6f}",
                        "source": source,
                    }
                )
