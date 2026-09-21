FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

RUN chmod +x docker-entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
# --preload: load and warm the app once, then fork the workers, so none of them
# pays for importing Django or loading the station data on its first request.
CMD ["gunicorn", "config.wsgi:application", "--preload", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
