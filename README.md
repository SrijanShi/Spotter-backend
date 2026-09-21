# Fuel-Optimal Route API

[![tests](https://github.com/SrijanShi/Spotter-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/SrijanShi/Spotter-backend/actions/workflows/tests.yml)

A Django REST API that plans a driving route between two US locations and works out the
cheapest way to fuel it — for a vehicle with a **500-mile range** doing **10 miles per gallon**,
using the supplied OPIS truck stop price file.

Give it a start and a finish; it returns the route, a link to a map of it, the fuel stops to make
in order, how many gallons to buy at each, and what the trip costs in fuel — using **one** call to
the external routing API. Stops are chosen for cost *and* practicality: Dallas → New York is 4
stops (the minimum a 500-mile tank allows), not 8 stops that save $4 by topping up 1-gallon at a
time.

```
GET /api/v1/route/?start=Dallas, TX&finish=New York, NY
```

```jsonc
{
  "map_url": "http://localhost:8000/map/?start=Dallas%2C+TX&finish=New+York%2C+NY",
  "route":  { "distance_miles": 1551.8, "duration_hours": 28.46, "provider": "osrm",
              "geometry": { "type": "LineString", "coordinates": [[-96.77, 32.79], …] } },
  "fuel_stops": [
    { "sequence": 1, "name": "One9 #1248", "city": "Wilmer", "state": "TX",
      "price_per_gallon": 2.756, "mile_marker": 0.0, "gallons": 50.0, "cost": 137.78 },
    { "sequence": 2, "name": "PIT STOP", "city": "Wheatley", "state": "AR",
      "price_per_gallon": 2.999, "mile_marker": 385.6, "gallons": 18.7, "cost": 56.08 },
    …
  ],
  "totals": { "stops": 4, "gallons": 155.2, "fuel_cost": 443.29,
              "cost_at_national_average": 529.7, "savings_vs_national_average": 86.41 },
  "optimization": {
    "objective": "fuel cost + $5.00 per stop + the fuel to drive off the route to each station and back",
    "cheapest_fuel_only_plan": { "stops": 8, "fuel_cost": 439.12 },
    "extra_fuel_cost_for_fewer_stops": 4.17
  },
  "meta":   { "external_api_calls": 1, "cached": false, "compute_ms": 1590.3,
              "routing_api_ms": 1443.7, "stations_considered": 385 }
}
```

`totals` is the exact sum of the stops as listed, to the cent.

**The map.** Every response carries `map_url` — open it and the route is drawn with numbered fuel
stops, prices and totals (Leaflet on OpenStreetMap tiles, rendered from this same response). The route is
also in the JSON as a GeoJSON `LineString`, and every stop has coordinates, for clients that draw
their own.

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

No API key is needed — the project uses the keyless public OSRM server. If you have a free
[OpenRouteService](https://openrouteservice.org/dev/#/signup) key, put it in `.env`
(`cp .env.example .env`) and it becomes the primary router, with OSRM as the fallback.

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
 ┌─────────────────┐   0 calls     "City, ST" → offline Census place file (33k US places)
 │   geocoding     │───────────────► "lat,lon" → used as-is
 └─────────────────┘                 anything else → online geocoder, then cached in the DB
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
 ┌─────────────────┐   0 calls     dynamic program: fuel cost + a cost per stop
 │   optimiser     │───────────────► where to stop, how much to buy, what it costs
 └─────────────────┘
```

### The API-call budget

The brief asks for one call to the free map/routing API, and no more than three.

| Step | Calls | Notes |
|---|---|---|
| Geocode `start` | 0 | `"Dallas, TX"`, `"Dallas, Texas"` or `"32.77,-96.79"` resolve offline |
| Geocode `finish` | 0 | same |
| Route | **1** | one request, full geometry |
| Everything else | 0 | corridor matching and optimisation are pure in-memory work |

**A normal request makes exactly one external call** — the route. `City, ST` inputs are answered
from the same US Census gazetteer that geocodes the truck stops, shipped as a 440 KB file. Only
input it cannot parse (a street address, a landmark) goes to an online geocoder, and that answer is
cached in the database permanently, so a request makes at most 3 calls, and only on first sight of
such a name. A repeat request makes **zero** calls: planned routes are cached for 24 hours.

`meta.external_api_calls` counts every HTTP request actually sent, including the rare retry (one,
on a network error or 5xx — never on a 4xx) or fallback to the second router, so you can check the
budget rather than take my word for it.

### Finding the stations near the route

The router returns about 21,000 points for a cross-country route. Every truck stop lives in an
in-memory grid index (0.25° cells, built once per process), so for each sampled route point only a
handful of candidate stations are ever distance-tested. Each station is reported once, at its
closest approach, with the distance along the route where that happens. On the Dallas → New York
route that is 388 candidate stations found in **63 ms**.

### The optimiser — [`services/optimizer.py`](routeplanner/services/optimizer.py)

The brief says optimal *mostly* means cost effective. Taken literally — cheapest fuel, nothing else
— the answer is impractical: prices vary by a few cents between neighbouring truck stops, so the
pure optimum pulls a truck off the interstate to buy 1.2 gallons that are 3¢ cheaper. Miami →
Seattle takes 27 stops that way. No dispatcher would send that plan.

So the plan minimises

> **fuel cost + $5 per stop + the fuel to drive off the route to that station and back**

$5 stands for the driver's time at a stop (roughly ten minutes); it is the `stop_penalty` query
parameter; `stop_penalty=0` drops the time cost and leaves only fuel, detour fuel included. The detour term means a station
14 miles off the interstate has to be genuinely cheaper to be worth the 28-mile round trip.

| Route | Cheapest fuel only | This plan | Extra fuel cost |
|---|---|---|---|
| Dallas → New York | 8 stops, $439.12 | **4 stops**, $443.29 | +$4.17 (0.9%) |
| Miami → Seattle | 27 stops, $1,005.33 | **9 stops**, $1,015.05 | +$9.72 (1.0%) |
| Chicago → Denver | 7 stops, $291.63 | **3 stops**, $292.89 | +$1.26 (0.4%) |
| Los Angeles → Dallas | 7 stops, $441.83 | **5 stops**, $442.09 | +$0.26 (0.1%) |

Every response reports both numbers under `optimization`, so the trade-off is visible, not hidden.

**How.** A dynamic program over (station, fuel in the tank). Walking the stations in order along
the route, it keeps, for every possible fuel level, the cheapest way to arrive there. At each
station the truck either drives past or stops and fills to any level; because the fill step is a
prefix minimum, each station costs O(tank) with numpy, and a cross-country route with ~500
candidate stations solves in **2–16 ms**. Fuel is tracked in whole miles of range (0.1 gallon at
10 mpg) — the only approximation, and the plan is exactly optimal at that resolution.

Two hard constraints are checked before any of that runs: no gap between consecutive usable
stations may exceed the range, and neither may the final run to the destination. If one does, the
API returns **422** naming the gap rather than quietly returning a plan that strands the driver.

A claim of optimality is worth little on its own, so
[`test_optimizer_optimality.py`](routeplanner/tests/test_optimizer_optimality.py) checks the
planner against an exhaustive search over *every* whole-gallon purchase and every choice of stops,
on 120 random instances — with no stop cost, a $5 stop cost, $5 plus detours, and $40 plus
detours. They agree exactly.

---

## Assumptions

Everything here is a judgement call the brief left open. Each one is easy to change — most are
query parameters.

**Fuel cost model — the tank starts empty, so every mile is paid for.**
A truck cannot set off on an empty tank, so mile 0 is treated as a fill-up at the cheapest truck
stop near the start (it appears in `fuel_stops` with `is_origin_fill: true`). The consequence is
that `totals.gallons` equals `distance ÷ mpg` (to within 0.1 gallon — fuel is planned in whole miles
of range) and the total covers the whole trip. The
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

**Start and finish resolve to the city centroid.** `Dallas, TX` means the Census internal point of
Dallas, which is plenty for routing a truck across the country. Pass `lat,lon` for an exact spot.

**"Within the USA" is enforced.** Canadian provinces are rejected offline (`Toronto, ON` → 400, no
API call), and names sent to the online geocoder are searched worldwide and then filtered to US
results, so nothing foreign quietly becomes a US namesake. Raw coordinates
are tested against a US outline (Natural Earth 1:50m), with a fallback that accepts any point
within 25 miles of a US truck stop in the dataset — without it, border and coastal cities like
El Paso and Manhattan fall marginally outside a 50m coastline.

**A stop costs $5 of driver time, plus its detour.** See the optimiser section: this is what turns
27 stops into 9. `stop_penalty=0` drops the time cost; the pure cheapest-fuel plan (no stop or detour
costs at all) is reported next to every answer under `optimization.cheapest_fuel_only_plan`. The detour fuel steers the choice of station; it is not
added to `totals`, which report the fuel for the route itself.

---

## Performance

Measured on this machine, Dallas → New York (1,552 miles, 21,016 route points), no API key:

| | |
|---|---|
| Total, cold | **1.6 s** |
| ↳ waiting on the free routing server | 1.44 s |
| ↳ **this API's own work** | **~150 ms** |
| Repeat request (cached plan) | **1 ms** compute; 5 ms median over HTTP in Docker |
| Geocoding `Dallas, TX` and `New York, NY` | 0 ms network — offline |
| Corridor match + distance accumulation | 63 ms + 14 ms |
| Optimiser, both plans (385 candidates) | ~10 ms |
| Thinning the geometry for the response | 58 ms |
| Station index build (once, before gunicorn forks its workers) | 76 ms |
| Response size | 25 KB (geometry thinned from 21,016 points to 1,066 with Douglas-Peucker) |

The work that is actually ours is the ~150 ms. The cold path is dominated by the one routing call to
a free public server; OpenRouteService with a key performs about the same from here, and a
self-hosted OSRM (`OSRM_BASE_URL`) would remove most of it. Requests ask for `polyline6` geometry,
which is about five times smaller over the wire than GeoJSON.

## Tests

```bash
python manage.py test        # 78 tests, ~0.7 s, no network access
```

The routing provider is mocked and locations are passed as coordinates, so the suite never touches
the internet. It covers the optimiser against hand-computed answers and against brute force, the
corridor index, the gazetteer name normalisation (`Oklahoma City city`, `Indianapolis city
(balance)`, `Mc Calla`), the US service-area rules, geocode caching, routing retries and exact call
counting, and the API contract — including every 400, 422, 502 and 503 path, `map_url`
reproducing the exact plan, totals summing to the cent, and the cache actually preventing a second
routing call.
They run on every push in GitHub Actions (badge at the top), and the suite ignores any ORS key in
your `.env`, so it behaves the same everywhere.

`postman_collection.json` carries its own assertions — 32 of them, checking one external call per
plan, cache hits, `gallons = miles ÷ mpg`, no gap longer than the range, and the error codes. Run it
in Postman or with `npx newman run postman_collection.json`.

---

## Layout

```
config/                     settings, urls, wsgi (warms the station index at start-up)
routeplanner/
  models.py                 FuelStation, GeocodeCache
  services/
    routing.py              RouteProvider ABC → OpenRouteService, OSRM; polyline decoding
    places.py               "City, ST" → coordinates offline, from the Census place file
    geocoding.py            everything else → online geocoder, US-filtered, DB-cached
    corridor.py             grid index; stations near a route, with mile markers
    optimizer.py            dynamic program: where to stop and how much to buy
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
  us_places.csv.gz                    33k US places for offline "City, ST" lookup (committed)
  us_boundary.json                    US outline, Natural Earth 1:50m (public domain)
```

## Known limitations

- **The price file is thin in the far west.** California has 8 truck stops in it, Oregon 29. Los
  Angeles → Seattle has an 877-mile stretch with nothing in range, so it returns 422 — correctly,
  given a 500-mile tank. `range_miles=1000` plans it fine. This is the dataset, not the algorithm.
- City-centroid coordinates mean `detour_miles` is an estimate of how far off the highway a stop
  is, not a measured driving detour. It steers which station is chosen but is not added to the
  trip distance or the totals.
- Prices are a static snapshot; there is no refresh job.
- Planned routes are cached on disk, so every gunicorn worker shares them. That is right for one
  machine; across several, point Django's cache at Redis instead. The station index is
  intentionally per-process and read-only.

## Data sources

- Fuel prices: the OPIS file supplied with the assignment.
- Routing: [OSRM](https://project-osrm.org/) demo server, or
  [OpenRouteService](https://openrouteservice.org/) with a free key.
- Geocoding: [US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html)
  and [Nominatim](https://nominatim.org/) (© OpenStreetMap contributors, ODbL).
- US outline: [Natural Earth](https://www.naturalearthdata.com/) 1:50m, public domain.
- Map tiles: [OpenStreetMap](https://www.openstreetmap.org/copyright) standard tiles, no key required.
