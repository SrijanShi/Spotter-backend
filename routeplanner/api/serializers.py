from django.conf import settings
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Validates the query string / JSON body for a route plan."""

    start = serializers.CharField(
        max_length=255,
        help_text='Start location: a place name ("Dallas, TX") or "lat,lon".',
    )
    finish = serializers.CharField(
        max_length=255,
        help_text='Finish location: a place name ("New York, NY") or "lat,lon".',
    )
    mpg = serializers.FloatField(
        required=False, min_value=0.1, max_value=200, help_text="Fuel economy. Default 10."
    )
    range_miles = serializers.FloatField(
        required=False,
        min_value=10,
        max_value=5000,
        help_text="Maximum distance on a full tank. Default 500.",
    )
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=1,
        help_text="How far off the route a truck stop may sit. Default 15.",
    )
    start_fuel_gallons = serializers.FloatField(
        required=False,
        min_value=0,
        help_text="Fuel already in the tank at the start. Default 0 (every mile is paid for).",
    )
    include_geometry = serializers.BooleanField(
        required=False, default=True, help_text="Include the route polyline in the response."
    )
    refresh = serializers.BooleanField(
        required=False, default=False, help_text="Bypass the cached plan for this route."
    )

    def validate_max_detour_miles(self, value: float) -> float:
        limit = settings.ROUTE_PLANNER["MAX_DETOUR_MILES_LIMIT"]
        if value > limit:
            raise serializers.ValidationError(f"Must not exceed {limit:.0f} miles.")
        return value

    def to_plan_request(self):
        from ..services.planner import PlanRequest

        config = settings.ROUTE_PLANNER
        data = self.validated_data
        return PlanRequest(
            start=data["start"],
            finish=data["finish"],
            mpg=data.get("mpg") or config["DEFAULT_MPG"],
            range_miles=data.get("range_miles") or config["DEFAULT_RANGE_MILES"],
            max_detour_miles=data.get("max_detour_miles") or config["DEFAULT_MAX_DETOUR_MILES"],
            start_fuel_gallons=data.get("start_fuel_gallons") or 0.0,
            include_geometry=data.get("include_geometry", True),
            refresh=data.get("refresh", False),
        )


# ---------------------------------------------------------------------------
# Response shapes - declared for the OpenAPI schema, not used for validation.
# ---------------------------------------------------------------------------
class FuelStopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    is_origin_fill = serializers.BooleanField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    opis_id = serializers.CharField(allow_null=True)
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    price_per_gallon = serializers.FloatField()
    mile_marker = serializers.FloatField()
    detour_miles = serializers.FloatField()
    gallons = serializers.FloatField()
    cost = serializers.FloatField()
    note = serializers.CharField(allow_null=True)


class RouteTotalsSerializer(serializers.Serializer):
    stops = serializers.IntegerField()
    gallons = serializers.FloatField()
    fuel_cost = serializers.FloatField()
    average_price_paid = serializers.FloatField()
    cost_at_national_average = serializers.FloatField()
    savings_vs_national_average = serializers.FloatField()
    national_average_price = serializers.FloatField()


class RoutePlanSerializer(serializers.Serializer):
    map_url = serializers.URLField(help_text="This route and its fuel stops drawn on a map.")
    start = serializers.DictField()
    finish = serializers.DictField()
    route = serializers.DictField()
    vehicle = serializers.DictField()
    fuel_stops = FuelStopSerializer(many=True)
    totals = RouteTotalsSerializer()
    meta = serializers.DictField()
