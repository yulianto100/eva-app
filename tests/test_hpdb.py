import json
import zipfile
from datetime import date

import pytest

openpyxl = pytest.importorskip("openpyxl")

from hpdb import (
    HPDB_CLUSTER_SHEET_NAME,
    HPDB_FAT_SHEET_NAME,
    HPDB_HOMEPASS_SHEET_NAME,
    collect_hpdb_placemarks,
    extract_fdt_suffix_number,
    generate_astri_excel,
    normalize_city_value,
    normalize_fat_code,
    normalize_province_value,
    normalize_street_value,
    parse_homepass_block_and_number,
    parse_hpdb_from_kmz,
)
from hpdb_mancore import generate_hpdb_excel as generate_mancore_hpdb_excel


CLUSTER_HEADERS = [
    "cluster_name",
    "fdt_code",
    "commercial_name",
    "province",
    "competition",
    "city",
    "area_name",
    "rfs_date",
    "district",
    "acquisition_class",
    "acquisition_tier",
    "postal_code",
    "sub_district",
    "olt_code",
    "cluster_type",
    "complex",
    "project_name",
    "project_id",
    "latitude_cluster",
    "longitude_cluster",
    "cluster_code_astri",
]

FAT_HEADERS = [
    "Area",
    "Clustername",
    "FAT Code",
    "FDT Code",
    "Port Count",
    "FAT Latitude",
    "FAT Longitude",
]

HOMEPASS_HEADERS = [
    "postalcode",
    "Roll_Out_ID",
    "acquisition_class",
    "acquisition_tier",
    "competition",
    "project_id",
    "projectName",
    "Complex_name",
    "ClusterId",
    "Clustername",
    "Commercial_name",
    "Rw",
    "Rt",
    "address_prefix",
    "street",
    "address_suffix",
    "sub_address_prefix",
    "sub_address",
    "sub_address_suffix",
    "block",
    "homenumber",
    "dwelling_type",
    "building_type",
    "building_property_name",
    "building_property_location",
    "floor",
    "unit",
    "homepass_source",
    "oltcode",
    "olthostname",
    "fdtcode",
    "fatcode",
    "RFSDate",
    "Latitude_homepass",
    "Longitude_homepass",
    "vendor_homepass_id",
    "residential_service_ready",
    "sme_service_ready",
    "enterprise_service_ready",
    "installation",
    "availability",
    "classification",
    "business_case",
    "network_presence",
    "wallplate_installation",
    "publish",
    "homepass_smt_id",
]


def make_hpdb_template(path):
    workbook = openpyxl.Workbook()
    cluster = workbook.active
    cluster.title = HPDB_CLUSTER_SHEET_NAME
    cluster.append(CLUSTER_HEADERS)
    cluster.append(["default"] + [""] * (len(CLUSTER_HEADERS) - 1))
    fat = workbook.create_sheet(HPDB_FAT_SHEET_NAME)
    fat.append(FAT_HEADERS)
    fat.append(["koordinat path FAT"] + [""] * (len(FAT_HEADERS) - 1))
    homepass = workbook.create_sheet(HPDB_HOMEPASS_SHEET_NAME)
    homepass.append(HOMEPASS_HEADERS)
    homepass.append(["nama path HP Cover"] + [""] * (len(HOMEPASS_HEADERS) - 1))
    workbook.save(path)

def header_index(worksheet, header: str) -> int:
    for cell in worksheet[1]:
        if cell.value == header:
            return int(cell.column)
    raise AssertionError(f"Header not found: {header}")

def test_normalize_city_value_adds_kabupaten_abbreviation_dot():
    assert normalize_city_value("Kab Bekasi") == "Kab. Bekasi"
    assert normalize_city_value("Kabupaten Bekasi") == "Kab. Bekasi"
    assert normalize_city_value("Kab. Bekasi") == "Kab. Bekasi"
    assert normalize_city_value("Kota Bekasi") == "Kota Bekasi"


def test_normalize_street_value_strips_road_prefixes_and_uppercases():
    assert normalize_street_value("Jalan Masjid Jami As-syuhada") == "MASJID JAMI AS-SYUHADA"
    assert normalize_street_value("Gang Haji Hotid") == "HAJI HOTID"
    assert normalize_street_value("Jl. Warung Gantung") == "WARUNG GANTUNG"
    assert normalize_street_value("Gg. Pintu Air II") == "PINTU AIR II"


def test_normalize_province_value_maps_short_java_value():
    assert normalize_province_value("Jawa") == "Jawa"
    assert normalize_province_value("jabar") == "Jawa Barat"


def test_parse_homepass_block_keeps_no_block_tokens_in_homenumber():
    assert parse_homepass_block_and_number("RUKO 16") == ("", "RUKO 16")
    assert parse_homepass_block_and_number("RK/3") == ("", "RK/3")
    assert parse_homepass_block_and_number("TK 5") == ("", "TK 5")
    assert parse_homepass_block_and_number("A-12") == ("A", "12")


def hpdb_kml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI</name>
      <Folder>
        <name>FDT</name>
        <Placemark><name>VMJ11.291</name><Point><coordinates>106.8020,-6.2020,0</coordinates></Point></Placemark>
        <Placemark><name>VMJ11.289</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
        <Placemark><name>VMJ11.290</name><Point><coordinates>106.8010,-6.2010,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>FAT A01</name><Point><coordinates>106.8100,-6.2100,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder>
            <name>A01</name>
            <Placemark><name>HP-001</name><Point><coordinates>106.8110,-6.2110,0</coordinates></Point></Placemark>
            <Placemark><name>HP-002</name><Point><coordinates>106.8120,-6.2120,0</coordinates></Point></Placemark>
          </Folder>
        </Folder>
      </Folder>
      <Folder>
        <name>LINE B - FDT 2</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>B01</name><Point><coordinates>106.8200,-6.2200,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder>
            <name>B01</name>
            <Placemark><name>HP-003</name><Point><coordinates>106.8210,-6.2210,0</coordinates></Point></Placemark>
          </Folder>
        </Folder>
        <Folder>
          <name>HP UNCOVER</name>
          <Folder>
            <name>B01</name>
            <Placemark><name>HP-UNCOVER-001</name><Point><coordinates>106.8220,-6.2220,0</coordinates></Point></Placemark>
          </Folder>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""

def named_road_osm() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2110" lon="106.8110"/>
  <node id="2" lat="-6.2120" lon="106.8120"/>
  <node id="3" lat="-6.2210" lon="106.8210"/>
  <node id="4" lat="-6.2220" lon="106.8220"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Jalan Melati"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Jalan Anggrek"/>
  </way>
</osm>
"""

def mancore_kml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI</name>
      <Folder>
        <name>FDT</name>
        <Placemark><name>VMJ11.289</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>FAT A01</name><Point><coordinates>106.8100,-6.2100,0</coordinates></Point></Placemark>
          <Placemark><name>FAT A02</name><Point><coordinates>106.8200,-6.2200,0</coordinates></Point></Placemark>
          <Placemark><name>FAT A06</name><Point><coordinates>106.8600,-6.2600,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-2.5</name>
          <Placemark><name>POLE-A01</name><Point><coordinates>106.8101,-6.2101,0</coordinates></Point></Placemark>
          <Placemark><name>POLE-A02</name><Point><coordinates>106.8201,-6.2201,0</coordinates></Point></Placemark>
          <Placemark><name>POLE-A06</name><Point><coordinates>106.8601,-6.2601,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder>
            <name>A01</name>
            <Placemark><name>HP-A01-01</name><Point><coordinates>106.8110,-6.2110,0</coordinates></Point></Placemark>
            <Placemark><name>HP-A01-02</name><Point><coordinates>106.8120,-6.2120,0</coordinates></Point></Placemark>
            <Placemark><name>HP-A01-03</name><Point><coordinates>106.8130,-6.2130,0</coordinates></Point></Placemark>
          </Folder>
          <Folder>
            <name>A02</name>
            <Placemark><name>HP-A02-01</name><Point><coordinates>106.8210,-6.2210,0</coordinates></Point></Placemark>
            <Placemark><name>HP-A02-02</name><Point><coordinates>106.8220,-6.2220,0</coordinates></Point></Placemark>
          </Folder>
          <Folder>
            <name>A06</name>
            <Placemark><name>HP-A06-01</name><Point><coordinates>106.8610,-6.2610,0</coordinates></Point></Placemark>
            <Placemark><name>HP-A06-02</name><Point><coordinates>106.8620,-6.2620,0</coordinates></Point></Placemark>
          </Folder>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""


def mancore_ug_kml() -> str:
    return (
        mancore_kml()
        .replace("<name>NEW POLE 7-2.5</name>", "<name>NEW HH 40X40X60</name>")
        .replace("POLE-A01", "HH-A01")
        .replace("POLE-A02", "HH-A02")
        .replace("POLE-A06", "HH-A06")
    )

def mancore_single_fdt_multi_line_kml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS004999 - SINGLE FDT MULTI LINE BEKASI</name>
      <Folder>
        <name>FDT</name>
        <Placemark><name>SFM01.001</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>A01</name><Point><coordinates>106.8100,-6.2100,0</coordinates></Point></Placemark>
          <Placemark><name>A02</name><Point><coordinates>106.8200,-6.2200,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-2.5</name>
          <Placemark><name>POLE-A01</name><Point><coordinates>106.8101,-6.2101,0</coordinates></Point></Placemark>
          <Placemark><name>POLE-A02</name><Point><coordinates>106.8201,-6.2201,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder><name>A01</name><Placemark><name>HP-A01</name><Point><coordinates>106.8110,-6.2110,0</coordinates></Point></Placemark></Folder>
          <Folder><name>A02</name><Placemark><name>HP-A02</name><Point><coordinates>106.8210,-6.2210,0</coordinates></Point></Placemark></Folder>
        </Folder>
      </Folder>
      <Folder>
        <name>LINE B - FDT 1</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>B01</name><Point><coordinates>106.8300,-6.2300,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-2.5</name>
          <Placemark><name>POLE-B01</name><Point><coordinates>106.8301,-6.2301,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder><name>B01</name><Placemark><name>HP-B01</name><Point><coordinates>106.8310,-6.2310,0</coordinates></Point></Placemark></Folder>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""

def test_normalizers():
    assert extract_fdt_suffix_number("PSGMB034.062") == 62
    assert normalize_fat_code("FAT A07") == "A07"


def test_parse_hpdb_from_kmz_sorts_fdt_and_ignores_hp_uncover(tmp_path):
    kml_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kml"
    kml_path.write_text(hpdb_kml(), encoding="utf-8")

    result = parse_hpdb_from_kmz(kml_path)

    assert [row["fdt_code"] for row in result.cluster_rows] == ["VMJ11.289", "VMJ11.290", "VMJ11.291"]
    assert result.cluster_rows[0]["cluster_name"] == "PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI - FDT 1"
    assert result.cluster_rows[0]["latitude_cluster"] == -6.2
    assert [row["FAT Code"] for row in result.fat_rows] == ["A01", "B01"]
    assert len(result.homepass_rows) == 3
    assert {row["fatcode"] for row in result.homepass_rows} == {"A01", "B01"}
    assert result.hp_uncover_ignored == 1
    assert all("UNCOVER" not in str(row.get("homenumber", "")) for row in result.homepass_rows)


def test_single_fdt_cluster_names_do_not_include_fdt_suffix(tmp_path):
    kml_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kml"
    kml_path.write_text(mancore_kml(), encoding="utf-8")

    result = parse_hpdb_from_kmz(kml_path)
    base_name = "PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI"

    assert len(result.cluster_rows) == 1
    assert result.cluster_rows[0]["cluster_name"] == base_name
    assert result.cluster_rows[0]["commercial_name"] == base_name
    assert {row["Clustername"] for row in result.fat_rows} == {base_name}
    assert {row["Complex_name"] for row in result.homepass_rows} == {base_name}
    assert {row["Clustername"] for row in result.homepass_rows} == {base_name}
    assert {row["Commercial_name"] for row in result.homepass_rows} == {base_name}

def test_generate_astri_excel_loads_template_and_writes_rows(tmp_path):
    template_path = tmp_path / "TEMPLATE HPDB.xlsx"
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    make_hpdb_template(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", hpdb_kml())

    result = generate_astri_excel(kmz_path, output_path, template_path, process_date=date(2026, 5, 21))
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    cluster = workbook[HPDB_CLUSTER_SHEET_NAME]
    fat = workbook[HPDB_FAT_SHEET_NAME]
    homepass = workbook[HPDB_HOMEPASS_SHEET_NAME]

    assert output_path.exists()
    assert cluster.max_row == 4
    assert fat.max_row == 3
    assert homepass.max_row == 4
    assert cluster["A2"].value == "PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI - FDT 1"
    assert cluster["B2"].value == "VMJ11.289"
    assert cluster["D2"].value in (None, "")
    assert cluster["F2"].value in (None, "")
    assert cluster["G2"].value in (None, "")
    assert cluster["U2"].value == "BKS004012"
    assert fat["C2"].value == "A01"
    assert fat["D3"].value == "VMJ11.290"
    assert homepass["B2"].value == "BKS0526"
    assert homepass["I2"].value == "BKS.GR0526.VMJ11.289"
    assert homepass["K2"].value == "PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI - FDT 1"
    assert homepass["L2"].value == 11
    assert homepass["AE2"].value == "VMJ11.289"
    assert homepass["AF2"].value == "A01"
    assert result["summary"]["total_hp_uncover_ignored"] == 1
    assert result["summary"]["total_homepass_rows"] == 3
    assert result["preview"]["totals"]["homepass"] == 3
    assert len(result["preview"]["homepass"]) == 3

def test_generate_astri_excel_fills_homepass_street_from_nearest_osm_road(tmp_path):
    template_path = tmp_path / "TEMPLATE HPDB.xlsx"
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    osm_path = tmp_path / "roads.osm"
    make_hpdb_template(template_path)
    osm_path.write_text(named_road_osm(), encoding="utf-8")
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", hpdb_kml())

    result = generate_astri_excel([kmz_path, osm_path], output_path, template_path)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    homepass = workbook[HPDB_HOMEPASS_SHEET_NAME]

    assert homepass["O2"].value == "MELATI"
    assert homepass["O3"].value == "MELATI"
    assert homepass["O4"].value == "ANGGREK"
    assert result["summary"]["street_matched"] == 3
    assert result["summary"]["street_unmatched"] == 0

def test_generate_astri_excel_auto_fills_cluster_and_homepass_admin_columns(tmp_path, monkeypatch):
    template_path = tmp_path / "TEMPLATE HPDB.xlsx"
    output_path = tmp_path / "PROJECT_ASTRI.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    make_hpdb_template(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", hpdb_kml())

    def fake_reverse_geocode(latitude, longitude):
        return {
            "province": "Jawa Barat",
            "regency_city": "Kabupaten Bekasi",
            "district": "Kedung Waringin",
            "sub_district": "Waringinjaya",
            "postal_code": "17540",
        }

    monkeypatch.setattr("geocode.reverse_geocode_with_fallback", fake_reverse_geocode)

    result = generate_astri_excel(kmz_path, output_path, template_path, auto_geocode=True)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    cluster = workbook[HPDB_CLUSTER_SHEET_NAME]
    homepass = workbook[HPDB_HOMEPASS_SHEET_NAME]

    assert result["summary"]["geocode_success"] is True
    assert cluster["D2"].value == "Jawa Barat"
    assert cluster["F2"].value == "Kab. Bekasi"
    assert cluster["I2"].value == "Kedung Waringin"
    assert cluster["L2"].value == "17540"
    assert cluster["M2"].value == "Waringinjaya"
    assert homepass["A2"].value == "17540"
    assert homepass["A3"].value == "17540"

def test_generate_astri_excel_single_fdt_writes_base_cluster_name(tmp_path):
    template_path = tmp_path / "TEMPLATE HPDB.xlsx"
    output_path = tmp_path / "PROJECT_ASTRI.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    make_hpdb_template(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_kml())

    generate_astri_excel(kmz_path, output_path, template_path)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    cluster = workbook[HPDB_CLUSTER_SHEET_NAME]
    fat = workbook[HPDB_FAT_SHEET_NAME]
    homepass = workbook[HPDB_HOMEPASS_SHEET_NAME]
    base_name = "PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI"

    assert cluster["A2"].value == base_name
    assert cluster["C2"].value == base_name
    assert fat["B2"].value == base_name
    assert homepass["H2"].value == base_name
    assert homepass["J2"].value == base_name
    assert homepass["K2"].value == base_name

def test_generate_astri_excel_ug_sets_installation_and_template_ae_labels_to_ug(tmp_path):
    template_path = tmp_path / "TEMPLATE HPDB_UG.xlsx"
    output_path = tmp_path / "PROJECT_ASTRI_UG.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    make_hpdb_template(template_path)
    workbook = openpyxl.load_workbook(template_path)
    workbook[HPDB_CLUSTER_SHEET_NAME]["Z1"] = "AE"
    workbook.save(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", hpdb_kml())

    generate_astri_excel(kmz_path, output_path, template_path, network_type="UG")
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    cluster = workbook[HPDB_CLUSTER_SHEET_NAME]
    homepass = workbook[HPDB_HOMEPASS_SHEET_NAME]
    installation_column = header_index(homepass, "installation")

    assert cluster["Z1"].value == "UG"
    assert homepass.cell(row=2, column=installation_column).value == "UG"
    assert homepass.cell(row=3, column=installation_column).value == "UG"

def test_generate_mancore_hpdb_writes_fdt_sheets_ports_cores_and_nearest_poles(tmp_path):
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_kml())

    result = generate_mancore_hpdb_excel(kmz_path, output_path, process_date=date(2026, 5, 21))
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]
    fat = workbook["FAT"]

    assert output_path.exists()
    assert workbook.sheetnames == ["FDT-1", "FAT"]
    assert fdt["A8"].value == "FDT Tray (Front)"
    assert fdt["B8"].value == "FDT \nPort"
    assert fdt["C5"].value == ": PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI"
    assert fdt["A9"].value == "G1"
    assert fdt["B9"].value == 1
    assert fdt["F9"].value == 1
    assert fdt["M9"].value in (None, "")
    assert fdt["N9"].value in (None, "")
    assert fdt["I9"].value == "POLE-A01"
    assert round(float(fdt["J9"].value), 4) == -6.2101
    assert round(float(fdt["K9"].value), 4) == 106.8101
    assert fdt["F10"].value == 2
    assert fdt["A11"].value in (None, "")
    assert fdt["B11"].value in (None, "")
    assert fdt["F11"].value in (None, "")
    assert fdt["H11"].value == 3

    row_by_fat = {}
    for row in range(9, fdt.max_row + 1):
        fat_id = fdt.cell(row=row, column=7).value
        if fat_id:
            row_by_fat.setdefault(fat_id, row)
    assert fdt.cell(row=row_by_fat["A02"], column=2).value == 3
    assert fdt.cell(row=row_by_fat["A06"], column=2).value == 11
    assert fdt.cell(row=row_by_fat["A06"], column=6).value == 13
    assert fdt.cell(row=row_by_fat["A06"] + 1, column=6).value == 14
    roll_out_column = next(cell.column for cell in fdt[8] if cell.value == "Roll_Out_ID")
    cluster_id_column = next(cell.column for cell in fdt[8] if cell.value == "ClusterId")
    assert fdt.cell(row=9, column=roll_out_column).value == "BKS0526"
    assert fdt.cell(row=9, column=cluster_id_column).value == "BKS.GR0526.VMJ11.289"

    assert fat["A2"].value == "A01"
    assert fat["B2"].value == -6.21
    assert fat["C2"].value == 106.81
    assert result["summary"]["total_homepass_rows"] == 7
    assert result["summary"]["hpdb_rows"] == 11
    assert result["summary"]["mancore_fdt_sheets"] == 1
    assert result["summary"]["mancore_poles_matched"] == 3

def test_generate_mancore_hpdb_continues_fdt_ports_across_lines_same_fdt(tmp_path):
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004999 - SINGLE FDT MULTI LINE BEKASI.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_single_fdt_multi_line_kml())

    generate_mancore_hpdb_excel(kmz_path, output_path)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]

    row_by_fat = {}
    for row in range(9, fdt.max_row + 1):
        fat_id = fdt.cell(row=row, column=7).value
        if fat_id:
            row_by_fat.setdefault(fat_id, row)

    assert fdt.cell(row=row_by_fat["A01"], column=2).value == 1
    assert fdt.cell(row=row_by_fat["A02"], column=2).value == 3
    assert fdt.cell(row=row_by_fat["B01"], column=2).value == 5

def test_generate_mancore_hpdb_auto_fills_admin_columns_from_fdt_coordinate(tmp_path, monkeypatch):
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_kml())

    def fake_reverse_geocode(latitude, longitude):
        assert round(latitude, 4) == -6.2
        assert round(longitude, 4) == 106.8
        return {
            "province": "Jawa Barat",
            "regency_city": "Kota Bekasi",
            "district": "Mustikajaya",
            "sub_district": "Pedurenan",
            "postal_code": "17156",
        }

    monkeypatch.setattr("geocode.reverse_geocode_with_fallback", fake_reverse_geocode)

    result = generate_mancore_hpdb_excel(kmz_path, output_path, auto_geocode=True)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]

    assert result["summary"]["geocode_success"] is True
    assert result["summary"]["geocode_fields"]["postal_code"] == "17156"
    for row_index in range(9, fdt.max_row + 1):
        assert fdt.cell(row=row_index, column=13).value == "JAWA BARAT"
        assert fdt.cell(row=row_index, column=14).value == "KOTA BEKASI"
        assert fdt.cell(row=row_index, column=15).value == "MUSTIKAJAYA"
        assert fdt.cell(row=row_index, column=16).value == "PEDURENAN"
        assert fdt.cell(row=row_index, column=17).value == "17156"

def test_generate_mancore_hpdb_uses_fat_coordinate_when_fdt_geocode_incomplete(tmp_path, monkeypatch):
    output_path = tmp_path / "PROJECT_HPDB.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_kml())

    def fake_reverse_geocode(latitude, longitude):
        if round(latitude, 4) == -6.2 and round(longitude, 4) == 106.8:
            return {
                "province": "Jawa Barat",
                "regency_city": "Kab Bekasi",
                "sub_district": "Waringinjaya",
                "postal_code": "17540",
            }
        if round(latitude, 4) == -6.21 and round(longitude, 4) == 106.81:
            return {
                "province": "Jawa Barat",
                "regency_city": "Kab. Bekasi",
                "district": "Tambun Selatan",
                "sub_district": "Waringinjaya",
                "postal_code": "17540",
            }
        return None

    monkeypatch.setattr("geocode.reverse_geocode_with_fallback", fake_reverse_geocode)

    result = generate_mancore_hpdb_excel(kmz_path, output_path, auto_geocode=True)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]

    assert result["summary"]["geocode_success"] is True
    assert round(result["summary"]["geocode_source_lat"], 4) == -6.21
    assert result["summary"]["geocode_fields"]["district"] == "Tambun Selatan"
    for row_index in range(9, fdt.max_row + 1):
        assert fdt.cell(row=row_index, column=14).value == "KAB. BEKASI"
        assert fdt.cell(row=row_index, column=15).value == "TAMBUN SELATAN"
        assert fdt.cell(row=row_index, column=16).value == "WARINGINJAYA"

def test_generate_mancore_hpdb_ug_overrides_installation_defaults(tmp_path):
    output_path = tmp_path / "PROJECT_HPDB_UG.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    defaults_path = tmp_path / "hpdb_defaults_UG.json"
    defaults_path.write_text(json.dumps({"installation": "AE"}), encoding="utf-8")
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_kml())

    generate_mancore_hpdb_excel(kmz_path, output_path, defaults_path=defaults_path, network_type="UG")
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]
    installation_column = next(
        cell.column
        for cell in fdt[8]
        if cell.value == "installation"
    )

    assert fdt.cell(row=9, column=installation_column).value == "UG"

def test_generate_mancore_hpdb_ug_uses_handhole_columns_and_lookup(tmp_path):
    output_path = tmp_path / "PROJECT_HPDB_UG.xlsx"
    kmz_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", mancore_ug_kml())

    result = generate_mancore_hpdb_excel(kmz_path, output_path, network_type="UG")
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    fdt = workbook["FDT-1"]

    assert fdt["I8"].value == "Handhole ID"
    assert fdt["J8"].value == "Handhole Latitude"
    assert fdt["K8"].value == "Handhole Longitude"
    assert fdt["M4"].value == ": 7 HP / Underground"
    assert fdt["I9"].value == "HH-A01"
    assert round(float(fdt["J9"].value), 4) == -6.2101
    assert round(float(fdt["K9"].value), 4) == 106.8101
    assert result["summary"]["mancore_access_label"] == "handhole"
    assert result["summary"]["mancore_poles_missing"] == 0
    assert not any("pole" in issue["message"].lower() for issue in result["issues"])

def hpdb_order_kml(homepass_body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS009999 - ORDER TEST BEKASI</name>
      <Folder>
        <name>FDT</name>
        <Placemark><name>ODR01.001</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>FAT</name>
          <Placemark><name>A01</name><Point><coordinates>106.8100,-6.2100,0</coordinates></Point></Placemark>
          <Placemark><name>B01</name><Point><coordinates>106.8200,-6.2200,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          {homepass_body}
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""


def hp_cover_folder(fat_code: str, placemark_names: list[str], lon_seed: float = 106.8110) -> str:
    placemarks = "".join(
        f'<Placemark><name>{name}</name><Point><coordinates>{lon_seed + index * 0.0001},-6.2110,0</coordinates></Point></Placemark>'
        for index, name in enumerate(placemark_names)
    )
    return f"<Folder><name>{fat_code}</name>{placemarks}</Folder>"


def test_homepass_order_preserves_kmz_walk_order_within_fat(tmp_path):
    kml_path = tmp_path / "BKS009999 - ORDER TEST BEKASI.kml"
    expected = ["DA1,1", "DA1,3", "DA1,5", "DA1,11", "DA1,13"]
    kml_path.write_text(hpdb_order_kml(hp_cover_folder("A01", expected)), encoding="utf-8")

    result = parse_hpdb_from_kmz(kml_path)
    rows = [row for row in result.homepass_rows if row["fatcode"] == "A01"]

    assert [row["homenumber"] for row in rows] == expected
    assert [row["homenumber"] for row in rows] != ["DA1,1", "DA1,11", "DA1,13", "DA1,3", "DA1,5"]


def test_homepass_group_ordering_kept_while_preserving_order_inside_each_fat(tmp_path):
    kml_path = tmp_path / "BKS009999 - GROUP ORDER TEST BEKASI.kml"
    homepass_body = "".join(
        [
            hp_cover_folder("B01", ["EB1,1"], lon_seed=106.8200),
            hp_cover_folder("A01", ["DA1,1"], lon_seed=106.8100),
            hp_cover_folder("B01", ["EB1,3"], lon_seed=106.8202),
            hp_cover_folder("A01", ["DA1,3"], lon_seed=106.8102),
        ]
    )
    kml_path.write_text(hpdb_order_kml(homepass_body), encoding="utf-8")

    result = parse_hpdb_from_kmz(kml_path)

    assert [(row["fatcode"], row["homenumber"]) for row in result.homepass_rows] == [
        ("A01", "DA1,1"),
        ("A01", "DA1,3"),
        ("B01", "EB1,1"),
        ("B01", "EB1,3"),
    ]


def test_hpdb_placemark_kml_index_is_unique_and_sequential(tmp_path):
    kml_path = tmp_path / "BKS009999 - INDEX TEST BEKASI.kml"
    names = ["DA1,1", "DA1,3", "DA1,5"]
    kml_path.write_text(hpdb_order_kml(hp_cover_folder("A01", names)), encoding="utf-8")

    import xml.etree.ElementTree as ET

    root = ET.fromstring(kml_path.read_text(encoding="utf-8"))
    placemarks = []
    collect_hpdb_placemarks(root, [], placemarks)

    assert [placemark.kml_index for placemark in placemarks] == list(range(len(placemarks)))
    assert len({placemark.kml_index for placemark in placemarks}) == len(placemarks)


def test_homepass_order_change_keeps_existing_mapping_fields(tmp_path):
    kml_path = tmp_path / "BKS004012 - PERUMAHAN VILLA MUTIARA JAYA RW 11 BEKASI.kml"
    kml_path.write_text(hpdb_kml(), encoding="utf-8")

    result = parse_hpdb_from_kmz(kml_path)
    row = next(row for row in result.homepass_rows if row["fatcode"] == "A01")

    assert row["ClusterId"] == "BKS.GR0526.VMJ11.289"
    assert row["fdtcode"] == "VMJ11.289"
    assert row["fatcode"] == "A01"
    assert row["homenumber"] == "HP-001"
    assert row["Latitude_homepass"] == -6.211
    assert row["Longitude_homepass"] == 106.811
