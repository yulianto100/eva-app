from __future__ import annotations

import json
import math
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import osmium
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from shapely.geometry import LineString, Point as ShapelyPoint
try:
    from shapely.strtree import STRtree
except Exception:  # pragma: no cover - optional acceleration depends on Shapely build.
    STRtree = None

from converter import (
    build_transformer,
    children_by_name,
    direct_child_by_name,
    direct_child_text_by_name,
    is_kml_path,
    local_name,
    normalize_input_paths,
    parse_kml_coordinates,
    project_coordinates,
    project_point,
    read_kml_root,
    should_export_road,
    tag_value,
)

HPDB_CLUSTER_SHEET_NAME = "CLUSTER"
HPDB_FAT_SHEET_NAME = "FAT"
HPDB_HOMEPASS_SHEET_NAME = "HOMEPASS"
HPDB_REQUIRED_SHEETS = (
    HPDB_CLUSTER_SHEET_NAME,
    HPDB_FAT_SHEET_NAME,
    HPDB_HOMEPASS_SHEET_NAME,
)
HPDB_MAX_FDT = 10
JAKARTA_TIMEZONE = timezone(timedelta(hours=7))

HPDB_DEFAULTS = {
    "Area": "Bekasi",
    "province": "Jawa Barat",
    "competition": "FTTH",
    "city": "Kab. Bekasi",
    "area_name": "Bekasi",
    "district": "",
    "acquisition_class": "C",
    "acquisition_tier": 3,
    "postal_code": "",
    "sub_district": "",
    "olt_code": "",
    "olthostname": "",
    "cluster_type": "Onnet",
    "complex": "",
    "project_name": "General Retail",
    "project_id": 2,
    "postalcode": "",
    "Roll_Out_ID": "",
    "address_prefix": "Jalan",
    "street": "",
    "address_suffix": "",
    "sub_address_prefix": "",
    "sub_address": "",
    "sub_address_suffix": "",
    "block": "",
    "dwelling_type": "Single Dwelling Unit",
    "building_type": "",
    "building_property_name": "",
    "building_property_location": "",
    "floor": "",
    "unit": "",
    "homepass_source": "RFS",
    "residential_service_ready": True,
    "sme_service_ready": False,
    "enterprise_service_ready": False,
    "installation": "AE",
    "availability": True,
    "classification": "RFS",
    "business_case": "",
    "network_presence": 31,
    "wallplate_installation": False,
    "publish": True,
    "port_count": 16,
    "Rt": 0,
    "Rw": "",
    "vendor_homepass_id": "",
    "homepass_smt_id": "",
    "cluster_id_format": "{cluster_code_astri}.GR0326.{fdt_code}",
    "fallback_fdt_code": "",
    "street_lookup_radius_meters": 150,
}

HPDB_INSTRUCTION_KEYWORDS = (
    "default",
    "nama cluster ditambah",
    "nama path di folder fdt",
    "koordinat dari path fdt",
    "koordinat path fat",
    "parent folder",
    "tanggal upload",
    "nama path hp cover",
)
NETWORK_TYPES = {"AE", "UG"}
ADMIN_GEOCODE_REQUIRED_KEYS = ("province", "regency_city", "district", "sub_district", "postal_code")
ADMIN_GEOCODE_FAT_CANDIDATE_LIMIT = 6
ADMIN_GEOCODE_HP_CANDIDATE_LIMIT = 8

LINE_TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])LINE\s*[-_\s]*([A-Z])(?=$|[^A-Z0-9])", re.IGNORECASE)
FDT_TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])FDT\s*[-_\s]*(\d+)(?=$|[^A-Z0-9])", re.IGNORECASE)
LINE_ONLY_PATTERN = re.compile(r"^\s*LINE\s*[-_\s]*([A-Z])\s*$", re.IGNORECASE)
FDT_SUFFIX_PATTERN = re.compile(r"\.(\d+)\D*$")
FAT_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z]\d{2})(?![A-Z0-9])", re.IGNORECASE)
RW_PATTERN = re.compile(r"\bRW\s*0*(\d+)\b", re.IGNORECASE)
UPLOAD_PREFIX_PATTERN = re.compile(r"^[0-9a-f]{32}_\d+_", re.IGNORECASE)
PROJECT_CODE_PATTERN = re.compile(r"^\s*([A-Z]{2,}\d{3,})\s+(.+)$", re.IGNORECASE)
CITY_KAB_PREFIX_PATTERN = re.compile(r"^\s*(kota|kabupaten|kab\.?)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass
class HpdbIssue:
    status: str
    message: str
    source_folder: str = ""
    parent_line_fdt: str = ""
    item: str = ""
    fdt: int | None = None
    fdt_code: str = ""
    fat_code: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "source_folder": self.source_folder,
            "parent_line_fdt": self.parent_line_fdt,
            "item": self.item,
            "fdt": self.fdt,
            "fdt_code": self.fdt_code,
            "fat_code": self.fat_code,
        }


@dataclass
class HpdbFdt:
    fdt_number: int
    name: str
    fdt_code: str
    suffix_number: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_folder: str = "FDT"


@dataclass
class HpdbPlacemark:
    name: str
    folder_stack: list[str]
    latitude: float | None
    longitude: float | None
    description: str = ""
    kml_index: int = 0

    @property
    def source_path(self) -> str:
        return " > ".join(self.folder_stack)

@dataclass
class HpdbNamedRoad:
    name: str
    line: LineString


@dataclass
class HpdbClusterInfo:
    project_label: str
    base_cluster_name: str
    cluster_code_astri: str
    root_folder_name: str = ""


@dataclass
class HpdbParseResult:
    cluster_info: HpdbClusterInfo
    fdt_map: dict[int, HpdbFdt] = field(default_factory=dict)
    cluster_rows: list[dict] = field(default_factory=list)
    fat_rows: list[dict] = field(default_factory=list)
    homepass_rows: list[dict] = field(default_factory=list)
    hp_uncover_ignored: int = 0
    fdt_detected_total: int = 0
    fdt_skipped_over_max: int = 0
    street_matched: int = 0
    street_unmatched: int = 0
    issues: list[HpdbIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    processed_files: int = 0
    geocode_success: bool = False
    geocode_source_lat: float | None = None
    geocode_source_lon: float | None = None
    geocode_fields: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> dict:
        warning_count = sum(1 for issue in self.issues if issue.status.lower() in {"warning", "skipped", "unmapped", "error"})
        return {
            "total_fdt_detected": self.fdt_detected_total,
            "total_cluster_rows": len(self.cluster_rows),
            "total_fat_rows": len(self.fat_rows),
            "total_homepass_rows": len(self.homepass_rows),
            "total_hp_cover_exported": len(self.homepass_rows),
            "total_hp_uncover_ignored": self.hp_uncover_ignored,
            "total_hpdb_warnings": warning_count,
            "total_fdt_skipped_over_max": self.fdt_skipped_over_max,
            "hpdb_rows": len(self.cluster_rows) + len(self.fat_rows) + len(self.homepass_rows),
            "hpdb_warnings": warning_count,
            "hpdb_fdt": self.fdt_detected_total,
            "hpdb_fat": len(self.fat_rows),
            "hpdb_homepass": len(self.homepass_rows),
            "hp_cover": len(self.homepass_rows),
            "hp_uncover": self.hp_uncover_ignored,
            "street_matched": self.street_matched,
            "street_unmatched": self.street_unmatched,
            "geocode_success": self.geocode_success,
            "geocode_source_lat": self.geocode_source_lat,
            "geocode_source_lon": self.geocode_source_lon,
            "geocode_fields": dict(self.geocode_fields),
        }


def pipeline_log(logger, severity: str, message: str) -> None:
    if logger is None:
        return
    method_name = "warn" if severity.upper() == "WARNING" else severity.lower()
    method = getattr(logger, method_name, None)
    if callable(method):
        method(message)


def pipeline_progress(progress_callback, logger, progress: int, message: str) -> None:
    if progress_callback:
        progress_callback(progress, message)
        return
    method = getattr(logger, "progress", None)
    if callable(method):
        method(progress, message)


def load_hpdb_defaults(defaults_path: str | Path | None = None) -> dict:
    defaults = dict(HPDB_DEFAULTS)
    if defaults_path:
        path = Path(defaults_path)
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                custom_defaults = json.load(file)
            if isinstance(custom_defaults, dict):
                defaults.update(custom_defaults)
    return normalize_hpdb_city_defaults(defaults)

def normalize_city_value(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    match = CITY_KAB_PREFIX_PATTERN.match(text)
    if match:
        prefix = match.group(1).lower().rstrip(".")
        name = match.group(2).strip()
        if prefix in {"kab", "kabupaten"}:
            return f"Kab. {name}"
        if prefix == "kota":
            return f"Kota {name}"
    return text


def normalize_area_name_value(value: str | None) -> str:
    """Keep Area Name in human/dropdown casing for HPDB and ASTRI exports."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^\s*(?:Kabupaten|Kab\.)\s+", "", text, flags=re.IGNORECASE).strip()
    # Legacy ASTRI/OSM sources often yield ALL CAPS. Present these like the Area dropdown.
    if text and text == text.upper() and any(ch.isalpha() for ch in text):
        text = text.title()
    return text

def normalize_street_value(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    # HPDB street stores the road name only; address_prefix keeps the road type.
    text = re.sub(r"^\s*(?:jl\.?|jalan|gang|gg\.?)\s+", "", text, flags=re.IGNORECASE).strip()
    return text.upper()


def normalize_province_value(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    normalized = {
        "jabar": "Jawa Barat",
        "dki": "DKI Jakarta",
        "jakarta": "DKI Jakarta",
    }
    return normalized.get(text.lower(), text)


def normalize_hpdb_city_defaults(defaults: dict) -> dict:
    return normalize_hpdb_city_defaults_in_place(dict(defaults))

def normalize_hpdb_city_defaults_in_place(defaults: dict) -> dict:
    normalized = defaults
    for key in ("city", "regency_city"):
        if key in normalized:
            normalized[key] = normalize_city_value(normalized.get(key))
    return normalized


def infer_network_type(
    network_type: str | None = None,
    template_path: str | Path | None = None,
    output_path: str | Path | None = None,
    defaults_path: str | Path | None = None,
) -> str:
    explicit = str(network_type or "").strip().upper()
    if explicit in NETWORK_TYPES:
        return explicit
    for path_value in (template_path, output_path, defaults_path):
        if not path_value:
            continue
        path_stem = Path(path_value).stem.upper()
        if path_stem.endswith("_UG") or "_UG_" in path_stem:
            return "UG"
        if path_stem.endswith("_AE") or "_AE_" in path_stem:
            return "AE"
    return "AE"

def normalize_hpdb_defaults_for_network(defaults: dict, network_type: str) -> dict:
    normalized = normalize_hpdb_city_defaults(defaults)
    normalized_network = str(network_type or "").strip().upper()
    if normalized_network == "UG":
        normalized["installation"] = "UG"
    elif normalized_network == "AE":
        normalized["installation"] = "AE"
    return normalized

def normalize_workbook_network_labels(workbook, network_type: str) -> int:
    if str(network_type or "").strip().upper() != "UG":
        return 0

    replacements = 0
    token_pattern = re.compile(r"(?<![A-Z0-9])AE(?![A-Z0-9])", re.IGNORECASE)
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or value.startswith("="):
                    continue
                updated = token_pattern.sub("UG", value)
                if updated != value:
                    cell.value = updated
                    replacements += 1
    return replacements

def normalized_folder_name(folder_name: str | None) -> str:
    return re.sub(r"\s+", " ", str(folder_name or "").strip()).upper()


def is_folder_named(folder_name: str | None, target: str) -> bool:
    return normalized_folder_name(folder_name) == normalized_folder_name(target)


def folder_path(folder_stack: Sequence[str]) -> str:
    return " > ".join(folder_stack)


def nearest_folder_index(folder_stack: Sequence[str], folder_name: str) -> int | None:
    target = normalized_folder_name(folder_name)
    for index in range(len(folder_stack) - 1, -1, -1):
        if normalized_folder_name(folder_stack[index]) == target:
            return index
    return None


def extract_fdt_suffix_number(name: str | None) -> int | None:
    match = FDT_SUFFIX_PATTERN.search(name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_fdt_code(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_fat_code(value: str | None) -> str:
    text = str(value or "").strip().upper()
    match = FAT_CODE_PATTERN.search(text)
    if match:
        return match.group(1)
    text = re.sub(r"^\s*FAT\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_homepass_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())

def normalize_homepass_block(value: str | None) -> str:
    text = normalize_homepass_name(value)
    text = re.sub(r"^\s*blok\s+", "", text, flags=re.IGNORECASE)
    return text.strip()

def should_prefix_nn_homepass(value: str) -> bool:
    text = normalize_homepass_name(value)
    return bool(text) and not re.search(r"\d", text) and not re.match(r"^NN\s*-", text, flags=re.IGNORECASE)

def parse_homepass_block_and_number(value: str | None) -> tuple[str, str]:
    text = normalize_homepass_name(value)
    if not text:
        return "", ""
    if re.match(r"^NN\s*-", text, flags=re.IGNORECASE):
        return "", text
    if re.match(r"^HP\s*-", text, flags=re.IGNORECASE):
        return "", text

    _NO_BLOCK = {"NN", "HP", "RUKO", "RK", "TK", "RUKAN", "KIOS", "KAVLING", "KAV", "KOST", "KONTRAKAN", "RUKAN", "PLN", "GRIYA", "GRIYA", "KOVELI", "POS"}

    if "/" in text:
        left, right = text.split("/", 1)
        block = normalize_homepass_block(left)
        homenumber = normalize_homepass_name(right)
        if block and homenumber:
            if block.upper() not in _NO_BLOCK:
                return block, homenumber
            return "", text

    match = re.match(r"^\s*blok\s+([A-Z0-9]+)\s*[-/]\s*(\S.+?)\s*$", text, flags=re.IGNORECASE)
    if match:
        if match.group(1).upper() not in _NO_BLOCK:
            return normalize_homepass_block(match.group(1)), normalize_homepass_name(match.group(2))
        return "", text

    match = re.match(r"^\s*blok\s+([A-Z0-9]+)\s+(\d\S*)\s*$", text, flags=re.IGNORECASE)
    if match:
        if match.group(1).upper() not in _NO_BLOCK:
            return normalize_homepass_block(match.group(1)), normalize_homepass_name(match.group(2))
        return "", text

    match = re.match(r"^\s*([A-Z]{1,2}\d*)\s*-\s*(\d\S*)\s*$", text, flags=re.IGNORECASE)
    if match and match.group(1).upper() not in _NO_BLOCK:
        return normalize_homepass_block(match.group(1)), normalize_homepass_name(match.group(2))

    match = re.match(r"^\s*([A-Z]{1,2})\s+(\d\S*)\s*$", text, flags=re.IGNORECASE)
    if match and match.group(1).upper() not in _NO_BLOCK:
        return normalize_homepass_block(match.group(1)), normalize_homepass_name(match.group(2))

    if should_prefix_nn_homepass(text):
        return "", f"NN-{text}"
    return "", text


def is_osm_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".osm", ".pbf"}


def normalize_project_label(value: str | Path | None) -> str:
    stem = Path(str(value or "")).stem
    stem = UPLOAD_PREFIX_PATTERN.sub("", stem)
    normalized = re.sub(r"[_\s]+", " ", stem).strip()
    normalized = re.sub(r"\s*-\s*", " - ", normalized)
    return normalized.strip().upper()


def split_project_code_and_base(value: str | None) -> tuple[str, str]:
    label = normalize_project_label(value)
    if " - " in label:
        code, base = label.split(" - ", 1)
        return code.strip(), base.strip() or label
    match = PROJECT_CODE_PATTERN.match(label)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", label


def direct_first_folder_name(root) -> str:
    for child in list(root):
        child_name = local_name(child.tag)
        if child_name == "Document":
            value = direct_first_folder_name(child)
            if value:
                return value
        elif child_name == "Folder":
            return direct_child_text_by_name(child, "name")
    return ""


def extract_cluster_info(path: str | Path, root=None) -> HpdbClusterInfo:
    file_label = normalize_project_label(path)
    root_folder = normalize_project_label(direct_first_folder_name(root)) if root is not None else ""
    source_label = root_folder if root_folder and normalized_folder_name(root_folder) not in {"PROJECT", "DOCUMENT"} else file_label
    code, base_name = split_project_code_and_base(source_label)
    if not code:
        file_code, _file_base = split_project_code_and_base(file_label)
        code = file_code
    if not base_name or normalized_folder_name(base_name) in {"PROJECT", "DOCUMENT"}:
        _file_code, base_name = split_project_code_and_base(file_label)
    return HpdbClusterInfo(
        project_label=source_label or file_label,
        base_cluster_name=base_name or source_label or file_label,
        cluster_code_astri=code,
        root_folder_name=root_folder,
    )


def detect_parent_line_fdt(folder_stack: Sequence[str]) -> tuple[str, int, str] | None:
    line_only_fallback: tuple[str, int, str] | None = None
    for folder_name in reversed(folder_stack):
        folder_text = folder_name or ""
        line_match = LINE_TOKEN_PATTERN.search(folder_text)
        fdt_match = FDT_TOKEN_PATTERN.search(folder_text)
        if line_match and fdt_match:
            line = line_match.group(1).upper()
            fdt = int(fdt_match.group(1))
            return line, fdt, f"LINE {line} - FDT {fdt}"

        line_only_match = LINE_ONLY_PATTERN.search(folder_text)
        if line_only_match and line_only_fallback is None:
            line = line_only_match.group(1).upper()
            line_only_fallback = line, 1, f"LINE {line} - FDT 1"
    return line_only_fallback


def detect_parent_fat_code(folder_stack: Sequence[str], placemark_name: str = "") -> str:
    hp_cover_index = nearest_folder_index(folder_stack, "HP COVER")
    if hp_cover_index is not None and hp_cover_index + 1 < len(folder_stack):
        code = normalize_fat_code(folder_stack[hp_cover_index + 1])
        if code:
            return code
    for value in [placemark_name, *reversed(folder_stack)]:
        code = normalize_fat_code(value)
        if FAT_CODE_PATTERN.fullmatch(code):
            return code
    return ""


def first_point_coordinate(placemark) -> tuple[float | None, float | None]:
    for point in children_by_name(placemark, "Point"):
        coordinate_element = direct_child_by_name(point, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if coordinates:
            lon, lat = coordinates[0]
            if is_valid_coordinate(lat, lon):
                return lat, lon
    return None, None


def is_valid_coordinate(latitude: float | None, longitude: float | None) -> bool:
    if latitude is None or longitude is None:
        return False
    return (
        math.isfinite(float(latitude))
        and math.isfinite(float(longitude))
        and -90 <= float(latitude) <= 90
        and -180 <= float(longitude) <= 180
    )

class HpdbRoadNameHandler(osmium.SimpleHandler):
    def __init__(self, target_crs: str = "EPSG:3857"):
        super().__init__()
        self.transformer = build_transformer(target_crs)
        self.named_roads: list[HpdbNamedRoad] = []

    def way(self, way) -> None:
        name = re.sub(r"\s+", " ", str(tag_value(way.tags, "name") or "").strip())
        if not name:
            return

        coordinates = []
        for node in way.nodes:
            try:
                location = node.location
                if not location.valid():
                    continue
                lon = float(location.lon)
                lat = float(location.lat)
            except Exception:
                continue
            if math.isfinite(lon) and math.isfinite(lat):
                coordinates.append((lon, lat))
        if len(coordinates) < 2:
            return

        projected = project_coordinates(coordinates, self.transformer)
        if not should_export_road(way.tags, projected):
            return

        line = LineString(projected)
        if not line.is_empty and line.length > 0:
            self.named_roads.append(HpdbNamedRoad(name=name, line=line))

class HpdbStreetLookup:
    def __init__(
        self,
        named_roads: Sequence[HpdbNamedRoad],
        max_distance_meters: float = 60,
        target_crs: str = "EPSG:3857",
    ):
        self.named_roads = [road for road in named_roads if road.name and not road.line.is_empty and road.line.length > 0]
        self.max_distance_meters = max(float(max_distance_meters or 0), 0.0)
        self.transformer = build_transformer(target_crs)
        self.lines = [road.line for road in self.named_roads]
        self._tree = STRtree(self.lines) if STRtree is not None and self.lines else None

    @property
    def has_roads(self) -> bool:
        return bool(self.named_roads) and self.max_distance_meters > 0

    def _query_candidates(self, point: ShapelyPoint) -> list[tuple[int, LineString]]:
        if not self.has_roads:
            return []
        if self._tree is None:
            return [
                (index, line)
                for index, line in enumerate(self.lines)
                if point.distance(line) <= self.max_distance_meters
            ]

        queried = self._tree.query(point.buffer(self.max_distance_meters))
        candidates: list[tuple[int, LineString]] = []
        id_to_index = {id(line): index for index, line in enumerate(self.lines)}
        for item in queried:
            if hasattr(item, "geom_type"):
                index = id_to_index.get(id(item))
                if index is not None:
                    candidates.append((index, item))
                continue
            try:
                index = int(item)
                candidates.append((index, self.lines[index]))
            except Exception:
                continue
        return candidates

    def nearest_street(self, latitude: float | None, longitude: float | None) -> str:
        if not self.has_roads or not is_valid_coordinate(latitude, longitude):
            return ""

        point = ShapelyPoint(project_point(float(longitude), float(latitude), self.transformer))
        candidates = self._query_candidates(point)
        if not candidates:
            return ""

        index, line = min(candidates, key=lambda candidate: point.distance(candidate[1]))
        if point.distance(line) > self.max_distance_meters:
            return ""
        return self.named_roads[index].name

def collect_named_osm_roads(
    input_paths: str | Path | Iterable[str | Path],
    target_crs: str = "EPSG:3857",
    pipeline_logger=None,
) -> list[HpdbNamedRoad]:
    paths = [path for path in normalize_input_paths(input_paths) if is_osm_path(path)]
    named_roads: list[HpdbNamedRoad] = []
    if not paths:
        return named_roads

    for path in paths:
        handler = HpdbRoadNameHandler(target_crs=target_crs)
        try:
            handler.apply_file(str(path), locations=True)
        except Exception as exc:
            pipeline_log(pipeline_logger, "WARNING", f"OSM street lookup skipped for {Path(path).name}: {exc}")
            continue
        named_roads.extend(handler.named_roads)
        pipeline_log(
            pipeline_logger,
            "INFO",
            f"Loaded {len(handler.named_roads):,} named OSM road(s) for HPDB street lookup from {Path(path).name}.",
        )
    return named_roads

def _find_server_osm_for_district(district: str) -> Path | None:
    """Find a matching kecamatan OSM/PBF file on the server for the given district name."""
    if not district:
        return None
    try:
        from config import Config
        kecamatan_dir = getattr(Config, "GEOFABRIK_KECAMATAN_DIR", None)
        if not kecamatan_dir or not kecamatan_dir.is_dir():
            return None
        normalized = re.sub(r"[.\s]+", "-", district.strip().lower())
        for path in kecamatan_dir.iterdir():
            if path.suffix.lower() == ".pbf" and normalized in path.stem.lower():
                return path
    except Exception:
        pass
    return None


def build_street_lookup(
    input_paths: str | Path | Iterable[str | Path],
    defaults: dict,
    pipeline_logger=None,
    target_crs: str = "EPSG:3857",
) -> HpdbStreetLookup | None:
    named_roads = collect_named_osm_roads(input_paths, target_crs=target_crs, pipeline_logger=pipeline_logger)
    if not named_roads:
        district = defaults.get("district", "")
        server_osm = _find_server_osm_for_district(district)
        if server_osm:
            pipeline_log(pipeline_logger, "INFO", f"No OSM in upload, trying server OSM for district '{district}': {server_osm.name}")
            named_roads = collect_named_osm_roads([server_osm], target_crs=target_crs, pipeline_logger=pipeline_logger)
    if not named_roads:
        pipeline_log(pipeline_logger, "INFO", "No named OSM road found for HPDB street lookup.")
        return None
    lookup = HpdbStreetLookup(
        named_roads,
        max_distance_meters=float(defaults.get("street_lookup_radius_meters", 150) or 150),
        target_crs=target_crs,
    )
    pipeline_log(
        pipeline_logger,
        "INFO",
        f"HPDB street lookup ready: {len(lookup.named_roads):,} named road(s), radius {lookup.max_distance_meters:g} m.",
    )
    return lookup


def collect_hpdb_placemarks(element, folder_stack: list[str], placemarks: list[HpdbPlacemark]) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Document":
            collect_hpdb_placemarks(child, folder_stack, placemarks)
        elif child_name == "Folder":
            folder_name = direct_child_text_by_name(child, "name") or "Tanpa Nama"
            collect_hpdb_placemarks(child, folder_stack + [folder_name], placemarks)
        elif child_name == "Placemark":
            name = direct_child_text_by_name(child, "name")
            description = direct_child_text_by_name(child, "description")
            latitude, longitude = first_point_coordinate(child)
            placemark = HpdbPlacemark(
                name=name,
                folder_stack=list(folder_stack),
                latitude=latitude,
                longitude=longitude,
                description=description,
                kml_index=len(placemarks),
            )
            placemarks.append(placemark)


def assign_fdt_number_by_sorted_suffix(fdt_placemarks: Sequence[HpdbPlacemark], pipeline_logger=None) -> tuple[dict[int, HpdbFdt], int, int, list[HpdbIssue]]:
    issues: list[HpdbIssue] = []
    grouped: dict[int, list[HpdbPlacemark]] = {}
    skipped_invalid = 0
    for placemark in fdt_placemarks:
        suffix = extract_fdt_suffix_number(placemark.name)
        pipeline_log(pipeline_logger, "INFO", f"Found FDT placemark: {placemark.name}")
        if suffix is None:
            skipped_invalid += 1
            issues.append(
                HpdbIssue(
                    status="Warning",
                    source_folder="FDT",
                    parent_line_fdt="FDT unknown",
                    item=placemark.name or "FDT",
                    message=f"FDT suffix could not be extracted from placemark {placemark.name or '-'}",
                )
            )
            continue
        pipeline_log(pipeline_logger, "INFO", f"Extracted FDT suffix: {suffix:03d}")
        grouped.setdefault(suffix, []).append(placemark)

    if grouped:
        pipeline_log(pipeline_logger, "INFO", "Sorting FDT placemarks by numeric suffix")

    fdt_map: dict[int, HpdbFdt] = {}
    skipped_over_max = 0
    for fdt_number, suffix in enumerate(sorted(grouped), start=1):
        placemark = grouped[suffix][0]
        if fdt_number > HPDB_MAX_FDT:
            skipped_over_max += len(grouped[suffix])
            issues.append(
                HpdbIssue(
                    status="Warning",
                    source_folder="FDT",
                    parent_line_fdt=f"FDT {fdt_number}",
                    item=placemark.name or f"FDT suffix {suffix}",
                    fdt=fdt_number,
                    message=f"FDT number exceeds maximum HPDB support: {HPDB_MAX_FDT}",
                )
            )
            pipeline_log(pipeline_logger, "WARNING", f"FDT number exceeds maximum HPDB support: {HPDB_MAX_FDT}")
            continue

        fdt_code = normalize_fdt_code(placemark.name)
        fdt_map[fdt_number] = HpdbFdt(
            fdt_number=fdt_number,
            name=placemark.name,
            fdt_code=fdt_code,
            suffix_number=suffix,
            latitude=placemark.latitude,
            longitude=placemark.longitude,
            source_folder="FDT",
        )
        pipeline_log(pipeline_logger, "INFO", f"Assigned {placemark.name} as FDT {fdt_number}")

        if len(grouped[suffix]) > 1:
            issues.append(
                HpdbIssue(
                    status="Warning",
                    source_folder="FDT",
                    parent_line_fdt=f"FDT {fdt_number}",
                    item=placemark.name or f"FDT suffix {suffix}",
                    fdt=fdt_number,
                    fdt_code=fdt_code,
                    message=f"Duplicate FDT suffix detected for FDT {fdt_number}",
                )
            )

    return fdt_map, len(grouped) + skipped_invalid, skipped_over_max, issues


def parse_fdt_folder(placemarks: Sequence[HpdbPlacemark], pipeline_logger=None) -> tuple[dict[int, HpdbFdt], int, int, list[HpdbIssue]]:
    fdt_placemarks = [placemark for placemark in placemarks if nearest_folder_index(placemark.folder_stack, "FDT") is not None]
    if not fdt_placemarks:
        return {}, 0, 0, []
    pipeline_log(pipeline_logger, "INFO", "Found FDT folder")
    return assign_fdt_number_by_sorted_suffix(fdt_placemarks, pipeline_logger=pipeline_logger)


def generate_cluster_name_for_fdt(base_cluster_name: str, fdt_number: int, include_fdt_suffix: bool = True) -> str:
    base = normalize_project_label(base_cluster_name)
    return f"{base} - FDT {fdt_number}" if include_fdt_suffix else base

def detect_referenced_line_fdt_numbers(placemarks: Sequence[HpdbPlacemark]) -> set[int]:
    numbers: set[int] = set()
    for placemark in placemarks:
        parent = detect_parent_line_fdt(placemark.folder_stack)
        if parent and parent[1] <= HPDB_MAX_FDT:
            numbers.add(parent[1])
    return numbers


def cluster_code_prefix(cluster_code: str | None) -> str:
    match = re.match(r"\s*([A-Z]+)", str(cluster_code or "").strip().upper())
    return match.group(1) if match else ""


def rollout_period(process_date: date) -> str:
    return f"{process_date.month:02d}{process_date.year % 100:02d}"


def current_process_date() -> date:
    return datetime.now(JAKARTA_TIMEZONE).date()


def generate_roll_out_id(cluster_code_astri: str, process_date: date) -> str:
    prefix = cluster_code_prefix(cluster_code_astri)
    period = rollout_period(process_date)
    return f"{prefix}{period}" if prefix else period


def generate_cluster_id(cluster_code_astri: str, fdt_code: str, process_date: date) -> str:
    prefix = cluster_code_prefix(cluster_code_astri)
    period = rollout_period(process_date)
    resolved_fdt_code = str(fdt_code or "").strip() or "XXX"
    parts = [part for part in (prefix, f"GR{period}", resolved_fdt_code) if part]
    return ".".join(parts)


def extract_rw(base_cluster_name: str, defaults: dict) -> int | str:
    match = RW_PATTERN.search(base_cluster_name or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return defaults.get("Rw", "")


def add_issue(result: HpdbParseResult, issue: HpdbIssue, pipeline_logger=None) -> None:
    result.issues.append(issue)
    if issue.status.lower() in {"warning", "skipped", "unmapped", "error"}:
        result.warnings.append(issue.message)
    severity = "ERROR" if issue.status.lower() == "error" else "WARNING" if issue.status.lower() in {"warning", "skipped", "unmapped"} else "INFO"
    pipeline_log(pipeline_logger, severity, issue.message)


def update_defaults_from_geocode(defaults: dict, geo_result: dict, force: bool = False) -> dict[str, str]:
    applied: dict[str, str] = {}
    locked_fields = set() if force else set(defaults.get("_locked_admin_fields") or [])
    mappings = {
        "province": ("province",),
        "regency_city": ("regency_city", "city"),
        "district": ("district",),
        "sub_district": ("sub_district", "subdistrict"),
        "postal_code": ("postal_code", "postalcode"),
    }
    for source_key, target_keys in mappings.items():
        if source_key in locked_fields or any(target_key in locked_fields for target_key in target_keys):
            continue
        value = str(geo_result.get(source_key) or geo_result.get(target_keys[0]) or "").strip()
        if not value:
            continue
        if source_key == "regency_city":
            value = normalize_city_value(value)
        for target_key in target_keys:
            defaults[target_key] = value
        if source_key == "regency_city":
            defaults["city"] = value
            defaults["area_name"] = value
            defaults["Area"] = value
        applied[source_key] = value
    return applied


def clear_admin_defaults_for_geocode(defaults: dict) -> None:
    for key in ("Area", "area_name", "province", "city", "regency_city", "district", "sub_district", "subdistrict", "postal_code", "postalcode"):
        defaults[key] = ""


def normalize_geocode_admin_fields(geo_result: dict | None) -> dict[str, str]:
    if not geo_result:
        return {}
    return {
        "province": str(geo_result.get("province") or "").strip(),
        "regency_city": normalize_city_value(geo_result.get("regency_city") or geo_result.get("city")),
        "district": str(geo_result.get("district") or "").strip(),
        "sub_district": str(geo_result.get("sub_district") or geo_result.get("subdistrict") or "").strip(),
        "postal_code": str(geo_result.get("postal_code") or geo_result.get("postalcode") or "").strip(),
    }


def geocode_admin_score(geo_result: dict | None) -> int:
    fields = normalize_geocode_admin_fields(geo_result)
    return sum(1 for key in ADMIN_GEOCODE_REQUIRED_KEYS if fields.get(key))


def is_complete_admin_geocode(geo_result: dict | None) -> bool:
    return geocode_admin_score(geo_result) == len(ADMIN_GEOCODE_REQUIRED_KEYS)


def sampled_sequence(items: Sequence, limit: int) -> list:
    values = list(items)
    if len(values) <= limit:
        return values
    if limit <= 1:
        return values[:1]
    indexes = sorted({round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)})
    return [values[index] for index in indexes]


def administrative_geocode_candidates(
    result: HpdbParseResult,
    placemarks: Sequence[HpdbPlacemark],
) -> list[dict]:
    candidates: list[dict] = []
    seen_coordinates: set[tuple[float, float]] = set()

    def add_candidate(source_folder: str, item: str, latitude, longitude, fdt: int | None = None, fdt_code: str = "") -> None:
        if not is_valid_coordinate(latitude, longitude):
            return
        key = (round(float(latitude), 6), round(float(longitude), 6))
        if key in seen_coordinates:
            return
        seen_coordinates.add(key)
        candidates.append(
            {
                "source_folder": source_folder,
                "item": item,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "fdt": fdt,
                "fdt_code": fdt_code,
            }
        )

    for fdt in sorted(result.fdt_map.values(), key=lambda item: item.fdt_number):
        add_candidate("FDT", fdt.name or f"FDT {fdt.fdt_number}", fdt.latitude, fdt.longitude, fdt=fdt.fdt_number, fdt_code=fdt.fdt_code)

    fat_placemarks = [
        placemark
        for placemark in placemarks
        if nearest_folder_index(placemark.folder_stack, "FAT") is not None
        and is_valid_coordinate(placemark.latitude, placemark.longitude)
    ]
    for placemark in sampled_sequence(fat_placemarks, ADMIN_GEOCODE_FAT_CANDIDATE_LIMIT):
        add_candidate("FAT", placemark.name or "FAT", placemark.latitude, placemark.longitude)

    hp_cover_placemarks = [
        placemark
        for placemark in placemarks
        if nearest_folder_index(placemark.folder_stack, "HP COVER") is not None
        and nearest_folder_index(placemark.folder_stack, "HP UNCOVER") is None
        and is_valid_coordinate(placemark.latitude, placemark.longitude)
    ]
    for placemark in sampled_sequence(hp_cover_placemarks, ADMIN_GEOCODE_HP_CANDIDATE_LIMIT):
        add_candidate("HP COVER", placemark.name or "Homepass", placemark.latitude, placemark.longitude)

    return candidates


def apply_administrative_defaults_from_candidates(
    result: HpdbParseResult,
    defaults: dict,
    placemarks: Sequence[HpdbPlacemark],
    pipeline_logger=None,
) -> None:
    candidates = administrative_geocode_candidates(result, placemarks)
    if not candidates:
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder="KML/KMZ",
                item="Admin coordinate",
                message="Auto-fill administrative area skipped: FDT/FAT/HP COVER coordinate not found.",
            ),
            pipeline_logger,
        )
        return

    try:
        from geocode import reverse_geocode_with_fallback
    except Exception as exc:
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder="KML/KMZ",
                item="Admin coordinate",
                message=f"Auto-fill administrative area failed: {exc}",
            ),
            pipeline_logger,
        )
        return

    selected_candidate = None
    selected_geo_result: dict | None = None
    best_candidate = None
    best_geo_result: dict | None = None
    best_score = 0

    for candidate in candidates:
        geo_result = reverse_geocode_with_fallback(candidate["latitude"], candidate["longitude"])
        score = geocode_admin_score(geo_result)
        pipeline_log(
            pipeline_logger,
            "INFO",
            "Auto-fill administrative lookup "
            f"{candidate['source_folder']} {candidate['item']}: {score}/{len(ADMIN_GEOCODE_REQUIRED_KEYS)} field(s).",
        )
        if score > best_score:
            best_score = score
            best_candidate = candidate
            best_geo_result = normalize_geocode_admin_fields(geo_result)
        if is_complete_admin_geocode(geo_result):
            selected_candidate = candidate
            selected_geo_result = normalize_geocode_admin_fields(geo_result)
            break

    if selected_geo_result is None:
        selected_candidate = best_candidate
        selected_geo_result = best_geo_result

    if not selected_candidate or not selected_geo_result:
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder="KML/KMZ",
                item="Admin coordinate",
                message="Auto-fill administrative area failed from FDT/FAT/HP COVER coordinates.",
            ),
            pipeline_logger,
        )
        return

    applied = update_defaults_from_geocode(defaults, selected_geo_result, force=True)
    if not applied:
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder=selected_candidate["source_folder"],
                item=selected_candidate["item"],
                fdt=selected_candidate.get("fdt"),
                fdt_code=selected_candidate.get("fdt_code", ""),
                message="Auto-fill administrative area returned no usable fields.",
            ),
            pipeline_logger,
        )
        return

    result.geocode_success = is_complete_admin_geocode(selected_geo_result)
    result.geocode_source_lat = selected_candidate["latitude"]
    result.geocode_source_lon = selected_candidate["longitude"]
    result.geocode_fields = applied
    if not result.geocode_success:
        missing = [key for key in ADMIN_GEOCODE_REQUIRED_KEYS if not selected_geo_result.get(key)]
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder=selected_candidate["source_folder"],
                item=selected_candidate["item"],
                fdt=selected_candidate.get("fdt"),
                fdt_code=selected_candidate.get("fdt_code", ""),
                message="Auto-fill administrative area incomplete; missing: " + ", ".join(missing),
            ),
            pipeline_logger,
        )
    pipeline_log(
        pipeline_logger,
        "INFO",
        f"Auto-fill administrative area from {selected_candidate['source_folder']} coordinate: "
        + ", ".join(f"{key}={value}" for key, value in applied.items()),
    )


def fdt_for_number(fdt_map: dict[int, HpdbFdt], fdt_number: int, defaults: dict) -> HpdbFdt:
    if fdt_number in fdt_map:
        return fdt_map[fdt_number]
    fallback_code = str(defaults.get("fallback_fdt_code") or "")
    return HpdbFdt(fdt_number=fdt_number, name="", fdt_code=fallback_code)


def build_cluster_rows_per_fdt(
    result: HpdbParseResult,
    referenced_fdt_numbers: set[int],
    defaults: dict,
    process_date: date,
    include_fdt_suffix: bool = True,
    pipeline_logger=None,
) -> list[dict]:
    candidate_numbers = set(result.fdt_map) | {number for number in referenced_fdt_numbers if number <= HPDB_MAX_FDT}
    if not candidate_numbers and referenced_fdt_numbers:
        candidate_numbers = {number for number in referenced_fdt_numbers if number <= HPDB_MAX_FDT}
    rows = []
    for fdt_number in sorted(candidate_numbers):
        if fdt_number > HPDB_MAX_FDT:
            continue
        fdt = fdt_for_number(result.fdt_map, fdt_number, defaults)
        cluster_name = generate_cluster_name_for_fdt(result.cluster_info.base_cluster_name, fdt_number, include_fdt_suffix=include_fdt_suffix)
        pipeline_log(pipeline_logger, "INFO", f"Writing CLUSTER row for FDT {fdt_number}")
        rows.append(
            {
                "cluster_name": cluster_name,
                "fdt_code": fdt.fdt_code,
                "commercial_name": cluster_name,
                "province": normalize_province_value(defaults.get("province", "")),
                "competition": defaults.get("competition", ""),
                "city": normalize_city_value(defaults.get("city", "")),
                "area_name": normalize_area_name_value(defaults.get("area_name", defaults.get("city", ""))),
                "rfs_date": process_date,

                "district": defaults.get("district", ""),
                "acquisition_class": defaults.get("acquisition_class", ""),
                "acquisition_tier": defaults.get("acquisition_tier", ""),
                "postal_code": defaults.get("postal_code", ""),
                "sub_district": defaults.get("sub_district", ""),
                "olt_code": defaults.get("olt_code", ""),
                "olthostname": defaults.get("olthostname", ""),
                "cluster_type": defaults.get("cluster_type", ""),
                "complex": defaults.get("complex", ""),
                "project_name": defaults.get("project_name", ""),
                "project_id": defaults.get("project_id", ""),
                "latitude_cluster": fdt.latitude,
                "longitude_cluster": fdt.longitude,
                "cluster_code_astri": result.cluster_info.cluster_code_astri,
                "_fdt": fdt_number,
            }
        )
    return rows


def extract_fat_rows(
    result: HpdbParseResult,
    placemarks: Sequence[HpdbPlacemark],
    defaults: dict,
    referenced_fdt_numbers: set[int],
    include_fdt_suffix: bool = True,
    pipeline_logger=None,
) -> list[dict]:
    rows = []
    for placemark in placemarks:
        if nearest_folder_index(placemark.folder_stack, "FAT") is None:
            continue
        parent = detect_parent_line_fdt(placemark.folder_stack)
        if not parent:
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="FAT",
                    item=placemark.name or "FAT",
                    message="Parent LINE/FDT not detected for FAT placemark",
                ),
                pipeline_logger,
            )
            continue
        line, fdt_number, parent_label = parent
        if fdt_number > HPDB_MAX_FDT:
            result.fdt_skipped_over_max += 1
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="FAT",
                    parent_line_fdt=parent_label,
                    item=placemark.name or "FAT",
                    fdt=fdt_number,
                    message=f"FDT number exceeds maximum HPDB support: {HPDB_MAX_FDT}",
                ),
                pipeline_logger,
            )
            continue
        if not is_valid_coordinate(placemark.latitude, placemark.longitude):
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="FAT",
                    parent_line_fdt=parent_label,
                    item=placemark.name or "FAT",
                    fdt=fdt_number,
                    message="FAT placemark has invalid coordinate",
                ),
                pipeline_logger,
            )
            continue
        referenced_fdt_numbers.add(fdt_number)
        fdt = fdt_for_number(result.fdt_map, fdt_number, defaults)
        cluster_name = generate_cluster_name_for_fdt(result.cluster_info.base_cluster_name, fdt_number, include_fdt_suffix=include_fdt_suffix)
        fat_code = normalize_fat_code(placemark.name)
        pipeline_log(pipeline_logger, "INFO", f"Found FAT folder under {parent_label}")
        rows.append(
            {
                "Area": normalize_area_name_value(defaults.get("area_name", defaults.get("Area", ""))),
                "Clustername": cluster_name,
                "FAT Code": fat_code,
                "FDT Code": fdt.fdt_code,
                "Port Count": defaults.get("port_count", 16),
                "olthostname": defaults.get("olthostname", ""),
                "FAT Latitude": placemark.latitude,
                "FAT Longitude": placemark.longitude,
                "_fdt": fdt_number,
                "_line": line,
                "_status": "Mapped",
            }
        )
    rows.sort(key=lambda row: (int(row.get("_fdt") or 0), str(row.get("_line") or ""), str(row.get("FAT Code") or "")))
    return rows


def extract_hp_cover_rows(
    result: HpdbParseResult,
    placemarks: Sequence[HpdbPlacemark],
    defaults: dict,
    referenced_fdt_numbers: set[int],
    process_date: date,
    street_lookup: HpdbStreetLookup | None = None,
    include_fdt_suffix: bool = True,
    pipeline_logger=None,
) -> list[dict]:
    rows = []
    grouped_counts: dict[tuple[int, str], int] = {}
    for placemark in placemarks:
        if nearest_folder_index(placemark.folder_stack, "HP COVER") is None:
            continue
        if nearest_folder_index(placemark.folder_stack, "HP UNCOVER") is not None:
            continue
        parent = detect_parent_line_fdt(placemark.folder_stack)
        if not parent:
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="HP COVER",
                    item=placemark.name or "Homepass",
                    message="Parent LINE/FDT not detected for homepass",
                ),
                pipeline_logger,
            )
            continue
        line, fdt_number, parent_label = parent
        if fdt_number > HPDB_MAX_FDT:
            result.fdt_skipped_over_max += 1
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="HP COVER",
                    parent_line_fdt=parent_label,
                    item=placemark.name or "Homepass",
                    fdt=fdt_number,
                    message=f"FDT number exceeds maximum HPDB support: {HPDB_MAX_FDT}",
                ),
                pipeline_logger,
            )
            continue
        fat_code = detect_parent_fat_code(placemark.folder_stack, placemark.name)
        if not fat_code:
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="HP COVER",
                    parent_line_fdt=parent_label,
                    item=placemark.name or "Homepass",
                    fdt=fdt_number,
                    message="Parent FAT code not detected for homepass",
                ),
                pipeline_logger,
            )
            continue
        if not is_valid_coordinate(placemark.latitude, placemark.longitude):
            add_issue(
                result,
                HpdbIssue(
                    status="Warning",
                    source_folder="HP COVER",
                    parent_line_fdt=parent_label,
                    item=placemark.name or "Homepass",
                    fdt=fdt_number,
                    fat_code=fat_code,
                    message="HOMEPASS placemark has invalid coordinate",
                ),
                pipeline_logger,
            )
            continue

        referenced_fdt_numbers.add(fdt_number)
        fdt = fdt_for_number(result.fdt_map, fdt_number, defaults)
        cluster_name = generate_cluster_name_for_fdt(result.cluster_info.base_cluster_name, fdt_number, include_fdt_suffix=include_fdt_suffix)
        parsed_block, homenumber = parse_homepass_block_and_number(placemark.name)
        roll_out_id = generate_roll_out_id(result.cluster_info.cluster_code_astri, process_date)
        cluster_id = generate_cluster_id(result.cluster_info.cluster_code_astri, fdt.fdt_code, process_date)
        street = ""
        if street_lookup and street_lookup.has_roads:
            street = street_lookup.nearest_street(placemark.latitude, placemark.longitude)
            if street:
                result.street_matched += 1
            else:
                result.street_unmatched += 1
        if not street:
            street = defaults.get("street", "")
        grouped_counts[(fdt_number, fat_code)] = grouped_counts.get((fdt_number, fat_code), 0) + 1
        rows.append(
            {
                "postalcode": defaults.get("postalcode", defaults.get("postal_code", "")),
                "Roll_Out_ID": roll_out_id,
                "acquisition_class": defaults.get("acquisition_class", ""),
                "acquisition_tier": defaults.get("acquisition_tier", ""),
                "competition": defaults.get("competition", ""),
                "project_id": defaults.get("project_id", ""),
                "projectName": defaults.get("project_name", ""),
                "Complex_name": cluster_name,
                "ClusterId": cluster_id,
                "Clustername": cluster_name,
                "Commercial_name": cluster_name,
                "Rw": extract_rw(result.cluster_info.base_cluster_name, defaults),
                "Rt": defaults.get("Rt", 0),
                "address_prefix": defaults.get("address_prefix", ""),
                "street": street,
                "address_suffix": defaults.get("address_suffix", ""),
                "sub_address_prefix": defaults.get("sub_address_prefix", ""),
                "sub_address": defaults.get("sub_address", ""),
                "sub_address_suffix": defaults.get("sub_address_suffix", ""),
                "block": parsed_block or defaults.get("block", ""),
                "homenumber": homenumber,
                "dwelling_type": defaults.get("dwelling_type", ""),
                "building_type": defaults.get("building_type", ""),
                "building_property_name": defaults.get("building_property_name", ""),
                "building_property_location": defaults.get("building_property_location", ""),
                "floor": defaults.get("floor", ""),
                "unit": defaults.get("unit", ""),
                "homepass_source": defaults.get("homepass_source", ""),
                "oltcode": defaults.get("olt_code", ""),
                "olthostname": defaults.get("olthostname", ""),
                "fdtcode": fdt.fdt_code,
                "fatcode": fat_code,
                "RFSDate": process_date,
                "Latitude_homepass": placemark.latitude,
                "Longitude_homepass": placemark.longitude,
                "vendor_homepass_id": defaults.get("vendor_homepass_id", ""),
                "residential_service_ready": defaults.get("residential_service_ready", True),
                "sme_service_ready": defaults.get("sme_service_ready", False),
                "enterprise_service_ready": defaults.get("enterprise_service_ready", False),
                "installation": defaults.get("installation", ""),
                "availability": defaults.get("availability", True),
                "classification": defaults.get("classification", ""),
                "business_case": defaults.get("business_case", ""),
                "network_presence": defaults.get("network_presence", ""),
                "wallplate_installation": defaults.get("wallplate_installation", False),
                "publish": defaults.get("publish", True),
                "homepass_smt_id": defaults.get("homepass_smt_id", ""),
                "_fdt": fdt_number,
                "_line": line,
                "_homepass_name": homenumber,
                "_status": "Mapped",
                "_kml_index": placemark.kml_index,
            }
        )

    for (fdt_number, fat_code), count in sorted(grouped_counts.items()):
        pipeline_log(pipeline_logger, "INFO", f"Added HOMEPASS rows under FDT {fdt_number} / {fat_code}: {count:,}")
    if street_lookup and street_lookup.has_roads:
        pipeline_log(
            pipeline_logger,
            "INFO",
            f"Street name matched for {result.street_matched:,} HOMEPASS row(s), unmatched {result.street_unmatched:,}.",
        )
    rows.sort(
        key=lambda row: (
            int(row.get("_fdt") or 0),
            str(row.get("_line") or ""),
            str(row.get("fatcode") or ""),
            int(row.get("_kml_index") or 0),
        )
    )
    previous_street = ""
    street_filled_from_previous = 0
    for row in rows:
        street = str(row.get("street") or "").strip()
        if street:
            previous_street = street
            continue
        if previous_street:
            row["street"] = previous_street
            street_filled_from_previous += 1
    if street_filled_from_previous:
        pipeline_log(
            pipeline_logger,
            "INFO",
            f"Street name filled from previous HOMEPASS row for {street_filled_from_previous:,} row(s).",
        )
    return rows


def detect_and_ignore_hp_uncover_rows(result: HpdbParseResult, placemarks: Sequence[HpdbPlacemark], pipeline_logger=None) -> int:
    ignored = sum(1 for placemark in placemarks if nearest_folder_index(placemark.folder_stack, "HP UNCOVER") is not None)
    if ignored:
        result.issues.append(
            HpdbIssue(
                status="Info",
                source_folder="HP UNCOVER",
                item="HP UNCOVER",
                message="HP UNCOVER ignored for HPDB export.",
            )
        )
        pipeline_log(pipeline_logger, "INFO", "HP UNCOVER ignored for HPDB export.")
        pipeline_log(pipeline_logger, "INFO", f"Ignored {ignored:,} HP UNCOVER rows")
    result.hp_uncover_ignored = ignored
    return ignored


def extract_homepass_rows(
    result: HpdbParseResult,
    placemarks: Sequence[HpdbPlacemark],
    defaults: dict,
    referenced_fdt_numbers: set[int],
    process_date: date,
    street_lookup: HpdbStreetLookup | None = None,
    include_fdt_suffix: bool = True,
    pipeline_logger=None,
) -> list[dict]:
    return extract_hp_cover_rows(
        result,
        placemarks,
        defaults,
        referenced_fdt_numbers,
        process_date,
        street_lookup=street_lookup,
        include_fdt_suffix=include_fdt_suffix,
        pipeline_logger=pipeline_logger,
    )


def parse_hpdb_from_kmz(
    input_paths: str | Path | Iterable[str | Path],
    defaults: dict | None = None,
    street_lookup: HpdbStreetLookup | None = None,
    auto_geocode: bool = False,
    pipeline_logger=None,
    progress_callback=None,
    process_date: date | None = None,
    cluster_name_override: str | None = None,
) -> HpdbParseResult:
    paths = [path for path in normalize_input_paths(input_paths) if is_kml_path(path)]
    if not paths:
        raise ValueError("KML/KMZ file is required for HPDB export.")

    defaults = normalize_hpdb_city_defaults_in_place(defaults if defaults is not None else dict(HPDB_DEFAULTS))
    process_date = process_date or current_process_date()
    pipeline_progress(progress_callback, pipeline_logger, 16, "Parsing KMZ for HPDB")
    pipeline_log(pipeline_logger, "INFO", "Parsing KMZ for HPDB")

    all_placemarks: list[HpdbPlacemark] = []
    first_root = None
    for path in paths:
        root = read_kml_root(path)
        if first_root is None:
            first_root = root
        pipeline_log(pipeline_logger, "INFO", f"Reading KML/KMZ for HPDB: {Path(path).name}")
        collect_hpdb_placemarks(root, [], all_placemarks)

    cluster_info = extract_cluster_info(paths[0], first_root)
    override_name = normalize_project_label(cluster_name_override)
    if override_name:
        cluster_info = HpdbClusterInfo(
            project_label=cluster_info.project_label,
            base_cluster_name=override_name,
            cluster_code_astri=cluster_info.cluster_code_astri,
            root_folder_name=cluster_info.root_folder_name,
        )
    pipeline_log(pipeline_logger, "INFO", f"Found root cluster: {cluster_info.base_cluster_name}")
    result = HpdbParseResult(cluster_info=cluster_info, processed_files=len(paths))

    fdt_map, fdt_detected_total, fdt_skipped, fdt_issues = parse_fdt_folder(all_placemarks, pipeline_logger=pipeline_logger)
    result.fdt_map = fdt_map
    result.fdt_detected_total = fdt_detected_total
    result.fdt_skipped_over_max += fdt_skipped
    for issue in fdt_issues:
        add_issue(result, issue, pipeline_logger=None)
    if not fdt_map:
        add_issue(
            result,
            HpdbIssue(
                status="Warning",
                source_folder="FDT",
                item="FDT folder",
                message="FDT folder not found, using parent LINE/FDT fallback.",
            ),
            pipeline_logger,
        )

    locked_admin_fields = set(defaults.get("_locked_admin_fields") or [])
    popup_admin_locked = bool(locked_admin_fields.intersection({
        "province", "city", "regency_city", "district",
        "sub_district", "subdistrict", "postal_code", "postalcode",
    }))
    if auto_geocode and not popup_admin_locked:
        clear_admin_defaults_for_geocode(defaults)
        apply_administrative_defaults_from_candidates(result, defaults, all_placemarks, pipeline_logger=pipeline_logger)
    elif auto_geocode and popup_admin_locked:
        pipeline_log(pipeline_logger, "INFO", "Administrative HPDB/ASTRI values locked from Pilih Wilayah popup; OSM reverse-geocode skipped.")

    cluster_name_fdt_numbers = set(result.fdt_map) | detect_referenced_line_fdt_numbers(all_placemarks)
    include_fdt_suffix = len(cluster_name_fdt_numbers) > 1
    if not include_fdt_suffix:
        pipeline_log(pipeline_logger, "INFO", "Single FDT detected; cluster name suffix FDT 1 will be omitted.")

    referenced_fdt_numbers: set[int] = set(result.fdt_map)
    pipeline_progress(progress_callback, pipeline_logger, 38, "Extracting FAT rows")
    result.fat_rows = extract_fat_rows(
        result,
        all_placemarks,
        defaults,
        referenced_fdt_numbers,
        include_fdt_suffix=include_fdt_suffix,
        pipeline_logger=pipeline_logger,
    )
    pipeline_progress(progress_callback, pipeline_logger, 56, "Extracting HP COVER rows")
    result.homepass_rows = extract_homepass_rows(
        result,
        all_placemarks,
        defaults,
        referenced_fdt_numbers,
        process_date,
        street_lookup=street_lookup,
        include_fdt_suffix=include_fdt_suffix,
        pipeline_logger=pipeline_logger,
    )
    detect_and_ignore_hp_uncover_rows(result, all_placemarks, pipeline_logger=pipeline_logger)
    pipeline_progress(progress_callback, pipeline_logger, 70, "Building CLUSTER rows")
    result.cluster_rows = build_cluster_rows_per_fdt(
        result,
        referenced_fdt_numbers,
        defaults,
        process_date,
        include_fdt_suffix=include_fdt_suffix,
        pipeline_logger=pipeline_logger,
    )
    pipeline_log(pipeline_logger, "INFO", f"HP COVER exported rows: {len(result.homepass_rows):,}")
    pipeline_log(pipeline_logger, "INFO", f"HP UNCOVER ignored rows: {result.hp_uncover_ignored:,}")
    return result


def load_hpdb_template(template_path: str | Path, output_path: str | Path, pipeline_logger=None):
    template_path = Path(template_path)
    output_path = Path(output_path)
    if not template_path.exists():
        raise FileNotFoundError("ASTRI template not found on server.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, output_path)
    pipeline_log(pipeline_logger, "SUCCESS", f"ASTRI template loaded: {template_path.name}")
    workbook = load_workbook(output_path)
    for sheet_name in HPDB_REQUIRED_SHEETS:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"Required ASTRI sheet not found: {sheet_name}")
        pipeline_log(pipeline_logger, "SUCCESS", f"Sheet {sheet_name} found")
    return workbook


def clear_template_instruction_rows(worksheet) -> None:
    if worksheet.max_row <= 1:
        return
    # The output must be clean: header row only, data starts at row 2.
    worksheet.delete_rows(2, worksheet.max_row - 1)


def header_column_map(worksheet) -> dict[str, int]:
    headers = {}
    for cell in worksheet[1]:
        if cell.value is None:
            continue
        headers[str(cell.value).strip()] = int(cell.column)
    return headers


def write_rows_by_header(worksheet, rows: Sequence[dict], date_headers: set[str] | None = None) -> None:
    headers = header_column_map(worksheet)
    date_headers = date_headers or set()
    clear_template_instruction_rows(worksheet)
    for row_index, row_data in enumerate(rows, start=2):
        for key, value in row_data.items():
            if key.startswith("_") or key not in headers:
                continue
            if key.lower() in {"city", "regency_city"}:
                value = normalize_city_value(value)
            elif key.lower() == "province":
                value = normalize_province_value(value)
            elif key.lower() == "block":
                value = str(value or "").upper()
            elif key.lower() == "homenumber":
                value = str(value or "").upper()
            elif key.lower() == "street":
                value = normalize_street_value(value)
            cell = worksheet.cell(row=row_index, column=headers[key])
            cell.value = value
            if key in date_headers and value:
                cell.number_format = "dd/mm/yyyy"


def write_cluster_sheet(workbook, rows: Sequence[dict]) -> None:
    write_rows_by_header(workbook[HPDB_CLUSTER_SHEET_NAME], rows, date_headers={"rfs_date"})


def write_fat_sheet(workbook, rows: Sequence[dict]) -> None:
    write_rows_by_header(workbook[HPDB_FAT_SHEET_NAME], rows)


def write_homepass_sheet(workbook, rows: Sequence[dict]) -> None:
    write_rows_by_header(workbook[HPDB_HOMEPASS_SHEET_NAME], rows, date_headers={"RFSDate"})


def preview_window(rows: Sequence[dict], limit: int | None = None) -> Sequence[dict]:
    return rows if limit is None else rows[:limit]


def preview_cluster_rows(rows: Sequence[dict], limit: int | None = None) -> list[dict]:
    return [
        {
            "FDT": row.get("_fdt"),
            "FDT Code": row.get("fdt_code", ""),
            "Clustername": row.get("cluster_name", ""),
            "Latitude": row.get("latitude_cluster"),
            "Longitude": row.get("longitude_cluster"),
            "Status": "Mapped",
        }
        for row in preview_window(rows, limit)
    ]


def preview_fat_rows(rows: Sequence[dict], limit: int | None = None) -> list[dict]:
    return [
        {
            "FDT": row.get("_fdt"),
            "FDT Code": row.get("FDT Code", ""),
            "Clustername": row.get("Clustername", ""),
            "FAT Code": row.get("FAT Code", ""),
            "FAT Latitude": row.get("FAT Latitude"),
            "FAT Longitude": row.get("FAT Longitude"),
            "Status": row.get("_status", "Mapped"),
        }
        for row in preview_window(rows, limit)
    ]


def preview_homepass_rows(rows: Sequence[dict], limit: int | None = None) -> list[dict]:
    return [
        {
            "FDT": row.get("_fdt"),
            "FDT Code": row.get("fdtcode", ""),
            "Clustername": row.get("Clustername", ""),
            "FAT Code": row.get("fatcode", ""),
            "Homepass Name": row.get("_homepass_name") or row.get("homenumber", ""),
            "Latitude_homepass": row.get("Latitude_homepass"),
            "Longitude_homepass": row.get("Longitude_homepass"),
            "Status": row.get("_status", "Mapped"),
        }
        for row in preview_window(rows, limit)
    ]


def build_hpdb_preview(result: HpdbParseResult, limit: int | None = None) -> dict:
    return {
        "cluster": preview_cluster_rows(result.cluster_rows, limit=limit),
        "fat": preview_fat_rows(result.fat_rows, limit=limit),
        "homepass": preview_homepass_rows(result.homepass_rows, limit=limit),
        "limit": limit,
        "totals": {
            "cluster": len(result.cluster_rows),
            "fat": len(result.fat_rows),
            "homepass": len(result.homepass_rows),
        },
    }


def generate_astri_excel(
    input_paths: str | Path | Iterable[str | Path],
    output_path: str | Path,
    template_path: str | Path,
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
        template_path=template_path,
        output_path=output_path,
        defaults_path=defaults_path,
    )
    defaults = normalize_hpdb_defaults_for_network(load_hpdb_defaults(defaults_path), resolved_network_type)
    if defaults_override:
        defaults.update(defaults_override)
    pipeline_log(pipeline_logger, "INFO", "ASTRI export started")
    pipeline_log(pipeline_logger, "INFO", f"ASTRI network type: {resolved_network_type}")
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
    pipeline_progress(progress_callback, pipeline_logger, 78, "Loading ASTRI template")
    workbook = load_hpdb_template(template_path, output_path, pipeline_logger=pipeline_logger)
    pipeline_progress(progress_callback, pipeline_logger, 86, "Writing ASTRI Excel")
    pipeline_log(pipeline_logger, "INFO", "Writing ASTRI Excel")
    write_cluster_sheet(workbook, parse_result.cluster_rows)
    write_fat_sheet(workbook, parse_result.fat_rows)
    write_homepass_sheet(workbook, parse_result.homepass_rows)
    replacements = normalize_workbook_network_labels(workbook, resolved_network_type)
    if replacements:
        pipeline_log(pipeline_logger, "INFO", f"Normalized {replacements:,} ASTRI template AE label(s) to UG.")
    workbook.save(output_path)
    pipeline_progress(progress_callback, pipeline_logger, 96, "Preparing ASTRI download")
    pipeline_log(pipeline_logger, "SUCCESS", "ASTRI Excel generated successfully")
    summary = {
        **parse_result.summary,
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
def export_hpdb_excel(*args, **kwargs) -> dict:
    return generate_hpdb_excel(*args, **kwargs)

def generate_hpdb_excel(*args, **kwargs) -> dict:
    return generate_astri_excel(*args, **kwargs)

def export_astri_excel(*args, **kwargs) -> dict:
    return generate_astri_excel(*args, **kwargs)

