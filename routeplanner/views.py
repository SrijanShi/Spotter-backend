from django.conf import settings
from django.views.generic import TemplateView


class MapView(TemplateView):
    """Leaflet map of the route and its fuel stops.

    The page calls the same public API the JSON clients use - it is a thin
    viewer, not a second implementation.
    """

    template_name = "routeplanner/map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = settings.ROUTE_PLANNER
        context.update(
            {
                "start": self.request.GET.get("start", "Dallas, TX"),
                "finish": self.request.GET.get("finish", "New York, NY"),
                "mpg": self.request.GET.get("mpg", config["DEFAULT_MPG"]),
                "range_miles": self.request.GET.get("range_miles", config["DEFAULT_RANGE_MILES"]),
                "autorun": "start" in self.request.GET and "finish" in self.request.GET,
            }
        )
        return context
