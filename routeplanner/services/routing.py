"""Routing providers.

One HTTP call per planned route. OpenRouteService is used when an API key is
configured; the keyless public OSRM server is the fallback, so the project runs
for anyone who clones it with no signup. A provider is retried once only on a
network error or a 5xx - never on a 4xx, which is an answer rather than an
outage.
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

    def __init__(self, message: str, *, api_calls: int = 0):
        super().__init__(message)
        self.api_calls = api_calls


class NoRouteError(RoutingError):
    """The router answered: there is no road between these points (e.g. islands)."""


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

    def __init__(self):
        self.calls = 0  # HTTP requests actually sent, retries included

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
        """Send the request, retrying once only on a network error or a 5xx.

        A 4xx is an answer, not an outage - retrying it would only spend a call -
        so it is returned for the provider to interpret.
        """
        timeout = self._config("HTTP_TIMEOUT_SECONDS")
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", self._config("USER_AGENT"))
        last_error: Exception | None = None
        for attempt in (1, 2):
            self.calls += 1
            try:
                response = requests.request(
                    method, url, timeout=timeout, headers=headers, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("%s attempt %s failed: %s", self.name, attempt, exc)
                continue
            if response.status_code >= 500:
                last_error = requests.HTTPError(f"{response.status_code} from {self.name}")
                logger.warning("%s attempt %s failed: %s", self.name, attempt, last_error)
                continue
            return response
        raise RoutingError(f"{self.name} unavailable: {last_error}")

    @staticmethod
    def _json(response: requests.Response) -> dict:
        try:
            return response.json()
        except ValueError:
            return {}


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
        payload = self._json(response)
        if response.status_code >= 400:
            error = payload.get("error") or {}
            message = error.get("message", f"HTTP {response.status_code}")
            # 2009: no route between the points; 2010: a point is nowhere near a road.
            if error.get("code") in (2009, 2010):
                raise NoRouteError(f"OpenRouteService: {message}")
            raise RoutingError(f"OpenRouteService: {message}")
        routes = payload.get("routes") or []
        if not routes:
            raise NoRouteError("OpenRouteService returned no route")
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
        payload = self._json(response)
        code = payload.get("code")
        if code in ("NoRoute", "NoSegment"):
            raise NoRouteError(f"OSRM: {payload.get('message', code)}")
        if response.status_code >= 400 or code != "Ok" or not payload.get("routes"):
            raise RoutingError(f"OSRM returned {code or f'HTTP {response.status_code}'}")
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
    """Fetch a route, trying each available provider in order.

    ``api_calls`` on the result (or the error) counts every HTTP request sent,
    across providers and retries, so the number reported to clients is exact.
    """
    errors: list[str] = []
    no_route_everywhere = True
    providers = get_providers()
    for provider in providers:
        try:
            result = provider.fetch(start, finish)
        except (RoutingError, ValueError, KeyError) as exc:
            errors.append(str(exc))
            no_route_everywhere &= isinstance(exc, NoRouteError)
            continue
        if len(result.points) < 2:
            errors.append(f"{provider.name}: route geometry too short")
            no_route_everywhere = False
            continue
        result.api_calls = sum(p.calls for p in providers)
        if errors:
            result.warnings.append(f"Primary routing provider failed; used {result.provider}")
        return result

    calls = sum(p.calls for p in providers)
    if errors and no_route_everywhere:
        raise NoRouteError("; ".join(errors), api_calls=calls)
    raise RoutingError("; ".join(errors) or "no routing provider configured", api_calls=calls)
