from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from routeplanner.views import MapView

urlpatterns = [
    path("", lambda request: redirect("map"), name="home"),
    path("map/", MapView.as_view(), name="map"),
    path("api/v1/", include("routeplanner.api.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("admin/", admin.site.urls),
]
