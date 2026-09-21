"""Django settings for the fuel-optimal route planner."""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-in-production")
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "routeplanner",
]

TESTING = "test" in sys.argv

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:
    # In development Django's staticfiles app serves these itself.
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Planned routes are cached on disk rather than in process memory: gunicorn runs
# several worker processes, and a per-process cache would make a repeat request
# miss whenever it lands on a different worker. Tests keep the in-memory cache.
CACHES = {
    "default": (
        {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "route-planner-tests",
        }
        if TESTING
        else {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": os.environ.get("DJANGO_CACHE_DIR", str(BASE_DIR / ".cache")),
            "TIMEOUT": 60 * 60 * 24,
            "OPTIONS": {"MAX_ENTRIES": 500},
        }
    )
}

REST_FRAMEWORK = {
    # The API is public and stateless: no login, no session, so the OpenAPI docs
    # do not advertise an "Authorize" step that does not exist.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel-Optimal Route API",
    "DESCRIPTION": (
        "Plans a driving route between two US locations and returns cost-effective fuel "
        "stops for a 500-mile-range, 10 mpg vehicle, with the total fuel cost and a map."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "routeplanner": {
            "handlers": ["console"],
            "level": "WARNING" if TESTING else "INFO",
        }
    },
}

# ---------------------------------------------------------------------------
# Route planner configuration
# ---------------------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
FUEL_PRICES_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
STATION_COORDINATES_CSV = DATA_DIR / "station_coordinates.csv"

ROUTE_PLANNER = {
    # Vehicle defaults from the assignment brief.
    "DEFAULT_MPG": 10.0,
    "DEFAULT_RANGE_MILES": 500.0,
    # Dollars per fuel stop when choosing stops: roughly a 10 minute stop at a
    # driver's hourly cost. 0 drops the time cost (detour fuel still counts).
    "DEFAULT_STOP_PENALTY": 5.0,
    # How far off the route a truck stop may sit and still be considered.
    "DEFAULT_MAX_DETOUR_MILES": 15.0,
    "MAX_DETOUR_MILES_LIMIT": 50.0,
    # Route geometry is sampled down to this many points before corridor matching.
    "ROUTE_SAMPLE_POINTS": 4000,
    # Grid cell size (degrees) for the in-memory station index.
    "GRID_CELL_DEGREES": 0.25,
    # Seconds to cache a fully planned route.
    "PLAN_CACHE_SECONDS": 60 * 60 * 24,
    "HTTP_TIMEOUT_SECONDS": float(os.environ.get("ROUTING_TIMEOUT", "20")),
    # Blank under test so the suite never depends on a developer's local .env.
    "ORS_API_KEY": "" if TESTING else os.environ.get("ORS_API_KEY", ""),
    "ORS_PROFILE": os.environ.get("ORS_PROFILE", "driving-car"),
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    "USER_AGENT": os.environ.get(
        "GEOCODER_USER_AGENT",
        "spotter-fuel-route-planner/1.0 (backend assessment)",
    ),
    # Skip building the in-memory index during management commands that do not need it.
    "BUILD_INDEX_ON_STARTUP": _env_bool("BUILD_INDEX_ON_STARTUP", True),
}
