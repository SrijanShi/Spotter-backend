import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import FuelStation
from ..services.corridor import get_station_index
from ..services.geocoding import GeocodingError, OutsideUnitedStatesError
from ..services.optimizer import InfeasibleRouteError
from ..services.planner import plan_route
from ..services.routing import RoutingError
from .serializers import RoutePlanSerializer, RouteRequestSerializer

logger = logging.getLogger(__name__)


class RoutePlanView(APIView):
    """Plan a route between two US locations and the cheapest way to fuel it."""

    serializer_class = RouteRequestSerializer

    @extend_schema(
        operation_id="plan_route",
        parameters=[
            OpenApiParameter("start", str, description='e.g. "Dallas, TX" or "32.7763,-96.7969"'),
            OpenApiParameter("finish", str, description='e.g. "New York, NY"'),
            OpenApiParameter("mpg", float, description="Fuel economy, default 10"),
            OpenApiParameter("range_miles", float, description="Tank range, default 500"),
            OpenApiParameter(
                "max_detour_miles", float, description="Corridor half-width, default 15"
            ),
            OpenApiParameter("start_fuel_gallons", float, description="Fuel on board, default 0"),
            OpenApiParameter("include_geometry", bool, description="Return the polyline"),
            OpenApiParameter("refresh", bool, description="Bypass the cache"),
        ],
        responses={200: RoutePlanSerializer},
        examples=[
            OpenApiExample(
                "Dallas to New York",
                value={"start": "Dallas, TX", "finish": "New York, NY"},
                request_only=True,
            )
        ],
    )
    def get(self, request):
        return self._plan(request.query_params)

    @extend_schema(request=RouteRequestSerializer, responses={200: RoutePlanSerializer})
    def post(self, request):
        return self._plan(request.data)

    def _plan(self, data):
        serializer = RouteRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        if not len(get_station_index()):
            return Response(
                {
                    "error": "no_fuel_data",
                    "detail": "No fuel stations are loaded. Run `python manage.py "
                    "import_fuel_prices` to load the price file.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            payload = plan_route(serializer.to_plan_request())
        except OutsideUnitedStatesError as exc:
            return Response(
                {"error": "outside_united_states", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except GeocodingError as exc:
            return Response(
                {"error": "location_not_found", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InfeasibleRouteError as exc:
            return Response(
                {"error": "route_not_fuelable", "detail": str(exc), **exc.detail},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except RoutingError as exc:
            logger.error("Routing failed: %s", exc)
            return Response(
                {
                    "error": "routing_unavailable",
                    "detail": f"The routing service could not be reached: {exc}",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(payload)


class HealthView(APIView):
    """Liveness probe plus a quick look at the loaded dataset."""

    @extend_schema(operation_id="health", responses={200: dict})
    def get(self, request):
        index = get_station_index()
        return Response(
            {
                "status": "ok",
                "stations_loaded": FuelStation.objects.count(),
                "stations_indexed": len(index),
                "routing_providers": [
                    name
                    for name, enabled in (
                        ("openrouteservice", bool(settings.ROUTE_PLANNER["ORS_API_KEY"])),
                        ("osrm", True),
                    )
                    if enabled
                ],
                "defaults": {
                    "mpg": settings.ROUTE_PLANNER["DEFAULT_MPG"],
                    "range_miles": settings.ROUTE_PLANNER["DEFAULT_RANGE_MILES"],
                    "max_detour_miles": settings.ROUTE_PLANNER["DEFAULT_MAX_DETOUR_MILES"],
                },
            }
        )
