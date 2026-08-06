import json
import logging
import math
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

import ezdxf
import osmium
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Matrix44
from pyproj import Geod, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, MultiPoint, Point as ShapelyPoint, Polygon
from shapely.ops import nearest_points, unary_union

try:
    from shapely.ops import voronoi_diagram as _shapely_voronoi_diagram
except Exception:  # pragma: no cover - depends on Shapely build/GEOS version.
    _shapely_voronoi_diagram = None

from config import Config
from eva_ai_safe import suggest_dxf_parcel_orientation, suggest_dxf_visual_parcel_layout, suggest_dxf_asset_layout

try:
    from shapely.strtree import STRtree
except Exception:  # pragma: no cover - optional acceleration depends on Shapely build.
    STRtree = None


LOGGER = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")
RULES_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "rules.json"


def load_rules_config() -> dict:
    if not RULES_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def rule_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def dxf_rule_map(key: str, default: dict) -> dict:
    value = (load_rules_config().get("dxf") or {}).get(key)
    if isinstance(value, dict):
        return {rule_text(k).upper(): v for k, v in value.items() if rule_text(k)}
    return dict(default)


DEFAULT_DXF_LAYERS = {
    "ROAD": 8,
    "BUILDING": 3,
    "WATERWAY": 5,
    "RAILWAY": 2,
    "WATER_AREA": 4,
    "LANDUSE": 30,
    "LEISURE": 94,
    "KML": 7,
    "POLE_ID": 1,
    "HP_UNCOVER": 8,
    "OTHER": 8,
}
DXF_LAYERS = {key: int(value) for key, value in dxf_rule_map("layers", DEFAULT_DXF_LAYERS).items()}

def pipeline_log(logger, severity: str, message: str) -> None:
    if logger is None:
        return
    method_name = "warn" if severity == "warning" else severity
    method = getattr(logger, method_name, None)
    if callable(method):
        method(message)

def pipeline_progress(logger, progress: int, message: str) -> None:
    if logger is None:
        return
    method = getattr(logger, "progress", None)
    if callable(method):
        method(progress, message)

def format_stage_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, second = divmod(int(round(seconds)), 60)
    hour, minute = divmod(minutes, 60)
    if hour:
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    return f"{minute:02d}:{second:02d}"

def progress_log_interval(total: int, target_updates: int = 20, min_interval: int = 25) -> int:
    if total <= 0:
        return min_interval
    return max(min_interval, int(math.ceil(total / max(target_updates, 1))))

REQUIRED_KML_FOLDERS = [
    "BOUNDARY FAT",
    "FAT",
    "HP COVER",
    "HP UNCOVER",
    "EXISTING POLE EMR 7-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "NEW POLE 7-2.5",
    "NEW POLE 7-3",
    "NEW POLE 7-4",
    "NEW POLE 9-4",
    "DISTRIBUTION CABLE",
    "SLACK HANGER",
    "SLING WIRE",
    "SUBDUCT 32/28",
    "SUBDUCT 40/34",
    "TALI PANCING",
    "NEW HH 20X20X20",
    "NEW HH 40X40X60",
    "NEW HH 80X80X130",
    "TWEY",
]
OPTIONAL_KML_FOLDERS = [
    "OLT",
    "FDT",
    "CABLE",
    "JOINT CLOSURE",
    "NEW POLE 7-5",
    "NEW POLE 9-5",
    "EXISTING POLE EMR 7-5",
    "EXISTING POLE EMR 9-5",
]
STANDARD_KML_FOLDERS = REQUIRED_KML_FOLDERS + OPTIONAL_KML_FOLDERS
STANDARD_KML_FOLDER_SET = set(STANDARD_KML_FOLDERS)


def _normalized_folder_key(folder_name: str) -> str:
    normalized = re.sub(r"\s+", " ", str(folder_name or "").strip()).upper()
    normalized = re.sub(r"(?<=\d)\s*[,]\s*(?=\d)", ".", normalized)
    normalized = re.sub(r"(?<=\d)\s*[.]\s*(?=\d)", ".", normalized)
    return normalized


STANDARD_KML_FOLDER_ALIASES = {
    _normalized_folder_key(canonical): canonical
    for canonical in STANDARD_KML_FOLDERS
}
STANDARD_KML_FOLDER_ALIASES.update(
    {
        _normalized_folder_key(alias): canonical
        for alias, canonical in {
            'NEW POLE 7M 2.5"': "NEW POLE 7-2.5",
            "NEW POLE 7M 2.5": "NEW POLE 7-2.5",
            "NEW POLE 7 M 2.5": "NEW POLE 7-2.5",
            'NEW POLE 7M 3"': "NEW POLE 7-3",
            "NEW POLE 7M 3": "NEW POLE 7-3",
            'NEW POLE 7M 4"': "NEW POLE 7-4",
            "NEW POLE 7M 4": "NEW POLE 7-4",
            'NEW POLE 9M 4"': "NEW POLE 9-4",
            "NEW POLE 9M 4": "NEW POLE 9-4",
            'NEW POLE 7M 5"': "NEW POLE 7-5",
            "NEW POLE 7M 5": "NEW POLE 7-5",
            'NEW POLE 9M 5"': "NEW POLE 9-5",
            "NEW POLE 9M 5": "NEW POLE 9-5",
            "TALI PANICNG": "TALI PANCING",
            "NEW HH 20 X 20 X 20": "NEW HH 20X20X20",
            "NEW HH 40 X 40 X 60": "NEW HH 40X40X60",
            "NEW HH 80 X 80 X 130": "NEW HH 80X80X130",
            "T-WAY": "TWEY",
            "T-WEY": "TWEY",
            "FAT AREA": "BOUNDARY FAT",
            "CABLE": "CABLE",
            "JOINT CLOSURE": "JOINT CLOSURE",
        }.items()
    }
)
ROAD_CLIP_BOUNDARY_FOLDER = "BOUNDARY CLUSTER"
KML_ICON_SIZE_METERS = 8.0
KML_LABEL_HEIGHT_METERS = 5.0
KML_ICON_DEFAULT_TRUE_COLOR = 0xFFFFFF
KML_POLE_ICON_SIZE_METERS = 4.0
KML_POLE_ID_HEIGHT_METERS = 0.92
KML_POLE_ID_GAP_METERS = 0.35
KML_POLE_ID_REFERENCE_SEARCH_RADIUS_METERS = 48.0
KML_POLE_INNER_TEXT_HEIGHT_METERS = 0.98
KML_POLE_TEXT_WIDTH_FACTOR = 0.58
KML_HOME_NUMBER_LAYER = "HOME_NUMBER"
KML_HOME_NUMBER_BOX_LAYER = "Basic_Map"
KML_HOME_NUMBER_BOX_COLOR = DXF_LAYERS["ROAD"]
KML_HOME_NUMBER_COLOR = 6

KML_UNCOVER_LAYER = "HP_UNCOVER"
KML_UNCOVER_BOX_LAYER = "HP_UNCOVER_BOX"
KML_UNCOVER_BOX_COLOR = DXF_LAYERS["ROAD"]
KML_UNCOVER_COLOR = DXF_LAYERS["ROAD"]
KML_HOME_NUMBER_HEIGHT_METERS = 0.86
KML_HOME_NUMBER_TEXT_WIDTH_FACTOR = 0.72
KML_HOME_NUMBER_PADDING_X_METERS = 0.7
KML_HOME_NUMBER_PADDING_Y_METERS = 0.62
KML_HOME_NUMBER_MIN_WIDTH_METERS = 4.6
KML_HOME_NUMBER_MAX_WIDTH_METERS = 9.0
KML_HOME_NUMBER_MIN_HEIGHT_METERS = 2.5
KML_HOME_NUMBER_MAX_HEIGHT_METERS = 3.2
KML_HOME_NUMBER_ROAD_CLEARANCE_METERS = 0.45
KML_HOME_NUMBER_BOX_CLEARANCE_METERS = 0.28
KML_HOME_NUMBER_ROTATION_SEARCH_RADIUS_METERS = 42.0
KML_HOME_NUMBER_FAT_AREA_ROTATION_BUCKET_DEGREES = 15.0
KML_HOME_NUMBER_STACK_GAP_METERS = 0.05
KML_HOME_NUMBER_TEXT_CLEARANCE_METERS = 0.16
# Parcel-tessellation mode for HP COVER. Instead of independent fixed-ratio
# boxes (which look scattered and can overlap), HP COVER points are turned
# into land-parcel cells via a Voronoi diagram clipped to the surrounding
# block (points hull minus roads). This yields a neat, hand-drawn cadastral
# look: cells share edges, vary in size, never overlap, never cross roads.
KML_HOME_PARCEL_ENABLED = True
# How far out from each point the block extends before roads are subtracted.
KML_HOME_PARCEL_BLOCK_PADDING_METERS = 14.0
# Roads are subtracted with this extra clearance so parcels never touch the
# road outline.
KML_HOME_PARCEL_ROAD_CLEARANCE_METERS = 2.0
# A parcel cell is capped to this distance from its own point, so an isolated
# HP point doesn't claim a huge cell stretching across an empty block.
KML_HOME_PARCEL_MAX_RADIUS_METERS = 22.0
# Parcels smaller than this are dropped (degenerate slivers).
KML_HOME_PARCEL_MIN_AREA_SQUARE_METERS = 1.5
# Each parcel is inset by this much before drawing so neighbouring cells read
# as separate parcels with a thin gap rather than a single shared line, and so
# simplify() can never make adjacent cells overlap.
KML_HOME_PARCEL_INSET_METERS = 0.18
# FAT area parcels are clipped to a slightly enlarged version of the FAT
# polygon so they overflow the boundary line by this amount, giving the
# hand-drawn cadastral look where parcel boxes extend a bit past the dashed
# FAT boundary ??? following its contour.
KML_HOME_PARCEL_FAT_OVERFLOW_METERS = 1.0
KML_HOME_NUMBER_OCCUPANCY_SEARCH_RADIUS_METERS = 72.0
KML_DISTRIBUTION_CABLE_WIDTH = 0.10
KML_SLING_WIRE_LAYER = "SLING_WIRE"
KML_SUBDUCT_32_LAYER = "SUBDUCT_32_28"
KML_SUBDUCT_40_LAYER = "SUBDUCT_40_34"
KML_TALI_PANCING_LAYER = "TALI_PANCING"
KML_SUBDUCT_32_WIDTH = 0.16
KML_SUBDUCT_40_WIDTH = 0.14
KML_TALI_PANCING_WIDTH = KML_DISTRIBUTION_CABLE_WIDTH
KML_CABLE_LINEWEIGHT = 100
KML_SUBDUCT_32_LINEWEIGHT = 120
KML_SUBDUCT_40_LINEWEIGHT = 110
KML_TALI_PANCING_LINEWEIGHT = KML_CABLE_LINEWEIGHT
KML_DISTANCE_LABEL_LAYER = "JARAK"
KML_DISTANCE_LABEL_COLOR = 161
KML_DISTANCE_LABEL_HEIGHT_METERS = 1.45
KML_DISTANCE_LABEL_OFFSET_METERS = 1.35
KML_DISTANCE_LABEL_MIN_METERS = 5.0
KML_DISTANCE_LABEL_DUPLICATE_RADIUS_METERS = 10.0
DISTRIBUTION_CABLE_CORE_LAYER_RULES = (
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?24\s*C(?:\s*/\s*2\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "FO_24_CORE"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?36\s*C(?:\s*/\s*3\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "FO_26_CORE"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?48\s*C(?:\s*/\s*4\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "FO_48_CORE"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?96\s*C\s*/\s*8\s*T(?![A-Z0-9])", re.IGNORECASE), "FO_96_CORE"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?144\s*C\s*/\s*12\s*T(?![A-Z0-9])", re.IGNORECASE), "FO_144_CORE"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?288\s*C\s*/\s*24\s*T(?![A-Z0-9])", re.IGNORECASE), "FO_288_CORE"),
)
KML_FAT_LAYER = "FAT"
KML_FAT_AREA_LAYER = "FAT_AREA"
KML_FAT_AREA_COLOR = 134
KML_FAT_AREA_LINETYPE = "PHANTOM2"
KML_FAT_BLOCK_NAME = "FAT_ICON"
KML_FAT_ATTR_TAG = "FAT_CODE"
KML_FAT_ICON_WIDTH_METERS = 3.2
KML_FAT_ICON_HEIGHT_METERS = 2.2
KML_FAT_ICON_INNER_INSET_METERS = 0.14
KML_FAT_TO_POLE_GAP_METERS = 0.6
KML_FAT_CODE_HEIGHT_METERS = 1.25
KML_FAT_BORDER_COLOR = 30
KML_FDT_LAYER = "FDT"
KML_FDT_BLOCK_NAME = "FDT_ICON"
KML_FDT_ATTR_TAG = "FDT_CODE"
KML_FDT_ICON_WIDTH_METERS = 8.4
KML_FDT_ICON_HEIGHT_METERS = 5.6
KML_FDT_ICON_INNER_INSET_METERS = 0.28
KML_FDT_LABEL_GAP_METERS = 0.65
KML_FDT_LABEL_HEIGHT_METERS = 1.45
KML_FDT_TEXT_HEIGHT_METERS = 2.75
KML_FDT_BORDER_COLOR = 1
KML_OLT_LAYER = "OLT"
KML_OLT_BLOCK_NAME = "OLT_ICON"
KML_OLT_COLOR = 1
KML_OLT_ICON_WIDTH_METERS = KML_FDT_ICON_WIDTH_METERS
KML_OLT_ICON_HEIGHT_METERS = KML_FDT_ICON_HEIGHT_METERS
KML_POLE_BLOCK_PREFIX = "POLE_ICON"
KML_SLACK_HANGER_BLOCK_NAME = "SLACK_HANGER_ICON"
KML_SLACK_HANGER_COLOR = 1
KML_SLACK_HANGER_WIDTH_METERS = 7.0
KML_SLACK_HANGER_HEIGHT_METERS = 2.6
KML_JOINT_CLOSURE_BLOCK_NAME = "JOINT_CLOSURE_ICON"
KML_JOINT_CLOSURE_LAYER = "CLOSURE"
KML_JOINT_CLOSURE_COLOR = 1
KML_JOINT_CLOSURE_DIAMETER_METERS = 4.0
# Snap radius for icons (slack hanger, joint closure, cable end vertices) onto
# the centre of a nearby pole icon (matches user expectation that placemarks placed
# next to a pole are visually attached to it).
KML_POLE_SNAP_RADIUS_METERS = 5.0
KML_HANDHOLE_40_LAYER = "NEW_HH_40X40X60"
KML_HANDHOLE_80_LAYER = "NEW_HH_80X80X130"
KML_HANDHOLE_20_LAYER = "NEW_HH_20X20X20"
KML_HANDHOLE_40_BLOCK_NAME = "HANDHOLE_40_ICON"
KML_HANDHOLE_80_BLOCK_NAME = "HANDHOLE_80_ICON"
KML_HANDHOLE_20_BLOCK_NAME = "HANDHOLE_20_ICON"
KML_HANDHOLE_COLOR = 5
KML_HANDHOLE_20_COLOR = 3
KML_HANDHOLE_ICON_SIZE_METERS = 3.2
KML_HANDHOLE_ICON_INSET_METERS = 0.22
KML_HANDHOLE_TEXT_HEIGHT_METERS = 0.86
KML_HANDHOLE_LABEL_HEIGHT_METERS = 0.75
KML_TWEY_LAYER = "TWEY"
KML_TWEY_BLOCK_NAME = "TWEY_ICON"
KML_TWEY_COLOR = 1
KML_TWEY_ICON_SIZE_METERS = 2.4
KML_TWEY_TEXT_HEIGHT_METERS = 0.52
KML_TWEY_CABLE_SEARCH_RADIUS_METERS = 60.0
TEMPLATE_LAYER = "TEMPLATE_FRAME"
DXF_TEMPLATE_DIR = Path(__file__).resolve().parent / "dxf_templates"
DXF_TEMPLATE_PATH = DXF_TEMPLATE_DIR / "Template_AE.dxf"
LEGACY_DXF_TEMPLATE_PATH = DXF_TEMPLATE_DIR / "Template.dxf"
OUTPUT_DXF_VERSION = "AC1015"
WHITE_TRUE_COLOR = 0xFFFFFF
WHITE_ACI_COLOR = 255
AUTOCAD_LT_XDATA_APPIDS = (
    "ACAD_MLEADERVER",
    "ACDBATTR",
    "ACDBBLOCKREPETAG",
    "GRADIENTCOLOR1ACI",
    "GRADIENTCOLOR2ACI",
    "HATCHBACKGROUNDCOLOR",
)
TEMPLATE_LAYOUT_NAME = "Cluster"
LEGACY_TEMPLATE_LAYOUT_NAME = "Layout1"
TEMPLATE_VIEWPORT_LAYER = "VIEWPORTS"
TEMPLATE_CLUSTER_VIEWPORT_BOUNDS = (12.87, 12.73, 350.12, 281.37)
TEMPLATE_FDT_VIEWPORT_BOUNDS = (12.87, 12.73, 350.12, 281.37)
TEMPLATE_PAPER_VIEWPORT_CENTER = (210.0, 148.5)
TEMPLATE_PAPER_VIEWPORT_SIZE = (420.0, 297.0)
# Measured from Template_Feeder_AE.dxf and Template_Feeder_UG.dxf; the
# Feeder paper viewports do not match the Cluster A3 viewport bounds.
TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_CENTER = (275.0936221713732, 145.9730807574958)
TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_SIZE = (873.1491237069812, 317.2608476561546)
TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_CENTER = (347.6070861829255, 115.4721414644476)
TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_SIZE = (1330.938937267691, 483.599884545991)
TEMPLATE_FEEDER_PAPER_VIEWPORTS = {
    "AE": (TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_CENTER, TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_SIZE),
    "UG": (TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_CENTER, TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_SIZE),
}
TEMPLATE_PAPER_SIZE = (420.0, 297.0)
FDT_DESIGN_SUMMARY_CLEAR_BOUNDS = (6.0, 4.0, 112.5, 95.0)
TEMPLATE_LAYOUT_VIEWPORT_CENTER = (
    (TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[0] + TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[2]) / 2,
    (TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[1] + TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[3]) / 2,
)
TEMPLATE_LAYOUT_VIEWPORT_SIZE = (
    TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[2] - TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[0],
    TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[3] - TEMPLATE_CLUSTER_VIEWPORT_BOUNDS[1],
)
TITLE_BLOCK_CLUSTER_NAME_BOUNDS = (354.0, 89.0, 412.0, 100.0)
FAT_CALLOUT_LAYER = "FAT_Info"
FAT_CALLOUT_BLOCK_NAME = "FAT_INFO_CALLOUT"
FAT_CALLOUT_TITLE_TAG = "FAT_NAME"
FAT_CALLOUT_DISTANCE_TAG = "FAT_DISTANCE"
FAT_CALLOUT_POWER_TAG = "FAT_POWER"
FAT_CALLOUT_COLOR = 5
FAT_CALLOUT_WIDTH = 24.0
FAT_CALLOUT_ROW_HEIGHT = 2.25
FAT_CALLOUT_MARGIN = 1.0
FAT_CALLOUT_GAP = 1.6
DESIGN_SUMMARY_LEFT_VALUE_X = 46.32
DESIGN_SUMMARY_RIGHT_VALUE_X = 97.86
DESIGN_SUMMARY_LEFT_VALUE_BOUNDS = (40.0, 54.0)
DESIGN_SUMMARY_RIGHT_VALUE_BOUNDS = (92.0, 106.0)
FDT_LAYOUT_PREFIX = "Cluster-FDT"


def normalize_scope_type_name(scope_type: str | None) -> str:
    raw = str(scope_type or "").strip().lower()
    if raw in {"feeder", "subfeeder", "sub feeder", "sub-feeder"}:
        return "Subfeeder" if raw != "feeder" else "Feeder"
    if raw == "cluster":
        return "Cluster"
    return ""


def is_feeder_like_scope(scope_type: str | None) -> bool:
    return normalize_scope_type_name(scope_type) in {"Feeder", "Subfeeder"}
# Fallback mode still fits converted data into a generated modelspace frame.
RIGHT_LAYOUT_CONTENT_MIN = (2108.63, 1552.76)
RIGHT_LAYOUT_CONTENT_MAX = (2120.66, 1562.71)
LEGACY_RIGHT_LAYOUT_CONTENT_MIN = (485.0, 70.0)
LEGACY_RIGHT_LAYOUT_CONTENT_MAX = (815.0, 282.0)

LINE_LAYERS = {"ROAD", "WATERWAY", "RAILWAY"}
POLYGON_LAYERS = {"BUILDING", "WATER_AREA", "LANDUSE", "LEISURE", "OTHER"}

Point = tuple[float, float]

DEFAULT_ROAD_WIDTHS_METERS = {
    "motorway": 16.0,
    "trunk": 14.0,
    "primary": 12.0,
    "secondary": 10.0,
    "tertiary": 9.0,
    "unclassified": 7.0,
    "residential": 6.0,
    "living_street": 5.0,
    "service": 4.0,
    "track": 3.5,
    "pedestrian": 4.0,
    "footway": 2.0,
    "path": 2.0,
    "cycleway": 2.5,
    "steps": 1.8,
}
LANE_WIDTH_METERS = 3.2
ROAD_EXCLUDED_HIGHWAYS = {
    "bridleway",
    "construction",
    "corridor",
    "cycleway",
    "elevator",
    "footway",
    "path",
    "platform",
    "proposed",
    "raceway",
    "steps",
}
ROAD_EXCLUDED_SERVICES = {"driveway", "drive-through", "parking_aisle"}
ROAD_MIN_LENGTH_METERS = {
    "service": 35.0,
    "track": 30.0,
    "living_street": 12.0,
    "residential": 8.0,
}
ROAD_SIMPLIFY_TOLERANCE_METERS = 0.4
ROAD_MIN_POLYGON_AREA_SQUARE_METERS = 12.0
# Inner "casing" line drawn parallel just inside each road outline so roads
# read as a double line (road edge + inner edge) like standard CAD road
# drawings, instead of a single thin outline. The inner boundary is the road
# polygon shrunk inward by this offset.
ROAD_INNER_LINE_OFFSET_METERS = 0.9
ROAD_INNER_LINE_MIN_POLYGON_AREA_SQUARE_METERS = 18.0
ROAD_NAME_LABEL_HEIGHT_METERS = 1.05
ROAD_NAME_LABEL_MIN_HEIGHT_METERS = 0.55
ROAD_NAME_LABEL_WIDTH_FACTOR = 0.62
ROAD_NAME_LABEL_MIN_LENGTH_METERS = 18.0
ROAD_NAME_LABEL_REPEAT_DISTANCE_METERS = 80.0


def build_transformer(target_crs: str = "EPSG:3857") -> Transformer:
    """Keep projection setup isolated so it can be changed to UTM later."""
    return Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)


def project_point(lon: float, lat: float, transformer: Transformer | None = None) -> tuple[float, float]:
    transformer = transformer or build_transformer()
    x, y = transformer.transform(lon, lat)
    return float(x), float(y)


def project_coordinates(
    coordinates: Iterable[Point],
    transformer: Transformer | None = None,
) -> list[Point]:
    transformer = transformer or build_transformer()
    return [project_point(lon, lat, transformer) for lon, lat in coordinates]


def _distance(first: Point, second: Point) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def point_polygon_distance_meter(point: Point, polygon_coords) -> float:
    """Distance from a point to a polygon ring. Minimum of perpendicular
    distance to each edge segment + corner distances."""
    coords = list(polygon_coords)
    if not coords:
        return float("inf")
    px, py = float(point[0]), float(point[1])
    best = float("inf")
    n = len(coords)
    for i in range(n):
        x1, y1 = float(coords[i][0]), float(coords[i][1])
        x2, y2 = float(coords[(i + 1) % n][0]), float(coords[(i + 1) % n][1])
        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq <= 1e-12:
            d = math.hypot(px - x1, py - y1)
        else:
            t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
            proj_x = x1 + t * dx
            proj_y = y1 + t * dy
            d = math.hypot(px - proj_x, py - proj_y)
        if d < best:
            best = d
    return best


def _clean_points(points: Sequence[Point], closed: bool = False) -> list[Point]:
    cleaned: list[Point] = []
    for point in points:
        if not cleaned or _distance(cleaned[-1], point) > 1e-6:
            cleaned.append(point)

    if closed and len(cleaned) > 1 and _distance(cleaned[0], cleaned[-1]) <= 1e-6:
        cleaned.pop()

    return cleaned


def _unit_direction(first: Point, second: Point) -> Point | None:
    length = _distance(first, second)
    if length <= 1e-9:
        return None
    return ((second[0] - first[0]) / length, (second[1] - first[1]) / length)


def _left_normal(direction: Point, side: float) -> Point:
    return (-direction[1] * side, direction[0] * side)


def _line_intersection(point_a: Point, direction_a: Point, point_b: Point, direction_b: Point) -> Point | None:
    cross = direction_a[0] * direction_b[1] - direction_a[1] * direction_b[0]
    if abs(cross) <= 1e-9:
        return None

    delta = (point_b[0] - point_a[0], point_b[1] - point_a[1])
    factor = (delta[0] * direction_b[1] - delta[1] * direction_b[0]) / cross
    return (point_a[0] + direction_a[0] * factor, point_a[1] + direction_a[1] * factor)


def offset_polyline(points: Sequence[Point], distance: float, side: float = 1.0, closed: bool = False) -> list[Point]:
    clean_points = _clean_points(points, closed=closed)
    if len(clean_points) < 2 or distance <= 0:
        return list(clean_points)

    if closed and len(clean_points) < 3:
        closed = False

    segment_count = len(clean_points) if closed else len(clean_points) - 1
    directions: list[Point] = []
    normals: list[Point] = []

    for index in range(segment_count):
        start = clean_points[index]
        end = clean_points[(index + 1) % len(clean_points)]
        direction = _unit_direction(start, end)
        if direction is None:
            continue
        directions.append(direction)
        normals.append(_left_normal(direction, side))

    if not directions:
        return []

    offset_points: list[Point] = []
    vertex_count = len(clean_points)
    max_miter = max(distance * 8, distance + 1)

    for index, point in enumerate(clean_points):
        if not closed and index == 0:
            normal = normals[0]
            offset_points.append((point[0] + normal[0] * distance, point[1] + normal[1] * distance))
            continue

        if not closed and index == vertex_count - 1:
            normal = normals[-1]
            offset_points.append((point[0] + normal[0] * distance, point[1] + normal[1] * distance))
            continue

        previous_index = (index - 1) % len(directions)
        next_index = index % len(directions)
        previous_normal = normals[previous_index]
        next_normal = normals[next_index]
        previous_point = (
            point[0] + previous_normal[0] * distance,
            point[1] + previous_normal[1] * distance,
        )
        next_point = (
            point[0] + next_normal[0] * distance,
            point[1] + next_normal[1] * distance,
        )
        intersection = _line_intersection(
            previous_point,
            directions[previous_index],
            next_point,
            directions[next_index],
        )

        if intersection and _distance(point, intersection) <= max_miter:
            offset_points.append(intersection)
        else:
            average_normal = (
                previous_normal[0] + next_normal[0],
                previous_normal[1] + next_normal[1],
            )
            average_length = math.hypot(average_normal[0], average_normal[1])
            if average_length <= 1e-9:
                offset_points.append(next_point)
            else:
                offset_points.append(
                    (
                        point[0] + average_normal[0] / average_length * distance,
                        point[1] + average_normal[1] / average_length * distance,
                    )
                )

    return offset_points


def tag_value(tags, key: str) -> str | None:
    try:
        return tags.get(key)
    except AttributeError:
        return dict(tags).get(key)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def parse_osm_measure_meters(value: str | None) -> float | None:
    if not value:
        return None

    normalized = value.strip().lower().replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if not match:
        return None

    number = float(match.group(0))
    if "ft" in normalized or "'" in normalized:
        number *= 0.3048

    return number if number > 0 else None


def estimate_road_half_width_meters(tags) -> float:
    for key in ("width", "est_width"):
        explicit_width = parse_osm_measure_meters(tag_value(tags, key))
        if explicit_width:
            return _clamp(explicit_width / 2, 0.8, 20.0)

    lanes = parse_osm_measure_meters(tag_value(tags, "lanes"))
    if lanes:
        full_width = lanes * LANE_WIDTH_METERS + 1.0
        return _clamp(full_width / 2, 1.2, 20.0)

    highway = tag_value(tags, "highway") or "residential"
    service = tag_value(tags, "service")
    if highway == "service" and service in {"driveway", "parking_aisle", "alley"}:
        return 1.6

    full_width = DEFAULT_ROAD_WIDTHS_METERS.get(highway, DEFAULT_ROAD_WIDTHS_METERS["residential"])
    return _clamp(full_width / 2, 0.8, 20.0)


def should_export_road(tags, projected_coordinates: Sequence[Point]) -> bool:
    highway = tag_value(tags, "highway")
    if not highway or highway in ROAD_EXCLUDED_HIGHWAYS:
        return False

    service = tag_value(tags, "service")
    if highway == "service" and service in ROAD_EXCLUDED_SERVICES:
        return False

    clean_coordinates = _clean_points(projected_coordinates)
    if len(clean_coordinates) < 2:
        return False

    line = LineString(clean_coordinates)
    if line.is_empty or line.length <= 0:
        return False

    minimum_length = ROAD_MIN_LENGTH_METERS.get(highway, 6.0)
    return line.length >= minimum_length


def classify_tags(tags) -> str:
    if tag_value(tags, "highway"):
        return "ROAD"
    if tag_value(tags, "building"):
        return "BUILDING"
    if tag_value(tags, "waterway"):
        return "WATERWAY"
    if tag_value(tags, "railway"):
        return "RAILWAY"
    if tag_value(tags, "natural") == "water" or tag_value(tags, "landuse") == "reservoir":
        return "WATER_AREA"
    if tag_value(tags, "landuse"):
        return "LANDUSE"
    if tag_value(tags, "leisure"):
        return "LEISURE"
    return "OTHER"


def tags_to_dict(tags) -> dict[str, str]:
    return {tag.k: tag.v for tag in tags}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text_by_name(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def direct_child_text_by_name(element: ET.Element, name: str) -> str:
    child = direct_child_by_name(element, name)
    return child.text.strip() if child is not None and child.text else ""


def children_by_name(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if local_name(child.tag) == name]


def direct_child_by_name(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if local_name(child.tag) == name:
            return child
    return None


def nearest_standard_folder(folder_stack: Sequence[str]) -> str | None:
    for folder_name in reversed(folder_stack):
        canonical = canonical_standard_folder(folder_name)
        if canonical:
            return canonical
    return None


def normalized_folder_name(folder_name: str) -> str:
    return _normalized_folder_key(folder_name)


def canonical_standard_folder(folder_name: str | None) -> str | None:
    return STANDARD_KML_FOLDER_ALIASES.get(normalized_folder_name(folder_name or ""))


def dxf_safe_layer_name(value: str, fallback: str = "KML") -> str:
    normalized = normalized_folder_name(value)
    safe_name = re.sub(r"[^A-Z0-9_]+", "_", normalized)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    return (safe_name or fallback)[:31]


def distribution_cable_parent_layer(folder_stack: Sequence[str]) -> str | None:
    for index in range(len(folder_stack) - 1, -1, -1):
        if normalized_folder_name(folder_stack[index]) == "DISTRIBUTION CABLE":
            if index > 0:
                return dxf_safe_layer_name(folder_stack[index - 1], "DISTRIBUTION_CABLE")
            if index + 1 < len(folder_stack):
                return dxf_safe_layer_name(folder_stack[index + 1], "DISTRIBUTION_CABLE")
            return None
    return None

def is_distribution_cable_folder(folder_stack: Sequence[str]) -> bool:
    return any(normalized_folder_name(folder_name) in {"DISTRIBUTION CABLE", "CABLE"} for folder_name in folder_stack)

def distribution_cable_core_layer(*values: str | None) -> str | None:
    clean_name = " ".join(value or "" for value in values)
    for pattern, layer_name in DISTRIBUTION_CABLE_CORE_LAYER_RULES:
        if pattern.search(clean_name):
            return layer_name
    return None

def distribution_cable_summary_key(*values: str | None) -> str | None:
    search_text = " ".join(value or "" for value in values)
    rules = (
        (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?24\s*C(?:\s*/\s*2\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "24/2T"),
        (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?36\s*C(?:\s*/\s*3\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "36/3T"),
        (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?48\s*C(?:\s*/\s*4\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "48/4T"),
        (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?72\s*C\s*/\s*6\s*T(?![A-Z0-9])", re.IGNORECASE), "72/6T"),
    )
    for pattern, key in rules:
        if pattern.search(search_text):
            return key
    return None

def calculate_kml_linestring_length_meter(linestrings: Sequence[Sequence[tuple[float, float]]]) -> float:
    total = 0.0
    for coordinates in linestrings:
        if len(coordinates) < 2:
            continue
        for first, second in zip(coordinates, coordinates[1:]):
            try:
                _az12, _az21, distance = GEOD.inv(first[0], first[1], second[0], second[1])
            except Exception:
                continue
            if math.isfinite(distance) and distance > 0:
                total += float(distance)
    return total

def extract_cluster_name_from_input_path(path: str | Path) -> str:
    stem = Path(path).stem
    stem = re.sub(r"^[0-9a-f]{32}_\d+_", "", stem, flags=re.IGNORECASE)
    normalized = re.sub(r"[_\s]+", " ", stem).strip()
    normalized = re.sub(r"\s*-\s*", " - ", normalized)
    if " - " in normalized:
        normalized = normalized.split(" - ", 1)[1].strip() or normalized
    normalized = re.sub(r"^[A-Z]{2,5}\d{3,}\s+", "", normalized, flags=re.IGNORECASE).strip()
    return normalized

def normalize_cluster_name_for_dxf(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", value.strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.upper()

def kml_special_linestring_spec(folder_stack: Sequence[str], name: str = "") -> dict | None:
    if is_distribution_cable_folder(folder_stack):
        core_layer = distribution_cable_core_layer(name, " ".join(folder_stack))
        if core_layer:
            return {
                "layer": core_layer,
                "width": KML_DISTRIBUTION_CABLE_WIDTH,
                "lineweight": KML_CABLE_LINEWEIGHT,
                "draw_order": "top_cable",
            }

    distribution_layer = distribution_cable_parent_layer(folder_stack)
    if distribution_layer:
        return {
            "layer": distribution_layer,
            "width": KML_DISTRIBUTION_CABLE_WIDTH,
            "lineweight": KML_CABLE_LINEWEIGHT,
            "draw_order": "top_cable",
        }

    ug_line_specs = {
        "SUBDUCT 32/28": {
            "layer": KML_SUBDUCT_32_LAYER,
            "width": KML_SUBDUCT_32_WIDTH,
            "lineweight": KML_SUBDUCT_32_LINEWEIGHT,
            "draw_order": "subduct",
        },
        "SUBDUCT 40/34": {
            "layer": KML_SUBDUCT_40_LAYER,
            "width": KML_SUBDUCT_40_WIDTH,
            "lineweight": KML_SUBDUCT_40_LINEWEIGHT,
            "draw_order": "subduct",
        },
        "TALI PANCING": {
            "layer": KML_TALI_PANCING_LAYER,
            "width": KML_TALI_PANCING_WIDTH,
            "lineweight": KML_TALI_PANCING_LINEWEIGHT,
            "draw_order": "top_cable",
        },
    }
    for folder_name in reversed(folder_stack):
        canonical = canonical_standard_folder(folder_name) or normalized_folder_name(folder_name)
        if canonical == "SLING WIRE":
            return {
                "layer": KML_SLING_WIRE_LAYER,
                "width": KML_DISTRIBUTION_CABLE_WIDTH,
                "lineweight": KML_CABLE_LINEWEIGHT,
                "draw_order": "top_cable",
            }
        if canonical in ug_line_specs:
            return ug_line_specs[canonical]
    return None


def kml_special_linestring_layer(folder_stack: Sequence[str], name: str = "") -> str | None:
    spec = kml_special_linestring_spec(folder_stack, name)
    return str(spec["layer"]) if spec else None


def true_color_to_aci(true_color: int | None) -> int:
    if true_color is None:
        return 7

    red = (int(true_color) >> 16) & 0xFF
    green = (int(true_color) >> 8) & 0xFF
    blue = int(true_color) & 0xFF
    palette = {
        1: (255, 0, 0),
        2: (255, 255, 0),
        3: (0, 255, 0),
        4: (0, 255, 255),
        5: (0, 0, 255),
        6: (255, 0, 255),
        7: (255, 255, 255),
        8: (128, 128, 128),
    }
    return min(
        palette,
        key=lambda aci: (palette[aci][0] - red) ** 2 + (palette[aci][1] - green) ** 2 + (palette[aci][2] - blue) ** 2,
    )


def kml_pole_folder_spec(folder_name: str | None) -> dict | None:
    normalized = normalized_folder_name(folder_name or "")
    if not normalized:
        return None

    if "EXISTING POLE" in normalized:
        return {
            "kind": "existing",
            "label": "EXISTING POLE",
            "color": 1,
        }

    specs = {
        "NEW POLE 9-4": {
            "label": 'NEW POLE 9M 4"',
            "top": "NP9",
            "bottom": '4"',
            "color": 1,
        },
        "NEW POLE 9-5": {
            "label": 'NEW POLE 9M 5"',
            "top": "NP9",
            "bottom": '5"',
            "color": 30,
        },
        "NEW POLE 7-4": {
            "label": 'NEW POLE 7M 4"',
            "top": "NP7",
            "bottom": '4"',
            "color": 3,
        },
        "NEW POLE 7-5": {
            "label": 'NEW POLE 7M 5"',
            "top": "NP7",
            "bottom": '5"',
            "color": 40,
        },
        "NEW POLE 7-3": {
            "label": 'NEW POLE 7M 3"',
            "top": "NP7",
            "bottom": '3"',
            "color": 4,
        },
        "NEW POLE 7-2.5": {
            "label": 'NEW POLE 7M 2.5"',
            "top": "NP7",
            "bottom": "2.5'",
            "color": 6,
        },
    }
    return specs.get(normalized)


FAT_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-E]\d{2})(?![A-Z0-9])")
PARENT_FDT_NUMBER_PATTERN = re.compile(r"(?<![A-Z0-9])FDT\s*[-_\s]*(\d+)(?![A-Z0-9])", re.IGNORECASE)
FDT_SUFFIX_PATTERN = re.compile(r"\.(\d+)\D*$")


def find_fat_code(value: str | None) -> str | None:
    match = FAT_CODE_PATTERN.search((value or "").upper())
    return match.group(1) if match else None


def extract_fat_code(name: str) -> str:
    return find_fat_code(name) or name.strip()


def extract_fat_code_from_path(folder_stack: Sequence[str], name: str = "") -> str:
    for value in [name, *reversed(folder_stack)]:
        code = find_fat_code(value)
        if code:
            return code
    return (name or "").strip() or "FAT"

def detect_parent_fdt_number(folder_stack: Sequence[str] | None) -> int | None:
    for folder_name in reversed(folder_stack or []):
        match = PARENT_FDT_NUMBER_PATTERN.search(folder_name or "")
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None

def extract_fdt_suffix_number(value: str | None) -> int | None:
    match = FDT_SUFFIX_PATTERN.search(value or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

def extract_fdt_label_from_path(folder_stack: Sequence[str], name: str = "") -> str:
    clean_name = (name or "").strip()
    if clean_name:
        return clean_name

    for folder_name in reversed(folder_stack):
        normalized = normalized_folder_name(folder_name)
        if normalized and normalized not in STANDARD_KML_FOLDER_SET:
            return folder_name.strip()
    return "FDT"


def is_road_clip_boundary_folder(folder_stack: Sequence[str]) -> bool:
    boundary_name = normalized_folder_name(ROAD_CLIP_BOUNDARY_FOLDER)
    return any(normalized_folder_name(folder_name) == boundary_name for folder_name in folder_stack)

def is_boundary_fat_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == "BOUNDARY FAT"

def is_hp_cover_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == "HP COVER"

def is_hp_uncover_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == "HP UNCOVER"

def is_slack_hanger_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == "SLACK HANGER"


def is_joint_closure_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == "JOINT CLOSURE"

def is_fdt_folder(folder_name: str | None) -> bool:
    return normalized_folder_name(folder_name or "") == KML_FDT_LAYER


def kml_handhole_folder_spec(folder_name: str | None) -> dict | None:
    canonical = canonical_standard_folder(folder_name) or normalized_folder_name(folder_name or "")
    specs = {
        "NEW HH 20X20X20": {
            "layer": KML_HANDHOLE_20_LAYER,
            "block": "HANDHOLE_PIT_20_BLOCK",
            "label_lines": ("HANDHOLE PIT", "20x20x30 cm"),
            "color": 5,
            "icon_size": KML_TWEY_ICON_SIZE_METERS,
            "show_text": False,
        },
        "NEW HH 40X40X60": {
            "layer": KML_HANDHOLE_40_LAYER,
            "block": KML_HANDHOLE_40_BLOCK_NAME,
            "size_text": "40",
            "label_lines": ("HANDHOLE", "40x40x60 cm"),
        },
        "NEW HH 80X80X130": {
            "layer": KML_HANDHOLE_80_LAYER,
            "block": KML_HANDHOLE_80_BLOCK_NAME,
            "size_text": "80",
            "label_lines": ("HANDHOLE", "80x80x130 cm"),
        },
    }
    return specs.get(canonical)


def is_twey_folder(folder_name: str | None) -> bool:
    canonical = canonical_standard_folder(folder_name) or normalized_folder_name(folder_name or "")
    return canonical == "TWEY"


def kml_color_to_true_color(value: str | None) -> int | None:
    if not value:
        return None

    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}", normalized):
        return None

    # KML stores colors as AABBGGRR, while DXF true_color expects RRGGBB.
    blue = int(normalized[2:4], 16)
    green = int(normalized[4:6], 16)
    red = int(normalized[6:8], 16)
    return (red << 16) + (green << 8) + blue


def parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_kml_coordinates(text: str | None) -> list[tuple[float, float]]:
    if not text:
        return []

    coordinates = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        if math.isfinite(lon) and math.isfinite(lat):
            coordinates.append((lon, lat))
    return coordinates


def sanitize_kml_xml(data: bytes | str) -> str:
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else str(data)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    # Fix unbound namespace prefixes (ns0, ns1, ns2, etc.)
    for match in re.finditer(r"\bxmlns:(ns\d+)=\"([^\"]+)\"", text):
        pass  # already declared
    unbound_prefixes = set(re.findall(r"<(ns\d+):", text)) - set(re.findall(r"xmlns:(ns\d+)=", text))
    for prefix in unbound_prefixes:
        if prefix == "ns1":
            text = re.sub(r"<kml(\s|>)", r'<kml xmlns:ns1="http://www.w3.org/2005/Atom"\1', text, count=1)
        elif prefix == "ns0":
            text = re.sub(r"<kml(\s|>)", r'<kml xmlns:ns0="http://www.opengis.net/kml/2.2"\1', text, count=1)
        else:
            text = re.sub(r"<kml(\s|>)", f'<kml xmlns:{prefix}="http://www.google.com/kml/ext/2.2"\\1', text, count=1)
    return re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][\w.-]*;)", "&amp;", text)


def parse_kml_xml(data: bytes | str, source_name: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as first_error:
        try:
            return ET.fromstring(sanitize_kml_xml(data))
        except ET.ParseError as second_error:
            raise ValueError(
                f"{source_name}: KML tidak valid atau struktur XML tidak bisa dibaca. Detail: {second_error}"
            ) from first_error


def read_kml_root(path: Path) -> ET.Element:
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError(f"KMZ tidak berisi file KML: {path}")
            preferred_name = next((name for name in kml_names if Path(name).name.lower() == "doc.kml"), kml_names[0])
            with archive.open(preferred_name) as file:
                return parse_kml_xml(file.read(), f"{path.name}/{preferred_name}")

    return parse_kml_xml(path.read_bytes(), path.name)


def parse_kml_styles(root: ET.Element) -> tuple[dict[str, dict], dict[str, str]]:
    styles: dict[str, dict] = {}
    style_maps: dict[str, str] = {}

    for element in root.iter():
        element_name = local_name(element.tag)
        style_id = element.attrib.get("id")
        if not style_id:
            continue

        style_key = f"#{style_id}"
        if element_name == "StyleMap":
            normal_style = ""
            for pair in [child for child in list(element) if local_name(child.tag) == "Pair"]:
                if direct_child_text_by_name(pair, "key") == "normal":
                    normal_style = direct_child_text_by_name(pair, "styleUrl")
                    break
            if normal_style:
                style_maps[style_key] = normal_style
            continue

        if element_name != "Style":
            continue

        icon_style = direct_child_by_name(element, "IconStyle")
        line_style = direct_child_by_name(element, "LineStyle")
        poly_style = direct_child_by_name(element, "PolyStyle")
        icon_href = ""
        icon_color = None
        icon_scale = 1.0

        if icon_style is not None:
            icon_color = kml_color_to_true_color(direct_child_text_by_name(icon_style, "color"))
            icon_scale = max(parse_float(direct_child_text_by_name(icon_style, "scale"), 1.0), 0.25)
            icon = direct_child_by_name(icon_style, "Icon")
            if icon is not None:
                icon_href = direct_child_text_by_name(icon, "href")

        line_color = kml_color_to_true_color(direct_child_text_by_name(line_style, "color")) if line_style is not None else None
        poly_color = kml_color_to_true_color(direct_child_text_by_name(poly_style, "color")) if poly_style is not None else None
        line_width = parse_float(direct_child_text_by_name(line_style, "width"), 1.0) if line_style is not None else 1.0

        styles[style_key] = {
            "icon_href": icon_href,
            "icon_color": icon_color,
            "icon_scale": icon_scale,
            "line_color": line_color,
            "poly_color": poly_color,
            "line_width": line_width,
        }

    return styles, style_maps


def resolve_kml_style(style_url: str, styles: dict[str, dict], style_maps: dict[str, str]) -> dict:
    resolved_url = style_url
    visited = set()
    while resolved_url in style_maps and resolved_url not in visited:
        visited.add(resolved_url)
        resolved_url = style_maps[resolved_url]
    return styles.get(resolved_url, {})



OSM_ROAD_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    min_lon REAL NOT NULL,
    min_lat REAL NOT NULL,
    max_lon REAL NOT NULL,
    max_lat REAL NOT NULL,
    half_width REAL NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    coordinates TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roads_bbox ON roads(min_lon, min_lat, max_lon, max_lat);
"""

class OSMRoadCacheBuilder(osmium.SimpleHandler):
    def __init__(self, db_path: Path, source_path: Path):
        super().__init__()
        self.db_path = Path(db_path)
        self.source_path = Path(source_path)
        self.transformer = build_transformer("EPSG:3857")
        self.conn = sqlite3.connect(self.db_path)
        self.pending: list[tuple[float, float, float, float, float, str, str]] = []
        self.ways_seen = 0
        self.roads_saved = 0
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(OSM_ROAD_CACHE_SCHEMA)
        self.conn.execute("DELETE FROM roads")
        self.conn.execute("DELETE FROM meta")

    def way(self, way) -> None:
        self.ways_seen += 1
        try:
            tags = tags_to_dict(way.tags)
            if classify_tags(tags) != "ROAD":
                return
            coordinates: list[tuple[float, float]] = []
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
            if not should_export_road(tags, projected):
                return
            lons = [point[0] for point in coordinates]
            lats = [point[1] for point in coordinates]
            self.pending.append((
                min(lons),
                min(lats),
                max(lons),
                max(lats),
                estimate_road_half_width_meters(tags),
                tags.get("name", "") or "",
                json.dumps(coordinates, separators=(",", ":")),
            ))
            self.roads_saved += 1
            if len(self.pending) >= 2000:
                self.flush()
        except Exception as exc:
            LOGGER.debug("Road cache way %s skipped: %s", getattr(way, "id", "unknown"), exc)

    def flush(self) -> None:
        if not self.pending:
            return
        self.conn.executemany(
            "INSERT INTO roads(min_lon, min_lat, max_lon, max_lat, half_width, name, coordinates) VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.pending,
        )
        self.conn.commit()
        self.pending.clear()

    def close(self) -> None:
        self.flush()
        stat = self.source_path.stat()
        self.conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            [
                ("source", str(self.source_path.resolve())),
                ("source_size", str(stat.st_size)),
                ("source_mtime_ns", str(stat.st_mtime_ns)),
                ("ways_seen", str(self.ways_seen)),
                ("roads_saved", str(self.roads_saved)),
            ],
        )
        self.conn.commit()
        self.conn.close()


def road_cache_path_for_pbf(path: Path) -> Path:
    return path.with_name(path.name + ".roadcache.sqlite")


def road_cache_is_fresh(source_path: Path, cache_path: Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        stat = source_path.stat()
        with sqlite3.connect(cache_path) as conn:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return rows.get("source_size") == str(stat.st_size) and rows.get("source_mtime_ns") == str(stat.st_mtime_ns)
    except Exception:
        return False


def ensure_road_cache(source_path: Path, pipeline_logger=None) -> Path:
    cache_path = road_cache_path_for_pbf(source_path)
    if road_cache_is_fresh(source_path, cache_path):
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    pipeline_log(pipeline_logger, "info", f"Building Geofabrik ROAD cache once: {source_path.name}")
    start = time.perf_counter()
    builder = OSMRoadCacheBuilder(temp_path, source_path)
    try:
        builder.apply_file(str(source_path), locations=True)
        builder.close()
        temp_path.replace(cache_path)
    except Exception:
        try:
            builder.conn.close()
        except Exception:
            pass
        if temp_path.exists():
            temp_path.unlink()
        raise
    pipeline_log(
        pipeline_logger,
        "success",
        f"Geofabrik ROAD cache ready: {builder.roads_saved:,} road(s) in {format_stage_seconds(time.perf_counter() - start)}.",
    )
    return cache_path


def projected_bounds_to_lonlat(bounds: tuple[float, float, float, float], target_crs: str) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bounds
    transformer = Transformer.from_crs(target_crs, "EPSG:4326", always_xy=True)
    corners = [
        transformer.transform(min_x, min_y),
        transformer.transform(min_x, max_y),
        transformer.transform(max_x, min_y),
        transformer.transform(max_x, max_y),
    ]
    lons = [float(point[0]) for point in corners if math.isfinite(point[0])]
    lats = [float(point[1]) for point in corners if math.isfinite(point[1])]
    if not lons or not lats:
        return (-180.0, -90.0, 180.0, 90.0)
    pad = 0.002
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


def is_geofabrik_pbf(path: Path) -> bool:
    try:
        return path.suffix.lower() == ".pbf" and Config.GEOFABRIK_DIR.resolve() in path.resolve().parents
    except Exception:
        return False

class OSMToDXFHandler(osmium.SimpleHandler):
    def __init__(
        self,
        output_path: str | Path,
        selected_layers: Iterable[str] | None = None,
        road_offset_meters: float | None = None,
        target_crs: str = "EPSG:3857",
        progress_callback: Callable[[int], None] | None = None,
        preview_feature_limit: int = 2500,
        dxf_template_path: str | Path | None = None,
        network_type: str | None = None,
        design_summary_overrides: dict[str, int | float | str] | None = None,
        scope_type: str | None = None,
    ):
        super().__init__()
        self.output_path = Path(output_path)
        self.dxf_template_path = Path(dxf_template_path) if dxf_template_path else DXF_TEMPLATE_PATH
        inferred_network_type = (network_type or "").strip().upper()
        if not inferred_network_type:
            template_stem = self.dxf_template_path.stem.upper()
            if template_stem.endswith("_UG"):
                inferred_network_type = "UG"
            elif template_stem.endswith("_AE"):
                inferred_network_type = "AE"
        self.network_type = inferred_network_type
        # Detect Feeder scope from template path or explicit param
        self.scope_type = normalize_scope_type_name(scope_type)
        if not self.scope_type:
            if "FEEDER" in self.dxf_template_path.stem.upper():
                self.scope_type = "Feeder"
            else:
                self.scope_type = "Cluster"
        self.design_summary_overrides = {
            str(key).strip().upper(): value
            for key, value in (design_summary_overrides or {}).items()
            if str(key).strip()
        }
        self.selected_layers = set(selected_layers or DXF_LAYERS.keys())
        self.road_offset_meters = None if road_offset_meters is None else max(float(road_offset_meters), 0.0)
        self.transformer = build_transformer(target_crs)
        self.progress_callback = progress_callback
        self.preview_feature_limit = preview_feature_limit
        self.preview_features: list[dict] = []
        self.road_buffers = []
        self.road_lines = []
        self.road_centerlines: list[LineString] = []
        self._road_centerline_tree = None
        self._home_parcel_road_solid_cache = None
        self._home_parcel_road_solid_ready = False
        self.kml_line_centerlines: list[LineString] = []
        self.kml_cable_centerlines: list[LineString] = []
        self.subduct_line_entities: list[object] = []
        self.top_cable_line_entities: list[object] = []
        self.twey_overlay_entities: list[object] = []
        self.fat_fdt_overlay_entities: list[object] = []
        self.road_clip_geometry = None
        self.road_name_candidates: list[dict] = []
        self.road_name_label_centers: list[tuple[str, Point]] = []
        self.fat_area_geometries = []
        self.pending_home_numbers: list[dict] = []
        self.pending_uncover_home_numbers: list[dict] = []
        self.pending_fat_icons: list[dict] = []
        self.pending_handhole_icons: list[dict] = []
        self.pending_twey_points: list[dict] = []
        self.pending_olt_icons: list[dict] = []
        self.pending_pole_ids: list[dict] = []
        self.fat_callout_refs: list[dict] = []
        self.home_number_boxes: list[Polygon] = []
        # Pole positions used for snapping nearby slack/joint closure/cable
        # placemarks to the centre of an existing pole icon.
        self.pole_positions: list[Point] = []
        self.pending_slack_icons: list[dict] = []
        self.pending_joint_closure_icons: list[dict] = []
        # Cable polyline entities whose endpoints should snap to nearby poles.
        self.cable_endpoint_snap_entities: list[object] = []
        self.distance_label_centers: list[Point] = []
        self.kml_detail_stats = {
            "home_number_points": 0,
            "hp_uncover_labels": 0,
            "hp_uncover_points": 0,
            "fat_blocks": 0,
            "fdt_blocks": 0,
            "slack_hanger_blocks": 0,
            "handhole_blocks": 0,
            "twey_blocks": 0,
            "olt_blocks": 0,
            "pole_blocks": 0,
            "generic_icons": 0,
            "line_features": 0,
            "distance_labels": 0,
            "polygon_features": 0,
            "labels": 0,
        }
        self.stats = {layer: 0 for layer in DXF_LAYERS}
        self.processed_ways = 0
        self.processed_nodes = 0
        self.repaired_polygons = 0
        self.autocad_lt_extents: tuple[float, float, float, float] | None = None
        self.fixed_width_polylines: list[tuple[object, float]] = []
        self.cluster_name = ""
        self.dxf_summary_counts: dict[str, int] = defaultdict(int)
        self.dxf_summary_lengths: dict[str, float] = defaultdict(float)
        self.fdt_output_entities: dict[int, list[object]] = defaultdict(list)
        self.fdt_suffix_output_entities: list[tuple[int, list[object]]] = []
        self.detected_fdt_numbers: set[int] = set()

        self.doc, self.uses_dxf_template = self._create_document()
        self.doc.header["$INSUNITS"] = 6
        self.modelspace = self.doc.modelspace()
        self.output_entities = []
        self.content_min = RIGHT_LAYOUT_CONTENT_MIN if self.uses_dxf_template else LEGACY_RIGHT_LAYOUT_CONTENT_MIN
        self.content_max = RIGHT_LAYOUT_CONTENT_MAX if self.uses_dxf_template else LEGACY_RIGHT_LAYOUT_CONTENT_MAX
        self._setup_layers()

    def node(self, _node) -> None:
        self.processed_nodes += 1

    def _create_document(self):
        if self.dxf_template_path.exists():
            return ezdxf.readfile(self.dxf_template_path), True
        if LEGACY_DXF_TEMPLATE_PATH.exists():
            LOGGER.warning(
                "Template DXF tidak ditemukan: %s. Menggunakan template lama: %s",
                self.dxf_template_path,
                LEGACY_DXF_TEMPLATE_PATH,
            )
            return ezdxf.readfile(LEGACY_DXF_TEMPLATE_PATH), True
        LOGGER.warning("Template DXF tidak ditemukan: %s. Menggunakan frame fallback.", self.dxf_template_path)
        return ezdxf.new("R2010", setup=True), False

    def _setup_layers(self) -> None:
        for layer_name, color in DXF_LAYERS.items():
            self._ensure_layer(layer_name, color=color)
        self._ensure_layer(KML_FAT_LAYER, color=KML_FAT_BORDER_COLOR)
        self._ensure_layer(KML_FDT_LAYER, color=KML_FDT_BORDER_COLOR)
        self._ensure_layer(KML_OLT_LAYER, color=KML_OLT_COLOR)
        self._ensure_layer(KML_FAT_AREA_LAYER, color=KML_FAT_AREA_COLOR, linetype=KML_FAT_AREA_LINETYPE)
        self._ensure_layer(KML_HANDHOLE_20_LAYER, color=KML_HANDHOLE_20_COLOR)
        self._ensure_layer(KML_HANDHOLE_40_LAYER, color=KML_HANDHOLE_COLOR)
        self._ensure_layer(KML_HANDHOLE_80_LAYER, color=KML_HANDHOLE_COLOR)
        self._ensure_layer(KML_TWEY_LAYER, color=KML_TWEY_COLOR)
        self._ensure_layer(KML_HOME_NUMBER_LAYER, color=KML_HOME_NUMBER_COLOR)
        self._ensure_layer(KML_DISTANCE_LABEL_LAYER, color=KML_DISTANCE_LABEL_COLOR)
        self._ensure_exact_layer(KML_HOME_NUMBER_BOX_LAYER, color=KML_HOME_NUMBER_BOX_COLOR)
        if TEMPLATE_LAYER not in self.doc.layers:
            self.doc.layers.add(TEMPLATE_LAYER, color=7)
        self._ensure_viewports_layer_style()
        self._ensure_bold_text_style()
        self._turn_all_layers_visible()

    def _ensure_viewports_layer_style(self) -> None:
        if TEMPLATE_VIEWPORT_LAYER not in self.doc.layers:
            self.doc.layers.add(TEMPLATE_VIEWPORT_LAYER, color=WHITE_ACI_COLOR)
        layer = self.doc.layers.get(TEMPLATE_VIEWPORT_LAYER)
        try:
            layer.dxf.color = WHITE_ACI_COLOR
            layer.dxf.true_color = WHITE_TRUE_COLOR
            layer.rgb = (255, 255, 255)
        except Exception as exc:
            LOGGER.debug("Layer %s tidak bisa diset putih: %s", TEMPLATE_VIEWPORT_LAYER, exc)

    def _ensure_bold_text_style(self) -> None:
        """Create the dedicated bold style used by converter labels.

        `arialbd.ttf` is the standard Arial Bold font on Windows/AutoCAD
        systems. DXF viewers without it fall back cleanly to their default
        TrueType font, unlike an SHX-only workaround.
        """
        style_name = "EVA_BOLD"
        try:
            if style_name in self.doc.styles:
                style = self.doc.styles.get(style_name)
            else:
                style = self.doc.styles.new(style_name)
            style.dxf.font = "arialbd.ttf"
            style.dxf.bigfont = ""
        except Exception as exc:
            LOGGER.debug("Gagal membuat style teks bold: %s", exc)

    def _ensure_linetype(self, linetype_name: str) -> None:
        if linetype_name in self.doc.linetypes:
            return
        patterns = {
            "DASHED": ([0.6, 0.35, -0.25], 0.6),
            "PHANTOM2": ([1.45, 0.7, -0.18, 0.0, -0.18, 0.0, -0.39], 1.45),
        }
        pattern, length = patterns.get(linetype_name.upper(), ([0.6, 0.35, -0.25], 0.6))
        try:
            self.doc.linetypes.add(linetype_name, pattern=pattern, description=linetype_name, length=length)
        except Exception as exc:
            LOGGER.debug("Gagal menambahkan linetype %s: %s", linetype_name, exc)

    def _ensure_layer(self, layer_name: str, color: int = 7, linetype: str | None = None) -> str:
        safe_name = dxf_safe_layer_name(layer_name)
        if linetype:
            self._ensure_linetype(linetype)
        if safe_name not in self.doc.layers:
            self.doc.layers.add(safe_name, color=int(color))
        else:
            self.doc.layers.get(safe_name).color = int(color)
        if linetype:
            try:
                self.doc.layers.get(safe_name).dxf.linetype = linetype
            except Exception as exc:
                LOGGER.debug("Layer %s tidak bisa memakai linetype %s: %s", safe_name, linetype, exc)
        if safe_name not in self.stats:
            self.stats[safe_name] = 0
        return safe_name

    def _ensure_exact_layer(
        self,
        layer_name: str,
        color: int = 7,
        linetype: str | None = None,
        preserve_color: bool = False,
    ) -> str:
        if linetype:
            self._ensure_linetype(linetype)
        if layer_name not in self.doc.layers:
            self.doc.layers.add(layer_name, color=int(color))
        elif not preserve_color:
            self.doc.layers.get(layer_name).color = int(color)
        if linetype:
            try:
                self.doc.layers.get(layer_name).dxf.linetype = linetype
            except Exception as exc:
                LOGGER.debug("Layer %s tidak bisa memakai linetype %s: %s", layer_name, linetype, exc)
        return layer_name

    def _turn_all_layers_visible(self) -> None:
        for layer in self.doc.layers:
            try:
                layer.on()
                layer.thaw()
                layer.unlock()
            except Exception as exc:
                LOGGER.debug("Layer %s tidak bisa dipaksa visible: %s", getattr(layer.dxf, "name", ""), exc)

    def _supported_attrs(self, dxfattribs: dict | None = None) -> dict:
        attrs = dict(dxfattribs or {})
        if self.doc.dxfversion == "AC1009":
            attrs.pop("true_color", None)
            attrs.pop("lineweight", None)
        return attrs

    def _remember_output(self, entity):
        self.output_entities.append(entity)
        return entity

    def kml_detail_snapshot(self) -> dict[str, int]:
        return dict(self.kml_detail_stats)

    def kml_detail_delta(self, before: dict[str, int]) -> dict[str, int]:
        keys = set(before) | set(self.kml_detail_stats)
        return {
            key: int(self.kml_detail_stats.get(key, 0)) - int(before.get(key, 0))
            for key in keys
        }

    @staticmethod
    def format_kml_detail_delta(delta: dict[str, int]) -> str:
        immediate_icons = sum(
            int(delta.get(key, 0))
            for key in (
                "fat_blocks",
                "fdt_blocks",
                "slack_hanger_blocks",
                "handhole_blocks",
                "twey_blocks",
                "pole_blocks",
                "generic_icons",
            )
        )
        parts = [
            f"{immediate_icons:,} icon/block point(s)",
            f"{int(delta.get('home_number_points', 0)):,} HOME_NUMBER queued",
            f"{int(delta.get('line_features', 0)):,} line feature(s)",
            f"{int(delta.get('distance_labels', 0)):,} distance label(s)",
            f"{int(delta.get('polygon_features', 0)):,} polygon feature(s)",
            f"{int(delta.get('labels', 0)):,} label text(s)",
        ]
        return ", ".join(parts)

    def set_cluster_name(self, cluster_name: str) -> None:
        self.cluster_name = (cluster_name or "").strip()

    def register_fdt_entities(self, fdt_number: int | None, entities: Sequence[object]) -> None:
        if not fdt_number or fdt_number <= 0 or not entities:
            return
        self.detected_fdt_numbers.add(int(fdt_number))
        self.fdt_output_entities[int(fdt_number)].extend(entities)

    def register_fdt_suffix_entities(self, suffix_number: int | None, entities: Sequence[object]) -> None:
        if suffix_number is None or not entities:
            return
        self.fdt_suffix_output_entities.append((int(suffix_number), list(entities)))

    def assign_fdt_folder_entities_by_suffix(self) -> None:
        if not self.fdt_suffix_output_entities:
            return
        ordered_suffixes = sorted({suffix for suffix, _entities in self.fdt_suffix_output_entities})
        fdt_by_suffix = {suffix: index for index, suffix in enumerate(ordered_suffixes, start=1)}
        for suffix, entities in self.fdt_suffix_output_entities:
            self.register_fdt_entities(fdt_by_suffix.get(suffix), entities)

    def record_dxf_design_summary(self, placemark: ET.Element, source_folder: str | None, folder_stack: Sequence[str]) -> None:
        normalized_source = normalized_folder_name(source_folder or "")
        if not normalized_source:
            return

        has_geometry = any(children_by_name(placemark, geometry_name) for geometry_name in ("Point", "LineString", "Polygon"))
        if not has_geometry:
            return

        self.dxf_summary_counts[normalized_source] += 1
        if "POLE" in normalized_source:
            self.dxf_summary_counts["POLE_ALL"] += 1

        placemark_name = direct_child_text_by_name(placemark, "name")
        if normalized_source.startswith("EXISTING POLE PARTNER"):
            upper_name = placemark_name.upper()
            if "TLKM" in upper_name or "TELKOM" in upper_name:
                self.dxf_summary_counts["EXISTING_POLE_PARTNER_TELKOM"] += 1
            elif "MTI" in upper_name:
                self.dxf_summary_counts["EXISTING_POLE_PARTNER_MTI"] += 1
            elif re.search(r"(^|[^A-Z0-9])LN([^A-Z0-9]|$)", upper_name):
                self.dxf_summary_counts["EXISTING_POLE_PARTNER_LINKNET"] += 1
            elif re.search(r"\bMR\b", upper_name):
                # MR poles in PARTNER folder count as MR (Existing Pole MR)
                self.dxf_summary_counts["EXISTING_POLE_PARTNER_MR"] += 1

        # Joint Closure: count NEW only by capacity (24/36/48/72/96/144/288)
        if normalized_source == "JOINT CLOSURE":
            upper_name = placemark_name.upper()
            if "EXT" not in upper_name:
                cap_match = re.search(r"\b(24|36|48|72|96|144|288)\s*C?\b", upper_name)
                if cap_match:
                    self.dxf_summary_counts[f"JOINT_CLOSURE_{cap_match.group(1)}C"] += 1

        linestrings = []
        for line in children_by_name(placemark, "LineString"):
            coordinate_element = direct_child_by_name(line, "coordinates")
            coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
            if len(coordinates) >= 2:
                linestrings.append(coordinates)
        if not linestrings:
            return

        length = calculate_kml_linestring_length_meter(linestrings)
        if length <= 0:
            return

        if normalized_source == "DISTRIBUTION CABLE" or normalized_source == "CABLE":
            self.dxf_summary_lengths["DISTRIBUTION_CABLE"] += length
            cable_key = distribution_cable_summary_key(placemark_name, " ".join(folder_stack))
            if cable_key:
                self.dxf_summary_lengths[f"FO_{cable_key}"] += length
        elif normalized_source == "SLING WIRE":
            self.dxf_summary_lengths["SLING_WIRE"] += length
        elif normalized_source in {"SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"}:
            self.dxf_summary_lengths[dxf_safe_layer_name(normalized_source)] += length
            # Also track separately for HDPE in design summary
            if normalized_source == "SUBDUCT 40/34":
                self.dxf_summary_lengths["SUBDUCT_40"] += length
            elif normalized_source == "SUBDUCT 32/28":
                self.dxf_summary_lengths["SUBDUCT_32"] += length

    def _add_polyline(
        self,
        points: Sequence[Point],
        close: bool = False,
        dxfattribs: dict | None = None,
        width: float | None = None,
    ):
        clean_points = [(float(point[0]), float(point[1])) for point in points]
        if len(clean_points) < 2:
            return None

        attrs = self._supported_attrs(dxfattribs)
        if width and width > 0:
            width = float(width)
            polyline_points = [(x, y, width, width, 0.0) for x, y in clean_points]
            entity = self.modelspace.add_polyline2d(
                polyline_points,
                format="xyseb",
                close=close,
                dxfattribs=attrs,
            )
            self.fixed_width_polylines.append((entity, width))
            return self._remember_output(entity)

        entity = self.modelspace.add_polyline2d(clean_points, close=close, dxfattribs=attrs)
        return self._remember_output(entity)

    def _add_line(self, start: Point, end: Point, dxfattribs: dict | None = None):
        entity = self.modelspace.add_line(start, end, dxfattribs=self._supported_attrs(dxfattribs))
        return self._remember_output(entity)

    def _add_circle(self, center: Point, radius: float, dxfattribs: dict | None = None):
        entity = self.modelspace.add_circle(center, radius, dxfattribs=self._supported_attrs(dxfattribs))
        return self._remember_output(entity)

    def _add_text(
        self,
        text: str,
        insert: Point,
        height: float,
        dxfattribs: dict | None = None,
    ):
        if not text.strip():
            return None
        attrs = self._supported_attrs(dxfattribs)
        attrs["height"] = float(height)
        entity = self.modelspace.add_text(text.strip(), dxfattribs=attrs)
        entity.dxf.insert = (float(insert[0]), float(insert[1]), 0)
        return self._remember_output(entity)

    def _add_centered_text(
        self,
        text: str,
        center: Point,
        height: float,
        dxfattribs: dict | None = None,
        rotation: float | None = None,
    ):
        if not text.strip():
            return None
        attrs = self._supported_attrs(dxfattribs)
        attrs["height"] = float(height)
        attrs["width"] = 1.0
        # Callers can opt out of bold. HP COVER uses a single normal TEXT
        # entity so it never looks like an overprinted/doubled label.
        attrs.setdefault("style", "EVA_BOLD")
        if rotation is not None:
            attrs["rotation"] = float(rotation)
        entity = self.modelspace.add_text(text.strip(), dxfattribs=attrs)
        entity.set_placement((float(center[0]), float(center[1])), align=TextEntityAlignment.MIDDLE_CENTER)
        return self._remember_output(entity)


    def _add_distribution_cable_distance_labels(
        self,
        coordinates: Sequence[tuple[float, float]],
        projected: Sequence[Point],
    ) -> None:
        layer_name = self._ensure_layer(KML_DISTANCE_LABEL_LAYER, color=KML_DISTANCE_LABEL_COLOR)
        for (first_lonlat, second_lonlat), (first_projected, second_projected) in zip(
            zip(coordinates, coordinates[1:]),
            zip(projected, projected[1:]),
        ):
            direction = _unit_direction(first_projected, second_projected)
            if direction is None:
                continue
            try:
                _az12, _az21, distance = GEOD.inv(first_lonlat[0], first_lonlat[1], second_lonlat[0], second_lonlat[1])
            except Exception:
                continue
            if not math.isfinite(distance) or distance <= KML_DISTANCE_LABEL_MIN_METERS:
                continue

            normal = _left_normal(direction, 1.0)
            center = (
                (first_projected[0] + second_projected[0]) / 2 + normal[0] * KML_DISTANCE_LABEL_OFFSET_METERS,
                (first_projected[1] + second_projected[1]) / 2 + normal[1] * KML_DISTANCE_LABEL_OFFSET_METERS,
            )
            if any(
                _distance(center, existing_center) <= KML_DISTANCE_LABEL_DUPLICATE_RADIUS_METERS
                for existing_center in self.distance_label_centers
            ):
                continue
            rotation = math.degrees(math.atan2(direction[1], direction[0]))
            if rotation > 90:
                rotation -= 180
            elif rotation < -90:
                rotation += 180
            label = f"{int(round(distance))}m"
            if label == "0m":
                continue
            self._add_centered_text(
                label,
                center,
                KML_DISTANCE_LABEL_HEIGHT_METERS,
                dxfattribs={"layer": layer_name, "color": KML_DISTANCE_LABEL_COLOR},
                rotation=rotation,
            )
            self.distance_label_centers.append(center)
            self.kml_detail_stats["distance_labels"] += 1
            self.stats[layer_name] += 1

    def way(self, way) -> None:
        self.processed_ways += 1
        if self.progress_callback and self.processed_ways % 1000 == 0:
            self.progress_callback(self.processed_ways)

        try:
            layer = classify_tags(way.tags)
            if layer not in self.selected_layers:
                return

            lonlat_coordinates = self._extract_way_coordinates(way)
            if len(lonlat_coordinates) < 2:
                return

            is_closed = self._is_closed_way(lonlat_coordinates) and layer in POLYGON_LAYERS
            projected_coordinates = project_coordinates(lonlat_coordinates, self.transformer)

            if layer == "ROAD":
                if not should_export_road(way.tags, projected_coordinates):
                    return

                road_offset_meters = (
                    estimate_road_half_width_meters(way.tags)
                    if self.road_offset_meters is None
                    else self.road_offset_meters
                )
                if road_offset_meters > 0:
                    self._collect_road_buffer(projected_coordinates, road_offset_meters)
                else:
                    self._collect_road_line(projected_coordinates)
                self._queue_road_name(tag_value(way.tags, "name"), projected_coordinates)
            else:
                self._add_polyline(
                    projected_coordinates,
                    close=is_closed,
                    dxfattribs={"layer": layer},
                )

            self.stats[layer] += 1
            self._add_preview_feature(layer, way.tags, lonlat_coordinates, is_closed)
        except Exception as exc:
            LOGGER.debug("Way %s skipped: %s", getattr(way, "id", "unknown"), exc)


    def load_roads_from_cache(self, cache_path: Path, lonlat_bbox: tuple[float, float, float, float] | None = None) -> int:
        if "ROAD" not in self.selected_layers:
            return 0
        params: list[object] = []
        where = ""
        if lonlat_bbox is not None:
            min_lon, min_lat, max_lon, max_lat = lonlat_bbox
            where = "WHERE max_lon >= ? AND min_lon <= ? AND max_lat >= ? AND min_lat <= ?"
            params = [min_lon, max_lon, min_lat, max_lat]
        loaded = 0
        with sqlite3.connect(cache_path) as conn:
            rows = conn.execute(
                f"SELECT half_width, name, coordinates FROM roads {where}",
                params,
            )
            for half_width, name, coordinates_json in rows:
                try:
                    lonlat_coordinates = json.loads(coordinates_json)
                    projected_coordinates = project_coordinates(lonlat_coordinates, self.transformer)
                    if self.road_offset_meters is not None:
                        road_offset_meters = self.road_offset_meters
                    else:
                        road_offset_meters = float(half_width or 0)
                    if road_offset_meters > 0:
                        self._collect_road_buffer(projected_coordinates, road_offset_meters)
                    else:
                        self._collect_road_line(projected_coordinates)
                    self._queue_road_name(name, projected_coordinates)
                    self.stats["ROAD"] += 1
                    loaded += 1
                except Exception as exc:
                    LOGGER.debug("Cached road skipped: %s", exc)
        self.processed_ways += loaded
        return loaded

    def _extract_way_coordinates(self, way) -> list[tuple[float, float]]:
        coordinates: list[tuple[float, float]] = []
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
        return coordinates

    def _collect_road_buffer(self, projected_coordinates: list[Point], half_width_meters: float) -> None:
        clean_coordinates = _clean_points(projected_coordinates)
        if len(clean_coordinates) < 2:
            return

        line = LineString(clean_coordinates)
        if line.is_empty or line.length <= 0:
            return
        self.road_centerlines.append(line)

        # Buffer all road centerlines first, then union them at the end. This removes
        # internal crossing lines at intersections and produces cleaner CAD outlines.
        road_area = line.buffer(
            half_width_meters,
            cap_style=2,
            join_style=2,
            mitre_limit=2.0,
        )
        if not road_area.is_empty:
            self.road_buffers.append(road_area)

    def _collect_road_line(self, projected_coordinates: list[Point]) -> None:
        clean_coordinates = _clean_points(projected_coordinates)
        if len(clean_coordinates) < 2:
            return

        line = LineString(clean_coordinates)
        if not line.is_empty and line.length > 0:
            self.road_centerlines.append(line)
            self.road_lines.append(line)

    def _queue_road_name(self, name: str | None, projected_coordinates: list[Point]) -> None:
        clean_name = (name or "").strip()
        clean_coordinates = _clean_points(projected_coordinates)
        if not clean_name or len(clean_coordinates) < 2:
            return
        line = LineString(clean_coordinates)
        if line.is_empty or line.length < ROAD_NAME_LABEL_MIN_LENGTH_METERS:
            return
        self.road_name_candidates.append({"name": clean_name, "line": line})

    def set_road_clip_geometry(self, geometry) -> None:
        if geometry is None or geometry.is_empty:
            return
        self.road_clip_geometry = geometry.buffer(0)

    @staticmethod
    def _is_closed_way(coordinates: list[tuple[float, float]]) -> bool:
        if len(coordinates) < 4:
            return False
        first_lon, first_lat = coordinates[0]
        last_lon, last_lat = coordinates[-1]
        return math.isclose(first_lon, last_lon) and math.isclose(first_lat, last_lat)

    def _add_preview_feature(self, layer: str, tags, coordinates: list[tuple[float, float]], is_closed: bool) -> None:
        if len(self.preview_features) >= self.preview_feature_limit:
            return

        tag_map = tags_to_dict(tags)
        properties = {
            "layer": layer,
            "name": tag_map.get("name", ""),
            "osm_type": "way",
        }

        if is_closed:
            geometry = {"type": "Polygon", "coordinates": [coordinates]}
        else:
            geometry = {"type": "LineString", "coordinates": coordinates}

        self.preview_features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": geometry,
            }
        )

    def _kml_dxf_attrs(self, style: dict | None = None, color_key: str = "icon_color") -> dict:
        style = style or {}
        true_color = (
            style.get(color_key)
            or style.get("line_color")
            or style.get("poly_color")
            or style.get("icon_color")
            or KML_ICON_DEFAULT_TRUE_COLOR
        )
        attrs = {"layer": "KML", "true_color": int(true_color)}
        attrs["color"] = true_color_to_aci(int(true_color))
        line_width = style.get("line_width")
        if line_width:
            attrs["lineweight"] = int(_clamp(float(line_width) * 25, 0, 211))
        return attrs

    def _add_kml_icon(self, projected: Point, style: dict | None = None) -> None:
        style = style or {}
        x, y = projected
        radius = KML_ICON_SIZE_METERS * float(style.get("icon_scale") or 1.0) / 2
        attrs = self._kml_dxf_attrs(style, "icon_color")
        href = str(style.get("icon_href") or "").lower()

        if "triangle" in href:
            points = [
                (x, y + radius),
                (x - radius * 0.95, y - radius * 0.65),
                (x + radius * 0.95, y - radius * 0.65),
            ]
            self._add_polyline(points, close=True, dxfattribs=attrs)
            return

        if "target" in href or "cross-hairs" in href:
            self._add_circle((x, y), radius, dxfattribs=attrs)
            self._add_circle((x, y), radius * 0.45, dxfattribs=attrs)
            self._add_line((x - radius * 1.25, y), (x + radius * 1.25, y), dxfattribs=attrs)
            self._add_line((x, y - radius * 1.25), (x, y + radius * 1.25), dxfattribs=attrs)
            return

        if "homegardenbusiness" in href:
            body = [
                (x - radius * 0.75, y - radius * 0.65),
                (x + radius * 0.75, y - radius * 0.65),
                (x + radius * 0.75, y + radius * 0.25),
                (x - radius * 0.75, y + radius * 0.25),
            ]
            roof = [
                (x - radius * 0.95, y + radius * 0.18),
                (x, y + radius * 0.95),
                (x + radius * 0.95, y + radius * 0.18),
            ]
            self._add_polyline(body, close=True, dxfattribs=attrs)
            self._add_polyline(roof, close=False, dxfattribs=attrs)
            return

        if "pushpin" in href:
            self._add_circle((x, y + radius * 0.35), radius * 0.65, dxfattribs=attrs)
            self._add_line((x, y - radius * 1.0), (x, y + radius * 0.05), dxfattribs=attrs)
            return

        self._add_circle((x, y), radius, dxfattribs=attrs)

    def _ensure_kml_fat_block(self) -> None:
        layer_name = self._ensure_layer(KML_FAT_LAYER, KML_FAT_BORDER_COLOR)
        if KML_FAT_BLOCK_NAME in self.doc.blocks:
            if self._block_line_extent_matches(
                KML_FAT_BLOCK_NAME,
                KML_FAT_ICON_WIDTH_METERS,
                KML_FAT_ICON_HEIGHT_METERS,
            ):
                return
            self.doc.blocks.delete_block(KML_FAT_BLOCK_NAME, safe=False)

        block = self.doc.blocks.new(name=KML_FAT_BLOCK_NAME)
        border_attrs = {"layer": layer_name, "color": KML_FAT_BORDER_COLOR}
        text_attrs = {"layer": layer_name, "color": 7}

        def rectangle(width: float, height: float) -> None:
            left = -width / 2
            right = width / 2
            bottom = -height / 2
            top = height / 2
            block.add_line((left, bottom), (right, bottom), dxfattribs=border_attrs)
            block.add_line((right, bottom), (right, top), dxfattribs=border_attrs)
            block.add_line((right, top), (left, top), dxfattribs=border_attrs)
            block.add_line((left, top), (left, bottom), dxfattribs=border_attrs)

        rectangle(KML_FAT_ICON_WIDTH_METERS, KML_FAT_ICON_HEIGHT_METERS)
        rectangle(
            KML_FAT_ICON_WIDTH_METERS - KML_FAT_ICON_INNER_INSET_METERS * 2,
            KML_FAT_ICON_HEIGHT_METERS - KML_FAT_ICON_INNER_INSET_METERS * 2,
        )
        code = block.add_attdef(
            KML_FAT_ATTR_TAG,
            insert=(0, 0),
            text="FAT",
            height=KML_FAT_CODE_HEIGHT_METERS,
            dxfattribs=text_attrs,
        )
        code.set_placement((0, 0), align=TextEntityAlignment.MIDDLE_CENTER)

    def _block_line_extent_matches(self, block_name: str, width: float, height: float) -> bool:
        try:
            block = self.doc.blocks.get(block_name)
        except Exception:
            return False
        points: list[tuple[float, float]] = []
        for entity in block:
            if entity.dxftype() != "LINE":
                continue
            points.append((float(entity.dxf.start.x), float(entity.dxf.start.y)))
            points.append((float(entity.dxf.end.x), float(entity.dxf.end.y)))
        if not points:
            return False
        actual_width = max(x for x, _y in points) - min(x for x, _y in points)
        actual_height = max(y for _x, y in points) - min(y for _x, y in points)
        return math.isclose(actual_width, width, abs_tol=1e-6) and math.isclose(actual_height, height, abs_tol=1e-6)

    def _add_kml_fat_icon(self, projected: Point, code: str, style: dict | None = None, rotation: float = 0.0, normal_side: int = 1, offset_scale: float = 1.0) -> object:
        self._ensure_kml_fat_block()
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        x, y = projected
        pole_gap = (
            KML_POLE_ICON_SIZE_METERS / 2
            + KML_FAT_TO_POLE_GAP_METERS
            + KML_FAT_ICON_HEIGHT_METERS / 2
        ) * scale * float(offset_scale)
        side = 1 if int(normal_side or 1) >= 0 else -1
        # Local cable normal. AI may choose side/scale only; anchor remains deterministic.
        radians = math.radians(float(rotation))
        insert_point = (
            x - math.sin(radians) * pole_gap * side,
            y + math.cos(radians) * pole_gap * side,
        )
        attrs = self._supported_attrs(
            {
                "layer": KML_FAT_LAYER,
                "color": KML_FAT_BORDER_COLOR,
                "xscale": scale,
                "yscale": scale,
                "rotation": float(rotation),
            }
        )
        insert = self.modelspace.add_blockref(KML_FAT_BLOCK_NAME, insert_point, dxfattribs=attrs)
        insert.add_auto_attribs({KML_FAT_ATTR_TAG: code.strip() or "FAT"})
        for attrib in insert.attribs:
            attrib.dxf.layer = KML_FAT_LAYER
            attrib.dxf.color = 7
        self.fat_fdt_overlay_entities.append(insert)
        return self._remember_output(insert)

    def _ensure_kml_fdt_block(self) -> None:
        layer_name = self._ensure_layer(KML_FDT_LAYER, KML_FDT_BORDER_COLOR)
        if KML_FDT_BLOCK_NAME in self.doc.blocks:
            if self._block_line_extent_matches(
                KML_FDT_BLOCK_NAME,
                KML_FDT_ICON_WIDTH_METERS,
                KML_FDT_ICON_HEIGHT_METERS,
            ):
                return
            self.doc.blocks.delete_block(KML_FDT_BLOCK_NAME, safe=False)

        block = self.doc.blocks.new(name=KML_FDT_BLOCK_NAME)
        border_attrs = {"layer": layer_name, "color": KML_FDT_BORDER_COLOR}
        text_attrs = {"layer": layer_name, "color": 7}

        def rectangle(width: float, height: float) -> None:
            left = -width / 2
            right = width / 2
            bottom = -height / 2
            top = height / 2
            block.add_line((left, bottom), (right, bottom), dxfattribs=border_attrs)
            block.add_line((right, bottom), (right, top), dxfattribs=border_attrs)
            block.add_line((right, top), (left, top), dxfattribs=border_attrs)
            block.add_line((left, top), (left, bottom), dxfattribs=border_attrs)

        rectangle(KML_FDT_ICON_WIDTH_METERS, KML_FDT_ICON_HEIGHT_METERS)
        rectangle(
            KML_FDT_ICON_WIDTH_METERS - KML_FDT_ICON_INNER_INSET_METERS * 2,
            KML_FDT_ICON_HEIGHT_METERS - KML_FDT_ICON_INNER_INSET_METERS * 2,
        )
        code = block.add_attdef(
            KML_FDT_ATTR_TAG,
            insert=(0, 0),
            text="FDT",
            height=KML_FDT_TEXT_HEIGHT_METERS,
            dxfattribs=text_attrs,
        )
        code.set_placement((0, 0), align=TextEntityAlignment.MIDDLE_CENTER)

    def _fdt_label_height(self, label: str, scale: float) -> float:
        available_width = KML_FDT_ICON_WIDTH_METERS * 1.6 * scale
        width_limited_height = available_width / max(len(label), 1) / KML_POLE_TEXT_WIDTH_FACTOR
        return _clamp(
            min(KML_FDT_LABEL_HEIGHT_METERS * scale, width_limited_height),
            0.72 * scale,
            KML_FDT_LABEL_HEIGHT_METERS * scale,
        )

    def _add_kml_fdt_icon(self, projected: Point, label: str, style: dict | None = None) -> None:
        self._ensure_kml_fdt_block()
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        x, y = projected
        insert_point = (
            x,
            y
            + (KML_POLE_ICON_SIZE_METERS / 2 + KML_FAT_TO_POLE_GAP_METERS + KML_FDT_ICON_HEIGHT_METERS / 2)
            * scale,
        )
        attrs = self._supported_attrs(
            {
                "layer": KML_FDT_LAYER,
                "color": KML_FDT_BORDER_COLOR,
                "xscale": scale,
                "yscale": scale,
            }
        )
        insert = self.modelspace.add_blockref(KML_FDT_BLOCK_NAME, insert_point, dxfattribs=attrs)
        insert.add_auto_attribs({KML_FDT_ATTR_TAG: "FDT"})
        for attrib in insert.attribs:
            attrib.dxf.layer = KML_FDT_LAYER
            attrib.dxf.color = 7
        self.fat_fdt_overlay_entities.append(insert)
        self._remember_output(insert)

        clean_label = (label or "").strip()
        if clean_label:
            label_height = self._fdt_label_height(clean_label, scale)
            label_center = (
                x,
                insert_point[1] + (KML_FDT_ICON_HEIGHT_METERS / 2 + KML_FDT_LABEL_GAP_METERS) * scale + label_height / 2,
            )
            label_text = self._add_centered_text(
                clean_label,
                label_center,
                label_height,
                dxfattribs={"layer": KML_FDT_LAYER, "color": KML_FDT_BORDER_COLOR},
            )
            if label_text is not None:
                self.fat_fdt_overlay_entities.append(label_text)

    def _ensure_kml_olt_block(self) -> None:
        layer_name = self._ensure_layer(KML_OLT_LAYER, KML_OLT_COLOR)
        if KML_OLT_BLOCK_NAME in self.doc.blocks:
            if self._block_line_extent_matches(KML_OLT_BLOCK_NAME, KML_OLT_ICON_WIDTH_METERS, KML_OLT_ICON_HEIGHT_METERS):
                return
            self.doc.blocks.delete_block(KML_OLT_BLOCK_NAME, safe=False)

        block = self.doc.blocks.new(name=KML_OLT_BLOCK_NAME)
        attrs = {"layer": layer_name, "color": KML_OLT_COLOR}
        width = KML_OLT_ICON_WIDTH_METERS
        height = KML_OLT_ICON_HEIGHT_METERS
        left, right = -width / 2, width / 2
        bottom, top = -height / 2, height / 2
        self._add_block_rectangle(block, width, height, attrs)

        tri_left = left + width * 0.12
        tri_right = left + width * 0.41
        tri_top = top - height * 0.18
        tri_bottom = bottom + height * 0.18
        block.add_lwpolyline([(tri_left, tri_bottom), (tri_left, tri_top), (tri_right, 0), (tri_left, tri_bottom)], dxfattribs=attrs)
        for index in range(11):
            fraction = (index + 1) / 12
            x = tri_left + (tri_right - tri_left) * fraction
            half_span = (tri_top - tri_bottom) * (1 - fraction) / 2
            block.add_line((x, -half_span), (x, half_span), dxfattribs=attrs)

        circle_center = (left + width * 0.74, 0.0)
        circle_radius = min(width, height) * 0.28
        block.add_circle(circle_center, circle_radius, dxfattribs=attrs)

        def lightning(points: list[Point]) -> None:
            block.add_lwpolyline([(circle_center[0] + x, circle_center[1] + y) for x, y in points], dxfattribs=attrs)

        lightning([
            (-circle_radius * 0.62, -circle_radius * 0.20),
            (circle_radius * 0.18, circle_radius * 0.62),
            (-circle_radius * 0.08, circle_radius * 0.34),
            (circle_radius * 0.45, circle_radius * 0.74),
            (circle_radius * 0.02, circle_radius * 0.18),
            (circle_radius * 0.22, circle_radius * 0.20),
        ])
        lightning([
            (circle_radius * 0.70, circle_radius * 0.16),
            (-circle_radius * 0.08, -circle_radius * 0.66),
            (circle_radius * 0.16, -circle_radius * 0.38),
            (-circle_radius * 0.38, -circle_radius * 0.78),
            (circle_radius * 0.04, -circle_radius * 0.20),
            (-circle_radius * 0.16, -circle_radius * 0.22),
        ])

    def _add_kml_olt_icon(self, projected: Point, style: dict | None = None, rotation: float = 0.0) -> object:
        self._ensure_kml_olt_block()
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        attrs = self._supported_attrs({
            "layer": KML_OLT_LAYER,
            "color": KML_OLT_COLOR,
            "xscale": scale,
            "yscale": scale,
            "rotation": float(rotation),
        })
        insert = self.modelspace.add_blockref(KML_OLT_BLOCK_NAME, projected, dxfattribs=attrs)
        return self._remember_output(insert)

    def _add_block_rectangle(self, block, width: float, height: float, dxfattribs: dict) -> None:
        left = -width / 2
        right = width / 2
        bottom = -height / 2
        top = height / 2
        block.add_line((left, bottom), (right, bottom), dxfattribs=dxfattribs)
        block.add_line((right, bottom), (right, top), dxfattribs=dxfattribs)
        block.add_line((right, top), (left, top), dxfattribs=dxfattribs)
        block.add_line((left, top), (left, bottom), dxfattribs=dxfattribs)

    def _ensure_kml_handhole_block(self, spec: dict) -> str:
        color = int(spec.get("color") or KML_HANDHOLE_COLOR)
        icon_size = float(spec.get("icon_size") or KML_HANDHOLE_ICON_SIZE_METERS)
        layer_name = self._ensure_layer(str(spec["layer"]), color)
        block_name = str(spec["block"])
        if block_name in self.doc.blocks:
            return block_name
        block = self.doc.blocks.new(name=block_name)
        attrs = {"layer": layer_name, "color": color}
        if block_name == "HANDHOLE_PIT_20_BLOCK":
            # UG HH20 template: double blue square with SH only. No callout,
            # no size/name text; source point remains the insertion anchor.
            inner = icon_size - KML_HANDHOLE_ICON_INSET_METERS * 2
            self._add_block_rectangle(block, icon_size, icon_size, attrs)
            self._add_block_rectangle(block, inner, inner, attrs)
            mark_attrs = {**attrs, "height": inner * 0.42, "style": "EVA_BOLD"}
            mark = block.add_text("SH", dxfattribs=mark_attrs)
            mark.set_placement((0, 0), align=TextEntityAlignment.MIDDLE_CENTER)
            return block_name

        self._add_block_rectangle(block, icon_size, icon_size, attrs)
        inner_size = icon_size - KML_HANDHOLE_ICON_INSET_METERS * 2
        self._add_block_rectangle(block, inner_size, inner_size, attrs)
        if spec.get("show_text") is False:
            return block_name
        label_attrs = {**attrs, "height": KML_POLE_ID_HEIGHT_METERS, "style": "EVA_BOLD"}
        top=block.add_text("HH",dxfattribs=label_attrs)
        top.set_placement((0,icon_size*.18),align=TextEntityAlignment.MIDDLE_CENTER)
        bottom=block.add_text(str(spec["size_text"]),dxfattribs=label_attrs)
        bottom.set_placement((0,-icon_size*.22),align=TextEntityAlignment.MIDDLE_CENTER)
        return block_name

    def _add_kml_handhole_icon(
        self,
        projected: Point,
        spec: dict,
        style: dict | None = None,
        rotation: float = 0.0,
    ) -> object:
        block_name = self._ensure_kml_handhole_block(spec)
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        color = int(spec.get("color") or KML_HANDHOLE_COLOR)
        layer_name = self._ensure_layer(str(spec["layer"]), color)
        attrs = self._supported_attrs(
            {
                "layer": layer_name,
                "color": color,
                "xscale": scale,
                "yscale": scale,
                "rotation": float(rotation),
            }
        )
        insert = self.modelspace.add_blockref(block_name, projected, dxfattribs=attrs)
        return self._remember_output(insert)

    def _ensure_kml_twey_block(self) -> None:
        layer_name = self._ensure_layer(KML_TWEY_LAYER, KML_TWEY_COLOR)
        if KML_TWEY_BLOCK_NAME in self.doc.blocks:
            return

        block = self.doc.blocks.new(name=KML_TWEY_BLOCK_NAME)
        attrs = {"layer": layer_name, "color": KML_TWEY_COLOR}
        self._add_block_rectangle(block, KML_TWEY_ICON_SIZE_METERS, KML_TWEY_ICON_SIZE_METERS, attrs)
        text = block.add_text("T-WAY", dxfattribs={**attrs, "height": KML_TWEY_TEXT_HEIGHT_METERS})
        text.set_placement((0, 0), align=TextEntityAlignment.MIDDLE_CENTER)

    def _add_kml_twey_icon(self, projected: Point, style: dict | None = None, rotation: float = 0.0) -> object:
        self._ensure_kml_twey_block()
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        attrs = self._supported_attrs(
            {
                "layer": KML_TWEY_LAYER,
                "color": KML_TWEY_COLOR,
                "xscale": scale,
                "yscale": scale,
                "rotation": float(rotation),
            }
        )
        insert = self.modelspace.add_blockref(KML_TWEY_BLOCK_NAME, projected, dxfattribs=attrs)
        self.twey_overlay_entities.append(insert)
        return self._remember_output(insert)

    def _line_angle_at_position(self, line: LineString, position: float) -> float:
        delta = min(max(line.length * 0.08, 1.0), 8.0)
        start = line.interpolate(max(0.0, position - delta))
        end = line.interpolate(min(line.length, position + delta))
        if start.distance(end) <= 1e-9:
            start = line.interpolate(0.0)
            end = line.interpolate(line.length)
        return self._readable_angle(math.degrees(math.atan2(end.y - start.y, end.x - start.x)))

    def _nearest_cable_frame(self, projected: Point) -> dict:
        reference_lines = [
            line
            for line in (self.kml_cable_centerlines or self.kml_line_centerlines)
            if not line.is_empty and line.length > 0
        ]
        if not reference_lines:
            return {"point": projected, "rotation": 0.0}

        point = ShapelyPoint(projected)
        radius = KML_TWEY_CABLE_SEARCH_RADIUS_METERS
        candidates: list[LineString] = []
        for _attempt in range(3):
            candidates = [line for line in reference_lines if point.distance(line) <= radius]
            if candidates:
                break
            radius *= 2
        if not candidates:
            candidates = reference_lines

        best_line = min(candidates, key=lambda line: point.distance(line))
        position = best_line.project(point)
        snapped = best_line.interpolate(position)
        return {
            # All cable-associated assets use the cable projection as anchor,
            # so symbols and labels do not drift away from the actual route.
            "point": (float(snapped.x), float(snapped.y)),
            "rotation": self._line_angle_at_position(best_line, position),
        }

    def _queue_kml_fat_icon(
        self,
        projected: Point,
        code: str,
        name: str = "",
        style: dict | None = None,
        fdt_number: int | None = None,
    ) -> str:
        clean_code = code.strip() or "FAT"
        self.pending_fat_icons.append(
            {
                "projected": projected,
                "code": clean_code,
                "name": name,
                "style": dict(style or {}),
                "fdt_number": fdt_number,
            }
        )
        return clean_code

    def _queue_kml_handhole_icon(
        self,
        projected: Point,
        spec: dict,
        name: str = "",
        style: dict | None = None,
        fdt_number: int | None = None,
    ) -> str:
        self.pending_handhole_icons.append(
            {
                "projected": projected,
                "spec": dict(spec),
                "name": name,
                "style": dict(style or {}),
                "fdt_number": fdt_number,
            }
        )
        return name or str((spec.get("label_lines") or ("HANDHOLE",))[0])

    def _queue_kml_twey_icon(
        self,
        projected: Point,
        name: str = "",
        style: dict | None = None,
        fdt_number: int | None = None,
    ) -> str:
        self.pending_twey_points.append(
            {
                "projected": projected,
                "name": name,
                "style": dict(style or {}),
                "fdt_number": fdt_number,
            }
        )
        return name or "T-WAY"

    def _queue_kml_olt_icon(
        self,
        projected: Point,
        name: str = "",
        style: dict | None = None,
        fdt_number: int | None = None,
    ) -> str:
        self.pending_olt_icons.append({
            "projected": projected,
            "name": name,
            "style": dict(style or {}),
            "fdt_number": fdt_number,
        })
        return name or "OLT"

    def finalize_olt_icons(self, pipeline_logger=None) -> None:
        if not self.pending_olt_icons:
            return
        for pending in self.pending_olt_icons:
            output_start_index = len(self.output_entities)
            self._add_kml_olt_icon(
                pending["projected"],
                pending.get("style") or {},
                rotation=self._olt_rotation_to_nearest_road(pending["projected"]),
            )
            self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])

    def finalize_fat_icons(self, pipeline_logger=None) -> None:
        if not self.pending_fat_icons:
            return

        for pending in self.pending_fat_icons:
            output_start_index = len(self.output_entities)
            source_point = pending["projected"]
            frame = self._nearest_cable_frame(source_point)
            # FAT terminates at its serving pole. Resolve against the original
            # FAT coordinate, never the projected cable point, to avoid cluster.
            anchor = self._snap_to_nearest_pole(source_point)
            rotation = float(frame.get("rotation") or 0.0)
            ai_asset = None
            try:
                ai_asset = suggest_dxf_asset_layout(
                    asset_type="FAT",
                    asset_name=str(pending.get("code") or "FAT"),
                    cable_angle=rotation,
                    has_serving_pole=anchor != source_point,
                )
            except Exception as exc:
                LOGGER.debug("FAT AI layout fallback: %s", exc)
            insert = self._add_kml_fat_icon(
                anchor,
                str(pending.get("code") or "FAT"),
                pending.get("style") or {},
                rotation=rotation,
                normal_side=int((ai_asset or {}).get("normal_side", 1)),
                offset_scale=float((ai_asset or {}).get("offset_scale", 1.0)),
            )
            self.fat_callout_refs.append(
                {
                    "fdt_number": pending.get("fdt_number"),
                    "code": str(pending.get("code") or "FAT"),
                    "name": str(pending.get("name") or ""),
                    "anchor": (float(insert.dxf.insert.x), float(insert.dxf.insert.y)),
                }
            )
            self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])

    def finalize_handhole_icons(self, pipeline_logger=None) -> None:
        if not self.pending_handhole_icons:
            return

        for pending in self.pending_handhole_icons:
            output_start_index = len(self.output_entities)
            source_point = pending["projected"]
            frame = self._nearest_cable_frame(source_point)
            self._add_kml_handhole_icon(
                source_point,
                pending["spec"],
                pending.get("style") or {},
                rotation=float(frame.get("rotation") or 0.0),
            )
            self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])

    def finalize_twey_icons(self, pipeline_logger=None) -> None:
        if not self.pending_twey_points:
            return

        total_twey = len(self.pending_twey_points)
        progress_interval = progress_log_interval(total_twey)
        for index, pending in enumerate(self.pending_twey_points, start=1):
            if pipeline_logger is not None and index > 1 and (index - 1) % progress_interval == 0:
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"[3/6] T-WAY placement progress: {index - 1:,}/{total_twey:,} block(s).",
                )
            output_start_index = len(self.output_entities)
            frame = self._nearest_cable_frame(pending["projected"])
            self._add_kml_twey_icon(
                frame["point"],
                pending.get("style") or {},
                rotation=float(frame.get("rotation") or 0.0),
            )
            self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])

    def _kml_pole_block_name(self, spec: dict) -> str:
        if spec.get("kind") == "existing":
            return f"{KML_POLE_BLOCK_PREFIX}_EXISTING"
        label = dxf_safe_layer_name(str(spec.get("label") or "NEW_POLE"), "NEW_POLE")
        return f"{KML_POLE_BLOCK_PREFIX}_{label}"[:31]

    def _ensure_kml_pole_block(self, spec: dict) -> str:
        block_name = self._kml_pole_block_name(spec)
        if block_name in self.doc.blocks:
            return block_name

        block = self.doc.blocks.new(name=block_name)
        color = int(spec["color"])
        attrs = {"layer": "KML", "color": color}
        radius = KML_POLE_ICON_SIZE_METERS / 2
        block.add_circle((0, 0), radius, dxfattribs=attrs)

        if spec.get("kind") == "existing":
            diagonal = radius * 0.72
            block.add_line((-diagonal, -diagonal), (diagonal, diagonal), dxfattribs=attrs)
            block.add_line((-diagonal, diagonal), (diagonal, -diagonal), dxfattribs=attrs)
        else:
            text_attrs = dict(attrs)
            text_attrs["height"] = KML_POLE_INNER_TEXT_HEIGHT_METERS
            top = block.add_text(str(spec["top"]), dxfattribs=text_attrs)
            top.set_placement((0, radius * 0.28), align=TextEntityAlignment.MIDDLE_CENTER)
            bottom = block.add_text(str(spec["bottom"]), dxfattribs=text_attrs)
            bottom.set_placement((0, -radius * 0.45), align=TextEntityAlignment.MIDDLE_CENTER)

        return block_name

    def _ellipse_points(
        self,
        center_x: float,
        center_y: float,
        radius_x: float,
        radius_y: float,
        segments: int = 48,
    ) -> list[Point]:
        return [
            (
                center_x + math.cos(2 * math.pi * index / segments) * radius_x,
                center_y + math.sin(2 * math.pi * index / segments) * radius_y,
            )
            for index in range(segments)
        ]

    def _ensure_kml_slack_hanger_block(self) -> None:
        if KML_SLACK_HANGER_BLOCK_NAME in self.doc.blocks:
            return

        block = self.doc.blocks.new(name=KML_SLACK_HANGER_BLOCK_NAME)
        attrs = {"layer": "KML", "color": KML_SLACK_HANGER_COLOR}
        radius_x = KML_SLACK_HANGER_WIDTH_METERS / 4
        radius_y = KML_SLACK_HANGER_HEIGHT_METERS / 2
        for center_x in (-radius_x, radius_x):
            points = self._ellipse_points(center_x, 0, radius_x, radius_y)
            block.add_polyline2d(points, close=True, dxfattribs=attrs)

    def _add_kml_slack_hanger_icon(self, projected: Point, style: dict | None = None) -> None:
        self._ensure_kml_slack_hanger_block()
        # Preserve the asset's actual coordinate. Cable frame is for rotation
        # only; projecting every asset onto a line makes nearby assets collapse.
        frame = self._nearest_cable_frame(projected)
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        attrs = self._supported_attrs(
            {
                "layer": "KML",
                "color": KML_SLACK_HANGER_COLOR,
                "xscale": scale,
                "yscale": scale,
            }
        )
        insert = self.modelspace.add_blockref(KML_SLACK_HANGER_BLOCK_NAME, projected, dxfattribs=attrs)
        try:
            insert.dxf.rotation = float(frame.get("rotation") or 0.0)
        except Exception:
            pass
        self._remember_output(insert)

    def _ensure_kml_joint_closure_block(self) -> None:
        """Build the Joint Closure block: circle with a solid-filled bowtie inside.

        Two filled triangles meeting at the centre, enclosed by a circle. All
        geometry is placed on the CLOSURE layer (red) so it follows the
        template's existing layer style.
        """
        if KML_JOINT_CLOSURE_BLOCK_NAME in self.doc.blocks:
            return

        # Make sure the layer exists (template ships with CLOSURE; fallback creates it).
        layer_name = self._ensure_layer(KML_JOINT_CLOSURE_LAYER, KML_JOINT_CLOSURE_COLOR)
        block = self.doc.blocks.new(name=KML_JOINT_CLOSURE_BLOCK_NAME)
        attrs = {"layer": layer_name, "color": KML_JOINT_CLOSURE_COLOR}
        radius = KML_JOINT_CLOSURE_DIAMETER_METERS / 2
        block.add_circle((0.0, 0.0), radius, dxfattribs=attrs)

        # Bowtie inscribed in the circle: two triangles meeting at the centre.
        # The triangle base sits on the chord whose endpoints are the
        # 30??/-30?? / 150??/-150?? points on the circle.
        cos30 = math.cos(math.radians(30))
        sin30 = math.sin(math.radians(30))
        right_top = (radius * cos30, radius * sin30)
        right_bottom = (radius * cos30, -radius * sin30)
        left_top = (-radius * cos30, radius * sin30)
        left_bottom = (-radius * cos30, -radius * sin30)
        centre = (0.0, 0.0)

        right_triangle = [right_top, right_bottom, centre]
        left_triangle = [left_top, left_bottom, centre]
        for triangle in (left_triangle, right_triangle):
            block.add_polyline2d(triangle, close=True, dxfattribs=attrs)
            try:
                hatch = block.add_hatch(color=KML_JOINT_CLOSURE_COLOR, dxfattribs={"layer": layer_name})
                hatch.set_solid_fill(color=KML_JOINT_CLOSURE_COLOR)
                hatch.paths.add_polyline_path(triangle + [triangle[0]], is_closed=True)
            except Exception as exc:
                LOGGER.debug("Joint closure hatch skipped: %s", exc)

    def _add_kml_joint_closure_icon(self, projected: Point, style: dict | None = None) -> None:
        self._ensure_kml_joint_closure_block()
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        attrs = self._supported_attrs(
            {
                "layer": KML_JOINT_CLOSURE_LAYER,
                "color": KML_JOINT_CLOSURE_COLOR,
                "xscale": scale,
                "yscale": scale,
            }
        )
        insert = self.modelspace.add_blockref(KML_JOINT_CLOSURE_BLOCK_NAME, projected, dxfattribs=attrs)
        self._remember_output(insert)

    def _snap_to_nearest_pole(self, projected: Point) -> Point:
        """If a pole is within KML_POLE_SNAP_RADIUS_METERS, return its centre."""
        if not self.pole_positions:
            return projected
        x, y = float(projected[0]), float(projected[1])
        radius = KML_POLE_SNAP_RADIUS_METERS
        best = None
        best_dist = radius * radius
        for px, py in self.pole_positions:
            dx = px - x
            dy = py - y
            d2 = dx * dx + dy * dy
            if d2 <= best_dist:
                best_dist = d2
                best = (px, py)
        return best if best is not None else projected

    def finalize_pole_snapped_icons(self) -> None:
        """Draw queued slack hangers and joint closures, snapping each to the
        nearest pole centre when one is within KML_POLE_SNAP_RADIUS_METERS.

        Also snaps cable polyline endpoints to nearby pole centres so the cable
        visually attaches to the pole.
        """
        for pending in self.pending_slack_icons:
            projected = self._snap_to_nearest_pole(pending["projected"])
            self._add_kml_slack_hanger_icon(projected, pending.get("style"))
        self.pending_slack_icons.clear()

        for pending in self.pending_joint_closure_icons:
            projected = self._snap_to_nearest_pole(pending["projected"])
            self._add_kml_joint_closure_icon(projected, pending.get("style"))
        self.pending_joint_closure_icons.clear()

        # Snap cable endpoints (LWPOLYLINE / 2D POLYLINE) to nearby poles.
        for entity in self.cable_endpoint_snap_entities:
            try:
                self._snap_polyline_endpoints_to_poles(entity)
            except Exception as exc:
                LOGGER.debug("Cable endpoint snap skipped: %s", exc)

    def _snap_polyline_endpoints_to_poles(self, entity) -> None:
        if not self.pole_positions:
            return
        dxftype = entity.dxftype()
        if dxftype == "LWPOLYLINE":
            try:
                points = list(entity.get_points("xyseb"))
            except Exception:
                return
            if len(points) < 2:
                return
            updated = False
            for index in (0, len(points) - 1):
                x, y = float(points[index][0]), float(points[index][1])
                snapped = self._snap_to_nearest_pole((x, y))
                if snapped != (x, y):
                    rest = points[index][2:]
                    points[index] = (float(snapped[0]), float(snapped[1])) + tuple(rest)
                    updated = True
            if updated:
                try:
                    entity.set_points(points, format="xyseb")
                except Exception:
                    pass
        elif dxftype == "POLYLINE":
            try:
                vertices = list(entity.vertices)
            except Exception:
                return
            if len(vertices) < 2:
                return
            for index in (0, len(vertices) - 1):
                vertex = vertices[index]
                try:
                    loc = vertex.dxf.location
                    snapped = self._snap_to_nearest_pole((float(loc.x), float(loc.y)))
                    if (float(loc.x), float(loc.y)) != snapped:
                        vertex.dxf.location = (float(snapped[0]), float(snapped[1]), float(getattr(loc, "z", 0.0)))
                except Exception:
                    continue

    def _queue_kml_pole_id(
        self,
        name: str,
        projected: Point,
        radius: float,
        scale: float,
        fdt_number: int | None = None,
    ) -> None:
        if not name.strip():
            return

        self.pending_pole_ids.append(
            {
                "name": name,
                "projected": projected,
                "radius": radius,
                "scale": scale,
                "fdt_number": fdt_number,
            }
        )

    def _pole_id_reference_lines(self) -> list[LineString]:
        return [line for line in [*self.road_centerlines, *self.kml_line_centerlines] if not line.is_empty and line.length > 0]

    def _nearest_pole_id_line_angle(self, projected: Point) -> float:
        reference_lines = self._pole_id_reference_lines()
        if not reference_lines:
            return 0.0

        point = ShapelyPoint(projected)
        radius = KML_POLE_ID_REFERENCE_SEARCH_RADIUS_METERS
        candidates: list[LineString] = []
        for _attempt in range(3):
            candidates = [line for line in reference_lines if point.distance(line) <= radius]
            if candidates:
                break
            radius *= 2
        if not candidates:
            candidates = reference_lines

        best_line = min(candidates, key=lambda line: point.distance(line))
        position = best_line.project(point)
        delta = min(max(best_line.length * 0.08, 1.0), 8.0)
        start = best_line.interpolate(max(0.0, position - delta))
        end = best_line.interpolate(min(best_line.length, position + delta))
        if start.distance(end) <= 1e-9:
            start = best_line.interpolate(0.0)
            end = best_line.interpolate(best_line.length)
        return self._readable_angle(math.degrees(math.atan2(end.y - start.y, end.x - start.x)))

    def _draw_kml_pole_id(self, pending: dict) -> object | None:
        name = str(pending.get("name") or "").strip()
        if not name:
            return None

        projected = pending["projected"]
        radius = float(pending["radius"])
        scale = float(pending["scale"])
        label_height = KML_POLE_ID_HEIGHT_METERS * scale
        rotation = self._nearest_pole_id_line_angle(projected)
        x, y = projected
        offset = radius + (KML_POLE_ID_GAP_METERS * scale) + (label_height / 2)
        label_center = (
            x - math.sin(math.radians(rotation)) * offset,
            y + math.cos(math.radians(rotation)) * offset,
        )
        return self._add_centered_text(
            name,
            label_center,
            label_height,
            dxfattribs={"layer": "POLE_ID", "color": 1},
            rotation=rotation,
        )

    def finalize_pole_ids(self, pipeline_logger=None) -> None:
        if not self.pending_pole_ids:
            return

        total_pole_ids = len(self.pending_pole_ids)
        progress_interval = progress_log_interval(total_pole_ids)
        for index, pending in enumerate(self.pending_pole_ids, start=1):
            if pipeline_logger is not None and index > 1 and (index - 1) % progress_interval == 0:
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"[3/6] POLE_ID placement progress: {index - 1:,}/{total_pole_ids:,} label(s).",
                )
            output_start_index = len(self.output_entities)
            entity = self._draw_kml_pole_id(pending)
            if entity is not None:
                self.stats["POLE_ID"] += 1
                self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])

    def _add_centered_kml_pole_text(
        self,
        text: str,
        center_x: float,
        baseline_y: float,
        height: float,
        dxfattribs: dict,
    ) -> None:
        estimated_width = max(len(text), 1) * height * KML_POLE_TEXT_WIDTH_FACTOR
        self._add_text(text, (center_x - estimated_width / 2, baseline_y), height, dxfattribs=dxfattribs)

    def _add_kml_pole_icon(
        self,
        projected: Point,
        spec: dict,
        name: str = "",
        style: dict | None = None,
        fdt_number: int | None = None,
    ) -> None:
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        radius = KML_POLE_ICON_SIZE_METERS * scale / 2
        block_name = self._ensure_kml_pole_block(spec)
        attrs = self._supported_attrs(
            {
                "layer": "KML",
                "color": int(spec["color"]),
                "xscale": scale,
                "yscale": scale,
            }
        )
        # Preserve actual pole position; cable angle only controls rotation.
        frame = self._nearest_cable_frame(projected)
        insert = self.modelspace.add_blockref(block_name, projected, dxfattribs=attrs)
        try:
            insert.dxf.rotation = float(frame.get("rotation") or 0.0)
        except Exception:
            pass
        self._remember_output(insert)

        self.pole_positions.append((float(projected[0]), float(projected[1])))
        self._queue_kml_pole_id(name, projected, radius, scale, fdt_number=fdt_number)

    def _queue_home_number(self, projected: Point, name: str, fdt_number: int | None = None) -> str:
        text = (name or "").strip() or "HP"
        self.pending_home_numbers.append({"text": text, "projected": projected, "fdt_number": fdt_number})
        return text

    def _queue_uncover_home_number(self, projected: Point, name: str, fdt_number: int | None = None) -> str:
        text = (name or "").strip() or "HPU"
        self.pending_uncover_home_numbers.append({"text": text, "projected": projected, "fdt_number": fdt_number})
        return text

    def _home_number_box_size(self, text: str) -> tuple[float, float]:
        text_width = max(len(text), 1) * KML_HOME_NUMBER_HEIGHT_METERS * KML_HOME_NUMBER_TEXT_WIDTH_FACTOR
        return (
            _clamp(
                text_width + KML_HOME_NUMBER_PADDING_X_METERS * 2,
                KML_HOME_NUMBER_MIN_WIDTH_METERS,
                KML_HOME_NUMBER_MAX_WIDTH_METERS,
            ),
            _clamp(
                KML_HOME_NUMBER_HEIGHT_METERS + KML_HOME_NUMBER_PADDING_Y_METERS * 2,
                KML_HOME_NUMBER_MIN_HEIGHT_METERS,
                KML_HOME_NUMBER_MAX_HEIGHT_METERS,
            ),
        )

    @staticmethod
    def _readable_angle(angle: float) -> float:
        while angle > 90:
            angle -= 180
        while angle < -90:
            angle += 180
        return angle

    def _home_number_box_polygon(
        self,
        center: Point,
        width: float,
        height: float,
        rotation: float = 0.0,
    ) -> Polygon:
        x, y = center
        half_width = width / 2
        half_height = height / 2
        corners = [
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        ]
        if abs(rotation) <= 1e-6:
            return Polygon([(x + dx, y + dy) for dx, dy in corners])

        radians = math.radians(rotation)
        cos_angle = math.cos(radians)
        sin_angle = math.sin(radians)
        return Polygon(
            [
                (
                    x + dx * cos_angle - dy * sin_angle,
                    y + dx * sin_angle + dy * cos_angle,
                )
                for dx, dy in corners
            ]
        )

    def _build_road_centerline_index(self) -> None:
        if self._road_centerline_tree is not None or not self.road_centerlines or STRtree is None:
            return
        self._road_centerline_tree = STRtree(self.road_centerlines)

    def _query_indexed_geometries(self, tree, geometries: Sequence, probe) -> list:
        queried = tree.query(probe)
        results = []
        for item in queried:
            if hasattr(item, "geom_type"):
                results.append(item)
                continue
            try:
                results.append(geometries[int(item)])
            except Exception:
                continue
        return results

    def _road_centerline_candidates(self, projected: Point) -> list[LineString]:
        if not self.road_centerlines:
            return []
        self._build_road_centerline_index()
        if self._road_centerline_tree is None:
            return self.road_centerlines

        point = ShapelyPoint(projected)
        radius = KML_HOME_NUMBER_ROTATION_SEARCH_RADIUS_METERS
        for _attempt in range(3):
            candidates = self._query_indexed_geometries(
                self._road_centerline_tree,
                self.road_centerlines,
                point.buffer(radius),
            )
            if candidates:
                return candidates
            radius *= 2
        return self.road_centerlines

    def _nearest_road_frame(self, projected: Point) -> dict | None:
        candidates = self._road_centerline_candidates(projected)
        if not candidates:
            return None

        point = ShapelyPoint(projected)
        best_line = min(candidates, key=lambda line: point.distance(line))
        if best_line.is_empty or best_line.length <= 0:
            return None

        return self._home_number_frame_from_line(projected, best_line, face_towards_line=False)

    def _home_number_kml_reference_lines(self) -> list[LineString]:
        lines: list[LineString] = []
        seen: set[int] = set()
        for line in [*self.kml_cable_centerlines, *self.kml_line_centerlines]:
            if line.is_empty or line.length <= 0:
                continue
            line_id = id(line)
            if line_id in seen:
                continue
            seen.add(line_id)
            lines.append(line)
        return lines

    def _home_number_reference_candidates(self, projected: Point) -> list[LineString]:
        point = ShapelyPoint(projected)
        kml_lines = self._home_number_kml_reference_lines() if self.network_type == "UG" else []
        radius = KML_HOME_NUMBER_ROTATION_SEARCH_RADIUS_METERS

        for _attempt in range(3):
            candidates: list[LineString] = []
            if self.road_centerlines:
                self._build_road_centerline_index()
                if self._road_centerline_tree is not None:
                    candidates.extend(
                        self._query_indexed_geometries(
                            self._road_centerline_tree,
                            self.road_centerlines,
                            point.buffer(radius),
                        )
                    )
                else:
                    candidates.extend(
                        line
                        for line in self.road_centerlines
                        if not line.is_empty and line.length > 0 and point.distance(line) <= radius
                    )
            candidates.extend(
                line
                for line in kml_lines
                if point.distance(line) <= radius
            )
            clean_candidates = [
                line
                for line in candidates
                if not line.is_empty and line.length > 0
            ]
            if clean_candidates:
                return clean_candidates
            radius *= 2

        return []

    def _home_number_frame_from_line(
        self,
        projected: Point,
        line: LineString,
        face_towards_line: bool = False,
    ) -> dict:
        point = ShapelyPoint(projected)
        position = line.project(point)
        delta = min(max(line.length * 0.08, 1.0), 8.0)
        start = line.interpolate(max(0.0, position - delta))
        end = line.interpolate(min(line.length, position + delta))
        if start.distance(end) <= 1e-9:
            start = line.interpolate(0.0)
            end = line.interpolate(line.length)
        road_angle = self._readable_angle(math.degrees(math.atan2(end.y - start.y, end.x - start.x)))
        nearest = line.interpolate(position)

        away_x = float(projected[0]) - nearest.x
        away_y = float(projected[1]) - nearest.y
        away_length = math.hypot(away_x, away_y)
        if away_length <= 1e-9:
            radians = math.radians(road_angle + 90)
            away = (math.cos(radians), math.sin(radians))
        else:
            away = (away_x / away_length, away_y / away_length)

        road_radians = math.radians(road_angle)
        rotation_vector = (-away[0], -away[1]) if face_towards_line else away
        return {
            "road_angle": road_angle,
            "text_rotation": self._readable_angle(math.degrees(math.atan2(rotation_vector[1], rotation_vector[0]))),
            "tangent": (math.cos(road_radians), math.sin(road_radians)),
            "away": away,
            "distance": away_length,
            "nearest": (float(nearest.x), float(nearest.y)),
        }

    def _nearest_home_number_reference_frame(self, projected: Point) -> dict | None:
        candidates = self._home_number_reference_candidates(projected)
        if not candidates:
            return None

        point = ShapelyPoint(projected)
        best_line = min(candidates, key=lambda line: point.distance(line))
        return self._home_number_frame_from_line(
            projected,
            best_line,
            face_towards_line=self.network_type == "UG",
        )

    def _nearest_road_angle(self, projected: Point) -> float | None:
        frame = self._nearest_road_frame(projected)
        return None if frame is None else float(frame["road_angle"])

    def _olt_rotation_to_nearest_road(self, projected: Point) -> float:
        candidates = self._road_centerline_candidates(projected)
        if not candidates:
            return 0.0
        point = ShapelyPoint(projected)
        best_line = min(candidates, key=lambda line: point.distance(line))
        if best_line.is_empty or best_line.length <= 0:
            return 0.0
        frame = self._home_number_frame_from_line(projected, best_line, face_towards_line=True)
        return self._readable_angle(float(frame["text_rotation"]) + 90.0)

    def _home_number_should_rotate(self, axis_box: Polygon, road_geometry, occupied_geometry, is_crowded: bool) -> bool:
        if is_crowded:
            return True
        if road_geometry is not None and not road_geometry.is_empty:
            if axis_box.buffer(KML_HOME_NUMBER_ROAD_CLEARANCE_METERS).intersects(road_geometry):
                return True
        if occupied_geometry is not None and not occupied_geometry.is_empty:
            if axis_box.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS).intersects(occupied_geometry):
                return True
        return False

    def _home_number_stack_offsets(self, spacing: float) -> list[float]:
        offsets = [0.0]
        for step in range(1, 13):
            offsets.extend((spacing * step, -spacing * step))
        return offsets

    def _home_number_limited_offsets(self, spacing: float, limit: float) -> list[float]:
        offsets = [0.0]
        step = max(float(spacing), 0.1)
        limit = max(float(limit), step)
        multiplier = 1
        while step * multiplier <= limit + 1e-9:
            distance = step * multiplier
            offsets.extend((distance, -distance))
            multiplier += 1
        return offsets

    def _home_number_text_polygon(self, center: Point, text: str, rotation: float, height: float) -> Polygon:
        text_width = max(len(text), 1) * height * KML_HOME_NUMBER_TEXT_WIDTH_FACTOR * 1.15
        text_height = height * 1.8
        return self._home_number_box_polygon(center, text_width, text_height, rotation)

    def _clip_home_number_box(self, base_box: Polygon, road_geometry, occupied_geometry, boundary_geometry=None):
        clipped_box = base_box
        if road_geometry is not None and not road_geometry.is_empty:
            clipped_box = clipped_box.difference(road_geometry.buffer(0.01))
        if occupied_geometry is not None and not occupied_geometry.is_empty:
            clipped_box = clipped_box.difference(occupied_geometry)
        if not clipped_box.is_empty:
            clipped_box = clipped_box.buffer(0)
        return clipped_box

    def _home_number_candidate_score(
        self,
        base_box: Polygon,
        text_box: Polygon,
        road_geometry,
        occupied_geometry,
        text_occupied_geometry,
        boundary_geometry,
        along_offset: float,
        normal_offset: float,
    ) -> float:
        text_boundary_outside_area = 0.0
        box_text_overlap_area = 0.0
        text_box_overlap_area = 0.0
        road_area = 0.0
        occupied_area = 0.0
        text_overlap_area = 0.0
        text_road_area = 0.0
        if boundary_geometry is not None and not boundary_geometry.is_empty:
            text_boundary_outside_area = text_box.difference(boundary_geometry).area
        if road_geometry is not None and not road_geometry.is_empty:
            road_area = base_box.intersection(road_geometry).area
            text_road_area = text_box.intersection(road_geometry).area
        if occupied_geometry is not None and not occupied_geometry.is_empty:
            occupied_area = base_box.intersection(occupied_geometry).area
            text_box_overlap_area = text_box.intersection(
                occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS)
            ).area
        if text_occupied_geometry is not None and not text_occupied_geometry.is_empty:
            box_text_overlap_area = base_box.intersection(
                text_occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS)
            ).area
            text_overlap_area = text_box.intersection(
                text_occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS)
            ).area

        return (
            text_boundary_outside_area * 16000
            + box_text_overlap_area * 14000
            + text_overlap_area * 10000
            + text_box_overlap_area * 9000
            + text_road_area * 500000
            + occupied_area * 800
            + road_area * 120
            + abs(along_offset) * 16.0
            + abs(normal_offset) * 14.0
        )

    def _home_number_crowded_indexes(self) -> set[int]:
        boxes = [
            self._home_number_box_polygon(
                pending["projected"],
                *self._home_number_box_size(str(pending["text"])),
            )
            for pending in self.pending_home_numbers
        ]
        crowded: set[int] = set()
        if len(boxes) < 2:
            return crowded

        if STRtree is not None:
            tree = STRtree(boxes)
            index_by_id = {id(box): index for index, box in enumerate(boxes)}
            for index, box in enumerate(boxes):
                probe = box.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS)
                for candidate in self._query_indexed_geometries(tree, boxes, probe):
                    candidate_index = index_by_id.get(id(candidate))
                    if candidate_index is None or candidate_index == index:
                        continue
                    if probe.intersects(candidate.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS)):
                        crowded.update((index, candidate_index))
            return crowded

        for first_index, first_box in enumerate(boxes):
            first_probe = first_box.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS)
            for second_index, second_box in enumerate(boxes[first_index + 1 :], start=first_index + 1):
                if first_probe.intersects(second_box.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS)):
                    crowded.update((first_index, second_index))
        return crowded

    def _polygon_long_axis_angle(self, polygon: Polygon) -> float:
        try:
            rectangle = polygon.minimum_rotated_rectangle
            coordinates = list(rectangle.exterior.coords)
        except Exception:
            coordinates = []
        if len(coordinates) < 2:
            return 0.0

        longest: tuple[float, Point, Point] | None = None
        for start, end in zip(coordinates, coordinates[1:]):
            length = _distance(start, end)
            if longest is None or length > longest[0]:
                longest = (length, start, end)
        if not longest:
            return 0.0

        _length, start, end = longest
        angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
        return self._readable_angle(angle)

    def _polygon_text_height(self, polygon: Polygon, text: str) -> float:
        min_x, min_y, max_x, max_y = polygon.bounds
        available_width = max(max_x - min_x, KML_HOME_NUMBER_HEIGHT_METERS)
        available_height = max(max_y - min_y, KML_HOME_NUMBER_HEIGHT_METERS)
        width_height = available_width / max(len(text), 1) / KML_HOME_NUMBER_TEXT_WIDTH_FACTOR * 0.82
        box_height = available_height * 0.55
        return _clamp(
            min(KML_HOME_NUMBER_HEIGHT_METERS, width_height, box_height),
            0.35,
            KML_HOME_NUMBER_HEIGHT_METERS,
        )

    def _geometry_polygons(self, geometry) -> list[Polygon]:
        if geometry is None or geometry.is_empty:
            return []
        if isinstance(geometry, Polygon):
            return [geometry]
        return [
            polygon
            for part in getattr(geometry, "geoms", [])
            for polygon in self._geometry_polygons(part)
        ]

    def _register_fat_area_geometry(self, geometry) -> None:
        for polygon in self._geometry_polygons(geometry):
            if polygon.area > 1e-9:
                self.fat_area_geometries.append(polygon.buffer(0))

    def _home_number_boundary_geometry(self, projected: Point):
        if not self.fat_area_geometries:
            return None

        point = ShapelyPoint(projected)
        containing = [
            polygon
            for polygon in self.fat_area_geometries
            if polygon.buffer(1e-6).covers(point)
        ]
        if containing:
            boundary = unary_union(containing).buffer(0)
            return None if boundary.is_empty else boundary

        nearest = min(self.fat_area_geometries, key=lambda polygon: point.distance(polygon))
        return None if nearest.is_empty else nearest

    def _road_avoidance_geometry(self):
        road_parts = []
        if self.road_buffers:
            road_area = unary_union(self.road_buffers).buffer(0)
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                road_area = road_area.intersection(self.road_clip_geometry)
            if not road_area.is_empty:
                road_parts.append(road_area)
        if self.road_lines:
            road_lines = unary_union(self.road_lines)
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                road_lines = road_lines.intersection(self.road_clip_geometry)
            if not road_lines.is_empty:
                road_parts.append(road_lines.buffer(2.0, cap_style=2, join_style=2))
        if not road_parts:
            return None
        road_geometry = unary_union(road_parts).buffer(0)
        return None if road_geometry.is_empty else road_geometry

    def _home_number_placement(
        self,
        projected: Point,
        text: str,
        road_geometry,
        occupied_geometry,
        text_occupied_geometry,
        boundary_geometry,
        is_crowded: bool,
        forced_rotation: float | None = None,
    ):
        width, height = self._home_number_box_size(text)
        axis_box = self._home_number_box_polygon(projected, width, height)
        frame = self._nearest_home_number_reference_frame(projected)
        rotation = 0.0
        tangent = (0.0, 1.0)
        away = (1.0, 0.0)
        if frame is not None:
            tangent = frame["tangent"]
            away = frame["away"]
        should_rotate = frame is not None and (
            self.network_type == "UG"
            or self._home_number_should_rotate(
                axis_box,
                road_geometry,
                occupied_geometry,
                is_crowded,
            )
        )
        if should_rotate:
            # The text baseline points away from the nearest road. That makes the
            # left edge of the HP COVER box run parallel with the road edge.
            rotation = float(frame["text_rotation"])
        if forced_rotation is not None:
            rotation = self._readable_angle(float(forced_rotation))

        anchor_box = self._home_number_box_polygon(projected, width, height, rotation)
        anchor_visible_box = self._clip_home_number_box(
            anchor_box,
            road_geometry,
            occupied_geometry,
            boundary_geometry,
        )
        text_probe_box = anchor_visible_box if anchor_visible_box is not None and not anchor_visible_box.is_empty else anchor_box
        anchor_text_height = self._polygon_text_height(text_probe_box, text)
        anchor_text_box = self._home_number_text_polygon(projected, text, rotation, anchor_text_height)
        if self.network_type == "UG":
            return {
                "center": projected,
                "base_box": anchor_box,
                "clipped_box": anchor_visible_box,
                "text_box": anchor_text_box,
                "text_height": anchor_text_height,
                "rotation": rotation,
            }
        has_box_overlap = (
            occupied_geometry is not None
            and not occupied_geometry.is_empty
            and anchor_box.buffer(KML_HOME_NUMBER_BOX_CLEARANCE_METERS).intersects(occupied_geometry)
        )
        has_text_overlap = (
            text_occupied_geometry is not None
            and not text_occupied_geometry.is_empty
            and anchor_text_box.intersects(text_occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS))
        )
        has_box_text_overlap = (
            text_occupied_geometry is not None
            and not text_occupied_geometry.is_empty
            and anchor_box.intersects(text_occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS))
        )
        has_text_box_overlap = (
            occupied_geometry is not None
            and not occupied_geometry.is_empty
            and anchor_text_box.intersects(occupied_geometry.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS))
        )
        has_text_road_overlap = (
            road_geometry is not None
            and not road_geometry.is_empty
            and anchor_text_box.intersects(road_geometry)
        )
        has_box_road_overlap = (
            road_geometry is not None
            and not road_geometry.is_empty
            and anchor_box.buffer(KML_HOME_NUMBER_ROAD_CLEARANCE_METERS).intersects(road_geometry)
        )
        has_text_boundary_overflow = (
            boundary_geometry is not None
            and not boundary_geometry.is_empty
            and not boundary_geometry.buffer(1e-6).covers(anchor_text_box)
        )

        along_offsets = [0.0]
        normal_offsets = [0.0]
        needs_text_clearance_shift = has_text_overlap or has_box_text_overlap or has_text_box_overlap
        if has_box_overlap:
            along_offsets = self._home_number_stack_offsets(height + KML_HOME_NUMBER_STACK_GAP_METERS)
        elif needs_text_clearance_shift:
            along_offsets = self._home_number_limited_offsets(
                height + KML_HOME_NUMBER_STACK_GAP_METERS,
                max(width, height),
            )
        if needs_text_clearance_shift:
            limited_normal_offsets = self._home_number_limited_offsets(
                height + KML_HOME_NUMBER_STACK_GAP_METERS,
                max(width, height),
            )
            normal_offsets = sorted(
                set(normal_offsets + limited_normal_offsets),
                key=lambda value: (abs(value), value < 0),
            )
        if has_text_road_overlap:
            road_clearance_offsets = self._home_number_limited_offsets(
                max(KML_HOME_NUMBER_HEIGHT_METERS * 0.25, KML_HOME_NUMBER_TEXT_CLEARANCE_METERS),
                max(width, height),
            )
            normal_offsets = sorted(
                set(normal_offsets + [offset for offset in road_clearance_offsets if offset >= 0]),
                key=lambda value: abs(value),
            )
        uses_rotated_layout = should_rotate or forced_rotation is not None
        if uses_rotated_layout and (has_box_road_overlap or has_text_road_overlap or has_text_boundary_overflow):
            road_escape_offsets = [
                0.0,
                KML_HOME_NUMBER_ROAD_CLEARANCE_METERS,
                width * 0.25,
                width * 0.5,
                width * 0.75,
                width,
                width * 1.25,
            ]
            normal_offsets = sorted(set(normal_offsets + road_escape_offsets), key=lambda value: abs(value))

        best = None
        for along_offset in along_offsets:
            for normal_offset in normal_offsets:
                center = (
                    float(projected[0]) + tangent[0] * along_offset + away[0] * normal_offset,
                    float(projected[1]) + tangent[1] * along_offset + away[1] * normal_offset,
                )
                base_box = self._home_number_box_polygon(center, width, height, rotation)
                clipped_box = self._clip_home_number_box(
                    base_box,
                    road_geometry,
                    occupied_geometry,
                    boundary_geometry,
                )
                text_probe_box = clipped_box if clipped_box is not None and not clipped_box.is_empty else base_box
                text_height = self._polygon_text_height(text_probe_box, text)
                text_box = self._home_number_text_polygon(center, text, rotation, text_height)
                score = self._home_number_candidate_score(
                    base_box,
                    text_box,
                    road_geometry,
                    occupied_geometry,
                    text_occupied_geometry,
                    boundary_geometry,
                    along_offset,
                    normal_offset,
                )
                if best is None or score < best["score"]:
                    best = {
                        "score": score,
                        "center": center,
                        "base_box": base_box,
                        "clipped_box": clipped_box,
                        "text_box": text_box,
                        "text_height": text_height,
                        "rotation": rotation,
                    }

        if best is None:
            base_box = self._home_number_box_polygon(projected, width, height, rotation)
            clipped_box = self._clip_home_number_box(base_box, road_geometry, occupied_geometry, boundary_geometry)
            text_probe_box = clipped_box if clipped_box is not None and not clipped_box.is_empty else base_box
            best = {
                "center": projected,
                "base_box": base_box,
                "clipped_box": clipped_box,
                "text_box": self._home_number_text_polygon(
                    projected,
                    text,
                    rotation,
                    self._polygon_text_height(text_probe_box, text),
                ),
                "text_height": self._polygon_text_height(text_probe_box, text),
                "rotation": rotation,
            }

        return best

    def _add_home_number_box_geometry(self, geometry) -> None:
        box_attrs = {"layer": KML_HOME_NUMBER_BOX_LAYER, "color": 256}
        for polygon in self._geometry_polygons(geometry):
            if polygon.area <= 1e-9:
                continue
            exterior = list(polygon.exterior.coords)
            if len(exterior) >= 4:
                self._add_polyline(exterior, close=True, dxfattribs=box_attrs)
            for interior in polygon.interiors:
                interior_points = list(interior.coords)
                if len(interior_points) >= 4:
                    self._add_polyline(interior_points, close=True, dxfattribs=box_attrs)

    def _draw_home_number(self, text: str, placement: dict) -> None:
        text_attrs = {
            "layer": KML_HOME_NUMBER_LAYER,
            "color": KML_HOME_NUMBER_COLOR,
            "style": "EVA_BOLD",
        }
        self._add_home_number_box_geometry(placement["clipped_box"])
        self._add_centered_text(
            text,
            placement["center"],
            placement["text_height"],
            dxfattribs=text_attrs,
            rotation=placement["rotation"],
        )

    def _local_occupied_geometry(self, geometries: Sequence, tree, indexed_count: int, probe):
        if not geometries:
            return None

        candidates = []
        if tree is not None:
            candidates.extend(self._query_indexed_geometries(tree, geometries, probe))
        candidates.extend(geometries[indexed_count:])

        filtered = []
        seen: set[int] = set()
        for geometry in candidates:
            if geometry is None or geometry.is_empty:
                continue
            geometry_id = id(geometry)
            if geometry_id in seen:
                continue
            seen.add(geometry_id)
            try:
                if geometry.intersects(probe):
                    filtered.append(geometry)
            except Exception:
                continue
        if not filtered:
            return None
        local_geometry = unary_union(filtered)
        return None if local_geometry.is_empty else local_geometry.buffer(0)

    def _home_number_fat_area_index(self, projected: Point) -> int | None:
        if not self.fat_area_geometries:
            return None

        point = ShapelyPoint(projected)
        containing: list[tuple[float, int]] = []
        for index, polygon in enumerate(self.fat_area_geometries):
            if polygon is None or polygon.is_empty:
                continue
            try:
                if polygon.buffer(1e-6).covers(point):
                    containing.append((float(polygon.area), index))
            except Exception:
                continue
        if not containing:
            return None
        return min(containing)[1]

    def _home_number_preferred_rotation(self, projected: Point, text: str, road_geometry, is_crowded: bool) -> float:
        frame = self._nearest_home_number_reference_frame(projected)
        if frame is None:
            return 0.0

        width, height = self._home_number_box_size(text)
        axis_box = self._home_number_box_polygon(projected, width, height)
        if self.network_type == "UG" or self._home_number_should_rotate(axis_box, road_geometry, None, is_crowded):
            return self._readable_angle(float(frame["text_rotation"]))
        return 0.0

    def _home_number_rotation_bucket(self, rotation: float) -> int:
        bucket_degrees = max(KML_HOME_NUMBER_FAT_AREA_ROTATION_BUCKET_DEGREES, 1.0)
        return int(round(self._readable_angle(rotation) / bucket_degrees))

    def _home_number_fat_area_majority_rotations(self, road_geometry, crowded_indexes: set[int]) -> dict[int, float]:
        grouped: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
        for index, pending in enumerate(self.pending_home_numbers):
            area_index = self._home_number_fat_area_index(pending["projected"])
            if area_index is None:
                continue
            rotation = self._home_number_preferred_rotation(
                pending["projected"],
                str(pending["text"]),
                road_geometry,
                index in crowded_indexes,
            )
            grouped[area_index].append((index, rotation, self._home_number_rotation_bucket(rotation)))

        forced_rotations: dict[int, float] = {}
        for items in grouped.values():
            if len(items) < 2:
                continue
            bucket_counts = Counter(bucket for _index, _rotation, bucket in items)
            ranked_buckets = bucket_counts.most_common()
            if len(ranked_buckets) > 1 and ranked_buckets[0][1] == ranked_buckets[1][1]:
                continue
            majority_bucket = ranked_buckets[0][0]
            majority_rotations = [
                rotation
                for _index, rotation, bucket in items
                if bucket == majority_bucket
            ]
            if not majority_rotations:
                continue
            majority_rotation = self._readable_angle(sum(majority_rotations) / len(majority_rotations))
            for index, _rotation, _bucket in items:
                forced_rotations[index] = majority_rotation

        return forced_rotations

    def _home_parcel_road_solid(self):
        """Solid road footprint (buffered polygons + buffered lines + the
        road inner casing offset) used to carve roads out of HP COVER parcel
        blocks. Includes road clearance so parcels keep a gap from the road."""
        if self._home_parcel_road_solid_ready:
            return self._home_parcel_road_solid_cache
        parts = []
        clearance = KML_HOME_PARCEL_ROAD_CLEARANCE_METERS
        if self.road_buffers:
            try:
                area = unary_union(self.road_buffers).buffer(clearance)
                if not area.is_empty:
                    parts.append(area)
            except Exception:
                pass
        if self.road_lines:
            try:
                lines = unary_union(self.road_lines).buffer(2.0 + clearance, cap_style=2, join_style=2)
                if not lines.is_empty:
                    parts.append(lines)
            except Exception:
                pass
        # Distribution Cable and SLING WIRE are hard parcel boundaries too.
        # Keep a 0.3m safety gap on each side, same treatment as ROAD.
        cable_boundary_lines = [
            line for line in (self.kml_cable_centerlines or [])
            if line is not None and not line.is_empty and line.length > 0
        ]
        if cable_boundary_lines:
            try:
                cable_solid = unary_union(cable_boundary_lines).buffer(0.3, cap_style=2, join_style=2)
                if not cable_solid.is_empty:
                    parts.append(cable_solid)
            except Exception:
                pass
        if not parts:
            return None
        solid = unary_union(parts).buffer(0)
        return None if solid.is_empty else solid

    def _home_parcel_block_geometry(self, points: list[Point]):
        """Build the 'block' a set of HP COVER points live in: the area around
        the points, minus roads. Parcels are clipped to this so they never
        cross a road and stay within the populated block."""
        if not points:
            return None
        multipoint = MultiPoint([ShapelyPoint(p) for p in points])
        # Envelope around the points so even a single point gets a sensible
        # block. convex_hull handles clusters; buffer pads it to the block edge.
        hull = multipoint.convex_hull
        block = hull.buffer(
            KML_HOME_PARCEL_BLOCK_PADDING_METERS,
            cap_style=2,
            join_style=2,
        )
        if block.is_empty:
            return None

        # Subtract roads (with clearance) so parcels never cross a road.
        road_geometry = self._road_avoidance_geometry()
        if road_geometry is not None and not road_geometry.is_empty:
            try:
                block = block.difference(
                    road_geometry.buffer(KML_HOME_PARCEL_ROAD_CLEARANCE_METERS)
                )
            except Exception:
                pass
        # Also subtract the raw road footprint (buffers + lines) directly, in
        # case the avoidance geometry simplified away thin road segments.
        road_solid = self._home_parcel_road_solid()
        if road_solid is not None and not road_solid.is_empty:
            try:
                block = block.difference(road_solid)
            except Exception:
                pass

        # Keep within the cluster boundary clip if one is set.
        if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
            try:
                block = block.intersection(self.road_clip_geometry)
            except Exception:
                pass

        if block.is_empty:
            return None
        return block.buffer(0)

    def _finalize_home_numbers_as_parcels(self, pipeline_logger=None) -> None:
        """Render HP COVER as land-parcel cells (Voronoi tessellation clipped
        to the block) instead of independent fixed-ratio boxes. Produces the
        neat, hand-drawn cadastral look: cells share edges, vary in size,
        never overlap, never cross roads, and never spill outside their FAT
        area."""
        pending = self.pending_home_numbers
        total = len(pending)
        if pipeline_logger is not None:
            pipeline_progress(pipeline_logger, 70, "Menyusun HP COVER sebagai parcel.")
            pipeline_log(
                pipeline_logger,
                "info",
                f"[3/6] HP COVER parcel tessellation untuk {total:,} titik.",
            )
        sub_step_start = time.perf_counter()

        # De-duplicate exact-coincident points (Voronoi cannot handle them):
        # nudge duplicates by a sub-metre amount so each gets its own cell.
        seen: dict[tuple[int, int], int] = {}
        sites: list[Point] = []
        for item in pending:
            px, py = float(item["projected"][0]), float(item["projected"][1])
            key = (round(px * 100), round(py * 100))
            bump = seen.get(key, 0)
            seen[key] = bump + 1
            if bump:
                angle = bump * 2.39996
                spread = 3.2 * (1 + (bump - 1) * 0.5)
                px += math.cos(angle) * spread
                py += math.sin(angle) * spread
            sites.append((px, py))

        # Group HP points by the FAT area that contains them. Each FAT area is
        # tessellated independently and every cell is clipped to that FAT
        # polygon, so HP COVER parcels can NEVER spill outside their FAT area.
        # Points that fall in no FAT area are tessellated against the block
        # (hull minus roads) as a fallback.
        fat_clip_for_area: list = []
        groups: dict[int, list[int]] = {}
        no_fat_indexes: list[int] = []
        fat_areas = [poly for poly in self.fat_area_geometries if poly is not None and not poly.is_empty]
        for poly in fat_areas:
            clip = poly
            road_solid = self._home_parcel_road_solid()
            if road_solid is not None and not road_solid.is_empty:
                try:
                    clip = clip.difference(road_solid)
                except Exception:
                    clip = poly
            fat_clip_for_area.append(clip.buffer(0) if not clip.is_empty else poly)

        for index, point in enumerate(sites):
            sp = ShapelyPoint(point)
            best_area_idx = None
            best_area = None
            for area_idx, poly in enumerate(fat_areas):
                if poly.buffer(1e-6).covers(sp):
                    # Prefer the smallest containing FAT area (most specific).
                    if best_area is None or poly.area < best_area:
                        best_area = poly.area
                        best_area_idx = area_idx
            if best_area_idx is None:
                no_fat_indexes.append(index)
            else:
                groups.setdefault(best_area_idx, []).append(index)

        box_attrs = {"layer": KML_HOME_NUMBER_BOX_LAYER, "color": KML_HOME_NUMBER_BOX_COLOR}
        text_attrs = {
            "layer": KML_HOME_NUMBER_LAYER,
            "color": KML_HOME_NUMBER_COLOR,
            "style": "EVA_BOLD",
        }
        site_points = [ShapelyPoint(p) for p in sites]

        drawn = 0
        # Run-1 identity tracking. Used by deferred compact retry; avoids
        # re-drawing HPs that already have a valid parcel.
        self._hp_cover_drawn_indexes = set()
        # Track the union of all drawn parcels so overflow from one FAT group
        # never intersects parcels from another group (no overlapping boxes).
        drawn_union = None

        batch_size = 250
        for area_idx, indexes in groups.items():
            clip_region = fat_clip_for_area[area_idx]
            if clip_region is None or clip_region.is_empty:
                no_fat_indexes.extend(indexes)
                continue
            if KML_HOME_PARCEL_FAT_OVERFLOW_METERS > 0:
                clip_region = clip_region.buffer(KML_HOME_PARCEL_FAT_OVERFLOW_METERS, join_style=2)
                road_solid = self._home_parcel_road_solid()
                if road_solid is not None and not road_solid.is_empty:
                    try:
                        clip_region = clip_region.difference(road_solid)
                    except Exception:
                        pass
                clip_region = clip_region.buffer(0)
            if drawn_union is not None and not drawn_union.is_empty:
                try:
                    clip_region = clip_region.difference(drawn_union)
                except Exception:
                    pass
                if clip_region.is_empty:
                    no_fat_indexes.extend(indexes)
                    continue
            for batch_start in range(0, len(indexes), batch_size):
                batch_indexes = indexes[batch_start:batch_start + batch_size]
                if pipeline_logger is not None and len(indexes) > batch_size:
                    pipeline_log(
                        pipeline_logger,
                        "info",
                        f"[3/6] HP COVER parcel batch {batch_start // batch_size + 1}/"
                        f"{math.ceil(len(indexes) / batch_size)}: {len(batch_indexes):,} titik.",
                    )
                drawn += self._draw_parcel_group(
                    batch_indexes,
                    sites,
                    site_points,
                    pending,
                    clip_region,
                    box_attrs,
                    text_attrs,
                    cap_radius=False,
                    fill_region=True,
                pipeline_logger=pipeline_logger,
                )

        if no_fat_indexes:
            block = self._home_parcel_block_geometry([sites[i] for i in no_fat_indexes])
            if block is not None and not block.is_empty:
                for batch_start in range(0, len(no_fat_indexes), batch_size):
                    batch_indexes = no_fat_indexes[batch_start:batch_start + batch_size]
                    if pipeline_logger is not None and len(no_fat_indexes) > batch_size:
                        pipeline_log(
                            pipeline_logger,
                            "info",
                            f"[3/6] HP COVER fallback batch {batch_start // batch_size + 1}/"
                            f"{math.ceil(len(no_fat_indexes) / batch_size)}: {len(batch_indexes):,} titik.",
                        )
                    drawn += self._draw_parcel_group(
                        batch_indexes,
                        sites,
                        site_points,
                        pending,
                        block,
                        box_attrs,
                        text_attrs,
                        cap_radius=True,
                        fill_region=False,
                pipeline_logger=pipeline_logger,
                    )

        # Run-2: only source HPs that Run-1 could not fit. AI visual axis
        # selection remains Run-1 behaviour; retry only finds nearby free room.
        missing_indexes = [i for i in range(total) if i not in self._hp_cover_drawn_indexes]
        if missing_indexes:
            retry_drawn = self._draw_missing_hp_cover_retry(
                missing_indexes, sites, pending, fat_areas, box_attrs, text_attrs, pipeline_logger
            )
            drawn += retry_drawn
            if pipeline_logger is not None:
                pipeline_log(pipeline_logger, "info", f"[3/6] HP COVER retry: {retry_drawn}/{len(missing_indexes)} titik conflict dipasang ke space kosong terdekat.")

        # Run-3: every HP COVER source remains visible even where FAT/ROAD/
        # cable constraints leave no valid non-overlapping parcel. Do not force
        # bad geometry; retain the HOME_NUMBER label at its original KMZ point.
        label_only_indexes = [i for i in range(total) if i not in self._hp_cover_drawn_indexes]
        if label_only_indexes:
            fallback_angle = 0.0
            for idx in label_only_indexes:
                item = pending[idx]
                source = site_points[idx]
                self._add_centered_text(
                    str(item["text"]), (source.x, source.y),
                    KML_HOME_NUMBER_HEIGHT_METERS,
                    dxfattribs=text_attrs, rotation=fallback_angle,
                )
                self._hp_cover_drawn_indexes.add(idx)
            if pipeline_logger is not None:
                pipeline_log(
                    pipeline_logger, "warning",
                    f"[3/6] HP COVER label-only fallback: {len(label_only_indexes)}/{len(label_only_indexes)} "
                    "titik retained tanpa parcel karena ruang aman tidak tersedia.",
                )

        if drawn == 0 and not label_only_indexes:
            raise ValueError("tidak ada HP COVER yang dapat dipertahankan")

        if pipeline_logger is not None:
            pipeline_log(
                pipeline_logger,
                "success",
                f"[3/6] HP COVER parcel selesai: {drawn:,}/{total:,} parcel digambar dalam "
                f"{format_stage_seconds(time.perf_counter() - sub_step_start)}.",
            )

    def _finalize_uncover_home_numbers_as_parcels(self, pipeline_logger=None) -> None:
        """Render HP UNCOVER as land-parcel cells (Voronoi tessellation clipped
        to the block) instead of independent fixed-ratio boxes. Produces the
        neat, hand-drawn cadastral look: cells share edges, vary in size,
        never overlap, never cross roads, and never spill outside their FAT
        area."""
        pending = self.pending_uncover_home_numbers
        total = len(pending)
        if pipeline_logger is not None:
            pipeline_progress(pipeline_logger, 70, "Menyusun HP UNCOVER sebagai parcel.")
            pipeline_log(
                pipeline_logger,
                "info",
                f"[3/6] HP UNCOVER parcel tessellation untuk {total:,} titik.",
            )
        sub_step_start = time.perf_counter()

        # De-duplicate exact-coincident points (Voronoi cannot handle them):
        # nudge duplicates by a sub-metre amount so each gets its own cell.
        seen: dict[tuple[int, int], int] = {}
        sites: list[Point] = []
        for item in pending:
            px, py = float(item["projected"][0]), float(item["projected"][1])
            key = (round(px * 100), round(py * 100))
            bump = seen.get(key, 0)
            seen[key] = bump + 1
            if bump:
                angle = bump * 2.39996
                spread = 3.2 * (1 + (bump - 1) * 0.5)
                px += math.cos(angle) * spread
                py += math.sin(angle) * spread
            sites.append((px, py))

        # Group HP points by the FAT area that contains them. Each FAT area is
        # tessellated independently and every cell is clipped to that FAT
        # polygon, so HP COVER parcels can NEVER spill outside their FAT area.
        # Points that fall in no FAT area are tessellated against the block
        # (hull minus roads) as a fallback.
        fat_clip_for_area: list = []
        groups: dict[int, list[int]] = {}
        no_fat_indexes: list[int] = []
        fat_areas = [poly for poly in self.fat_area_geometries if poly is not None and not poly.is_empty]
        for poly in fat_areas:
            clip = poly
            road_solid = self._home_parcel_road_solid()
            if road_solid is not None and not road_solid.is_empty:
                try:
                    clip = clip.difference(road_solid)
                except Exception:
                    clip = poly
            fat_clip_for_area.append(clip.buffer(0) if not clip.is_empty else poly)

        for index, point in enumerate(sites):
            sp = ShapelyPoint(point)
            best_area_idx = None
            best_dist = None
            for area_idx, poly in enumerate(fat_areas):
                dist = point_polygon_distance_meter(point, poly.exterior.coords)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_area_idx = area_idx
            if best_area_idx is not None:
                groups.setdefault(best_area_idx, []).append(index)
            else:
                no_fat_indexes.append(index)

        box_attrs = {"layer": KML_UNCOVER_BOX_LAYER, "color": KML_UNCOVER_BOX_COLOR}
        text_attrs = {"layer": KML_UNCOVER_LAYER, "color": KML_UNCOVER_COLOR}
        site_points = [ShapelyPoint(p) for p in sites]

        drawn = 0
        # Track the union of all drawn parcels so overflow from one FAT group
        # never intersects parcels from another group (no overlapping boxes).
        drawn_union = None
        # FAT-area groups: clip strictly to the FAT polygon (roads removed).
        # Inside a FAT area the parcels must FILL it completely (cover every
        # FAT boundary line) with no leftover rounded gaps ??? so we disable the
        # circular radius cap and draw cells edge-to-edge (no inset).
        # The clip region is enlarged by FAT_OVERFLOW so parcels extend
        # slightly past the boundary, following its contour (cadastral look).
        for area_idx, indexes in groups.items():
            clip_region = fat_clip_for_area[area_idx]
            if clip_region is None or clip_region.is_empty:
                no_fat_indexes.extend(indexes)
                continue
            # Buffer outward so parcels overflow the FAT boundary slightly.
            if KML_HOME_PARCEL_FAT_OVERFLOW_METERS > 0:
                clip_region = clip_region.buffer(KML_HOME_PARCEL_FAT_OVERFLOW_METERS, join_style=2)
                # Re-subtract roads from the enlarged region so parcels still
                # respect road gaps even in the overflow zone.
                road_solid = self._home_parcel_road_solid()
                if road_solid is not None and not road_solid.is_empty:
                    try:
                        clip_region = clip_region.difference(road_solid)
                    except Exception:
                        pass
                clip_region = clip_region.buffer(0)
            # Subtract already-drawn parcel regions so this group's overflow
            # doesn't collide with parcels from a previously drawn group.
            if drawn_union is not None and not drawn_union.is_empty:
                try:
                    clip_region = clip_region.difference(drawn_union)
                except Exception:
                    pass
                if clip_region.is_empty:
                    no_fat_indexes.extend(indexes)
                    continue
            before_drawn = len(self.home_number_boxes)
            drawn += self._draw_parcel_group(
                indexes,
                sites,
                site_points,
                pending,
                clip_region,
                box_attrs,
                text_attrs,
                cap_radius=False,
                fill_region=True,
                pipeline_logger=pipeline_logger,
            )
            # Update drawn_union with newly placed parcels.
            new_boxes = self.home_number_boxes[before_drawn:]
            if new_boxes:
                from shapely.ops import unary_union as _unary_union
                new_union = _unary_union(new_boxes)
                drawn_union = new_union if drawn_union is None else drawn_union.union(new_union)

        # No-FAT points: tessellate against the block (hull minus roads).
        if no_fat_indexes:
            block = self._home_parcel_block_geometry([sites[i] for i in no_fat_indexes])
            if block is not None and not block.is_empty:
                drawn += self._draw_parcel_group(
                    no_fat_indexes,
                    sites,
                    site_points,
                    pending,
                    block,
                    box_attrs,
                    text_attrs,
                    cap_radius=True,
                    fill_region=False,
                pipeline_logger=pipeline_logger,
                )

        if drawn == 0:
            if pipeline_logger is not None:
                pipeline_log(
                    pipeline_logger,
                    "warn",
                    f"[3/6] HP UNCOVER parcel kosong: 0/{total:,} parcel digambar, lanjut tanpa fail.",
                )
            return

        if pipeline_logger is not None:
            pipeline_log(
                pipeline_logger,
                "success",
                f"[3/6] HP UNCOVER parcel selesai: {drawn:,}/{total:,} parcel digambar dalam "
                f"{format_stage_seconds(time.perf_counter() - sub_step_start)}.",
            )

    def _split_hp_parcel_subblocks(self, indexes: list[int], sites: list[Point]) -> list[list[int]]:
        """Split one FAT region into local layout blocks before selecting an axis.

        Split only by source-point topology and road side. AI is called later per
        returned block and never sees/creates geometry.
        """
        if len(indexes) < 3:
            return [indexes]
        gap = 32.0  # ponytail: fixed locality ceiling; make config only after field tuning.
        remaining = set(indexes)
        components: list[list[int]] = []
        while remaining:
            seed = remaining.pop()
            group = {seed}
            frontier = [seed]
            while frontier:
                current = frontier.pop()
                close = [other for other in remaining if _distance(sites[current], sites[other]) <= gap]
                for other in close:
                    remaining.remove(other)
                    group.add(other)
                    frontier.append(other)
            components.append(sorted(group))

        # Roads, Distribution Cable, and SLING WIRE create independent local
        # layout sides so AI may choose different axes per side.
        out: list[list[int]] = []
        roads = [line for line in self.road_centerlines if line is not None and not line.is_empty and line.length > 2]
        boundary_lines = list(roads)
        boundary_lines.extend(line for line in (self.kml_cable_centerlines or []) if line is not None and not line.is_empty and line.length > 2)
        for component in components:
            if len(component) < 4 or not boundary_lines:
                out.append(component)
                continue
            center = MultiPoint([ShapelyPoint(sites[i]) for i in component]).centroid
            if not boundary_lines:
                out.append(component)
                continue
            road = min(boundary_lines, key=lambda line: line.distance(center))
            if road.distance(center) > 18.0:
                out.append(component)
                continue
            d = road.project(center)
            delta = min(2.0, max(.25, road.length * .01))
            a = road.interpolate(max(0.0, d-delta)); b = road.interpolate(min(road.length, d+delta))
            dx, dy = b.x-a.x, b.y-a.y
            plus=[]; minus=[]
            for index in component:
                x,y=sites[index]
                (plus if (x-a.x)*dy-(y-a.y)*dx >= 0 else minus).append(index)
            if len(plus) >= 2 and len(minus) >= 2:
                out.extend([plus, minus])
            else:
                out.append(component)
        return out

    def _draw_missing_hp_cover_retry(self, missing_indexes, sites, pending, fat_areas, box_attrs, text_attrs, pipeline_logger=None):
        """Second pass for HPs blocked by FAT/ROAD edge conflicts.

        Search compact offsets <=6m around source, reserve earlier parcels, and
        draw one small non-overlapping parcel. This never changes AI Run-1 axis.
        """
        road_solid = self._home_parcel_road_solid()
        drawn = 0
        offsets = [(0.0, 0.0)]  # immutable source anchor; never relocate HP COVER
        for idx in missing_indexes:
            source = ShapelyPoint(sites[idx])
            # Prefer containing FAT; otherwise nearest FAT only within 6m.
            ranked = sorted(fat_areas, key=lambda poly: poly.distance(source))
            placed = False
            for fat in ranked[:3]:
                if fat is None or fat.is_empty or fat.distance(source) > 6.0:
                    continue
                for dx, dy in offsets:
                    candidate_anchor = ShapelyPoint(source.x + dx, source.y + dy)
                    if not fat.buffer(0.01).covers(candidate_anchor):
                        continue
                    if road_solid is not None and not road_solid.is_empty and road_solid.buffer(0.01).covers(candidate_anchor):
                        continue
                    # Compact 6x10m base, centered on relocated retry anchor.
                    parcel = Polygon([
                        (candidate_anchor.x - 3.0, candidate_anchor.y - 5.0),
                        (candidate_anchor.x + 3.0, candidate_anchor.y - 5.0),
                        (candidate_anchor.x + 3.0, candidate_anchor.y + 5.0),
                        (candidate_anchor.x - 3.0, candidate_anchor.y + 5.0),
                    ]).intersection(fat)
                    if road_solid is not None and not road_solid.is_empty:
                        parcel = parcel.difference(road_solid)
                    for existing in self.home_number_boxes:
                        if existing is not None and not existing.is_empty:
                            parcel = parcel.difference(existing)
                    parcel = self._parcel_piece_containing(parcel, candidate_anchor)
                    if parcel is None or parcel.is_empty or parcel.area < 24.0:
                        continue
                    polys = self._geometry_polygons(parcel)
                    label_polygon = max(polys, key=lambda poly: poly.area, default=None)
                    if label_polygon is None:
                        continue
                    output_start = len(self.output_entities)
                    for poly in polys:
                        if poly.area <= 0.05:
                            continue
                        self._add_polyline(list(poly.exterior.coords), close=True, dxfattribs=box_attrs)
                        self.home_number_boxes.append(poly)
                    center = label_polygon.representative_point()
                    self._add_centered_text(str(pending[idx]["text"]), (center.x, center.y), KML_HOME_NUMBER_HEIGHT_METERS,
                                            dxfattribs=text_attrs, rotation=self._polygon_label_rotation(label_polygon))
                    self.register_fdt_entities(pending[idx].get("fdt_number"), self.output_entities[output_start:])
                    self._hp_cover_drawn_indexes.add(idx)
                    drawn += 1
                    placed = True
                    break
                if placed:
                    break
        return drawn

    def _draw_parcel_group(
        self,
        indexes: list[int],
        sites: list[Point],
        site_points: list,
        pending: list[dict],
        clip_region,
        box_attrs: dict,
        text_attrs: dict,
        cap_radius: bool = True,
        fill_region: bool = False,
        pipeline_logger=None,
        _subblocks_split: bool = False,
    ) -> int:
        """Tessellate one group of HP points (Voronoi) and clip every cell to
        `clip_region`. Returns the number of parcels drawn. `clip_region` is
        the hard boundary parcels may never cross (a FAT area, or the block).

        When `fill_region` is True the parcels tile `clip_region` completely,
        edge-to-edge, with no radius cap and no inset gap ??? so all FAT-area
        boundary lines are covered and no rounded leftover gaps remain."""
        if not indexes:
            return 0

        # Do not force a full FAT AREA into one orientation. Split local blocks
        # first, then this function selects one axis per sub-block.
        if not _subblocks_split:
            subblocks = self._split_hp_parcel_subblocks(indexes, sites)
            if len(subblocks) > 1:
                if pipeline_logger is not None:
                    pipeline_log(pipeline_logger, "info", f"[3/6] HP COVER split into {len(subblocks)} local blocks before axis selection.")
                total = 0
                for block in subblocks:
                    local_region = clip_region
                    # Build half-plane clip based on nearest road side for this sub-block.
                    if len(block) >= 2 and self.road_centerlines:
                        center = MultiPoint([ShapelyPoint(sites[i]) for i in block]).centroid
                        road = min(self.road_centerlines, key=lambda line: line.distance(center))
                        probe = ShapelyPoint(sites[block[0]])
                        d = road.project(probe)
                        eps = min(2.0, max(.25, road.length * .01))
                        a = road.interpolate(max(0.0, d-eps)); b = road.interpolate(min(road.length, d+eps))
                        dx, dy = b.x-a.x, b.y-a.y
                        side = (probe.x-a.x)*dy-(probe.y-a.y)*dx
                        minx,miny,maxx,maxy=clip_region.bounds
                        span=max(maxx-minx,maxy-miny,1.0)*4.0
                        if side >= 0:
                            cutter=Polygon([(a.x-dy*span,a.y+dx*span),(b.x-dy*span,b.y+dx*span),(b.x+dy*span,b.y-dx*span),(a.x+dy*span,a.y-dx*span)])
                        else:
                            cutter=Polygon([(a.x+dy*span,a.y-dx*span),(b.x+dy*span,b.y-dx*span),(b.x-dy*span,b.y+dx*span),(a.x-dy*span,a.y+dx*span)])
                        try:
                            candidate_region = clip_region.intersection(cutter)
                            # Local road-side cut must retain every source HP.
                            anchors_ok = candidate_region is not None and not candidate_region.is_empty and all(
                                candidate_region.buffer(0.01).covers(ShapelyPoint(sites[i])) for i in block
                            )
                            local_region = candidate_region if anchors_ok else clip_region
                        except Exception:
                            local_region=clip_region
                    if local_region is not None and not local_region.is_empty:
                        total += self._draw_parcel_group(block, sites, site_points, pending, local_region, box_attrs, text_attrs, cap_radius, fill_region, pipeline_logger, True)
                return total

        # Stable spatial order inside each FAT/sub-block. AI axis remains
        # unchanged; only deterministic parcel allocation order is stabilized.
        if len(indexes) > 1:
            _stable_angle = self._polygon_label_rotation(clip_region)
            _radians = math.radians(_stable_angle)
            _tx, _ty = math.cos(_radians), math.sin(_radians)
            indexes = sorted(indexes, key=lambda i: (
                round(sites[i][0] * _tx + sites[i][1] * _ty, 3),
                round(-sites[i][0] * _ty + sites[i][1] * _tx, 3),
                i,
            ))
        group_sites = [sites[i] for i in indexes]
        if len(group_sites) == 1:
            # Single point in this region: the parcel IS the clip region
            # (capped), so it fills the FAT area without spilling out.
            cells = [clip_region]
        else:
            multipoint = MultiPoint([ShapelyPoint(p) for p in group_sites])
            envelope = clip_region.buffer(KML_HOME_PARCEL_BLOCK_PADDING_METERS)
            try:
                diagram = _shapely_voronoi_diagram(multipoint, envelope=envelope.envelope)
                cells = list(getattr(diagram, "geoms", []))
            except Exception:
                cells = []
            if not cells:
                # Never give every HP the full FAT region. Build deterministic
                # per-point ownership cells below instead.
                cells = []

        # Map each group site to the Voronoi cell that contains it.
        cell_for_local: list = [None] * len(group_sites)
        cell_area_for_local: list[float] = [0.0] * len(group_sites)
        if len(group_sites) > 1:
            for local_idx, point in enumerate(group_sites):
                sp = ShapelyPoint(point)
                for cell in cells:
                    if isinstance(cell, Polygon):
                        clipped_cell = cell.intersection(clip_region)
                        if clipped_cell.buffer(0.01).covers(sp):
                            cell_for_local[local_idx] = clipped_cell
                            cell_area_for_local[local_idx] = float(clipped_cell.area)
                            break
        else:
            cell_for_local[0] = cells[0]
            cell_area_for_local[0] = float(cells[0].area) if not cells[0].is_empty else float(clip_region.area)

        # Find the dominant orientation of the clip_region (FAT area) so all
        # rectangles in this group align to the same axis. This is the
        # "rectangular, aligned to FAT area" style: parcels are rotated
        # rectangles, not irregular polygons, and they all share edges.
        region_angle = self._polygon_label_rotation(clip_region) if clip_region is not None else 0.0
        # One deterministic axis per local sub-block. Visual AI parcel hints
        # are disabled: they caused neighboring boxes to rotate inconsistently.
        nearest_road = min(
            (line for line in self.road_centerlines if line is not None and not line.is_empty and line.length > 2),
            key=lambda line: line.distance(clip_region.representative_point()),
            default=None,
        )
        if nearest_road is not None:
            reference = clip_region.representative_point()
            if nearest_road.distance(reference) <= 18.0:
                try:
                    position = nearest_road.project(reference)
                    epsilon = min(2.0, max(0.25, nearest_road.length * .01))
                    point_a = nearest_road.interpolate(max(0.0, position - epsilon))
                    point_b = nearest_road.interpolate(min(nearest_road.length, position + epsilon))
                    dx, dy = point_b.x - point_a.x, point_b.y - point_a.y
                    if dx * dx + dy * dy > 1e-9:
                        region_angle = math.degrees(math.atan2(dy, dx))
                except Exception:
                    pass
        region_angle = self._readable_angle(region_angle)
        ai_orientation = None
        if pipeline_logger is not None:
            pipeline_log(pipeline_logger, "info", f"[3/6] HP COVER deterministic local axis {region_angle:.1f}°.")

        row_grid_parcels = self._hp_row_grid_parcels(indexes, sites, pending, clip_region, region_angle)
        drawn = 0
        for local_idx, global_idx in enumerate(indexes):
            cell = cell_for_local[local_idx]
            if cell is None or cell.is_empty:
                # Row-grid is the safe per-HP fallback. Never substitute the
                # whole FAT region, which makes parcels overlap.
                cell = row_grid_parcels.get(global_idx)
                if cell is None or cell.is_empty:
                    continue
            site = site_points[global_idx]
            item = pending[global_idx]

            # Road-fronting parcel: front edge attaches to nearest ROAD,
            # parcel extends away from ROAD toward/through HP point, and width
            # comes from local Voronoi density. This matches real cadastral
            # subdivision: lots line up along roads instead of floating as
            # free rectangles.
            cell_area = cell_area_for_local[local_idx] if cell_area_for_local[local_idx] > 0 else float(cell.area)
            # Intact compact rectangle. Ownership is a fit constraint, not
            # a clipping blade; failed fits go to Run-2 relocation.
            preferred = region_angle
            allowed = cell.intersection(clip_region)
            road_solid = self._home_parcel_road_solid()
            if road_solid is not None and not road_solid.is_empty:
                allowed = allowed.difference(road_solid)
            if self.home_number_boxes:
                allowed = allowed.difference(unary_union(self.home_number_boxes))
            parcel = self._fit_compact_rect(site, allowed, preferred)
            if parcel is None or parcel.is_empty:
                # Road-front rectangle can disappear (common for HP UNCOVER
                # outside/near boundary). Fall back to cell envelope clipped
                # to clip region — keeps rectangle shape.
                try:
                    cell_clip = cell.intersection(clip_region)
                    if isinstance(cell_clip, Polygon) and not cell_clip.is_empty:
                        parcel = cell_clip.minimum_rotated_rectangle
                    else:
                        parcel = cell.envelope
                except Exception:
                    parcel = cell.envelope
            if parcel is None or parcel.is_empty:
                continue
            # Final hard guard for every renderer path (row-grid, road-front,
            # and fallback): a parcel may never leave FAT AREA or enter ROAD.
            # Do this after shape generation because row-grid can bypass the
            # block-builder road subtraction.
            try:
                parcel = parcel.intersection(clip_region)
                road_solid = self._home_parcel_road_solid()
                if road_solid is not None and not road_solid.is_empty:
                    # ROAD + 0.3m cable solid is absolute. Never reopen it for
                    # an anchor pocket; Run-2 relocates blocked HPs safely.
                    parcel = self._parcel_piece_containing(parcel.difference(road_solid), site)
                else:
                    parcel = self._parcel_piece_containing(parcel, site)
            except Exception:
                continue
            if parcel is None or parcel.is_empty or not parcel.buffer(0.01).covers(site):
                # Final anchored fallback. Never drop a source HP because a
                # Voronoi/road clip produced no usable polygon. Build a small
                # deterministic rectangle around the immutable source point,
                # then constrain it to the local FAT-safe region.
                try:
                    fallback_region = clip_region
                    if fallback_region is not None and not fallback_region.is_empty:
                        half = 2.0
                        anchor_box = ShapelyPoint(site).buffer(half, cap_style=3, join_style=2)
                        candidate = anchor_box.intersection(fallback_region)
                        if road_solid is not None and not road_solid.is_empty:
                            candidate = candidate.difference(road_solid)
                        # Preserve one-HP-one-parcel: reserve all prior boxes.
                        for existing_box in self.home_number_boxes:
                            if existing_box is not None and not existing_box.is_empty:
                                candidate = candidate.difference(existing_box)
                        anchored = self._parcel_piece_containing(candidate, site)
                        if anchored is not None and not anchored.is_empty:
                            parcel = anchored
                except Exception:
                    parcel = None
            if parcel is None or parcel.is_empty or not parcel.buffer(0.01).covers(site):
                continue
            # A house parcel must never collapse to a text-sized sliver.
            # Let Run-2 relocate conflict anchors to nearby free space instead.
            if parcel.area < 24.0:
                continue
            # Road clipping can leave a long thin spur at junctions. Keep the
            # original KMZ anchor piece only; reject slivers that make parcels
            # look like they cross a road in DXF.
            try:
                minx, miny, maxx, maxy = parcel.bounds
                short_side = min(maxx - minx, maxy - miny)
                if short_side < 0.45 and parcel.area < 8.0:
                    continue
            except Exception:
                pass

            # Global occupancy: blocks from all FAT/sub-blocks are mutually
            # exclusive. Use a tiny buffer to avoid DXF visual overlap while
            # allowing shared boundaries.
            try:
                occupied = [box for box in self.home_number_boxes if box is not None and not box.is_empty]
                if occupied:
                    parcel = parcel.difference(unary_union(occupied))
                parcel = self._parcel_piece_containing(parcel, site)
            except Exception:
                parcel = None
            if parcel is None or parcel.is_empty or parcel.area < 24.0:
                continue
            parcel = parcel.simplify(0.05, preserve_topology=True)
            parcel = self._parcel_piece_containing(parcel, site)
            if parcel is None or parcel.is_empty or parcel.area < 24.0:
                continue

            output_start_index = len(self.output_entities)
            # One HP owns exactly one connected polygon. Detached clipped
            # remnants are deliberately not drawn.
            exterior = list(parcel.exterior.coords)
            if len(exterior) < 4:
                continue
            self._add_polyline(exterior, close=True, dxfattribs=box_attrs)
            self.home_number_boxes.append(parcel)

            text = str(item["text"])
            label_polygon = parcel
            if label_polygon is None:
                continue
            # HP COVER label is anchored at the original KMZ placemark.
            # Do not substitute polygon centre after clipping; that makes the
            # source HP appear to move in DXF.
            if not label_polygon.buffer(0.01).covers(site):
                continue
            # Text always sits at final parcel centre. Source coordinates are
            # retained for parcel generation; this only prevents edge labels.
            anchor = label_polygon.representative_point()
            text_height = KML_HOME_NUMBER_HEIGHT_METERS
            rotation = self._polygon_label_rotation(label_polygon)
            self._add_centered_text(
                text, (anchor.x, anchor.y), text_height,
                dxfattribs=text_attrs, rotation=rotation,
            )
            self.register_fdt_entities(item.get("fdt_number"), self.output_entities[output_start_index:])
            self._hp_cover_drawn_indexes.add(global_idx)
            drawn += 1
        return drawn

    def _hp_visual_parcel_svg(self, region, points):
        """Small dependency-free visual preview for multimodal parcel review."""
        if region is None or region.is_empty: return ""
        minx,miny,maxx,maxy=region.bounds; w=max(maxx-minx, 1.0); h=max(maxy-miny, 1.0)
        pad=12; scale=min(480/w, 480/h)
        def xy(x,y): return (pad+(x-minx)*scale, pad+(maxy-y)*scale)
        def path(poly):
            coords=list(poly.exterior.coords); return ' '.join((('M' if i==0 else 'L')+f'{xy(x,y)[0]:.1f},{xy(x,y)[1]:.1f}') for i,(x,y) in enumerate(coords))+' Z'
        fat=''.join(f'<path d="{path(p)}" fill="#00d9ff22" stroke="#00d9ff" stroke-width="2"/>' for p in self._geometry_polygons(region))
        roads=self._home_parcel_road_solid(); road=''
        if roads is not None and not roads.is_empty:
            road=''.join(f'<path d="{path(p)}" fill="#ff333355" stroke="#ff3333" stroke-width="2"/>' for p in self._geometry_polygons(roads.intersection(region.envelope)))
        dots=''.join(f'<circle cx="{xy(p.x,p.y)[0]:.1f}" cy="{xy(p.x,p.y)[1]:.1f}" r="4" fill="#ffe600" stroke="#111"/>' for p in points)
        size=int(max(w,h)*scale+pad*2)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}"><rect width="100%" height="100%" fill="#111"/>{fat}{road}{dots}</svg>'

    def _hp_row_grid_parcels(self, indexes, sites, pending, clip_region, angle):
        """Aligned midpoint cells; never move HP source points."""
        if not indexes or clip_region is None or clip_region.is_empty:
            return {}
        a=math.radians(float(angle)); tx,ty=math.cos(a),math.sin(a); nx,ny=-ty,tx
        def uv(pt):
            x,y=float(pt[0]),float(pt[1]); return x*tx+y*ty, x*nx+y*ny
        def xy(u,v): return (tx*u+nx*v, ty*u+ny*v)
        coords=[]
        for g in self._geometry_polygons(clip_region): coords.extend(g.exterior.coords)
        if not coords: return {}
        us=[uv(c)[0] for c in coords]; vs=[uv(c)[1] for c in coords]
        min_u,max_u,min_v,max_v=min(us)-2,max(us)+2,min(vs)-2,max(vs)+2
        pts=[]
        for i in indexes:
            u,v=uv(sites[i]); pts.append((i,u,v))
        rows=[]
        for item in sorted(pts,key=lambda x:x[2]):
            if not rows or abs(item[2]-rows[-1][0])>14.0: rows.append([item[2],[item]])
            else: rows[-1][1].append(item); rows[-1][0]=sum(x[2] for x in rows[-1][1])/len(rows[-1][1])
        centers=[r[0] for r in rows]; out={}
        for ri,(_,row) in enumerate(rows):
            v0=(centers[ri-1]+centers[ri])/2 if ri else max(min_v,centers[ri]-5)
            v1=(centers[ri]+centers[ri+1])/2 if ri<len(rows)-1 else min(max_v,centers[ri]+5)
            row=sorted(row,key=lambda x:x[1])
            for j,(idx,u,v) in enumerate(row):
                u0=(row[j-1][1]+u)/2 if j else max(min_u,u-3.0)
                u1=(u+row[j+1][1])/2 if j<len(row)-1 else min(max_u,u+3.0)
                try: cell=Polygon([xy(u0,v0),xy(u1,v0),xy(u1,v1),xy(u0,v1)]).intersection(clip_region)
                except Exception: continue
                if not cell.is_empty and cell.area>=KML_HOME_PARCEL_MIN_AREA_SQUARE_METERS: out[idx]=cell
        return out

    def _fit_compact_rect(self, site, allowed, angle, width=6.0, depth=10.0):
        """Fit largest intact house rectangle without producing slivers.

        Start from the capped 12x18 module and step down toward 6x10. Shared
        parcel edges are valid; geometry may touch but never overlap.
        """
        if site is None or allowed is None or allowed.is_empty:
            return None
        rad = math.radians(float(angle))
        tx, ty = math.cos(rad), math.sin(rad)
        nx, ny = -ty, tx
        # Balanced house parcels: max 14m each axis. Avoid old 12x18 strips.
        # HP source remains the fixed centre/anchor; only parcel extent grows.
        widths = (14.0, 13.0, 12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0)
        depths = (14.0, 13.0, 12.0, 11.0, 10.0)
        candidates = sorted(
            ((w * d, w, d) for w in widths for d in depths),
            reverse=True,
        )
        for _area, w, d in candidates:
            hw, hd = w / 2.0, d / 2.0
            rect = Polygon([
                (site.x-tx*hw-nx*hd, site.y-ty*hw-ny*hd),
                (site.x+tx*hw-nx*hd, site.y+ty*hw-ny*hd),
                (site.x+tx*hw+nx*hd, site.y+ty*hw+ny*hd),
                (site.x-tx*hw+nx*hd, site.y-ty*hw+ny*hd),
            ])
            if allowed.buffer(1e-6).covers(rect):
                return rect
        return None

    def _road_front_parcel(
        self, site: ShapelyPoint, cell, clip_region, cell_area: float,
        fallback_angle: float, preferred_angle: float | None = None,
    ):
        """Build a pure rectangle for one HP point.

        - SHORT side (width) lies along the road tangent — faces the road.
        - LONG side (depth) extends away from the road along the normal.
        - The rectangle is clipped to the cell envelope (no overlap with
          neighbouring parcels) and shrunk until all 4 corners lie inside
          the FAT/block polygon so the actual boundary is respected while
          the shape stays a perfect rectangle.
        """
        if site is None or cell is None or clip_region is None or cell.is_empty or clip_region.is_empty:
            return None

        road_solid = self._home_parcel_road_solid()
        road_point = None
        road_line_point = None
        # EVA AI may select a group axis; fallback remains FAT axis.
        tangent_angle = preferred_angle if preferred_angle is not None else fallback_angle
        if road_solid is not None and not road_solid.is_empty:
            try:
                _p_site, p_road = nearest_points(site, road_solid.boundary)
                road_point = (float(p_road.x), float(p_road.y))
            except Exception:
                road_point = None

        best_line = None
        best_dist = None
        if self.road_centerlines:
            for line in self.road_centerlines:
                try:
                    d = line.distance(site)
                except Exception:
                    continue
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best_line = line
            if best_line is not None and not best_line.is_empty:
                try:
                    proj = best_line.project(site)
                    p = best_line.interpolate(proj)
                    road_line_point = (float(p.x), float(p.y))
                    eps = min(2.0, max(0.25, best_line.length * 0.01))
                    p1 = best_line.interpolate(max(0.0, proj - eps))
                    p2 = best_line.interpolate(min(best_line.length, proj + eps))
                    dx, dy = float(p2.x - p1.x), float(p2.y - p1.y)
                    if preferred_angle is None and dx * dx + dy * dy > 1e-9:
                        tangent_angle = math.degrees(math.atan2(dy, dx))
                except Exception:
                    pass

        if road_point is None:
            road_point = road_line_point
        if road_point is None:
            road_point = (float(site.x), float(site.y))

        cos_t = math.cos(math.radians(tangent_angle))
        sin_t = math.sin(math.radians(tangent_angle))
        tx, ty = cos_t, sin_t
        nx, ny = -sin_t, cos_t
        vx, vy = float(site.x) - road_point[0], float(site.y) - road_point[1]
        if vx * nx + vy * ny < 0:
            nx, ny = -nx, -ny

        road_to_site = abs(vx * nx + vy * ny)
        # Standard large HP parcel module. Never derive its dimensions from
        # a tiny/irregular Voronoi cell: all parcels begin consistently sized.
        # Clipping below creates clean shared edges at FAT/ROAD/midpoint limits.
        width = 6.0          # compact house parcel base width (metres)
        depth = 10.0         # compact house parcel base depth (metres)
        # One house = one consistent compact module. Do not independently
        # grow parcels from their cell bounds: that produced mixed tiny/long
        # lots in one FAT group. Free space remains as free space.
        half_w = width / 2.0

        # Rectangle: front edge at road_point along tangent, back edge at road + depth along normal.
        # Side offset: align tangent center with HP point so HP lies inside the rectangle.
        side_offset = vx * tx + vy * ty
        cx_front = road_point[0] + tx * side_offset
        cy_front = road_point[1] + ty * side_offset
        cx_back = cx_front + nx * depth
        cy_back = cy_front + ny * depth
        rect_coords = [
            (cx_front - tx * half_w, cy_front - ty * half_w),
            (cx_front + tx * half_w, cy_front + ty * half_w),
            (cx_back + tx * half_w, cy_back + ty * half_w),
            (cx_back - tx * half_w, cy_back - ty * half_w),
        ]
        rect = Polygon(rect_coords)
        if rect.is_empty:
            return None

        # Hard clip, do NOT shrink. Ownership borders are exact midpoint lines
        # between HP points, so intersections produce touching parcels without
        # any overlap. `clip_region` is FAT AREA minus ROAD safety geometry.
        try:
            allowed = cell.intersection(clip_region)
        except Exception:
            allowed = clip_region
        if allowed is None or allowed.is_empty:
            return None
        try:
            parcel = rect.intersection(allowed)
        except Exception:
            return None
        if parcel is None or parcel.is_empty or parcel.area < KML_HOME_PARCEL_MIN_AREA_SQUARE_METERS:
            return None
        return parcel

    @staticmethod
    def _fit_rect_inside_polygon(rect, polygon, max_iterations: int = 30):
        """Shrink a rectangle uniformly about its center until all 4 corners
        lie inside `polygon`. Returns the rectangle (still a pure rectangle)
        or None if it cannot fit."""
        if polygon is None or polygon.is_empty:
            return None
        if rect is None or rect.is_empty:
            return None
        # First, try to intersect with polygon and take its MBR — already a rectangle.
        # Then iterate: while any corner is outside polygon, scale toward center.
        try:
            inter = rect.intersection(polygon)
            if inter is None or inter.is_empty:
                return None
            if isinstance(inter, Polygon):
                rect = inter.minimum_rotated_rectangle
        except Exception:
            pass
        if rect is None or rect.is_empty:
            return None
        for _ in range(max_iterations):
            coords = list(rect.exterior.coords)[:-1]
            if all(polygon.covers(ShapelyPoint(c)) for c in coords):
                return rect
            minx, miny, maxx, maxy = rect.bounds
            cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
            scale = 0.92
            new_coords = [
                (cx + (x - cx) * scale, cy + (y - cy) * scale)
                for x, y in coords
            ]
            rect = Polygon(new_coords)
            if rect.is_empty or rect.area < 1.0:
                return None
        return rect

    @staticmethod
    def _polygon_to_minimum_rectangle(polygon):
        """Replace a polygon's shape with its minimum-rotated-rectangle,
        clipped back to the original polygon so the rectangle never extends
        outside its cell. Returns the clipped rectangle or the original
        polygon on failure."""
        if polygon is None or polygon.is_empty:
            return polygon
        try:
            mrr = polygon.minimum_rotated_rectangle
            rect = mrr.intersection(polygon)
            if rect.is_empty:
                return polygon
            return rect
        except Exception:
            return polygon

    @staticmethod
    def _polygon_label_rotation(polygon) -> float:
        """Return the rotation angle (degrees) along the longest axis of a
        polygon so labels run along the cell's longest edge. Uses the
        minimum-rotated-rectangle so the axis matches the parcel edges
        (not diagonal corners). Returns 0 for degenerate polygons."""
        try:
            if polygon is None or polygon.is_empty:
                return 0.0
            mrr = polygon.minimum_rotated_rectangle
            coords = list(mrr.exterior.coords)
        except Exception:
            return 0.0
        if len(coords) < 4:
            return 0.0
        # Find the longest edge of the rotated rectangle.
        best_len_sq = -1.0
        best_angle = 0.0
        for i in range(4):
            x1, y1 = coords[i][0], coords[i][1]
            x2, y2 = coords[i + 1][0], coords[i + 1][1]
            dx, dy = x2 - x1, y2 - y1
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq > best_len_sq:
                best_len_sq = seg_len_sq
                best_angle = math.degrees(math.atan2(dy, dx))
        # Normalize to (-90, 90] so labels stay readable (no upside-down text).
        while best_angle > 90.0:
            best_angle -= 180.0
        while best_angle <= -90.0:
            best_angle += 180.0
        return best_angle

    @staticmethod
    def _parcel_piece_containing(geometry, site: ShapelyPoint):
        """From a (possibly multi-part) geometry, return the polygon piece that
        contains the site point, or the largest piece as a fallback."""
        if geometry is None or geometry.is_empty:
            return None
        if isinstance(geometry, Polygon):
            return geometry
        pieces = [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
        if not pieces:
            return None
        for piece in pieces:
            if piece.covers(site):
                return piece
        # Never borrow the largest clipped piece: it belongs to another HP
        # when the source HP lies outside after FAT/ROAD clipping.
        return None

    def finalize_home_numbers(self, pipeline_logger=None) -> None:
        if not self.pending_home_numbers:
            return

        if KML_HOME_PARCEL_ENABLED and _shapely_voronoi_diagram is not None:
            # Never retain partial tessellation before legacy box fallback.
            # Otherwise the same HP COVER is drawn twice.
            parcel_entity_start = len(self.output_entities)
            parcel_box_start = len(self.home_number_boxes)
            try:
                self._finalize_home_numbers_as_parcels(pipeline_logger)
                return
            except Exception as exc:
                del self.output_entities[parcel_entity_start:]
                del self.home_number_boxes[parcel_box_start:]
                # Parcel tessellation is best-effort; if anything goes wrong
                # (degenerate geometry, GEOS edge case) fall back to the
                # original independent-box placement so output is never lost.
                if pipeline_logger is not None:
                    pipeline_log(
                        pipeline_logger,
                        "warn",
                        f"[3/6] HP COVER parcel tessellation gagal, fallback ke box mode: {exc}",
                    )

        total_home_numbers = len(self.pending_home_numbers)
        if pipeline_logger is not None:
            pipeline_progress(pipeline_logger, 69, "Menyusun HOME_NUMBER: menghitung area ROAD.")
            pipeline_log(
                pipeline_logger,
                "info",
                f"[3/6] HOME_NUMBER sub-step: building ROAD avoidance geometry from "
                f"{len(self.road_buffers) + len(self.road_lines):,} road geometry part(s).",
            )
        sub_step_start = time.perf_counter()
        road_geometry = self._road_avoidance_geometry()
        if pipeline_logger is not None:
            pipeline_log(
                pipeline_logger,
                "success",
                f"[3/6] HOME_NUMBER road avoidance geometry ready in "
                f"{format_stage_seconds(time.perf_counter() - sub_step_start)}.",
            )
        occupied_geometries = []
        text_geometries = []
        occupied_tree = None
        occupied_indexed_count = 0
        text_tree = None
        text_indexed_count = 0
        if pipeline_logger is not None:
            pipeline_progress(pipeline_logger, 70, "Menyusun HOME_NUMBER: deteksi titik berdekatan.")
            pipeline_log(
                pipeline_logger,
                "info",
                f"[3/6] HOME_NUMBER sub-step: scanning crowding for {total_home_numbers:,} HP COVER point(s).",
            )
        sub_step_start = time.perf_counter()
        crowded_indexes = self._home_number_crowded_indexes()
        if pipeline_logger is not None:
            pipeline_log(
                pipeline_logger,
                "success",
                f"[3/6] HOME_NUMBER crowding scan complete: {len(crowded_indexes):,} crowded point(s) in "
                f"{format_stage_seconds(time.perf_counter() - sub_step_start)}.",
            )
        fat_area_majority_rotations: dict[int, float] = {}
        if self.network_type == "UG":
            fat_area_majority_rotations = self._home_number_fat_area_majority_rotations(road_geometry, crowded_indexes)
            if pipeline_logger is not None and fat_area_majority_rotations:
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"[3/6] UG HOME_NUMBER orientation: {len(fat_area_majority_rotations):,} HP COVER point(s) "
                    "aligned to their FAT_AREA majority direction.",
                )
        progress_interval = progress_log_interval(total_home_numbers)
        for index, pending in enumerate(self.pending_home_numbers):
            if pipeline_logger is not None and index and index % progress_interval == 0:
                pipeline_progress(
                    pipeline_logger,
                    min(76, 70 + int(index / max(total_home_numbers, 1) * 6)),
                    f"Menyusun HOME_NUMBER / HP COVER... {index:,}/{total_home_numbers:,}.",
                )
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"[3/6] HOME_NUMBER placement progress: {index:,}/{total_home_numbers:,} point(s).",
                )
            text = str(pending["text"])
            projected = pending["projected"]
            width, height = self._home_number_box_size(text)
            occupancy_probe = self._home_number_box_polygon(projected, width, height).buffer(
                KML_HOME_NUMBER_OCCUPANCY_SEARCH_RADIUS_METERS
            )
            occupied_geometry = self._local_occupied_geometry(
                occupied_geometries,
                occupied_tree,
                occupied_indexed_count,
                occupancy_probe,
            )
            text_occupied_geometry = self._local_occupied_geometry(
                text_geometries,
                text_tree,
                text_indexed_count,
                occupancy_probe,
            )
            boundary_geometry = self._home_number_boundary_geometry(projected)
            placement = self._home_number_placement(
                projected,
                text,
                road_geometry,
                occupied_geometry,
                text_occupied_geometry,
                boundary_geometry,
                index in crowded_indexes,
                forced_rotation=fat_area_majority_rotations.get(index),
            )
            output_start_index = len(self.output_entities)
            self._draw_home_number(text, placement)
            self.register_fdt_entities(pending.get("fdt_number"), self.output_entities[output_start_index:])
            text_box = placement["text_box"]
            if text_box is not None and not text_box.is_empty:
                text_geometries.append(text_box)
                if STRtree is not None and len(text_geometries) - text_indexed_count >= 96:
                    text_tree = STRtree(text_geometries)
                    text_indexed_count = len(text_geometries)
            clipped_box = placement["clipped_box"]
            if clipped_box is None or clipped_box.is_empty:
                continue
            self.home_number_boxes.append(clipped_box)
            occupied_geometries.append(clipped_box)
            if STRtree is not None and len(occupied_geometries) - occupied_indexed_count >= 96:
                occupied_tree = STRtree(occupied_geometries)
                occupied_indexed_count = len(occupied_geometries)

    def _longest_linestring(self, geometry) -> LineString | None:
        if geometry is None or geometry.is_empty:
            return None
        if isinstance(geometry, LineString):
            return geometry if geometry.length > 0 else None
        if isinstance(geometry, MultiLineString):
            lines = [line for line in geometry.geoms if line.length > 0]
            return max(lines, key=lambda line: line.length) if lines else None
        lines = [
            line
            for part in getattr(geometry, "geoms", [])
            if (line := self._longest_linestring(part)) is not None
        ]
        return max(lines, key=lambda line: line.length) if lines else None

    def _road_label_angle(self, line: LineString) -> float:
        midpoint_distance = line.length / 2
        delta = min(max(line.length * 0.12, 1.0), 8.0)
        start = line.interpolate(max(0.0, midpoint_distance - delta))
        end = line.interpolate(min(line.length, midpoint_distance + delta))
        angle = math.degrees(math.atan2(end.y - start.y, end.x - start.x))
        return self._readable_angle(angle)

    def _road_label_height(self, name: str, line: LineString) -> float:
        available_width = max(line.length * 0.7, ROAD_NAME_LABEL_MIN_LENGTH_METERS)
        height_by_width = available_width / max(len(name), 1) / ROAD_NAME_LABEL_WIDTH_FACTOR
        return _clamp(
            min(ROAD_NAME_LABEL_HEIGHT_METERS, height_by_width),
            ROAD_NAME_LABEL_MIN_HEIGHT_METERS,
            ROAD_NAME_LABEL_HEIGHT_METERS,
        )

    def _road_label_too_close(self, normalized_name: str, center: Point) -> bool:
        return any(
            existing_name == normalized_name and _distance(existing_center, center) < ROAD_NAME_LABEL_REPEAT_DISTANCE_METERS
            for existing_name, existing_center in self.road_name_label_centers
        )

    def finalize_road_names(self, pipeline_logger=None) -> None:
        if not self.road_name_candidates:
            return

        road_geometry = self._road_avoidance_geometry()
        total_candidates = len(self.road_name_candidates)
        progress_interval = progress_log_interval(total_candidates)
        for index, candidate in enumerate(self.road_name_candidates, start=1):
            if pipeline_logger is not None and index > 1 and (index - 1) % progress_interval == 0:
                pipeline_progress(
                    pipeline_logger,
                    min(67, 58 + int((index - 1) / max(total_candidates, 1) * 9)),
                    f"Membuat label nama jalan... {index - 1:,}/{total_candidates:,}.",
                )
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"[2/6] Road label progress: {index - 1:,}/{total_candidates:,} candidate(s) scanned, "
                    f"{len(self.road_name_label_centers):,} label(s) placed.",
                )
            name = str(candidate["name"]).strip().upper()
            line = candidate["line"]
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                line = self._longest_linestring(line.intersection(self.road_clip_geometry))
                if line is None:
                    continue
            if line.length < ROAD_NAME_LABEL_MIN_LENGTH_METERS:
                continue

            midpoint = line.interpolate(0.5, normalized=True)
            center = (float(midpoint.x), float(midpoint.y))
            if road_geometry is not None and not road_geometry.buffer(0.05).covers(midpoint):
                continue
            normalized_name = normalized_folder_name(name)
            if self._road_label_too_close(normalized_name, center):
                continue

            self._add_centered_text(
                name,
                center,
                self._road_label_height(name, line),
                dxfattribs={"layer": "ROAD", "color": 7},
                rotation=self._road_label_angle(line),
            )
            self.road_name_label_centers.append((normalized_name, center))

    def _add_kml_label(self, name: str, projected: Point, style: dict | None = None) -> None:
        if not name.strip():
            return
        self.kml_detail_stats["labels"] += 1
        scale = float((style or {}).get("icon_scale") or 1.0)
        x, y = projected
        offset = KML_ICON_SIZE_METERS * scale * 0.75
        self._add_text(
            name,
            (x + offset, y + offset),
            KML_LABEL_HEIGHT_METERS * scale,
            dxfattribs=self._kml_dxf_attrs(style, "icon_color"),
        )

    def add_kml_point(
        self,
        coordinate: tuple[float, float],
        name: str = "",
        style: dict | None = None,
        folder_name: str | None = None,
        folder_stack: Sequence[str] | None = None,
    ) -> None:
        if "KML" not in self.selected_layers:
            return

        projected = project_point(coordinate[0], coordinate[1], self.transformer)
        pole_spec = kml_pole_folder_spec(folder_name)
        handhole_spec = kml_handhole_folder_spec(folder_name)
        fdt_number = detect_parent_fdt_number(folder_stack or [])
        if is_hp_cover_folder(folder_name):
            preview_name = self._queue_home_number(projected, name, fdt_number=fdt_number)
            self.kml_detail_stats["home_number_points"] += 1
            self.stats[KML_HOME_NUMBER_LAYER] += 1
        elif is_hp_uncover_folder(folder_name):
            preview_name = self._queue_uncover_home_number(projected, name, fdt_number=fdt_number)
            self.kml_detail_stats["hp_uncover_points"] += 1
            self.stats[KML_UNCOVER_LAYER] += 1
        elif normalized_folder_name(folder_name or "") == KML_FAT_LAYER:
            fat_code = extract_fat_code_from_path(folder_stack or [], name)
            self._queue_kml_fat_icon(projected, fat_code, name, style, fdt_number=fdt_number)
            self.kml_detail_stats["fat_blocks"] += 1
            self.stats[KML_FAT_LAYER] += 1
            preview_name = fat_code
        elif normalized_folder_name(folder_name or "") == KML_OLT_LAYER:
            preview_name = self._queue_kml_olt_icon(projected, name, style, fdt_number=fdt_number)
            self.kml_detail_stats["olt_blocks"] += 1
            self.stats.setdefault(KML_OLT_LAYER, 0)
            self.stats[KML_OLT_LAYER] += 1
        elif is_fdt_folder(folder_name):
            fdt_label = extract_fdt_label_from_path(folder_stack or [], name)
            self._add_kml_fdt_icon(projected, fdt_label, style)
            self.kml_detail_stats["fdt_blocks"] += 1
            self.stats[KML_FDT_LAYER] += 1
            preview_name = fdt_label
        elif is_slack_hanger_folder(folder_name):
            self.pending_slack_icons.append({"projected": projected, "style": style})
            self.kml_detail_stats["slack_hanger_blocks"] += 1
            preview_name = name or "SLACK HANGER"
        elif is_joint_closure_folder(folder_name):
            self.pending_joint_closure_icons.append({"projected": projected, "style": style})
            self.kml_detail_stats["slack_hanger_blocks"] += 1
            preview_name = name or "JOINT CLOSURE"
        elif handhole_spec:
            preview_name = self._queue_kml_handhole_icon(
                projected,
                handhole_spec,
                name,
                style,
                fdt_number=fdt_number,
            )
            self.kml_detail_stats["handhole_blocks"] += 1
            self.stats[str(handhole_spec["layer"])] += 1
        elif is_twey_folder(folder_name):
            self._queue_kml_twey_icon(projected, name, style, fdt_number=fdt_number)
            self.kml_detail_stats["twey_blocks"] += 1
            self.stats[KML_TWEY_LAYER] += 1
            preview_name = name or "T-WAY"
        elif pole_spec:
            self._add_kml_pole_icon(projected, pole_spec, name, style, fdt_number=fdt_number)
            self.kml_detail_stats["pole_blocks"] += 1
            preview_name = name or str(pole_spec["label"])
        else:
            self._add_kml_icon(projected, style)
            self.kml_detail_stats["generic_icons"] += 1
            self._add_kml_label(name, projected, style)
            preview_name = name
        self.stats["KML"] += 1
        self._add_kml_preview_feature("Point", coordinate, preview_name)

    def add_kml_linestring(
        self,
        coordinates: list[tuple[float, float]],
        name: str = "",
        style: dict | None = None,
        folder_stack: Sequence[str] | None = None,
    ) -> None:
        if "KML" not in self.selected_layers or len(coordinates) < 2:
            return

        projected = project_coordinates(coordinates, self.transformer)
        clean_projected = _clean_points(projected)
        line = None
        if len(clean_projected) >= 2:
            line = LineString(clean_projected)
            if not line.is_empty and line.length > 0:
                self.kml_line_centerlines.append(line)
        attrs = self._kml_dxf_attrs(style, "line_color")
        line_spec = kml_special_linestring_spec(folder_stack or [], name)
        line_layer = str(line_spec["layer"]) if line_spec else None
        line_width = None
        layer_name = None
        if line_spec and line_layer:
            attrs["color"] = true_color_to_aci(attrs.get("true_color"))
            layer_name = self._ensure_layer(line_layer, attrs["color"])
            attrs["layer"] = layer_name
            attrs["lineweight"] = int(line_spec.get("lineweight") or 20)
            line_width = float(line_spec.get("width") or KML_DISTRIBUTION_CABLE_WIDTH)
            if line is not None and not line.is_empty and line.length > 0:
                self.kml_cable_centerlines.append(line)

        polyline_entity = self._add_polyline(projected, close=False, dxfattribs=attrs, width=line_width)
        if line_spec and is_distribution_cable_folder(folder_stack or []):
            self._add_distribution_cable_distance_labels(coordinates, projected)
        if polyline_entity is not None and line_spec:
            draw_order = str(line_spec.get("draw_order") or "")
            if draw_order == "subduct":
                self.subduct_line_entities.append(polyline_entity)
            elif draw_order == "top_cable":
                self.top_cable_line_entities.append(polyline_entity)
            # Snap cable endpoints to nearby poles during finalize so cables visually
            # attach to the pole centre.
            if is_distribution_cable_folder(folder_stack or []):
                self.cable_endpoint_snap_entities.append(polyline_entity)
        self.kml_detail_stats["line_features"] += 1
        midpoint = LineString(projected).interpolate(0.5, normalized=True)
        if not line_layer:
            self._add_kml_label(name, (float(midpoint.x), float(midpoint.y)), style)
        self.stats["KML"] += 1
        if layer_name:
            self.stats[layer_name] += 1
        self._add_kml_preview_feature("LineString", coordinates, name)

    def add_kml_polygon(
        self,
        outer_coordinates: list[tuple[float, float]],
        inner_rings: list[list[tuple[float, float]]] | None = None,
        name: str = "",
        style: dict | None = None,
        folder_name: str | None = None,
        folder_stack: Sequence[str] | None = None,
    ) -> None:
        if "KML" not in self.selected_layers or len(outer_coordinates) < 4:
            return

        is_boundary_fat = is_boundary_fat_folder(folder_name)
        if is_boundary_fat:
            layer_name = self._ensure_layer(
                KML_FAT_AREA_LAYER,
                KML_FAT_AREA_COLOR,
                linetype=KML_FAT_AREA_LINETYPE,
            )
            attrs = {
                "layer": layer_name,
                "color": 256,
            }
        else:
            attrs = self._kml_dxf_attrs(style, "poly_color")
        projected_outer = project_coordinates(outer_coordinates, self.transformer)
        valid_inner_rings = []
        projected_inner_rings = []
        for inner_ring in inner_rings or []:
            if len(inner_ring) < 4:
                continue
            valid_inner_rings.append(inner_ring)
            projected_inner_rings.append(project_coordinates(inner_ring, self.transformer))

        polygon_for_validation = Polygon(projected_outer, projected_inner_rings)
        if not polygon_for_validation.is_valid:
            self.repaired_polygons += 1
            polygon_for_validation = polygon_for_validation.buffer(0)
        if is_boundary_fat:
            self._register_fat_area_geometry(polygon_for_validation)

        self._add_polyline(projected_outer, close=True, dxfattribs=attrs)
        self.kml_detail_stats["polygon_features"] += 1

        for projected_inner in projected_inner_rings:
            self._add_polyline(projected_inner, close=True, dxfattribs=attrs)

        if not is_boundary_fat:
            if polygon_for_validation.is_valid and not polygon_for_validation.is_empty:
                center = polygon_for_validation.representative_point()
                self._add_kml_label(name, (float(center.x), float(center.y)), style)
        self.stats["KML"] += 1
        if is_boundary_fat:
            self.stats[KML_FAT_AREA_LAYER] += 1
        preview_name = "" if is_boundary_fat else name
        self._add_kml_preview_feature("Polygon", [outer_coordinates] + valid_inner_rings, preview_name)

    def _add_kml_preview_feature(self, geometry_type: str, coordinates, name: str) -> None:
        if len(self.preview_features) >= self.preview_feature_limit:
            return

        self.preview_features.append(
            {
                "type": "Feature",
                "properties": {"layer": "KML", "name": name, "osm_type": "kml"},
                "geometry": {"type": geometry_type, "coordinates": coordinates},
            }
        )

    def apply_cable_draw_order(self) -> None:
        if not (
            self.subduct_line_entities
            or self.top_cable_line_entities
            or self.twey_overlay_entities
            or self.fat_fdt_overlay_entities
        ):
            return

        redraw_order = dict(self.modelspace.get_redraw_order())
        used_handles: set[str] = set()

        def append_group(entities: Sequence[object], start_sort_handle: int) -> None:
            offset = 0
            for entity in entities:
                handle = str(getattr(getattr(entity, "dxf", None), "handle", "") or "")
                if not handle or handle in used_handles:
                    continue
                used_handles.add(handle)
                redraw_order[handle] = f"{start_sort_handle + offset:X}"
                offset += 1

        append_group(self.subduct_line_entities, 0xF00000)
        append_group(self.top_cable_line_entities, 0xF10000)
        append_group(self.twey_overlay_entities, 0xF20000)
        append_group(self.fat_fdt_overlay_entities, 0xF30000)
        self.modelspace.set_redraw_order(redraw_order)

    def save(self) -> None:
        if self.uses_dxf_template:
            self.assign_fdt_folder_entities_by_suffix()
            self.fit_template_layout_viewport_to_output()
            self.populate_template_design_summary()
            self.create_fdt_layout_viewports()
        else:
            self.fit_entities_to_right_layout()
            self.add_standard_layout_frame()
        self.remove_fat_info_artifacts()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.dxfversion = OUTPUT_DXF_VERSION
        self._turn_all_layers_visible()
        self.apply_cable_draw_order()
        self.prepare_autocad_lt_output()
        self.doc.audit()
        self.doc.saveas(self.output_path)
        self.patch_ac1015_linetype_pattern_flags()
        self.patch_autocad_lt_header_extents()

    def prepare_autocad_lt_output(self) -> None:
        for style in self.doc.styles:
            try:
                # Preserve the converter's real bold TTF style. Other styles
                # remain on txt.shx for AutoCAD LT compatibility.
                if style.dxf.name == "EVA_BOLD":
                    style.dxf.font = "arialbd.ttf"
                else:
                    style.dxf.font = "txt.shx"
                style.dxf.bigfont = ""
            except Exception:
                continue

        for entity in list(self.modelspace):
            if entity.dxftype() == "VIEWPORT":
                try:
                    self.modelspace.delete_entity(entity)
                except Exception:
                    continue

        # XData cleanup is case-sensitive in ezdxf, but DXF appid registration is
        # case-insensitive. Build a case-insensitive set so we strip xdata regardless
        # of how it was originally written (e.g. "AcDbBlockRepETag" vs "ACDBBLOCKREPETAG").
        appid_targets_lower = {appid.lower() for appid in AUTOCAD_LT_XDATA_APPIDS}
        for entity in list(self.doc.entitydb.values()):
            xdata = getattr(entity, "xdata", None)
            if xdata is None:
                continue
            try:
                actual_appids = list(xdata.data.keys())
            except Exception:
                continue
            for actual_appid in actual_appids:
                if actual_appid.lower() in appid_targets_lower:
                    try:
                        entity.discard_xdata(actual_appid)
                    except Exception:
                        continue

        for appid in AUTOCAD_LT_XDATA_APPIDS:
            try:
                self.doc.appids.discard(appid)
            except Exception:
                continue

        try:
            extent = bbox.extents(list(self.modelspace))
        except Exception as exc:
            LOGGER.debug("Gagal menghitung extents AutoCAD LT: %s", exc)
            return

        self.doc.header["$EXTMIN"] = (float(extent.extmin.x), float(extent.extmin.y), 0.0)
        self.doc.header["$EXTMAX"] = (float(extent.extmax.x), float(extent.extmax.y), 0.0)
        self.doc.header["$LIMMIN"] = (float(extent.extmin.x), float(extent.extmin.y))
        self.doc.header["$LIMMAX"] = (float(extent.extmax.x), float(extent.extmax.y))
        self.autocad_lt_extents = (
            float(extent.extmin.x),
            float(extent.extmin.y),
            float(extent.extmax.x),
            float(extent.extmax.y),
        )
        self.update_modelspace_initial_view(extent)

    def update_modelspace_initial_view(self, extent) -> None:
        if not getattr(extent, "has_data", False):
            return

        min_x, min_y = float(extent.extmin.x), float(extent.extmin.y)
        max_x, max_y = float(extent.extmax.x), float(extent.extmax.y)
        source_width = max(max_x - min_x, 1.0)
        source_height = max(max_y - min_y, 1.0)
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0

        for vport in self.doc.viewports:
            try:
                if str(vport.dxf.name).upper() != "*ACTIVE":
                    continue
                aspect_ratio = max(float(vport.dxf.get("aspect_ratio", 1.6) or 1.6), 0.1)
                vport.dxf.center = (center_x, center_y)
                vport.dxf.height = max(source_height, source_width / aspect_ratio) * 1.08
                vport.dxf.lower_left = (0.0, 0.0)
                vport.dxf.upper_right = (1.0, 1.0)
                vport.dxf.target = (0.0, 0.0, 0.0)
                vport.dxf.direction = (0.0, 0.0, 1.0)
                vport.dxf.view_twist = 0.0
            except Exception as exc:
                LOGGER.debug("Gagal mengatur initial view modelspace: %s", exc)

    def _layout_name_for_scope(self) -> str:
        """Layout name for the active scope (Cluster vs Feeder)."""
        return "Feeder" if is_feeder_like_scope(self.scope_type) else TEMPLATE_LAYOUT_NAME

    def _fdt_layout_prefix_for_scope(self) -> str:
        """Prefix used for per-FDT layouts under the active scope."""
        if is_feeder_like_scope(self.scope_type):
            return "Feeder-FDT"
        return FDT_LAYOUT_PREFIX

    def ensure_template_layout(self):
        scoped_name = self._layout_name_for_scope()
        # If the scope expects "Feeder" but the template still has "Cluster"
        # (or legacy "Layout1"), rename it so AutoCAD shows the correct tab.
        try:
            return self.doc.layouts.get(scoped_name)
        except Exception:
            pass
        for legacy in (TEMPLATE_LAYOUT_NAME, LEGACY_TEMPLATE_LAYOUT_NAME):
            if legacy == scoped_name:
                continue
            try:
                self.doc.layouts.rename(legacy, scoped_name)
                return self.doc.layouts.get(scoped_name)
            except Exception:
                continue
        try:
            return self.doc.layouts.get(TEMPLATE_LAYOUT_NAME)
        except Exception as exc:
            LOGGER.debug("Layout template %s tidak ditemukan: %s", scoped_name, exc)
            return None

    def _set_layout_viewport_to_entities(
        self,
        layout,
        entities: Sequence[object],
        viewport_bounds: tuple[float, float, float, float] | None = None,
    ) -> None:
        if not entities:
            return

        try:
            extent = bbox.extents(list(entities))
        except Exception as exc:
            LOGGER.debug("Gagal menghitung extents viewport layout: %s", exc)
            return
        if not extent.has_data:
            return

        min_x, min_y = float(extent.extmin.x), float(extent.extmin.y)
        max_x, max_y = float(extent.extmax.x), float(extent.extmax.y)
        source_width = max(max_x - min_x, 1.0)
        source_height = max(max_y - min_y, 1.0)
        view_center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        paper_center, paper_size = self._viewport_bounds_to_center_size(viewport_bounds)
        self._ensure_viewports_layer_style()
        self._normalize_layout_page_setup(layout)
        self._normalize_layout_paper_viewport(layout)
        viewports = [
            entity
            for entity in layout
            if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("status", 0) or 0) > 1
        ]
        if not viewports:
            try:
                viewports = [
                    layout.add_viewport(
                        center=paper_center,
                        size=paper_size,
                        view_center_point=view_center,
                        view_height=source_height,
                        status=2,
                    )
                ]
            except Exception as exc:
                LOGGER.debug("Gagal membuat viewport layout template: %s", exc)
                return

        for viewport in viewports:
            self._style_viewport_entity(viewport)
            try:
                viewport.dxf.center = (float(paper_center[0]), float(paper_center[1]), 0.0)
                viewport.dxf.width = float(paper_size[0])
                viewport.dxf.height = float(paper_size[1])
            except Exception:
                pass
            paper_width = max(float(paper_size[0]), 1.0)
            paper_height = max(float(paper_size[1]), 1.0)
            paper_aspect = paper_width / paper_height
            view_height = max(source_height, source_width / max(paper_aspect, 0.01)) * 1.02
            try:
                viewport.dxf.view_target_point = (0.0, 0.0, 0.0)
                viewport.dxf.view_direction_vector = (0.0, 0.0, 1.0)
                viewport.dxf.view_twist_angle = 0.0
                viewport.dxf.view_height = float(view_height)
                viewport.dxf.view_center_point = (float(view_center[0]), float(view_center[1]))
                viewport.dxf.perspective_lens_length = 50.0
                viewport.dxf.front_clip_plane_z_value = 0.0
                viewport.dxf.back_clip_plane_z_value = 0.0
                viewport.dxf.render_mode = 0
                viewport.dxf.circle_zoom = 100
                viewport.dxf.ucs_icon = 0
                viewport.dxf.snap_angle = 0.0
                viewport.dxf.snap_base_point = (0.0, 0.0)
                viewport.dxf.snap_spacing = (10.0, 10.0)
                viewport.dxf.grid_spacing = (10.0, 10.0)
            except Exception as exc:
                LOGGER.debug("Gagal mengatur viewport layout template: %s", exc)

    def _style_viewport_entity(self, viewport) -> None:
        try:
            viewport.dxf.layer = TEMPLATE_VIEWPORT_LAYER
            viewport.dxf.color = WHITE_ACI_COLOR
            viewport.dxf.true_color = WHITE_TRUE_COLOR
        except Exception as exc:
            LOGGER.debug("Viewport tidak bisa diset putih: %s", exc)

    def _paper_viewport_center_size(self) -> tuple[tuple[float, float], tuple[float, float]]:
        if is_feeder_like_scope(self.scope_type):
            return TEMPLATE_FEEDER_PAPER_VIEWPORTS.get(
                self.network_type,
                TEMPLATE_FEEDER_PAPER_VIEWPORTS["AE"],
            )
        return TEMPLATE_PAPER_VIEWPORT_CENTER, TEMPLATE_PAPER_VIEWPORT_SIZE

    def _normalize_layout_paper_viewport(self, layout) -> None:
        paper_center, paper_size = self._paper_viewport_center_size()
        for viewport in layout:
            if viewport.dxftype() != "VIEWPORT":
                continue
            try:
                self._style_viewport_entity(viewport)
                if int(viewport.dxf.get("status", 0) or 0) > 1:
                    continue
                viewport.dxf.center = (paper_center[0], paper_center[1], 0.0)
                viewport.dxf.width = paper_size[0]
                viewport.dxf.height = paper_size[1]
                viewport.dxf.view_center_point = paper_center
                viewport.dxf.view_height = paper_size[1]
            except Exception as exc:
                LOGGER.debug("Gagal mengatur paper viewport layout: %s", exc)

    def _normalize_layout_page_setup(self, layout) -> None:
        try:
            layout.page_setup(
                size=TEMPLATE_PAPER_SIZE,
                margins=(0.0, 0.0, 0.0, 0.0),
                units="mm",
                offset=(0.0, 0.0),
                rotation=0,
                scale=16,
                name="A3",
                device="DWG To PDF.pc3",
            )
        except Exception as exc:
            LOGGER.debug("Gagal mengatur page setup layout: %s", exc)

    def _viewport_bounds_to_center_size(
        self,
        bounds: tuple[float, float, float, float] | None,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if bounds is None:
            return TEMPLATE_LAYOUT_VIEWPORT_CENTER, TEMPLATE_LAYOUT_VIEWPORT_SIZE
        min_x, min_y, max_x, max_y = bounds
        return (
            ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
            (max(max_x - min_x, 1.0), max(max_y - min_y, 1.0)),
        )

    def fit_template_layout_viewport_to_output(self) -> None:
        layout = self.ensure_template_layout()
        if layout is None:
            return
        self._set_layout_viewport_to_entities(layout, self.output_entities, TEMPLATE_CLUSTER_VIEWPORT_BOUNDS)

    def create_fdt_layout_viewports(self) -> None:
        base_layout = self.ensure_template_layout()
        if base_layout is None:
            return

        fdt_prefix = self._fdt_layout_prefix_for_scope()
        # Clean up any pre-existing per-FDT layouts (both Cluster and Feeder
        # variants) so we don't leak old tabs when scope changes.
        for layout_name in list(self.doc.layout_names()):
            if layout_name.startswith(f"{FDT_LAYOUT_PREFIX} ") or layout_name.startswith("Feeder-FDT "):
                try:
                    self.doc.layouts.delete(layout_name)
                except Exception as exc:
                    LOGGER.debug("Gagal menghapus layout lama %s: %s", layout_name, exc)

        # Feeder scope keeps a single "Feeder" layout for all FDTs (no per-FDT tabs).
        if is_feeder_like_scope(self.scope_type):
            return

        fdt_numbers = sorted(fdt for fdt in self.detected_fdt_numbers if fdt in self.fdt_output_entities)
        if len(fdt_numbers) <= 1:
            return

        template_entities = [
            entity
            for entity in base_layout
            if entity.dxftype() != "VIEWPORT" and not self._is_design_summary_entity(entity)
        ]
        for fdt_number in fdt_numbers:
            layout_name = f"{fdt_prefix} {fdt_number}"
            try:
                layout = self.doc.layouts.new(layout_name)
            except Exception:
                layout = self.doc.layouts.get(layout_name)

            for entity in list(layout):
                try:
                    layout.delete_entity(entity)
                except Exception:
                    pass
            try:
                layout.reset_viewports()
            except Exception:
                pass
            for entity in template_entities:
                try:
                    layout.add_entity(entity.copy())
                except Exception as exc:
                    LOGGER.debug("Gagal copy entity template ke %s: %s", layout_name, exc)
            self._set_layout_viewport_to_entities(
                layout,
                self.fdt_output_entities[fdt_number],
                TEMPLATE_FDT_VIEWPORT_BOUNDS,
            )

    def add_fat_layout_callouts(self) -> None:
        self.remove_fat_info_artifacts()

    def remove_fat_info_artifacts(self) -> None:
        # Hapus semua entity FAT_Info dari semua layouts (termasuk Model)
        for layout in list(self.doc.layouts):
            self._clear_fat_layout_callouts(layout)

        # Hapus juga dari modelspace secara eksplisit (safety net)
        msp = self.doc.modelspace()
        for entity in list(msp):
            if self._is_fat_info_entity(entity):
                try:
                    msp.delete_entity(entity)
                except Exception:
                    pass

        # Hapus block definition
        if FAT_CALLOUT_BLOCK_NAME in self.doc.blocks:
            try:
                self.doc.blocks.delete_block(FAT_CALLOUT_BLOCK_NAME, safe=False)
            except Exception:
                # Kalau gagal hapus block, kosongkan isinya saja
                try:
                    block = self.doc.blocks.get(FAT_CALLOUT_BLOCK_NAME)
                    for entity in list(block):
                        try:
                            block.delete_entity(entity)
                        except Exception:
                            pass
                except Exception as exc:
                    LOGGER.debug("Gagal mengosongkan block %s: %s", FAT_CALLOUT_BLOCK_NAME, exc)

        # Hapus layer
        if FAT_CALLOUT_LAYER in self.doc.layers:
            try:
                self.doc.layers.remove(FAT_CALLOUT_LAYER)
            except Exception as exc:
                LOGGER.debug("Gagal menghapus layer %s: %s", FAT_CALLOUT_LAYER, exc)

    def _is_fat_info_entity(self, entity) -> bool:
        if str(entity.dxf.get("layer", "")).upper() == FAT_CALLOUT_LAYER.upper():
            return True
        if entity.dxftype() == "INSERT" and str(entity.dxf.get("name", "")).upper() == FAT_CALLOUT_BLOCK_NAME.upper():
            return True
        return False

    def _add_fat_layout_callouts(self) -> None:
        if not self.fat_callout_refs:
            return
        base_layout = self.ensure_template_layout()
        if base_layout is None:
            return

        fdt_numbers = sorted(
            {
                int(number)
                for number in [
                    *self.detected_fdt_numbers,
                    *(
                        ref.get("fdt_number")
                        for ref in self.fat_callout_refs
                        if ref.get("fdt_number")
                    ),
                ]
                if int(number or 0) > 0
            }
        )
        if len(fdt_numbers) <= 1:
            self._clear_fat_layout_callouts(base_layout)
            self._add_fat_callouts_to_layout(base_layout, self.fat_callout_refs, self.output_entities)
            return

        self._clear_fat_layout_callouts(base_layout)
        fdt_prefix = self._fdt_layout_prefix_for_scope()
        for fdt_number in fdt_numbers:
            try:
                layout = self.doc.layouts.get(f"{fdt_prefix} {fdt_number}")
            except Exception:
                continue
            refs = [
                ref
                for ref in self.fat_callout_refs
                if int(ref.get("fdt_number") or 0) == fdt_number
            ]
            self._clear_fat_layout_callouts(layout)
            self._add_fat_callouts_to_layout(layout, refs, self.fdt_output_entities.get(fdt_number, []))

    def _clear_fat_layout_callouts(self, layout) -> None:
        for entity in list(layout):
            if not self._is_fat_info_entity(entity):
                continue
            try:
                layout.delete_entity(entity)
            except Exception:
                pass

    def _active_layout_viewport(self, layout):
        viewports = [
            entity
            for entity in layout
            if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("status", 0) or 0) > 1
        ]
        return viewports[-1] if viewports else None

    def _model_point_to_layout_paper(self, layout, point: Point) -> Point | None:
        viewport = self._active_layout_viewport(layout)
        if viewport is None:
            return None
        try:
            view_center = viewport.dxf.view_center_point
            paper_center = viewport.dxf.center
            view_height = max(float(viewport.dxf.view_height), 1e-9)
            paper_height = max(float(viewport.dxf.height), 1e-9)
            scale = paper_height / view_height
            dx = float(point[0]) - float(view_center.x)
            dy = float(point[1]) - float(view_center.y)
            twist = math.radians(float(viewport.dxf.get("view_twist_angle", 0.0) or 0.0))
            cos_twist = math.cos(-twist)
            sin_twist = math.sin(-twist)
            rotated_x = dx * cos_twist - dy * sin_twist
            rotated_y = dx * sin_twist + dy * cos_twist
            return (
                float(paper_center.x) + rotated_x * scale,
                float(paper_center.y) + rotated_y * scale,
            )
        except Exception as exc:
            LOGGER.debug("Gagal memetakan point model ke paper layout: %s", exc)
            return None

    def _add_fat_callouts_to_layout(self, layout, refs: Sequence[dict], occupied_entities: Sequence[object]) -> None:
        if not refs:
            return
        attrs = {"layer": FAT_CALLOUT_LAYER, "color": FAT_CALLOUT_COLOR}
        boxes: list[tuple[float, float, float, float]] = []
        occupied_boxes = self._layout_output_occupancy_rects(layout, occupied_entities)
        candidates = self._fat_callout_candidate_rects(layout)
        refs_with_paper_points = []
        for ref in refs:
            anchor = self._model_point_to_layout_paper(layout, ref.get("anchor") or (0.0, 0.0))
            if anchor is None:
                continue
            refs_with_paper_points.append((ref, anchor))

        for index, (ref, anchor) in enumerate(sorted(refs_with_paper_points, key=lambda item: (-item[1][1], item[1][0]))):
            rect = self._fat_callout_rect(anchor, boxes, occupied_boxes, candidates, index)
            boxes.append(rect)
            self._draw_fat_callout(layout, rect, anchor, self._fat_callout_title(ref), attrs)

    def _layout_viewport_rect(self, layout) -> tuple[float, float, float, float] | None:
        viewport = self._active_layout_viewport(layout)
        if viewport is None:
            return None
        try:
            center = viewport.dxf.center
            width = float(viewport.dxf.width)
            height = float(viewport.dxf.height)
            return (
                float(center.x) - width / 2.0,
                float(center.y) - height / 2.0,
                float(center.x) + width / 2.0,
                float(center.y) + height / 2.0,
            )
        except Exception:
            return None

    def _layout_output_occupancy_rects(self, layout, entities: Sequence[object]) -> list[tuple[float, float, float, float]]:
        rects: list[tuple[float, float, float, float]] = []
        for entity in entities:
            try:
                extent = bbox.extents([entity])
            except Exception:
                continue
            if not getattr(extent, "has_data", False):
                continue
            corners = [
                (float(extent.extmin.x), float(extent.extmin.y)),
                (float(extent.extmax.x), float(extent.extmin.y)),
                (float(extent.extmax.x), float(extent.extmax.y)),
                (float(extent.extmin.x), float(extent.extmax.y)),
            ]
            paper_points = [point for corner in corners if (point := self._model_point_to_layout_paper(layout, corner)) is not None]
            if not paper_points:
                continue
            xs = [point[0] for point in paper_points]
            ys = [point[1] for point in paper_points]
            rects.append((min(xs), min(ys), max(xs), max(ys)))
        return rects

    def _layout_protected_callout_rects(self, layout) -> list[tuple[float, float, float, float]]:
        has_design_summary = any(
            str(entity.dxf.get("layer", "")).upper().replace(" ", "_") == "DESIGN_SUMMARY"
            for entity in layout
        )
        return [FDT_DESIGN_SUMMARY_CLEAR_BOUNDS] if has_design_summary else []

    def _fat_callout_candidate_rects(self, layout) -> list[tuple[float, float, float, float]]:
        width = FAT_CALLOUT_WIDTH
        height = FAT_CALLOUT_ROW_HEIGHT * 3.0
        step_x = width + FAT_CALLOUT_GAP
        step_y = height + FAT_CALLOUT_GAP
        candidates: list[tuple[float, float, float, float]] = []
        viewport_rect = self._layout_viewport_rect(layout)
        protected_rects = self._layout_protected_callout_rects(layout)

        def append_grid(min_x: float, min_y: float, max_x: float, max_y: float, top_down: bool = True) -> None:
            if max_x - min_x < width or max_y - min_y < height:
                return
            x_values = []
            x = min_x
            while x <= max_x - width + 1e-6:
                x_values.append(x)
                x += step_x
            y_values = []
            y = max_y - height if top_down else min_y
            while min_y - 1e-6 <= y <= max_y - height + 1e-6:
                y_values.append(y)
                y += -step_y if top_down else step_y
            for y in y_values:
                for x in x_values:
                    candidates.append((x, y, x + width, y + height))

        if viewport_rect is not None:
            min_x, min_y, max_x, max_y = viewport_rect
            append_grid(min_x + FAT_CALLOUT_MARGIN, min_y + FAT_CALLOUT_MARGIN, max_x - FAT_CALLOUT_MARGIN, max_y - FAT_CALLOUT_MARGIN)
        else:
            append_grid(FAT_CALLOUT_MARGIN, FAT_CALLOUT_MARGIN, TEMPLATE_PAPER_SIZE[0] - FAT_CALLOUT_MARGIN, TEMPLATE_PAPER_SIZE[1] - FAT_CALLOUT_MARGIN)

        if protected_rects:
            candidates = [
                rect
                for rect in candidates
                if not self._rect_overlaps_any(rect, protected_rects, padding=0.0)
            ]

        return candidates

    def _fat_callout_rect(
        self,
        anchor: Point,
        existing: Sequence[tuple[float, float, float, float]],
        occupied: Sequence[tuple[float, float, float, float]],
        candidates: Sequence[tuple[float, float, float, float]],
        index: int,
    ) -> tuple[float, float, float, float]:
        ordered_candidates = sorted(
            candidates,
            key=lambda rect: (
                self._rect_overlaps_any(rect, occupied, padding=0.25),
                (self._rect_center(rect)[0] - anchor[0]) ** 2 + (self._rect_center(rect)[1] - anchor[1]) ** 2,
            ),
        )
        existing_padding = max(0.25, FAT_CALLOUT_GAP / 2.0)
        for rect in ordered_candidates:
            if self._rect_overlaps_any(rect, existing, padding=existing_padding):
                continue
            if self._rect_overlaps_any(rect, occupied, padding=0.25):
                continue
            return rect
        for rect in ordered_candidates:
            if not self._rect_overlaps_any(rect, existing, padding=existing_padding):
                return rect
        return candidates[index % len(candidates)] if candidates else (FAT_CALLOUT_MARGIN, FAT_CALLOUT_MARGIN, FAT_CALLOUT_MARGIN + FAT_CALLOUT_WIDTH, FAT_CALLOUT_MARGIN + FAT_CALLOUT_ROW_HEIGHT * 3.0)

    def _rect_center(self, rect: tuple[float, float, float, float]) -> Point:
        min_x, min_y, max_x, max_y = rect
        return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)

    def _rect_overlaps_any(
        self,
        rect: tuple[float, float, float, float],
        existing: Sequence[tuple[float, float, float, float]],
        padding: float = 0.25,
    ) -> bool:
        min_x, min_y, max_x, max_y = rect
        for other_min_x, other_min_y, other_max_x, other_max_y in existing:
            if (
                min_x < other_max_x + padding
                and max_x > other_min_x - padding
                and min_y < other_max_y + padding
                and max_y > other_min_y - padding
            ):
                return True
        return False

    def _fat_callout_title(self, ref: dict) -> str:
        name = str(ref.get("name") or "").strip()
        code = str(ref.get("code") or "").strip() or "FAT"
        if "." in name and find_fat_code(name):
            return name
        return code

    def _draw_fat_callout(
        self,
        layout,
        rect: tuple[float, float, float, float],
        anchor: Point,
        title: str,
        attrs: dict,
    ) -> None:
        min_x, min_y, max_x, max_y = rect
        self._ensure_fat_info_block()
        attach_x = min_x if anchor[0] <= (min_x + max_x) / 2.0 else max_x
        attach_y = min(max(anchor[1], min_y), max_y)
        layout.add_line(anchor, (attach_x, attach_y), dxfattribs=attrs)

        insert = layout.add_blockref(
            FAT_CALLOUT_BLOCK_NAME,
            (min_x, min_y),
            dxfattribs={"layer": FAT_CALLOUT_LAYER, "color": FAT_CALLOUT_COLOR},
        )
        insert.add_auto_attribs(
            {
                FAT_CALLOUT_TITLE_TAG: title,
                FAT_CALLOUT_DISTANCE_TAG: "Distance to FDT : --- m",
                FAT_CALLOUT_POWER_TAG: "Power @1490 nm : --db",
            }
        )
        title_height = min(0.95, max(0.65, (FAT_CALLOUT_WIDTH - 1.2) / max(len(title), 1) / 0.62))
        for attrib in insert.attribs:
            tag = str(attrib.dxf.tag).upper()
            attrib.dxf.layer = FAT_CALLOUT_LAYER
            attrib.dxf.color = FAT_CALLOUT_COLOR
            if tag == FAT_CALLOUT_TITLE_TAG:
                attrib.dxf.height = title_height
                attrib.set_placement((min_x + FAT_CALLOUT_WIDTH / 2.0, min_y + FAT_CALLOUT_ROW_HEIGHT * 2.5, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
            elif tag == FAT_CALLOUT_DISTANCE_TAG:
                attrib.dxf.height = 0.68
                attrib.set_placement((min_x + FAT_CALLOUT_WIDTH / 2.0, min_y + FAT_CALLOUT_ROW_HEIGHT * 1.5, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
            elif tag == FAT_CALLOUT_POWER_TAG:
                attrib.dxf.height = 0.68
                attrib.set_placement((min_x + FAT_CALLOUT_WIDTH / 2.0, min_y + FAT_CALLOUT_ROW_HEIGHT * 0.5, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)

    def _ensure_fat_info_block(self) -> None:
        if FAT_CALLOUT_BLOCK_NAME in self.doc.blocks:
            return
        block = self.doc.blocks.new(name=FAT_CALLOUT_BLOCK_NAME)
        attrs = {"layer": FAT_CALLOUT_LAYER, "color": FAT_CALLOUT_COLOR}
        width = FAT_CALLOUT_WIDTH
        row = FAT_CALLOUT_ROW_HEIGHT
        height = row * 3.0
        block.add_lwpolyline(
            [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)],
            close=True,
            dxfattribs=attrs,
        )
        block.add_line((0.0, row), (width, row), dxfattribs=attrs)
        block.add_line((0.0, row * 2.0), (width, row * 2.0), dxfattribs=attrs)

        def add_attdef(tag: str, text: str, y: float, height_value: float) -> None:
            attdef = block.add_attdef(
                tag,
                insert=(width / 2.0, y),
                text=text,
                height=height_value,
                dxfattribs=attrs,
            )
            attdef.set_placement((width / 2.0, y), align=TextEntityAlignment.MIDDLE_CENTER)

        add_attdef(FAT_CALLOUT_TITLE_TAG, "FAT", row * 2.5, 0.9)
        add_attdef(FAT_CALLOUT_DISTANCE_TAG, "Distance to FDT : --- m", row * 1.5, 0.68)
        add_attdef(FAT_CALLOUT_POWER_TAG, "Power @1490 nm : --db", row * 0.5, 0.68)

    def _is_design_summary_entity(self, entity) -> bool:
        layer = str(entity.dxf.get("layer", "")).upper().replace(" ", "_")
        if layer == "DESIGN_SUMMARY":
            return True
        return self._entity_is_fully_inside_bounds(entity, FDT_DESIGN_SUMMARY_CLEAR_BOUNDS)

    def _entity_is_fully_inside_bounds(self, entity, bounds: tuple[float, float, float, float]) -> bool:
        try:
            extent = bbox.extents([entity])
        except Exception:
            return False
        if not getattr(extent, "has_data", False):
            return False
        min_x, min_y, max_x, max_y = bounds
        return (
            float(extent.extmin.x) >= min_x
            and float(extent.extmin.y) >= min_y
            and float(extent.extmax.x) <= max_x
            and float(extent.extmax.y) <= max_y
        )

    def dxf_design_summary_values(self) -> dict[str, int]:
        counts = self.dxf_summary_counts
        lengths = self.dxf_summary_lengths

        distribution_route = math.ceil(lengths.get("DISTRIBUTION_CABLE", 0.0))
        is_ug = self.network_type == "UG"
        is_feeder = is_feeder_like_scope(self.scope_type)
        aerial_route = 0 if is_ug else distribution_route
        duct_route = distribution_route if is_ug else 0
        fdt_count = counts.get("FDT", 0)
        fat_count = counts.get("FAT", 0)
        handhole_20_count = counts.get("NEW HH 20X20X20", 0) if is_ug else 0
        handhole_40_count = counts.get("NEW HH 40X40X60", 0) if is_ug else 0
        handhole_80_count = counts.get("NEW HH 80X80X130", 0) if is_ug else 0
        aerial_fat_count = 0 if is_ug else fat_count
        pedestal_fat_count = fat_count if is_ug else 0
        cable_loop_holder_count = self._design_summary_override_int(
            "TOTAL CABLE LOOP HOLDER/HANGER",
            counts.get("SLACK HANGER", 0),
        )
        # Subduct lengths for UG Feeder
        subduct_40 = math.ceil(lengths.get("SUBDUCT_40", 0.0)) if is_ug else 0
        subduct_32 = math.ceil(lengths.get("SUBDUCT_32", 0.0)) if is_ug else 0

        values = {
            'TOTAL NEW POLE 7M 4"': counts.get("NEW POLE 7-4", 0),
            'TOTAL NEW POLE 7M 3"': counts.get("NEW POLE 7-3", 0),
            'TOTAL NEW POLE 7M 2.5"': counts.get("NEW POLE 7-2.5", 0),
            'TOTAL NEW POLE 9M 4"': counts.get("NEW POLE 9-4", 0),
            'TOTAL POLE EXT EMR 7M 4"': counts.get("EXISTING POLE EMR 7-4", 0),
            'TOTAL POLE EXT EMR 7M 3"': counts.get("EXISTING POLE EMR 7-3", 0),
            'TOTAL POLE EXT EMR 7M 2.5"': counts.get("EXISTING POLE EMR 7-2.5", 0),
            'TOTAL POLE EXT EMR 9M 4"': counts.get("EXISTING POLE EMR 9-4", 0),
            "TOTAL POLE EXT MTI": counts.get("EXISTING_POLE_PARTNER_MTI", 0),
            "TOTAL POLE EXT LINKNET": counts.get("EXISTING_POLE_PARTNER_LINKNET", 0),
            "TOTAL POLE EXT TELKOM": counts.get("EXISTING_POLE_PARTNER_TELKOM", 0),
            "TOTAL POLE (ALL TYPE)": counts.get("POLE_ALL", 0),
            "TOTAL AERIAL ROUTE": aerial_route,
            "TOTAL DUCT ROUTE": duct_route,
            "TOTAL ROUTE LENGTH": aerial_route + duct_route,
            "TOTAL HOMEPASS": counts.get("HP COVER", 0),
            "TOTAL FDT (48C)": 0 if is_ug else fdt_count,
            "TOTAL FDT (72C)": 0,
            "TOTAL FDT (144C)": fdt_count if is_ug else 0,
            "(FO CABLE AERIAL 288/24T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 288/24T)",
                math.ceil(lengths.get("FO_288/24T", 0.0)),
            ),
            "(FO CABLE AERIAL 144/12T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 144/12T)",
                math.ceil(lengths.get("FO_144/12T", 0.0)),
            ),
            "(FO CABLE AERIAL 96/8T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 96/8T)",
                math.ceil(lengths.get("FO_96/8T", 0.0)),
            ),
            "(FO CABLE AERIAL 72/6T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 72/6T)",
                math.ceil(lengths.get("FO_72/6T", 0.0)),
            ),
            "(FO CABLE AERIAL 48/4T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 48/4T)",
                math.ceil(lengths.get("FO_48/4T", 0.0)),
            ),
            "(FO CABLE AERIAL 36/3T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 36/3T)",
                math.ceil(lengths.get("FO_36/3T", 0.0)),
            ),
            "(FO CABLE AERIAL 24/2T)": self._design_summary_override_int(
                "(FO CABLE AERIAL 24/2T)",
                math.ceil(lengths.get("FO_24/2T", 0.0)),
            ),
            "TOTAL HANDHOLE 80X80X130": handhole_80_count,
            "TOTAL HANDHOLE 40X40X60": handhole_40_count,
            "TOTAL HANDHOLE PIT 20X20X20": handhole_20_count,
            "TOTAL HDPE (40/32 mm)": subduct_40,
            "TOTAL HDPE (32/28 mm)": subduct_32,
            "TOTAL FAT 16 PORT (AERIAL)": aerial_fat_count,
            "TOTAL FAT 16 PORT (PEDESTAL)": pedestal_fat_count,
            "TOTAL CLOSURE 288C": counts.get("JOINT_CLOSURE_288C", 0),
            "TOTAL CLOSURE 144C": counts.get("JOINT_CLOSURE_144C", 0),
            "TOTAL CLOSURE 48C": counts.get("JOINT_CLOSURE_48C", 0),
            "TOTAL CABLE LOOP HOLDER/HANGER": cable_loop_holder_count,
        }
        if is_feeder:
            if is_ug:
                trenching_override = self._design_summary_override_int_or_none("TOTAL TRENCHING")
                if trenching_override is None:
                    # Until a Feeder BOQ NRO trenching quantity is supplied,
                    # UG trenching follows the current duct-route assumption.
                    values["TOTAL TRENCHING"] = duct_route
                    LOGGER.warning(
                        "Feeder UG TOTAL TRENCHING has no BOQ NRO/design override source; "
                        "using TOTAL DUCT ROUTE fallback (%s m). Real trenching should come "
                        "from Feeder BOQ NRO trenching quantity cells.",
                        duct_route,
                    )
                else:
                    values["TOTAL TRENCHING"] = trenching_override

                boring_override = self._design_summary_override_int_or_none("TOTAL BORING")
                if boring_override is None:
                    values["TOTAL BORING"] = 0
                    LOGGER.warning(
                        "Feeder UG TOTAL BORING has no BOQ NRO/design override or BORING "
                        "folder source; writing 0. Real boring should come from Feeder BOQ "
                        "NRO boring quantity cells or a BORING line folder.",
                    )
                else:
                    values["TOTAL BORING"] = boring_override
            else:
                values["TOTAL TRENCHING"] = 0
                values["TOTAL BORING"] = 0
        return values

    def _design_summary_override_int_or_none(self, label: str) -> int | None:
        value = self.design_summary_overrides.get(label.strip().upper())
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return int(math.ceil(float(value)))
        try:
            normalized = str(value).strip().replace(",", ".")
            return int(math.ceil(float(normalized)))
        except (TypeError, ValueError):
            return None

    def _design_summary_override_int(self, label: str, fallback: int | float) -> int:
        value = self._design_summary_override_int_or_none(label)
        if value is None:
            return int(fallback or 0)
        return value

    def populate_template_design_summary(self) -> None:
        layout = self.ensure_template_layout()
        if layout is None:
            return

        cluster_name = normalize_cluster_name_for_dxf(self.cluster_name) if self.cluster_name else ""
        if cluster_name:
            self._set_design_summary_cluster_name(layout, cluster_name)
            self._set_title_block_cluster_name(layout, cluster_name)

        if self.network_type == "UG":
            self._rename_ug_design_summary_labels(layout)

        for label, value in self.dxf_design_summary_values().items():
            self._set_design_summary_value(layout, label, str(int(value or 0)))

    def _rename_ug_design_summary_labels(self, layout) -> None:
        replacements = {
            "TOTAL FDT (48C)": "TOTAL FDT (144C)",
        }
        for entity in self._layout_text_entities(layout, "DESIGN_SUMMARY"):
            text = str(entity.dxf.text).strip()
            replacement = replacements.get(text.upper())
            if replacement:
                entity.dxf.text = replacement

    def _layout_text_entities(self, layout, layer_name: str | None = None) -> list:
        entities = []
        for entity in layout:
            if entity.dxftype() != "TEXT":
                continue
            if layer_name:
                entity_layer = str(entity.dxf.get("layer", "")).upper()
                target = layer_name.upper()
                # Accept both underscore and space variants of layer name
                # (e.g., "DESIGN_SUMMARY" matches "DESIGN SUMMARY" too)
                if entity_layer != target and entity_layer.replace(" ", "_") != target.replace(" ", "_"):
                    continue
            entities.append(entity)
        return entities

    def _set_design_summary_cluster_name(self, layout, cluster_name: str) -> None:
        candidates = [
            entity
            for entity in self._layout_text_entities(layout, "DESIGN_SUMMARY")
            if (
                (set(str(entity.dxf.text).strip()) <= {"."} and len(str(entity.dxf.text).strip()) >= 10)
                or str(entity.dxf.text).strip().upper() == "MAP"
            )
        ]
        if not candidates:
            return
        entity = max(candidates, key=lambda item: float(item.dxf.insert.y))
        entity.dxf.text = cluster_name
        entity.dxf.height = min(float(entity.dxf.height), max(0.9, 70.0 / max(len(cluster_name), 1) / 0.62))
        try:
            entity.set_placement((60.0, float(entity.dxf.insert.y), 0.0), align=TextEntityAlignment.MIDDLE_CENTER)
        except Exception:
            entity.dxf.insert = (29.963, float(entity.dxf.insert.y), 0.0)

    def _set_title_block_cluster_name(self, layout, cluster_name: str) -> None:
        min_x, min_y, max_x, max_y = TITLE_BLOCK_CLUSTER_NAME_BOUNDS
        for entity in self._layout_text_entities(layout, "TITLE_BLOCK"):
            try:
                x = float(entity.dxf.insert.x)
                y = float(entity.dxf.insert.y)
            except Exception:
                continue
            text = str(entity.dxf.text).strip()
            if min_x <= x <= max_x and min_y <= y <= max_y and text not in {"Nama Gambar", "Drawing Name"}:
                try:
                    layout.delete_entity(entity)
                except Exception:
                    pass

        width = max_x - min_x
        height = min(1.6, max(0.55, (width - 4.0) / max(len(cluster_name), 1) / 0.86))
        entity = layout.add_text(
            cluster_name,
            dxfattribs={
                "layer": "TITLE_BLOCK",
                "height": height,
                "color": 7,
            },
        )
        entity.set_placement(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, 0.0), align=TextEntityAlignment.MIDDLE_CENTER)

    def _set_design_summary_value(self, layout, label: str, value: str) -> None:
        label_upper = label.upper()
        design_texts = self._layout_text_entities(layout, "DESIGN_SUMMARY")
        label_entities = [
            entity
            for entity in design_texts
            if str(entity.dxf.text).strip().upper() == label_upper
            or label_upper in str(entity.dxf.text).strip().upper()
        ]
        if not label_entities:
            return

        label_entity = min(label_entities, key=lambda item: len(str(item.dxf.text)))
        target_y = float(label_entity.dxf.insert.y)
        label_x = float(label_entity.dxf.insert.x)
        value_x_min, value_x_max, value_x = self._design_summary_value_column(label_x)
        row_texts = [
            entity
            for entity in design_texts
            if abs(float(entity.dxf.insert.y) - target_y) <= 0.75
        ]
        placeholders = [
            entity
            for entity in row_texts
            if str(entity.dxf.text).strip() == "--"
            and value_x_min <= float(entity.dxf.insert.x) <= value_x_max
        ]
        if placeholders:
            placeholder = min(placeholders, key=lambda item: float(item.dxf.insert.x))
            self._remove_stale_design_summary_values(layout, row_texts, value_x_min, value_x_max, keep_entity=placeholder)
            placeholder.dxf.text = value
            placeholder.dxf.insert = (value_x, target_y, 0.0)
            return

        existing_values = [
            entity
            for entity in row_texts
            if re.fullmatch(r"\d+", str(entity.dxf.text).strip())
            and value_x_min <= float(entity.dxf.insert.x) <= value_x_max
        ]
        if existing_values:
            value_entity = min(existing_values, key=lambda item: abs(float(item.dxf.insert.x) - value_x))
            self._remove_stale_design_summary_values(layout, row_texts, value_x_min, value_x_max, keep_entity=value_entity)
            value_entity.dxf.text = value
            value_entity.dxf.insert = (value_x, target_y, 0.0)
            return

        colon_entities = [
            entity
            for entity in row_texts
            if str(entity.dxf.text).strip() == ":"
            and value_x_min <= float(entity.dxf.insert.x) + 3.0 <= value_x_max
        ]
        if colon_entities:
            value_x = max(value_x, float(min(colon_entities, key=lambda item: float(item.dxf.insert.x)).dxf.insert.x) + 2.1)
        entity = layout.add_text(
            value,
            dxfattribs={
                "layer": str(label_entity.dxf.get("layer", "DESIGN_SUMMARY") or "DESIGN_SUMMARY"),
                "height": float(label_entity.dxf.height),
                "color": int(label_entity.dxf.get("color", 7) or 7),
            },
        )
        entity.dxf.insert = (value_x, target_y, 0.0)

    def _design_summary_value_column(self, label_x: float) -> tuple[float, float, float]:
        if label_x < 55.0:
            return (*DESIGN_SUMMARY_LEFT_VALUE_BOUNDS, DESIGN_SUMMARY_LEFT_VALUE_X)
        return (*DESIGN_SUMMARY_RIGHT_VALUE_BOUNDS, DESIGN_SUMMARY_RIGHT_VALUE_X)

    def _remove_stale_design_summary_values(
        self,
        layout,
        row_texts: Sequence[object],
        value_x_min: float,
        value_x_max: float,
        keep_entity=None,
    ) -> None:
        for entity in row_texts:
            if entity is keep_entity:
                continue
            text = str(entity.dxf.text).strip()
            if not re.fullmatch(r"\d+", text):
                continue
            x = float(entity.dxf.insert.x)
            if value_x_min <= x <= value_x_max:
                try:
                    layout.delete_entity(entity)
                except Exception:
                    pass

    def patch_autocad_lt_header_extents(self) -> None:
        if self.doc.dxfversion != "AC1009":
            return
        if not self.autocad_lt_extents:
            return

        min_x, min_y, max_x, max_y = self.autocad_lt_extents
        lines = self.output_path.read_text(encoding="latin1", errors="ignore").splitlines()

        def patch_header_var(name: str, values: dict[str, float]) -> None:
            try:
                index = next(i for i, line in enumerate(lines) if line.strip().upper() == name)
            except StopIteration:
                return

            patched_codes = set()
            cursor = index + 1
            while cursor < len(lines) - 1 and len(patched_codes) < len(values):
                code = lines[cursor].strip()
                if code == "9":
                    break
                if code in values:
                    lines[cursor + 1] = f"{values[code]:.12g}"
                    patched_codes.add(code)
                    cursor += 2
                else:
                    cursor += 1

        patch_header_var("$EXTMIN", {"10": min_x, "20": min_y, "30": 0.0})
        patch_header_var("$EXTMAX", {"10": max_x, "20": max_y, "30": 0.0})
        patch_header_var("$LIMMIN", {"10": min_x, "20": min_y})
        patch_header_var("$LIMMAX", {"10": max_x, "20": max_y})
        with self.output_path.open("w", encoding="latin1", newline="\r\n") as file:
            file.write("\n".join(lines) + "\n")

    def patch_ac1015_linetype_pattern_flags(self) -> None:
        if self.doc.dxfversion != "AC1015" or not self.output_path.exists():
            return

        lines = self.output_path.read_text(encoding="latin1", errors="ignore").splitlines()
        patched_lines: list[str] = []
        inside_ltype_table = False
        inside_ltype_record = False
        expect_ltype_handle = False
        changed = False
        cursor = 0

        while cursor < len(lines):
            if cursor + 1 >= len(lines):
                patched_lines.append(lines[cursor])
                break

            code = lines[cursor]
            value = lines[cursor + 1]
            stripped_code = code.strip()
            stripped_value = value.strip().upper()
            skip_pair = False

            if stripped_code == "0" and stripped_value == "TABLE":
                next_code = lines[cursor + 2].strip() if cursor + 2 < len(lines) else ""
                next_value = lines[cursor + 3].strip().upper() if cursor + 3 < len(lines) else ""
                inside_ltype_table = next_code == "2" and next_value == "LTYPE"
                inside_ltype_record = False
                expect_ltype_handle = False
            elif inside_ltype_table and stripped_code == "0" and stripped_value == "LTYPE":
                inside_ltype_record = True
                expect_ltype_handle = True
            elif inside_ltype_table and stripped_code == "0" and stripped_value == "ENDTAB":
                inside_ltype_table = False
                inside_ltype_record = False
                expect_ltype_handle = False
            elif inside_ltype_record and stripped_code == "5":
                if expect_ltype_handle:
                    expect_ltype_handle = False
                else:
                    skip_pair = True
                    changed = True
            elif inside_ltype_record and expect_ltype_handle and stripped_code != "100":
                expect_ltype_handle = False

            if not skip_pair:
                patched_lines.extend([code, value])

            if inside_ltype_record and not skip_pair and stripped_code == "49":
                next_code = lines[cursor + 2].strip() if cursor + 2 < len(lines) else ""
                if next_code != "74":
                    patched_lines.extend([" 74", "0"])
                    changed = True

            cursor += 2

        if changed:
            with self.output_path.open("w", encoding="latin1", newline="\r\n") as file:
                file.write("\n".join(patched_lines) + "\n")

    def fit_entities_to_right_layout(self) -> None:
        entities = list(self.output_entities)
        if not entities:
            return

        try:
            extent = bbox.extents(entities)
        except Exception as exc:
            LOGGER.warning("Gagal menghitung bounding box output: %s", exc)
            return

        min_x, min_y = float(extent.extmin.x), float(extent.extmin.y)
        max_x, max_y = float(extent.extmax.x), float(extent.extmax.y)
        source_width = max(max_x - min_x, 1.0)
        source_height = max(max_y - min_y, 1.0)
        target_min_x, target_min_y = self.content_min
        target_max_x, target_max_y = self.content_max
        fit_margin = 1e-6
        target_min_x += fit_margin
        target_min_y += fit_margin
        target_max_x -= fit_margin
        target_max_y -= fit_margin
        target_width = target_max_x - target_min_x
        target_height = target_max_y - target_min_y
        scale = min(target_width / source_width, target_height / source_height)
        offset_x = target_min_x + (target_width - source_width * scale) / 2
        offset_y = target_min_y + (target_height - source_height * scale) / 2

        matrix = Matrix44.chain(
            Matrix44.translate(-min_x, -min_y, 0),
            Matrix44.scale(scale, scale, 1),
            Matrix44.translate(offset_x, offset_y, 0),
        )
        for entity in entities:
            try:
                entity.transform(matrix)
            except Exception as exc:
                LOGGER.debug("Entity %s tidak bisa ditransform: %s", entity.dxftype(), exc)

        for entity, width in self.fixed_width_polylines:
            try:
                if entity.dxftype() == "POLYLINE":
                    for vertex in entity.vertices:
                        vertex.dxf.start_width = float(width)
                        vertex.dxf.end_width = float(width)
                elif entity.dxftype() == "LWPOLYLINE":
                    points = [
                        (point[0], point[1], float(width), float(width), point[4])
                        for point in entity.get_points("xyseb")
                    ]
                    entity.set_points(points, format="xyseb")
            except Exception as exc:
                LOGGER.debug("Lebar polyline %s tidak bisa dikunci: %s", entity.dxftype(), exc)

    def add_standard_layout_frame(self) -> None:
        attrs = {"layer": TEMPLATE_LAYER, "color": 7}

        def rectangle(min_x: float, min_y: float, max_x: float, max_y: float) -> None:
            self.modelspace.add_lwpolyline(
                [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)],
                close=True,
                dxfattribs=attrs,
            )

        # Dua frame mengikuti template DWG user: frame kiri referensi, frame kanan area hasil convert.
        rectangle(0, 0, 420, 297)
        rectangle(470, 0, 890, 297)

        # Area judul/legend kanan dan tabel bawah dibuat sebagai linework sederhana,
        # supaya geometri selalu masuk ke kotak kosong di frame kanan.
        rectangle(835, 0, 890, 297)
        rectangle(470, 0, 835, 65)
        rectangle(470, 0, 555, 65)
        rectangle(745, 0, 835, 45)
        self.modelspace.add_line((835, 235), (890, 235), dxfattribs=attrs)
        self.modelspace.add_line((835, 185), (890, 185), dxfattribs=attrs)
        self.modelspace.add_line((835, 125), (890, 125), dxfattribs=attrs)

    def preview_geojson(self) -> dict:
        return {"type": "FeatureCollection", "features": self.preview_features}

    def finalize_roads(self) -> None:
        if not self.road_buffers and not self.road_lines:
            return

        if self.road_buffers:
            road_union = unary_union(self.road_buffers)
            road_union = road_union.buffer(0)
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                road_union = road_union.intersection(self.road_clip_geometry)
            road_union = road_union.simplify(ROAD_SIMPLIFY_TOLERANCE_METERS, preserve_topology=True)
            road_union = road_union.buffer(0)
            self._add_road_boundaries_to_dxf(road_union)

        if self.road_lines:
            road_lines = unary_union(self.road_lines)
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                road_lines = road_lines.intersection(self.road_clip_geometry)
            self._add_road_lines_to_dxf(road_lines)

    def _add_road_boundaries_to_dxf(self, geometry) -> None:
        if geometry.is_empty:
            return

        if isinstance(geometry, Polygon):
            self._add_polygon_boundary(geometry)
            return

        if isinstance(geometry, MultiPolygon):
            for polygon in geometry.geoms:
                self._add_polygon_boundary(polygon)
            return

        if isinstance(geometry, GeometryCollection):
            for part in geometry.geoms:
                self._add_road_boundaries_to_dxf(part)

    def _add_road_lines_to_dxf(self, geometry) -> None:
        if geometry.is_empty:
            return

        if isinstance(geometry, LineString):
            points = list(geometry.coords)
            if len(points) >= 2:
                self._add_polyline(points, close=False, dxfattribs={"layer": "ROAD"})
            return

        if isinstance(geometry, GeometryCollection):
            for part in geometry.geoms:
                self._add_road_lines_to_dxf(part)
            return

        for part in getattr(geometry, "geoms", []):
            self._add_road_lines_to_dxf(part)

    def _add_polygon_boundary(self, polygon: Polygon) -> None:
        if polygon.area < ROAD_MIN_POLYGON_AREA_SQUARE_METERS:
            return

        exterior = list(polygon.exterior.coords)
        if len(exterior) >= 4:
            self._add_polyline(exterior, close=True, dxfattribs={"layer": "ROAD"})

        for interior in polygon.interiors:
            interior_points = list(interior.coords)
            if len(interior_points) >= 4:
                self._add_polyline(interior_points, close=True, dxfattribs={"layer": "ROAD"})

        self._add_road_inner_line(polygon)

    def _add_road_inner_line(self, polygon: Polygon) -> None:
        """Draw a second outline just inside the road polygon so the road
        renders as a double line (outer edge + inner casing), matching the
        standard CAD road style. The inner outline is the polygon eroded
        inward by ROAD_INNER_LINE_OFFSET_METERS."""
        if ROAD_INNER_LINE_OFFSET_METERS <= 0:
            return
        if polygon.area < ROAD_INNER_LINE_MIN_POLYGON_AREA_SQUARE_METERS:
            return

        try:
            inner = polygon.buffer(
                -ROAD_INNER_LINE_OFFSET_METERS,
                cap_style=2,
                join_style=2,
                mitre_limit=2.0,
            )
        except Exception:
            return
        if inner.is_empty:
            return

        inner_polygons: list[Polygon]
        if isinstance(inner, Polygon):
            inner_polygons = [inner]
        elif isinstance(inner, MultiPolygon):
            inner_polygons = list(inner.geoms)
        else:
            inner_polygons = [geom for geom in getattr(inner, "geoms", []) if isinstance(geom, Polygon)]

        for inner_polygon in inner_polygons:
            if inner_polygon.is_empty or inner_polygon.area < ROAD_MIN_POLYGON_AREA_SQUARE_METERS:
                continue
            inner_exterior = list(inner_polygon.exterior.coords)
            if len(inner_exterior) >= 4:
                self._add_polyline(inner_exterior, close=True, dxfattribs={"layer": "ROAD"})
            for interior in inner_polygon.interiors:
                interior_points = list(interior.coords)
                if len(interior_points) >= 4:
                    self._add_polyline(interior_points, close=True, dxfattribs={"layer": "ROAD"})


def normalize_input_paths(input_paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(input_paths, (str, Path)):
        paths = [Path(input_paths)]
    else:
        paths = [Path(path) for path in input_paths]

    if not paths:
        raise ValueError("Minimal satu file input diperlukan.")

    return paths


def is_kml_path(path: Path) -> bool:
    return path.suffix.lower() in {".kml", ".kmz"}


def kml_folder_key(file_name: str, folder_path: str) -> str:
    return f"{file_name}::{folder_path or 'Tanpa Folder'}"


def collect_kml_folder_summary(path: Path) -> dict[str, dict[str, int | str]]:
    root = read_kml_root(path)
    summary: dict[str, dict[str, int | str]] = {
        folder_name: {"name": folder_name, "count": 0, "exists": 0} for folder_name in STANDARD_KML_FOLDERS
    }
    _collect_kml_folder_summary(root, [], summary)
    return summary


def _collect_kml_folder_summary(
    element: ET.Element,
    folder_stack: list[str],
    summary: dict[str, dict[str, int | str]],
) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Document":
            _collect_kml_folder_summary(child, folder_stack, summary)
        elif child_name == "Folder":
            folder_name = direct_child_text_by_name(child, "name") or "Tanpa Nama"
            standard_folder = canonical_standard_folder(folder_name)
            if standard_folder in summary:
                summary[standard_folder]["exists"] = 1
            _collect_kml_folder_summary(child, folder_stack + [folder_name], summary)
        elif child_name == "Placemark" and folder_stack:
            standard_folder = nearest_standard_folder(folder_stack)
            if standard_folder in summary:
                summary[standard_folder]["count"] = int(summary[standard_folder]["count"]) + 1


def validate_standard_kml_folders(path: Path) -> None:
    if not is_kml_path(path):
        return

    read_kml_root(path)


def parse_kml_file(
    path: Path,
    handler: OSMToDXFHandler,
    selected_kml_folders: Iterable[str] | None = None,
) -> int:
    root = read_kml_root(path)
    styles, style_maps = parse_kml_styles(root)
    selected_folder_set = set()
    for folder in selected_kml_folders or []:
        normalized = folder.strip()
        if not normalized:
            continue
        canonical = canonical_standard_folder(normalized)
        selected_folder_set.add(normalized)
        if canonical:
            selected_folder_set.add(canonical)
        if "::" in normalized:
            folder_part = normalized.rsplit("::", 1)[-1].strip()
            selected_folder_set.add(folder_part)
            canonical_folder_part = canonical_standard_folder(folder_part)
            if canonical_folder_part:
                selected_folder_set.add(canonical_folder_part)
    return parse_kml_container(
        root,
        handler,
        file_name=path.name,
        folder_stack=[],
        selected_kml_folders=selected_folder_set,
        filter_enabled=selected_kml_folders is not None,
        styles=styles,
        style_maps=style_maps,
    )


def parse_kml_container(
    element: ET.Element,
    handler: OSMToDXFHandler,
    file_name: str,
    folder_stack: list[str],
    selected_kml_folders: set[str],
    filter_enabled: bool,
    styles: dict[str, dict],
    style_maps: dict[str, str],
) -> int:
    feature_count = 0

    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Document":
            feature_count += parse_kml_container(
                child,
                handler,
                file_name,
                folder_stack,
                selected_kml_folders,
                filter_enabled,
                styles,
                style_maps,
            )
        elif child_name == "Folder":
            folder_name = direct_child_text_by_name(child, "name") or "Tanpa Nama"
            feature_count += parse_kml_container(
                child,
                handler,
                file_name,
                folder_stack + [folder_name],
                selected_kml_folders,
                filter_enabled,
                styles,
                style_maps,
            )
        elif child_name == "Placemark":
            standard_folder = nearest_standard_folder(folder_stack)
            if not standard_folder:
                continue
            key = kml_folder_key(file_name, standard_folder)
            if filter_enabled and key not in selected_kml_folders and standard_folder not in selected_kml_folders:
                continue
            name = direct_child_text_by_name(child, "name")
            style = resolve_kml_style(direct_child_text_by_name(child, "styleUrl"), styles, style_maps)
            feature_count += parse_kml_placemark(child, handler, name, style, standard_folder, folder_stack)

    return feature_count


def parse_kml_placemark(
    placemark: ET.Element,
    handler: OSMToDXFHandler,
    name: str,
    style: dict | None = None,
    folder_name: str | None = None,
    folder_stack: Sequence[str] | None = None,
) -> int:
    feature_count = 0
    handler.record_dxf_design_summary(placemark, folder_name, folder_stack or [])
    output_start_index = len(handler.output_entities)

    for point in children_by_name(placemark, "Point"):
        coordinate_element = direct_child_by_name(point, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if coordinates:
            handler.add_kml_point(coordinates[0], name, style, folder_name, folder_stack)
            feature_count += 1

    for line in children_by_name(placemark, "LineString"):
        coordinate_element = direct_child_by_name(line, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if len(coordinates) >= 2:
            handler.add_kml_linestring(coordinates, name, style, folder_stack)
            feature_count += 1

    for outer_coordinates, inner_rings in extract_kml_placemark_polygons(placemark):
        if len(outer_coordinates) >= 4:
            handler.add_kml_polygon(
                outer_coordinates,
                inner_rings,
                name,
                style=style,
                folder_name=folder_name,
                folder_stack=folder_stack,
            )
            feature_count += 1

    new_entities = handler.output_entities[output_start_index:]
    fdt_number = detect_parent_fdt_number(folder_stack or [])
    if fdt_number:
        handler.register_fdt_entities(fdt_number, new_entities)
    elif is_fdt_folder(folder_name):
        handler.register_fdt_suffix_entities(extract_fdt_suffix_number(name), new_entities)

    return feature_count


def extract_kml_placemark_polygons(
    placemark: ET.Element,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    polygons = []
    for polygon in children_by_name(placemark, "Polygon"):
        outer_coordinates: list[tuple[float, float]] = []
        inner_rings: list[list[tuple[float, float]]] = []

        outer_boundary = direct_child_by_name(polygon, "outerBoundaryIs")
        if outer_boundary is not None:
            coordinates_element = next(
                (element for element in outer_boundary.iter() if local_name(element.tag) == "coordinates"),
                None,
            )
            outer_coordinates = parse_kml_coordinates(coordinates_element.text if coordinates_element is not None else None)

        for inner_boundary in [element for element in list(polygon) if local_name(element.tag) == "innerBoundaryIs"]:
            coordinates_element = next(
                (element for element in inner_boundary.iter() if local_name(element.tag) == "coordinates"),
                None,
            )
            inner_coordinates = parse_kml_coordinates(coordinates_element.text if coordinates_element is not None else None)
            if inner_coordinates:
                inner_rings.append(inner_coordinates)

        if outer_coordinates:
            polygons.append((outer_coordinates, inner_rings))

    return polygons


def _auto_boundary_from_geometries(geometries: Sequence, buffer_meters: float):
    if not geometries:
        return None

    geometry = unary_union(geometries)
    if geometry.is_empty:
        return None

    boundary = geometry.buffer(float(buffer_meters))
    return None if boundary.is_empty else boundary


def _collect_auto_boundary_geometries_from_kml(path: Path, transformer: Transformer) -> list:
    if not is_kml_path(path):
        return []

    root = read_kml_root(path)
    geometries = []

    for placemark in [element for element in root.iter() if local_name(element.tag) == "Placemark"]:
        for point in children_by_name(placemark, "Point"):
            coordinate_element = direct_child_by_name(point, "coordinates")
            coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
            if coordinates:
                geometries.append(ShapelyPoint(project_point(coordinates[0][0], coordinates[0][1], transformer)))

        for line in children_by_name(placemark, "LineString"):
            coordinate_element = direct_child_by_name(line, "coordinates")
            coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
            projected = _clean_points(project_coordinates(coordinates, transformer))
            if len(projected) >= 2:
                line_geometry = LineString(projected)
                if not line_geometry.is_empty and line_geometry.length > 0:
                    geometries.append(line_geometry)

        for outer_coordinates, inner_rings in extract_kml_placemark_polygons(placemark):
            if len(outer_coordinates) < 4:
                continue
            projected_outer = project_coordinates(outer_coordinates, transformer)
            projected_inners = [
                project_coordinates(inner_ring, transformer)
                for inner_ring in inner_rings
                if len(inner_ring) >= 4
            ]
            polygon = Polygon(projected_outer, projected_inners)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if not polygon.is_empty:
                geometries.append(polygon)

    return geometries


def _collect_auto_boundary_geometries_from_inputs(input_paths: Iterable[str | Path], target_crs: str) -> list:
    transformer = build_transformer(target_crs)
    geometries = []
    for path in input_paths:
        path = Path(path)
        if is_kml_path(path):
            geometries.extend(_collect_auto_boundary_geometries_from_kml(path, transformer))
    return geometries


def compute_auto_boundary_from_kml(path: str | Path, target_crs: str, buffer_meters: float):
    transformer = build_transformer(target_crs)
    geometries = _collect_auto_boundary_geometries_from_kml(Path(path), transformer)
    return _auto_boundary_from_geometries(geometries, buffer_meters)


def compute_auto_boundary_from_inputs(input_paths: str | Path | Iterable[str | Path], target_crs: str, buffer_meters: float):
    geometries = _collect_auto_boundary_geometries_from_inputs(normalize_input_paths(input_paths), target_crs)
    return _auto_boundary_from_geometries(geometries, buffer_meters)

def auto_boundary_buffer_meters_for_scope(scope_type: str | None) -> float:
    normalized = normalize_scope_type_name(scope_type)
    if normalized == "Subfeeder":
        return float(Config.SUBFEEDER_AUTO_BOUNDARY_BUFFER_METERS)
    if normalized == "Feeder":
        return float(Config.FEEDER_AUTO_BOUNDARY_BUFFER_METERS)
    return float(Config.AUTO_BOUNDARY_BUFFER_METERS)


def collect_road_clip_boundary(path: Path, target_crs: str = "EPSG:3857", repair_counter: dict[str, int] | None = None):
    if not is_kml_path(path):
        return None

    root = read_kml_root(path)
    transformer = build_transformer(target_crs)
    polygons = collect_road_clip_boundary_polygons(root, [], transformer, repair_counter=repair_counter)
    if not polygons:
        return None

    boundary = unary_union(polygons).buffer(0)
    return None if boundary.is_empty else boundary


def collect_road_clip_boundary_polygons(
    element: ET.Element,
    folder_stack: list[str],
    transformer: Transformer,
    repair_counter: dict[str, int] | None = None,
) -> list[Polygon]:
    polygons: list[Polygon] = []

    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Document":
            polygons.extend(collect_road_clip_boundary_polygons(child, folder_stack, transformer, repair_counter=repair_counter))
        elif child_name == "Folder":
            folder_name = direct_child_text_by_name(child, "name") or "Tanpa Nama"
            polygons.extend(collect_road_clip_boundary_polygons(child, folder_stack + [folder_name], transformer, repair_counter=repair_counter))
        elif child_name == "Placemark" and is_road_clip_boundary_folder(folder_stack):
            for outer_coordinates, inner_rings in extract_kml_placemark_polygons(child):
                if len(outer_coordinates) < 4:
                    continue
                projected_outer = project_coordinates(outer_coordinates, transformer)
                projected_inners = [
                    project_coordinates(inner_ring, transformer)
                    for inner_ring in inner_rings
                    if len(inner_ring) >= 4
                ]
                polygon = Polygon(projected_outer, projected_inners)
                if not polygon.is_valid:
                    if repair_counter is not None:
                        repair_counter["count"] = int(repair_counter.get("count", 0)) + 1
                    polygon = polygon.buffer(0)
                if not polygon.is_empty:
                    polygons.append(polygon)

    return polygons


def convert_osm_to_dxf(
    input_path: str | Path | Iterable[str | Path],
    output_path: str | Path,
    selected_layers: Iterable[str] | None = None,
    road_offset_meters: float | None = None,
    selected_kml_folders: Iterable[str] | None = None,
    road_clip_boundary: bool = True,
    target_crs: str = "EPSG:3857",
    progress_callback: Callable[[int], None] | None = None,
    preview_feature_limit: int = 2500,
    pipeline_logger=None,
    validate_inputs: bool = True,
    dxf_template_path: str | Path | None = None,
    network_type: str | None = None,
    design_summary_overrides: dict[str, int | float | str] | None = None,
    scope_type: str | None = None,
    original_filenames: Iterable[str] | None = None,
) -> dict:
    input_paths = normalize_input_paths(input_path)
    output_path = Path(output_path)

    missing_paths = [path for path in input_paths if not path.exists()]
    if missing_paths:
        pipeline_log(pipeline_logger, "error", f"Input file missing: {missing_paths[0]}")
        raise FileNotFoundError(f"Input file tidak ditemukan: {missing_paths[0]}")
    if validate_inputs:
        for path in input_paths:
            validate_standard_kml_folders(path)

    LOGGER.info("Mulai konversi %s file ke %s", len(input_paths), output_path)
    pipeline_log(pipeline_logger, "info", f"Starting conversion pipeline for {len(input_paths)} file(s).")
    pipeline_log(pipeline_logger, "info", "Initializing DXF template and layer table...")
    handler = OSMToDXFHandler(
        output_path=output_path,
        selected_layers=selected_layers,
        road_offset_meters=road_offset_meters,
        target_crs=target_crs,
        progress_callback=progress_callback,
        preview_feature_limit=preview_feature_limit,
        dxf_template_path=dxf_template_path,
        network_type=network_type,
        design_summary_overrides=design_summary_overrides,
        scope_type=scope_type,
    )
    cluster_source_path = next((path for path in input_paths if is_kml_path(path)), input_paths[0])
    # Prefer original user-uploaded filename so the title/drawing name doesn't show
    # the random standardized stem (e.g. "9FCC...CE02_STANDARDIZED_1").
    cluster_name = ""
    original_names_list = [str(name) for name in (original_filenames or []) if name]
    original_kml_names = [name for name in original_names_list if is_kml_path(Path(name))]
    original_name_candidates = original_kml_names + [name for name in original_names_list if name not in original_kml_names]
    for original in original_name_candidates:
        candidate = extract_cluster_name_from_input_path(original)
        if candidate and "STANDARDIZED" not in candidate.upper():
            cluster_name = candidate
            break
    if not cluster_name:
        cluster_name = extract_cluster_name_from_input_path(cluster_source_path)
    handler.set_cluster_name(cluster_name)
    if cluster_name:
        pipeline_log(pipeline_logger, "info", f"DXF title block cluster name: {normalize_cluster_name_for_dxf(cluster_name)}")
    pipeline_log(pipeline_logger, "success", f"DXF template ready with {len(list(handler.doc.layers)):,} layer(s).")
    if road_clip_boundary:
        pipeline_log(pipeline_logger, "info", "Scanning BOUNDARY CLUSTER polygons for ROAD clipping...")
        repair_counter = {"count": 0}
        road_clip_boundaries = [
            boundary
            for path in input_paths
            if (boundary := collect_road_clip_boundary(path, target_crs=target_crs, repair_counter=repair_counter)) is not None
        ]
        handler.repaired_polygons += int(repair_counter.get("count", 0))
        if road_clip_boundaries:
            handler.set_road_clip_geometry(unary_union(road_clip_boundaries))
            pipeline_log(pipeline_logger, "success", f"Loaded {len(road_clip_boundaries):,} BOUNDARY CLUSTER clipping geometry source(s).")
        elif is_feeder_like_scope(handler.scope_type):
            buffer_meters = auto_boundary_buffer_meters_for_scope(handler.scope_type)
            auto_boundary_geometries = _collect_auto_boundary_geometries_from_inputs(input_paths, target_crs)
            auto_boundary = _auto_boundary_from_geometries(auto_boundary_geometries, buffer_meters)
            if auto_boundary is not None:
                handler.set_road_clip_geometry(auto_boundary)
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"Feeder scope: no BOUNDARY CLUSTER found - using {buffer_meters:g}m auto-buffer around all KML geometries.",
                )
                pipeline_log(
                    pipeline_logger,
                    "info",
                    f"Feeder scope auto-buffer: {buffer_meters:g}m around {len(auto_boundary_geometries):,} KML geometries.",
                )
            else:
                pipeline_log(pipeline_logger, "warning", "Feeder scope: no KML geometries found, skipping auto-buffer.")
        else:
            pipeline_log(pipeline_logger, "warning", "ROAD clipping enabled, but no BOUNDARY CLUSTER polygon was found.")
    else:
        pipeline_log(pipeline_logger, "info", "ROAD clipping skipped by user setting.")
    kml_feature_count = 0

    for path in input_paths:
        if is_kml_path(path):
            LOGGER.info("Membaca input KML/KMZ: %s", path)
            pipeline_log(pipeline_logger, "info", f"Parsing KML/KMZ and generating KML icons/CAD blocks: {path.name}")
            kml_detail_before = handler.kml_detail_snapshot()
            before_features = kml_feature_count
            kml_feature_count += parse_kml_file(path, handler, selected_kml_folders=selected_kml_folders)
            pipeline_log(pipeline_logger, "success", f"Parsed {kml_feature_count - before_features:,} KML feature(s) from {path.name}.")
            pipeline_log(
                pipeline_logger,
                "info",
                f"KML detail from {path.name}: "
                f"{handler.format_kml_detail_delta(handler.kml_detail_delta(kml_detail_before))}.",
            )
        else:
            LOGGER.info("Membaca input OSM/PBF: %s", path)
            before_nodes = handler.processed_nodes
            before_ways = handler.processed_ways
            before_roads = handler.stats.get("ROAD", 0)
            if is_geofabrik_pbf(path) and "ROAD" in handler.selected_layers:
                pipeline_log(pipeline_logger, "info", f"Reading Geofabrik ROAD cache: {path.name}")
                cache_path = ensure_road_cache(path, pipeline_logger=pipeline_logger)
                lonlat_bbox = (
                    projected_bounds_to_lonlat(handler.road_clip_geometry.bounds, target_crs)
                    if handler.road_clip_geometry is not None and not handler.road_clip_geometry.is_empty
                    else None
                )
                loaded_roads = handler.load_roads_from_cache(cache_path, lonlat_bbox)
                bbox_label = "project boundary" if lonlat_bbox else "full region"
                pipeline_log(
                    pipeline_logger,
                    "success",
                    f"Loaded {loaded_roads:,} cached road geometries from {path.name} ({bbox_label}).",
                )
            else:
                pipeline_log(pipeline_logger, "info", f"Reading OSM/PBF ways and nodes: {path.name}")
                handler.apply_file(str(path), locations=True)
                pipeline_log(
                    pipeline_logger,
                    "success",
                    f"Parsed {handler.processed_ways - before_ways:,} ways, {handler.processed_nodes - before_nodes:,} nodes, "
                    f"{handler.stats.get('ROAD', 0) - before_roads:,} road geometries from {path.name}.",
                )

    pipeline_progress(pipeline_logger, 45, "Membuat offset polygon ROAD.")
    road_stage_start = time.perf_counter()
    road_part_count = len(handler.road_buffers) + len(handler.road_lines)
    pipeline_log(
        pipeline_logger,
        "info",
        f"[1/6] Generating offset road polygons from {road_part_count:,} road geometry part(s)...",
    )
    handler.finalize_roads()
    pipeline_log(
        pipeline_logger,
        "success",
        f"[1/6] Road layer generation complete: {handler.stats.get('ROAD', 0):,} road(s) parsed in "
        f"{format_stage_seconds(time.perf_counter() - road_stage_start)}.",
    )

    pipeline_progress(pipeline_logger, 58, "Membuat label nama jalan.")
    road_label_start = time.perf_counter()
    pipeline_log(
        pipeline_logger,
        "info",
        f"[2/6] Generating road labels from {len(handler.road_name_candidates):,} named road candidate(s)...",
    )
    handler.finalize_road_names(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[2/6] Road label generation complete: {len(handler.road_name_label_centers):,} label(s) placed in "
        f"{format_stage_seconds(time.perf_counter() - road_label_start)}.",
    )

    pipeline_progress(pipeline_logger, 64, "Menyusun label POLE_ID.")
    pole_id_stage_start = time.perf_counter()
    pipeline_log(
        pipeline_logger,
        "info",
        f"[3/6] Generating POLE_ID labels from {len(handler.pending_pole_ids):,} pole point(s)...",
    )
    handler.finalize_pole_ids(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[3/6] POLE_ID label generation complete: {handler.stats.get('POLE_ID', 0):,} label(s) placed in "
        f"{format_stage_seconds(time.perf_counter() - pole_id_stage_start)}.",
    )

    # Snap slack hangers, joint closures, and cable endpoints onto pole centres
    # now that all pole positions are known.
    handler.finalize_pole_snapped_icons()

    pipeline_progress(pipeline_logger, 68, "Menyusun HOME_NUMBER / HP COVER.")
    home_stage_start = time.perf_counter()
    pipeline_log(
        pipeline_logger,
        "info",
        f"[3/6] Generating HOME_NUMBER boxes from {len(handler.pending_home_numbers):,} HP COVER point(s)...",
    )
    handler.finalize_home_numbers(pipeline_logger=pipeline_logger)
    handler._finalize_uncover_home_numbers_as_parcels(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[3/6] HOME_NUMBER generation complete: {len(handler.home_number_boxes):,} box geometry part(s) placed in "
        f"{format_stage_seconds(time.perf_counter() - home_stage_start)}.",
    )

    point_icon_stage_start = time.perf_counter()
    pipeline_log(
        pipeline_logger,
        "info",
        f"[3/6] Orienting {len(handler.pending_fat_icons):,} FAT and "
        f"{len(handler.pending_handhole_icons):,} HH block(s) to nearest cable line...",
    )
    handler.finalize_olt_icons(pipeline_logger=pipeline_logger)
    handler.finalize_fat_icons(pipeline_logger=pipeline_logger)
    handler.finalize_handhole_icons(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[3/6] FAT/HH orientation complete in {format_stage_seconds(time.perf_counter() - point_icon_stage_start)}.",
    )

    twey_stage_start = time.perf_counter()
    pipeline_log(
        pipeline_logger,
        "info",
        f"[3/6] Snapping {len(handler.pending_twey_points):,} T-WAY block(s) to nearest cable line...",
    )
    handler.finalize_twey_icons(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[3/6] T-WAY placement complete in {format_stage_seconds(time.perf_counter() - twey_stage_start)}.",
    )

    pipeline_progress(pipeline_logger, 78, "Merapikan geometry dan layer DXF.")
    repair_stage_start = time.perf_counter()
    pipeline_log(pipeline_logger, "info", "[4/6] Checking repaired polygons and empty layer report...")
    if handler.repaired_polygons:
        pipeline_log(pipeline_logger, "warning", f"{handler.repaired_polygons:,} invalid polygon(s) repaired.")
    else:
        pipeline_log(pipeline_logger, "info", "Geometry repair check complete: no invalid polygons found.")

    selected_layer_names = list(selected_layers or DXF_LAYERS.keys())
    empty_layers = [layer for layer in selected_layer_names if handler.stats.get(layer, 0) == 0]
    if empty_layers:
        pipeline_log(pipeline_logger, "warning", "Empty selected layer(s): " + ", ".join(empty_layers) + ".")

    non_empty_layers = [(layer, count) for layer, count in handler.stats.items() if int(count or 0) > 0]
    if non_empty_layers:
        preview_layers = ", ".join(f"{layer} ({count:,})" for layer, count in non_empty_layers[:8])
        suffix = "" if len(non_empty_layers) <= 8 else f", +{len(non_empty_layers) - 8} more"
        pipeline_log(pipeline_logger, "success", f"Generated DXF layers: {preview_layers}{suffix}.")
    pipeline_log(
        pipeline_logger,
        "success",
        f"[4/6] Geometry/layer report complete in {format_stage_seconds(time.perf_counter() - repair_stage_start)}.",
    )

    pipeline_progress(pipeline_logger, 88, "Export DXF ke file.")
    export_stage_start = time.perf_counter()
    pipeline_log(pipeline_logger, "info", "[5/6] Exporting DXF file...")
    handler.save()
    output_size = output_path.stat().st_size if output_path.exists() else 0
    pipeline_log(
        pipeline_logger,
        "success",
        f"[5/6] DXF export written: {output_size / 1024 / 1024:.2f} MB in "
        f"{format_stage_seconds(time.perf_counter() - export_stage_start)}.",
    )
    pipeline_progress(pipeline_logger, 94, "Menyiapkan preview dan statistik akhir.")
    pipeline_log(
        pipeline_logger,
        "info",
        f"[6/6] Preparing preview/statistics payload from {len(handler.output_entities):,} CAD output entity/entities...",
    )
    LOGGER.info("Konversi selesai: %s ways diproses", handler.processed_ways)

    kml_layers = sum(
        1
        for layer, count in handler.stats.items()
        if int(count or 0) > 0
        and (
            layer == "KML"
            or layer in {
                KML_FAT_LAYER,
                KML_FDT_LAYER,
                KML_FAT_AREA_LAYER,
                KML_HOME_NUMBER_LAYER,
                KML_HOME_NUMBER_BOX_LAYER,
                KML_SLING_WIRE_LAYER,
            }
            or layer not in DXF_LAYERS
        )
    )
    pipeline_log(
        pipeline_logger,
        "success",
        f"[6/6] Preview/statistics ready: {kml_layers:,} KML-derived layer(s), "
        f"{len(list(handler.doc.layers)):,} DXF layer(s).",
    )

    return {
        "stats": handler.stats,
        "preview": handler.preview_geojson(),
        "processed_ways": handler.processed_ways,
        "processed_nodes": handler.processed_nodes,
        "kml_features": kml_feature_count,
        "kml_details": handler.kml_detail_snapshot(),
        "roads": handler.stats.get("ROAD", 0),
        "polygon_repairs": handler.repaired_polygons,
        "empty_layers": empty_layers,
        "kml_layers": kml_layers,
        "dxf_layers": len(list(handler.doc.layers)),
        "output_size": output_size,
        "input_count": len(input_paths),
    }

