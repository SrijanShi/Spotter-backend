"""WSGI entry point.

The station index is warmed here - after Django is fully initialised but before
the first request - so no request pays for building it.
"""

import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

logger = logging.getLogger(__name__)

try:
    from django.conf import settings

    if settings.ROUTE_PLANNER.get("BUILD_INDEX_ON_STARTUP", True):
        from routeplanner.services.corridor import get_station_index

        index = get_station_index()
        if not len(index):
            logger.warning(
                "No fuel stations loaded - run `python manage.py import_fuel_prices`."
            )
except Exception as exc:  # noqa: BLE001 - never block start-up over a warm-up
    logger.warning("Station index warm-up skipped: %s", exc)
