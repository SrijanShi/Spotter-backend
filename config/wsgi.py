"""WSGI entry point.

The station index and the offline place lookup are warmed here - after Django
is fully initialised but before the first request - so no request pays for
loading them.
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
        from django.db import connections
        from django.urls import get_resolver

        from routeplanner.services import places
        from routeplanner.services.corridor import get_station_index

        get_resolver().url_patterns  # import every view, DRF and the schema generator
        places.lookup("Dallas, TX")  # load the 33k-place file into memory
        index = get_station_index()
        if not len(index):
            logger.warning(
                "No fuel stations loaded - run `python manage.py import_fuel_prices`."
            )
        # gunicorn --preload forks workers from this process; they must each open
        # their own database connection rather than share this one.
        connections.close_all()
except Exception as exc:  # noqa: BLE001 - never block start-up over a warm-up
    logger.warning("Start-up warm-up skipped: %s", exc)
