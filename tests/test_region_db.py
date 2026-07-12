import sqlite3

import region_db


def test_region_db_resolves_manifest_city_alias_and_district(tmp_path, monkeypatch):
    path = tmp_path / "region.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE regions (
                province TEXT, city TEXT, district TEXT, subdistrict TEXT,
                city_key TEXT, district_key TEXT, subdistrict_key TEXT
            );
            INSERT INTO regions VALUES (
                'Jawa Barat', 'Kabupaten Bekasi', 'Cikarang Utara', 'Karangasih',
                'bekasi', 'cikarangutara', 'karangasih'
            );
            INSERT INTO regions VALUES (
                'Jawa Barat', 'Kota Bekasi', 'Medansatria', 'Harapan Mulya',
                'bekasi', 'medansatria', 'harapanmulya'
            );
            """
        )
    monkeypatch.setattr(region_db, "_REGION_DB_PATH", path)
    region_db._LOCAL.__dict__.clear()

    assert region_db.database_ready() is True
    assert region_db.province_for_city("Bekasi") == "Jawa Barat"
    assert region_db.city_for_kecamatan("Cikarang Utara") == "Kabupaten Bekasi"
    assert region_db.city_for_kecamatan("Medan Satria") == "Kota Bekasi"
    assert region_db.resolve_admin_path(city="Bekasi", district="Cikarang Utara")["subdistrict"] == "Karangasih"
