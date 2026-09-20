# Fuel-Optimal Route API

A Django REST API that plans a driving route between two US locations and works out the
cheapest way to fuel it — for a vehicle with a **500-mile range** doing **10 miles per gallon**,
using the supplied OPIS truck stop price file.

Give it a start and a finish; it returns the route, a map of it, the fuel stops to make in order,
how many gallons to buy at each, and what the trip costs in fuel.

```
GET /api/v1/route/?start=Dallas, TX&finish=New York, NY
```

```jsonc
{
  "route":  { "distance_miles": 1548.8, "duration_hours": 28.28, "provider": "osrm",
              "geometry": { "type": "LineString", "coordinates": [[-96.79, 32.77], …] } },
  "fuel_stops": [
    { "sequence": 1, "name": "One9 #1248", "city": "Wilmer", "state": "TX",
      "price_per_gallon": 2.756, "mile_marker": 0.0, "gallons": 50.7, "cost": 139.62 },
    …
  ],
  "totals": { "stops": 8, "gallons": 154.88, "fuel_cost": 438.19,
              "cost_at_national_average": 528.63, "savings_vs_national_average": 90.44 },
  "meta":   { "external_api_calls": 1, "cached": false, "compute_ms": 1484.4,
              "routing_api_ms": 1345.7, "stations_considered": 388 }
}
```

There is also a map page at **`/map/`** that renders the same response with Leaflet.

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py import_fuel_prices     # loads 6,614 truck stops (~2 s)
python manage.py runserver
```

Then open <http://localhost:8000/map/> or:

```bash
curl "http://localhost:8000/api/v1/route/?start=Dallas,%20TX&finish=New%20York,%20NY" | jq .totals
```

No API key is needed — the project falls back to the keyless public OSRM server. If you have a
free [OpenRouteService](https://openrouteservice.org/dev/#/signup) key, put it in `.env`
(`cp .env.example .env`) and it will be used instead; it is faster.

With Docker:

```bash
docker compose up --build        # migrates, loads the data, serves on :8000
```

Other entry points: `/api/docs/` (Swagger UI), `/api/v1/health/` (dataset summary),
`postman_collection.json` (import into Postman — the requests are ordered for a demo).

---

## How it works

```
  start, finish
        │
        ▼
 ┌─────────────────┐   0–2 calls   place name → coordinates
 │   geocoding     │───────────────► (skipped for "lat,lon" input; cached in the DB forever)
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   1 call      OSRM / OpenRouteService
 │    routing      │───────────────► ~21,000 point polyline, distance, duration
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   0 calls     in-memory grid index of 6,614 truck stops
 │ corridor match  │───────────────► the ~390 stations within 15 miles of the line,
 └─────────────────┘                 each with its mile marker along the route
        │
        ▼
 ┌─────────────────┐   0 calls     the gas station problem, solved exactly
 │   optimiser     │───────────────► where to stop, how much to buy, what it costs
 └─────────────────┘
```

### The API-call budget

The brief asks for one call to the free map/routing API, and no more than three.

| Step | Calls | Notes |
|---|---|---|
| Geocode `start` | 0–1 | 0 when you pass `lat,lon`, or when the name has been seen before |
| Geocode `finish` | 0–1 | same |
| Route | 1 | one request, full geometry |
| Everything else | 0 | corridor matching and optimisation are pure in-memory work |

**Worst case 3. Typical 1. Repeat request 0** — a planned route is cached for 24 hours under its
rounded coordinates, and resolved place names are cached in the database permanently. The response
reports the actual count in `meta.external_api_calls`, so you can check rather than take my word
for it.

### Finding the stations near the route

The router returns about 21,000 points for a cross-country route. Every truck stop lives in an
in-memory grid index (0.25° cells, built once per process), so for each sampled route point only a
handful of candidate stations are ever distance-tested. Each station is reported once, at its
closest approach, with the distance along the route where that happens. On the Dallas → New York
route that is 388 candidate stations found in **63 ms**.

### The optimiser — [`services/optimizer.py`](routeplanner/services/optimizer.py)

This is the classic *gas station problem*: a uniform tank, fuel bought by the fraction of a gallon,
prices varying by stop. The greedy rule is provably optimal for that model:

> At the stop you are standing at, look ahead as far as the tank can carry you.
> * If a **cheaper** station is in reach, buy exactly enough fuel to get to the **first** cheaper
>   one — never pay today's higher price for fuel you can buy cheaper down the road.
> * Otherwise this is the cheapest fuel you will see for a full tank, so **fill up** and drive to
>   the cheapest station in reach.
>
> Near the destination, never buy more than the fuel needed to finish.

Two hard constraints are checked before any of that runs: no gap between consecutive usable
stations may exceed the range, and neither may the final run to the destination. If one does, the
API returns **422** naming the gap rather than quietly returning a plan that strands the driver.

A proof in a docstring is worth little on its own, so
[`test_optimizer_optimality.py`](routeplanner/tests/test_optimizer_optimality.py) checks the greedy
against a dynamic program that enumerates *every* whole-gallon purchase on 40 random instances.
They agree exactly.

---

## Assumptions

Everything here is a judgement call the brief left open. Each one is easy to change — most are
query parameters.

**Fuel cost model — the tank starts empty, so every mile is paid for.**
A truck cannot set off on an empty tank, so mile 0 is treated as a fill-up at the cheapest truck
stop near the start (it appears in `fuel_stops` with `is_origin_fill: true`). The consequence is
that `totals.gallons` comes to exactly `distance ÷ mpg` and the total covers the whole trip. The
alternative — assume a free full tank — would report **$0.00** for any trip under 500 miles.
Pass `start_fuel_gallons=50` if you want that reading instead.

**Station coordinates are city centroids.**
The price file has no latitude or longitude, and its addresses are highway-exit descriptions
(`I-44, EXIT 283 & US-69`) that no geocoder handles well. Cities and states, however, geocode
cleanly, and a truck stop is generally within a few miles of the town it is listed under. So a stop
may sit a few miles from the exact forecourt — which is why the default corridor is **15 miles**
wide rather than 1 (`max_detour_miles`, up to 50).

**Geocoding happens once, offline, and the result is committed.**
`manage.py geocode_stations` resolves all 3,808 distinct city/state pairs and writes
[`data/station_coordinates.csv`](data/station_coordinates.csv), which is in the repo. The API never
geocodes a station at request time. Coverage: **99.8%** of US price rows — 95% from the US Census
place gazetteer (free, offline, instant) and the rest from a one-time rate-limited Nominatim pass
over the ~240 towns the gazetteer missed.

**Canadian rows are dropped.** 620 of the 8,151 rows are in Ontario, Alberta, BC and Manitoba. Both
endpoints must be in the USA, so they are not loaded.

**Duplicate truck stops are collapsed.** 904 rows share an OPIS Truckstop ID with another row —
mostly rebrands (`PILOT TRAVEL CENTER #1243` / `PILOT #1243`). One row per ID is kept, at the lower
price. 8,151 rows in, 6,614 stations out.

**"Within the USA" is enforced.** Place names are geocoded worldwide and then filtered to US
results, so `Toronto, ON` is rejected instead of quietly becoming Toronto, Ohio. Raw coordinates
are tested against a US outline (Natural Earth 1:50m), with a fallback that accepts any point
within 25 miles of a US truck stop in the dataset — without it, border and coastal cities like
El Paso and Manhattan fall marginally outside a 50m coastline.

**The plan is cost-optimal, not stop-count-optimal.** On a short route with prices falling as you
go, buying the minimum at each of several stops really is cheapest, so that is what is returned.

---

## Performance

Measured on this machine, Dallas → New York (1,549 miles, 20,871 route points):

| | |
|---|---|
| Total, cold | **1.5 s** |
| ↳ waiting on the free routing server | 1.35 s |
| ↳ **this API's own work** | **~139 ms** |
| Repeat request (cached plan) | **5 ms** compute, 56 ms wall |
| Corridor match + distance accumulation | 63 ms + 14 ms |
| Optimiser (388 candidates) | 0.3 ms |
| Thinning the geometry for the response | 58 ms |
| Station index build (once per process, at start-up) | 76 ms |
| Response size | 25 KB (geometry thinned from 20,871 points to 1,072 with Douglas-Peucker) |

The work that is actually ours is the 139 ms. What dominates the cold path is the public OSRM demo
server, which is a shared free box; an OpenRouteService key or a self-hosted OSRM
(`OSRM_BASE_URL`) cuts it substantially. Requests are made with `polyline6` geometry, which is
about five times smaller over the wire than GeoJSON.

---

## Tests

```bash
python manage.py test        # 58 tests, ~0.2 s, no network access
```

The routing provider is mocked and locations are passed as coordinates, so the suite never touches
the internet. It covers the optimiser against hand-computed answers and against brute force, the
corridor index, the gazetteer name normalisation (`Oklahoma City city`, `Indianapolis city
(balance)`, `Mc Calla`), the US service-area rules, geocode caching, and the API contract —
including the 400, 422, 502 and 503 paths and the cache actually preventing a second routing call.

---

## Layout

```
config/                     settings, urls, wsgi (warms the station index at start-up)
routeplanner/
  models.py                 FuelStation, GeocodeCache
  services/
    routing.py              RouteProvider ABC → OpenRouteService, OSRM; polyline decoding
    geocoding.py            place name → coordinates, US-filtered, DB-cached
    corridor.py             grid index; stations near a route, with mile markers
    optimizer.py            the gas station algorithm
    planner.py              orchestration, caching, response building
    gazetteer.py            Census place-name normalisation
    service_area.py         "is this in the USA?"
    geo.py                  haversine, polyline decode, Douglas-Peucker
  management/commands/
    geocode_stations.py     offline build step → data/station_coordinates.csv
    import_fuel_prices.py   CSV + coordinates → database
  api/                      serializers, views, urls
  templates/                the Leaflet map page
  tests/
data/
  fuel-prices-for-be-assessment.csv   supplied
  station_coordinates.csv             geocoding output (committed)
  us_boundary.json                    US outline, Natural Earth 1:50m (public domain)
```

## Known limitations

- **The price file is thin in the far west.** California has 8 truck stops in it, Oregon 29. Los
  Angeles → Seattle has an 877-mile stretch with nothing in range, so it returns 422 — correctly,
  given a 500-mile tank. `range_miles=1000` plans it fine. This is the dataset, not the algorithm.
- City-centroid coordinates mean `detour_miles` is an estimate of how far off the highway a stop
  is, not a measured driving detour. Detours are not added to the trip distance.
- Prices are a static snapshot; there is no refresh job.
- SQLite and a local-memory cache are fine for a single process. For several gunicorn workers a
  shared cache (Redis) would stop each worker planning the same route separately; the station index
  is intentionally per-process and read-only.

## Data sources

- Fuel prices: the OPIS file supplied with the assignment.
- Routing: [OSRM](https://project-osrm.org/) demo server, or
  [OpenRouteService](https://openrouteservice.org/) with a free key.
- Geocoding: [US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html)
  and [Nominatim](https://nominatim.org/) (© OpenStreetMap contributors, ODbL).
- US outline: [Natural Earth](https://www.naturalearthdata.com/) 1:50m, public domain.
- Map tiles: OpenStreetMap via CARTO.
