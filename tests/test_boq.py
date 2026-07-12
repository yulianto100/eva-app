import math
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("pyproj")

from boq import (
    BOQ_FEEDER_NRO_SHEET_NAME,
    BOQ_FEEDER_SHEET_NAME,
    BOQ_NRO_CLUSTER_CELL,
    BOQ_NRO_OLT_CELL,
    BOQ_NRO_SHEET_NAME,
    BOQ_SHEET_NAME,
    BoqParseResult,
    _feeder_collect_data,
    calculate_feeder_boq_design_summary_values,
    calculate_linestring_length_meter,
    calculate_boq_nro_cell_values,
    detect_cable_type,
    evaluate_formula_expression,
    extract_cluster_name_from_kmz_filename,
    extract_parent_line_fdt,
    generate_boq_excel,
    normalize_kml_color_to_rgb_hex,
    parse_feeder_kmz_structure,
    parse_kmz_structure,
    write_boq_feeder,
)


def make_template(path):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = BOQ_SHEET_NAME
    worksheet["A1"] = "Template marker"
    worksheet["M2"] = "=SUM(C2:L2)"
    worksheet["C24"] = "=C2"
    worksheet["C53"] = "=C77"
    worksheet["C61"] = "=C45"
    worksheet["C62"] = "=C46"
    worksheet["C63"] = "=C47"
    worksheet["C64"] = "=C48"
    worksheet["C84"] = "=MAX(C49:C52)+MAX(C53:C56)+SUM(C61:C68)"
    nro_sheet = workbook.create_sheet(BOQ_NRO_SHEET_NAME)
    nro_sheet["A1"] = "NRO marker"
    nro_sheet["N6"] = "No Material"
    nro_sheet["B6"] = "Description/Item"
    nro_sheet["D6"] = "Material Qty"
    nro_sheet["E6"] = "Service Qty"
    nro_sheet["N8"] = 500004774
    nro_sheet["B8"] = "Pengamanan Perizinan dan K3"
    nro_sheet["E8"] = 1.2
    nro_sheet["N9"] = 500003890
    nro_sheet["B9"] = "Transportasi, Mobilisasi & demobilisasi"
    nro_sheet["E9"] = 1
    nro_sheet["N15"] = 200000100
    nro_sheet["B15"] = "FO core type SM G.652.D-ADSS 24 cores"
    nro_sheet["D15"] = "=E16"
    nro_sheet["N16"] = 500002050
    nro_sheet["B16"] = "Instal FO core type SM G.652.D-ADSS 24 cores"
    nro_sheet["E16"] = f"=SUM('{BOQ_SHEET_NAME}'!C24:L27)"
    nro_sheet["N162"] = 500099999
    nro_sheet["B162"] = "Double pole service"
    nro_sheet["D162"] = f"=SUM('{BOQ_SHEET_NAME}'!C53:L56)"
    nro_sheet["E162"] = f"=SUM('{BOQ_SHEET_NAME}'!C53:L56)*2"
    workbook.create_sheet("Do Not Touch")
    workbook["Do Not Touch"]["A1"] = "keep me"
    workbook["Do Not Touch"]["B1"] = 10.2
    workbook["Do Not Touch"]["C1"] = "=B1/2"
    workbook["Do Not Touch"]["D1"] = "10,2"
    workbook.save(path)


def make_feeder_template(path):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = BOQ_FEEDER_SHEET_NAME
    for row in range(2, 9):
        worksheet[f"C{row}"] = 0
    for row in range(9, 16):
        route_row = row - 7
        slack_row = row + 7
        worksheet[f"C{row}"] = f"=ROUNDUP(((C{route_row})+(C{slack_row}*20))*1.05,0)"
    for row in range(16, 23):
        worksheet[f"C{row}"] = 0
    for row in range(23, 30):
        worksheet[f"C{row}"] = 0
    for row in range(49, 60):
        worksheet[f"C{row}"] = 0
    worksheet["C63"] = 0
    worksheet["C66"] = 0
    workbook.create_sheet(BOQ_FEEDER_NRO_SHEET_NAME)
    workbook.save(path)


def feeder_boq_kml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>CABLE</name>
        <Placemark>
          <name>SF TEST - FO 24C/2T - 100 m</name>
          <LineString><coordinates>0,0,0 0.00089,0,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>SLACK HANGER</name>
        <Placemark><name>SH-1</name><Point><coordinates>0,0,0</coordinates></Point></Placemark>
        <Placemark><name>SH-2</name><Point><coordinates>0.0001,0,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>JOINT CLOSURE</name>
        <Placemark><name>NEW JOINT CLOSURE 24C</name><Point><coordinates>0.0002,0,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>NEW POLE 7-3</name>
        <Placemark><name>POLE-1</name><Point><coordinates>0.0003,0,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>EXISTING POLE PARTNER 7-4</name>
        <Placemark><name>EXT.MR.001</name><Point><coordinates>0.0004,0,0</coordinates></Point></Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""


def boq_kml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Style id="fdtBlue"><IconStyle><color>ffff0000</color></IconStyle></Style>
    <StyleMap id="fdtBlueMap">
      <Pair><key>normal</key><styleUrl>#fdtBlue</styleUrl></Pair>
      <Pair><key>highlight</key><styleUrl>#unused</styleUrl></Pair>
    </StyleMap>
      <Folder>
        <name>PROJECT</name>
        <Folder>
          <name>HP COVER</name>
          <Placemark><name>HP-01</name><Point><coordinates>106.8000,-6.2004,0</coordinates></Point></Placemark>
          <Placemark><name>HP-02</name><Point><coordinates>106.8001,-6.2004,0</coordinates></Point></Placemark>
          <Placemark><name>HP-03</name><Point><coordinates>106.8002,-6.2004,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>FDT</name>
        <Placemark>
          <name>PSGMB034.062</name>
          <styleUrl>#fdtBlueMap</styleUrl>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>PSGMB034.063</name>
          <Style><LabelStyle><color>ffffff00</color></LabelStyle></Style>
          <Point><coordinates>106.8010,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>PSGMB034.062 - CABLE LINE A - ( FO 24C/2T ) - AE - 1044M</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8000,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>SLING WIRE</name>
          <Placemark>
            <name>Total Route : 3.2 m</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8000,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>FAT</name>
          <Placemark><name>A01</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
          <Placemark><name>A02</name><Point><coordinates>106.8001,-6.2000,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-2.5</name>
          <Placemark><name>NP-A1</name><Point><coordinates>106.8000,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>EXISTING POLE PARTNER 7-4</name>
          <Placemark><name>EXT.TLKM.PSGMB034.P006</name><Point><coordinates>106.8000,-6.2002,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.TELKOM.PLMGR.P001</name><Point><coordinates>106.8004,-6.2002,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.MTI.PSGMB034.P010</name><Point><coordinates>106.8001,-6.2002,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.LN.PSGMB034.P011</name><Point><coordinates>106.8002,-6.2002,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.UNKNOWN.PSGMB034.P012</name><Point><coordinates>106.8003,-6.2002,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>EXISTING POLE EMR 7-3</name>
          <Placemark><name>MR-A1</name><Point><coordinates>106.8000,-6.2003,0</coordinates></Point></Placemark>
        </Folder>
      </Folder>
      <Folder>
        <name>LINE B - FDT 1</name>
        <Folder>
          <name>SLING WIRE</name>
          <Placemark>
            <name>Total Route : 4.1 m</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8000,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-2.5</name>
          <Placemark><name>NP-B1</name><Point><coordinates>106.8010,-6.2001,0</coordinates></Point></Placemark>
          <Placemark><name>NP-B2</name><Point><coordinates>106.8011,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""


def test_normalize_kml_color_converts_aabbggrr_to_rgb():
    assert normalize_kml_color_to_rgb_hex("ff0000ff") == "#FF0000"
    assert normalize_kml_color_to_rgb_hex("ffff0000") == "#0000FF"
    assert normalize_kml_color_to_rgb_hex("#AA00FF") == "#AA00FF"


def test_extract_cluster_name_from_kmz_filename_strips_project_code_and_upload_prefix():
    assert extract_cluster_name_from_kmz_filename("BKS004470 - SERANG RW 05 DAN 06 BEKASI.kmz") == "SERANG RW 05 DAN 06 BEKASI"
    assert (
        extract_cluster_name_from_kmz_filename("abc123abc123abc123abc123abc123ab_2_BKS004470_-_SERANG_RW_05_DAN_06_BEKASI.kmz")
        == "SERANG RW 05 DAN 06 BEKASI"
    )


def test_extract_parent_line_fdt_accepts_line_only_and_loose_separators():
    assert extract_parent_line_fdt(["PROJECT", "LINE A", "FAT"]) == ("A", 1, "LINE A - FDT 1")
    assert extract_parent_line_fdt(["PROJECT", "LINE B FDT 2", "NEW POLE 7-2.5"]) == ("B", 2, "LINE B - FDT 2")
    assert extract_parent_line_fdt(["PROJECT", "LINE C_FDT_3", "SLING WIRE"]) == ("C", 3, "LINE C - FDT 3")
    assert extract_parent_line_fdt(["PROJECT", "LINE D - FDT 4", "DISTRIBUTION CABLE"]) == ("D", 4, "LINE D - FDT 4")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24C", "24C/2T"),
        ("36C", "36C/3T"),
        ("48C", "48C/4T"),
        ("FO 24C", "24C/2T"),
        ("FO 36C", "36C/3T"),
        ("FO 48C", "48C/4T"),
    ],
)
def test_detect_cable_type_accepts_short_distribution_capacity(value, expected):
    assert detect_cable_type(value) == expected

def test_parse_kmz_structure_maps_distribution_cable_short_capacity_folder(tmp_path):
    kml_path = tmp_path / "distribution-short-capacity.kml"
    coordinates = [(0, 0), (0.00089, 0)]
    expected_length = math.ceil(calculate_linestring_length_meter([coordinates]))
    kml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Folder>
            <name>48C</name>
            <Placemark>
              <name>CABLE LINE A</name>
              <LineString><coordinates>0,0,0 0.00089,0,0</coordinates></LineString>
            </Placemark>
          </Folder>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = parse_kmz_structure(kml_path)

    assert result.cell_values["C10"] == expected_length
    assert any(
        row.source_folder == "DISTRIBUTION CABLE"
        and row.cable_type == "48C/4T"
        and row.target_cell == "C10"
        for row in result.preview_rows
    )

def test_evaluate_formula_expression_handles_mixed_range_functions():
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = BOQ_SHEET_NAME
    worksheet["C53"] = 1
    worksheet["C54"] = 1
    worksheet["C61"] = 10
    worksheet["C62"] = 17

    result = evaluate_formula_expression(
        workbook,
        BOQ_SHEET_NAME,
        "MAX(C49:C52)+MAX(C53:C56)+SUM(C61:C68)",
        {},
        set(),
    )

    assert result == 28

def test_calculate_boq_nro_cell_values_reads_finalized_cable_loop_holder(tmp_path):
    template_path = tmp_path / "template.xlsx"
    kml_path = tmp_path / "BKS004470 - SERANG RW 05 DAN 06 BEKASI.kml"
    make_template(template_path)
    workbook = openpyxl.load_workbook(template_path)
    workbook[BOQ_NRO_SHEET_NAME]["D225"] = f"=SUM('{BOQ_SHEET_NAME}'!C84:L84)"
    workbook.save(template_path)
    kml_path.write_text(boq_kml(), encoding="utf-8")

    values = calculate_boq_nro_cell_values(kml_path, template_path, ["D225"])

    assert values["D225"] == 5

def test_generate_boq_excel_writes_target_sheets_from_template(tmp_path):
    template_path = tmp_path / "template.xlsx"
    output_path = tmp_path / "PROJECT_BOQ.xlsx"
    kml_path = tmp_path / "BKS004470 - SERANG RW 05 DAN 06 BEKASI.kml"
    make_template(template_path)
    kml_path.write_text(boq_kml(), encoding="utf-8")

    result = generate_boq_excel(kml_path, output_path, template_path)
    workbook = openpyxl.load_workbook(output_path, data_only=False)
    worksheet = workbook[BOQ_SHEET_NAME]
    nro_worksheet = workbook[BOQ_NRO_SHEET_NAME]

    assert output_path.exists()
    assert worksheet["C2"].value == 1044
    assert worksheet["C15"].value == 8
    assert worksheet["C39"].value == 1
    assert worksheet["D40"].value == 1
    assert worksheet["C45"].value == 2
    assert worksheet["C77"].value == 3
    assert worksheet["C80"].value == 1
    assert worksheet["C81"].value == 1
    assert worksheet["C82"].value == 1
    assert worksheet["C83"].value == 1
    assert worksheet["M2"].value == 1044
    assert worksheet["C24"].value == 1044
    assert worksheet["C53"].value == 3
    assert nro_worksheet["O3"].value == "SERANG RW 05 DAN 06 BEKASI"
    assert nro_worksheet["O5"].value == 3
    assert workbook["Do Not Touch"]["A1"].value == "keep me"
    assert workbook["Do Not Touch"]["B1"].value == 11
    assert workbook["Do Not Touch"]["C1"].value == 6
    assert workbook["Do Not Touch"]["D1"].value == 11
    formula_values = [
        cell.value
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith("=")
    ]
    assert formula_values == []
    numeric_values = [
        cell.value
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
    ]
    assert all(float(value).is_integer() for value in numeric_values)
    assert result["summary"]["total_hp_cover"] == 3
    assert result["summary"]["total_mapped_boq_item"] >= 9
    assert result["summary"]["total_warning_unmapped"] == 2
    assert any(row["status"] == "Unmapped" for row in result["preview_rows"])
    assert any("EXT.TELKOM.PLMGR.P001" in (row.get("message") or "") for row in result["preview_rows"])
    assert [column["label"] for column in result["nro_preview"]["columns"]] == [
        "No Material",
        "Description/Item",
        "Material Qty",
        "Service Qty",
    ]
    nro_rows = {row["row"]: row["values"] for row in result["nro_preview"]["rows"]}
    assert sorted(nro_rows) == [8, 9, 15, 16]
    assert nro_rows[8] == {
        "N": 500004774,
        "B": "Pengamanan Perizinan dan K3",
        "D": None,
        "E": 2,
    }
    assert nro_rows[15]["D"] == 1044
    assert nro_rows[16]["E"] == 1044
    assert nro_worksheet["D162"].value is None
    assert nro_worksheet["E162"].value is None

def test_generate_boq_excel_writes_nro_olt_and_cluster_overrides(tmp_path):
    template_path = tmp_path / "template.xlsx"
    output_path = tmp_path / "PROJECT_BOQ.xlsx"
    kml_path = tmp_path / "825a61072f26412dbfa5bee76ebb7bae_STANDARDIZED_1.kmz"
    make_template(template_path)
    with zipfile.ZipFile(kml_path, "w") as archive:
        archive.writestr("doc.kml", boq_kml())

    result = generate_boq_excel(
        kml_path,
        output_path,
        template_path,
        cluster_name_override="PERUMAHAN JATIMULYA BLOK I BEKASI",
        olt_name="OLT Taman Narogong",
    )

    workbook = openpyxl.load_workbook(output_path, data_only=False)
    nro_worksheet = workbook[BOQ_NRO_SHEET_NAME]
    assert result["nro_cell_values"][BOQ_NRO_OLT_CELL] == "OLT Taman Narogong"
    assert result["nro_cell_values"][BOQ_NRO_CLUSTER_CELL] == "PERUMAHAN JATIMULYA BLOK I BEKASI"
    assert nro_worksheet[BOQ_NRO_OLT_CELL].value == "OLT Taman Narogong"
    assert nro_worksheet[BOQ_NRO_CLUSTER_CELL].value == "PERUMAHAN JATIMULYA BLOK I BEKASI"


def test_parse_kmz_structure_reads_doc_kml(tmp_path):
    kmz_path = tmp_path / "project.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", boq_kml())

    result = parse_kmz_structure(kmz_path)

    assert result.cell_values["C2"] == 1044
    assert result.cell_values["C77"] == 3
    assert result.nro_cell_values["O5"] == 3
    assert result.summary["total_fdt_detected"] == 2

def test_parse_kmz_structure_maps_fdt_without_suffix_by_folder_order(tmp_path):
    kml_path = tmp_path / "BKS000001 - AS PLAN.kml"
    kml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Style id="fdtBlue"><IconStyle><color>ffff0000</color></IconStyle></Style>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT</name>
          <styleUrl>#fdtBlue</styleUrl>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = parse_kmz_structure(kml_path)

    assert result.cell_values["C39"] == 1
    assert result.summary["total_fdt_detected"] == 1
    assert not any("FDT suffix could not be extracted" in warning for warning in result.warnings)
    assert any(row.source_folder == "FDT" and row.status == "Mapped" for row in result.preview_rows)

def test_parse_kmz_structure_maps_ug_subduct_handhole_and_twey(tmp_path):
    kml_path = tmp_path / "BKS000001 - UG SAMPLE.kml"
    subduct_32 = [(106.8000, -6.2000), (106.8001, -6.2000)]
    subduct_40 = [(106.8000, -6.2000), (106.8000, -6.2002)]
    kml_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>SUBDUCT 32/28</name>
          <Placemark>
            <name>999</name>
            <LineString><coordinates>{subduct_32[0][0]},{subduct_32[0][1]},0 {subduct_32[1][0]},{subduct_32[1][1]},0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>SUBDUCT 40/34</name>
          <Placemark>
            <name>43</name>
            <LineString><coordinates>{subduct_40[0][0]},{subduct_40[0][1]},0 {subduct_40[1][0]},{subduct_40[1][1]},0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>NEW HH 20X20X20</name>
          <Placemark><name>HH20</name><Point><coordinates>106.7999,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW HH 40X40X60</name>
          <Placemark><name>HH40</name><Point><coordinates>106.8000,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW HH 80X80X130</name>
          <Placemark><name>HH80</name><Point><coordinates>106.8001,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>TWEY</name>
          <Placemark><name>Reducer 1</name><Point><coordinates>106.8002,-6.2001,0</coordinates></Point></Placemark>
          <Placemark><name>Reducer 2</name><Point><coordinates>106.8003,-6.2001,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>TALI PANICNG</name>
          <Placemark>
            <name>Typo folder still canonical</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8001,-6.2001,0</coordinates></LineString>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = parse_kmz_structure(kml_path)

    assert result.cell_values["C108"] == math.ceil(calculate_linestring_length_meter([subduct_32]))
    assert result.cell_values["C109"] == math.ceil(calculate_linestring_length_meter([subduct_40]))
    assert result.cell_values["C109"] != 43
    assert result.cell_values["C117"] == 1
    assert result.cell_values["C116"] == 1
    assert result.cell_values["C119"] == 1
    assert result.cell_values["C125"] == 2
    assert result.summary["total_subduct_length"] == result.cell_values["C108"] + result.cell_values["C109"]
    assert result.summary["total_ug_handhole_detected"] == 3
    assert result.summary["total_ug_twey_detected"] == 2


def test_feeder_boq_cell_mapping_and_formula_rows(tmp_path):
    template_path = tmp_path / "feeder-template.xlsx"
    output_path = tmp_path / "feeder-output.xlsx"
    kmz_path = tmp_path / "SF_TEST.kmz"
    make_feeder_template(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", feeder_boq_kml())

    data = _feeder_collect_data(kmz_path)
    assert math.ceil(data["cable_route"]["24C/2T"]) == 100
    assert data["slack_hanger_count"] == 2
    assert data["joint_closure_new"][24] == 1
    assert data["new_pole"]["NEW POLE 7-3"] == 1
    assert data["partner_counts"]["EXT.MR"] == 1
    summary_values = calculate_feeder_boq_design_summary_values(kmz_path, template_path, network_type="AE")
    assert summary_values["(FO CABLE AERIAL 24/2T)"] == 147
    assert summary_values["(FO CABLE AERIAL 36/3T)"] == 0
    assert "TOTAL TRENCHING" not in summary_values

    write_boq_feeder(
        template_path,
        output_path,
        BoqParseResult({}, {}, [], [], {}),
        input_paths=kmz_path,
        network_type="AE",
    )

    workbook = openpyxl.load_workbook(output_path, data_only=False)
    worksheet = workbook[BOQ_FEEDER_SHEET_NAME]
    assert worksheet["C2"].value == 100
    assert worksheet["C16"].value == 2
    assert worksheet["C23"].value == 1
    assert worksheet["C50"].value == 1
    assert worksheet["C59"].value == 1
    # Formulas in rows 9-15 are frozen to cached numeric results so the
    # values render in Excel Protected View before the user clicks "Enable
    # Editing". `freeze_workbook_cached_values()` replaces each formula with
    # its computed value during write.
    for row in range(9, 16):
        cell_value = worksheet[f"C{row}"].value
        assert cell_value is not None
        assert not str(cell_value).startswith("=")
        assert isinstance(cell_value, (int, float))
    assert workbook.calculation.fullCalcOnLoad is True
    assert workbook.calculation.calcMode == "auto"


def test_feeder_collects_distribution_cable_short_capacity_folder(tmp_path):
    kml_path = tmp_path / "SF_TEST.kml"
    coordinates = [(0, 0), (0.00089, 0)]
    expected_length = calculate_linestring_length_meter([coordinates])
    kml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>DISTRIBUTION CABLE</name>
        <Folder>
          <name>36C</name>
          <Placemark>
            <name>CABLE LINE A</name>
            <LineString><coordinates>0,0,0 0.00089,0,0</coordinates></LineString>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    data = _feeder_collect_data(kml_path)

    assert data["cable_route"]["36C/3T"] == pytest.approx(expected_length)

def test_feeder_boq_c63_uses_fdt_coding_count(tmp_path):
    template_path = tmp_path / "feeder-template.xlsx"
    output_path = tmp_path / "feeder-output.xlsx"
    kmz_path = tmp_path / "SF_TEST.kmz"
    make_feeder_template(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", feeder_boq_kml())

    result = generate_boq_excel(
        kmz_path,
        output_path,
        template_path,
        scope_type="Feeder",
        fdt_coding_count=3,
    )

    workbook = openpyxl.load_workbook(output_path, data_only=False)
    worksheet = workbook[BOQ_FEEDER_SHEET_NAME]
    assert worksheet["C63"].value == 15
    assert result["cell_values"]["C63"] == 15
    assert result["summary"]["total_fdt_coding_detected"] == 3
    assert result["summary"]["total_core_sf"] == 15

def test_feeder_boq_preview_uses_flat_cable_without_parent_warnings(tmp_path):
    kmz_path = tmp_path / "SF_TEST.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", feeder_boq_kml())

    result = parse_feeder_kmz_structure(kmz_path, network_type="AE")

    assert result.warnings == []
    assert any(row.source_folder == "CABLE" and row.target_cell == "C2" for row in result.preview_rows)
    assert not any(row.source_folder == "DISTRIBUTION CABLE" for row in result.preview_rows)
    assert not any("Invalid parent" in row.parent_line_fdt for row in result.preview_rows)
    assert not any("parent LINE/FDT invalid" in row.message for row in result.preview_rows)
    assert result.summary["cable_length"] == 100


def test_feeder_design_summary_values_read_nro_cells_and_boring_folder(tmp_path):
    template_path = tmp_path / "feeder-template.xlsx"
    kmz_path = tmp_path / "SF_BORING.kmz"
    make_feeder_template(template_path)
    workbook = openpyxl.load_workbook(template_path)
    worksheet = workbook[BOQ_FEEDER_NRO_SHEET_NAME]
    worksheet["E109"] = 10
    worksheet["E112"] = 4
    workbook.save(template_path)
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BORING</name>
      <Placemark>
        <name>Boring 1</name>
        <LineString><coordinates>0,0,0 0.0005,0,0</coordinates></LineString>
      </Placemark>
    </Folder>
  </Document>
</kml>
""",
        )

    values = calculate_feeder_boq_design_summary_values(kmz_path, template_path, network_type="UG")

    assert values["TOTAL TRENCHING"] == 10
    assert values["TOTAL BORING"] == 4

    worksheet["E112"] = None
    workbook.save(template_path)

    values = calculate_feeder_boq_design_summary_values(kmz_path, template_path, network_type="UG")

    assert values["TOTAL BORING"] == 56
