from django.urls import path

from .views import HealthView, RoutePlanView

app_name = "api"

urlpatterns = [
    path("route/", RoutePlanView.as_view(), name="route"),
    path("health/", HealthView.as_view(), name="health"),
]
