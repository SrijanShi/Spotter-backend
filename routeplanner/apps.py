from django.apps import AppConfig


class RouteplannerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routeplanner"

    # The in-memory station index is built lazily by
    # `routeplanner.services.corridor.get_station_index()` and warmed at WSGI
    # start-up (see config/wsgi.py). It is deliberately NOT built in ready():
    # Django discourages database access during app initialisation.
