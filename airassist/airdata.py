"""Optional live PM2.5 lookup through Open-Meteo (free, no API key).

Only fixed HTTPS hosts are contacted, and the user's text is sent solely as a
URL-encoded query value. Results are cached briefly in a bounded cache.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
TIMEOUT_S = 8
MAX_BYTES = 64 * 1024
CACHE_TTL_S = 900
CACHE_MAX = 128

Fetch = Callable[[str, Mapping[str, object]], dict]


class InvalidCity(ValueError):
    """The place name is empty, too short or too long."""


class PlaceNotFound(Exception):
    """The geocoder knows no such place, or it has no PM2.5 reading."""


@dataclass(frozen=True)
class Reading:
    place: str
    pm25: float


def _fetch(url: str, params: Mapping[str, object]) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=TIMEOUT_S) as resp:  # noqa: S310 - fixed https hosts
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("response too large")
    return json.loads(data)


_cache: dict[str, tuple[float, Reading]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def lookup(city: str, fetch: Fetch = _fetch, now: Callable[[], float] = time.monotonic) -> Reading:
    """Return the current PM2.5 for a place name. Raises InvalidCity or PlaceNotFound."""
    name = " ".join(str(city).split())
    if not 2 <= len(name) <= 80:
        raise InvalidCity("enter a town or city name of 2 to 80 characters")
    key = name.casefold()
    with _lock:
        hit = _cache.get(key)
        if hit and now() - hit[0] < CACHE_TTL_S:
            return hit[1]
    places = fetch(GEOCODE_URL, {"name": name, "count": 1, "format": "json"}).get("results") or []
    if not places:
        raise PlaceNotFound(name)
    place = places[0]
    air = fetch(AIR_URL, {"latitude": place["latitude"], "longitude": place["longitude"],
                          "current": "pm2_5"})  # fmt: skip
    pm25 = air["current"]["pm2_5"]
    if pm25 is None:
        raise PlaceNotFound(name)
    reading = Reading(", ".join(filter(None, (place.get("name"), place.get("country")))), float(pm25))
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))  # evict the oldest entry
        _cache[key] = (now(), reading)
    return reading
