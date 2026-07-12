import json
from io import BytesIO

import geocode


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_parse_nominatim_address_maps_indonesia_admin_fields():
    result = geocode.parse_nominatim_address(
        {
            "address": {
                "village": "Karangraharja",
                "city_district": "Cikarang Utara",
                "county": "Kabupaten Bekasi",
                "state": "Jawa Barat",
                "postcode": "17530",
            }
        }
    )

    assert result["province"] == "Jawa Barat"
    assert result["regency_city"] == "Kabupaten Bekasi"
    assert result["district"] == "Cikarang Utara"
    assert result["sub_district"] == "Karangraharja"
    assert result["postal_code"] == "17530"


def test_parse_nominatim_address_uses_postal_override_for_known_missing_district():
    result = geocode.parse_nominatim_address(
        {
            "address": {
                "village": "Waringinjaya",
                "regency": "Kab Bekasi",
                "state": "Jawa Barat",
                "postcode": "17540",
            }
        }
    )

    assert result["province"] == "Jawa Barat"
    assert result["regency_city"] == "Kab Bekasi"
    assert result["district"] == "Kedung Waringin"
    assert result["sub_district"] == "Waringinjaya"
    assert result["postal_code"] == "17540"


def test_parse_nominatim_address_does_not_cross_fill_unknown_missing_district():
    result = geocode.parse_nominatim_address(
        {
            "address": {
                "village": "Desa Contoh",
                "regency": "Kab Contoh",
                "state": "Jawa Barat",
                "postcode": "99999",
            }
        }
    )

    assert result["regency_city"] == "Kab Contoh"
    assert "district" not in result
    assert result["sub_district"] == "Desa Contoh"


def test_reverse_geocode_returns_mapped_payload(monkeypatch):
    geocode._CACHE.clear()
    monkeypatch.setattr(geocode, "_throttle_request", lambda: None)
    monkeypatch.setattr(
        geocode.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {
                "address": {
                    "village": "Pedurenan",
                    "city_district": "Mustikajaya",
                    "city": "Kota Bekasi",
                    "state": "Jawa Barat",
                    "postcode": "17156",
                }
            }
        ),
    )

    result = geocode.reverse_geocode(-6.2649, 107.2557)

    assert result == {
        "province": "Jawa Barat",
        "regency_city": "Kota Bekasi",
        "city": "Kota Bekasi",
        "district": "Mustikajaya",
        "sub_district": "Pedurenan",
        "subdistrict": "Pedurenan",
        "postal_code": "17156",
        "postalcode": "17156",
    }


def test_reverse_geocode_handles_invalid_json(monkeypatch):
    geocode._CACHE.clear()
    monkeypatch.setattr(geocode, "_throttle_request", lambda: None)

    class InvalidResponse(FakeResponse):
        def __init__(self):
            super().__init__({})

        def read(self):
            return BytesIO(b"{").read()

    monkeypatch.setattr(geocode.urllib.request, "urlopen", lambda request, timeout: InvalidResponse())

    assert geocode.reverse_geocode(-6.2649, 107.2557) is None


def test_reverse_geocode_with_fallback_retries(monkeypatch):
    calls = {"count": 0}

    def fake_reverse_geocode(latitude, longitude):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return {"district": "Mustikajaya"}

    monkeypatch.setattr(geocode, "reverse_geocode", fake_reverse_geocode)
    monkeypatch.setattr(geocode.time, "sleep", lambda seconds: None)

    assert geocode.reverse_geocode_with_fallback(-6.2649, 107.2557, retries=1) == {"district": "Mustikajaya"}
    assert calls["count"] == 2
