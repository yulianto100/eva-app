"""Reverse geocoding utilities untuk auto-fill data administratif HPDB."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TypedDict

from region_db import resolve_from_nominatim

LOGGER = logging.getLogger(__name__)

NOMINATIM_URL = os.getenv("GEOCODE_NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
USER_AGENT = os.getenv("GEOCODE_USER_AGENT", "eva-converter/1.0 (FTTH planning tool)")
REQUEST_TIMEOUT = float(os.getenv("GEOCODE_TIMEOUT_SECONDS", "8"))
MIN_REQUEST_INTERVAL_SECONDS = 1.0

_LAST_REQUEST_AT = 0.0
_REQUEST_LOCK = threading.Lock()
_CACHE: dict[tuple[float, float], GeoResult] = {}
ADMIN_AREA_OVERRIDES = {
    ("17540", "WARINGINJAYA"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Waringinjaya",
        "postal_code": "17540",
    },
    ("17540", "KARANGSAMBUNG"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Karangsambung",
        "postal_code": "17540",
    },
    ("17540", "KARANGMEKAR"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Karangmekar",
        "postal_code": "17540",
    },
    ("17540", "MEKARJAYA"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Mekarjaya",
        "postal_code": "17540",
    },
    ("17540", "KARANGHARUM"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Karangharum",
        "postal_code": "17540",
    },
    ("17540", "BOJONGSARI"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Bojongsari",
        "postal_code": "17540",
    },
    ("17540", "KEDUNGWARINGIN"): {
        "province": "Jawa Barat",
        "regency_city": "Kabupaten Bekasi",
        "district": "Kedung Waringin",
        "sub_district": "Kedungwaringin",
        "postal_code": "17540",
    },
}


class GeoResult(TypedDict, total=False):
    province: str
    regency_city: str
    city: str
    district: str
    sub_district: str
    subdistrict: str
    postal_code: str
    postalcode: str


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _admin_key(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def apply_admin_area_overrides(result: GeoResult) -> GeoResult:
    postal_code = _first_text(result.get("postal_code"), result.get("postalcode"))
    sub_district = _first_text(result.get("sub_district"), result.get("subdistrict"))
    override = ADMIN_AREA_OVERRIDES.get((postal_code, _admin_key(sub_district)))
    if not override:
        return result

    completed: GeoResult = dict(result)
    for key, value in override.items():
        if key not in completed or not str(completed.get(key) or "").strip():
            completed[key] = value
    completed["city"] = _first_text(completed.get("city"), completed.get("regency_city"))
    completed["subdistrict"] = _first_text(completed.get("subdistrict"), completed.get("sub_district"))
    completed["postalcode"] = _first_text(completed.get("postalcode"), completed.get("postal_code"))
    return completed


def _cache_key(latitude: float, longitude: float) -> tuple[float, float]:
    return round(float(latitude), 6), round(float(longitude), 6)


def _throttle_request() -> None:
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        _LAST_REQUEST_AT = time.monotonic()


def parse_nominatim_address(payload: dict) -> GeoResult:
    """Map Nominatim address JSON ke field administratif HPDB."""
    address = payload.get("address") if isinstance(payload, dict) else {}
    if not isinstance(address, dict):
        address = {}

    province = _first_text(address.get("state"), address.get("region"))
    regency_city = _first_text(
        address.get("city"),
        address.get("town"),
        address.get("regency"),
        address.get("county"),
        address.get("state_district"),
        address.get("municipality"),
    )
    district = _first_text(
        address.get("city_district"),
        address.get("district"),
        address.get("suburb"),
        address.get("township"),
        address.get("municipality"),
        address.get("borough"),
    )
    sub_district = _first_text(
        address.get("village"),
        address.get("neighbourhood"),
        address.get("hamlet"),
        address.get("quarter"),
        address.get("residential"),
    )
    postal_code = _first_text(address.get("postcode"))

    result: GeoResult = {}
    if province:
        result["province"] = province
    if regency_city:
        result["regency_city"] = regency_city
        result["city"] = regency_city
    if district:
        result["district"] = district
    if sub_district:
        result["sub_district"] = sub_district
        result["subdistrict"] = sub_district
    if postal_code:
        result["postal_code"] = postal_code
        result["postalcode"] = postal_code
    return apply_admin_area_overrides(result)


def reverse_geocode(latitude: float, longitude: float) -> GeoResult | None:
    """Reverse geocode satu koordinat memakai Nominatim, return None kalau gagal."""
    key = _cache_key(latitude, longitude)
    if key in _CACHE:
        return dict(_CACHE[key])

    query = urllib.parse.urlencode(
        {
            "lat": f"{float(latitude):.8f}",
            "lon": f"{float(longitude):.8f}",
            "format": "jsonv2",
            "addressdetails": 1,
            "accept-language": "id",
        }
    )
    url = f"{NOMINATIM_URL}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        _throttle_request()
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                LOGGER.warning("Reverse geocode failed with HTTP %s", response.status)
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("Reverse geocode failed: %s", exc)
        return None

    result = parse_nominatim_address(payload)
    if result:
        enriched = resolve_from_nominatim(result)
        if enriched:
            result.update(enriched)
        _CACHE[key] = dict(result)
        return result
    return None


def reverse_geocode_with_fallback(latitude: float, longitude: float, retries: int = 1) -> GeoResult | None:
    """Reverse geocode dengan retry ringan; selalu fallback None kalau gagal."""
    attempts = max(int(retries or 0), 0) + 1
    for attempt in range(attempts):
        result = reverse_geocode(latitude, longitude)
        if result:
            return result
        if attempt < attempts - 1:
            time.sleep(1)
    return None
