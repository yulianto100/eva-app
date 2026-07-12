"""Extract placemark name + coordinates from a STANDARDIZED KMZ tree.

Categories extracted:
- FAT      (placemarks in a "FAT" standard folder)
- FDT      (placemarks in a "FDT" standard folder)
- Pole     (placemarks in any "NEW POLE ..." / "EXISTING POLE ..." folder),
           further labelled: New Pole, Pole EXT TLKM/LN/MTI/MR
- HP Cover (placemarks in a "HP COVER" standard folder)

Output: one Excel sheet per source KMZ file, columns:
    Nama Placemark | Latitude | Longitude | Kategori

FAT placemark names get the full FDT code prepended (e.g. "KRHJ25.073.A03").
"""
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

import openpyxl

from converter import (
    direct_child_text_by_name,
    local_name,
    nearest_standard_folder,
)
from kmz_standardizer import (
    FDT_CODE_PATTERN,
    extract_fdt_order,
    find_fat_code,
    normalize_text,
    placemark_point_coordinate,
    placemark_polygon_coordinates,
    polygon_centroid,
)

# Base categories used for user selection / filtering.
CATEGORY_FAT = "FAT"
CATEGORY_FDT = "FDT"
CATEGORY_POLE = "Pole"
CATEGORY_HP = "HP Cover"

ALL_CATEGORIES = [CATEGORY_FAT, CATEGORY_FDT, CATEGORY_POLE, CATEGORY_HP]

# Detailed labels for counting / the Kategori column.
LABEL_NEW_POLE = "New Pole"
EXT_POLE_RE = re.compile(r"\bEXT\.(TLKM|LN|MTI|MR)\b", re.IGNORECASE)


def _category_for_folder(folder: str | None) -> str | None:
    if not folder:
        return None
    f = folder.upper()
    if f == "FAT":
        return CATEGORY_FAT
    if f == "FDT":
        return CATEGORY_FDT
    if f.startswith("NEW POLE") or f.startswith("EXISTING POLE"):
        return CATEGORY_POLE
    if f == "HP COVER":
        return CATEGORY_HP
    return None


def _pole_label(name: str, folder: str | None) -> str:
    """Detailed pole label: New Pole or Pole EXT <partner>."""
    match = EXT_POLE_RE.search(name or "")
    if match:
        return f"Pole EXT {match.group(1).upper()}"
    f = (folder or "").upper()
    if f.startswith("EXISTING POLE"):
        if "EMR" in f:
            return "Pole EXT MR"
        return "Pole EXT"
    return LABEL_NEW_POLE


def _placemark_coordinate(placemark: ET.Element) -> tuple[float, float] | None:
    """Return (lon, lat). Point first, else polygon centroid."""
    pt = placemark_point_coordinate(placemark)
    if pt:
        return pt
    poly = placemark_polygon_coordinates(placemark)
    if poly:
        c = polygon_centroid(poly)
        if c:
            return c
    return None


def extract_coordinates(root: ET.Element) -> list[dict]:
    """Walk a standardized KMZ tree and return placemark rows. Each row has:
    name, lon, lat, category (base), label (detailed)."""
    rows: list[dict] = []
    fdt_codes_in_order: list[str] = []  # full FDT codes (e.g. "KRHJ25.073") in doc order

    def walk(elem: ET.Element, stack: list[str]) -> None:
        for child in list(elem):
            tag = local_name(child.tag)
            if tag in ("Document", "Folder"):
                next_stack = stack
                if tag == "Folder":
                    folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                    next_stack = stack + [folder_name]
                walk(child, next_stack)
                continue
            if tag != "Placemark":
                continue
            folder = nearest_standard_folder(stack)
            category = _category_for_folder(folder)
            if not category:
                continue
            coord = _placemark_coordinate(child)
            if not coord:
                continue
            name = normalize_text(direct_child_text_by_name(child, "name"))
            if category == CATEGORY_FDT:
                match = FDT_CODE_PATTERN.search(name)
                fdt_codes_in_order.append(match.group(0).upper() if match else name.upper())
            label = category
            if category == CATEGORY_POLE:
                label = _pole_label(name, folder)
            rows.append({
                "name": name,
                "lon": coord[0],
                "lat": coord[1],
                "category": category,
                "label": label,
                "stack": list(stack),
            })

    walk(root, [])
    _prefix_fat_names(rows, fdt_codes_in_order)
    for r in rows:
        r.pop("stack", None)
    return rows


def _prefix_fat_names(rows: list[dict], fdt_codes_in_order: list[str]) -> None:
    """Prepend the FULL FDT code (cluster + index, e.g. 'KRHJ25.073') to FAT
    names. Multi-FDT files match each FAT to its FDT via the 'FDT N' order in
    its line folder."""
    if not fdt_codes_in_order:
        return
    order_to_code = {i: code for i, code in enumerate(fdt_codes_in_order, start=1)}
    default_code = fdt_codes_in_order[0]
    for r in rows:
        if r["category"] != CATEGORY_FAT:
            continue
        fat_code = find_fat_code(r["name"]) or r["name"]
        fdt_order = None
        for folder in r.get("stack", []):
            order = extract_fdt_order(folder)
            if order:
                fdt_order = order
        full_code = order_to_code.get(fdt_order) if fdt_order else None
        if not full_code:
            full_code = default_code
        if r["name"].upper().startswith(full_code + "."):
            continue
        r["name"] = f"{full_code}.{fat_code}"


def count_summary(rows: list[dict]) -> dict:
    """Return counts keyed by detailed label, plus base-category totals."""
    label_counts = Counter(r["label"] for r in rows)
    base_counts = Counter(r["category"] for r in rows)
    return {
        "labels": dict(label_counts),
        "categories": dict(base_counts),
        "total": len(rows),
    }


def read_kmz_root(kmz_path: Path) -> ET.Element:
    """Read doc.kml from a KMZ (or a raw KML) and return its root element."""
    kmz_path = Path(kmz_path)
    if zipfile.is_zipfile(kmz_path):
        with zipfile.ZipFile(kmz_path) as archive:
            kml_name = next((n for n in archive.namelist() if n.lower().endswith(".kml")), None)
            if kml_name is None:
                raise ValueError("KMZ tidak punya file .kml di dalamnya")
            data = archive.read(kml_name)
    else:
        data = kmz_path.read_bytes()
    return ET.fromstring(data)


def _safe_sheet_name(filename: str, used: set[str]) -> str:
    """Excel sheet names: max 31 chars, no : \\ / ? * [ ]."""
    base = Path(filename).stem
    for ch in r':\/?*[]':
        base = base.replace(ch, "_")
    base = base[:31] or "Sheet"
    name = base
    i = 1
    while name.upper() in used:
        suffix = f"_{i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(name.upper())
    return name


def build_excel(per_file_rows: dict[str, list[dict]], out_path: Path, categories: list[str] | None = None) -> Path:
    """Build an Excel workbook with one sheet per source KMZ file.
    `categories` filters which base categories to include (None = all)."""
    selected = set(categories) if categories else set(ALL_CATEGORIES)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used_names: set[str] = set()
    order = {CATEGORY_FAT: 0, CATEGORY_FDT: 1, CATEGORY_POLE: 2, CATEGORY_HP: 3}
    if not per_file_rows:
        wb.create_sheet(title="Kosong")
    for filename, rows in per_file_rows.items():
        ws = wb.create_sheet(title=_safe_sheet_name(filename, used_names))
        ws.append(["Nama Placemark", "Latitude", "Longitude", "Kategori"])
        filtered = [r for r in rows if r["category"] in selected]
        for r in sorted(filtered, key=lambda x: (order.get(x["category"], 9), x.get("label", ""), x["name"])):
            ws.append([
                r["name"],
                round(float(r["lat"]), 6),
                round(float(r["lon"]), 6),
                r.get("label", r["category"]),
            ])
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 16
    wb.save(out_path)
    return out_path

