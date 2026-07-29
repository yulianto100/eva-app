from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from shapely.geometry import Point as ShapelyPoint

from converter import build_transformer, is_kml_path, project_point, read_kml_root
from hpdb import (
    HpdbIssue,
    HpdbParseResult,
    HpdbPlacemark,
    add_issue,
    build_hpdb_preview,
    build_street_lookup,
    collect_hpdb_placemarks,
    detect_parent_line_fdt,
    infer_network_type,
    is_valid_coordinate,
    load_hpdb_defaults,
    normalize_city_value,
    normalize_street_value,
    normalize_hpdb_defaults_for_network,
    normalize_input_paths,
    normalized_folder_name,
    parse_hpdb_from_kmz,
    pipeline_log,
    pipeline_progress,
)

MANCORE_HEADERS = [
    "FDT Tray (Front)", "FDT \nPort", "Line", "Capacity", "Tube Colour", "Core Number", "FAT ID", "FAT Port",
    "Pole ID", "Pole Latitude", "Pole Longitude", "FAT Address", "Province", "regency_city", "district",
    "subdistrict", "postalcode", "Roll_Out_ID", "acquisition_class", "acquisition_tier", "competition",
    "project_id", "projectName", "Complex_name", "ClusterId", "Clustername", "Commercial_name", "Rw", "Rt",
    "address_prefix", "street", "address_suffix", "sub_address_prefix", "sub_address", "sub_address_suffix",
    "block", "homenumber", "dwelling_type", "building_type", "building_property_name",
    "building_property_location", "floor", "unit", "homepass_source", "oltcode", "olthostname", "fdtcode",
    "fatcode", "RFSDate", "Latitude_homepass", "Longitude_homepass", "vendor_homepass_id",
    "residential_service_ready", "sme_service_ready", "enterprise_service_ready", "installation", "availability",
    "classification", "business_case", "network_presence", "wallplate_installation", "publish",
]
MANCORE_ACCESS_COLUMNS = ("Pole ID", "Pole Latitude", "Pole Longitude")
MANCORE_UG_ACCESS_COLUMNS = ("Handhole ID", "Handhole Latitude", "Handhole Longitude")
MANCORE_COLUMN_WIDTHS = {
    "FAT Address": 24,
    "Province": 18,
    "regency_city": 20,
    "district": 18,
    "subdistrict": 18,
    "postalcode": 14,
    "Roll_Out_ID": 16,
    "acquisition_class": 18,
    "acquisition_tier": 16,
    "projectName": 18,
    "Complex_name": 24,
    "Clustername": 24,
    "Commercial_name": 24,
    "homenumber": 20,
    "Latitude_homepass": 16,
    "Longitude_homepass": 16,
}

MANCORE_HPDB_HEADERS = {
    "postalcode", "Roll_Out_ID", "acquisition_class", "acquisition_tier", "competition", "project_id",
    "projectName", "Complex_name", "ClusterId", "Clustername", "Commercial_name", "Rw", "Rt",
    "address_prefix", "street", "address_suffix", "sub_address_prefix", "sub_address", "sub_address_suffix",
    "block", "homenumber", "dwelling_type", "building_type", "building_property_name",
    "building_property_location", "floor", "unit", "homepass_source", "oltcode", "olthostname", "fdtcode",
    "fatcode", "RFSDate", "Latitude_homepass", "Longitude_homepass", "vendor_homepass_id",
    "residential_service_ready", "sme_service_ready", "enterprise_service_ready", "installation",
    "availability", "classification", "business_case", "network_presence", "wallplate_installation", "publish",
}

def fat_suffix_number(fat_code: str | None) -> int:
    match = re.search(r"(\d+)$", str(fat_code or ""))
    return int(match.group(1)) if match else 0

def line_name_from_fat_code(fat_code: str | None, fallback: str = "") -> str:
    match = re.match(r"([A-Z])", str(fat_code or "").strip().upper())
    return f"LINE {match.group(1)}" if match else fallback

def mancore_capacity_for_line(fat_codes: Sequence[str]) -> str:
    max_number = max([fat_suffix_number(code) for code in fat_codes] or [0])
    if max_number <= 10:
        return "24C/2T"
    if max_number <= 15:
        return "36C/3T"
    return "48C/4T"

def mancore_used_cores(capacity: str) -> list[int]:
    match = re.search(r"\d+", capacity or "")
    capacity_number = int(match.group(0)) if match else 24
    groups = 2 if capacity_number <= 24 else 3 if capacity_number <= 36 else 4
    cores: list[int] = []
    for group in range(groups):
        start = 1 + group * 12
        cores.extend(range(start, start + 10))
    return cores

def mancore_core_pair_for_fat(fat_code: str | None, capacity: str) -> tuple[int | None, int | None]:
    index = max(fat_suffix_number(fat_code) - 1, 0)
    cores = mancore_used_cores(capacity)
    first = index * 2
    if first + 1 >= len(cores):
        return None, None
    return cores[first], cores[first + 1]

def mancore_fdt_port_for_fat(fat_code: str | None) -> int | None:
    number = fat_suffix_number(fat_code)
    return number * 2 - 1 if number else None

def line_sort_key(line_name: str | None) -> tuple[str, str]:
    text = str(line_name or "").strip().upper()
    match = re.search(r"\bLINE\s+([A-Z])\b", text)
    return (match.group(1) if match else text, text)

def mancore_cluster_label(cluster_name: str) -> str:
    return re.sub(r"\s+", " ", str(cluster_name or "").replace(" - FDT ", " FDT-")).strip()

def mancore_city(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^Kabupaten\s+", "Kab. ", text, flags=re.IGNORECASE)
    text = re.sub(r"^Kota\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()

def mancore_fat_address(street: str | None) -> str:
    clean = re.sub(r"\s+", " ", str(street or "").strip()).upper()
    if not clean:
        return ""
    return clean if clean.startswith(("JL.", "JALAN ")) else f"JL. {clean}"

def mancore_admin_value(defaults: dict, *keys: str) -> str:
    for key in keys:
        value = str(defaults.get(key) or "").strip()
        if value:
            if key in {"city", "regency_city"}:
                return normalize_city_value(value)
            return value.upper()
    return ""

def collect_hpdb_input_placemarks(input_paths: str | Path | Iterable[str | Path]) -> list[HpdbPlacemark]:
    placemarks: list[HpdbPlacemark] = []
    for path in [path for path in normalize_input_paths(input_paths) if is_kml_path(path)]:
        collect_hpdb_placemarks(read_kml_root(path), [], placemarks)
    return placemarks

def is_hpdb_pole_placemark(placemark: HpdbPlacemark) -> bool:
    return any("POLE" in normalized_folder_name(folder) and "HP" not in normalized_folder_name(folder) for folder in placemark.folder_stack)

def is_hpdb_handhole_placemark(placemark: HpdbPlacemark) -> bool:
    return any(normalized_folder_name(folder).startswith("NEW HH") for folder in placemark.folder_stack)

def mancore_headers(network_type: str | None = None) -> list[str]:
    headers = list(MANCORE_HEADERS)
    if str(network_type or "").strip().upper() == "UG":
        for old_header, new_header in zip(MANCORE_ACCESS_COLUMNS, MANCORE_UG_ACCESS_COLUMNS):
            headers[headers.index(old_header)] = new_header
    return headers

def mancore_access_columns(network_type: str | None = None) -> tuple[str, str, str]:
    return MANCORE_UG_ACCESS_COLUMNS if str(network_type or "").strip().upper() == "UG" else MANCORE_ACCESS_COLUMNS

def mancore_access_item_label(network_type: str | None = None) -> str:
    return "handhole" if str(network_type or "").strip().upper() == "UG" else "pole"

def nearest_access_for_fat(fat_row: dict, access_placemarks: Sequence[HpdbPlacemark], transformer=None) -> HpdbPlacemark | None:
    if not access_placemarks or not is_valid_coordinate(fat_row.get("FAT Latitude"), fat_row.get("FAT Longitude")):
        return None
    transformer = transformer or build_transformer()
    fat_point = ShapelyPoint(project_point(float(fat_row["FAT Longitude"]), float(fat_row["FAT Latitude"]), transformer))
    fdt_number = int(fat_row.get("_fdt") or 0)
    candidates = []
    for placemark in access_placemarks:
        if not is_valid_coordinate(placemark.latitude, placemark.longitude):
            continue
        parent = detect_parent_line_fdt(placemark.folder_stack)
        if parent and fdt_number and parent[1] != fdt_number:
            continue
        access_point = ShapelyPoint(project_point(float(placemark.longitude), float(placemark.latitude), transformer))
        candidates.append((fat_point.distance(access_point), placemark))
    if not candidates:
        for placemark in access_placemarks:
            if is_valid_coordinate(placemark.latitude, placemark.longitude):
                access_point = ShapelyPoint(project_point(float(placemark.longitude), float(placemark.latitude), transformer))
                candidates.append((fat_point.distance(access_point), placemark))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None

def nearest_pole_for_fat(fat_row: dict, pole_placemarks: Sequence[HpdbPlacemark], transformer=None) -> HpdbPlacemark | None:
    return nearest_access_for_fat(fat_row, pole_placemarks, transformer=transformer)

def build_mancore_access_lookup(
    result: HpdbParseResult,
    placemarks: Sequence[HpdbPlacemark],
    network_type: str | None = None,
) -> dict[tuple[int, str], HpdbPlacemark | None]:
    transformer = build_transformer()
    if str(network_type or "").strip().upper() == "UG":
        access_placemarks = [placemark for placemark in placemarks if is_hpdb_handhole_placemark(placemark)]
    else:
        access_placemarks = [placemark for placemark in placemarks if is_hpdb_pole_placemark(placemark)]
    return {
        (int(row.get("_fdt") or 0), str(row.get("FAT Code") or "")): nearest_access_for_fat(row, access_placemarks, transformer=transformer)
        for row in result.fat_rows
    }

def build_mancore_pole_lookup(result: HpdbParseResult, placemarks: Sequence[HpdbPlacemark]) -> dict[tuple[int, str], HpdbPlacemark | None]:
    return build_mancore_access_lookup(result, placemarks, network_type="AE")

def apply_mancore_sheet_style(worksheet, headers: Sequence[str]) -> None:
    green_fill = PatternFill("solid", fgColor="92D050")
    blue_fill = PatternFill("solid", fgColor="0070C0")
    brown_fill = PatternFill("solid", fgColor="A23A00")
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    worksheet.freeze_panes = "A9"
    worksheet.auto_filter.ref = f"A8:{worksheet.cell(8, len(headers)).coordinate}"
    for cell in worksheet[8]:
        cell.fill = brown_fill if cell.column >= 13 else green_fill
        cell.font = Font(bold=True, color="FFFFFF" if cell.column >= 13 else "000000", size=9)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    for row in worksheet.iter_rows(min_row=9, max_row=worksheet.max_row, max_col=len(headers)):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(horizontal="center" if cell.column <= 12 else "left", vertical="center")
    for column_index in range(1, len(headers) + 1):
        header = headers[column_index - 1]
        width = MANCORE_COLUMN_WIDTHS.get(header, 12)
        if column_index in {9, 24, 26, 27, 37}:
            width = max(width, 24)
        elif column_index in {10, 11, 50, 51}:
            width = max(width, 16)
        worksheet.column_dimensions[worksheet.cell(1, column_index).column_letter].width = width
    worksheet.row_dimensions[8].height = 42
    for column_letter in ("E", "H"):
        for cell in worksheet[column_letter][8:]:
            cell.fill = blue_fill

def write_mancore_metadata(worksheet, cluster_row: dict, rows: Sequence[dict], defaults: dict, network_type: str | None = None) -> None:
    construction_type = "Underground" if str(network_type or "").strip().upper() == "UG" else "Aerial"
    worksheet["A1"] = "FIBER CORE ASSIGNMENT"
    worksheet["A3"] = "Region"
    worksheet["C3"] = f": {str(defaults.get('Area') or defaults.get('area_name') or '').strip()}"
    worksheet["L3"] = "FDT TYPE"
    worksheet["M3"] = ": 48C"
    worksheet["A4"] = "OLT / Feeder Line"
    worksheet["C4"] = f": {defaults.get('olt_code') or ''}"
    worksheet["L4"] = "HP / Construction type"
    worksheet["M4"] = f": {len(rows)} HP / {construction_type}"
    worksheet["A5"] = "Cluster Name"
    worksheet["C5"] = f": {mancore_cluster_label(str(cluster_row.get('cluster_name') or ''))}"
    worksheet["L5"] = "Distance From OLT"
    worksheet["M5"] = ":  m"
    worksheet["A6"] = "FDT ID"
    worksheet["C6"] = f": {cluster_row.get('fdt_code') or ''}"
    worksheet["L6"] = "COORDINATE FDT"
    worksheet["M6"] = f":  {cluster_row.get('latitude_cluster') or ''} {cluster_row.get('longitude_cluster') or ''}"
    for cell in ("A1", "A3", "A4", "A5", "A6", "L3", "L4", "L5", "L6"):
        worksheet[cell].font = Font(bold=True)

def write_mancore_fdt_sheet(
    workbook,
    sheet_name: str,
    cluster_row: dict,
    rows: Sequence[dict],
    defaults: dict,
    access_lookup: dict[tuple[int, str], HpdbPlacemark | None],
    network_type: str | None = None,
    fill_admin_location: bool = False,
) -> None:
    worksheet = workbook.create_sheet(sheet_name)
    headers = mancore_headers(network_type)
    access_id_header, access_lat_header, access_lon_header = mancore_access_columns(network_type)
    write_mancore_metadata(worksheet, cluster_row, rows, defaults, network_type=network_type)
    for column_index, header in enumerate(headers, start=1):
        worksheet.cell(row=8, column=column_index, value=header)
    rows_by_line: dict[str, list[str]] = defaultdict(list)
    rows_by_fat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        fat_code = str(row.get("fatcode") or "")
        rows_by_fat[fat_code].append(row)
        line_name = line_name_from_fat_code(fat_code, str(row.get("_line") or ""))
        if fat_code and fat_code not in rows_by_line[line_name]:
            rows_by_line[line_name].append(fat_code)
    capacity_by_line = {line: mancore_capacity_for_line(codes) for line, codes in rows_by_line.items()}
    sorted_fat_codes = sorted(rows_by_fat, key=lambda code: (code[:1], fat_suffix_number(code), code))
    fdt_port_by_fat: dict[str, int] = {}
    line_port_offset = 0
    for line_name in sorted(rows_by_line, key=line_sort_key):
        line_fat_codes = sorted(rows_by_line[line_name], key=lambda code: (fat_suffix_number(code), code))
        line_max_fat = max([fat_suffix_number(code) for code in line_fat_codes] or [0])
        for fat_code in line_fat_codes:
            port = mancore_fdt_port_for_fat(fat_code)
            if port:
                fdt_port_by_fat[fat_code] = line_port_offset + port
        line_port_offset += line_max_fat * 2
    header_index = {header: index for index, header in enumerate(headers, start=1)}
    row_index = 9
    for fat_code in sorted_fat_codes:
        fat_rows = rows_by_fat[fat_code]
        first_row = fat_rows[0]
        fdt_number = int(first_row.get("_fdt") or 0)
        line_name = line_name_from_fat_code(fat_code, str(first_row.get("_line") or ""))
        capacity = capacity_by_line.get(line_name, "24C/2T")
        core_pair = mancore_core_pair_for_fat(fat_code, capacity)
        access = access_lookup.get((fdt_number, fat_code))
        access_id = access.name if access else ""
        access_lat = access.latitude if access else ""
        access_lon = access.longitude if access else ""
        fat_address = mancore_fat_address(first_row.get("street") or defaults.get("street"))
        for item_index, row in enumerate(fat_rows, start=1):
            values = {
                "FDT Tray (Front)": "G1" if item_index == 1 else "",
                "FDT \nPort": fdt_port_by_fat.get(fat_code) if item_index == 1 else "",
                "Line": line_name if item_index <= 2 else "",
                "Capacity": capacity if item_index <= 2 else "",
                "Tube Colour": 1 if item_index <= 2 else "",
                "Core Number": core_pair[item_index - 1] if item_index <= 2 else "",
                "FAT ID": fat_code if item_index <= 2 else "",
                "FAT Port": item_index,
                access_id_header: access_id,
                access_lat_header: access_lat,
                access_lon_header: access_lon,
                "FAT Address": fat_address,
                "Province": mancore_admin_value(defaults, "province") if fill_admin_location else "",
                "regency_city": mancore_admin_value(defaults, "regency_city", "city") if fill_admin_location else "",
                "district": mancore_admin_value(defaults, "district"),
                "subdistrict": mancore_admin_value(defaults, "sub_district", "subdistrict"),
            }
            for header in MANCORE_HPDB_HEADERS:
                values[header] = row.get(header)
            for header, value in values.items():
                column = header_index.get(header)
                if column:
                    if header.lower() in {"block", "homenumber"}:
                        value = str(value or "").upper()
                    elif header.lower() == "street":
                        value = normalize_street_value(value)
                    cell = worksheet.cell(row=row_index, column=column, value=value)
                    if header == "RFSDate" and value:
                        cell.number_format = "dd/mm/yyyy"
            row_index += 1
    for column_index in range(8, len(headers) + 1):
        worksheet.cell(
            row=7,
            column=column_index,
            value=sum(1 for row_number in range(9, worksheet.max_row + 1) if worksheet.cell(row_number, column_index).value not in (None, "")),
        )
    apply_mancore_sheet_style(worksheet, headers)

def write_mancore_fat_sheet(workbook, rows: Sequence[dict]) -> None:
    worksheet = workbook.create_sheet("FAT")
    for column_index, header in enumerate(["LABEL", "LATITUDE", "LONGITUDE"], start=1):
        worksheet.cell(row=1, column=column_index, value=header)
    for row_index, row in enumerate(rows, start=2):
        worksheet.cell(row=row_index, column=1, value=row.get("FAT Code"))
        worksheet.cell(row=row_index, column=2, value=row.get("FAT Latitude"))
        worksheet.cell(row=row_index, column=3, value=row.get("FAT Longitude"))
    green_fill = PatternFill("solid", fgColor="92D050")
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for row in worksheet.iter_rows(min_row=1, max_row=worksheet.max_row, max_col=3):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if cell.row == 1:
                cell.fill = green_fill
                cell.font = Font(bold=True)
    worksheet.column_dimensions["A"].width = 16
    worksheet.column_dimensions["B"].width = 18
    worksheet.column_dimensions["C"].width = 18

def generate_hpdb_mancore_excel(
    input_paths: str | Path | Iterable[str | Path],
    output_path: str | Path,
    defaults_path: str | Path | None = None,
    network_type: str | None = None,
    auto_geocode: bool = False,
    defaults_override: dict | None = None,
    pipeline_logger=None,
    progress_callback=None,
    process_date: date | None = None,
    cluster_name_override: str | None = None,
) -> dict:
    started_at = time.perf_counter()
    output_path = Path(output_path)
    resolved_network_type = infer_network_type(
        network_type,
        output_path=output_path,
        defaults_path=defaults_path,
    )
    defaults = normalize_hpdb_defaults_for_network(load_hpdb_defaults(defaults_path), resolved_network_type)
    if defaults_override:
        defaults.update(defaults_override)
    pipeline_log(pipeline_logger, "INFO", "HPDB Mancore export started")
    pipeline_log(pipeline_logger, "INFO", f"HPDB Mancore network type: {resolved_network_type}")
    street_lookup = build_street_lookup(input_paths, defaults, pipeline_logger=pipeline_logger)
    parse_result = parse_hpdb_from_kmz(
        input_paths,
        defaults=defaults,
        street_lookup=street_lookup,
        auto_geocode=auto_geocode,
        pipeline_logger=pipeline_logger,
        progress_callback=progress_callback,
        process_date=process_date,
        cluster_name_override=cluster_name_override,
    )
    access_label = mancore_access_item_label(resolved_network_type)
    access_item = "Handhole ID" if resolved_network_type == "UG" else "Pole ID"
    access_source = "HANDHOLE" if resolved_network_type == "UG" else "POLE"
    access_lookup = build_mancore_access_lookup(
        parse_result,
        collect_hpdb_input_placemarks(input_paths),
        network_type=resolved_network_type,
    )
    missing_access = sum(1 for value in access_lookup.values() if value is None)
    if missing_access:
        add_issue(
            parse_result,
            HpdbIssue(
                status="Warning",
                source_folder=access_source,
                item=access_item,
                message=f"Nearest {access_label} not found for {missing_access:,} FAT row(s).",
            ),
            pipeline_logger,
        )
    workbook = Workbook()
    workbook.remove(workbook.active)
    rows_by_fdt: dict[int, list[dict]] = defaultdict(list)
    for row in parse_result.homepass_rows:
        rows_by_fdt[int(row.get("_fdt") or 0)].append(row)
    cluster_by_fdt = {int(row.get("_fdt") or 0): row for row in parse_result.cluster_rows}
    for fdt_number in sorted(set(rows_by_fdt) | set(cluster_by_fdt)):
        fill_admin_location = bool(
            parse_result.geocode_fields
            or defaults.get("province")
            or defaults.get("regency_city")
            or defaults.get("city")
            or defaults.get("district")
            or defaults.get("sub_district")
            or defaults.get("subdistrict")
        )
        write_mancore_fdt_sheet(
            workbook,
            f"FDT-{fdt_number}",
            cluster_by_fdt.get(fdt_number, {}),
            rows_by_fdt[fdt_number],
            defaults,
            access_lookup,
            network_type=resolved_network_type,
            fill_admin_location=fill_admin_location,
        )
    write_mancore_fat_sheet(workbook, parse_result.fat_rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    pipeline_progress(progress_callback, pipeline_logger, 96, "Preparing HPDB download")
    pipeline_log(pipeline_logger, "SUCCESS", "HPDB Mancore Excel generated successfully")
    summary = {
        **parse_result.summary,
        "mancore_fdt_sheets": len(rows_by_fdt),
        "mancore_poles_matched": sum(1 for value in access_lookup.values() if value is not None),
        "mancore_poles_missing": missing_access,
        "mancore_access_label": access_label,
        "output_size": output_path.stat().st_size if output_path.exists() else 0,
        "duration_seconds": time.perf_counter() - started_at,
    }
    return {
        "output_path": str(output_path),
        "summary": summary,
        "preview": build_hpdb_preview(parse_result),
        "warnings": parse_result.warnings,
        "issues": [issue.to_dict() for issue in parse_result.issues],
        "cluster_rows": parse_result.cluster_rows,
        "fat_rows": parse_result.fat_rows,
        "homepass_rows": parse_result.homepass_rows,
    }

def generate_hpdb_excel(*args, **kwargs) -> dict:
    return generate_hpdb_mancore_excel(*args, **kwargs)

