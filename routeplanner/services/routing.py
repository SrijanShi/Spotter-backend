"""Routing providers.

One HTTP call per planned route. OpenRouteService is used when an API key is
configured (it is noticeably faster); the keyless public OSRM server is the
fallback so the project runs for anyone who clones it with no signup.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests
from django.conf import settings

from .geo import METERS_PER_MILE, decode_polyline

logger = logging.getLogger(__name__)


class RoutingError(RuntimeError):
    """No routing provider could return a route."""


@dataclass
class RouteResult:
    points: list[tuple[float, float]]  # (lat, lon) along the route
    distance_miles: float
    duration_hours: float
    provider: str
    api_calls: int = 1
    warnings: list[str] = field(default_factory=list)


class RouteProvider(ABC):
    name: str = "provider"

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def fetch(
        self, start: tuple[float, float], finish: tuple[float, float]
    ) -> RouteResult:  # pragma: no cover - interface
        ...

    def _config(self, key: str):
        return settings.ROUTE_PLANNER[key]

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """One retry, then give up so the next provider gets a turn."""
        timeout = self._config("HTTP_TIMEOUT_SECONDS")
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", self._config("USER_AGENT"))
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = requests.request(
                    method, url, timeout=timeout, headers=headers, **kwargs
                )
                if response.status_code >= 500:
                    raise requests.HTTPError(f"{response.status_code} from {self.name}")
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - deliberately broad, we fall through
                last_error = exc
                logger.warning("%s attempt %s failed: %s", self.name, attempt, exc)
        raise RoutingError(f"{self.name} unavailable: {last_error}")


class ORSProvider(RouteProvider):
    """OpenRouteService directions (free tier: 2,000 requests/day)."""

    name = "openrouteservice"
    endpoint = "https://api.openrouteservice.org/v2/directions/{profile}"

    @property
    def available(self) -> bool:
        return bool(self._config("ORS_API_KEY"))

    def fetch(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        profile = settings.ROUTE_PLANNER.get("ORS_PROFILE", "driving-car")
        response = self._request(
            "POST",
            self.endpoint.format(profile=profile),
            json={
                "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
                "instructions": False,
                "geometry_simplify": False,
            },
            headers={
                "Authorization": self._config("ORS_API_KEY"),
                "Content-Type": "application/json",
            },
        )
        payload = response.json()
        routes = payload.get("routes") or []
        if not routes:
            raise RoutingError("OpenRouteService returned no route")
        route = routes[0]
        summary = route.get("summary") or {}
        # ORS encodes geometry as a precision-5 polyline.
        points = decode_polyline(route["geometry"], precision=5)
        return RouteResult(
            points=points,
            distance_miles=summary.get("distance", 0.0) / METERS_PER_MILE,
            duration_hours=summary.get("duration", 0.0) / 3600.0,
            provider=self.name,
        )


class OSRMProvider(RouteProvider):
    """Public OSRM demo server - no API key required."""

    name = "osrm"

    def fetch(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        base = self._config("OSRM_BASE_URL").rstrip("/")
        coords = f"{start[1]},{start[0]};{finish[1]},{finish[0]}"
        response = self._request(
            "GET",
            f"{base}/route/v1/driving/{coords}",
            params={
                "overview": "full",
                "geometries": "polyline6",
                "steps": "false",
                "alternatives": "false",
            },
        )
        payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RoutingError(f"OSRM returned {payload.get('code', 'no route')}")
        route = payload["routes"][0]
        return RouteResult(
            points=decode_polyline(route["geometry"], precision=6),
            distance_miles=route["distance"] / METERS_PER_MILE,
            duration_hours=route["duration"] / 3600.0,
            provider=self.name,
        )


def get_providers() -> list[RouteProvider]:
    return [p for p in (ORSProvider(), OSRMProvider()) if p.available]


def fetch_route(start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
    """Fetch a route, trying each available provider in order."""
    errors: list[str] = []
    for provider in get_providers():
        try:
            result = provider.fetch(start, finish)
        except (RoutingError, ValueError, KeyError) as exc:
            errors.append(f"{provider.name}: {exc}")
            continue
        if len(result.points) < 2:
            errors.append(f"{provider.name}: route geometry too short")
            continue
        if errors:
            result.warnings.append("Primary routing provider failed; used " + result.provider)
        return result
    raise RoutingError("; ".join(errors) or "no routing provider configured")
