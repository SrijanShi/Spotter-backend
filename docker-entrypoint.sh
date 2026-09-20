#!/bin/sh
# Migrate and load the fuel data, then hand over to gunicorn.
# import_fuel_prices replaces the station table in one transaction, so running
# it on every boot is safe and takes about two seconds.
set -e

python manage.py migrate --noinput
python manage.py import_fuel_prices

exec "$@"
