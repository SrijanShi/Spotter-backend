"""Load the OPIS price CSV plus the committed coordinates into the database.

    python manage.py import_fuel_prices

Idempotent: it replaces the station table in one transaction.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routeplanner.models import FuelStation
from routeplanner.services.corridor import reset_station_index
from routeplanner.services.gazetteer import US_STATES


class Command(BaseCommand):
    help = "Import fuel prices and geocoded coordinates into the FuelStation table"

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=str(settings.FUEL_PRICES_CSV))
        parser.add_argument("--coords", default=str(settings.STATION_COORDINATES_CSV))

    def handle(self, *args, **options):
        price_path = Path(options["csv"])
        coords_path = Path(options["coords"])
        if not price_path.exists():
            raise CommandError(f"Fuel price CSV not found at {price_path}")
        if not coords_path.exists():
            raise CommandError(
                f"Coordinates file not found at {coords_path}. "
                "Run `python manage.py geocode_stations` first."
            )

        coordinates = self._load_coordinates(coords_path)

        with open(price_path, encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        stations: dict[str, FuelStation] = {}
        skipped_non_us = 0
        skipped_no_coords = 0
        skipped_bad_price = 0
        duplicates = 0

        for row in rows:
            state = (row.get("State") or "").strip().upper()
            city = (row.get("City") or "").strip()
            opis_id = (row.get("OPIS Truckstop ID") or "").strip()
            if state not in US_STATES:
                skipped_non_us += 1
                continue
            hit = coordinates.get((city.upper(), state))
            if not hit:
                skipped_no_coords += 1
                continue
            try:
                price = Decimal(str(row.get("Retail Price") or "").strip()).quantize(
                    Decimal("0.00001")
                )
            except (InvalidOperation, ValueError):
                skipped_bad_price += 1
                continue
            if not opis_id:
                skipped_bad_price += 1
                continue

            latitude, longitude, source = hit
            station = FuelStation(
                opis_id=opis_id,
                name=(row.get("Truckstop Name") or "").strip()[:255],
                address=(row.get("Address") or "").strip()[:255],
                city=city.title()[:128],
                state=state,
                latitude=latitude,
                longitude=longitude,
                retail_price=price,
                geocode_source=source,
            )
            existing = stations.get(opis_id)
            if existing is None:
                stations[opis_id] = station
            else:
                # The same truck stop appears under old and new brand names;
                # keep one row, at the better price.
                duplicates += 1
                if price < existing.retail_price:
                    stations[opis_id] = station

        if not stations:
            raise CommandError("No stations could be imported - check the input files.")

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(list(stations.values()), batch_size=1000)

        reset_station_index()

        self.stdout.write(
            "\n".join(
                [
                    f"CSV rows read        : {len(rows)}",
                    f"Non-US rows skipped  : {skipped_non_us}",
                    f"Rows without coords  : {skipped_no_coords}",
                    f"Unusable price rows  : {skipped_bad_price}",
                    f"Duplicate OPIS IDs   : {duplicates}",
                ]
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Imported {len(stations)} fuel stations"))

    def _load_coordinates(self, path: Path) -> dict[tuple[str, str], tuple[float, float, str]]:
        coordinates: dict[tuple[str, str], tuple[float, float, str]] = {}
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    coordinates[(row["city"].strip().upper(), row["state"].strip().upper())] = (
                        float(row["latitude"]),
                        float(row["longitude"]),
                        (row.get("source") or "unknown")[:16],
                    )
                except (KeyError, ValueError):
                    continue
        return coordinates
