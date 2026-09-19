from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price list, geocoded to a coordinate.

    Coordinates come from the offline geocoding build step (`geocode_stations`),
    so nothing here is resolved at request time.
    """

    opis_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    retail_price = models.DecimalField(max_digits=7, decimal_places=5, db_index=True)
    geocode_source = models.CharField(max_length=16, default="census")

    class Meta:
        ordering = ("state", "city", "name")
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"


class GeocodeCache(models.Model):
    """Resolved user-supplied place names, so a repeated query costs no API call."""

    query = models.CharField(max_length=255, unique=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    display_name = models.CharField(max_length=512, blank=True)
    provider = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "geocode cache entries"

    def __str__(self) -> str:
        return f"{self.query} -> ({self.latitude}, {self.longitude})"
