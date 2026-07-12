import math
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import kmz_standardizer

openpyxl = pytest.importorskip("openpyxl")

from boq import parse_kmz_structure
from kmz_standardizer import (
    FdtCodingRecord,
    _collect_pole_placemarks,
    _compute_cluster_pole_path_order,
    _compute_feeder_pole_path_order,
    _project_distance_along_segment,
    explicit_line_order,
    feeder_standard_folders_for_network,
    feeder_source_olt_reference,
    handhole_source_code,
    is_feeder_project,
    line_standard_folders_for_network,
    parse_fdt_coding_workbook,
    select_project_records,
    spreadsheet_download_url,
    standardize_handhole_placemark_names,
    standardize_kmz_file,
)


def xml_local_name(tag):
    return tag.rsplit("}", 1)[-1]

def direct_child_text(element, name):
    for child in list(element):
        if xml_local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""

def placemark_style_url(root, placemark_name):
    for placemark in root.iter():
        if xml_local_name(placemark.tag) != "Placemark":
            continue
        if direct_child_text(placemark, "name") == placemark_name:
            return direct_child_text(placemark, "styleUrl")
    return ""

def placemark_description(root, placemark_name):
    for placemark in root.iter():
        if xml_local_name(placemark.tag) != "Placemark":
            continue
        if direct_child_text(placemark, "name") == placemark_name:
            return direct_child_text(placemark, "description")
    return ""

def placemark_folder_path(root, placemark_name):
    def walk(element, folder_stack):
        for child in list(element):
            child_name = xml_local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = folder_stack + [direct_child_text(child, "name")]
                found = walk(child, next_stack)
                if found is not None:
                    return found
            elif child_name == "Placemark" and direct_child_text(child, "name") == placemark_name:
                return folder_stack
        return None

    return walk(root, [])

def folder_path_exists(root, expected_path):
    def walk(element, folder_stack):
        if folder_stack == expected_path:
            return True
        for child in list(element):
            child_name = xml_local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = folder_stack + [direct_child_text(child, "name")]
                if walk(child, next_stack):
                    return True
        return False

    return walk(root, [])

def child_folder_names_at_path(root, expected_path):
    def walk(element, folder_stack):
        if folder_stack == expected_path:
            return [
                direct_child_text(child, "name")
                for child in list(element)
                if xml_local_name(child.tag) == "Folder"
            ]
        for child in list(element):
            child_name = xml_local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = folder_stack + [direct_child_text(child, "name")]
                found = walk(child, next_stack)
                if found is not None:
                    return found
        return None

    return walk(root, [])

def document_top_level_folder_names(root):
    document = next(element for element in root.iter() if xml_local_name(element.tag) == "Document")
    return [
        direct_child_text(child, "name")
        for child in list(document)
        if xml_local_name(child.tag) == "Folder"
    ]

def placemark_names(root):
    return [
        direct_child_text(placemark, "name")
        for placemark in root.iter()
        if xml_local_name(placemark.tag) == "Placemark"
    ]

def style_color(root, style_id, style_type):
    for style in root.iter():
        if xml_local_name(style.tag) != "Style" or style.attrib.get("id") != style_id:
            continue
        for child in list(style):
            if xml_local_name(child.tag) == style_type:
                return direct_child_text(child, "color")
    return ""

def style_scale(root, style_id):
    for style in root.iter():
        if xml_local_name(style.tag) != "Style" or style.attrib.get("id") != style_id:
            continue
        for child in list(style):
            if xml_local_name(child.tag) != "IconStyle":
                continue
            return direct_child_text(child, "scale")
    return ""

def style_list_icon(root, style_id):
    for style in root.iter():
        if xml_local_name(style.tag) != "Style" or style.attrib.get("id") != style_id:
            continue
        for list_style in list(style):
            if xml_local_name(list_style.tag) != "ListStyle":
                continue
            for item_icon in list(list_style):
                if xml_local_name(item_icon.tag) == "ItemIcon":
                    return direct_child_text(item_icon, "href")
    return ""


def style_icon_href(root, style_id):
    for style in root.iter():
        if xml_local_name(style.tag) != "Style" or style.attrib.get("id") != style_id:
            continue
        for icon_style in list(style):
            if xml_local_name(icon_style.tag) != "IconStyle":
                continue
            for icon in list(icon_style):
                if xml_local_name(icon.tag) == "Icon":
                    return direct_child_text(icon, "href")
    return ""


def test_feeder_scope_detection_and_ug_folder_set():
    assert is_feeder_project("SF WARINGINJAYA RW 06 BEKASI")
    assert is_feeder_project("FBKS0150")
    assert is_feeder_project("BKS - FBKS0141")
    assert is_feeder_project("FBKS0136 KMZ")
    folders = feeder_standard_folders_for_network("UG")
    assert "OLT" in folders
    assert "CABLE" in folders
    assert "SUBDUCT 32/28" in folders
    assert "NEW HH 80X80X130" in folders

    subfeeder_folders = feeder_standard_folders_for_network("UG", "Subfeeder")
    assert "OLT" not in subfeeder_folders
    assert "CABLE" in subfeeder_folders

    cluster_ug_folders = line_standard_folders_for_network("UG")
    assert "NEW HH 20X20X20" in cluster_ug_folders


def test_feeder_source_olt_reference_uses_endpoint_before_segment():
    title = "FBKS0108 - BEKASI-MAIN_FEEDER_OLT_SEGARA_CITY_SEGMENT_3_TO_OLT_MEDAN_SATRIA_FO_144C"
    assert feeder_source_olt_reference([title]) == "OLT SEGARA CITY"


def test_short_cable_line_prefix_maps_to_observed_line_letter():
    assert explicit_line_order("LA 48C") == "A"
    assert explicit_line_order("LB 72C") == "B"


def test_all_scope_folder_orders_match_supplied_ae_ug_templates():
    assert feeder_standard_folders_for_network("UG", "Feeder") == [
        "OLT", "CABLE", "JOINT CLOSURE", "EXISTING HH 80X80X130",
        "NEW HH 80X80X130", "SUBDUCT 40/34", "SUBDUCT 32/28",
        "SLACK HANGER", "SEWER", "OPEN TRENCHING", "BORING MANUAL",
        "BORING MACHINE",
    ]
    assert feeder_standard_folders_for_network("UG", "Subfeeder") == [
        "OPEN TRENCHING", "BORING MACHINE", "BORING MANUAL", "SEWER",
        "JOINT CLOSURE", "CABLE", "EXISTING HH 80X80X130",
        "EXISTING HH 40X40X60", "NEW HH 80X80X130",
        "NEW HH 40X40X60", "SUBDUCT 40/34", "SUBDUCT 32/28",
        "SLACK HANGER",
    ]
    assert line_standard_folders_for_network("UG") == [
        "BOUNDARY FAT", "FAT", "HP COVER", "HP UNCOVER",
        "EXISTING HH 40X40X60", "EXISTING HH 20X20X20",
        "NEW HH 40X40X60", "NEW HH 20X20X20", "SUBDUCT 40/34",
        "SUBDUCT 32/28", "DISTRIBUTION CABLE", "SLACK HANGER",
        "SLING WIRE", "OPEN TRENCHING", "SEWER", "BORING MANUAL",
        "BORING MACHINE",
    ]
    assert feeder_standard_folders_for_network("AE", "Feeder") == [
        "OLT", "CABLE", "EXISTING POLE EMR 7-3", "EXISTING POLE EMR 7-4",
        "EXISTING POLE EMR 7-5", "EXISTING POLE EMR 9-5",
        "EXISTING POLE EMR 9-4", "EXISTING POLE PARTNER 7-4",
        "EXISTING POLE PARTNER 9-4", "NEW POLE 7-5", "NEW POLE 7-4",
        "NEW POLE 9-5", "NEW POLE 9-4", "JOINT CLOSURE", "SLACK HANGER",
        "HANDHOLE 80X80X130",
    ]
    assert feeder_standard_folders_for_network("AE", "Subfeeder") == [
        "JOINT CLOSURE", "EXISTING POLE EMR 7-2.5", "EXISTING POLE EMR 7-3",
        "EXISTING POLE EMR 7-4", "EXISTING POLE EMR 7-5",
        "EXISTING POLE EMR 9-5", "EXISTING POLE EMR 9-4",
        "EXISTING POLE PARTNER 7-4", "EXISTING POLE PARTNER 9-4",
        "NEW POLE 7-5", "NEW POLE 9-5", "NEW POLE 7-4", "NEW POLE 9-4",
        "CABLE", "SLACK HANGER",
    ]
    assert line_standard_folders_for_network("AE") == [
        "BOUNDARY FAT", "FAT", "HP COVER", "HP UNCOVER",
        "EXISTING POLE EMR 7-2.5", "EXISTING POLE EMR 7-3",
        "EXISTING POLE EMR 7-4", "EXISTING POLE EMR 9-4",
        "EXISTING POLE PARTNER 7-4", "EXISTING POLE PARTNER 9-4",
        "NEW POLE 7-2.5", "NEW POLE 7-3", "NEW POLE 7-4", "NEW POLE 9-4",
        "DISTRIBUTION CABLE", "SLACK HANGER", "SLING WIRE",
    ]


def test_feeder_ug_uses_olt_and_segment_for_hh_and_exact_folder_template(tmp_path, monkeypatch):
    project = "FBKS0101 - BEKASI - HUB FEEDER OLT LIPPO CIKARANG SEGMENT 1 TO COMMAND CENTER"
    source_kmz = tmp_path / f"{project}.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>{project}</name>
  <Folder><name>FDT</name></Folder>
  <Folder><name>OLT</name><Placemark><name>OLT LIPPO CIKARANG</name><Point><coordinates>106.8,-6.2,0</coordinates></Point></Placemark></Folder>
  <Folder><name>EXISTING HH 40X40X60</name><Placemark><name>EXT.MR.S01.P001</name><styleUrl>#keepExt</styleUrl><Point><coordinates>106.8001,-6.2,0</coordinates></Point></Placemark></Folder>
  <Folder><name>NEW HH 40X40X60</name><Placemark><name>MR.S01.P001</name><styleUrl>#keepNew</styleUrl><Point><coordinates>106.8002,-6.2,0</coordinates></Point></Placemark></Folder>
  <Folder><name>BORING</name><Placemark><name>Boring</name><LineString><coordinates>106.8,-6.2,0 106.8002,-6.2,0</coordinates></LineString></Placemark></Folder>
</Folder></Document></kml>'''
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)

    olt_row = {
        "olt_code": "BKS.100.2401",
        "olt_initial": "LCK",
        "olt_name": "OLT LIPPO CIKARANG",
        "olt_hostname": "LCK-OLT-01",
        "olt_name_initial": "LCK",
    }
    monkeypatch.setattr(
        kmz_standardizer,
        "lookup_olt_catalog_from_text",
        lambda text: olt_row if "LIPPO CIKARANG" in str(text).upper() else None,
    )

    standardize_kmz_file(
        source_kmz,
        None,
        output_kmz,
        network_type="UG",
        scope_type="Feeder",
        coding_records=[],
        source_name=f"{project}.kmz",
    )
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert child_folder_names_at_path(root, [project]) == feeder_standard_folders_for_network("UG", "Feeder")
    assert "FDT" not in child_folder_names_at_path(root, [project])
    assert "BORING" not in child_folder_names_at_path(root, [project])
    assert "MR.LCK.S01.HH001" in placemark_names(root)
    assert "EXT.MR.LCK.S01.HH001" in placemark_names(root)
    assert placemark_folder_path(root, "MR.LCK.S01.HH001")[-1] == "NEW HH 80X80X130"
    assert placemark_folder_path(root, "EXT.MR.LCK.S01.HH001")[-1] == "EXISTING HH 80X80X130"
    assert placemark_folder_path(root, "Boring")[-1] == "BORING MANUAL"
    assert placemark_style_url(root, "MR.LCK.S01.HH001") == "#keepNew"
    assert placemark_style_url(root, "EXT.MR.LCK.S01.HH001") == "#keepExt"
    assert handhole_source_code("MR.LCK.S01.HH001") == "LCK.S01"
    assert handhole_source_code("EXT.MR.LCK.S01.HH001") == "LCK.S01"


def test_feeder_uses_source_olt_and_title_segment_for_poles_and_cable(tmp_path, monkeypatch):
    project = "FBKS0108 - BEKASI-MAIN_FEEDER_OLT_SEGARA_CITY_SEGMENT_3_TO_OLT_MEDAN_SATRIA_FO_144C"
    source_kmz = tmp_path / f"{project}.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>{project}</name>
  <Folder><name>OLT</name><Placemark><name>OLT MEDAN SATRIA</name><Point><coordinates>106.98,-6.18,0</coordinates></Point></Placemark></Folder>
  <Folder><name>CABLE</name><Placemark><name>FO 144C/12T</name><LineString><coordinates>106.98,-6.18,0 106.981,-6.18,0</coordinates></LineString></Placemark></Folder>
  <Folder><name>EXISTING POLE EMR 7-4</name><Placemark><name>EXT.MR.MDS.S03.P001</name><Point><coordinates>106.9805,-6.18,0</coordinates></Point></Placemark></Folder>
</Folder></Document></kml>'''
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)

    segara = {
        "olt_code": "BKS.100.2601", "olt_initial": "SGC",
        "olt_name": "OLT SEGARA CITY", "olt_hostname": "SGC-OLT",
        "olt_name_initial": "SGC",
    }
    medan = {
        "olt_code": "BKS.100.0801", "olt_initial": "MDS",
        "olt_name": "OLT MEDAN SATRIA", "olt_hostname": "MDS-OLT",
        "olt_name_initial": "MDS",
    }

    def fake_olt_lookup(text):
        upper = str(text).upper()
        if "SEGARA CITY" in upper:
            return segara
        if "MEDAN SATRIA" in upper:
            return medan
        return None

    monkeypatch.setattr(kmz_standardizer, "lookup_olt_catalog_from_text", fake_olt_lookup)
    coding_record = FdtCodingRecord(
        sheet_name="TEST", row_number=1, site_name=project, fdt_order=1,
        fdt_id="001", fdt_code="SGC.001", index_code="BKS.100.2601.SGC.001",
        cluster_initial="OLD",
    )

    standardize_kmz_file(
        source_kmz,
        None,
        output_kmz,
        network_type="AE",
        scope_type="Feeder",
        coding_records=[coding_record],
        source_name=source_kmz.name,
    )
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "EXT.MR.MF.SGC.S03.P001" in names
    cable_name = next(name for name in names if "MAINFEEDER CABLE" in name)
    assert cable_name.startswith("BKS.100.2601 - MAINFEEDER CABLE SEGMENT 3 ")
    assert "SEGMENT 1" not in cable_name
    assert "BKS.100.0801" not in cable_name


def test_cluster_cikarang_shape_preserves_fat_sling_and_observed_lines(tmp_path):
    project = "BKS003586 - CIKARANG UTAMA RESIDENCE"
    source_kmz = tmp_path / f"{project}.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>{project}</name>
  <Folder><name>FDT1 72 C</name>
    <Placemark><name>FDT 1 CKUR.043</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
    <Folder><name>CABLE</name><Placemark><name>LA 48C</name><LineString><coordinates>106.8000,-6.2000,0 106.8010,-6.2000,0</coordinates></LineString></Placemark></Folder>
    <Folder><name>COVERAGE AREA</name><Placemark><name>A01</name><Polygon><outerBoundaryIs><LinearRing><coordinates>106.8007,-6.2002,0 106.8012,-6.2002,0 106.8012,-6.1998,0 106.8007,-6.1998,0 106.8007,-6.2002,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Folder>
    <Folder><name>FAT</name><Placemark><name>MR.JYS.CKUR.043.A01</name><Point><coordinates>106.8010,-6.2000,0</coordinates></Point></Placemark></Folder>
    <Folder><name>SLINGWIRE</name><Placemark><name>Untitled Path</name><LineString><coordinates>106.8004,-6.2000,0 106.8006,-6.2000,0</coordinates></LineString></Placemark></Folder>
    <Folder><name>HOMEPASS</name><Folder><name>A01</name><Placemark><name>E16-3</name><Point><coordinates>106.8009,-6.2000,0</coordinates></Point></Placemark></Folder></Folder>
  </Folder>
</Folder></Document></kml>'''
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)

    record = FdtCodingRecord(
        sheet_name="TEST", row_number=1, site_name=project, fdt_order=1,
        fdt_id="043", fdt_code="CKUR.043", index_code="BKS.100.1701.CKUR.043",
        cluster_initial="CKUR", olt_initial="JYS", olt_name_initial="JYS",
    )
    standardize_kmz_file(
        source_kmz,
        None,
        output_kmz,
        network_type="AE",
        scope_type="Cluster",
        coding_records=[record],
        source_name=source_kmz.name,
    )
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "MR.JYS.CKUR.043.A01" in names
    assert not any(name.startswith("EXT.MR.JYS.P") for name in names)
    line_folder = placemark_folder_path(root, "MR.JYS.CKUR.043.A01")[-2]
    assert line_folder in {"LINE A", "LINE A - FDT 1"}
    assert placemark_folder_path(root, "MR.JYS.CKUR.043.A01")[-1] == "FAT"
    assert placemark_folder_path(root, "A01")[-2:] == [line_folder, "BOUNDARY FAT"]
    assert placemark_folder_path(root, "Untitled Path")[-2:] == [line_folder, "SLING WIRE"]
    assert placemark_folder_path(root, "E16-3")[-3:] == [line_folder, "HP COVER", "A01"]
    assert not folder_path_exists(root, [project, line_folder, "CABLE"])
    assert not folder_path_exists(root, [project, "LINE E - FDT 1"])
    cable_name = next(name for name in names if "CABLE LINE A" in name)
    assert placemark_folder_path(root, cable_name)[-2:] == [line_folder, "DISTRIBUTION CABLE"]



def test_subfeeder_standardize_does_not_create_olt_folder(tmp_path):
    source_kmz = tmp_path / "BKS002833 - SF GRAHA HARAPAN RW 17.kmz"
    output_kmz = tmp_path / "subfeeder-standardized.kmz"
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS002833 - SF GRAHA HARAPAN RW 17</name>
      <Folder>
        <name>CABLE</name>
        <Placemark>
          <name>subfeeder cable FO 24C</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8009,-6.2000,0</coordinates></LineString>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    result = standardize_kmz_file(source_kmz, None, output_kmz, network_type="AE", scope_type="Subfeeder")

    assert result["scope_type"] == "Subfeeder"
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    assert folder_path_exists(root, ["BKS002833 - SF GRAHA HARAPAN RW 17", "CABLE"])
    assert not folder_path_exists(root, ["BKS002833 - SF GRAHA HARAPAN RW 17", "OLT"])

def test_feeder_standardize_moves_distribution_cable_to_cable_folder(tmp_path):
    source_kmz = tmp_path / "BKS004877 - SF WARINGINJAYA RW 06 BEKASI.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS004877 - SF WARINGINJAYA RW 06 BEKASI</name>
      <Folder>
        <name>DISTRIBUTION CABLE</name>
        <Placemark>
          <name>raw feeder cable FO 24C/2T</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8009,-6.2000,0</coordinates></LineString>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    result = standardize_kmz_file(source_kmz, None, output_kmz, network_type="AE", scope_type="Feeder")

    assert result["scope_type"] == "Feeder"
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    cable_name = next(name for name in placemark_names(root) if "SUBFEEDER CABLE" in name)
    assert placemark_folder_path(root, cable_name)[-2:] == [
        "BKS004877 - SF WARINGINJAYA RW 06 BEKASI",
        "CABLE",
    ]
    assert not folder_path_exists(root, ["BKS004877 - SF WARINGINJAYA RW 06 BEKASI", "DISTRIBUTION CABLE"])
    assert any(change["folder"] == "CABLE" for change in result["distribution_cable_changes"])


def make_coding_workbook(path):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "OLT Taman Narogong"
    headers = [
        "No.",
        "Site Name",
        "No.HUB & No.SubHub/OLT",
        "Coding Number (Index - HUB & OLT)",
        "Inisial Name OLT",
        "Inisial Name Cluster",
        "No. FDT ID",
        "Coding FDT  (FDT  ID)",
        "Coding Number (Index - FDT )",
    ]
    for column, header in enumerate(headers, start=2):
        worksheet.cell(12, column, header)
    worksheet.cell(13, 3, "MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7")
    worksheet.cell(13, 6, "NRG")
    worksheet.cell(13, 7, "MGTDMR")
    worksheet.cell(13, 8, "149")
    worksheet.cell(13, 9, "MGTDMR.149")
    worksheet.cell(13, 10, "BKS.100.0501.MGTDMR.149")
    workbook.save(path)


def make_unmatched_coding_workbook(path):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "OLT Other"
    headers = [
        "No.",
        "Site Name",
        "No.HUB & No.SubHub/OLT",
        "Coding Number (Index - HUB & OLT)",
        "Inisial Name OLT",
        "Inisial Name Cluster",
        "No. FDT ID",
        "Coding FDT  (FDT  ID)",
        "Coding Number (Index - FDT )",
    ]
    for column, header in enumerate(headers, start=2):
        worksheet.cell(12, column, header)
    worksheet.cell(13, 3, "PROJECT YANG TIDAK ADA DI KMZ")
    worksheet.cell(13, 6, "OTH")
    worksheet.cell(13, 7, "WRONG")
    worksheet.cell(13, 8, "999")
    worksheet.cell(13, 9, "WRONG.999")
    worksheet.cell(13, 10, "BKS.100.9999.WRONG.999")
    workbook.save(path)


def make_two_fdt_coding_workbook(path):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "OLT Taman Narogong"
    headers = [
        "No.",
        "Site Name",
        "No.HUB & No.SubHub/OLT",
        "Coding Number (Index - HUB & OLT)",
        "Inisial Name OLT",
        "Inisial Name Cluster",
        "No. FDT ID",
        "Coding FDT  (FDT  ID)",
        "Coding Number (Index - FDT )",
    ]
    for column, header in enumerate(headers, start=2):
        worksheet.cell(12, column, header)
    for row_number, fdt_no in [(13, "149"), (14, "150")]:
        order = row_number - 12
        worksheet.cell(row_number, 3, f"MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7 FDT {order}")
        worksheet.cell(row_number, 6, "NRG")
        worksheet.cell(row_number, 7, "MGTDMR")
        worksheet.cell(row_number, 8, fdt_no)
        worksheet.cell(row_number, 9, f"MGTDMR.{fdt_no}")
        worksheet.cell(row_number, 10, f"BKS.100.0501.MGTDMR.{fdt_no}")
    workbook.save(path)


def make_nonstandard_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Style id="fdtBlue"><IconStyle><color>ffff0000</color></IconStyle></Style>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT</name>
          <styleUrl>#fdtBlue</styleUrl>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>T-WAY</name>
        <Placemark>
          <name>Reducer</name>
          <Point><coordinates>106.8001,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>CLUSTER CODE.KMZ</name>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>FAT</name>
          <Placemark>
            <name>FAT A01</name>
            <Point><coordinates>106.80015,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>NEW POLE 7-3</name>
          <Placemark>
            <name>MR.XXX.P001</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>EXISTING POLE EMR 7-4</name>
          <Placemark>
            <name>EXT.MR.P002</name>
            <Point><coordinates>106.8003,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>EXISTING POLE PARTNER 7-4</name>
          <Placemark>
            <name>EXT.TEL.P003</name>
            <Point><coordinates>106.8004,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TLKM.MGTDMR.P006</name>
            <Point><coordinates>106.800405,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TELKOM</name>
            <Point><coordinates>106.800406,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT LN</name>
            <Point><coordinates>106.800407,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>MTI</name>
            <Point><coordinates>106.800408,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>Linknet</name>
            <Point><coordinates>106.800409,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.MTI</name>
            <Point><coordinates>106.800410,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TELKOM.PLMGR.P013</name>
            <Point><coordinates>106.80041,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.LINKNET.PLMGR.P014</name>
            <Point><coordinates>106.80042,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.MORATEL.PLMGR.P015</name>
            <Point><coordinates>106.80043,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TELECOM.PLMGR.P016</name>
            <Point><coordinates>106.80044,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TLKOM.PLMGR.P017</name>
            <Point><coordinates>106.80045,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.TELEKOM.PLMGR.P018</name>
            <Point><coordinates>106.80046,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>XXX.XXX - CABLE LINE A (FO 24C/2T) - AE - 100 M</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8004,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
      </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_fdt_capacity_description_kmz(path, description, fdt_folder_name="FDT"):
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>{fdt_folder_name}</name>
        <Placemark>
          <name>FDT 1</name>
          <description>{description}</description>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>FAT</name>
          <Placemark>
            <name>FAT A01</name>
            <Point><coordinates>106.8001,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_messy_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>Survey Dump</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>MR.XXX.P010</name>
          <Point><coordinates>106.80018,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>EXT.TELKOM</name>
          <Point><coordinates>106.80022,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>NN-01</name>
          <Point><coordinates>106.80025,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>Cable Dump</name>
        <Placemark>
          <name>FO 24C/2T</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8005,-6.2000,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>Polygon Dump</name>
        <Placemark>
          <name>FAT A01</name>
          <Polygon>
            <outerBoundaryIs>
              <LinearRing>
                <coordinates>106.7998,-6.2002,0 106.8006,-6.2002,0 106.8006,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
              </LinearRing>
            </outerBoundaryIs>
          </Polygon>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)


def make_hp_cover_outside_fat_area_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>FAT A01</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7998,-6.2002,0 106.8002,-6.2002,0 106.8002,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
          <Placemark>
            <name>FAT B01</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.8010,-6.2002,0 106.8014,-6.2002,0 106.8014,-6.1998,0 106.8010,-6.1998,0 106.8010,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
      </Folder>
      <Folder>
        <name>Survey Dump</name>
        <Placemark>
          <name>NN-OUT</name>
          <Point><coordinates>106.8016,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)


def make_hp_folder_context_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>FDT 2</name>
          <Point><coordinates>106.8050,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE B - FDT 2</name>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>B05</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.8048,-6.2002,0 106.8052,-6.2002,0 106.8052,-6.1998,0 106.8048,-6.1998,0 106.8048,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
          <Placemark>
            <name>B02 New</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.8038,-6.2002,0 106.8042,-6.2002,0 106.8042,-6.1998,0 106.8038,-6.1998,0 106.8038,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
      </Folder>
      <Folder>
        <name>HP</name>
        <Placemark>
          <name>SMK</name>
          <description>RAWA BUGEL,B02,7</description>
          <Point><coordinates>106.8040,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>PAUD</name>
          <description>SILI,A12,4</description>
          <Point><coordinates>106.8050,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_unnamed_distribution_cable_to_fat_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>FDT 2</name>
          <Point><coordinates>106.8100,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>Polygon Dump</name>
        <Placemark>
          <name>FAT B16</name>
          <Polygon>
            <outerBoundaryIs>
              <LinearRing>
                <coordinates>106.8118,-6.2002,0 106.8122,-6.2002,0 106.8122,-6.1998,0 106.8118,-6.1998,0 106.8118,-6.2002,0</coordinates>
              </LinearRing>
            </outerBoundaryIs>
          </Polygon>
        </Placemark>
      </Folder>
      <Folder>
        <name>Cable Dump</name>
        <Placemark>
          <name>Untitled</name>
          <description>FO 24C/2T</description>
          <LineString><coordinates>106.8100,-6.2000,0 106.8120,-6.2000,0</coordinates></LineString>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)


def make_short_ae_sling_wire_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>Cable Dump</name>
        <Placemark>
          <name>XXX.XXX - CABLE LINE A (FO 24C/2T) - AE - 200 M</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8020,-6.2000,0</coordinates></LineString>
        </Placemark>
        <Placemark>
          <name>short aerial span</name>
          <LineString><coordinates>106.8010,-6.2000,0 106.8012,-6.2000,0</coordinates></LineString>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)


def make_fdt_grouped_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>FDT 1</name>
      <Placemark>
        <name>BKS.100.0501.MGTDMR.149</name>
        <styleUrl>#msn_cross-hairs</styleUrl>
        <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
      </Placemark>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>A01</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7998,-6.2002,0 106.8006,-6.2002,0 106.8006,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
        <Folder>
          <name>FAT</name>
          <Placemark>
            <name>MGTDMR.149.A01</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>HP COVER</name>
          <Folder>
            <name>B01</name>
            <Placemark>
              <name>NN-01</name>
              <Point><coordinates>106.80025,-6.2000,0</coordinates></Point>
            </Placemark>
          </Folder>
        </Folder>
        <Folder>
          <name>NEW POLE 7-3</name>
          <Placemark>
            <name>MR.XXX.P010</name>
            <Point><coordinates>106.8003,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>SLING WIRE</name>
          <Placemark>
            <name>line</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8005,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>XXX.XXX - CABLE LINE A (FO 24C/2T) - AE - 100 M</name>
            <LineString><coordinates>106.8000,-6.2000,0 106.8005,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_standard_line_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder><name>BOUNDARY FAT</name></Folder>
        <Folder><name>FAT</name></Folder>
        <Folder><name>HP COVER</name></Folder>
        <Folder><name>HP UNCOVER</name></Folder>
        <Folder><name>NEW POLE 7-3</name></Folder>
        <Folder>
          <name>SLACK HANGER</name>
          <Placemark>
            <name>MGTDMR.149.A01</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder><name>SLING WIRE</name></Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_duplicate_project_root_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7 (OLD)</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7 (OLD)</name>
      <Folder>
        <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7 (OLD)</name>
        <Folder>
          <name>LINE A</name>
          <Folder>
            <name>DISTRIBUTION CABLE</name>
            <Placemark>
              <name>old cable FO 24C/2T</name>
              <LineString><coordinates>106.8000,-6.2000,0 106.8010,-6.2000,0</coordinates></LineString>
            </Placemark>
          </Folder>
          <Folder><name>NEW POLE 7-3</name></Folder>
        </Folder>
      </Folder>
    </Folder>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7 (OLD)</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_partner_dot_pole_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>EXISTING POLE PARTNER 7-4</name>
          <Placemark>
            <name>EXT.LN.MGTDMR.P001</name>
            <Point><coordinates>106.8001,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>EXT.MTI.MGTDMR.P003</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>LN.P999</name>
            <Point><coordinates>106.8003,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>MTI.P002</name>
            <Point><coordinates>106.8004,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>TELKOM.P120</name>
            <Point><coordinates>106.8005,-6.2000,0</coordinates></Point>
          </Placemark>
          <Placemark>
            <name>TLKM.P777</name>
            <Point><coordinates>106.8006,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_fm_partner_dot_pole_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>EXISTING POLE PARTNER 7-4</name>
          <Placemark>
            <name>EXT.LN.MGTDMR.P001</name>
            <Point><coordinates>106.8001,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
        <Folder>
          <name>Survey Dump</name>
          <Placemark>
            <name>FM.P777</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_raw_pole_folder_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A - FDT 1</name>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>FO 24C/2T</name>
            <LineString><coordinates>106.7999,-6.2000,0 106.8006,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>FAT A01</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7998,-6.2002,0 106.8007,-6.2002,0 106.8007,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
      </Folder>
      <Folder>
        <name>HP COVER</name>
        <Folder>
          <name>FAT A01</name>
          <Placemark>
            <name>P002.HC</name>
            <Point><coordinates>106.80015,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
      <Folder>
        <name>POLE</name>
        <Placemark>
          <name>MR.HRJ26.014.P001.HC</name>
          <Point><coordinates>106.8001,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>FM.11</name>
          <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>MR.HRJ26.014.P033</name>
          <Point><coordinates>106.8003,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>Existing ARJ04</name>
          <Point><coordinates>106.8004,-6.2000,0</coordinates></Point>
        </Placemark>
        <Placemark>
          <name>NN-POLE</name>
          <Point><coordinates>106.8005,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def make_ug_twey_with_fat_area_kmz(path):
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
    <Folder>
      <name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name>
      <Folder>
        <name>FDT</name>
        <Placemark>
          <name>FDT 1</name>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>FO 24C/2T</name>
            <LineString><coordinates>106.7999,-6.2000,0 106.8006,-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>FAT A01</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7998,-6.2002,0 106.8006,-6.2002,0 106.8006,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
        <Folder>
          <name>T-WAY</name>
          <Placemark>
            <name>Reducer [1:Point]</name>
            <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)

def test_parse_fdt_coding_workbook_reads_template_layout(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    make_coding_workbook(workbook_path)

    records = parse_fdt_coding_workbook(workbook_path)

    assert len(records) == 1
    assert records[0].site_name == "MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert records[0].fdt_id == "149"
    assert records[0].fdt_code == "MGTDMR.149"
    assert records[0].index_code == "BKS.100.0501.MGTDMR.149"


def test_select_project_records_ignores_too_short_site_name_substring_match():
    records = [
        FdtCodingRecord(
            sheet_name="OLT North Bekasi",
            row_number=122,
            site_name="c",
            fdt_order=None,
            fdt_id="104",
            fdt_code="GHP08.104",
            index_code="BKS.100.0301.GHP08.104",
            cluster_initial="GHP08",
        ),
        FdtCodingRecord(
            sheet_name="OLT North Bekasi",
            row_number=310,
            site_name="Perumahan The Palm Green Resindence - FDT 1",
            fdt_order=1,
            fdt_id="282",
            fdt_code="PLMGR.282",
            index_code="BKS.100.0301.PLMGR.282",
            cluster_initial="PLMGR",
        ),
    ]

    matched, matched_from, score = select_project_records(
        records,
        ["BKS004565 PERUMAHAN THE PALM GREEN RESIDENCE BEKASI"],
    )

    assert matched_from == "BKS004565 PERUMAHAN THE PALM GREEN RESIDENCE BEKASI"
    assert score > 0.72
    assert [record.fdt_code for record in matched] == ["PLMGR.282"]
    assert matched[0].row_number == 310


def test_spreadsheet_download_url_preserves_gid_only_when_present():
    assert spreadsheet_download_url(
        "https://docs.google.com/spreadsheets/d/sheet-id/edit?gid=282266964#gid=282266964"
    ) == "https://docs.google.com/spreadsheets/d/sheet-id/export?format=xlsx&gid=282266964"

    assert spreadsheet_download_url(
        "https://docs.google.com/spreadsheets/d/sheet-id/edit?usp=sharing&rtpof=true&sd=true"
    ) == "https://docs.google.com/spreadsheets/d/sheet-id/export?format=xlsx"


def test_standardize_kmz_renames_fdt_from_spreadsheet_and_outputs_kmz(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_nonstandard_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert output_kmz.exists()
    assert result["matched_site"] == "MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert result["matched_sheet"] == "OLT Taman Narogong"
    assert result["fdt_renamed"] == 1
    assert result["folder_renamed"] == 1
    assert result["folder_unwrapped"] == 1
    assert result["name_standardized"] == 15
    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = [
        element.text
        for element in root.iter()
        if element.tag.endswith("name") and element.text
    ]
    assert "MGTDMR.149" in names
    assert "TWEY" in names
    assert "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7" in names
    assert "CLUSTER CODE.KMZ" not in names
    # NEW pole counter starts at P001 independently of EXT counter.
    assert "MR.MGTDMR.P001" in names
    # EXT pole counter is independent and also starts at P001.
    # The exact P-number per partner depends on input order and seeding,
    # so we just check the names exist (alias normalization etc.) and
    # that the NEW vs EXT counter separation rule holds.
    assert "EXT.TLKM.MGTDMR.P001" in names
    assert "EXT.MR.MGTDMR.P002" in names
    assert "EXT.TLKM.MGTDMR.P003" in names
    assert "EXT.LN.MGTDMR.P004" in names
    assert "EXT.MTI.MGTDMR.P005" in names
    assert "EXT.TLKM.MGTDMR.P006" in names
    assert "EXT.LN.MGTDMR.P007" in names
    assert "EXT.MTI.MGTDMR.P008" in names
    assert "EXT.TLKM.PLMGR.P013" in names
    assert "EXT.LN.PLMGR.P014" in names
    assert "EXT.MTI.PLMGR.P015" in names
    assert "EXT.TLKM.PLMGR.P016" in names
    assert "EXT.TLKM.PLMGR.P017" in names
    assert "EXT.TLKM.PLMGR.P018" in names
    assert "EXT.TELKOM.PLMGR.P013" not in names
    assert "EXT.LINKNET.PLMGR.P014" not in names
    assert "EXT.MORATEL.PLMGR.P015" not in names
    assert "EXT.TELKOM" not in names
    assert "EXT LN" not in names
    assert "MTI" not in names
    assert "Linknet" not in names
    assert "EXT.MTI" not in names
    cable_name = next(name for name in names if name.startswith("MGTDMR.149 - CABLE LINE A (FO 24C/2T) - AE - "))
    assert cable_name.endswith("m")
    assert result["distribution_cable_standardized"] == 1
    assert result["style_standardized"] >= 6
    assert result["fat_counts_by_fdt"] == {1: 1}
    assert result["fat_counts_by_line_fdt"] == {"A-1": 1}
    assert placemark_style_url(root, "MGTDMR.149") == "#eva_fdt_48c"
    assert placemark_style_url(root, "FAT A01") == "#eva_fat_16c"
    assert placemark_style_url(root, "MR.MGTDMR.P001") == "#eva_pole_7m_3inch"
    assert placemark_style_url(root, "EXT.MR.MGTDMR.P002") == "#eva_existing_pole"
    assert placemark_style_url(root, cable_name) == "#eva_distribution_cable_24c_2t"
    cable_description = placemark_description(root, cable_name)
    assert "Total Route :" in cable_description
    assert "Total Slack : 2 unit (1 slack FDT & 1 slack FAT) @20 m" in cable_description
    assert "Toleransi : 5%" in cable_description
    assert "Total Cable Lenght :" in cable_description
    assert "Total Length Cable :" not in cable_description
    assert style_color(root, "eva_fdt_48c", "IconStyle") == "ffff00aa"
    assert style_color(root, "eva_fat_16c", "IconStyle") == "ff00ffff"
    assert style_scale(root, "eva_hp_cover") == "0.8"
    assert style_color(root, "eva_distribution_cable_24c_2t", "LineStyle") == "ff00ff00"
    assert style_color(root, "eva_distribution_cable_24c_2t", "IconStyle") == ""
    assert style_list_icon(root, "eva_distribution_cable_24c_2t") == ""

    boq_result = parse_kmz_structure(output_kmz)
    expected_total_cable_length = math.ceil(
        (boq_result.cell_values["C2"] + (boq_result.cell_values["C45"] + 1) * 20) * 1.05
    )
    assert f"Total Cable Lenght : {expected_total_cable_length}m" in cable_description
    assert cable_name.endswith(f"{expected_total_cable_length}m")
    assert result["distribution_boq_totals"]["A-1-24C/2T"]["quantity"] == expected_total_cable_length
    assert result["distribution_boq_totals"]["A-1-24C/2T"]["cell"] == "C24"
    assert boq_result.cell_values["C38"] == 1
    assert boq_result.cell_values["C45"] == 1
    assert boq_result.summary["total_fdt_detected"] == 1
    assert boq_result.summary["total_fat_detected"] == 1


@pytest.mark.parametrize(
    ("description", "expected_style", "expected_capacity"),
    [
        ("Existing compact FDT: 24C", "#eva_fdt_48c", 24),
        ("Panel existing: FDT 48", "#eva_fdt_48c", 48),
        ("Keterangan icon FDT: FDT 72C", "#eva_fdt_72c", 72),
        ("Kapasitas FDT - 144", "#eva_fdt_144c", 144),
    ],
)
def test_standardize_kmz_uses_fdt_capacity_from_description_before_fat_count_rule(
    tmp_path,
    description,
    expected_style,
    expected_capacity,
):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "as_plan_capacity.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_fdt_capacity_description_kmz(source_kmz, description)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_style_url(root, "MGTDMR.149") == expected_style
    fdt_style_change = next(change for change in result["style_changes"] if change["folder"] == "FDT")
    assert fdt_style_change["capacity"] == expected_capacity
    assert fdt_style_change["capacity_source"] == "description"
    assert fdt_style_change["fat_count"] == 1


def test_standardize_kmz_preserves_fdt_capacity_from_source_folder(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "as_plan_folder_capacity.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_fdt_capacity_description_kmz(source_kmz, "", fdt_folder_name="FDT1 72 C")

    standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert "FDT Capacity: 72 CORE" in placemark_description(root, "MGTDMR.149")
    assert placemark_style_url(root, "MGTDMR.149") == "#eva_fdt_72c"

def test_standardize_kmz_uses_default_xxx_when_spreadsheet_project_not_found(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "BKS009999 - PROJECT BELUM ADA DI SPREADSHEET.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_unmatched_coding_workbook(workbook_path)
    make_nonstandard_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["default_fdt_coding"] is True
    assert result["matched_sheet"] == "DEFAULT"
    assert result["matched_row"] == 0
    assert result["available_fdt_codes"][0]["fdt_code"] == "XXX"
    assert result["available_fdt_codes"][0]["cluster_initial"] == "XXX"
    assert any("default XXX" in warning for warning in result["warnings"])

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = placemark_names(root)
    assert "XXX" in names
    assert "WRONG.999" not in names
    assert "EXT.MR.XXX.P002" in names
    assert any(name.startswith("XXX - CABLE LINE A") for name in names)

def test_standardize_kmz_uses_default_xxx_when_spreadsheet_missing(tmp_path):
    source_kmz = tmp_path / "BKS009999 - PROJECT BELUM ADA DI SPREADSHEET.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_nonstandard_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, None, output_kmz)

    assert result["default_fdt_coding"] is True
    assert result["matched_sheet"] == "DEFAULT"
    assert any("Spreadsheet coding FDT belum tersedia" in warning for warning in result["warnings"])
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    assert "XXX" in placemark_names(root)

def test_standardize_kmz_organizes_messy_placemarks_into_standard_folders(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "messy_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_messy_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["fdt_renamed"] == 1
    assert result["name_standardized"] == 3
    assert result["placemark_organized"] == 6
    assert result["distribution_cable_standardized"] == 1
    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    cable_name = next(name for name in names if name.startswith("MGTDMR.149 - CABLE LINE A (FO 24C/2T) - AE - "))
    assert placemark_folder_path(root, "MGTDMR.149")[-2:] == [
        "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
        "FDT",
    ]
    assert placemark_folder_path(root, cable_name)[-2:] == ["LINE A", "DISTRIBUTION CABLE"]
    assert placemark_folder_path(root, "MR.MGTDMR.P001")[-2:] == ["LINE A", "NEW POLE 7-4"]
    assert placemark_folder_path(root, "EXT.TLKM.MGTDMR.P001")[-2:] == ["LINE A", "EXISTING POLE PARTNER 7-4"]
    assert placemark_folder_path(root, "FAT A01")[-2:] == ["LINE A", "BOUNDARY FAT"]
    assert placemark_folder_path(root, "NN-01")[-3:] == ["LINE A", "HP COVER", "A01"]
    assert placemark_style_url(root, "NN-01") == "#eva_hp_cover"
    assert folder_path_exists(
        root,
        [
            "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
            "LINE A",
            "NEW POLE 7-4",
        ],
    )
    assert folder_path_exists(
        root,
        [
            "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
            "LINE A",
            "HP UNCOVER",
        ],
    )

def test_standardize_kmz_uses_google_earth_default_boundary_fat_polygon_icon(tmp_path):
    source_kmz = tmp_path / "cad_boundary_fat.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Style id="eva_boundary_fat">
      <ListStyle>
        <ItemIcon>
          <state>open closed</state>
          <href>http://maps.google.com/mapfiles/kml/shapes/polygon.png</href>
        </ItemIcon>
      </ListStyle>
    </Style>
    <Folder>
      <name>CAD SAMPLE</name>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>BOUNDARY FAT</name>
          <Placemark>
            <name>A01</name>
            <styleUrl>#eva_boundary_fat</styleUrl>
            <Style>
              <IconStyle><scale>0</scale></IconStyle>
            </Style>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7998,-6.2002,0 106.8002,-6.2002,0 106.8002,-6.1998,0 106.7998,-6.1998,0 106.7998,-6.2002,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
        <Folder>
          <name>BOUNDARY</name>
          <Placemark>
            <name>CLUSTER BOUNDARY</name>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>106.7997,-6.2003,0 106.8003,-6.2003,0 106.8003,-6.1997,0 106.7997,-6.1997,0 106.7997,-6.2003,0</coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    result = standardize_kmz_file(source_kmz, None, output_kmz)

    assert result["style_standardized"] >= 1
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "A01")[-2:] == ["LINE A - FDT 1", "BOUNDARY FAT"]
    assert placemark_style_url(root, "A01") == "#eva_boundary_polygon"
    assert placemark_folder_path(root, "CLUSTER BOUNDARY")[-2:] == ["LINE A - FDT 1", "BOUNDARY"]
    assert placemark_style_url(root, "CLUSTER BOUNDARY") == "#eva_boundary_polygon"
    assert style_color(root, "eva_boundary_polygon", "LineStyle") == "ffffffff"
    assert style_color(root, "eva_boundary_polygon", "PolyStyle") == "4dffffff"
    assert style_list_icon(root, "eva_boundary_fat") == ""
    assert not any(
        xml_local_name(style.tag) == "Style" and style.attrib.get("id") == "eva_boundary_fat"
        for style in root.iter()
    )
    placemark = next(
        item
        for item in root.iter()
        if xml_local_name(item.tag) == "Placemark" and direct_child_text(item, "name") == "A01"
    )
    assert all(xml_local_name(child.tag) != "Style" for child in list(placemark))

def test_standardize_kmz_assigns_hp_cover_outside_polygon_to_nearest_fat_area(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "hp_cover_outside_fat_area.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_hp_cover_outside_fat_area_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "NN-OUT")[-3:] == ["LINE A", "HP COVER", "B01"]
    assert placemark_folder_path(root, "NN-OUT")[-1] != "UNCATEGORIZED"

def test_standardize_kmz_treats_hp_folder_as_hp_cover_by_fat_area(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "hp_folder_context.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_two_fdt_coding_workbook(workbook_path)
    make_hp_folder_context_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "PAUD")[-3:] == ["LINE B - FDT 2", "HP COVER", "B05"]
    assert placemark_folder_path(root, "SMK")[-3:] == ["LINE B - FDT 2", "HP COVER", "B02"]
    assert child_folder_names_at_path(
        root,
        [
            "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
            "LINE B - FDT 2",
            "HP COVER",
        ],
    ) == ["B02", "B05"]

def test_standardize_kmz_infers_unnamed_distribution_cable_line_from_start_fdt_and_end_fat(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "unnamed_distribution_cable.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_two_fdt_coding_workbook(workbook_path)
    make_unnamed_distribution_cable_to_fat_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    cable_name = next(name for name in names if name.startswith("MGTDMR.150 - CABLE LINE B (FO 24C/2T) - AE - "))
    assert "Untitled" not in names
    assert placemark_folder_path(root, cable_name)[-2:] == ["LINE B - FDT 2", "DISTRIBUTION CABLE"]
    assert placemark_style_url(root, cable_name) == "#eva_distribution_cable_24c_2t"
    assert result["distribution_cable_standardized"] == 1

def test_standardize_kmz_moves_short_ae_line_to_nearest_distribution_sling_wire(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "short_ae_sling_wire.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_short_ae_sling_wire_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE")

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "short aerial span")[-1] == "SLING WIRE"
    assert placemark_folder_path(root, "short aerial span")[-2].startswith("LINE A")
    assert placemark_style_url(root, "short aerial span") == "#eva_sling_wire"

def test_standardize_kmz_does_not_auto_sling_short_lines_for_ug(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "short_ug_line.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_short_ae_sling_wire_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="UG")

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "short aerial span")[-1] == "Cable Dump"
    assert placemark_folder_path(root, "short aerial span")[-2:] != ["LINE A - FDT 1", "SLING WIRE"]

def test_standardize_kmz_keeps_fdt_group_children_in_line_folders(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "fdt_grouped_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_fdt_grouped_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["fdt_renamed"] == 1
    assert result["fdt_placemarks"] == 1
    assert result["distribution_cable_standardized"] == 1
    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    cable_name = next(name for name in names if name.startswith("MGTDMR.149 - CABLE LINE A (FO 24C/2T) - AE - "))
    assert placemark_folder_path(root, "MGTDMR.149") == [
        "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
        "FDT",
    ]
    assert placemark_folder_path(root, "MGTDMR.149.A01")[-2:] == ["LINE A - FDT 1", "FAT"]
    assert placemark_folder_path(root, "MR.MGTDMR.P010")[-2:] == ["LINE A - FDT 1", "NEW POLE 7-3"]
    assert placemark_folder_path(root, "NN-01")[-3:] == ["LINE A - FDT 1", "HP COVER", "B01"]
    assert placemark_folder_path(root, cable_name)[-2:] == ["LINE A - FDT 1", "DISTRIBUTION CABLE"]
    assert placemark_folder_path(root, "line")[-2:] == ["LINE A - FDT 1", "SLING WIRE"]
    assert folder_path_exists(
        root,
        [
            "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
            "LINE A - FDT 1",
            "EXISTING POLE PARTNER 9-4",
        ],
    )

def test_standardize_kmz_preserves_existing_standard_folder_assignment_and_empty_folders(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "standard_line_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_standard_line_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["fdt_renamed"] == 1
    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert "MGTDMR.149.A01" not in placemark_names(root)
    assert placemark_folder_path(root, "SLACK HANGER")[-2:] == ["LINE A", "SLACK HANGER"]
    assert placemark_folder_path(root, "SLACK HANGER")[-2:] != ["LINE A", "FAT"]
    assert placemark_style_url(root, "SLACK HANGER") == "#eva_slack_hanger"
    assert folder_path_exists(root, [project, "LINE A", "HP UNCOVER"])
    assert not folder_path_exists(root, [project, "LINE A", "BOUNDARY"])
    assert folder_path_exists(root, [project, "LINE A", "EXISTING POLE EMR 7-4"])
    assert folder_path_exists(root, [project, "LINE A", "SLING WIRE"])
    assert not folder_path_exists(root, [project, "LINE A", "SUBDUCT 32/28"])
    assert not folder_path_exists(root, [project, "LINE A", "NEW HH 80X80X130"])


def test_standardize_kmz_moves_top_level_boundary_cluster_inside_project(tmp_path):
    source_kmz = tmp_path / "BKS003250 - MEGA REGENCY BLOK P.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BKS003250 - MEGA REGENCY BLOK P</name>
    <Folder>
      <name>BOUNDARY CLUSTER</name>
      <Placemark>
        <name>BOUNDARY CLUSTER</name>
        <Polygon>
          <outerBoundaryIs><LinearRing><coordinates>107.0,-6.0,0 107.1,-6.0,0 107.1,-6.1,0 107.0,-6.1,0 107.0,-6.0,0</coordinates></LinearRing></outerBoundaryIs>
        </Polygon>
      </Placemark>
    </Folder>
    <Folder>
      <name>BKS003250 - MEGA REGENCY BLOK P</name>
      <Folder>
        <name>FDT</name>
        <Placemark><name>FDT 1</name><Point><coordinates>107.01,-6.01,0</coordinates></Point></Placemark>
      </Folder>
      <Folder><name>LINE A - FDT 1</name></Folder>
      <Folder><name>LINE A - FDT 2</name></Folder>
      <Folder><name>LINE B - FDT 2</name></Folder>
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)

    standardize_kmz_file(source_kmz, None, output_kmz, network_type="AE", scope_type="Cluster")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    project = "BKS003250 - MEGA REGENCY BLOK P"
    assert document_top_level_folder_names(root) == [project]
    assert folder_path_exists(root, [project, "BOUNDARY CLUSTER"])
    assert folder_path_exists(root, [project, "FDT"])
    assert folder_path_exists(root, [project, "LINE A - FDT 1"])
    assert placemark_folder_path(root, "BOUNDARY CLUSTER") == [project, "BOUNDARY CLUSTER"]

def test_standardize_kmz_consolidates_duplicate_project_roots(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_duplicate_project_root_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert document_top_level_folder_names(root) == [project]
    assert not folder_path_exists(root, [project, project])
    cable_name = next(name for name in placemark_names(root) if name.startswith("MGTDMR.149 - CABLE LINE A"))
    assert placemark_folder_path(root, "MGTDMR.149") == [project, "FDT"]
    assert placemark_folder_path(root, cable_name)[-2:] == ["LINE A - FDT 1", "DISTRIBUTION CABLE"]
    assert folder_path_exists(root, [project, "LINE A - FDT 1", "NEW POLE 7-3"])

def test_standardize_kmz_updates_partner_dot_pole_labels_to_next_available_existing_poles(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "partner_dot_pole_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_partner_dot_pole_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    assert result["name_standardized"] == 4
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "EXT.LN.MGTDMR.P001" in names
    assert "EXT.LN.MGTDMR.P002" in names
    assert "EXT.MTI.MGTDMR.P003" in names
    assert "EXT.MTI.MGTDMR.P004" in names
    assert "EXT.TLKM.MGTDMR.P005" in names
    assert "EXT.TLKM.MGTDMR.P006" in names
    assert "LN.P999" not in names
    assert "MTI.P002" not in names
    assert "TELKOM.P120" not in names
    assert "TLKM.P777" not in names
    assert placemark_folder_path(root, "EXT.LN.MGTDMR.P002")[-2:] == ["LINE A", "EXISTING POLE PARTNER 7-4"]

def test_standardize_kmz_updates_fm_dot_pole_labels_to_ln_existing_partner(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "fm_dot_pole_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_fm_partner_dot_pole_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "EXT.LN.MGTDMR.P002" in names
    assert "FM.P777" not in names
    assert placemark_folder_path(root, "EXT.LN.MGTDMR.P001")[-2:] == ["LINE A", "EXISTING POLE PARTNER 7-4"]
    assert placemark_folder_path(root, "EXT.LN.MGTDMR.P002")[-2:] == ["LINE A", "EXISTING POLE PARTNER 7-4"]

def test_standardize_kmz_treats_raw_pole_folder_items_as_poles_not_hp_cover(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "raw_pole_folder_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_raw_pole_folder_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz)

    assert result["warnings"] == []
    assert result["name_standardized"] >= 6
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "FM.11" not in names
    assert "Existing ARJ04" not in names
    assert "EXT.LN.MGTDMR.P001" in names
    assert "EXT.MR.MGTDMR.P002" in names
    assert placemark_folder_path(root, "MR.MGTDMR.P001")[-2:] == ["LINE A", "NEW POLE 7-3"]
    assert placemark_folder_path(root, "MR.MGTDMR.P002")[-2:] == ["LINE A", "NEW POLE 7-3"]
    assert placemark_folder_path(root, "EXT.LN.MGTDMR.P001")[-2:] == ["LINE A", "EXISTING POLE PARTNER 7-4"]
    assert placemark_folder_path(root, "MR.MGTDMR.P003")[-2:] == ["LINE A", "NEW POLE 7-4"]
    assert placemark_folder_path(root, "EXT.MR.MGTDMR.P002")[-2:] == ["LINE A", "EXISTING POLE EMR 7-4"]
    assert placemark_folder_path(root, "MR.MGTDMR.P004")[-2:] == ["LINE A", "NEW POLE 7-3"]
    assert "MR.HRJ26.014.P001.HC" not in names
    assert "P002.HC" not in names
    assert "NN-POLE" not in names
    assert placemark_style_url(root, "MR.MGTDMR.P001") == "#eva_pole_7m_3inch"
    assert placemark_style_url(root, "MR.MGTDMR.P002") == "#eva_pole_7m_3inch"
    assert placemark_style_url(root, "MR.MGTDMR.P003") == "#eva_pole_7m_4inch"
    assert placemark_style_url(root, "MR.MGTDMR.P004") == "#eva_pole_7m_3inch"
    assert placemark_style_url(root, "EXT.MR.MGTDMR.P002") == "#eva_existing_pole"

def test_standardize_kmz_uses_ug_folder_set_without_empty_ae_folders(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "standard_line_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_standard_line_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="UG")

    assert result["network_type"] == "UG"
    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert "MGTDMR.149.A01" not in placemark_names(root)
    assert placemark_folder_path(root, "SLACK HANGER")[-2:] == ["LINE A", "SLACK HANGER"]
    assert folder_path_exists(root, [project, "LINE A", "SUBDUCT 32/28"])
    assert folder_path_exists(root, [project, "LINE A", "SUBDUCT 40/34"])
    assert folder_path_exists(root, [project, "LINE A", "TALI PANCING"])
    assert folder_path_exists(root, [project, "LINE A", "NEW HH 20X20X20"])
    assert folder_path_exists(root, [project, "LINE A", "NEW HH 40X40X60"])
    assert folder_path_exists(root, [project, "LINE A", "NEW HH 80X80X130"])
    assert folder_path_exists(root, [project, "LINE A", "TWEY"])
    assert not folder_path_exists(root, [project, "LINE A", "NEW POLE 7-3"])
    assert not folder_path_exists(root, [project, "LINE A", "SLING WIRE"])


def test_standardize_kmz_numbers_new_hh80_before_hh40_and_hh20_separately(tmp_path):
    source_kmz = tmp_path / "hh_sequence_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    coding_record = FdtCodingRecord(
        sheet_name="TEST",
        row_number=1,
        site_name="MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7",
        fdt_order=1,
        fdt_id="149",
        fdt_code="MGTDMR.149",
        index_code="BKS.100.0501.MGTDMR.149",
        cluster_initial="MGTDMR",
    )
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>{project}</name>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>NEW HH 40X40X60</name>
          <Placemark><name>EXT.MR.MGTDMR.P003</name><Point><coordinates>106.8003,-6.2000,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.MR.MGTDMR.P004</name><Point><coordinates>106.8004,-6.2000,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW HH 20X20X20</name>
          <Placemark><name>EXT.MR.MGTDMR.P005</name><Point><coordinates>106.8005,-6.2000,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.MR.MGTDMR.P006</name><Point><coordinates>106.8006,-6.2000,0</coordinates></Point></Placemark>
        </Folder>
        <Folder>
          <name>NEW HH 80X80X130</name>
          <Placemark><name>EXT.MR.MGTDMR.P001</name><Point><coordinates>106.8001,-6.2000,0</coordinates></Point></Placemark>
          <Placemark><name>EXT.MR.MGTDMR.P002</name><Point><coordinates>106.8002,-6.2000,0</coordinates></Point></Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    result = standardize_kmz_file(
        source_kmz,
        None,
        output_kmz,
        network_type="UG",
        coding_records=[coding_record],
    )

    assert result["warnings"] == []
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    names = placemark_names(root)
    assert "MR.MGTDMR.HH001" in names
    assert "MR.MGTDMR.HH002" in names
    assert "MR.MGTDMR.HH003" in names
    assert "MR.MGTDMR.HH004" in names
    assert "MR.MGTDMR.HHPIT001" in names
    assert "MR.MGTDMR.HHPIT002" in names
    assert placemark_folder_path(root, "MR.MGTDMR.HH001")[-1] == "NEW HH 80X80X130"
    assert placemark_folder_path(root, "MR.MGTDMR.HH002")[-1] == "NEW HH 80X80X130"
    assert placemark_folder_path(root, "MR.MGTDMR.HH003")[-1] == "NEW HH 40X40X60"
    assert placemark_folder_path(root, "MR.MGTDMR.HHPIT001")[-1] == "NEW HH 20X20X20"
    line_folders = child_folder_names_at_path(root, [project, "LINE A"])
    assert line_folders.index("NEW HH 80X80X130") < line_folders.index("NEW HH 40X40X60")
    assert line_folders.index("NEW HH 40X40X60") < line_folders.index("NEW HH 20X20X20")


def test_standardize_existing_hh_names_without_changing_folder_or_style():
    root = ET.fromstring(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>EXISTING HH 40X40X60</name>
      <Placemark><name>EXT.MR.PTPB09.P003</name><styleUrl>#keep40</styleUrl><Point><coordinates>106.8003,-6.2000,0</coordinates></Point></Placemark>
    </Folder>
    <Folder>
      <name>EXISTING HH 20X20X20</name>
      <Placemark><name>EXT.MR.PTPB09.P005</name><styleUrl>#keep20</styleUrl><Point><coordinates>106.8005,-6.2000,0</coordinates></Point></Placemark>
    </Folder>
    <Folder>
      <name>EXISTING HH 80X80X130</name>
      <Placemark><name>EXT.MR.PTPB09.P001</name><styleUrl>#keep80</styleUrl><Point><coordinates>106.8001,-6.2000,0</coordinates></Point></Placemark>
    </Folder>
  </Document>
</kml>
"""
    )
    record = FdtCodingRecord(
        sheet_name="TEST",
        row_number=1,
        site_name="HH TEST",
        fdt_order=1,
        fdt_id="001",
        fdt_code="PTPB09.001",
        index_code="BKS.000.0000.PTPB09.001",
        cluster_initial="PTPB09",
    )
    changes = []

    standardize_handhole_placemark_names(
        root,
        record,
        "http://www.opengis.net/kml/2.2",
        changes,
    )

    assert "EXT.MR.PTPB09.HH001" in placemark_names(root)
    assert "EXT.MR.PTPB09.HH002" in placemark_names(root)
    assert "EXT.MR.PTPB09.HHPIT001" in placemark_names(root)
    assert placemark_folder_path(root, "EXT.MR.PTPB09.HH001")[-1] == "EXISTING HH 80X80X130"
    assert placemark_folder_path(root, "EXT.MR.PTPB09.HH002")[-1] == "EXISTING HH 40X40X60"
    assert placemark_folder_path(root, "EXT.MR.PTPB09.HHPIT001")[-1] == "EXISTING HH 20X20X20"
    assert placemark_style_url(root, "EXT.MR.PTPB09.HH001") == "#keep80"
    assert placemark_style_url(root, "EXT.MR.PTPB09.HH002") == "#keep40"
    assert placemark_style_url(root, "EXT.MR.PTPB09.HHPIT001") == "#keep20"
    assert len(changes) == 3

def test_standardize_kmz_preserves_ug_tway_reducers_as_twey(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "ug_twey_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    make_ug_twey_with_fat_area_kmz(source_kmz)

    result = standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="UG")

    assert result["warnings"] == []

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    assert placemark_folder_path(root, "Reducer [1:Point]")[-2:] == ["LINE A", "TWEY"]
    assert placemark_folder_path(root, "Reducer [1:Point]")[-2:] != ["HP COVER", "FAT A01"]
    assert folder_path_exists(root, [project, "LINE A", "TWEY"])
    assert placemark_style_url(root, "Reducer [1:Point]") == "#eva_twey"


def test_standardize_kmz_styles_ug_handhole_20_as_green_square(tmp_path):
    source_kmz = tmp_path / "ug_hh20_as_plan.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>PROJECT</name>
      <Folder>
        <name>LINE A</name>
        <Folder>
          <name>NEW HH 20X20X20</name>
          <Placemark>
            <name>HH PIT 20</name>
            <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    standardize_kmz_file(source_kmz, None, output_kmz, network_type="UG")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    assert placemark_folder_path(root, "MR.XXX.HHPIT001")[-1] == "NEW HH 20X20X20"
    assert placemark_style_url(root, "MR.XXX.HHPIT001") == "#eva_handhole_20x20x20"
    assert style_color(root, "eva_handhole_20x20x20", "IconStyle") == "ff00ff00"


def test_feeder_olt_folder_uses_ranger_station_style(tmp_path):
    source_kmz = tmp_path / "olt.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr(
            "doc.kml",
            """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>FBKS PONDOK UNGU</name>
      <Folder>
        <name>OLT</name>
        <Placemark>
          <name>OLT PONDOK UNGU</name>
          <styleUrl>#oldStyle</styleUrl>
          <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        )

    result = standardize_kmz_file(source_kmz, None, output_kmz, network_type="AE", scope_type="Feeder")

    assert result["scope_type"] == "Feeder"
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    assert placemark_style_url(root, "OLT PONDOK UNGU") == "#eva_olt"
    assert style_color(root, "eva_olt", "IconStyle") == "ffffffff"
    assert style_icon_href(root, "eva_olt") == "http://maps.google.com/mapfiles/kml/shapes/ranger_station.png"

def make_bare_ext_partner_kmz(path: Path, project_name: str, folder_blocks: list[tuple[str, list[str]]]) -> None:
    folders = "".join(
        f"""
      <Folder>
        <name>{folder_name}</name>
        {''.join(f'<Placemark><name>{name}</name><Point><coordinates>{106.8000 + index * 0.0001},-6.2000,0</coordinates></Point></Placemark>' for index, name in enumerate(names))}
      </Folder>
"""
        for folder_name, names in folder_blocks
    )
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>{project_name}</name>
      {folders}
    </Folder>
  </Document>
</kml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", kml)


def ext_partner_pole_numbers(names: list[str]) -> list[str]:
    return sorted(name.rsplit(".", 1)[-1] for name in names if name.startswith("EXT."))


def bare_ext_partner_names(root) -> list[str]:
    return [name for name in placemark_names(root) if name.startswith("EXT.")]


def test_bare_ext_partners_move_to_correct_feeder_folders_regardless_of_starting_folder(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_partner_mixed.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(
        source_kmz,
        project,
        [
            ("EXISTING POLE EMR 7-4", ["EXT.MR", "EXT.LN"]),
            ("EXISTING POLE PARTNER 7-4", ["EXT.MTI", "EXT.TLKM"]),
            ("Some Random Folder", ["EXT.MR"]),
        ],
    )

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = bare_ext_partner_names(root)
    assert len(names) == 5
    assert ext_partner_pole_numbers(names) == ["P001", "P002", "P003", "P004", "P005"]
    emr_names = [name for name in names if name.startswith(("EXT.MR.", "EXT.MTI."))]
    partner_names = [name for name in names if name.startswith(("EXT.LN.", "EXT.TLKM."))]
    assert len(emr_names) == 3
    assert len(partner_names) == 2
    assert all("NRG.MGTDMR" in name for name in names)
    assert all(placemark_folder_path(root, name)[-1] == "EXISTING POLE EMR 7-4" for name in emr_names)
    assert all(placemark_folder_path(root, name)[-1] == "EXISTING POLE PARTNER 7-4" for name in partner_names)


def test_bare_ext_mr_feeder_happy_path_renames_in_emr_folder(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_mr.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("EXISTING POLE EMR 7-4", ["EXT.MR", "EXT.MR"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    assert "EXT.MR.NRG.MGTDMR.P001" in placemark_names(root)
    assert "EXT.MR.NRG.MGTDMR.P002" in placemark_names(root)
    assert placemark_folder_path(root, "EXT.MR.NRG.MGTDMR.P001")[-1] == "EXISTING POLE EMR 7-4"
    assert placemark_folder_path(root, "EXT.MR.NRG.MGTDMR.P002")[-1] == "EXISTING POLE EMR 7-4"


def test_bare_ext_partner_aliases_are_honored(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_aliases.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("Some Random Folder", ["EXT.TELKOM", "EXT.FM", "EXT.EMR"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = placemark_names(root)
    tlkm = next(name for name in names if name.startswith("EXT.TLKM."))
    ln = next(name for name in names if name.startswith("EXT.LN."))
    mr = next(name for name in names if name.startswith("EXT.MR."))
    assert placemark_folder_path(root, tlkm)[-1] == "EXISTING POLE PARTNER 7-4"
    assert placemark_folder_path(root, ln)[-1] == "EXISTING POLE PARTNER 7-4"
    assert placemark_folder_path(root, mr)[-1] == "EXISTING POLE EMR 7-4"


def test_bare_ext_partner_does_not_double_rewrite_existing_canonical_name(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_existing_canonical.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("EXISTING POLE EMR 7-4", ["EXT.MR.NRG.MGTDMR.P003", "EXT.MR"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = placemark_names(root)
    assert "EXT.MR.NRG.MGTDMR.P001" in names
    assert "EXT.MR.NRG.MGTDMR.P002" in names
    assert len([name for name in names if name.startswith("EXT.MR.NRG.MGTDMR.P")]) == 2


def test_bare_ext_partner_cluster_scope_omits_olt_segment(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_cluster.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("LINE A", ["EXT.MR", "EXT.LN"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Cluster")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = placemark_names(root)
    assert "EXT.MR.MGTDMR.P001" in names
    assert "EXT.LN.MGTDMR.P002" in names
    assert all("NRG" not in name for name in names if name.startswith("EXT."))


def test_bare_ext_partner_numbering_shared_across_partner_types(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_shared_numbers.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("Some Random Folder", ["EXT.MR", "EXT.MR", "EXT.MTI", "EXT.LN", "EXT.TLKM"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    names = bare_ext_partner_names(root)
    assert ext_partner_pole_numbers(names) == ["P001", "P002", "P003", "P004", "P005"]


def test_bare_ext_partner_outside_existing_pole_folder_is_organized(tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    source_kmz = tmp_path / "bare_ext_random_folder.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    make_coding_workbook(workbook_path)
    project = "BKS002583 - SF MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7"
    make_bare_ext_partner_kmz(source_kmz, project, [("Some Random Folder", ["EXT.MR"])])

    standardize_kmz_file(source_kmz, workbook_path, output_kmz, network_type="AE", scope_type="Feeder")

    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))
    assert "EXT.MR.NRG.MGTDMR.P001" in placemark_names(root)
    assert placemark_folder_path(root, "EXT.MR.NRG.MGTDMR.P001")[-1] == "EXISTING POLE EMR 7-4"


def pole_path_record(olt_initial: str = "") -> FdtCodingRecord:
    return FdtCodingRecord("Sheet", 1, "Site", 1, "149", "MGTDMR.149", "IDX", "MGTDMR", olt_initial)


def pole_path_root(body: str) -> ET.Element:
    return ET.fromstring(f'''<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Project</name>{body}</Folder></Document></kml>''')


def point_placemark(name: str, lon: float, lat: float) -> str:
    return f'<Placemark><name>{name}</name><Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>'


def line_placemark(name: str, coords: str) -> str:
    return f'<Placemark><name>{name}</name><LineString><coordinates>{coords}</coordinates></LineString></Placemark>'


def test_cluster_new_pole_74_and_hc_73_share_one_path_sequence(tmp_path):
    source_kmz = tmp_path / "mixed-new-poles.kmz"
    output_kmz = tmp_path / "standardized.kmz"
    kml = f'''<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Site</name>
      <Folder><name>FDT</name>{point_placemark("FDT 1 MGTDMR.149", 106.0000, -6.0)}</Folder>
      <Folder><name>LINE A - FDT 1</name>
        <Folder><name>DISTRIBUTION CABLE</name>{line_placemark("CABLE LINE A (FO 24C/2T)", "106.0040,-6.0,0 106.0000,-6.0,0")}</Folder>
        <Folder><name>NEW POLE 7-4</name>
          {point_placemark("MR", 106.0005, -6.0)}
          {point_placemark("MR", 106.0015, -6.0)}
        </Folder>
        <Folder><name>POLE</name><Folder><name>HC</name>
          {point_placemark("HC", 106.0010, -6.0)}
          {point_placemark("HC", 106.0020, -6.0)}
        </Folder></Folder>
      </Folder>
    </Folder></Document></kml>'''
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)

    standardize_kmz_file(
        source_kmz,
        None,
        output_kmz,
        network_type="AE",
        scope_type="Cluster",
        coding_records=[pole_path_record()],
        source_name="Site.kmz",
    )
    with zipfile.ZipFile(output_kmz) as archive:
        root = ET.fromstring(archive.read("doc.kml"))

    poles = _collect_pole_placemarks(root, [])
    by_lon = {
        round(pole["point"][0], 4): direct_child_text(pole["element"], "name")
        for pole in poles
        if pole["kind"] == "NEW"
    }
    assert [
        by_lon[106.0005],
        by_lon[106.0010],
        by_lon[106.0015],
        by_lon[106.0020],
    ] == [
        "MR.MGTDMR.P001",
        "MR.MGTDMR.P002",
        "MR.MGTDMR.P003",
        "MR.MGTDMR.P004",
    ]
    assert placemark_folder_path(root, by_lon[106.0005])[-1] == "NEW POLE 7-4"
    assert placemark_folder_path(root, by_lon[106.0010])[-1] == "NEW POLE 7-3"
    assert placemark_folder_path(root, by_lon[106.0015])[-1] == "NEW POLE 7-4"
    assert placemark_folder_path(root, by_lon[106.0020])[-1] == "NEW POLE 7-3"
    assert len(set(by_lon.values())) == 4
    assert not any(".HC" in name for name in by_lon.values())


def test_pole_path_order_numbers_cluster_poles_along_cable_direction():
    root = pole_path_root(f'''
      <Folder><name>LINE A</name><Folder><name>FDT 1</name><Folder><name>DISTRIBUTION CABLE</name>{line_placemark('Cable', '106.0,-6.0,0 106.003,-6.0,0')}</Folder></Folder></Folder>
      <Folder><name>LINE A</name><Folder><name>NEW POLE 7-3</name>{point_placemark('MR', 106.002, -6.0)}{point_placemark('MR', 106.001, -6.0)}</Folder></Folder>
      <Folder><name>LINE A</name><Folder><name>EXISTING POLE EMR 7-4</name>{point_placemark('EXT.MR', 106.0025, -6.0)}{point_placemark('EXT.MR', 106.0005, -6.0)}</Folder></Folder>
    ''')
    used: dict[str, set[int]] = {}
    order = _compute_cluster_pole_path_order(root, pole_path_record(), used)
    assigned = {direct_child_text(p["element"], "name") + str(p["point"]): order[p["id"]] for p in _collect_pole_placemarks(root, [])}
    assert assigned["MR(106.001, -6.0)"] == "P001"
    assert assigned["MR(106.002, -6.0)"] == "P002"
    assert assigned["EXT.MR(106.0005, -6.0)"] == "P001"
    assert assigned["EXT.MR(106.0025, -6.0)"] == "P002"


def test_pole_path_order_cluster_sweeps_fdt_before_line():
    root = pole_path_root(''.join(
        f'<Folder><name>LINE {line}</name><Folder><name>FDT {fdt}</name><Folder><name>DISTRIBUTION CABLE</name>{line_placemark("Cable", f"{106 + index * .01},-6,0 {106 + index * .01 + .001},-6,0")}</Folder></Folder><Folder><name>NEW POLE 7-3</name>{point_placemark("MR", 106 + index * .01 + .0005, -6)}</Folder></Folder>'
        for index, (fdt, line) in enumerate([(1, 'A'), (2, 'A'), (1, 'B'), (2, 'B')])
    ))
    used: dict[str, set[int]] = {}
    order = _compute_cluster_pole_path_order(root, pole_path_record(), used)
    by_point = {round(p["point"][0], 4): order[p["id"]] for p in _collect_pole_placemarks(root, [])}
    assert [by_point[106.0005], by_point[106.0205], by_point[106.0105], by_point[106.0305]] == ["P001", "P002", "P003", "P004"]


def test_pole_path_order_ignores_poles_outside_snap_radius():
    root = pole_path_root(f'''
      <Folder><name>DISTRIBUTION CABLE</name>{line_placemark('Cable', '106.0,-6.0,0 106.001,-6.0,0')}</Folder>
      <Folder><name>NEW POLE 7-3</name>{point_placemark('MR', 106.0005, -6.001)}</Folder>
    ''')
    order = _compute_cluster_pole_path_order(root, pole_path_record(), {})
    # Pole is outside snap radius of the cable, so it goes to the leftover
    # bucket — still gets a number, just appended after path-walked poles.
    assert len(order) == 1
    assert all(code.startswith("P") for code in order.values())


def test_pole_path_order_empty_without_cables_falls_back_to_tree_order(tmp_path):
    source_kmz = tmp_path / "no_cable.kmz"
    output_kmz = tmp_path / "out.kmz"
    workbook_path = tmp_path / "coding.xlsx"
    make_coding_workbook(workbook_path)
    kml = f'''<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>BKS002583 - MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7</name><Folder><name>NEW POLE 7-3</name>{point_placemark('MR',106,-6)}{point_placemark('MR',106.001,-6)}</Folder><Folder><name>EXISTING POLE EMR 7-4</name>{point_placemark('EXT.MR',106.002,-6)}</Folder></Folder></Document></kml>'''
    with zipfile.ZipFile(source_kmz, "w") as archive:
        archive.writestr("doc.kml", kml)
    standardize_kmz_file(source_kmz, workbook_path, output_kmz, scope_type="Cluster")
    root = ET.fromstring(zipfile.ZipFile(output_kmz).read("doc.kml"))
    names = placemark_names(root)
    assert "MR.MGTDMR.P001" in names
    assert "MR.MGTDMR.P002" in names
    assert "EXT.MR.MGTDMR.P001" in names


def test_project_distance_along_segment_projects_forward():
    assert 45 < _project_distance_along_segment((106.0005, -6.0), (106.0, -6.0), (106.001, -6.0)) < 60


def test_feeder_pole_path_order_uses_fdt_sorted_cables():
    root = pole_path_root(f'''
      <Folder><name>FDT</name>{point_placemark('MGTDMR.002', 106.02, -6.0)}{point_placemark('MGTDMR.001', 106.0, -6.0)}</Folder>
      <Folder><name>CABLE</name>{line_placemark('Cable 2', '106.02,-6,0 106.021,-6,0')}{line_placemark('Cable 1', '106.0,-6,0 106.001,-6,0')}</Folder>
      <Folder><name>NEW POLE 7-3</name>{point_placemark('MR.MGTDMR.P099', 107, -6)}{point_placemark('MR', 106.0205, -6)}{point_placemark('MR', 106.0005, -6)}</Folder>
    ''')
    used: dict[str, set[int]] = {}
    fdt_entries = [{"code": "MGTDMR.002", "lon": 106.02, "lat": -6.0}, {"code": "MGTDMR.001", "lon": 106.0, "lat": -6.0}]
    order = _compute_feeder_pole_path_order(root, pole_path_record("NRG"), used, fdt_entries)
    by_point = {p["point"][0]: order[p["id"]] for p in _collect_pole_placemarks(root, []) if p["id"] in order}
    assert by_point[106.0005] == "P001"
    assert by_point[106.0205] == "P002"

def test_subfeeder_pole_path_order_uses_target_fdt_not_nearest_fdt():
    """Regression: a Subfeeder cable belongs to FDT 268, but its away-end is
    drawn closer to neighbouring FDT 267 than its near-end is to FDT 268.

    The chain orienter must anchor the cable to its OWN target FDT (268) and
    not flip orientation just because a neighbouring FDT (267) sits closer
    to the away-end.

    Layout (longitudes, lat=-6.0 throughout):
      FDT 267 at 106.0000      ← unrelated FDT
      FDT 268 at 106.0030      ← cable's target
      Cable drawn away→near:
          start (away) = 106.00050   ~58 m from FDT 267, ~278 m from FDT 268
          end   (near) = 106.00295    ~5.6 m from FDT 268, ~328 m from FDT 267
      Three poles along the cable, west→east: 106.00100, 106.00170, 106.00240

    Pre-fix: "min distance to any FDT" picks FDT 267 for the away-end (58 m)
    and FDT 268 for the near-end (5.6 m). Both endpoints are far from the
    cable's target (>30 m fdt_radius), neither end is "FDT-side", chain
    propagation has nothing to anchor to, and the cable keeps its drawn
    direction (away→near) — pole order ends up backward.

    Post-fix: target_fdt for this cable is 268 (whichever FDT is closest to
    ANY endpoint of THIS cable). The end-point sits 5.6 m from FDT 268, well
    within fdt_radius_meter (30 m), so orientation flips so the FDT-side
    becomes the start. P001 ends up nearest the FDT.
    """
    fdt_entries = [
        {"code": "MGTDMR.267", "lon": 106.0, "lat": -6.0},
        {"code": "MGTDMR.268", "lon": 106.003, "lat": -6.0},
    ]
    cable_coords = "106.00050,-6.0,0 106.00100,-6.0,0 106.00170,-6.0,0 106.00240,-6.0,0 106.00295,-6.0,0"
    root = pole_path_root(f'''
      <Folder><name>FDT</name>
        {point_placemark('MGTDMR.267', 106.0, -6.0)}
        {point_placemark('MGTDMR.268', 106.003, -6.0)}
      </Folder>
      <Folder><name>CABLE</name>
        {line_placemark('Cable 268', cable_coords)}
      </Folder>
      <Folder><name>NEW POLE 7-3</name>
        {point_placemark('MR', 106.00100, -6.0)}
        {point_placemark('MR', 106.00170, -6.0)}
        {point_placemark('MR', 106.00240, -6.0)}
      </Folder>
    ''')
    used: dict[str, set[int]] = {}
    order = _compute_feeder_pole_path_order(
        root, pole_path_record("NRG"), used, fdt_entries, is_subfeeder=True
    )
    by_lon = {round(p["point"][0], 5): order[p["id"]] for p in _collect_pole_placemarks(root, []) if p["id"] in order}
    # Pole closest to FDT 268 (eastmost) must be P001, then walking west.
    assert by_lon[106.00240] == "P001"
    assert by_lon[106.00170] == "P002"
    assert by_lon[106.00100] == "P003"



def test_subfeeder_pole_path_order_chain_with_three_fdts_walks_from_smallest_fdt():
    """User's explicit endpoint rules:
      Rule 1: endpoint with FDT only (no other cable)        → START
      Rule 2: endpoint with FDT AND another cable joining    → END
      Rule 3: endpoint with no FDT                           → END
    And cables are sorted by smallest FDT order first.

    Layout: a chain with three FDTs (265, 266, 267) and three cables.
      FDT 265 ─── Cable_265 ─── FDT 266 ─── Cable_266 ─── FDT 267 ─── Cable_267 ─── (free stub end)

    Expected behaviour:
      - Cable_265: starts at FDT 265 (FDT-only end), ends at FDT 266
        (FDT+junction). Cable_266 must therefore start at FDT 266 (its
        upstream side) and end at FDT 267 (downstream FDT+junction).
      - Cable_267: starts at FDT 267 (FDT-only end on its side because the
        free stub has no FDT), ends at the free vertex.
      - Pole numbering walks Cable_265 first (smallest FDT order), then
        Cable_266, then Cable_267, and within each cable runs from the
        upstream end toward the downstream end.

    Concrete layout (lat = -6.0):
      FDT 265 at lon 106.0000
      FDT 266 at lon 106.0030
      FDT 267 at lon 106.0060
      Cable_265 vertices: 106.00005 → 106.00295  (one pole at 106.0010, one at 106.0020)
      Cable_266 vertices: 106.00305 → 106.00595  (one pole at 106.0040, one at 106.0050)
      Cable_267 vertices: 106.00605 → 106.00800  (one pole at 106.0070)
      → All cables drawn forward (start = upstream FDT) so a working
        orienter must NOT reverse them.

    Pole numbering expected (walking smallest FDT first):
      P001 = 106.0010   (Cable_265, near FDT 265)
      P002 = 106.0020   (Cable_265, midway to FDT 266)
      P003 = 106.0040   (Cable_266, near FDT 266)
      P004 = 106.0050   (Cable_266, midway to FDT 267)
      P005 = 106.0070   (Cable_267, near FDT 267)
    """
    fdt_entries = [
        {"code": "MGTDMR.265", "lon": 106.0000, "lat": -6.0},
        {"code": "MGTDMR.266", "lon": 106.0030, "lat": -6.0},
        {"code": "MGTDMR.267", "lon": 106.0060, "lat": -6.0},
    ]
    cable_265 = "106.00005,-6.0,0 106.00100,-6.0,0 106.00200,-6.0,0 106.00295,-6.0,0"
    cable_266 = "106.00305,-6.0,0 106.00400,-6.0,0 106.00500,-6.0,0 106.00595,-6.0,0"
    cable_267 = "106.00605,-6.0,0 106.00700,-6.0,0 106.00800,-6.0,0"
    root = pole_path_root(f'''
      <Folder><name>FDT</name>
        {point_placemark('MGTDMR.265', 106.0000, -6.0)}
        {point_placemark('MGTDMR.266', 106.0030, -6.0)}
        {point_placemark('MGTDMR.267', 106.0060, -6.0)}
      </Folder>
      <Folder><name>CABLE</name>
        {line_placemark('Cable 267', cable_267)}
        {line_placemark('Cable 265', cable_265)}
        {line_placemark('Cable 266', cable_266)}
      </Folder>
      <Folder><name>NEW POLE 7-3</name>
        {point_placemark('MR', 106.0010, -6.0)}
        {point_placemark('MR', 106.0020, -6.0)}
        {point_placemark('MR', 106.0040, -6.0)}
        {point_placemark('MR', 106.0050, -6.0)}
        {point_placemark('MR', 106.0070, -6.0)}
      </Folder>
    ''')
    used: dict[str, set[int]] = {}
    order = _compute_feeder_pole_path_order(
        root, pole_path_record("NRG"), used, fdt_entries, is_subfeeder=True
    )
    by_lon = {round(p["point"][0], 5): order[p["id"]] for p in _collect_pole_placemarks(root, []) if p["id"] in order}
    assert by_lon[106.00100] == "P001"
    assert by_lon[106.00200] == "P002"
    assert by_lon[106.00400] == "P003"
    assert by_lon[106.00500] == "P004"
    assert by_lon[106.00700] == "P005"


def test_subfeeder_pole_path_order_uses_ten_meter_junction_radius():
    fdt_entries = [{"code": "MGTDMR.265", "lon": 106.0000, "lat": -6.0}]
    root = pole_path_root(f'''
      <Folder><name>FDT</name>{point_placemark('MGTDMR.265', 106.0000, -6.0)}</Folder>
      <Folder><name>CABLE</name>
        {line_placemark('Cable 1', '106.00000,-6.0,0 106.00100,-6.0,0')}
        {line_placemark('Cable 2 reversed', '106.00200,-6.0,0 106.00108,-6.0,0')}
      </Folder>
      <Folder><name>NEW POLE 7-3</name>
        {point_placemark('MR', 106.00120, -6.0)}
        {point_placemark('MR', 106.00180, -6.0)}
      </Folder>
    ''')
    used: dict[str, set[int]] = {}
    order = _compute_feeder_pole_path_order(
        root, pole_path_record("NRG"), used, fdt_entries, is_subfeeder=True
    )
    by_lon = {round(p["point"][0], 5): order[p["id"]] for p in _collect_pole_placemarks(root, []) if p["id"] in order}
    assert by_lon[106.00120] == "P001"
    assert by_lon[106.00180] == "P002"


def test_subfeeder_pole_path_order_reversed_drawing_still_walks_from_fdt():
    """Same chain as the test above, but the user drew cable_266
    BACKWARDS (from FDT 267 toward FDT 266). The orienter must still
    detect that FDT 266 (the smaller, "upstream" FDT) is the start side
    because:
      - At FDT 266 endpoint: there's an FDT and another cable (cable_265)
        ends here too — score -2 (END per Rule 2). Wait, that's END.

    Actually re-reading: Rule 2 says FDT+junction → END. So for cable_266
    drawn from 266 to 267:
      - 266 endpoint: FDT 266 + cable_265's end → score -2 → END
      - 267 endpoint: FDT 267 + cable_267's start → score -2 → END
    Both ends are scored END. Tie → chain propagation kicks in:
      Cable_265 is oriented (FDT-only at 265 → START at 265, FDT+junction
      at 266 → END at 266). Cable_266's endpoint matches cable_265's END,
      so cable_266's START is at 266 (chain-propagated from cable_265).

    This test verifies the chain propagation handles a fork-like junction
    where Rule 1/2/3 alone leave both ends tied.
    """
    fdt_entries = [
        {"code": "MGTDMR.265", "lon": 106.0000, "lat": -6.0},
        {"code": "MGTDMR.266", "lon": 106.0030, "lat": -6.0},
        {"code": "MGTDMR.267", "lon": 106.0060, "lat": -6.0},
    ]
    cable_265 = "106.00005,-6.0,0 106.00100,-6.0,0 106.00200,-6.0,0 106.00295,-6.0,0"
    # Cable_266 drawn BACKWARDS (267 → 266).
    cable_266_rev = "106.00595,-6.0,0 106.00500,-6.0,0 106.00400,-6.0,0 106.00305,-6.0,0"
    cable_267 = "106.00605,-6.0,0 106.00700,-6.0,0 106.00800,-6.0,0"
    root = pole_path_root(f'''
      <Folder><name>FDT</name>
        {point_placemark('MGTDMR.265', 106.0000, -6.0)}
        {point_placemark('MGTDMR.266', 106.0030, -6.0)}
        {point_placemark('MGTDMR.267', 106.0060, -6.0)}
      </Folder>
      <Folder><name>CABLE</name>
        {line_placemark('Cable 265', cable_265)}
        {line_placemark('Cable 266', cable_266_rev)}
        {line_placemark('Cable 267', cable_267)}
      </Folder>
      <Folder><name>NEW POLE 7-3</name>
        {point_placemark('MR', 106.0010, -6.0)}
        {point_placemark('MR', 106.0020, -6.0)}
        {point_placemark('MR', 106.0040, -6.0)}
        {point_placemark('MR', 106.0050, -6.0)}
        {point_placemark('MR', 106.0070, -6.0)}
      </Folder>
    ''')
    used: dict[str, set[int]] = {}
    order = _compute_feeder_pole_path_order(
        root, pole_path_record("NRG"), used, fdt_entries, is_subfeeder=True
    )
    by_lon = {round(p["point"][0], 5): order[p["id"]] for p in _collect_pole_placemarks(root, []) if p["id"] in order}
    # Even though cable_266 was drawn 267→266, walking from FDT 266
    # outward must put 106.0040 (closer to FDT 266) as P003.
    assert by_lon[106.00100] == "P001"
    assert by_lon[106.00200] == "P002"
    assert by_lon[106.00400] == "P003"
    assert by_lon[106.00500] == "P004"
    assert by_lon[106.00700] == "P005"
