import json
import heapq
import io
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
from PIL import Image, ImageDraw
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Matrix44
from pyproj import Geod, Transformer
from shapely.affinity import translate as _translate
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, MultiPoint, Point as ShapelyPoint, Polygon
from shapely.ops import nearest_points, substring, unary_union

try:
    from shapely.ops import voronoi_diagram as _shapely_voronoi_diagram
except Exception:  # pragma: no cover - depends on Shapely build/GEOS version.
    _shapely_voronoi_diagram = None

from config import Config
from eva_ai_safe import suggest_dxf_parcel_orientation, suggest_dxf_visual_parcel_layout, suggest_dxf_asset_layout, suggest_dxf_fat_boundary_trim

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



class FatAreaGeometryError(ValueError):
    """Deterministic FAT ownership/topology failure; reprocessing cannot repair it."""


def _fat_polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if isinstance(geometry,Polygon):
        return [geometry]
    return [p for g in geometry.geoms for p in _fat_polygon_parts(g)]


def _fat_connector_path(a,b,blocked,pad):
    # Offset obstacle vertices create a visibility graph, not a pixel/raster approximation.
    region=Polygon.from_bounds(*a.union(b).bounds).buffer(pad,join_style=2)
    obstacles=blocked.intersection(region)
    start=a.representative_point();end=b.representative_point()
    near_a,near_b=nearest_points(a,b)
    vertices=[(start.x,start.y),(end.x,end.y),(near_a.x,near_a.y),(near_b.x,near_b.y)]
    for p in _fat_polygon_parts(obstacles):
        vertices.extend((float(x),float(y)) for x,y in p.exterior.coords[:-1])
        for ring in p.interiors:
            vertices.extend((float(x),float(y)) for x,y in ring.coords[:-1])
    vertices=list(dict.fromkeys(vertices))
    # A node can be on an obstacle edge; its segment must not enter obstacle interior.
    interior=obstacles.buffer(-1e-7)
    graph=[[] for _ in vertices]
    for i,v in enumerate(vertices):
        for j in range(i):
            line=LineString([v,vertices[j]])
            if not interior.is_empty and line.intersects(interior):
                continue
            length=line.length
            graph[i].append((j,length));graph[j].append((i,length))
    starts=[i for i,v in enumerate(vertices) if a.buffer(1e-7).covers(ShapelyPoint(v))]
    targets={i for i,v in enumerate(vertices) if b.buffer(1e-7).covers(ShapelyPoint(v))}
    queue=[(0,i) for i in starts];heapq.heapify(queue);cost={i:0 for i in starts};previous={}
    while queue:
        distance,i=heapq.heappop(queue)
        if distance!=cost.get(i):
            continue
        if i in targets:
            path=[i]
            while i in previous:
                i=previous[i];path.append(i)
            return LineString([vertices[j] for j in reversed(path)]) if len(path)>1 else None
        for j,length in graph[i]:
            value=distance+length
            if value<cost.get(j,float('inf')):
                cost[j]=value;previous[j]=i;heapq.heappush(queue,(value,j))
    return None


def _connect_fat_components(geometry,excluded,own_union):
    parts=_fat_polygon_parts(geometry)
    # Discard numerical-only debris, never a component owning any positive-area HP cell.
    kept=[p for p in parts if p.area>1e-7 or p.intersection(own_union).area>1e-9]
    if len(kept)!=len(parts):
        geometry=unary_union(kept).buffer(0);parts=_fat_polygon_parts(geometry)
    if len(parts)<=1:
        return geometry
    # 1m nominal corridor; shared-edge/narrow passages may use a narrower legal connector.
    for _ in range(len(parts)-1):
        parts=_fat_polygon_parts(geometry)
        if len(parts)==1:
            return geometry
        pairs=sorted((a.distance(b),i,j) for i,a in enumerate(parts) for j,b in enumerate(parts[:i]))
        joined=None
        for _,i,j in pairs:
            a,b=parts[i],parts[j]
            for width in (.5,.2,.05):
                clearance=width+.02
                blocked=excluded.buffer(clearance,join_style=2).difference(own_union)
                carve=excluded.buffer(.02,join_style=2).difference(own_union)
                p,q=nearest_points(a,b)
                direct=LineString([p,q])
                routes=[direct]
                if not blocked.is_empty and direct.intersects(blocked.buffer(-1e-7)):
                    routes=[]
                    for pad in (20.0,60.0):
                        route=_fat_connector_path(a,b,blocked,pad)
                        if route is not None:
                            routes.append(route);break
                for route in routes:
                    corridor=route.buffer(width,cap_style=3,join_style=2).difference(carve)
                    candidate=geometry.union(corridor).buffer(0)
                    if candidate.is_valid and len(_fat_polygon_parts(candidate))<len(parts) and candidate.intersection(excluded).area<=1e-7:
                        joined=candidate;break
                if joined is not None:
                    break
            if joined is not None:
                break
        if joined is None:
            return geometry
        geometry=joined
    return geometry


def _trim_fat_boundary(polygon, owned, excluded, tolerance):
    if not isinstance(polygon, Polygon) or polygon.is_empty:
        return None
    trimmed = polygon.simplify(tolerance, preserve_topology=True)
    vertices = lambda p: len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
    if (not isinstance(trimmed, Polygon) or not trimmed.is_valid or trimmed.is_empty
            or vertices(trimmed) >= vertices(polygon)
            or trimmed.difference(polygon).area > 1e-7
            or owned.difference(trimmed).area > 1e-7
            or (not excluded.is_empty and trimmed.intersection(excluded).area > 1e-7)):
        return None
    return trimmed


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
    "T-WAY",
]
OPTIONAL_KML_FOLDERS = [
    "EXISTING HH 20X20X20",
    "EXISTING HH 40X40X60",
    "EXISTING HH 80X80X130",
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
            "T-WAY": "T-WAY",
            "T-WEY": "T-WAY",
            "TWEY": "T-WAY",
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

# Authoritative HP COVER parcel ladder. Every active strict planner tries one
# complete tier before moving to the next smaller module.
HP_COVER_PARCEL_SIZE_TIERS = (
    (6.0, 12.0),
    (5.0, 10.0),
    (4.0, 10.0),
    (4.0, 8.0),
)

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
# User-approved DXF-only ROAD relocation caps, measured from immutable KMZ source.
KML_ROAD_POLE_MAX_SHIFT_METERS = 8.0
KML_ROADSIDE_POLE_MAX_SHIFT_METERS = 5.0
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
    "residential": 4.0,
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
MAX_ROAD_WIDTH_METERS = 8.0
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
ROAD_INNER_LINE_OFFSET_METERS = 0.6
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
            return _clamp(explicit_width / 2, 0.8, MAX_ROAD_WIDTH_METERS / 2)

    lanes = parse_osm_measure_meters(tag_value(tags, "lanes"))
    if lanes:
        full_width = lanes * LANE_WIDTH_METERS + 1.0
        return _clamp(full_width / 2, 1.2, MAX_ROAD_WIDTH_METERS / 2)

    highway = tag_value(tags, "highway") or "residential"
    service = tag_value(tags, "service")
    if highway == "service" and service in {"driveway", "parking_aisle", "alley"}:
        return 1.6

    full_width = DEFAULT_ROAD_WIDTHS_METERS.get(highway, DEFAULT_ROAD_WIDTHS_METERS["residential"])
    return _clamp(full_width / 2, 0.8, MAX_ROAD_WIDTH_METERS / 2)


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

def kml_fat_owner_key(folder_stack: Sequence[str], name: str = "", source_scope: tuple = ()) -> tuple | None:
    """Keep source project/LINE/FDT plus FAT bucket; HP labels are not FAT IDs."""
    for index in range(len(folder_stack) - 1, -1, -1):
        category = canonical_standard_folder(folder_stack[index])
        if category not in {"HP COVER", "BOUNDARY FAT"}:
            continue
        values = [name] if category == "BOUNDARY FAT" else list(reversed(folder_stack[index + 1:]))
        code = next((find_fat_code(value) for value in values if find_fat_code(value)), None)
        return (source_scope, tuple(normalized_folder_name(value) for value in folder_stack[:index]), code)
    return None


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
    existing = canonical.startswith("EXISTING HH ")
    if existing:
        canonical = "NEW HH " + canonical.removeprefix("EXISTING HH ")
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
    spec = specs.get(canonical)
    if spec and existing:
        return {**spec, "layer": str(spec["layer"]).replace("NEW_HH_", "EXISTING_HH_"),
                "block": "EXISTING_" + str(spec["block"])}
    return spec


def is_twey_folder(folder_name: str | None) -> bool:
    canonical = canonical_standard_folder(folder_name) or normalized_folder_name(folder_name or "")
    return canonical == "T-WAY"


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



OSM_ROAD_CACHE_POLICY_SIGNATURE = json.dumps(
    {
        "schema": 3,
        "max_road_width_m": MAX_ROAD_WIDTH_METERS,
        "default_widths": DEFAULT_ROAD_WIDTHS_METERS,
        "lane_width": LANE_WIDTH_METERS,
        "excluded_highways": sorted(ROAD_EXCLUDED_HIGHWAYS),
        "excluded_services": sorted(ROAD_EXCLUDED_SERVICES),
        "minimum_lengths": ROAD_MIN_LENGTH_METERS,
    },
    sort_keys=True,
    separators=(",", ":"),
)

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
                ("policy_signature", OSM_ROAD_CACHE_POLICY_SIGNATURE),
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
        return (
            rows.get("source_size") == str(stat.st_size)
            and rows.get("source_mtime_ns") == str(stat.st_mtime_ns)
            and rows.get("policy_signature") == OSM_ROAD_CACHE_POLICY_SIGNATURE
        )
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
        self._road_buffer_centerlines = []
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
        self._last_label_only_indexes: set[int] = set()
        self._last_relaxed_4x8_indexes: set[int] = set()
        self.pending_fat_icons: list[dict] = []
        self.pending_handhole_icons: list[dict] = []
        self.pending_twey_points: list[dict] = []
        self.pending_olt_icons: list[dict] = []
        self.pending_pole_ids: list[dict] = []
        self.fat_callout_refs: list[dict] = []
        self.pending_boundary_fat_polygons: list[dict] = []
        self.home_number_boxes: list[Polygon] = []
        # Parallel flags: True when the matching box belongs to an HP UNCOVER
        # source point. The FAT AREA wrap must never absorb UNCOVER boxes.
        self.home_number_box_uncover: list[bool] = []
        self.home_number_cells: list[dict] = []
        self.home_number_relocated_indexes: set[int] = set()
        # Pole positions used for snapping nearby slack/joint closure/cable
        # placemarks to the centre of an existing pole icon.
        self.pole_positions: list[Point] = []
        self._roadside_poles: list[dict] = []
        self._pole_follow_lines: list[dict] = []
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
        self.preserve_native_template = False
        if self.dxf_template_path.exists():
            doc = ezdxf.readfile(self.dxf_template_path)
            self.preserve_native_template = (
                is_feeder_like_scope(self.scope_type) and doc.dxfversion == "AC1032"
            )
            return doc, True
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
        self._force_viewport_layer_noplot()

    def _ensure_viewports_layer_style(self) -> None:
        """Viewport frames must never plot.

        Previously this layer was forced to ACI 255 (white) with plot enabled,
        which renders invisible on white paper and overlaps the sheet. Standard
        AutoCAD behavior is a non-plotting viewport frame, so we set plot=0 and
        keep a dark color for on-screen visibility.
        """
        if TEMPLATE_VIEWPORT_LAYER not in self.doc.layers:
            self.doc.layers.add(TEMPLATE_VIEWPORT_LAYER, color=WHITE_ACI_COLOR)
        layer = self.doc.layers.get(TEMPLATE_VIEWPORT_LAYER)
        try:
            layer.dxf.color = WHITE_ACI_COLOR
            layer.dxf.true_color = WHITE_TRUE_COLOR
            layer.rgb = (255, 255, 255)
            # Non-plotting: viewport frames must not appear on paper.
            layer.dxf.plot = 0
        except Exception as exc:
            LOGGER.debug("Layer %s tidak bisa diset putih: %s", TEMPLATE_VIEWPORT_LAYER, exc)

    def _force_viewport_layer_noplot(self) -> None:
        """Re-assert plot=0 on the viewport layer after template load.

        The DXF template ships the VIEWPORTS layer with plot=1; the template is
        loaded after _setup_layers() on some paths, so enforce it again here.
        """
        try:
            if TEMPLATE_VIEWPORT_LAYER in self.doc.layers:
                self.doc.layers.get(TEMPLATE_VIEWPORT_LAYER).dxf.plot = 0
        except Exception as exc:
            LOGGER.debug("Gagal set plot=0 pada %s: %s", TEMPLATE_VIEWPORT_LAYER, exc)

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
        # Operator full-width cap also covers cached/manual half-widths.
        half_width_meters = min(half_width_meters, MAX_ROAD_WIDTH_METERS / 2)
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
            self._road_buffer_centerlines.append(line)

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
            existing = self.doc.blocks[KML_FAT_BLOCK_NAME]
            points = [(float(p.x), float(p.y)) for entity in existing if entity.dxftype() == "LINE"
                      for p in (entity.dxf.start, entity.dxf.end)]
            centred = bool(points) and all(math.isclose(actual, expected, abs_tol=1e-6) for actual, expected in zip(
                (min(p[0] for p in points), max(p[0] for p in points), min(p[1] for p in points), max(p[1] for p in points)),
                (-KML_FAT_ICON_WIDTH_METERS/2, KML_FAT_ICON_WIDTH_METERS/2,
                 -KML_FAT_ICON_HEIGHT_METERS/2, KML_FAT_ICON_HEIGHT_METERS/2)))
            if (centred and tuple(existing.block.dxf.base_point) == (0, 0, 0) and
                    all(e.dxftype() in ("LINE", "ATTDEF") for e in existing)):
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

    def _add_kml_fat_icon(self, projected: Point, code: str, style: dict | None = None, rotation: float = 0.0, normal_side: int = 1, offset_scale: float = 1.0, insert_override: Point | None = None) -> object:
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
        insert_point = insert_override if insert_override is not None else (
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
        if block_name.endswith("HANDHOLE_PIT_20_BLOCK"):
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
        folder_stack: Sequence[str] | None = None,
    ) -> str:
        clean_code = code.strip() or "FAT"
        self.pending_fat_icons.append(
            {
                "projected": projected,
                "code": clean_code,
                "name": name,
                "style": dict(style or {}),
                "fdt_number": fdt_number,
                "group": self._pole_line_group(folder_stack),
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

    def _source_owned_asset_pole(self, pending: dict) -> dict | None:
        """Prefer the source route; sharing requires that route's source cable contact."""
        group, source = pending.get("group"), pending["projected"]
        same = min((p for p in self._roadside_poles if p["group"] == group),
                   key=lambda p: _distance(source, p["source"]), default=None)
        if same is not None and _distance(source, same["source"]) <= KML_POLE_SNAP_RADIUS_METERS:
            return same
        # Canonical storage may change LINE, never project/FDT or source coordinates.
        pattern = r"LINE\s+[A-Z](?:\s*-\s*FDT\s+(\d+))?"
        owner = re.fullmatch(pattern, group[-1]) if group and len(group) >= 2 else None
        if owner is None:
            return None
        records = [r for r in getattr(self, "_pole_follow_lines", ()) if r["group"] == group
                   and r.get("cable_kind") in ("distribution", "sling") and len(r["source"]) >= 2]
        candidates = []
        for pole in self._roadside_poles:
            other = pole["group"]
            route = re.fullmatch(pattern, other[-1]) if other and other[:-1] == group[:-1] else None
            if (route is None or int(route.group(1) or 1) != int(owner.group(1) or 1)
                    or _distance(source, pole["source"]) > KML_POLE_SNAP_RADIUS_METERS):
                continue
            if any(ShapelyPoint(pole["source"]).distance(LineString(r["source"])) <= 0.15 or
                   any(_distance(pole["source"], v) <= 1.25 for v in r["source"]) for r in records):
                candidates.append(pole)
        return min(candidates, key=lambda p: _distance(source, p["source"]), default=None)

    def _serving_pole_cable_rotation(self, pole: dict, group: tuple[str, ...] | None = None) -> float:
        """Keep an asset's rotation on its source-owned route's rendered cable."""
        records = getattr(self, "_pole_follow_lines", ())
        group = pole["group"] if group is None else group
        same_route = [r for r in records if r["group"] == group and r.get("cable_kind")]
        if not same_route:
            return float(self._nearest_cable_frame(pole["source"]).get("rotation") or 0.0) if not records else 0.0
        source = ShapelyPoint(pole["source"])
        record = min(same_route, key=lambda r: source.distance(LineString(r["source"])))
        entity = record.get("entity")
        vertices = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices] if entity is not None else record["source"]
        line = LineString(vertices)
        if line.is_empty or line.length <= 1e-9:
            return 0.0
        return self._line_angle_at_position(line, line.project(ShapelyPoint(pole["target"])))

    def _plan_ae_fat_icons(self, pipeline_logger=None) -> list[dict]:
        """Plan a complete bounded batch before emitting any FAT INSERT."""
        owners = []
        for pending in self.pending_fat_icons:
            pole = self._source_owned_asset_pole(pending)
            distance = _distance(pending["projected"], pole["source"]) if pole is not None else None
            if distance is None or distance > KML_POLE_SNAP_RADIUS_METERS:
                nearest = f"; nearest same-route pole {distance:.2f}m" if distance is not None else ""
                raise ValueError(f"FAT {pending.get('code') or 'FAT'} has no serving pole within "
                                 f"{KML_POLE_SNAP_RADIUS_METERS:g}m{nearest}")
            if _distance(pole["source"], pole["target"]) > KML_ROAD_POLE_MAX_SHIFT_METERS + (
                    2.0 if pole.get("crossing") else 0.0) + 1e-6:
                raise ValueError(f"FAT {pending.get('code') or 'FAT'} serving pole exceeds its DXF relocation limit")
            owners.append((pending, pole))

        road = self._road_avoidance_geometry()
        pole_disks = [(ShapelyPoint(float(p["icon"].dxf.insert.x), float(p["icon"].dxf.insert.y)),
                       KML_POLE_ICON_SIZE_METERS / 2 * abs(float(p["icon"].dxf.get("xscale", 1.0))))
                      for p in self._roadside_poles]
        rows = []
        for pending, pole in owners:
            anchor = pole["target"]
            scale = _clamp(float((pending.get("style") or {}).get("icon_scale") or 1.0), 0.5, 1.5)
            pole_radius = KML_POLE_ICON_SIZE_METERS / 2 * abs(float(pole["icon"].dxf.get("xscale", 1.0)))
            rotation_degrees = self._serving_pole_cable_rotation(pole, pending.get("group"))
            rotation = math.radians(rotation_degrees)
            along, normal = (math.cos(rotation), math.sin(rotation)), (-math.sin(rotation), math.cos(rotation))
            half_width, half_height = KML_FAT_ICON_WIDTH_METERS * scale / 2, KML_FAT_ICON_HEIGHT_METERS * scale / 2
            base_gap = pole_radius + (KML_FAT_TO_POLE_GAP_METERS + KML_FAT_ICON_HEIGHT_METERS / 2) * scale
            side, offset_scale = 1, 1.0
            try:
                hint = suggest_dxf_asset_layout(asset_type="FAT", asset_name=str(pending.get("code") or "FAT"),
                                                cable_angle=rotation_degrees, has_serving_pole=True)
                if isinstance(hint, dict) and hint.get("normal_side") in (-1, 1):
                    side = int(hint["normal_side"])
                    value = float(hint.get("offset_scale", 1.0))
                    if math.isfinite(value) and 0.75 <= value <= 1.35:
                        offset_scale = value
            except Exception as exc:
                LOGGER.debug("FAT AI layout fallback: %s", exc)
            # AI only ranks this finite envelope; pole shift and symbol gap are separate.
            # ponytail: 5-degree angles, <=0.75m extra, 100000 trials; extend only with a red fixture.
            angles = [rotation + candidate_side*math.pi/2 + math.radians(delta)
                      for delta in [0, *[d for n in range(5, 91, 5) for d in (n, -n)]]
                      for candidate_side in (side, -side)]
            extras = sorted((0.0, 0.25, 0.5, 0.75), key=lambda extra: abs(base_gap + extra - base_gap*offset_scale))
            options, road_blocked_options, seen = [], [], set()
            retained_in_road = (_distance(pole["source"], anchor) <= 1e-7 and
                                road is not None and not road.is_empty and
                                road.intersects(ShapelyPoint(anchor).buffer(pole_radius)))
            for extra in extras:
                for angle in angles:
                    gap = base_gap + extra
                    center = (anchor[0] + gap*math.cos(angle), anchor[1] + gap*math.sin(angle))
                    key = (round(center[0], 8), round(center[1], 8))
                    if key in seen:
                        continue
                    seen.add(key)
                    footprint = Polygon([
                        (center[0] + u*half_width*along[0] + v*half_height*normal[0],
                         center[1] + u*half_width*along[1] + v*half_height*normal[1])
                        for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1))
                    ])
                    edge_gap = footprint.distance(ShapelyPoint(anchor)) - pole_radius
                    if (0.1 <= edge_gap <= KML_FAT_TO_POLE_GAP_METERS*scale + 0.75 + 1e-6 and
                            all(footprint.distance(point) >= radius + 0.1 - 1e-7 for point, radius in pole_disks)):
                        if road is None or road.is_empty or road.distance(footprint) >= 0.1:
                            options.append((center, footprint))
                        elif retained_in_road:
                            road_blocked_options.append((center, footprint))
            if not options and road_blocked_options:
                # Operator fallback: preserve source pole and its FAT attachment;
                # ROAD alone is advisory here, never pole/FAT collisions or ownership.
                options = sorted(road_blocked_options, key=lambda option: option[1].intersection(road).area)
                pipeline_log(pipeline_logger, "warning",
                             f"FAT {pending.get('code') or 'FAT'} ROAD obstruction advisory: "
                             "serving pole retained at source; FAT stays beside its serving pole.")
            if not options:
                raise ValueError(f"FAT {pending.get('code') or 'FAT'} cannot attach beside serving pole "
                                 f"and clear ROAD/poles within {base_gap + 0.75:.2f}m of its fixed centre")
            rows.append({"anchor": anchor, "rotation": rotation_degrees, "options": options})

        order = sorted(range(len(rows)), key=lambda i: (len(rows[i]["options"]),
                       str(self.pending_fat_icons[i].get("code") or ""), self.pending_fat_icons[i]["projected"], i))
        cursors, selected, position, trials = [0]*len(rows), {}, 0, 0
        while 0 <= position < len(order):
            index = order[position]
            options = rows[index]["options"]
            if cursors[position] == len(options):
                cursors[position] = 0
                position -= 1
                if position >= 0:
                    selected.pop(order[position], None)
                continue
            center, footprint = options[cursors[position]]
            cursors[position] += 1
            trials += 1
            if trials > 100000:
                raise ValueError("FAT batch cannot attach beside serving poles within bounded search budget")
            if all(footprint.intersection(old[1]).area <= 1e-7 for old in selected.values()):
                selected[index] = (center, footprint)
                position += 1
        if position < 0:
            raise ValueError("FAT batch cannot attach beside serving poles without overlapping FATs")
        return [{"anchor": row["anchor"], "rotation": row["rotation"], "center": selected[i][0]}
                for i, row in enumerate(rows)]

    def finalize_fat_icons(self, pipeline_logger=None) -> None:
        if not self.pending_fat_icons:
            return

        ae_plans = self._plan_ae_fat_icons(pipeline_logger) if getattr(self, "network_type", "") == "AE" else None
        for index, pending in enumerate(self.pending_fat_icons):
            output_start_index = len(self.output_entities)
            source_point = pending["projected"]
            side, offset_scale = 1, 1.0
            if ae_plans is not None:
                plan = ae_plans[index]
                anchor, rotation, insert_override = plan["anchor"], plan["rotation"], plan["center"]
            else:
                frame = self._nearest_cable_frame(source_point)
                serving_pole = min(
                    (pole for pole in self._roadside_poles if pole["group"] == pending["group"]),
                    key=lambda pole: _distance(source_point, pole["source"]), default=None,
                )
                anchor = (serving_pole["target"] if serving_pole is not None and
                          _distance(source_point, serving_pole["source"]) <= KML_POLE_SNAP_RADIUS_METERS else source_point)
                rotation = float(frame.get("rotation") or 0.0)
                ai_asset = None
                try:
                    ai_asset = suggest_dxf_asset_layout(asset_type="FAT", asset_name=str(pending.get("code") or "FAT"),
                                                        cable_angle=rotation, has_serving_pole=anchor != source_point)
                except Exception as exc:
                    LOGGER.debug("FAT AI layout fallback: %s", exc)
                side = 1 if int((ai_asset or {}).get("normal_side", 1)) >= 0 else -1
                offset_scale = float((ai_asset or {}).get("offset_scale", 1.0))
                insert_override = None
            road = self._road_avoidance_geometry()
            if ae_plans is None and road is not None and not road.is_empty:
                style_scale = _clamp(float((pending.get("style") or {}).get("icon_scale") or 1.0), 0.5, 1.5)
                radians = math.radians(rotation)
                along = (math.cos(radians), math.sin(radians))
                normal = (-along[1], along[0])
                half_width = KML_FAT_ICON_WIDTH_METERS * style_scale / 2
                half_height = KML_FAT_ICON_HEIGHT_METERS * style_scale / 2
                base_gap = (KML_POLE_ICON_SIZE_METERS / 2 + KML_FAT_TO_POLE_GAP_METERS + KML_FAT_ICON_HEIGHT_METERS / 2) * style_scale * offset_scale

                def clear_fat(center):
                    corners = [
                        (center[0] + u * half_width * along[0] + v * half_height * normal[0],
                         center[1] + u * half_width * along[1] + v * half_height * normal[1])
                        for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1))
                    ]
                    return road.distance(Polygon(corners)) >= 0.1

                # Try the original cable-normal side first. Widen the offset only
                # for this FAT when its complete rotated footprint still touches ROAD.
                for extra in (step * 0.5 for step in range(51)):
                    for candidate_side in (side, -side):
                        gap = base_gap + extra
                        center = (anchor[0] + normal[0] * gap * candidate_side,
                                  anchor[1] + normal[1] * gap * candidate_side)
                        if _distance(source_point, center) <= 30 and clear_fat(center):
                            insert_override = center
                            side = candidate_side
                            break
                    if insert_override is not None:
                        break
                if insert_override is None:
                    # A sharply angled cable normal can follow ROAD for >25m.
                    # Use its true outer edge rather than silently drawing in-road.
                    expanded = road.buffer(math.hypot(half_width, half_height) + 0.15)
                    edge = nearest_points(ShapelyPoint(anchor), expanded.boundary)[1]
                    candidate = (float(edge.x), float(edge.y))
                    if _distance(source_point, candidate) <= 30 and clear_fat(candidate):
                        insert_override = candidate
                if insert_override is None:
                    raise ValueError(f"FAT {pending.get('code') or 'FAT'} cannot clear ROAD within 30m")
            insert = self._add_kml_fat_icon(
                anchor,
                str(pending.get("code") or "FAT"),
                pending.get("style") or {},
                rotation=rotation,
                normal_side=side,
                offset_scale=offset_scale,
                insert_override=insert_override,
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
        self.pending_fat_icons.clear()

    def _add_kml_handhole_label(self, source_point: Point, spec: dict, name: str,
                                style: dict | None = None, icon_rotation: float = 0.0) -> None:
        name = name.strip()
        if not name:
            return
        scale = _clamp(float((style or {}).get("icon_scale") or 1.0), 0.5, 1.5)
        size = float(spec.get("icon_size") or KML_HANDHOLE_ICON_SIZE_METERS) * scale
        height = KML_HANDHOLE_LABEL_HEIGHT_METERS * scale
        road = self._nearest_road_frame(source_point)
        rotation = float(road["road_angle"] if road else icon_rotation)
        radians = math.radians(rotation)
        normal = (-math.sin(radians), math.cos(radians))
        if road:
            side = -1.0 if sum(a * b for a, b in zip(normal, road["away"])) >= 0 else 1.0
        else:
            side = -1.0  # ponytail: no ROAD uses the cable's lower side; real ROAD wins when present.
        label = self._add_centered_text(name, source_point, height,
            dxfattribs={"layer": str(spec["layer"]),
                        "color": int(spec.get("color") or KML_HANDHOLE_COLOR)},
            rotation=rotation)
        width = min(size + 4.0, max(height, len(name) * height * 0.65))
        icon_angle = math.radians(float(icon_rotation))
        delta = radians - icon_angle
        edge = size / 2 * (abs(math.cos(delta)) + abs(math.sin(delta)))
        center = tuple(float(p) + side * n * (edge + 0.22 * scale + height) for p, n in zip(source_point, normal))
        tangent = (math.cos(radians), math.sin(radians))
        start = tuple(p - t * width / 2 for p, t in zip(center, tangent))
        end = tuple(p + t * width / 2 for p, t in zip(center, tangent))
        # Native FIT enforces the width in CAD even when server fonts are absent.
        label.set_placement(start, end, align=TextEntityAlignment.FIT)
        self.kml_detail_stats["labels"] += 1

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
            if self.network_type == "UG":
                self._add_kml_handhole_label(source_point, pending["spec"],
                    str(pending.get("name") or ""), pending.get("style") or {},
                    float(frame.get("rotation") or 0.0))
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

    def _add_kml_slack_hanger_icon(self, projected: Point, style: dict | None = None, rotation: float | None = None) -> None:
        self._ensure_kml_slack_hanger_block()
        # AE may follow a verified source-owned pole; orientation stays on its route.
        if rotation is None:
            rotation = float(self._nearest_cable_frame(projected).get("rotation") or 0.0)
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
        insert.dxf.rotation = float(rotation)
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

    @staticmethod
    def _pole_line_group(folder_stack: Sequence[str] | None) -> tuple[str, ...]:
        # Category differs (POLE vs CABLE), but parent route must be identical.
        return tuple(normalized_folder_name(folder) for folder in (folder_stack or ())[:-1])

    def finalize_roadside_poles_and_lines(self, pipeline_logger=None) -> None:
        """Reposition DXF pole symbols; attach only their source-connected KML lines."""
        if not self._roadside_poles:
            self._space_unanchored_parallel_cables()
            self._validate_new_cable_crossings()
            return
        road = self._road_avoidance_geometry()
        radii = {id(pole): KML_POLE_ICON_SIZE_METERS / 2 * abs(float(pole["icon"].dxf.get("xscale", 1.0)))
                 for pole in self._roadside_poles}
        moved = []
        for pole in self._roadside_poles:
            source = ShapelyPoint(pole["source"])
            if road is None or road.is_empty:
                continue
            radius = KML_POLE_ICON_SIZE_METERS / 2 * float(pole["icon"].dxf.get("xscale", 1.0))
            # Outside ROAD: pull to the outer edge within 5m; inside: escape within 8m.
            # ponytail: nearest point per local boundary component; no cable rerouter.
            max_shift = (KML_ROAD_POLE_MAX_SHIFT_METERS if road.covers(source)
                         else KML_ROADSIDE_POLE_MAX_SHIFT_METERS)
            if source.distance(road) > radius + max_shift + 1e-8:
                continue
            expanded = road.buffer(radius)
            boundary = nearest_points(source, expanded.boundary)[1]
            boundaries = [boundary]
            # A legal hole edge can still collapse cables; try only the nearest
            # exterior as a second bounded target, not an angular routing search.
            outer_edges = unary_union([polygon.exterior for polygon in
                                       getattr(expanded, "geoms", (expanded,))])
            if boundary.distance(outer_edges) > 1e-7:
                boundaries.append(nearest_points(source, outer_edges)[1])
            # Defer other nearby ROAD sides until primary targets fail; keep
            # each trial inside the same source5/8m disk and full native guards.
            def bounded_edges():
                yield from boundaries
                local = expanded.boundary.intersection(source.buffer(max_shift + 1e-8))
                parts = sorted(getattr(local, "geoms", (local,)), key=source.distance)
                for part in parts:
                    if part.is_empty:
                        continue
                    point = nearest_points(source, part)[1]
                    if all(point.distance(tried) > 1e-7 for tried in boundaries):
                        yield point
            candidates = list(bounded_edges())
            # Both-side probe: when the local boundary intersection is one connected
            # part, the opposite road side is never enumerated; cast bounded rays
            # along the local road normal so the other side is tried before restore.
            frame = self._nearest_road_frame(pole["source"])
            if frame is not None:
                tangent = frame["tangent"]
                normal = (-tangent[1], tangent[0])
                for direction in (normal, (-normal[0], -normal[1])):
                    ray = LineString([pole["source"],
                                      (pole["source"][0] + direction[0] * (max_shift + 2.0),
                                       pole["source"][1] + direction[1] * (max_shift + 2.0))])
                    hit = expanded.boundary.intersection(ray)
                    points = [part for part in getattr(hit, "geoms", (hit,))
                              if part.geom_type == "Point" and not part.is_empty]
                    if not points:
                        continue
                    point = min(points, key=lambda part: _distance(pole["source"], (part.x, part.y)))
                    if all(point.distance(tried) > 1e-7 for tried in candidates):
                        candidates.append(point)
            # v6 (operator): an in-road pole whose own-side exit would pile onto
            # an existing pole bank (~10m, "numpuk pole") crosses the carriageway
            # to the empty opposite side instead of hugging the crowded edge.
            crossing = None
            crossing_options = []
            if road.covers(source):
                near_edge = nearest_points(source, road.boundary)[1]
                near_vector = (near_edge.x - source.x, near_edge.y - source.y)
                near_length = math.hypot(near_vector[0], near_vector[1])
                near_points = []
                for boundary in candidates:
                    point = (float(boundary.x), float(boundary.y))
                    vector = (point[0] - source.x, point[1] - source.y)
                    if vector[0] * near_vector[0] + vector[1] * near_vector[1] >= 0:
                        near_points.append(point)

                def _bank_within(target):
                    for other in self._roadside_poles:
                        if other is pole:
                            continue
                        other_point = other.get("target") or other["source"]
                        if _distance(target, other_point) < 10.0 - 1e-5:
                            return True
                    return False

                near_best = (min(near_points, key=lambda point: _distance(pole["source"], point))
                             if near_points else None)
                if near_best is not None and _bank_within(near_best) and near_length > 1e-9:
                    # Geometric across target: continue the ray from the nearest
                    # edge through the source to the next raw-road exit, then
                    # offset outward until the full scaled icon clears the road
                    # (fork geometry can angle the far edge away from the ray).
                    ux = (pole["source"][0] - near_edge.x) / near_length
                    uy = (pole["source"][1] - near_edge.y) / near_length
                    ray = LineString([pole["source"], (pole["source"][0] + ux * 60.0,
                                                       pole["source"][1] + uy * 60.0)])
                    exit_here = ray.intersection(road.boundary)
                    hits = (list(exit_here.geoms) if hasattr(exit_here, "geoms")
                            else ([exit_here] if exit_here.geom_type == "Point" else []))
                    hits = [hit for hit in hits
                            if (hit.x - pole["source"][0]) * ux + (hit.y - pole["source"][1]) * uy > 1e-6]
                    hits.sort(key=lambda hit: (hit.x - pole["source"][0]) * ux + (hit.y - pole["source"][1]) * uy)
                    if hits:
                        exit_point = hits[0]
                        base = _distance(pole["source"], (exit_point.x, exit_point.y))
                        # Fan search around the across direction: fork geometry
                        # can angle the far edge so the straight ray stays
                        # parallel to it. Try bounded angular offsets at the
                        # exit until the full scaled icon clears the road and
                        # the point stays genuinely across (segment crosses the
                        # road body) and outside every pole bank.
                        base_angle = math.atan2(uy, ux)
                        # Sweep the radial distance up to the remaining +2m
                        # crossing allowance; a fixed small ring can miss the
                        # only legal spot (fork edge angles away from the ray).
                        max_extra = max(radius, max_shift + 2.0 - base)
                        dists = []
                        ring = radius
                        while ring <= max_extra + 1e-9:
                            dists.append(ring)
                            ring += 0.25
                        fan = []
                        for delta_deg in (0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90, 105, -105, 120, -120):
                            for extra_dist in dists:
                                angle = base_angle + math.radians(delta_deg)
                                candidate = (exit_point.x + math.cos(angle) * extra_dist,
                                             exit_point.y + math.sin(angle) * extra_dist)
                                shift = _distance(pole["source"], candidate)
                                if shift > max_shift + 2.0 + 1e-8:
                                    continue
                                if road.distance(ShapelyPoint(candidate)) < radius - 0.01:
                                    continue
                                if (candidate[0] - pole["source"][0]) * ux + (candidate[1] - pole["source"][1]) * uy <= base - 0.5:
                                    continue
                                crossing_seg = LineString([pole["source"], candidate])
                                if crossing_seg.intersection(road).length < 0.1:
                                    continue
                                if _bank_within(candidate):
                                    continue
                                fan.append((abs(delta_deg), shift, candidate))
                        if fan:
                            fan.sort(key=lambda row: (row[0], row[1]))
                            crossing_options = [row[2] for row in fan]
            def _is_crossing(point):
                return any(_distance(point, option) <= 1e-6 for option in crossing_options)

            rest = []
            for boundary in candidates:
                point = (float(boundary.x), float(boundary.y))
                if not _is_crossing(point):
                    rest.append(point)
            rest.sort(key=lambda point: (
                not self._candidate_is_empty_side(pole, point, radii),
                _distance(pole["source"], point)))
            # Try every operator-authorized crossing candidate first (fan order:
            # smallest angular deviation, then shortest shift); fall back to the
            # regular same-side candidates only if all crossings are rejected.
            ordered = crossing_options + rest
            for target in ordered:
                crossing_here = _is_crossing(target)
                shift = _distance(pole["source"], target)
                if (shift > max_shift + (2.0 if crossing_here else 0.0) + 1e-8
                        or road.distance(ShapelyPoint(target)) < radius - 0.01):
                    continue
                pole["crossing"] = crossing_here
                # Side rule: a candidate that would collide with or merge another
                # pole is skipped so the opposite side can be tried first.
                if self._pole_target_conflicts_with_poles(pole, target, radii):
                    pipeline_log(pipeline_logger, "warning",
                                 "ROAD pole target would merge or collide with another pole; trying the other side.")
                    continue
                # Compare with the current accepted layout, not inherited source conflicts.
                before = self._preview_pole_follow_lines()
                pole["target"] = target
                try:
                    after = self._preview_pole_follow_lines()
                    if self._pole_relocation_causes_collision(before, after):
                        pole["target"] = pole["source"]
                        pipeline_log(pipeline_logger, "warning",
                                     "ROAD pole movement would introduce a cable collision; trying the other side.")
                        continue
                except ValueError as error:
                    pole["target"] = pole["source"]
                    pipeline_log(pipeline_logger, "warning", f"ROAD pole target rejected: {error}; trying the next bounded target.")
                    continue
                if crossing_here:
                    pipeline_log(pipeline_logger, "info",
                                 f"ROAD pole crossing to the empty opposite side: {shift:.2f}m.")
                break
            else:
                pole.pop("crossing", None)
                pole["target"] = pole["source"]
                pipeline_log(pipeline_logger, "warning", f"No collision-free ROAD pole target within {max_shift:g}m; left at exact source coordinate.")
                continue
            pole["icon"].dxf.insert = (target[0], target[1], pole["icon"].dxf.insert.z)
            self.pole_positions[pole["position_index"]] = target
            if pole["label"] is not None:
                pole["label"]["projected"] = target
            moved.append(pole)
        self._restore_conflicting_roadside_poles(pipeline_logger)
        moved = [pole for pole in moved if _distance(pole["source"], pole["target"]) > 1e-7]
        updated = self._render_pole_follow_lines(pipeline_logger)
        pipeline_log(pipeline_logger, "info", f"DXF roadside layout: {len(moved)} pole(s) moved, {updated} connected LineString(s) aligned.")

    def _candidate_is_empty_side(self, pole, target, radii) -> bool:
        """User side rule: a candidate side is 'empty' when no other pole sits near it."""
        for other in self._roadside_poles:
            if other is pole:
                continue
            other_point = other.get("target") or other["source"]
            if _distance(target, other_point) < radii[id(pole)] + radii[id(other)] + 1.0 - 1e-5:
                return False
        return True

    def _pole_target_conflicts_with_poles(self, pole, target, radii) -> bool:
        """Side rule: reject a target that collides with or merges another pole bank."""
        for other in self._roadside_poles:
            if other is pole:
                continue
            other_point = other.get("target") or other["source"]
            gap_limit = radii[id(pole)] + radii[id(other)]
            source_gap = _distance(pole["source"], other["source"])
            if source_gap >= gap_limit - 1e-5 and _distance(target, other_point) < gap_limit - 1e-5:
                return True
            if source_gap <= KML_HOME_NUMBER_ROTATION_SEARCH_RADIUS_METERS:
                midpoint = tuple((a + b) / 2 for a, b in zip(pole["source"], other["source"]))
                frame = self._nearest_road_frame(midpoint)
                if frame is not None:
                    tangent = frame["tangent"]
                    normal = (-tangent[1], tangent[0])
                    source_side = sum((a - b) * n for a, b, n in
                                      zip(pole["source"], other["source"], normal))
                    target_side = sum((a - b) * n for a, b, n in zip(target, other_point, normal))
                    if (abs(source_side) > gap_limit + 1e-5 and
                            (abs(target_side) < gap_limit - 1e-5 or source_side * target_side < 0)):
                        # ponytail: an in-road pole must exit the carriageway; a
                        # same-side bounded pull that keeps at least one icon
                        # radius of lateral separation is accepted so the exit
                        # is not blocked by a distant bank partner.
                        road = self._road_avoidance_geometry()
                        if (source_side * target_side > 0
                                and abs(source_side) - abs(target_side) <= gap_limit + 1e-5
                                and abs(target_side) >= KML_POLE_ICON_SIZE_METERS / 2.0 - 1e-5
                                and road is not None and not road.is_empty
                                and road.covers(ShapelyPoint(pole["source"]))):
                            continue
                        # v6: an explicit carriageway crossing to the empty
                        # opposite side may flip the sign while keeping every
                        # other pole at least as far apart as at source.
                        if (pole.get("crossing")
                                and road is not None and not road.is_empty
                                and road.covers(ShapelyPoint(pole["source"]))
                                and abs(target_side) >= abs(source_side) - 1e-5):
                            continue
                        return True
        return False

    def _pole_relocation_causes_collision(self, before, after) -> bool:
        """Reject move-created collisions; inherited cable warnings remain advisory."""
        records = [record for record in self._pole_follow_lines
                   if record["entity"].dxftype() == "POLYLINE" and len(record["source"]) >= 2]
        for index, current in enumerate(after):
            previous = before[index]
            if previous.is_simple and not current.is_simple:
                return True
            for other in range(index + 1, len(after)):
                old_a, old_b, new_a, new_b = previous, before[other], current, after[other]
                if len(records) == len(after):
                    # Only actual common owners permit cable contact inside their icons.
                    masks = []
                    for pole in self._roadside_poles:
                        if all(self._source_cable_contact(pole, records[i]) for i in (index, other)):
                            radius = KML_POLE_ICON_SIZE_METERS / 2 * abs(float(pole["icon"].dxf.get("xscale", 1.0)))
                            masks.extend(ShapelyPoint(pole[key]).buffer(radius + 1e-5)
                                         for key in ("source", "target"))
                    if masks:
                        mask = unary_union(masks)
                        old_a, old_b, new_a, new_b = (line.difference(mask) for line in
                                                     (old_a, old_b, new_a, new_b))
                old_cross, new_cross = old_a.intersection(old_b), new_a.intersection(new_b)
                if old_cross.is_empty and not new_cross.is_empty:
                    # ponytail: a source crossing hidden inside a shared pole
                    # icon may pop out just beyond the icon when its pole moves.
                    # It is the same crossing while it stays within the cap.
                    source_cross = previous.intersection(before[other])
                    if (source_cross.is_empty
                            or new_cross.length > source_cross.length + 1e-5
                            or new_cross.distance(source_cross) > KML_ROAD_POLE_MAX_SHIFT_METERS + 1e-5):
                        return True
                elif new_cross.length > old_cross.length + 1e-5:
                    return True
                if (old_a.distance(old_b) >= 0.7 and new_a.distance(new_b) < 0.7 - 1e-4
                        and previous.distance(before[other]) >= 0.7 - 1e-4):
                    return True
        return False

    def _restore_conflicting_roadside_poles(self, pipeline_logger=None) -> None:
        """Keep only implicated poles at KMZ source; never infer sides from numbering."""
        poles = self._roadside_poles
        while True:
            restore = set()
            for index, first in enumerate(poles):
                for other in range(index + 1, len(poles)):
                    second = poles[other]
                    if all(_distance(p["source"], p["target"]) <= 1e-7 for p in (first, second)):
                        continue
                    radius = sum(KML_POLE_ICON_SIZE_METERS / 2 * abs(float(p["icon"].dxf.get("xscale", 1.0)))
                                 for p in (first, second))
                    source_gap = _distance(first["source"], second["source"])
                    conflict = (source_gap >= radius - 1e-5 and
                                _distance(first["target"], second["target"]) < radius - 1e-5)
                    if source_gap <= KML_HOME_NUMBER_ROTATION_SEARCH_RADIUS_METERS:
                        midpoint = tuple((a + b) / 2 for a, b in zip(first["source"], second["source"]))
                        frame = self._nearest_road_frame(midpoint)
                        if frame is not None:
                            tangent = frame["tangent"]
                            normal = (-tangent[1], tangent[0])
                            source_side = sum((a - b) * n for a, b, n in
                                              zip(first["source"], second["source"], normal))
                            target_side = sum((a - b) * n for a, b, n in
                                              zip(first["target"], second["target"], normal))
                            # ponytail: local transverse bank collapse only; no ROAD realignment.
                            # In-road exits with bounded same-side pulls are kept.
                            road = self._road_avoidance_geometry()
                            if (abs(source_side) > radius + 1e-5 and
                                    (abs(target_side) < radius - 1e-5 or source_side * target_side < 0) and
                                    not (source_side * target_side > 0
                                         and abs(source_side) - abs(target_side) <= radius + 1e-5
                                         and abs(target_side) >= KML_POLE_ICON_SIZE_METERS / 2.0 - 1e-5
                                         and road is not None and not road.is_empty
                                         and (road.covers(ShapelyPoint(first["source"]))
                                              or road.covers(ShapelyPoint(second["source"]))))
                                    and not ((first.get("crossing") or second.get("crossing"))
                                             and road is not None and not road.is_empty
                                             and abs(target_side) >= abs(source_side) - 1e-5)):
                                conflict = True
                    if conflict:
                        restore.update(i for i in (index, other)
                                       if _distance(poles[i]["source"], poles[i]["target"]) > 1e-7)
            if not restore:
                return
            for index in sorted(restore):
                pole = poles[index]
                pole["target"] = pole["source"]
                insert = pole["icon"].dxf.insert
                pole["icon"].dxf.insert = (*pole["source"], insert.z)
                self.pole_positions[pole["position_index"]] = pole["source"]
                if pole["label"] is not None:
                    pole["label"]["projected"] = pole["source"]
                pipeline_log(pipeline_logger, "warning",
                             f"ROAD pole {pole['icon'].dxf.handle}: move would merge source banks or collide; retained exact KMZ coordinate.")

    def _nearest_vertex_pole(self, vertex: Point) -> dict | None:
        """DXF vertex matching only; never broadens FAT/slack source ownership."""
        pole = min(self._roadside_poles, key=lambda p: _distance(vertex, p["source"]), default=None)
        return pole if pole is not None and _distance(vertex, pole["source"]) <= 10.0 + 1e-8 else None

    def _source_cable_contact(self, pole: dict, record: dict) -> bool:
        """Nearest source vertices within10m or a proven segment-only contact."""
        if not record.get("cable_kind") or len(record["source"]) < 2:
            return False
        return (any(self._nearest_vertex_pole(vertex) is pole for vertex in record["source"]) or
                ShapelyPoint(pole["source"]).distance(LineString(record["source"])) <= 0.15)

    def _preview_pole_follow_lines(self) -> list[LineString]:
        """Render disposable cable entities through exactly the commit path."""
        from copy import copy
        preview = copy(self)
        document = ezdxf.new()
        preview._pole_follow_lines = []
        for record in self._pole_follow_lines:
            entity = record["entity"]
            if entity.dxftype() != "POLYLINE":
                continue
            trial = document.modelspace().add_polyline2d(record["source"],
                                                        dxfattribs={"layer": entity.dxf.layer})
            preview._pole_follow_lines.append({**record, "entity": trial})
        preview._render_pole_follow_lines()
        rendered = [LineString([(v.dxf.location.x, v.dxf.location.y) for v in record["entity"].vertices])
                    for record in preview._pole_follow_lines]
        return rendered

    def _cable_collision_warning(self, message: str, records, pipeline_logger=None) -> bool:
        """Operator policy: cable collisions are advisory, not attachment failures."""
        records = tuple(records)
        if not records or any(record.get("cable_kind") not in {"distribution", "sling"}
                              for record in records):
            return False
        routes = ','.join(str(record["entity"].dxf.handle) for record in records)
        pipeline_log(pipeline_logger, "warning",
                     f"DXF cable collision advisory (handles {routes}): {message}; "
                     "Distribution/Sling pole attachments retained.")
        return True

    def _render_pole_follow_lines(self, pipeline_logger=None) -> int:
        """Shared existing port/interpolation renderer; preview and commit agree."""
        road = self._road_avoidance_geometry()
        # Keep unchanged poles as anchors too: all connected LineStrings need
        # an exact vertex at each pole circle, even when its source is roadside.

        # Source geometry, not LINE ancestry, establishes cable contact.
        associated: dict[int, list[tuple[dict, float]]] = defaultdict(list)
        for pole in self._roadside_poles:
            source = ShapelyPoint(pole["source"])
            options = []
            for index, record in enumerate(self._pole_follow_lines):
                line = LineString(record["source"])
                distance = source.distance(line)
                if self._source_cable_contact(pole, record):
                    vertices = [vertex for vertex in record["source"] if self._nearest_vertex_pole(vertex) is pole]
                    contact = min(vertices, key=lambda vertex: _distance(vertex, pole["source"])) if vertices else source
                    options.append((distance, index, line.project(ShapelyPoint(contact))))
            if not options:
                continue
            options.sort()
            # All verified source visits count, including an interior vertex
            # beside another cable's endpoint; proximity alone is not contact.
            for _distance_m, index, station in options:
                associated[index].append((pole, station))

        links: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for index, anchors in associated.items():
            for pole, station in anchors:
                links[id(pole)].append((index, station))
        # Assign shared pole-to-pole runs in one frame. Per-pole greedy angles
        # can reverse their order when a source LineString runs B -> A.
        runs: dict[tuple[int, int], list[tuple[int, dict, dict, float, float]]] = defaultdict(list)
        for index, anchors in associated.items():
            for (left, s_left), (right, s_right) in zip(sorted(anchors, key=lambda item: item[1]),
                                                       sorted(anchors, key=lambda item: item[1])[1:]):
                if abs(s_right - s_left) <= 2.5:
                    continue
                a, b = sorted((left, right), key=lambda pole: pole["source"])
                runs[(id(a), id(b))].append((index, a, b, min(s_left, s_right), max(s_left, s_right)))

        requests: dict[tuple[int, int], list[Point]] = defaultdict(list)
        shared_runs = {}
        for key, members in runs.items():
            if len(members) < 2:
                continue
            a, b = members[0][1:3]
            dx, dy = b["source"][0] - a["source"][0], b["source"][1] - a["source"][1]
            length = math.hypot(dx, dy)
            if length < 2.5:
                continue
            normal = (-dy / length, dx / length)
            origin = a["source"]
            def side(member):
                _, _, _, start, end = member
                mid = LineString(self._pole_follow_lines[member[0]]["source"]).interpolate((start + end) / 2)
                return (mid.x - origin[0]) * normal[0] + (mid.y - origin[1]) * normal[1]
            ordered = sorted(members, key=lambda member: (round(side(member), 2), member[0]))
            lanes = {2: (-0.6, 0.6), 3: (-1.05, -0.35, 0.35),
                     4: (-1.125, -0.375, 0.375, 1.125)}.get(len(ordered))
            if lanes is None:
                lanes = tuple(0.7 * (i - (len(ordered) - 1) / 2) for i in range(len(ordered)))
            for member, lane in zip(ordered, lanes):
                index, _, _, start, end = member
                for pole in (a, b):
                    requests[(id(pole), index)].append((normal[0] * lane, normal[1] * lane))
            shared_runs[key] = (a, b, tuple(member[0] for member in members))

        ports: dict[tuple[int, int], Point] = {}
        for pole in self._roadside_poles:
            connections = sorted(links[id(pole)])
            if len(connections) < 2:
                continue
            center = pole["target"]
            radius = (KML_POLE_ICON_SIZE_METERS / 2) * float(pole["icon"].dxf.get("xscale", 1.0))
            inset = radius * 0.6
            used = []
            for index, station in connections:
                key = (id(pole), index)
                if key not in requests:
                    continue
                vectors = requests[key]
                vx = sum(item[0] for item in vectors) / len(vectors)
                vy = sum(item[1] for item in vectors) / len(vectors)
                if math.hypot(vx, vy) > radius - 0.2 + 1e-8:
                    message = "Shared cable lane cannot fit inside pole"
                    if not self._cable_collision_warning(message, [self._pole_follow_lines[i] for i, _ in connections], pipeline_logger):
                        raise ValueError(message)
                    factor = max(0.0, radius - 0.2) / math.hypot(vx, vy)
                    vx, vy = vx * factor, vy * factor
                ports[key] = (center[0] + vx, center[1] + vy)
                used.append(math.atan2(vy, vx))
            if any(_distance(ports[(id(pole), i)], ports[(id(pole), j)]) < 0.7 - 1e-5
                   for n, (i, _) in enumerate(connections) for j, _ in connections[n + 1:]
                   if (id(pole), i) in ports and (id(pole), j) in ports):
                message = "Shared cable lanes collide inside pole"
                if not self._cable_collision_warning(message, [self._pole_follow_lines[i] for i, _ in connections], pipeline_logger):
                    raise ValueError(message)
            for index, station in connections:
                if (id(pole), index) in ports:
                    continue
                line = LineString(self._pole_follow_lines[index]["source"])
                before = line.interpolate(max(0.0, station - 0.5))
                after = line.interpolate(min(line.length, station + 0.5))
                dx, dy = after.x - before.x, after.y - before.y
                if station > 0.5 and station < line.length - 0.5:
                    dx, dy = -dy, dx
                elif station >= line.length - 0.5:
                    dx, dy = -dx, -dy
                angle = math.atan2(dy, dx)
                angles = sorted((2 * math.pi * step / 72 for step in range(72)),
                                key=lambda candidate: (1 - math.cos(candidate - angle), candidate))
                chosen = next((candidate for candidate in angles if all(
                    2 * inset * abs(math.sin((candidate - other) / 2)) >= min(0.7, inset)
                    for other in used)), None)
                if chosen is None:
                    message = "Too many cables to separate inside one pole circle"
                    if not self._cable_collision_warning(message, [self._pole_follow_lines[i] for i, _ in connections], pipeline_logger):
                        raise ValueError(message)
                    chosen = angles[0]  # Keep the owning pole attachment when lanes are full.
                used.append(chosen)
                ports[(id(pole), index)] = (center[0] + inset * math.cos(chosen),
                                             center[1] + inset * math.sin(chosen))
            if any(_distance(ports[(id(pole), i)], ports[(id(pole), j)]) < 0.7 - 1e-5
                   for n, (i, _) in enumerate(connections) for j, _ in connections[n + 1:]):
                message = "Cable ports collide inside pole circle"
                if not self._cable_collision_warning(message, [self._pole_follow_lines[i] for i, _ in connections], pipeline_logger):
                    raise ValueError(message)

        # Reserve one position per native vertex visit, including return legs.
        # Route-level ports alone can hide a returning vertex on another cable.
        vertex_ports = {}
        visits = defaultdict(list)
        for index, record in enumerate(self._pole_follow_lines):
            if not record.get("cable_kind"):
                continue
            owners = [self._nearest_vertex_pole(point) for point in record["source"]]
            for k, owner in enumerate(owners):
                if owner is None:
                    continue
                anchor = ports.get((id(owner), index), owner["target"])
                points = [point for point, pole in zip(record["source"], owners) if pole is owner]
                dx, dy = anchor[0] - owner["target"][0], anchor[1] - owner["target"][1]
                radius = KML_POLE_ICON_SIZE_METERS / 2 * abs(float(owner["icon"].dxf.get("xscale", 1.0)))
                if len(points) > 1:
                    extent = max(math.hypot(dx + point[0] - owner["source"][0],
                                            dy + point[1] - owner["source"][1]) for point in points)
                    factor = min(1.0, (radius - 0.2) / max(extent, 1e-8))
                    target = (owner["target"][0] + factor * (dx + record["source"][k][0] - owner["source"][0]),
                              owner["target"][1] + factor * (dy + record["source"][k][1] - owner["source"][1]))
                else:
                    target = anchor
                vertex_ports[(index, k)] = target
                visits[id(owner)].append((index, k, record["source"][k]))
        inserted_visits = {}
        for index, anchors in associated.items():
            record = self._pole_follow_lines[index]
            for pole, station in anchors:
                if any(self._nearest_vertex_pole(point) is pole for point in record["source"]):
                    continue
                k = -self._roadside_poles.index(pole) - 1
                point = LineString(record["source"]).interpolate(station)
                vertex_ports[(index, k)] = ports.get((id(pole), index), pole["target"])
                visits[id(pole)].append((index, k, (point.x, point.y)))
                inserted_visits[(index, k)] = (pole, station)
        for pole in self._roadside_poles:
            occupied = visits[id(pole)]
            has_inserted_return = any(k < 0 for _, k, _ in occupied) and any(
                sum(j == i and l >= 0 for j, l, _ in occupied) > 1 for i, k, _ in occupied if k >= 0)
            if len(occupied) < 2 or (not has_inserted_return and all(
                    _distance(vertex_ports[(i, k)], vertex_ports[(j, l)]) >= 0.7 - 1e-4
                    for n, (i, k, _) in enumerate(occupied) for j, l, _ in occupied[n + 1:])):
                continue
            # ponytail: crowded visits share one source-tangent frame; no route detour.
            spans = [(points[q][0] - points[k][0], points[q][1] - points[k][1])
                     for i, k, _ in occupied if k >= 0 for points in [self._pole_follow_lines[i]["source"]]
                     for q in (k - 1, k + 1) if 0 <= q < len(points)]
            for i, k, _ in occupied:
                if k >= 0:
                    continue
                _, station = inserted_visits[(i, k)]
                line = LineString(self._pole_follow_lines[i]["source"])
                left, right = line.interpolate(max(0.0, station - 8)), line.interpolate(min(line.length, station + 8))
                spans.append((right.x - left.x, right.y - left.y))
            dx, dy = max(spans, key=lambda delta: math.hypot(*delta))
            length = math.hypot(dx, dy)
            if length < 1e-8:
                raise ValueError("Coincident cable visits have no source lane direction")
            if dx < 0 or (abs(dx) < 1e-8 and dy < 0):
                dx, dy = -dx, -dy
            normal = (-dy / length, dx / length)
            ordered = sorted(occupied, key=lambda item: ((item[2][0] - pole["source"][0]) * normal[0] +
                                                          (item[2][1] - pole["source"][1]) * normal[1], item[:2]))
            radius = KML_POLE_ICON_SIZE_METERS / 2 * abs(float(pole["icon"].dxf.get("xscale", 1.0)))
            if 0.7 * (len(ordered) - 1) / 2 > radius - 0.2 + 1e-8:
                message = "Cable vertex visits cannot fit distinct lanes inside pole"
                if not self._cable_collision_warning(message, [self._pole_follow_lines[i] for i, _, _ in ordered], pipeline_logger):
                    raise ValueError(message)
            spacing = min(0.8, 2 * (radius - 0.2) / (len(ordered) - 1))
            for n, (i, k, _) in enumerate(ordered):
                lane = spacing * (n - (len(ordered) - 1) / 2)
                vertex_ports[(i, k)] = (pole["target"][0] + normal[0] * lane,
                                        pole["target"][1] + normal[1] * lane)
            if len({i for i, _, _ in occupied}) == 1:
                i = occupied[0][0]
                source = self._pole_follow_lines[i]["source"]
                def trial_curve(options):
                    return LineString([options.get((i, k), vertex_ports.get((i, k), point))
                                       for k, point in enumerate(source)])
                if LineString(source).is_simple and not trial_curve({}).is_simple:
                    # A bent return loop needs angular ports, not collinear lanes.
                    angular = sorted(occupied, key=lambda item: (math.atan2(item[2][1] - pole["source"][1],
                                                                           item[2][0] - pole["source"][0]), item[1]))
                    count = len(angular)
                    for anchor, (_, _, point) in enumerate(angular):
                        angle = math.atan2(point[1] - pole["source"][1], point[0] - pole["source"][0]) - anchor * math.tau / count
                        options = {(i, k): (pole["target"][0] + (radius - 0.2) * math.cos(angle + n * math.tau / count),
                                            pole["target"][1] + (radius - 0.2) * math.sin(angle + n * math.tau / count))
                                   for n, (i, k, _) in enumerate(angular)}
                        if (trial_curve(options).is_simple and
                                all(_distance(a, b) >= 0.7 - 1e-4 for n, a in enumerate(options.values())
                                    for b in list(options.values())[n + 1:])):
                            vertex_ports.update(options)
                            break

        updated = 0
        for index, anchors in associated.items():
            record = self._pole_follow_lines[index]
            entity = record["entity"]
            if entity.dxftype() != "POLYLINE" or len(entity.vertices) != len(record["source"]):
                continue
            old = record["source"]
            line = LineString(old)
            stations = [line.project(ShapelyPoint(p)) for p in old]
            entries = []
            for pole, station in sorted(anchors, key=lambda item: item[1]):
                if entries and abs(entries[-1][0] - station) < 0.1:
                    continue
                entries.append((station, pole))
            vertex_owners = [self._nearest_vertex_pole(point) for point in old]
            for vertex_index, vertex in enumerate(entity.vertices):
                station = stations[vertex_index]
                owner = vertex_owners[vertex_index]
                if owner is not None:
                    target = vertex_ports[(index, vertex_index)]
                else:
                    continue  # Unmatched >10m vertices retain exact source.
                loc = vertex.dxf.location
                vertex.dxf.location = (target[0], target[1], loc.z)
            inserted = []
            for station, pole in entries:
                if any(self._nearest_vertex_pole(original) is pole for original in old):
                    continue
                segment = next((j for j in range(len(stations)-1) if stations[j]+1e-7 < station < stations[j+1]-1e-7), None)
                if segment is not None:
                    k = -self._roadside_poles.index(pole) - 1
                    inserted.append((segment + 1, station, vertex_ports[(index, k)]))
            for position, _, target in sorted(inserted, key=lambda entry: entry[:2], reverse=True):
                source_vertex = entity.vertices[position - 1]
                attrs = {key: source_vertex.dxf.get(key, 0.0) for key in ("start_width", "end_width", "bulge")}
                entity.insert_vertices(position, [(target[0], target[1], 0.0)], dxfattribs=attrs)
            updated += 1
        # A third cable leaving a shared pole can collide just outside its
        # symbol even when its port is distinct. Rotate only that branch's DXF
        # port; the pole, other vertices and the distant cable route stay put.
        for pole in self._roadside_poles:
            attached = sorted({index for index, _ in links[id(pole)]})
            if len(attached) < 2:
                continue
            center = pole["target"]
            radius = KML_POLE_ICON_SIZE_METERS / 2 * float(pole["icon"].dxf.get("xscale", 1.0))
            circle = ShapelyPoint(center).buffer(radius + 1e-5)
            nearby = ShapelyPoint(center).buffer(12.0)
            def exposed(points):
                return LineString(points).intersection(nearby).difference(circle)
            def coords(index):
                return [(v.dxf.location.x, v.dxf.location.y)
                        for v in self._pole_follow_lines[index]["entity"].vertices]
            for index in attached:
                if len(associated[index]) != 1:
                    continue
                record = self._pole_follow_lines[index]
                entity = record["entity"]
                if entity.dxftype() != "POLYLINE" or entity.dxf.layer == KML_TALI_PANCING_LAYER:
                    continue
                points = coords(index)
                ends = [end for end in (0, -1)
                        if _distance(record["source"][end], pole["source"]) <= 1.25
                        and 0.3 <= _distance(points[end], center) <= radius - 0.2]
                if not ends:
                    continue
                for end in ends:
                    others = [(other, coords(other)) for other in attached if other != index
                              and self._pole_follow_lines[other]["entity"].dxftype() == "POLYLINE"]
                    if not others or all(exposed(points).distance(exposed(other_points)) >= 0.7 - 1e-4
                                         for _, other_points in others):
                        continue
                    origin = points[end]
                    port_radius = _distance(origin, center)
                    angle = math.atan2(origin[1] - center[1], origin[0] - center[0])
                    old_curve = LineString(points)
                    # Only routes within the 1.5m movement envelope can be newly crossed.
                    third_routes = [third for other in range(len(self._pole_follow_lines))
                                    if other != index and self._pole_follow_lines[other]["entity"].dxftype() == "POLYLINE"
                                    for third in [LineString(coords(other))]
                                    if old_curve.distance(third) <= 1.5 + 1e-5]
                    angles = sorted((2 * math.pi * step / 72 for step in range(72)),
                                    key=lambda test: (1 - math.cos(test - angle), test))
                    for test in angles:
                        rotated = (center[0] + port_radius * math.cos(test),
                                   center[1] + port_radius * math.sin(test))
                        if _distance(rotated, origin) > 1.5:
                            continue
                        if any(_distance(rotated, vertex) < 0.7 - 1e-4
                               for _, other_points in others for vertex in other_points
                               if _distance(vertex, center) <= radius + 1e-4):
                            continue
                        candidate = list(points)
                        candidate[end] = rotated
                        new_curve = LineString(candidate)
                        # ponytail: preserve source intersections, reject newly introduced ones.
                        if (new_curve.is_simple and
                            all(exposed(candidate).distance(exposed(other_points)) >= 0.7 - 1e-4
                                for _, other_points in others) and
                            (road is None or new_curve.intersection(road).difference(
                                old_curve.intersection(road).buffer(1e-5)).is_empty) and
                            all(new_curve.intersection(third).difference(
                                old_curve.intersection(third).buffer(1e-4)).is_empty
                                for third in third_routes)):
                            entity.vertices[end].dxf.location = (rotated[0], rotated[1], entity.vertices[end].dxf.location.z)
                            break
        # A valid port at each end is insufficient: reject any shared span that
        # still crosses or collapses between poles. Conversion cannot save it.
        for a, b, indexes in shared_runs.values():
            circles = ShapelyPoint(a["target"]).buffer(KML_POLE_ICON_SIZE_METERS / 2 + 1e-5).union(
                ShapelyPoint(b["target"]).buffer(KML_POLE_ICON_SIZE_METERS / 2 + 1e-5))
            for n, first_index in enumerate(indexes):
                first = self._pole_follow_lines[first_index]["entity"]
                first_line = LineString([(vertex.dxf.location.x, vertex.dxf.location.y)
                                         for vertex in first.vertices])
                first_run = substring(first_line,
                                      first_line.project(ShapelyPoint(ports[(id(a), first_index)])),
                                      first_line.project(ShapelyPoint(ports[(id(b), first_index)])))
                for second_index in indexes[n + 1:]:
                    second = self._pole_follow_lines[second_index]["entity"]
                    second_line = LineString([(vertex.dxf.location.x, vertex.dxf.location.y)
                                              for vertex in second.vertices])
                    second_run = substring(second_line,
                                           second_line.project(ShapelyPoint(ports[(id(a), second_index)])),
                                           second_line.project(ShapelyPoint(ports[(id(b), second_index)])))
                    if not first_run.intersection(second_run).difference(circles).is_empty:
                        message = "Shared cable crossing outside pole; no non-twisting route found"
                        if not self._cable_collision_warning(message, [self._pole_follow_lines[first_index], self._pole_follow_lines[second_index]], pipeline_logger):
                            raise ValueError(message)
        self._space_unanchored_parallel_cables()
        self._validate_new_cable_crossings(pipeline_logger)
        # Collision-only checks are advisory for Distribution/Sling; unrelated
        # line types and asset/attachment checks retain their original guards.
        records = [record for record in self._pole_follow_lines
                   if record["entity"].dxftype() == "POLYLINE"]
        rendered = [LineString([(v.dxf.location.x, v.dxf.location.y) for v in record["entity"].vertices])
                    for record in records]
        for index, first in enumerate(rendered):
            source = LineString(records[index]["source"])
            if source.is_simple and not first.is_simple:
                message = "Cable attachment introduced a self-crossing or shared return span"
                if not self._cable_collision_warning(message, [records[index]], pipeline_logger):
                    raise ValueError(message)
            for other in range(index + 1, len(rendered)):
                reference = LineString(records[other]["source"])
                if source.distance(reference) >= 0.7 and first.distance(rendered[other]) < 0.7 - 1e-4:
                    message = "ROAD pole relocation would collapse another cable"
                    if not self._cable_collision_warning(message, [records[index], records[other]], pipeline_logger):
                        raise ValueError(message)
        return updated

    def _validate_new_cable_crossings(self, pipeline_logger=None) -> None:
        """Report Distribution/Sling collisions; guard other source line types."""
        records = [record for record in self._pole_follow_lines
                   if record["entity"].dxftype() == "POLYLINE" and len(record["source"]) >= 2]
        source = [LineString(record["source"]) for record in records]
        rendered = [LineString([(v.dxf.location.x, v.dxf.location.y)
                                for v in record["entity"].vertices]) for record in records]
        tree = STRtree(rendered) if STRtree is not None and rendered else None
        for index, current in enumerate(rendered):
            neighbours = (tree.query(current) if tree is not None else range(index + 1, len(rendered)))
            for item in neighbours:
                other = int(item) if not hasattr(item, "geom_type") else next(
                    (n for n, geom in enumerate(rendered) if geom is item), -1)
                if other <= index or not current.intersects(rendered[other]):
                    continue
                crossing = current.intersection(rendered[other])
                original = source[index].intersection(source[other])
                # A source point crossing never authorizes a newly shared span.
                if crossing.length > original.length + 1e-5:
                    message = "New cable crossing outside source topology"
                    if not self._cable_collision_warning(message, [records[index], records[other]], pipeline_logger):
                        raise ValueError(message)
                    continue
                # Existing intersection locations may shift up to the pole
                # relocation cap, but one source crossing cannot license two
                # output ones.
                # ponytail: line-overlap source remains bounded by its buffer;
                # richer matching only needed for multi-component overlaps.
                if original.is_empty or not crossing.difference(
                        original.buffer(KML_ROAD_POLE_MAX_SHIFT_METERS + 0.01)).is_empty:
                    message = "New cable crossing outside source topology"
                    if not self._cable_collision_warning(message, [records[index], records[other]], pipeline_logger):
                        raise ValueError(message)
                    continue
                def points(geometry):
                    if geometry.geom_type == "Point":
                        return [geometry]
                    if hasattr(geometry, "geoms"):
                        return [point for part in geometry.geoms for point in points(part)]
                    return []
                old_points = points(original)
                new_points = points(crossing)
                if old_points and len(new_points) > len(old_points):
                    message = "New cable crossing outside source topology"
                    if not self._cable_collision_warning(message, [records[index], records[other]], pipeline_logger):
                        raise ValueError(message)

    def _space_unanchored_parallel_cables(self) -> None:
        """Unmatched native vertices stay exact; retain pre-existing source gaps."""
        # ponytail: no unowned DXF detours; explicit source edits need a new contract.

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
        slack_plan = []
        if getattr(self, "network_type", "") == "AE":
            for pending in self.pending_slack_icons:
                source = pending["projected"]
                pole = self._source_owned_asset_pole(pending)
                if pole is None or _distance(source, pole["source"]) > KML_POLE_SNAP_RADIUS_METERS:
                    raise ValueError(f"Slack hanger {pending.get('name') or 'SLACK HANGER'} has no serving pole "
                                     f"within {KML_POLE_SNAP_RADIUS_METERS:g}m of its source on the same route")
                rotation = self._serving_pole_cable_rotation(pole, pending.get("group"))
                slack_plan.append((pending, pole["target"], rotation))
        else:
            slack_plan = [(p, self._snap_to_nearest_pole(p["projected"]), None) for p in self.pending_slack_icons]
        # Validate the complete AE ownership batch before emitting any slack.
        for pending, projected, rotation in slack_plan:
            self._add_kml_slack_hanger_icon(projected, pending.get("style"), rotation=rotation)
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

    def _record_home_number_cell(self, cell: Polygon, item: dict) -> None:
        # Individual final cells retain identity even when the planner stores a mixed outer block.
        self.home_number_cells.append({
            "polygon": cell, "fat_owner": item.get("fat_owner"),
            "source": item["projected"], "text": item["text"],
            "hp_uncover": bool(item.get("hp_uncover")),
        })

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

    def _hp_cover_axis_lines(
        self, indexes: Sequence[int] | None = None, sites: Sequence[Point] | None = None,
    ) -> tuple[list[LineString], str]:
        """Prefer a local usable ROAD; otherwise use Distribution Cable as axis."""
        roads = [
            line for line in self.road_centerlines
            if line is not None and not line.is_empty and line.length > 2
        ]
        local_roads = roads
        if roads and indexes and sites is not None:
            # ROAD membership is source-local, not group-centroid based. A long
            # HP group may span a bend/corridor whose centroid is >12m from ROAD
            # even though one or more immutable HP sources have a valid nearby
            # ROAD. Centroid-only filtering demotes the whole group to CABLE.
            source_points = [
                ShapelyPoint(sites[index])
                for index in indexes
                if index is not None and 0 <= index < len(sites)
            ]
            local_roads = [
                line for line in roads
                if any(point.distance(line) <= 12.0 + 1e-6 for point in source_points)
            ]
        if local_roads:
            return local_roads, "ROAD"
        cables = [
            line for line in self.kml_cable_centerlines
            if line is not None and not line.is_empty and line.length > 2
        ]
        if cables:
            return cables, "CABLE"
        # Preserve existing safe behavior when cable is absent: the normal ROAD
        # planner still decides whether a distant route can produce legal cells.
        return roads, "ROAD"

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
        # Hard ROAD exclusion uses actual OSM footprint only. Parcel edge-touch
        # is legal; clearance is a candidate-spacing preference, not ROAD area.
        if self.road_buffers:
            try:
                area = unary_union(self.road_buffers).buffer(0)
                if not area.is_empty:
                    parts.append(area)
            except Exception:
                pass
        if self.road_lines:
            try:
                lines = unary_union(self.road_lines).buffer(2.0, cap_style=2, join_style=2)
                if not lines.is_empty:
                    parts.append(lines)
            except Exception:
                pass
        # Distribution Cable and SLING WIRE remain visible reference lines.
        # They do not clip HP COVER parcels; ROAD remains the hard exclusion.
        if not parts:
            self._home_parcel_road_solid_cache = None
            self._home_parcel_road_solid_ready = True
            return None
        solid = unary_union(parts).buffer(0)
        self._home_parcel_road_solid_cache = None if solid.is_empty else solid
        self._home_parcel_road_solid_ready = True
        return self._home_parcel_road_solid_cache

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
            cap_style=3 if hull.geom_type == "Point" else 2,
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

    def _hp_rotation_candidates(self, source):
        frame = self._nearest_road_frame(source) or self._nearest_home_number_reference_frame(source)
        base = frame["road_angle"] if frame else 0.0
        # Rotation first: sample the whole rectangle-axis range before allowing a shift.
        deltas = [0.0] + [sign * step for step in range(5, 91, 5) for sign in (-1, 1)]
        return tuple(dict.fromkeys(self._readable_angle(base + delta) for delta in deltas))

    def _hp_centered_road_clear_options(self, source, road):
        # ROAD-only zero-translation pass; local neighbour relief is a separate gate.
        if road is not None:
            point = ShapelyPoint(source)
            if road.distance(point) < 2.0 - 1e-6:
                return []
            road = road.intersection(point.buffer(10.0))
        options = []
        for width, depth in HP_COVER_PARCEL_SIZE_TIERS:
            for angle in self._hp_rotation_candidates(source):
                parcel = self._home_number_box_polygon(source, width, depth, angle)
                if road is None or not self._strict_positive_overlap(parcel, road):
                    options.append((parcel, angle))
        return options

    def _hp_same_side_relief_contexts(self, pending, centered=None):
        """Bound permission to ROAD/dense seeds plus same-owner neighbours within13m."""
        road = self._home_parcel_road_solid()
        sites = [ShapelyPoint(item["projected"]) for item in pending]
        centered = centered if centered is not None else [
            self._hp_centered_road_clear_options(item["projected"], road) for item in pending]
        seeds = {i for i, options in enumerate(centered) if not options and not pending[i].get("hp_uncover")}
        for i, point in enumerate(sites):
            for j in range(i + 1, len(sites)):
                if (pending[i].get("fat_owner") == pending[j].get("fat_owner")
                        and point.distance(sites[j]) < 4.0 - 1e-7):
                    seeds.update(k for k in (i, j) if not pending[k].get("hp_uncover"))
        # A narrow ROAD corridor can make long centred cells conflict even
        # with sources >4m apart. Discover those local same-owner seed conflicts
        # before granting neighbour relief; distant/foreign HP remain fixed.
        tree = STRtree(sites) if STRtree is not None and sites else None
        source_index = {id(point): i for i, point in enumerate(sites)}
        for i, options in enumerate(centered):
            if not options or pending[i].get("hp_uncover"):
                continue
            conflicts = set()
            for parcel, angle in options:
                neighbours = (self._query_indexed_geometries(tree, sites, parcel.buffer(2.0))
                              if tree is not None else sites)
                blockers = {source_index[id(point)] for point in neighbours
                            if source_index[id(point)] != i
                            and parcel.distance(point) < 2.0 - 1e-7}
                if not blockers:
                    break
                conflicts.update(j for j in blockers if not pending[j].get("hp_uncover")
                                 and pending[j].get("fat_owner") == pending[i].get("fat_owner")
                                 and sites[i].distance(sites[j]) <= 13.0 + 1e-7)
            else:
                if conflicts:
                    seeds.add(i)
                    seeds.update(conflicts)
        contexts = {}
        for i in sorted(seeds):
            point = sites[i]
            roads = self._road_centerline_candidates(pending[i]["projected"])
            reference = min(roads, key=lambda line: point.distance(line)) if roads else None
            frame = self._home_number_frame_from_line(pending[i]["projected"], reference) if reference is not None else None
            tangent = frame["tangent"] if frame else (1.0, 0.0)
            normal = (-tangent[1], tangent[0])
            origin = frame["nearest"] if frame else pending[i]["projected"]
            def signed(index):
                q = sites[index]
                return (q.x - origin[0]) * normal[0] + (q.y - origin[1]) * normal[1]
            own = [j for j in range(len(pending)) if not pending[j].get("hp_uncover")
                   and pending[j].get("fat_owner") == pending[i].get("fat_owner")
                   and point.distance(sites[j]) <= 13.0 + 1e-7]
            side_value = signed(i)
            if abs(side_value) <= 1e-6:
                votes = [signed(j) for j in own if j != i and abs(signed(j)) > 1e-6]
                side_value = sum(1 if v > 0 else -1 for v in votes) or 1.0
            side = 1.0 if side_value >= 0 else -1.0
            for j in own:
                if reference is not None and signed(j) * side < -1e-6:
                    continue
                context = {"seed": i, "source": pending[j]["projected"],
                           "normal": normal, "origin": origin, "side": side,
                           "road_reference": reference, "road_angle": frame["road_angle"] if frame else 0.0}
                if j not in contexts or point.distance(sites[j]) < sites[contexts[j]["seed"]].distance(sites[j]):
                    contexts[j] = context
        return contexts

    @staticmethod
    def _hp_center_stays_on_side(center, context):
        if context["road_reference"] is None:
            return True
        nx, ny = context["normal"]
        ox, oy = context["origin"]
        return ((center[0] - ox) * nx + (center[1] - oy) * ny) * context["side"] >= -1e-6

    @staticmethod
    def _hp_relief_path_clear(source, center, road):
        """Only exit the source ROAD; never enter another ROAD during relief."""
        if road is None or road.is_empty or _distance(source, center) <= 1e-6:
            return True
        path = LineString([source, center])
        interior = road.buffer(-1e-7)
        touch = path.intersection(interior)
        if touch.is_empty:
            return True
        parts = ([touch] if isinstance(touch, LineString) else
                 [part for part in getattr(touch, "geoms", ()) if isinstance(part, LineString)])
        parts = [part for part in parts if not part.is_empty and part.length > 1e-7]
        if not parts:
            return True
        if len(parts) != 1 or not interior.covers(ShapelyPoint(source)):
            return False
        return min(path.project(ShapelyPoint(parts[0].coords[0])),
                   path.project(ShapelyPoint(parts[0].coords[-1]))) <= 1e-5

    def _finalize_home_numbers_as_parcels(self, pipeline_logger=None) -> None:
        """Rotate first; same-side local relief<=8m, never cross ROAD for a free slot."""
        pending = self.pending_home_numbers
        sites = [tuple(float(value) for value in item["projected"]) for item in pending]
        if any(len(site) != 2 or not all(math.isfinite(v) for v in site) for site in sites):
            raise ValueError("HP fixed anchor has invalid coordinates")
        points = [ShapelyPoint(site) for site in sites]
        road = self._home_parcel_road_solid()
        centered = [self._hp_centered_road_clear_options(site, road) for site in sites]
        contexts = self._hp_same_side_relief_contexts(pending, centered)
        movable = set(contexts)
        tight = [(i, j) for i, point in enumerate(points) if i not in movable
                 for j in range(i + 1, len(points)) if j not in movable
                 and point.distance(points[j]) < 4.0 - 1e-7]
        if tight:
            raise ValueError(f"HP fixed-anchor density infeasible: stationary centers below 4m pairs={tight}")
        source_tree = STRtree(points) if STRtree is not None and points else None
        fixed = [p for p in self.home_number_boxes if p is not None and not p.is_empty]
        cluster = self.road_clip_geometry
        cluster = cluster.buffer(1e-6) if cluster is not None and not cluster.is_empty else None
        # Retain alternatives; one neighbour can force another HP to a smaller tier.
        plans = []
        angles_by_index = {}
        missing = []
        source_index = {id(point): i for i, point in enumerate(points)}
        for i in range(len(pending)):
            options = []
            angles_by_index[i] = {}
            def accept(parcel, angle):
                if cluster is not None and not cluster.covers(parcel):
                    return
                if any(self._strict_positive_overlap(parcel, other) for other in fixed):
                    return
                neighbours = (self._query_indexed_geometries(source_tree, points, parcel.buffer(2.0))
                              if source_tree is not None else points)
                if any(source_index[id(other)] != i and source_index[id(other)] not in movable
                       and parcel.distance(other) < 2.0 - 1e-7 for other in neighbours):
                    return
                if parcel.wkb not in angles_by_index[i]:
                    options.append({i: parcel})
                    angles_by_index[i][parcel.wkb] = angle
            # Any centred ROAD-clear tier keeps this HP immobile, even if crowded.
            for parcel, angle in centered[i]:
                accept(parcel, angle)
            if i in movable:
                context = contexts[i]
                base = context["road_angle"]
                # Normal/tangent local relief, not grid-slot assignment or a cross-road exit.
                angles = tuple(dict.fromkeys([self._readable_angle(base), self._readable_angle(base + 90)]
                                             + list(self._hp_rotation_candidates(sites[i]))))
                directions = (90., -90., 0., 180., 75., 105., -75., -105., 60., 120., -60., -120.)
                local_road = road.intersection(points[i].buffer(18.0)) if road is not None else None
                # ponytail: 16rings x12directions x2rotations, intact4x8 relief;
                # span all<=8m, then grow accepted cells without moving centers.
                for ring in range(1, 17):
                    radius = ring * 0.5
                    for width, depth in HP_COVER_PARCEL_SIZE_TIERS[-1:]:
                        for angle in angles[:2]:
                            for delta in directions:
                                theta = math.radians(base + delta)
                                center = (sites[i][0] + radius * math.cos(theta),
                                          sites[i][1] + radius * math.sin(theta))
                                if (not self._hp_center_stays_on_side(center, context)
                                        or not self._hp_relief_path_clear(sites[i], center, local_road)):
                                    continue
                                parcel = self._home_number_box_polygon(center, width, depth, angle)
                                if local_road is not None and self._strict_positive_overlap(parcel, local_road):
                                    continue
                                accept(parcel, angle)
            if not options:
                missing.append(i)
            plans.append({"indexes": [i], "options": options})
        def identities(indexes):
            return [{"index": i, "text": pending[i]["text"], "source": sites[i],
                     "class": "HP UNCOVER" if pending[i].get("hp_uncover") else "HP COVER",
                     "owner": pending[i].get("fat_owner")} for i in indexes]
        if missing:
            raise ValueError(f"HP fixed-anchor bounded angle/size options exhausted: {identities(missing)}")
        # Candidate-envelope components only; no 823-member global greedy allocator.
        envelopes = [Polygon.from_bounds(
            min(option[i].bounds[0] for option in plan["options"]),
            min(option[i].bounds[1] for option in plan["options"]),
            max(option[i].bounds[2] for option in plan["options"]),
            max(option[i].bounds[3] for option in plan["options"]),
        ) for i, plan in enumerate(plans)]
        parent = list(range(len(plans)))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        envelope_tree = STRtree(envelopes) if STRtree is not None and envelopes else None
        envelope_index = {id(poly): i for i, poly in enumerate(envelopes)}
        for i, envelope in enumerate(envelopes):
            nearby = (self._query_indexed_geometries(envelope_tree, envelopes, envelope)
                      if envelope_tree is not None else envelopes)
            for other in nearby:
                j = envelope_index[id(other)]
                if j > i and self._strict_positive_overlap(envelope, other):
                    parent[find(j)] = find(i)
        components = defaultdict(list)
        for i in range(len(plans)):
            components[find(i)].append(i)
        selected = {}
        for indexes in components.values():
            result = self._select_strict_strip_candidates([plans[i] for i in indexes], base_obstacles=fixed)
            if result is None:
                # ponytail: finite angles and native 500000-node ceiling;
                # exhaustion is not proof that continuous-angle geometry is impossible.
                raise ValueError(f"HP fixed-anchor bounded candidate search exhausted: {identities(indexes)}")
            selected.update(result)
        rotations = {i: angles_by_index[i][parcel.wkb] for i, parcel in selected.items()}
        centers = {i: (parcel.centroid.x, parcel.centroid.y) for i, parcel in selected.items()}
        # Centred13x13 cells have radius<9.2m: only centres<=19m can overlap.
        center_ids = list(selected)
        center_points = [ShapelyPoint(centers[i]) for i in center_ids]
        center_tree = STRtree(center_points) if STRtree is not None and center_points else None
        center_index = {id(point): i for i, point in zip(center_ids, center_points)}
        fixed_tree = STRtree(fixed) if STRtree is not None and fixed else None
        # Grow symmetrically only; keep selected center/angle and all hard gates.
        for i in sorted(selected):
            corners = list(selected[i].exterior.coords)
            width, depth = (round(math.dist(corners[j], corners[j + 1]), 6) for j in (0, 1))
            probe = ShapelyPoint(centers[i]).buffer(19.0)
            nearby = (self._query_indexed_geometries(center_tree, center_points, probe)
                      if center_tree is not None else center_points)
            neighbours_ids = [center_index[id(point)] for point in nearby if center_index[id(point)] != i]
            local_fixed = (self._query_indexed_geometries(fixed_tree, fixed, probe)
                           if fixed_tree is not None else fixed)
            local_road = road.intersection(probe) if road is not None else None
            dimensions = [(w, d) for w in dict.fromkeys([width + .5 * n for n in range(27)
                                                        if width + .5 * n < 13.0] + [13.0])
                          for d in dict.fromkeys([depth + .5 * n for n in range(27)
                                                 if depth + .5 * n < 13.0] + [13.0])]
            others = local_fixed + [selected[j] for j in neighbours_ids]
            for w, d in sorted(dimensions, key=lambda size: (-size[0] * size[1], -size[1], -size[0])):
                parcel = self._home_number_box_polygon(centers[i], w, d, rotations[i])
                if local_road is not None and self._strict_positive_overlap(parcel, local_road):
                    continue
                if cluster is not None and not cluster.covers(parcel):
                    continue
                if any(self._strict_positive_overlap(parcel, other) for other in others):
                    continue
                neighbours = (self._query_indexed_geometries(source_tree, points, parcel.buffer(1e-7))
                              if source_tree is not None else points)
                if any(other is not points[i] and parcel.buffer(1e-7).covers(other) for other in neighbours):
                    continue
                selected[i] = parcel
                break
        self._last_label_only_indexes = set()
        self._last_relaxed_4x8_indexes = set()
        self._hp_cover_drawn_indexes = set()
        self._commit_strict_strip_parcels(
            list(range(len(pending))), selected, points, pending,
            {"layer": KML_HOME_NUMBER_BOX_LAYER, "color": KML_HOME_NUMBER_BOX_COLOR},
            {"layer": KML_HOME_NUMBER_LAYER, "color": KML_HOME_NUMBER_COLOR, "style": "EVA_BOLD"},
            rotation_by_index=rotations, centers_by_index=centers,
        )
        if self._hp_cover_drawn_indexes != set(range(len(pending))):
            raise ValueError("HP fixed-anchor source/cell/label accounting failed")
        pipeline_log(pipeline_logger, "success",
                     f"[3/6] HP rotate-first: {len(pending)} cells; "
                     f"same-side shifted={sum(ShapelyPoint(centers[i]).distance(points[i]) > 1e-6 for i in centers)}, cap=8m.")

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
        # A shared strip may not bridge a source gap beyond 13m parcel cap.
        gap = 13.0
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
        # Run-2 may relocate only HPs that failed normal corridor layout.
        # Search nearest valid empty slot first; original source remains intact
        # in KMZ and normal HPs are never moved.
        offsets = [(0.0, 0.0)]
        for radius in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0):
            for degrees in range(0, 360, 20):
                theta = math.radians(degrees)
                offsets.append((radius * math.cos(theta), radius * math.sin(theta)))
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
                    # Try intact modules, largest first. Never intersect or
                    # carve a parcel into fragments.
                    allowed = fat
                    if road_solid is not None and not road_solid.is_empty:
                        allowed = allowed.difference(road_solid)
                    if self.home_number_boxes:
                        allowed = allowed.difference(unary_union(self.home_number_boxes))
                    allowed = allowed.buffer(0)
                    retry_angle = self._nearest_road_angle((candidate_anchor.x, candidate_anchor.y))
                    if retry_angle is None:
                        retry_angle = self._polygon_label_rotation(fat)
                    parcel = self._fit_compact_parcel_ladder(candidate_anchor, allowed, retry_angle)
                    if parcel is None:
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
                        self.home_number_box_uncover.append(bool(pending[idx].get("hp_uncover")))
                        self._record_home_number_cell(poly, pending[idx])
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

    def _solve_hp_block_axis(self, indexes, sites, site_points, clip_region, base_angle):
        """Choose one shared axis with the best collision-free compact layout."""
        if not indexes or clip_region is None or clip_region.is_empty:
            return base_angle
        road_solid=self._home_parcel_road_solid()
        occupied=[box for box in self.home_number_boxes if box is not None and not box.is_empty]
        best=None
        for delta in (0.0,-3.0,3.0,-6.0,6.0,-9.0,9.0,-12.0,12.0,-15.0,15.0):
            angle=self._readable_angle(base_angle+delta)
            cells=self._hp_row_grid_parcels(indexes,sites,[],clip_region,angle)
            trial=[]; count=0; area=0.0
            for idx in indexes:
                site=site_points[idx]; allowed=cells.get(idx)
                if allowed is None or allowed.is_empty:
                    continue
                if road_solid is not None and not road_solid.is_empty:
                    allowed=allowed.difference(road_solid)
                if occupied:
                    allowed=allowed.difference(unary_union(occupied))
                others=[site_points[j].buffer(3.0) for j in indexes if j != idx]
                if others:
                    allowed=allowed.difference(unary_union(others))
                if trial:
                    allowed=allowed.difference(unary_union(trial))
                parcel=self._fit_compact_rect(site,allowed,angle)
                if parcel is None or parcel.is_empty or parcel.area<24.0:
                    continue
                trial.append(parcel); count+=1; area+=parcel.area
            # Max placed then max usable area then minimum angular deviation.
            score=(count,round(area,3),-abs(delta))
            if best is None or score>best[0]: best=(score,angle)
        return best[1] if best is not None else base_angle

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

        # Strict final topology uses ROAD-side planar slot allocation. Cells are
        # created before source assignment, so local planner boxes cannot overlap.
        if not _subblocks_split:
            strip_parcels, rotation_by_index, _segment_by_index = self._strict_segmented_hp_plan(
                indexes,
                sites,
                clip_region,
                fill_region,
                base_obstacles=list(self.home_number_boxes),
                pipeline_logger=pipeline_logger,
            )
            return self._commit_strict_strip_parcels(
                indexes, strip_parcels, site_points, pending, box_attrs, text_attrs,
                rotation_by_index=rotation_by_index,
            )

        # Legacy local strip route remains only for explicit recursive calls.
        if not _subblocks_split:
            local_blocks = self._split_hp_parcel_subblocks(indexes, sites)
            roads = [line for line in self.road_centerlines if line is not None and not line.is_empty and line.length > 2]
            subblocks = []
            for local_block in local_blocks:
                corridor_groups = defaultdict(list)
                for index in local_block:
                    point = ShapelyPoint(sites[index])
                    if not roads:
                        corridor_groups[(None, 0, 0)].append(index)
                        continue
                    road_index, road = min(enumerate(roads), key=lambda item: point.distance(item[1]))
                    distance = road.project(point)
                    epsilon = min(2.0, max(.25, road.length * .01))
                    point_a = road.interpolate(max(0.0, distance - epsilon))
                    point_b = road.interpolate(min(road.length, distance + epsilon))
                    dx, dy = point_b.x - point_a.x, point_b.y - point_a.y
                    near = road.interpolate(distance)
                    signed = (point.x - near.x) * (-dy) + (point.y - near.y) * dx
                    # Keep one local ROAD segment + side together. Splitting by
                    # each source tangent fragments one physical row at curves;
                    # continuous projection runs below provide the real 13m cap.
                    corridor_groups[(road_index, 1 if signed >= 0 else -1)].append(index)
                # Same tangent bucket may recur far apart on one long curved
                # OSM LineString. A shared strip cannot bridge a ROAD-projection
                # gap above the 13m parcel cap, so split continuous local runs.
                for corridor_key, members in corridor_groups.items():
                    road_index = corridor_key[0]
                    if road_index is None or len(members) < 2:
                        subblocks.append(members)
                        continue
                    road = roads[road_index]
                    ordered = sorted(members, key=lambda item: (road.project(ShapelyPoint(sites[item])), item))

                    def _local_tangent(item):
                        projection = road.project(ShapelyPoint(sites[item]))
                        epsilon = min(2.0, max(.25, road.length * .01))
                        point_a = road.interpolate(max(0.0, projection - epsilon))
                        point_b = road.interpolate(min(road.length, projection + epsilon))
                        return math.degrees(math.atan2(point_b.y - point_a.y, point_b.x - point_a.x))

                    def _axis_delta(left, right):
                        return abs(((right - left + 90.0) % 180.0) - 90.0)

                    run = [ordered[0]]
                    previous_projection = road.project(ShapelyPoint(sites[ordered[0]]))
                    previous_tangent = _local_tangent(ordered[0])
                    for item in ordered[1:]:
                        projection = road.project(ShapelyPoint(sites[item]))
                        tangent = _local_tangent(item)
                        # Split only a real bend/junction. Small tangent jitter on
                        # one physical row stays together and shares one axis.
                        if projection - previous_projection > 13.0 or _axis_delta(previous_tangent, tangent) > 35.0:
                            subblocks.append(run)
                            run = []
                        run.append(item)
                        previous_projection = projection
                        previous_tangent = tangent
                    if run:
                        subblocks.append(run)
            if len(subblocks) > 1:
                if pipeline_logger is not None:
                    pipeline_log(
                        pipeline_logger, "info",
                        f"[3/6] HP COVER global ROAD planning: {len(subblocks)} local corridor blocks.",
                    )
                # Initial one-candidate planning is linear. Only local blocks
                # whose first cells overlap are expanded and solved together.
                strip_parcels, rotation_by_index = self._strict_global_hp_plan(
                    subblocks,
                    sites,
                    clip_region,
                    fill_region,
                    base_obstacles=list(self.home_number_boxes),
                    pipeline_logger=pipeline_logger,
                )
                all_indexes = [index for block in subblocks for index in block]
                return self._commit_strict_strip_parcels(
                    all_indexes, strip_parcels, site_points, pending, box_attrs, text_attrs,
                    rotation_by_index=rotation_by_index,
                )
            # One local corridor: skip legacy Voronoi/compact route entirely.
            indexes = subblocks[0] if subblocks else indexes
            _subblocks_split = True

        if _subblocks_split:
            _angle, _options, _ai_hint = self._strict_ai_strip_options(
                indexes, sites, clip_region, fill_region,
                pipeline_logger=pipeline_logger, base_obstacles=list(self.home_number_boxes),
                candidate_limit=1,
            )
            if not _options:
                raise ValueError(
                    f"HP COVER shared ROAD strip tidak lengkap: 0/{len(indexes)} parcel valid; "
                    "Tidak membuat compact/label-only fallback."
                )
            return self._commit_strict_strip_parcels(
                indexes, _options[0], site_points, pending, box_attrs, text_attrs,
                rotation_by_index={index: _angle for index in indexes},
            )

        # Road-first allocation. Front-row HPs establish a clean parcel row;
        # interior HPs then follow that same local axis/spacing.
        if len(indexes) > 1:
            _roads = [line for line in self.road_centerlines if line is not None and not line.is_empty and line.length > 2]
            _stable_angle = self._polygon_label_rotation(clip_region)
            _road = None
            if _roads:
                _center = MultiPoint([ShapelyPoint(sites[i]) for i in indexes]).centroid
                _road = min(_roads, key=lambda line: line.distance(_center))
                if _road.distance(_center) <= 24.0:
                    _at = _road.project(_center)
                    _eps = min(2.0, max(.25, _road.length * .01))
                    _a = _road.interpolate(max(0.0, _at-_eps)); _b = _road.interpolate(min(_road.length, _at+_eps))
                    _dx, _dy = _b.x-_a.x, _b.y-_a.y
                    if _dx*_dx+_dy*_dy > 1e-9:
                        _stable_angle = math.degrees(math.atan2(_dy, _dx))
            _radians = math.radians(_stable_angle)
            _tx, _ty = math.cos(_radians), math.sin(_radians)
            def _road_first_key(i):
                _point = ShapelyPoint(sites[i])
                _along = _road.project(_point) if _road is not None else sites[i][0]*_tx+sites[i][1]*_ty
                _near = _road.distance(_point) if _road is not None else 9999.0
                return (0 if _near <= 18.0 else 1, round(_along, 3), round(_near, 3), i)
            indexes = sorted(indexes, key=_road_first_key)
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
            # Use ROAD tangent at source HP block, never a large clip centroid.
            reference = MultiPoint([ShapelyPoint(sites[i]) for i in indexes]).centroid
            local_roads = [
                line for line in self.road_centerlines
                if line is not None and not line.is_empty and line.length > 2
            ]
            nearest_road = min(local_roads, key=lambda line: line.distance(reference))
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
        deterministic_angle = region_angle
        ai_orientation = None
        # Visual AI may refine one shared axis for the whole local sub-block.
        # Guardrail: never accept a wild turn more than 15 degrees away from
        # the road/FAT deterministic reference.
        try:
            deterministic_candidate = self._road_strip_cells(
                indexes, sites, clip_region, deterministic_angle, {}, require_clip=bool(fill_region)
            )
            preview = self._hp_visual_parcel_png(
                clip_region,
                [site_points[i] for i in indexes],
                candidate_parcels=list(deterministic_candidate.values()),
                failed_points=[site_points[i] for i in indexes if i not in deterministic_candidate],
            )
            ai_hint = suggest_dxf_visual_parcel_layout(
                preview_png=preview,
                fat_angle=deterministic_angle,
                road_angle=deterministic_angle,
                point_count=len(group_sites),
            )
            if ai_hint:
                candidate = self._readable_angle(float(ai_hint["angle_degrees"]))
                delta = abs(((candidate - deterministic_angle + 90.0) % 180.0) - 90.0)
                if delta <= 15.0:
                    region_angle = candidate
                    ai_orientation = ai_hint
                    if pipeline_logger is not None:
                        pipeline_log(
                            pipeline_logger,
                            "info",
                            "[3/6] HP COVER AI visual hint accepted: "
                            f"axis {candidate:.1f}°, confidence {float(ai_hint['confidence']):.2f}, "
                            f"road side {int(ai_hint['road_side'])}, "
                            f"scale {float(ai_hint['width_scale']):.2f}×{float(ai_hint['depth_scale']):.2f}, "
                            f"shift along {float(ai_hint.get('along_shift_m', 0.0)):.1f}m / normal {float(ai_hint.get('normal_shift_m', 0.0)):.1f}m.",
                        )
                elif pipeline_logger is not None:
                    pipeline_log(
                        pipeline_logger,
                        "info",
                        f"[3/6] HP COVER AI visual hint ignored: axis delta {delta:.1f}° exceeds 15° guard.",
                    )
        except Exception as exc:
            LOGGER.debug("HP COVER constrained AI axis fallback: %s", exc)
        # Strict HP COVER has one renderer: complete shared ROAD strip. AI may
        # refine one group axis, but never reroutes through compact/open-grid.
        if not any(line is not None and not line.is_empty and line.length > 2 for line in self.road_centerlines):
            raise ValueError("HP COVER memerlukan ROAD OSM; pilih wilayah/kecamatan dengan source ROAD aktif.")
        ai_layout_hint = ai_orientation or {}
        if pipeline_logger is not None:
            mode = "AI-assisted" if ai_orientation else "deterministic"
            pipeline_log(pipeline_logger, "info", f"[3/6] HP COVER {mode} shared ROAD axis {region_angle:.1f}°.")
        if clip_region is None or clip_region.is_empty:
            clip_region = MultiPoint([ShapelyPoint(sites[i]) for i in indexes]).buffer(18.0, cap_style=3, join_style=2)
        strip_parcels = self._road_strip_cells(
            indexes, sites, clip_region, region_angle, ai_layout_hint, require_clip=bool(fill_region)
        )
        missing = [index for index in indexes if index not in strip_parcels]
        if missing:
            raise ValueError(
                f"HP COVER shared ROAD strip tidak lengkap: {len(strip_parcels)}/{len(indexes)} parcel valid; "
                f"{len(missing)} titik gagal. Tidak membuat compact/label-only fallback."
            )
        drawn = 0
        for global_idx in indexes:
            parcel = strip_parcels[global_idx]
            site = site_points[global_idx]
            item = pending[global_idx]
            road_solid = self._home_parcel_road_solid()
            if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                raise ValueError(f"HP COVER parcel {global_idx} overlap ROAD after strip generation.")
            if not parcel.buffer(1e-6).covers(site):
                road_lines = [
                    line for line in getattr(
                        self, "_last_hp_relocation_roads", self.road_centerlines
                    )
                    if line is not None and not line.is_empty
                ]
                max_relocation = self._strict_relocation_limit(site, road_lines)
                if parcel.distance(site) > max_relocation + 1e-6:
                    raise ValueError(
                        f"HP COVER parcel {global_idx} melewati batas relokasi {max_relocation:g}m."
                    )
            if any(parcel.intersection(existing).area > 1e-7 for existing in self.home_number_boxes):
                raise ValueError(f"HP COVER parcel {global_idx} positive-area overlap.")
            exterior = list(parcel.exterior.coords)
            output_start_index = len(self.output_entities)
            self._add_polyline(exterior, close=True, dxfattribs=box_attrs)
            self.home_number_boxes.append(parcel)
            self.home_number_box_uncover.append(bool(item.get("hp_uncover")))
            self._record_home_number_cell(parcel, item)
            anchor = parcel.representative_point()
            self._add_centered_text(
                str(item["text"]), (anchor.x, anchor.y), KML_HOME_NUMBER_HEIGHT_METERS,
                dxfattribs=text_attrs, rotation=self._polygon_label_rotation(parcel),
            )
            self.register_fdt_entities(item.get("fdt_number"), self.output_entities[output_start_index:])
            self._hp_cover_drawn_indexes.add(global_idx)
            drawn += 1
        return drawn

    def _hp_visual_parcel_png(self, region, points, candidate_parcels=None, failed_points=None):
        """PNG review: cyan region, red ROAD, green candidate cells, fixed HP points."""
        if region is None or region.is_empty:
            return b""
        minx, miny, maxx, maxy = region.bounds
        width, height = max(maxx - minx, 1.0), max(maxy - miny, 1.0)
        pad, canvas = 16, 512
        scale = min((canvas - 2 * pad) / width, (canvas - 2 * pad) / height)
        draw_w = max(1, int(width * scale + 2 * pad))
        draw_h = max(1, int(height * scale + 2 * pad))
        image = Image.new("RGB", (draw_w, draw_h), "#111827")
        draw = ImageDraw.Draw(image, "RGBA")

        def xy(x, y):
            return (pad + (x - minx) * scale, pad + (maxy - y) * scale)

        for polygon in self._geometry_polygons(region):
            coords = [xy(x, y) for x, y in polygon.exterior.coords]
            draw.polygon(coords, fill=(0, 217, 255, 28), outline=(0, 217, 255, 255), width=2)
        roads = self._home_parcel_road_solid()
        if roads is not None and not roads.is_empty:
            for polygon in self._geometry_polygons(roads.intersection(region.envelope)):
                coords = [xy(x, y) for x, y in polygon.exterior.coords]
                draw.polygon(coords, fill=(255, 51, 51, 92), outline=(255, 51, 51, 255), width=2)
        for parcel in candidate_parcels or []:
            if parcel is None or parcel.is_empty:
                continue
            for polygon in self._geometry_polygons(parcel):
                coords = [xy(x, y) for x, y in polygon.exterior.coords]
                draw.polygon(coords, fill=(34, 197, 94, 48), outline=(74, 222, 128, 255), width=2)
        radius = 4
        failed = {(round(point.x, 4), round(point.y, 4)) for point in (failed_points or [])}
        for point in points:
            x, y = xy(point.x, point.y)
            color = (255, 138, 31, 255) if (round(point.x, 4), round(point.y, 4)) in failed else (255, 230, 0, 255)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=(17, 17, 17, 255), width=1)
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    def _parcel_for_strip_or_fallback(self, strip_parcel, site, clip_region, angle):
        """Keep accepted strip geometry intact; compact fit is fallback only."""
        if strip_parcel is not None and not strip_parcel.is_empty:
            return strip_parcel
        if clip_region is None or clip_region.is_empty:
            return None
        allowed = clip_region
        road = self._home_parcel_road_solid()
        if road is not None and not road.is_empty:
            allowed = allowed.difference(road)
        return self._fit_compact_rect(site, allowed, angle)

    @staticmethod
    def _packed_axis_centers(values, minimum_gap):
        """Least-squares monotone packing with a fixed minimum spacing."""
        if not values:
            return []
        ordered = [float(value) for value in values]
        adjusted = [value - index * minimum_gap for index, value in enumerate(ordered)]
        blocks = []
        for index, value in enumerate(adjusted):
            blocks.append([index, index, value, 1])
            while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
                right = blocks.pop(); left = blocks.pop()
                count = left[3] + right[3]
                mean = (left[2] * left[3] + right[2] * right[3]) / count
                blocks.append([left[0], right[1], mean, count])
        fitted = [0.0] * len(ordered)
        for start, end, mean, _count in blocks:
            for index in range(start, end + 1):
                fitted[index] = mean + index * minimum_gap
        return fitted

    def _open_ai_grid_cells(self, indexes, sites, angle, layout_hint=None):
        """No-ROAD/no-FAT fallback: packed shared-edge grid, DXF relocation <=12m."""
        if not indexes:
            return {}
        hint = layout_hint or {}
        radians = math.radians(float(angle))
        tx, ty = math.cos(radians), math.sin(radians)
        nx, ny = -ty, tx
        along_shift = max(-12.0, min(12.0, float(hint.get('along_shift_m', 0.0) or 0.0)))
        normal_shift = max(-12.0, min(12.0, float(hint.get('normal_shift_m', 0.0) or 0.0)))
        points = []
        for index in indexes:
            point = ShapelyPoint(sites[index])
            points.append((index, point, point.x * tx + point.y * ty, point.x * nx + point.y * ny))
        points.sort(key=lambda item: (item[3], item[2]))

        # Natural rows remain source-driven; only DXF parcel/label may move.
        rows = []
        for item in points:
            if not rows or abs(item[3] - rows[-1][0]) > 7.5:
                rows.append([item[3], [item]])
            else:
                rows[-1][1].append(item)
                rows[-1][0] = sum(entry[3] for entry in rows[-1][1]) / len(rows[-1][1])
        desired_rows = [row[0] for row in rows]
        packed_rows = self._packed_axis_centers(desired_rows, 10.0)
        packed_rows = [value + normal_shift for value in packed_rows]

        result = {}
        placed = []
        road_solid = self._home_parcel_road_solid()
        for row_index, (_desired_v, entries) in enumerate(rows):
            entries.sort(key=lambda item: item[2])
            desired_u = [entry[2] for entry in entries]
            centers_u = [value + along_shift for value in self._packed_axis_centers(desired_u, 4.0)]
            center_v = packed_rows[row_index]
            lower_mid = (packed_rows[row_index - 1] + center_v) / 2.0 if row_index else center_v - 6.5
            upper_mid = (center_v + packed_rows[row_index + 1]) / 2.0 if row_index < len(rows) - 1 else center_v + 6.5
            lower = max(lower_mid, center_v - 6.5)
            upper = min(upper_mid, center_v + 6.5)
            if upper - lower < 10.0:
                continue
            for column, entry in enumerate(entries):
                index, source = entry[0], entry[1]
                center_u = centers_u[column]
                left_mid = (centers_u[column - 1] + center_u) / 2.0 if column else center_u - 6.5
                right_mid = (center_u + centers_u[column + 1]) / 2.0 if column < len(entries) - 1 else center_u + 6.5
                left = max(left_mid, center_u - 6.5)
                right = min(right_mid, center_u + 6.5)
                if right - left < 4.0:
                    continue
                def xy(u, v):
                    return (tx * u + nx * v, ty * u + ny * v)
                parcel = Polygon([xy(left, lower), xy(right, lower), xy(right, upper), xy(left, upper)])
                if parcel.is_empty or parcel.area < 40.0 or parcel.area > 169.0 + 1e-6:
                    continue
                if parcel.distance(source) > self._strict_relocation_limit(source, roads) + 1e-6:
                    continue
                if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                    continue
                if any(parcel.intersection(old).area > 1e-7 for old in placed):
                    continue
                placed.append(parcel)
                result[index] = parcel
        return result

    def _strict_strip_options(
        self, indexes, sites, clip_region, fill_region, base_obstacles=None,
        candidate_limit=48, angle_override=None, layout_hint=None, dense_repack=False,
    ):
        """Return complete, static-valid shared-ROAD strip candidates for one corridor.

        Does not reserve other in-flight corridors. The global selector owns that
        decision, preventing input-order-dependent HP dead letters.
        """
        roads = [line for line in self.road_centerlines if line is not None and not line.is_empty and line.length > 2]
        if not roads:
            raise ValueError("HP COVER memerlukan ROAD OSM; pilih wilayah/kecamatan dengan source ROAD aktif.")
        reference = MultiPoint([ShapelyPoint(sites[index]) for index in indexes]).centroid
        road = min(roads, key=lambda line: line.distance(reference))
        angle = self._polygon_label_rotation(clip_region) if clip_region is not None and not clip_region.is_empty else 0.0
        if road.distance(reference) <= 24.0:
            position = road.project(reference)
            epsilon = min(2.0, max(.25, road.length * .01))
            point_a = road.interpolate(max(0.0, position - epsilon))
            point_b = road.interpolate(min(road.length, position + epsilon))
            dx, dy = point_b.x - point_a.x, point_b.y - point_a.y
            if dx * dx + dy * dy > 1e-9:
                angle = math.degrees(math.atan2(dy, dx))
        if angle_override is not None:
            try:
                angle = float(angle_override)
            except (TypeError, ValueError):
                pass
        angle = self._readable_angle(angle)
        base_hint = dict(layout_hint or {})
        # Junction rows need width choices. Every candidate remains one full,
        # shared-edge strip: 4–13m width, 10–13m depth, no per-HP fallback.
        options = []
        seen = set()

        def collect(width_scale, road_side=None):
            hint = dict(base_hint)
            try:
                preferred_width = float(hint.get("width_scale", 1.0) or 1.0)
            except (TypeError, ValueError):
                preferred_width = 1.0
            hint["width_scale"] = max(4.0 / 13.0, min(1.0, preferred_width * width_scale))
            if road_side is not None:
                hint["road_side"] = road_side
            for option in self._road_strip_cells(
                indexes, sites, clip_region, angle, hint,
                require_clip=bool(fill_region), obstacles=base_obstacles or [],
                candidate_limit=candidate_limit, return_candidates=True,
                dense_repack=dense_repack,
            ):
                key = tuple((index, option[index].wkb) for index in sorted(option))
                if key not in seen:
                    seen.add(key)
                    options.append(option)

        # Source-spacing width first. Initial global planning needs only one
        # deterministic candidate. Extra widths are generated only for actual
        # collision components, never for every singleton corridor.
        for width_scale in (1.0, 10.0 / 13.0, 8.0 / 13.0, 6.0 / 13.0, 4.0 / 13.0):
            collect(width_scale)
            # Candidate budget is total per local corridor, never per width scale.
            if options and (candidate_limit <= 1 or len(options) >= candidate_limit):
                break

        # Only if source-side strips are impossible, test the opposite complete
        # ROAD side. This handles raw HP points inside the ROAD footprint.
        if not options:
            for road_side in (1.0, -1.0):
                for width_scale in (1.0, 10.0 / 13.0, 8.0 / 13.0, 6.0 / 13.0, 4.0 / 13.0):
                    collect(width_scale, road_side)
                    if len(options) >= max(1, int(candidate_limit)):
                        break
                if options:
                    break
        return angle, options

    def _strict_ai_strip_options(
        self, indexes, sites, clip_region, fill_region, pipeline_logger=None,
        base_obstacles=None, candidate_limit=48, dense_repack=False,
    ):
        """Return one bounded AI-refined shared axis, else deterministic strips.

        AI sees a PNG only and returns no geometry. Converter applies the accepted
        block hint to every candidate cell; commit reuses that exact angle for all
        separators and HOME_NUMBER text in the local block.
        """
        deterministic_angle, deterministic_options = self._strict_strip_options(
            indexes, sites, clip_region, fill_region,
            base_obstacles=base_obstacles, candidate_limit=candidate_limit,
            dense_repack=dense_repack,
        )
        if not deterministic_options:
            return deterministic_angle, deterministic_options, None
        try:
            preview = self._hp_visual_parcel_png(
                clip_region,
                [ShapelyPoint(sites[index]) for index in indexes],
                candidate_parcels=list(deterministic_options[0].values()),
            )
            hint = suggest_dxf_visual_parcel_layout(
                preview_png=preview,
                fat_angle=deterministic_angle,
                road_angle=deterministic_angle,
                point_count=len(indexes),
            )
            if not hint:
                return deterministic_angle, deterministic_options, None
            angle = self._readable_angle(float(hint["angle_degrees"]))
            delta = abs(((angle - deterministic_angle + 90.0) % 180.0) - 90.0)
            if delta > 15.0:
                if pipeline_logger is not None:
                    pipeline_log(
                        pipeline_logger, "info",
                        f"[3/6] HP COVER AI visual hint ignored: axis delta {delta:.1f}° exceeds 15° guard.",
                    )
                return deterministic_angle, deterministic_options, None
            _angle, options = self._strict_strip_options(
                indexes, sites, clip_region, fill_region,
                base_obstacles=base_obstacles, candidate_limit=candidate_limit,
                angle_override=angle, layout_hint=hint, dense_repack=dense_repack,
            )
            if not options:
                return deterministic_angle, deterministic_options, None
            if pipeline_logger is not None:
                pipeline_log(
                    pipeline_logger, "info",
                    "[3/6] HP COVER AI visual hint accepted: "
                    f"axis {angle:.1f}°, confidence {float(hint['confidence']):.2f}, "
                    f"road side {int(hint['road_side'])}, "
                    f"scale {float(hint['width_scale']):.2f}×{float(hint['depth_scale']):.2f}.",
                )
            return angle, options, hint
        except Exception as exc:
            LOGGER.debug("HP COVER strict AI axis fallback: %s", exc)
            return deterministic_angle, deterministic_options, None

    @staticmethod
    def _strict_positive_overlap(left, right):
        # Geometry planner accepts Shapely geometries only.
        if isinstance(left, dict) or isinstance(right, dict):
            return False
        return left.intersection(right).area > 1e-7

    @staticmethod
    def _strict_relocation_limit(source, roads, frontage=None):
        """Return the active source-to-cell limit for one local ROAD segment."""
        road_lines = [line for line in (roads or []) if line is not None and not line.is_empty]
        road_dist = min((source.distance(line) for line in road_lines), default=0.0)
        if frontage is not None and frontage < 3.0:
            return 30.0
        return 18.0 if road_dist > 15.0 else 15.0 if road_dist > 12.0 else 12.0

    @staticmethod
    def _strict_parcel_short_axis(parcel):
        """Return stable local axes and bounds for one rectangular strict cell."""
        if parcel is None or parcel.is_empty:
            return None
        try:
            corners = list(parcel.minimum_rotated_rectangle.exterior.coords)[:-1]
        except Exception:
            return None
        if len(corners) != 4:
            return None
        edges = []
        for left, right in zip(corners, corners[1:] + corners[:1]):
            dx, dy = right[0] - left[0], right[1] - left[1]
            length = math.hypot(dx, dy)
            if length > 1e-8:
                edges.append((length, dx / length, dy / length))
        if len(edges) != 4:
            return None
        _long, tx, ty = max(edges, key=lambda item: item[0])
        nx, ny = -ty, tx
        projected_u = [x * tx + y * ty for x, y in corners]
        projected_v = [x * nx + y * ny for x, y in corners]
        return tx, ty, nx, ny, min(projected_u), max(projected_u), min(projected_v), max(projected_v)

    @staticmethod
    def _strict_axis_rectangle(tx, ty, nx, ny, min_u, max_u, min_v, max_v):
        return Polygon([
            (tx * min_u + nx * min_v, ty * min_u + ny * min_v),
            (tx * max_u + nx * min_v, ty * max_u + ny * min_v),
            (tx * max_u + nx * max_v, ty * max_u + ny * max_v),
            (tx * min_u + nx * max_v, ty * min_u + ny * max_v),
        ])

    def _expand_strict_hp_parcels_into_free_space(
        self, selected, sites, clip_region, fill_region, base_obstacles=None,
    ):
        """Widen only exposed 4m strip ends into verified empty space.

        The strict planner remains the geometry authority. This post-pass never
        moves an anchor, never rotates a cell, and only replaces a 4×10 cell
        with a 6×10 candidate when it is disjoint from ROAD, all original cells,
        other raw HP points, fixed obstacles, and a real FAT clip. Interior
        shared dividers stay frozen; only a legal outer left/right edge grows.
        """
        if not selected:
            return selected
        road_solid = self._home_parcel_road_solid()
        clip_cover = (
            clip_region.buffer(1e-6)
            if fill_region and clip_region is not None and not clip_region.is_empty
            else None
        )
        fixed = [item for item in (base_obstacles or []) if item is not None and not item.is_empty]
        originals = {index: parcel for index, parcel in selected.items() if parcel is not None and not parcel.is_empty}
        source_points = {index: ShapelyPoint(sites[index]) for index in originals}
        # Spatial index keeps this visual-only pass local. Do not union then
        # difference every candidate: that turns a 350-HP run into repeated
        # global overlay operations.
        original_geometries = list(originals.values())
        original_tree = STRtree(original_geometries) if STRtree is not None and original_geometries else None
        fixed_tree = STRtree(fixed) if STRtree is not None and fixed else None
        source_geometries = list(source_points.values())
        source_tree = STRtree(source_geometries) if STRtree is not None and source_geometries else None
        candidates = []
        target_width = 6.0
        for index, parcel in originals.items():
            axis = self._strict_parcel_short_axis(parcel)
            if axis is None:
                continue
            tx, ty, nx, ny, min_u, max_u, min_v, max_v = axis
            current_width = max_v - min_v
            if current_width >= target_width - 1e-6 or current_width < 4.0 - 1e-5:
                continue
            grow = min(target_width, 13.0) - current_width
            # Prefer symmetric growth, then a complete legal outer-side growth.
            variants = (
                (min_v - grow / 2.0, max_v + grow / 2.0),
                (min_v - grow, max_v),
                (min_v, max_v + grow),
            )
            for candidate_min_v, candidate_max_v in variants:
                candidate = self._strict_axis_rectangle(
                    tx, ty, nx, ny, min_u, max_u, candidate_min_v, candidate_max_v,
                )
                if candidate.is_empty or not candidate.is_valid:
                    continue
                if road_solid is not None and not road_solid.is_empty and candidate.intersection(road_solid).area > 1e-7:
                    continue
                if clip_cover is not None and not clip_cover.covers(candidate):
                    continue
                nearby_originals = (
                    self._query_indexed_geometries(original_tree, original_geometries, candidate)
                    if original_tree is not None else original_geometries
                )
                if any(other is not parcel and self._strict_positive_overlap(candidate, other) for other in nearby_originals):
                    continue
                nearby_fixed = (
                    self._query_indexed_geometries(fixed_tree, fixed, candidate)
                    if fixed_tree is not None else fixed
                )
                if any(self._strict_positive_overlap(candidate, other) for other in nearby_fixed):
                    continue
                nearby_points = (
                    self._query_indexed_geometries(source_tree, source_geometries, candidate.buffer(1e-6))
                    if source_tree is not None else source_geometries
                )
                if any(other_point is not source_points[index] and candidate.buffer(1e-6).covers(other_point) for other_point in nearby_points):
                    continue
                candidates.append((index, candidate, candidate.area - parcel.area))
        # Candidates only compete for newly claimed free space. Original cells
        # are immutable obstacles above, so greedy max-area selection cannot
        # steal, shrink, or relabel a neighbouring parcel.
        result = dict(originals)
        claimed = []
        for index, candidate, gain in sorted(candidates, key=lambda item: (-item[2], item[0], item[1].wkb)):
            current = result.get(index)
            if current is None or candidate.area <= current.area + 1e-7:
                continue
            if any(self._strict_positive_overlap(candidate, other) for other in claimed):
                continue
            result[index] = candidate
            claimed.append(candidate)
        return result

    def _hp_cover_progress_checkpoint(self, pipeline_logger, completed, phase):
        """Persist HP COVER planning progress every 25 source IDs.

        Planning is side-effect free. These checkpoints update only job status/log;
        DXF entities still commit once after global strict validation succeeds.
        """
        total = int(getattr(self, "_hp_cover_progress_total", 0) or 0)
        if pipeline_logger is None or total <= 0:
            return
        completed = min(max(0, int(completed)), total)
        next_mark = int(getattr(self, "_hp_cover_progress_next", 25) or 25)
        last_mark = int(getattr(self, "_hp_cover_progress_last", 0) or 0)
        marks = []
        while next_mark <= total and completed >= next_mark:
            marks.append(next_mark)
            next_mark += 25
        if completed >= total and last_mark < total and total not in marks:
            marks.append(total)
        for mark in marks:
            if mark <= last_mark:
                continue
            elapsed = time.perf_counter() - float(getattr(self, "_hp_cover_progress_started", time.perf_counter()))
            progress = min(82, 70 + max(1, int(math.ceil(12.0 * mark / total))))
            message = f"Menyusun HP COVER {mark:,}/{total:,} titik ({phase})."
            pipeline_progress(pipeline_logger, progress, message)
            pipeline_log(
                pipeline_logger,
                "info",
                f"[3/6] strict_hp_progress phase={phase} completed={mark}/{total} elapsed_s={elapsed:.1f}",
            )
            last_mark = mark
        self._hp_cover_progress_next = next_mark
        self._hp_cover_progress_last = last_mark

    def _strict_segment_slot_options(
        self, indexes, sites, road, side, clip_region, fill_region,
        base_obstacles=None, angle_override=None, layout_hint=None, candidate_limit=1,
    ):
        """Return the largest complete legal tier for one ROAD-side segment."""
        last_angle = 0.0
        for parcel_size in HP_COVER_PARCEL_SIZE_TIERS:
            last_angle, options = self._strict_segment_slot_options_for_size(
                indexes, sites, road, side, clip_region, fill_region,
                base_obstacles=base_obstacles,
                angle_override=angle_override,
                layout_hint=layout_hint,
                candidate_limit=candidate_limit,
                parcel_size=parcel_size,
            )
            if options:
                return last_angle, options
        return last_angle, []

    def _strict_segment_slot_options_for_size(
        self, indexes, sites, road, side, clip_region, fill_region,
        base_obstacles=None, angle_override=None, layout_hint=None, candidate_limit=1,
        parcel_size=(4.0, 10.0),
    ):
        """Return one planar allocation for an exact ``(frontage, depth)`` tier."""
        indexes = tuple(sorted(indexes))
        if not indexes:
            return 0.0, []
        source_points = [ShapelyPoint(sites[index]) for index in indexes]
        reference = MultiPoint(source_points).centroid
        station = road.project(reference)
        epsilon = min(2.0, max(.25, road.length * .01))
        tangent_a = road.interpolate(max(0.0, station - epsilon))
        tangent_b = road.interpolate(min(road.length, station + epsilon))
        dx, dy = tangent_b.x - tangent_a.x, tangent_b.y - tangent_a.y
        if dx * dx + dy * dy <= 1e-9:
            return 0.0, []
        angle = self._readable_angle(math.degrees(math.atan2(dy, dx)))
        if angle_override is not None:
            try:
                requested = self._readable_angle(float(angle_override))
                delta = abs(((requested - angle + 90.0) % 180.0) - 90.0)
                if delta <= 15.0:
                    angle = requested
            except (TypeError, ValueError):
                pass
        radians = math.radians(angle)
        tx, ty = math.cos(radians), math.sin(radians)
        nx, ny = -ty, tx
        # Grow lattice rows from the ROAD toward the source centroid. `side` is
        # only the segment key: readable-angle normalization may reverse normal
        # sign, so the projected centroid is the authoritative direction.
        nearest = road.interpolate(road.project(reference))
        signed = (reference.x - nearest.x) * nx + (reference.y - nearest.y) * ny
        if signed < 0.0:
            nx, ny = -nx, -ny
        hint = dict(layout_hint or {})
        try:
            along_hint = float(hint.get('along_shift_m', 0.0) or 0.0)
            normal_hint = float(hint.get('normal_shift_m', 0.0) or 0.0)
            width_scale = float(hint.get('width_scale', 1.0) or 1.0)
            depth_scale = float(hint.get('depth_scale', 1.0) or 1.0)
        except (TypeError, ValueError):
            along_hint = normal_hint = 0.0
            width_scale = depth_scale = 1.0
        if not all(math.isfinite(value) for value in (along_hint, normal_hint, width_scale, depth_scale)):
            along_hint = normal_hint = 0.0
            width_scale = depth_scale = 1.0
        along_hint = max(-12.0, min(12.0, along_hint))
        normal_hint = max(-12.0, min(12.0, normal_hint))
        if math.hypot(along_hint, normal_hint) > 12.0:
            scale = 12.0 / math.hypot(along_hint, normal_hint)
            along_hint *= scale; normal_hint *= scale
        entries = [
            (index, point, point.x * tx + point.y * ty, point.x * nx + point.y * ny)
            for index, point in zip(indexes, source_points)
        ]
        road_solid = self._home_parcel_road_solid()
        fixed = [item for item in (base_obstacles or []) if item is not None and not item.is_empty]
        clip_cover = clip_region.buffer(1e-6) if fill_region and clip_region is not None and not clip_region.is_empty else None
        # Size is policy, not an AI hint. AI may shift/rotate the whole local
        # component, but must never invent dimensions outside the ladder.
        try:
            width, depth = (float(value) for value in parcel_size)
        except (TypeError, ValueError):
            return angle, []
        if (width, depth) not in HP_COVER_PARCEL_SIZE_TIERS:
            return angle, []
        min_u, max_u = min(item[2] for item in entries), max(item[2] for item in entries)
        min_v, max_v = min(item[3] for item in entries), max(item[3] for item in entries)
        fronts = []
        for _index, point, _u, _v in entries:
            try:
                edge = nearest_points(point, road_solid.boundary)[1] if road_solid is not None and not road_solid.is_empty else road.interpolate(road.project(point))
            except Exception:
                edge = road.interpolate(road.project(point))
            fronts.append(edge.x * nx + edge.y * ny)
        # Multiple curved-road front samples are deterministic phase candidates.
        # Every candidate grows rows outward from its ROAD-side front; no source-box
        # fallback exists.
        front_values = [min(fronts), sum(fronts) / len(fronts), max(fronts)] if fronts else [min_v - depth / 2.0]
        u_origin = math.floor((min_u - width) / width) * width
        u_bases = [u_origin + phase for phase in (0.0, 1.0, 2.0, 3.0)]
        scored_options = []
        for base_v in dict.fromkeys(round(value + normal_hint, 6) for value in front_values):
            row_count = max(1, int(math.ceil((max_v - base_v) / depth)) + 2)
            for base_u in u_bases:
                column_count = max(1, int(math.ceil((max_u - base_u) / width)) + 3)
                slots = []
                for row in range(row_count):
                    for column in range(column_count):
                        left = base_u + column * width + along_hint
                        bottom = base_v + row * depth
                        def xy(u, v):
                            return (tx * u + nx * v, ty * u + ny * v)
                        parcel = Polygon([
                            xy(left, bottom), xy(left + width, bottom),
                            xy(left + width, bottom + depth), xy(left, bottom + depth),
                        ])
                        if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                            continue
                        if clip_cover is not None and not clip_cover.covers(parcel):
                            continue
                        if any(parcel.intersection(old).area > 1e-7 for old in fixed):
                            continue
                        slots.append(parcel)
                if len(slots) < len(entries):
                    continue
                choices = {}
                for position, (_index, point, _u, _v) in enumerate(entries):
                    slot_choices = [
                        (point.distance(parcel), slot_position)
                        for slot_position, parcel in enumerate(slots)
                        if point.distance(parcel) <= self._strict_relocation_limit(point, [road]) + 1e-6
                    ]
                    if not slot_choices:
                        choices = {}
                        break
                    choices[position] = [slot for _distance, slot in sorted(slot_choices)]
                if not choices:
                    continue
                owner = {}
                def claim(position, seen):
                    for slot_position in choices[position]:
                        if slot_position in seen:
                            continue
                        seen.add(slot_position)
                        prior = owner.get(slot_position)
                        if prior is None or claim(prior, seen):
                            owner[slot_position] = position
                            return True
                    return False
                ordered = sorted(choices, key=lambda position: (len(choices[position]), entries[position][0]))
                if not all(claim(position, set()) for position in ordered):
                    continue
                def option_from_owner(owner_map):
                    return {
                        entries[position][0]: slots[slot_position]
                        for slot_position, position in owner_map.items()
                    }

                slot_grid_cache = {}

                def slot_grid(slot_position):
                    cached = slot_grid_cache.get(slot_position)
                    if cached is not None:
                        return cached
                    parcel = slots[slot_position]
                    center = parcel.centroid
                    center_u = center.x * tx + center.y * ty
                    center_v = center.x * nx + center.y * ny
                    cached = (
                        round((center_u - (base_u + width / 2.0)) / width),
                        round((center_v - (base_v + depth / 2.0)) / depth),
                    )
                    slot_grid_cache[slot_position] = cached
                    return cached

                def packing_score(owner_map):
                    occupied_grid = {slot_grid(slot_position) for slot_position in owner_map}
                    if not occupied_grid:
                        return (0, 0)
                    holes = 0
                    rows = defaultdict(list)
                    columns = defaultdict(list)
                    for column, row in occupied_grid:
                        rows[row].append(column)
                        columns[column].append(row)
                    for values in (*rows.values(), *columns.values()):
                        holes += max(values) - min(values) + 1 - len(set(values))
                    adjacent = sum(
                        (column + 1, row) in occupied_grid
                        for column, row in occupied_grid
                    ) + sum(
                        (column, row + 1) in occupied_grid
                        for column, row in occupied_grid
                    )
                    return (holes, -adjacent)

                def force_slot(owner_map, source_position, target_slot, visited=None):
                    visited = set(visited or ())
                    marker = (source_position, target_slot)
                    if marker in visited:
                        return None
                    visited.add(marker)
                    trial = dict(owner_map)
                    old_slots = [
                        slot_position
                        for slot_position, position in trial.items()
                        if position == source_position
                    ]
                    for old_slot in old_slots:
                        trial.pop(old_slot, None)
                    prior = trial.pop(target_slot, None)
                    if prior is None or prior == source_position:
                        trial[target_slot] = source_position
                        return trial
                    for alternate_slot in choices[prior]:
                        if alternate_slot == target_slot:
                            continue
                        moved = force_slot(trial, prior, alternate_slot, visited)
                        if moved is not None:
                            moved[target_slot] = source_position
                            return moved
                    return None

                # Reassign only compact local strips. Large corridors retain the
                # established allocator; a cubic rematch there gives no bounded
                # visual benefit. ponytail: raise 24 only with a performance audit.
                if len(entries) <= 24:
                    # Greedy matching can leave an empty slot inside an otherwise
                    # valid local row. Retain only a lower-hole ownership map; every
                    # tested slot already passed ROAD/clip/obstacle/relocation gates.
                    for _repair_round in range(3):
                        current_score = packing_score(owner)
                        if current_score[0] == 0:
                            break
                        best_owner = owner
                        best_score = current_score
                        for source_position in choices:
                            for target_slot in choices[source_position]:
                                trial_owner = force_slot(owner, source_position, target_slot)
                                if trial_owner is None or len(trial_owner) != len(entries):
                                    continue
                                trial_score = packing_score(trial_owner)
                                if trial_score < best_score:
                                    best_owner = trial_owner
                                    best_score = trial_score
                        if best_owner is owner:
                            break
                        owner = best_owner

                option = option_from_owner(owner)
                if len(option) != len(entries):
                    continue
                distances = [entries[position][1].distance(option[entries[position][0]]) for position in range(len(entries))]
                holes, negative_adjacent = packing_score(owner)
                # Prefer a contiguous parking-bay occupancy inside this one real
                # ROAD segment. Distance remains the legality tie-break after a
                # lower internal-hole count and higher shared-edge adjacency.
                score = (
                    holes,
                    negative_adjacent,
                    max(distances),
                    sum(distances),
                    abs(base_v - sum(fronts) / len(fronts)) if fronts else 0.0,
                    base_u,
                )
                scored_options.append((score, option))
        scored_options.sort(key=lambda item: item[0])
        unique = []
        seen = set()
        for _score, option in scored_options:
            fingerprint = tuple(option[index].wkb for index in sorted(option))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            unique.append(option)
            if len(unique) >= max(1, int(candidate_limit)):
                break
        return angle, unique

    def _expand_segment_rotation_options(
        self, segment, sites, clip_region, fill_region, base_obstacles=None, axis_lines=None,
    ):
        """Add bounded shared-axis candidates for a real ROAD conflict segment."""
        options = segment.setdefault("options", [])
        angles = segment.setdefault("option_angles", [segment["angle"]] * len(options))
        seen = {
            tuple(option[index].wkb for index in sorted(option))
            for option in options
        }
        # ±5° is intentionally small: component resolution may turn a full
        # segment, never independent HP cells. The slot allocator still enforces
        # 4x10m, ROAD=0, <=12m, and full source membership.
        axis_lines = axis_lines or self._hp_cover_axis_lines(segment["indexes"], sites)[0]
        if not axis_lines or segment["road_position"] >= len(axis_lines):
            return segment
        for delta in (-5.0, 5.0):
            for normal in (0.0, -2.0, 2.0):
                angle, variants = self._strict_segment_slot_options(
                    segment["indexes"],
                    sites,
                    axis_lines[segment["road_position"]],
                    segment["side"],
                    clip_region,
                    fill_region,
                    base_obstacles=base_obstacles,
                    angle_override=segment["angle"] + delta,
                    layout_hint={"normal_shift_m": normal},
                    candidate_limit=1,
                )
                for option in variants:
                    fingerprint = tuple(option[index].wkb for index in sorted(option))
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    options.append(option)
                    angles.append(angle)
        return segment

    def _strict_singleton_recovery_option(
        self, index, sites, road, clip_region, base_obstacles=None, base_angle=0.0,
        angle_deltas=None, return_candidates=False, grid_step=1,
        cell_width=4.0, cell_depth=10.0,
    ):
        """Find one bounded exact-tier recovery cell after normal plans fail."""
        try:
            cell_width = float(cell_width)
            cell_depth = float(cell_depth)
        except (TypeError, ValueError):
            return [] if return_candidates else (None, None)
        if (
            not all(math.isfinite(value) for value in (cell_width, cell_depth))
            or (cell_width, cell_depth) not in HP_COVER_PARCEL_SIZE_TIERS
        ):
            return [] if return_candidates else (None, None)
        source = ShapelyPoint(sites[index])
        road_solid = self._home_parcel_road_solid()
        clip_cover = clip_region.buffer(1e-6) if clip_region is not None and not clip_region.is_empty else None
        fixed = [item for item in (base_obstacles or []) if item is not None and not item.is_empty]
        candidates = []
        deltas = angle_deltas if angle_deltas is not None else range(-15, 16, grid_step)
        # Use the same local ROAD tier as planner/commit. Singleton ID 61
        # previously had a special 18m exception; every source now derives
        # the tier from its active ROAD distance.
        road_dist = source.distance(road)
        max_rel = (
            18.0 if road_dist > 15.0 else
            15.0 if road_dist > 12.0 else
            12.0
        )
        for delta in deltas:
            angle = self._readable_angle(float(base_angle) + delta)
            radians = math.radians(angle)
            tx, ty = math.cos(radians), math.sin(radians)
            nx, ny = -ty, tx
            for along in range(-20, 21, grid_step):
                for normal in range(-20, 21, grid_step):
                    center_x = source.x + tx * along + nx * normal
                    center_y = source.y + ty * along + ny * normal
                    def xy(u, v):
                        return (center_x + tx * u + nx * v, center_y + ty * u + ny * v)
                    half_width = cell_width / 2.0
                    half_depth = cell_depth / 2.0
                    parcel = Polygon([
                        xy(-half_width, -half_depth), xy(half_width, -half_depth),
                        xy(half_width, half_depth), xy(-half_width, half_depth),
                    ])
                    if source.distance(parcel) > max_rel + 1e-6:
                        continue
                    if clip_cover is not None and not clip_cover.covers(parcel):
                        continue
                    if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                        continue
                    if any(parcel.intersection(old).area > 1e-7 for old in fixed):
                        continue
                    candidates.append((
                        source.distance(parcel), abs(delta), abs(along) + abs(normal), angle, parcel,
                    ))
        if not candidates:
            return [] if return_candidates else (None, None)
        if return_candidates:
            return [(item[3], item[4]) for item in sorted(candidates, key=lambda item: item[:3])]
        _distance, _delta, _shift, angle, parcel = min(candidates, key=lambda item: item[:3])
        return angle, {index: parcel}

    def _strict_small_group_recovery_options(
        self, indexes, sites, road, clip_region, base_obstacles=None, base_angle=0.0, max_members=8,
        cell_width=4.0, cell_depth=10.0, allow_equal_split=True,
    ):
        """Joint exact-tier recovery for small segments with no primary slot."""
        indexes = tuple(indexes)
        if not indexes:
            return None, None
        # The joint backtracking cap bounds search complexity; it is not a
        # geometry-feasibility gate. Large local groups use linear equal-split
        # recovery instead of falling straight to label-only.
        if len(indexes) > max_members:
            if not allow_equal_split:
                return None, None
            equal = self._strict_equal_split_group_recovery(
                indexes, sites, road, clip_region,
                base_obstacles=base_obstacles, base_angle=base_angle,
            )
            if equal is not None:
                return equal
            equal = self._strict_equal_split_boundary_recovery(
                indexes, sites, road, clip_region,
                base_obstacles=base_obstacles, base_angle=base_angle,
            )
            return equal if equal is not None else (None, None)
        per_member = []
        for index in indexes:
            # Coarse angular sweep first: enough unique non-overlap combinations
            # for small groups without a 30x direct explosion.
            cands = self._strict_singleton_recovery_option(
                index, sites, road, clip_region,
                base_obstacles=base_obstacles, base_angle=base_angle,
                return_candidates=True, grid_step=3,
                cell_width=cell_width, cell_depth=cell_depth,
            )
            if not cands:
                # Retry with full sweep only for the failing member.
                cands = self._strict_singleton_recovery_option(
                    index, sites, road, clip_region,
                    base_obstacles=base_obstacles, base_angle=base_angle,
                    return_candidates=True, grid_step=1,
                    cell_width=cell_width, cell_depth=cell_depth,
                )
            if not cands:
                if not allow_equal_split:
                    return None, None
                # Preserve the existing equal-split parcel fallback when the
                # caller explicitly allows it; label-only remains downstream.
                equal = self._strict_equal_split_group_recovery(
                    indexes, sites, road, clip_region,
                    base_obstacles=base_obstacles, base_angle=base_angle,
                )
                if equal is not None:
                    return equal
                return None, None
            per_member.append((index, cands[:12]))
        # Joint backtracking: pick one cell per HP with zero pairwise overlap.
        chosen_cells = {}
        # ponytail: joint backtracking bounded to max_members=8; if still no
        # non-overlap, equal-split fallback takes over at lattice level.
        # Upgrade: per-HP rotation sweep for groups >4 members.
        node_budget = 200000
        nodes = 0

        def search(pos, occupied):
            nonlocal nodes
            nodes += 1
            if nodes > node_budget:
                return False
            if pos == len(per_member):
                return True
            index, candidates = per_member[pos]
            for _angle, cell in candidates:
                if any(self._strict_positive_overlap(cell, old) for old in occupied):
                    continue
                chosen_cells[index] = cell
                if search(pos + 1, occupied + [cell]):
                    return True
                del chosen_cells[index]
            return False

        if search(0, []):
            if len(chosen_cells) == len(indexes) and not any(
                self._strict_positive_overlap(chosen_cells[left], chosen_cells[right])
                for right in chosen_cells for left in chosen_cells if left < right
            ):
                return base_angle, dict(chosen_cells)

        if not allow_equal_split:
            return None, None
        # Preserve the existing equal-split fallback only for callers that
        # explicitly allow it; the strict tiered path tries 4x7 first.
        equal = self._strict_equal_split_group_recovery(
            indexes, sites, road, clip_region, base_obstacles=base_obstacles,
        )
        if equal is not None:
            return equal
        equal = self._strict_equal_split_boundary_recovery(
            indexes, sites, road, clip_region, base_obstacles=base_obstacles,
            base_angle=base_angle,
        )
        return equal if equal is not None else (None, None)

    def _strict_equal_split_boundary_recovery(
        self, indexes, sites, road, clip_region, base_obstacles=None, base_angle=0.0,
    ):
        """Equal frontage strip placed outward from the actual ROAD boundary."""
        indexes = tuple(sorted(indexes, key=lambda i: road.project(ShapelyPoint(sites[i]))))
        if not indexes or road is None or road.is_empty:
            return None
        stations = [road.project(ShapelyPoint(sites[i])) for i in indexes]
        start, end = min(stations), max(stations)
        span = max(end - start, 4.0 * len(indexes))
        start = max(0.0, min(float(road.length) - span, start - max(0.0, (span - (end - start)) / 2.0)))
        end = min(float(road.length), start + span)
        frontage = (end - start) / len(indexes)
        if frontage <= 1e-6:
            return None
        road_solid = self._home_parcel_road_solid()
        clip_cover = clip_region.buffer(1e-6) if clip_region is not None and not clip_region.is_empty else None
        fixed = [g for g in (base_obstacles or []) if g is not None and not g.is_empty]
        mid = road.interpolate((start + end) / 2.0)
        src_mid = MultiPoint([ShapelyPoint(sites[i]) for i in indexes]).centroid
        before = road.interpolate(max(0.0, (start + end) / 2.0 - .5))
        after = road.interpolate(min(float(road.length), (start + end) / 2.0 + .5))
        tx, ty = after.x - before.x, after.y - before.y
        norm = math.hypot(tx, ty)
        if norm <= 1e-9:
            return None
        tx, ty = tx / norm, ty / norm
        nx, ny = -ty, tx
        if (src_mid.x - mid.x) * nx + (src_mid.y - mid.y) * ny < 0:
            nx, ny = -nx, -ny
        angle = self._readable_angle(math.degrees(math.atan2(ty, tx)))
        for depth in (4.0, 3.0, 2.0, 1.0):
            for offset in (0.0, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0):
                result = {}
                valid = True
                for position, index in enumerate(indexes):
                    station = start + (position + .5) * frontage
                    anchor = road.interpolate(station)
                    edge = anchor
                    if road_solid is not None and not road_solid.is_empty:
                        try:
                            edge = nearest_points(anchor, road_solid.boundary)[1]
                        except Exception:
                            pass
                    cx = edge.x + nx * (depth / 2.0 + offset + 1e-3)
                    cy = edge.y + ny * (depth / 2.0 + offset + 1e-3)
                    cell = Polygon([
                        (cx - tx * frontage / 2 - nx * depth / 2, cy - ty * frontage / 2 - ny * depth / 2),
                        (cx + tx * frontage / 2 - nx * depth / 2, cy + ty * frontage / 2 - ny * depth / 2),
                        (cx + tx * frontage / 2 + nx * depth / 2, cy + ty * frontage / 2 + ny * depth / 2),
                        (cx - tx * frontage / 2 + nx * depth / 2, cy - ty * frontage / 2 + ny * depth / 2),
                    ])
                    source = ShapelyPoint(sites[index])
                    if clip_cover is not None and not clip_cover.covers(cell): valid = False; break
                    if road_solid is not None and not road_solid.is_empty and cell.intersection(road_solid).area > 1e-7: valid = False; break
                    if any(cell.intersection(old).area > 1e-7 for old in fixed): valid = False; break
                    if source.distance(cell) > self._strict_relocation_limit(source, [road]) + 1e-6: valid = False; break
                    if any(self._strict_positive_overlap(cell, old) for old in result.values()): valid = False; break
                    result[index] = cell
                if valid and len(result) == len(indexes):
                    return angle, result
        return None

    def _strict_equal_split_group_recovery(
        self, indexes, sites, road, clip_region, base_obstacles=None, base_angle=0.0,
    ):
        """Fallback per user: bagi rata interval jalan lokal; cell dibangun di
        sekitar source DI DALAM FAT polygon (bukan dari road edge). HP yang
        terhimpit jauh dari centerline tetap dapat parcel tanpa melanggar
        relocate<=12m, ROAD=0, clip-covers, non-overlap.
        """
        indexes = tuple(indexes)
        if not indexes or road is None or road.is_empty:
            return None
        index_order = tuple(sorted(indexes, key=lambda i: road.project(ShapelyPoint(sites[i]))))
        stations = {i: road.project(ShapelyPoint(sites[i])) for i in index_order}
        start = min(stations[i] for i in index_order)
        end = max(stations[i] for i in index_order)
        span = end - start
        if span <= 1e-6:
            span = max(2.0 * len(index_order), 1.0)
            start = max(0.0, stations[index_order[0]] - span / 2.0)
            end = start + span
        frontage = span / len(index_order)
        if frontage <= 1e-6:
            return None
        road_solid = self._home_parcel_road_solid()
        clip_cover = clip_region.buffer(1e-6) if clip_region is not None and not clip_region.is_empty else None
        fixed = [g for g in (base_obstacles or []) if g is not None and not g.is_empty]
        mid = road.interpolate((start + end) / 2.0)
        src_mid = MultiPoint([ShapelyPoint(sites[i]) for i in index_order]).centroid
        before = road.interpolate(max(0.0, (start + end) / 2.0 - .5))
        after = road.interpolate(min(float(road.length), (start + end) / 2.0 + .5))
        tx, ty = after.x - before.x, after.y - before.y
        norm = math.hypot(tx, ty)
        if norm <= 1e-9:
            return None
        tx, ty = tx / norm, ty / norm
        nx, ny = -ty, tx
        # Normal menjauhi road centerline.
        if (src_mid.x - mid.x) * nx + (src_mid.y - mid.y) * ny < 0:
            nx, ny = -nx, -ny
        angle = self._readable_angle(math.degrees(math.atan2(ty, tx)))
        # Grid seragam: station_i = start + (i+0.5)*frontage; cell dibangun
        # mengelilingi titik pada grid tsb, dicoba depth besar->kecil dan
        # offset normal kecil->besar agar muat di dalam clip polygon.
        grid_points = []
        for position, index in enumerate(index_order):
            station = start + (position + 0.5) * frontage
            anchor = road.interpolate(min(end, max(start, station)))
            grid_points.append((index, station, anchor))
        # Fallback: AI rotation hint is applied but bounded. If AI suggests
        # rotation, it is fed to equal-split as base_angle with ±30° sweep.
        # ponytail: AI hint is bounded; deterministic gate (≤12m, non-overlap)
        # overrides AI when AI violates physical constraints.
        base_recovery_angle = self._readable_angle(math.degrees(math.atan2(ty, tx)))
        # Apply AI rotation only for joint backtracking; equal-split uses it directly.
        base_recovery_angle = angle  # AI override
        # ponytail: bounded rotation ±30° already covers AI ±15°; no extra sweep needed.
        # clusters. If still no non-overlap, the joint backtracking already
        # failed and the same geometry will still fail; next improvement is
        # per-member rotation sweep in _strict_singleton_recovery_option.
        for rot_deg in (0, 10, 20, 30, -10, -20, -30):
            if rot_deg == 0:
                rtx, rty = tx, ty
                rnx, rny = nx, ny
            else:
                rad = math.radians(math.degrees(math.atan2(ty, tx)) + rot_deg)
                rtx, rty = math.cos(rad), math.sin(rad)
                rnx, rny = -rty, rtx
            for shift in (0.0, 1.0, 2.0, 3.0):
                for depth in (4.0, 3.0, 2.0, 1.0):
                    for offset in (0.0, 2.0, 4.0, 6.0):
                        result = {}
                        valid = True
                        for position, index in enumerate(index_order):
                            station = start + (position + 0.5) * frontage
                            source = ShapelyPoint(sites[index])
                            # Dense mesh: parcel centered on tangent at station,
                            # shifted normal toward source (capped at ≤12m).
                            # Tangent-aligned rectangle shares full edge with neighbor.
                            anchor = road.interpolate(min(end, max(start, station)))
                            # Use ROAD coordinates as the origin. `station` is a
                            # scalar distance along ROAD, not an absolute X/Y.
                            # ponytail: preserve local station, offset from anchor.
                            src_norm = (source.x - anchor.x) * nx + (source.y - anchor.y) * ny
                            # Cap shift so parcel still covers source within 18m max.
                            # ponytail: tiered relocation: normal ≤12m, moderate ≤15m, extreme ≤18m
                            half_diag = math.hypot(frontage / 2.0, depth / 2.0)
                            max_normal = max(0.0, 18.0 - half_diag)
                            src_norm = max(-max_normal, min(max_normal, src_norm))
                            # Center shifted toward source normal direction.
                            cx = anchor.x + nx * (depth / 2 + offset + src_norm)
                            cy = anchor.y + ny * (depth / 2 + offset + src_norm)
                            cell = Polygon([
                                (cx - rtx * frontage / 2 - rnx * depth / 2, cy - rty * frontage / 2 - rny * depth / 2),
                                (cx + rtx * frontage / 2 - rnx * depth / 2, cy + rty * frontage / 2 - rny * depth / 2),
                                (cx + rtx * frontage / 2 + rnx * depth / 2, cy + rty * frontage / 2 + rny * depth / 2),
                                (cx - rtx * frontage / 2 + rnx * depth / 2, cy - rty * frontage / 2 + rny * depth / 2),
                            ])
                            # Pinched segments may relax FAT clipping and source
                            # relocation, but ROAD overlap and positive parcel
                            # overlap remain hard invariants.
                            if frontage >= 3.0 and clip_cover is not None and not clip_cover.covers(cell):
                                valid = False; break
                            if road_solid is not None and not road_solid.is_empty and cell.intersection(road_solid).area > 1e-7:
                                valid = False; break
                            if any(cell.intersection(old).area > 1e-7 for old in fixed):
                                valid = False; break
                            dist = source.distance(cell)
                            road_dist = source.distance(road)
                            max_relocation = (
                                18.0 if road_dist > 15.0 else
                                15.0 if road_dist > 12.0 else
                                12.0
                            )
                            if dist > (30.0 if frontage < 3.0 else max_relocation) + 1e-6:
                                valid = False; break
                            if any(self._strict_positive_overlap(cell, old) for old in result.values()):
                                valid = False; break
                            result[index] = cell
                        if valid and len(result) == len(index_order):
                            return self._readable_angle(rot_deg), result
        # ponytail: equal-split second pass — relax clip_cover for mepet parcels
        # If all HP cannot fit strictly inside FAT, generate parcels anyway
        # (mepet = edge-touch/slightly outside FAT allowed) as long as
        # ROAD=0, non-overlap, and source distance <= 18m are satisfied.
        for rot_deg in (0, 10, 20, 30, -10, -20, -30):
            if rot_deg == 0:
                rtx, rty = tx, ty
                rnx, rny = nx, ny
            else:
                rad = math.radians(math.degrees(math.atan2(ty, tx)) + rot_deg)
                rtx, rty = math.cos(rad), math.sin(rad)
                rnx, rny = -rty, rtx
            for shift in (0.0, 1.0, 2.0, 3.0):
                for depth in (4.0, 3.0, 2.0, 1.0):
                    for offset in (0.0, 2.0, 4.0, 6.0):
                        result = {}
                        valid = True
                        for position, index in enumerate(index_order):
                            station = start + (position + 0.5) * frontage
                            source = ShapelyPoint(sites[index])
                            anchor = road.interpolate(min(end, max(start, station)))
                            # `station` is a scalar distance along ROAD, not an X/Y origin.
                            src_norm = (source.x - anchor.x) * nx + (source.y - anchor.y) * ny
                            half_diag = math.hypot(frontage / 2.0, depth / 2.0)
                            max_normal = max(0.0, 18.0 - half_diag)
                            src_norm = max(-max_normal, min(max_normal, src_norm))
                            cx = anchor.x + nx * (depth / 2 + offset + src_norm)
                            cy = anchor.y + ny * (depth / 2 + offset + src_norm)
                            cell = Polygon([
                                (cx - rtx * frontage / 2 - rnx * depth / 2, cy - rty * frontage / 2 - rny * depth / 2),
                                (cx + rtx * frontage / 2 - rnx * depth / 2, cy + rty * frontage / 2 - rny * depth / 2),
                                (cx + rtx * frontage / 2 + rnx * depth / 2, cy + rty * frontage / 2 + rny * depth / 2),
                                (cx - rtx * frontage / 2 + rnx * depth / 2, cy - rty * frontage / 2 + rny * depth / 2),
                            ])
                            # Second pass relaxes FAT clipping only. ROAD,
                            # fixed-obstacle, and positive-overlap gates remain hard.
                            if road_solid is not None and not road_solid.is_empty and cell.intersection(road_solid).area > 1e-7:
                                valid = False; break
                            if any(cell.intersection(old).area > 1e-7 for old in fixed):
                                valid = False; break
                            dist = source.distance(cell)
                            road_dist = source.distance(road)
                            max_relocation = (
                                18.0 if road_dist > 15.0 else
                                15.0 if road_dist > 12.0 else
                                12.0
                            )
                            if dist > (30.0 if frontage < 3.0 else max_relocation) + 1e-6:
                                valid = False; break
                            if any(self._strict_positive_overlap(cell, old) for old in result.values()):
                                valid = False; break
                            result[index] = cell
                        if valid and len(result) == len(index_order):
                            return self._readable_angle(rot_deg), result
        # ponytail: no label-only fallback; return None to let caller raise
        # "tidak menemukan slot planar legal" — invariant 1 HP = 1 parcel stays.
        return None

    def _apply_ai_segment_hint(
        self, segment, sites, clip_region, fill_region, roads, base_obstacles=None,
        pipeline_logger=None,
    ):
        """Add bounded AI options for one real ROAD conflict segment only.

        AI returns a hint, never polygons. The slot allocator remains the geometry
        authority and rejects ROAD/overlap/distance-invalid candidates.
        """
        indexes = tuple(segment.get("indexes") or ())
        options = segment.get("options") or []
        if len(indexes) < 2 or not options:
            return None
        try:
            preview = self._hp_visual_parcel_png(
                clip_region,
                [ShapelyPoint(sites[index]) for index in indexes],
                candidate_parcels=list(options[0].values()),
            )
            hint = suggest_dxf_visual_parcel_layout(
                preview_png=preview,
                fat_angle=segment["angle"],
                road_angle=segment["angle"],
                point_count=len(indexes),
            )
            if not hint:
                return None
            angle = self._readable_angle(float(hint["angle_degrees"]))
            delta = abs(((angle - segment["angle"] + 90.0) % 180.0) - 90.0)
            if delta > 15.0:
                return None
            requested_side = 1 if float(hint.get("road_side", segment["side"]) or segment["side"]) >= 0 else -1
            _candidate_angle, candidates = self._strict_segment_slot_options(
                indexes, sites, roads[segment["road_position"]], requested_side,
                clip_region, fill_region, base_obstacles=base_obstacles,
                angle_override=angle, layout_hint=hint, candidate_limit=3,
            )
            known = {
                tuple(option[index].wkb for index in sorted(option))
                for option in options
            }
            added = 0
            option_angles = segment.setdefault("option_angles", [segment["angle"]] * len(options))
            for candidate in candidates:
                fingerprint = tuple(candidate[index].wkb for index in sorted(candidate))
                if fingerprint in known:
                    continue
                options.append(candidate)
                option_angles.append(angle)
                known.add(fingerprint)
                added += 1
            if added and pipeline_logger is not None:
                pipeline_log(
                    pipeline_logger, "info",
                    "[3/6] HP COVER AI segment hint accepted: "
                    f"axis {angle:.1f}°, confidence {float(hint['confidence']):.2f}, "
                    f"road side {requested_side}, "
                    f"scale {float(hint.get('width_scale', 1.0)):.2f}×{float(hint.get('depth_scale', 1.0)):.2f}, "
                    f"candidates +{added}.",
                )
            return hint if added else None
        except Exception as exc:
            LOGGER.debug("HP COVER segment AI hint fallback: %s", exc)
            return None

    def _hp_road_side_segments(self, indexes, sites, road, gap_limit=13.0, bend_limit=30.0):
        """Split one ROAD-side membership at real station gaps and bends."""
        indexes = tuple(sorted(indexes))
        if not indexes:
            return []
        def tangent(index):
            station = road.project(ShapelyPoint(sites[index]))
            epsilon = min(2.0, max(.25, road.length * .01))
            before = road.interpolate(max(0.0, station - epsilon))
            after = road.interpolate(min(road.length, station + epsilon))
            return station, math.degrees(math.atan2(after.y - before.y, after.x - before.x))
        def axis_delta(left, right):
            return abs(((right - left + 90.0) % 180.0) - 90.0)
        ordered = sorted(((index, *tangent(index)) for index in indexes), key=lambda item: (item[1], item[0]))
        segments, current = [], [ordered[0][0]]
        previous_station, previous_angle = ordered[0][1], ordered[0][2]
        for index, station, angle in ordered[1:]:
            if station - previous_station > gap_limit or axis_delta(previous_angle, angle) > bend_limit:
                segments.append(tuple(current))
                current = []
            current.append(index)
            previous_station, previous_angle = station, angle
        if current:
            segments.append(tuple(current))
        return segments

    def _guaranteed_final_4x8_repack(
        self, indexes, sites, roads, base_obstacles=None,
    ):
        """Assign every HP COVER to a shared 4×8 grid outside ROAD.

        This is the final tier only. FAT clipping and relocation are relaxed so
        no HP COVER degrades to text-only; ROAD and positive-area overlap remain
        hard constraints.
        """
        indexes = tuple(sorted(indexes))
        if not indexes:
            return 0.0, {}
        source_points = [ShapelyPoint(sites[index]) for index in indexes]
        reference = MultiPoint(source_points).centroid
        usable_roads = [road for road in (roads or []) if road is not None and not road.is_empty]
        angle = 0.0
        if usable_roads:
            road = min(usable_roads, key=lambda item: item.distance(reference))
            station = road.project(reference)
            epsilon = min(2.0, max(.25, road.length * .01))
            before = road.interpolate(max(0.0, station - epsilon))
            after = road.interpolate(min(road.length, station + epsilon))
            dx, dy = after.x - before.x, after.y - before.y
            if dx * dx + dy * dy > 1e-9:
                angle = self._readable_angle(math.degrees(math.atan2(dy, dx)))
        radians = math.radians(angle)
        tx, ty = math.cos(radians), math.sin(radians)
        nx, ny = -ty, tx
        width, depth = HP_COVER_PARCEL_SIZE_TIERS[-1]
        mean_u = sum(point.x * tx + point.y * ty for point in source_points) / len(source_points)
        mean_v = sum(point.x * nx + point.y * ny for point in source_points) / len(source_points)
        origin_u = round(mean_u / width) * width
        origin_v = round(mean_v / depth) * depth
        road_solid = self._home_parcel_road_solid()
        fixed = [item for item in (base_obstacles or []) if item is not None and not item.is_empty]
        slots = []
        target = len(indexes) + max(8, len(indexes) // 4)

        def cell_at(column, row):
            center_u = origin_u + column * width
            center_v = origin_v + row * depth
            left, right = center_u - width / 2.0, center_u + width / 2.0
            front, back = center_v - depth / 2.0, center_v + depth / 2.0
            def xy(u, v):
                return (tx * u + nx * v, ty * u + ny * v)
            return Polygon([
                xy(left, front), xy(right, front),
                xy(right, back), xy(left, back),
            ])

        # Expanding square rings keep the fallback local in normal cases. The
        # 512-ring ceiling spans at least 2 km × 4 km around the source group.
        for radius in range(513):
            coordinates = (
                [(column, row) for column in range(-radius, radius + 1) for row in (-radius, radius)]
                + [(column, row) for row in range(-radius + 1, radius) for column in (-radius, radius)]
                if radius else [(0, 0)]
            )
            for column, row in coordinates:
                parcel = cell_at(column, row)
                if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                    continue
                if any(parcel.intersection(obstacle).area > 1e-7 for obstacle in fixed):
                    continue
                slots.append(parcel)
            if len(slots) >= target:
                break
        if len(slots) < len(indexes):
            raise RuntimeError('HP COVER final 4×8 grid tidak menemukan ruang bebas di luar ROAD.')

        available = set(range(len(slots)))
        assignments = {}
        # Hardest/farthest sources choose first; each then claims its nearest free
        # grid cell. Grid slots are disjoint by construction.
        source_order = sorted(
            range(len(indexes)),
            key=lambda position: (
                -min(source_points[position].distance(slot) for slot in slots),
                indexes[position],
            ),
        )
        for position in source_order:
            slot_position = min(
                available,
                key=lambda candidate: (
                    source_points[position].distance(slots[candidate]), candidate,
                ),
            )
            assignments[indexes[position]] = slots[slot_position]
            available.remove(slot_position)
        self._last_relaxed_4x8_indexes.update(indexes)
        return angle, assignments

    def _strict_segmented_hp_plan(
        self, indexes, sites, clip_region, fill_region, base_obstacles=None,
        pipeline_logger=None,
    ):
        """Plan all HP cells by real ROAD-side segments, then commit atomically.

        Segments own their complete planar slot lattice before sources are matched.
        Each later segment treats earlier cells as hard obstacles, so separator cells
        cannot overlap across segments. This replaces local-box conflict DFS.
        """
        indexes = tuple(sorted(indexes))
        self._last_label_only_indexes = set()
        self._last_relaxed_4x8_indexes = set()
        roads, axis_source = self._hp_cover_axis_lines(indexes, sites)
        # Commit must validate relocation against the same local ROAD set used
        # during planning; recomputing against every global road can select a
        # different nearer line and silently tighten the active tier.
        self._last_hp_relocation_roads = list(roads)
        if not roads:
            raise ValueError(
                'HP COVER strict planner membutuhkan ROAD centerline atau Distribution Cable.'
            )
        groups = {}
        for index in indexes:
            point = ShapelyPoint(sites[index])
            road_position, road = min(enumerate(roads), key=lambda pair: point.distance(pair[1]))
            station = road.project(point)
            epsilon = min(2.0, max(.25, road.length * .01))
            tangent_a = road.interpolate(max(0.0, station - epsilon))
            tangent_b = road.interpolate(min(road.length, station + epsilon))
            dx, dy = tangent_b.x - tangent_a.x, tangent_b.y - tangent_a.y
            signed = (point.x - tangent_a.x) * (-dy) + (point.y - tangent_a.y) * dx
            groups.setdefault((road_position, 1.0 if signed >= 0 else -1.0), []).append(index)
        fixed = [parcel for parcel in (base_obstacles or []) if parcel is not None and not parcel.is_empty]
        selected, rotations, segment_by_index = {}, {}, {}
        completed = 0
        # ROAD+side is not enough: one curved LineString may contain several
        # physical corridors. Split on genuine tangent bend / station gap first.
        segments = []
        for (road_position, side), members in groups.items():
            for member_ids in self._hp_road_side_segments(members, sites, roads[road_position]):
                segments.append({'road_position': road_position, 'side': side, 'indexes': tuple(member_ids)})

        # Phase 1: independent bounded candidates. No cross-segment reservation yet;
        # it would greedily freeze one segment over another at a junction.
        for segment in segments:
            angle, options = self._strict_segment_slot_options(
                segment['indexes'], sites, roads[segment['road_position']], segment['side'],
                clip_region, fill_region, base_obstacles=fixed, candidate_limit=6,
            )
            # A nearest ROAD can have no legal 4x10 frontage at a junction.
            # Keep evaluating nearby ROAD alternatives before rejecting this source
            # segment; final global ROAD/relocation checks still apply below.
            segment['angle'] = angle
            segment['options'] = options
            segment['option_angles'] = [angle] * len(options)
            # Junction sources can legally front a second nearby ROAD. Add its
            # bounded slot plans; the component selector chooses only a plan that
            # remains ROAD-safe, <=12m from the immutable KMZ point, and disjoint.
            reference = MultiPoint([ShapelyPoint(sites[index]) for index in segment['indexes']]).centroid
            alternate_roads = sorted(
                (
                    (reference.distance(other_road), other_position, other_road)
                    for other_position, other_road in enumerate(roads)
                    if other_position != segment['road_position']
                    and reference.distance(other_road) <= 12.0 + 1e-6
                ),
                key=lambda item: (item[0], item[1]),
            )[:2]
            for _distance, other_position, other_road in alternate_roads:
                tangent_station = other_road.project(reference)
                epsilon = min(2.0, max(.25, other_road.length * .01))
                tangent_a = other_road.interpolate(max(0.0, tangent_station - epsilon))
                tangent_b = other_road.interpolate(min(other_road.length, tangent_station + epsilon))
                dx, dy = tangent_b.x - tangent_a.x, tangent_b.y - tangent_a.y
                signed = (reference.x - tangent_a.x) * (-dy) + (reference.y - tangent_a.y) * dx
                other_side = 1.0 if signed >= 0 else -1.0
                other_angle, alternate_options = self._strict_segment_slot_options(
                    segment['indexes'], sites, other_road, other_side,
                    clip_region, fill_region, base_obstacles=fixed, candidate_limit=6,
                )
                for option in alternate_options:
                    # Skip label-only markers (dict) from fingerprint comparison
                    if any(isinstance(option.get(idx), dict) for idx in option):
                        continue
                    fingerprint = tuple(option[index].wkb for index in sorted(option))
                    known = {
                        tuple(existing[index].wkb for index in sorted(existing))
                        for existing in segment['options']
                    }
                    if fingerprint in known:
                        continue
                    segment['options'].append(option)
                    segment['option_angles'].append(other_angle)

            if not segment['options']:
                # Bounded exact-tier recovery before the guaranteed 4×8 lattice.
                recovery_option = None
                recovery_angle = segment['angle']
                recovery_tier = None
                for recovery_width, recovery_depth in HP_COVER_PARCEL_SIZE_TIERS:
                    recovery_angle, recovery_option = self._strict_small_group_recovery_options(
                        segment['indexes'],
                        sites,
                        roads[segment['road_position']],
                        clip_region,
                        base_obstacles=fixed,
                        base_angle=segment['angle'],
                        cell_width=recovery_width,
                        cell_depth=recovery_depth,
                        allow_equal_split=False,
                    )
                    if recovery_option:
                        recovery_tier = f'{recovery_width:g}×{recovery_depth:g}'
                        break
                if recovery_option:
                    segment['options'].append(recovery_option)
                    segment['option_angles'].append(recovery_angle)
                    pipeline_log(
                        pipeline_logger, 'info',
                        f'[3/6] HP COVER strict small-group recovery: bounded {recovery_tier} cells '
                        f"for {len(segment['indexes'])} HP at segment "
                        f"{segment['road_position']}/{int(segment['side'])}, axis {recovery_angle:.1f}°.",
                    )
            if not segment['options']:
                # Final policy: every HP COVER remains present and boxed. Expand a
                # shared 4×8 lattice beyond FAT if required; ROAD and parcel overlap
                # remain forbidden.
                indexes_tuple = tuple(segment['indexes'] or [])
                recovery_angle, recovery_option = self._guaranteed_final_4x8_repack(
                    indexes_tuple, sites, roads, base_obstacles=fixed,
                )
                segment['options'].append(recovery_option)
                segment['option_angles'].append(recovery_angle)
                pipeline_log(
                    pipeline_logger, 'warning',
                    f"[FINAL 4×8] Segment={segment['road_position']}/{int(segment['side'])} "
                    f"repacked {len(indexes_tuple)} HP COVER; FAT clip relaxed, ROAD/overlap tetap nol.",
                )

        # Phase 2: build conflicts from independent first candidates. Components are
        # small real ROAD junctions (not the prior 350-HP local-box chain).
        parent = list(range(len(segments)))
        def find(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value
        def union(left, right):
            left, right = find(left), find(right)
            if left != right:
                parent[right] = left
        for right, segment_right in enumerate(segments):
            for left, segment_left in enumerate(segments[:right]):
                if any(
                    self._strict_positive_overlap(first, second)
                    for first in segment_left['options'][0].values()
                    for second in segment_right['options'][0].values()
                ):
                    union(left, right)
        components = {}
        for position in range(len(segments)):
            components.setdefault(find(position), []).append(position)
        # Expand axis only when a segment is in a real overlap component.
        # Singleton segments retain deterministic primary/alternate ROAD plans.
        for positions in components.values():
            if len(positions) > 1:
                for position in positions:
                    self._expand_segment_rotation_options(
                        segments[position], sites, clip_region, fill_region,
                        base_obstacles=fixed, axis_lines=roads,
                    )
                    self._apply_ai_segment_hint(
                        segments[position], sites, clip_region, fill_region, roads,
                        base_obstacles=fixed, pipeline_logger=pipeline_logger,
                    )

        occupied = list(fixed)
        completed = 0
        ordered_components = sorted(
            components.values(),
            key=lambda positions: (-sum(len(segments[position]['indexes']) for position in positions), min(positions)),
        )
        for component_number, positions in enumerate(ordered_components, 1):
            plans = [segments[position] for position in positions]
            # Junction conflict may only appear when multiple segments share cells.
            # Singleton components can fall back to alternate ROAD via
            # `_expand_segment_rotation_options`; if that still fails, raise early.
            chosen = self._select_strict_strip_candidates(plans, base_obstacles=occupied)
            # Candidate options generated in Phase 1 only avoid `fixed`, but the
            # slots that actually occupy this junction belong to other segments
            # placed earlier. Regenerate per-segment options against the *real*
            # occupied cells before giving up.
            if chosen is None:
                for plan in plans:
                    regenerate, regenerate_angles = [], []
                    for option, option_angle in zip(plan['options'], plan.get('option_angles', [plan['angle']] * len(plan['options']))):
                        fingerprint = tuple(option[index].wkb for index in sorted(option))
                        seen_r = {tuple(old[index].wkb for index in sorted(old)) for old in regenerate}
                        if fingerprint in seen_r:
                            continue
                        regenerate.append(option)
                        regenerate_angles.append(option_angle)
                    angle, fresh = self._strict_segment_slot_options(
                        plan['indexes'], sites, roads[plan['road_position']], plan['side'],
                        clip_region, fill_region, base_obstacles=occupied, candidate_limit=6,
                    )
                    for option in fresh:
                        fingerprint = tuple(option[index].wkb for index in sorted(option))
                        if fingerprint in seen_r:
                            continue
                        seen_r.add(fingerprint)
                        regenerate.append(option)
                        regenerate_angles.append(angle)
                    plan['options'] = regenerate
                    plan['option_angles'] = regenerate_angles
                chosen = self._select_strict_strip_candidates(plans, base_obstacles=occupied)
            combined_plan = None
            combined_axis_position = None
            if chosen is None:
                combined_ids = tuple(sorted(index for plan in plans for index in plan['indexes']))
                lattice_angle, lattice_options = self._strict_component_lattice_options(
                    combined_ids, sites, clip_region, fill_region,
                    base_obstacles=occupied,
                    candidate_limit=1,
                    axis_lines=roads,
                )
                if lattice_options:
                    combined_plan = lattice_options[0]
                    chosen = combined_plan
                    component_axis = getattr(self, '_last_strict_component_axis_line', None)
                    combined_axis_position = next(
                        (position for position, line in enumerate(roads) if line is component_axis),
                        None,
                    )
                    if combined_axis_position is None:
                        raise ValueError('HP COVER component lattice kehilangan active axis.')
                if combined_plan is None:
                    # Independent per-segment cells cannot all avoid each other at a
                    # multi-way junction. Replan the whole conflict component as one
                    # shared planar 4x10 lattice rooted at one ROAD; source-to-cell
                    # stays <=12m, no cell may overlap ROAD or another cell.
                    combined_ids = tuple(sorted(index for plan in plans for index in plan['indexes']))
                    lattice = None
                    lattice_angle = None
                    tried_roads = []
                if combined_plan is None:
                    # Try ALL roads only when the first component lattice failed.
                    # A valid active-axis lattice must never be overwritten by a
                    # later, farther ROAD candidate.
                    for road_position in sorted(range(len(roads))):
                        for side in (-1.0, 1.0):
                            tried_roads.append((road_position, side))
                            angle, options = self._strict_segment_slot_options(
                                combined_ids, sites, roads[road_position], side,
                                clip_region, fill_region, base_obstacles=occupied, candidate_limit=1,
                            )
                            if not options:
                                continue
                            option = options[0]
                            if any(
                                self._strict_positive_overlap(left, right)
                                for right_index, right in option.items()
                                for left_index, left in option.items()
                                if left_index < right_index
                            ):
                                continue
                            lattice = option
                            lattice_angle = angle
                            combined_axis_position = road_position
                            break
                        if lattice is not None:
                            break
                    if lattice is None:
                        # Guaranteed final tier: every HP COVER gets one 4×8 cell.
                        # FAT/relocation may relax; ROAD and overlap remain hard.
                        combined_ids = tuple(sorted(index for plan in plans for index in plan['indexes']))
                        lattice_angle, lattice = self._guaranteed_final_4x8_repack(
                            combined_ids, sites, roads, base_obstacles=occupied,
                        )
                        combined_axis_position = min(
                            range(len(roads)),
                            key=lambda position: MultiPoint(
                                [ShapelyPoint(sites[index]) for index in combined_ids]
                            ).centroid.distance(roads[position]),
                        )
                        pipeline_log(
                            pipeline_logger, 'warning',
                            f"[FINAL 4×8] component={component_number} repacked "
                            f"{len(combined_ids)} HP COVER; ROAD/overlap tetap nol.",
                        )
                    combined_plan = lattice
            chosen = combined_plan or chosen
            if chosen is None:
                detail = [list(plan['indexes'])[:12] for plan in plans]
                raise ValueError(f'HP COVER ROAD segment conflict tidak punya kombinasi planar: {detail}.')
            if combined_plan is not None:
                # The component lattice owns one shared active ROAD axis. Do not
                # restore each member's pre-conflict segment mapping: that would
                # validate a component cell against a different ROAD and recreate
                # the ID 186 failure.
                combined_side = getattr(self, '_last_strict_component_axis_side', None)
                if combined_side is None:
                    combined_side = plans[0]['side'] if plans else 1
                for plan in plans:
                    for index in plan['indexes']:
                        selected[index] = chosen[index]
                        rotations[index] = lattice_angle
                        segment_by_index[index] = (
                            combined_axis_position,
                            int(combined_side),
                            component_number,
                        )
            else:
                for plan in plans:
                    chosen_angle = plan['angle']
                    option = None
                    for option_position, candidate in enumerate(plan['options']):
                        # Skip comparison if candidate doesn't contain all indexes.
                        try:
                            if all(
                                index in candidate and index in chosen
                                and not isinstance(candidate[index], dict)
                                and not isinstance(chosen[index], dict)
                                and candidate[index].equals(chosen[index])
                                for index in plan['indexes']
                            ):
                                option = candidate
                                chosen_angle = plan.get('option_angles', [plan['angle']])[option_position]
                                break
                        except Exception:
                            continue
                    if option is None:
                        raise ValueError('HP COVER selector kehilangan kepemilikan local corridor.')
                    for index in plan['indexes']:
                        selected[index] = chosen[index]
                        rotations[index] = chosen_angle
                        segment_by_index[index] = (plan['road_position'], int(plan['side']), component_number)
            occupied.extend(chosen.values())
            completed += sum(len(plan['indexes']) for plan in plans)
            self._hp_cover_progress_checkpoint(pipeline_logger, completed, 'road-segment-slots')
        # Final fixpoint: the conflict graph above was built only from first
        # candidates. When a large segment picks a non-first option, its actual
        # cells can overlap sources that never unioned. Merge every positively
        # overlapping final pair into one combined-lattice component and replan
        # until the whole 350-cell selection is disjoint (cap 5 iterations).
        for _fixpoint_round in range(5):
            overlap_pairs = []
            ids_order = sorted(selected)
            for left_pos, left_id in enumerate(ids_order):
                for right_id in ids_order[left_pos + 1:]:
                    if (
                        left_id not in self._last_label_only_indexes
                        or right_id not in self._last_label_only_indexes
                    ) and self._strict_positive_overlap(selected[left_id], selected[right_id]):
                        overlap_pairs.append((left_id, right_id))
            if not overlap_pairs:
                break
            affected = sorted({index for pair in overlap_pairs for index in pair})
            for index in affected:
                del selected[index]
                del rotations[index]
                segment_by_index.pop(index, None)
            other_ids = [index for index in indexes if index not in affected]
            outer_obstacles = [selected[index] for index in other_ids]
            lattice = None
            lattice_angle = None
            for road_position in sorted({segment['road_position'] for segment in segments}):
                for side in (-1.0, 1.0):
                    angle, options = self._strict_segment_slot_options(
                        tuple(sorted(affected)), sites, roads[road_position], side,
                        clip_region, fill_region, base_obstacles=outer_obstacles, candidate_limit=1,
                    )
                    if not options:
                        continue
                    if any(
                        self._strict_positive_overlap(left, right)
                        for right_index, right in options[0].items()
                        for left_index, left in options[0].items()
                        if left_index < right_index
                    ):
                        continue
                    lattice = options[0]
                    lattice_angle = angle
                    break
                if lattice is not None:
                    break
            if lattice is None:
                fallback_ids = tuple(sorted(affected))
                lattice_angle, lattice = self._guaranteed_final_4x8_repack(
                    fallback_ids, sites, roads, base_obstacles=outer_obstacles,
                )
                pipeline_log(
                    pipeline_logger, 'warning',
                    f"[FINAL 4×8] fixpoint repacked {len(fallback_ids)} HP COVER; "
                    "ROAD/overlap tetap nol.",
                )
            for index, parcel in lattice.items():
                selected[index] = parcel
                rotations[index] = lattice_angle
                segment_by_index[index] = (road_position, int(side), 0)
            self._hp_cover_progress_checkpoint(
                pipeline_logger, len(selected), 'road-fixpoint-round'
            )
        if set(selected) != set(indexes):
            raise ValueError('HP COVER ROAD segment planner kehilangan source ID.')

        # Any legacy micro-marker produced while resolving junction conflicts is
        # replaced before validation/render. Final DXF therefore has one real
        # 4×8 box for every HP COVER and no label-only geometry.
        if self._last_label_only_indexes:
            fallback_ids = tuple(sorted(self._last_label_only_indexes))
            protected = list(fixed) + [
                parcel for index, parcel in selected.items()
                if index not in self._last_label_only_indexes
            ]
            fallback_angle, fallback_cells = self._guaranteed_final_4x8_repack(
                fallback_ids, sites, roads, base_obstacles=protected,
            )
            for index, parcel in fallback_cells.items():
                selected[index] = parcel
                rotations[index] = fallback_angle
            self._last_label_only_indexes.clear()

        # Preserve the exact planner tier; do not widen 4×10 into the obsolete
        # 6×10 visual-only size.
        road_solid = self._home_parcel_road_solid()
        for index, parcel in selected.items():
            # Explicit label-only fallback parcel is allowed to sit on/overlap ROAD
            # and be farther than 12m; it is drawn only as a label anchor.
            if index in self._last_label_only_indexes:
                continue
            source = ShapelyPoint(sites[index])
            # Match the active tier used by recovery: moderate/ extreme
            # relocation is legal only when the source is far from ROAD.
            road_position = segment_by_index.get(index, (None,))[0]
            active_roads = [roads[road_position]] if road_position is not None else roads
            max_relocation = self._strict_relocation_limit(source, active_roads)
            if (
                index not in self._last_relaxed_4x8_indexes
                and parcel.distance(source) > max_relocation + 1e-6
            ):
                raise ValueError(
                    f'HP COVER ROAD segment relokasi >{max_relocation:g}m: ID {index}.'
                )
            if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                raise ValueError(f'HP COVER ROAD segment overlap ROAD: ID {index}.')
        if any(
            self._strict_positive_overlap(left, right)
            for right_index, right in selected.items()
            for left_index, left in selected.items()
            if left_index < right_index and left_index not in self._last_label_only_indexes and right_index not in self._last_label_only_indexes
        ):
            raise ValueError('HP COVER ROAD segment planner menghasilkan final cell overlap.')
        if pipeline_logger is not None:
            pipeline_log(
                pipeline_logger,
                'info',
                f'[3/6] HP COVER {axis_source} slot plan: {len(segments)} segment, {len(selected)} cell.',
            )
        return selected, rotations, segment_by_index

    def _strict_global_hp_plan(
        self, subblocks, sites, clip_region, fill_region, base_obstacles=None,
        pipeline_logger=None,
    ):
        """Fast strict plan: AI only for dense blocks; expand candidates only on conflict.

        Every initial local block produces one full deterministic option. Positive
        overlap graph decides which blocks need alternatives. This avoids a 48-way
        Cartesian/MRV search across unrelated singleton corridors.
        """
        fixed_obstacles = [
            parcel for parcel in (base_obstacles or [])
            if parcel is not None and not parcel.is_empty
        ]
        plans = []
        planned = 0
        expected = set()
        for block in subblocks:
            indexes = tuple(sorted(block))
            if not indexes or expected.intersection(indexes):
                raise ValueError("HP COVER local corridor membership invalid.")
            expected.update(indexes)
            # Fast pass is deterministic for every corridor. AI is deliberately
            # deferred until a dense/conflicting component needs a shared shift;
            # never fan out one vision call per harmless singleton/pair.
            angle, options = self._strict_strip_options(
                indexes, sites, clip_region, fill_region,
                base_obstacles=fixed_obstacles,
                candidate_limit=1,
            )
            ai_hint = None
            initial_internal_overlap = bool(options) and any(
                self._strict_positive_overlap(left, right)
                for right_index, right in options[0].items()
                for left_index, left in options[0].items()
                if left_index < right_index
            )
            if (not options or initial_internal_overlap) and len(indexes) >= 2:
                # A genuinely dense *local* corridor may need stacked parking bays.
                # Keep this local; never merge a broad overlap chain into one grid.
                angle, options = self._strict_strip_options(
                    indexes, sites, clip_region, fill_region,
                    base_obstacles=fixed_obstacles,
                    candidate_limit=1, dense_repack=True,
                )
            if not options and len(indexes) >= 2:
                # Last local-only strict fallback for a dense corridor whose ROAD
                # strip rows cannot fit without overlap. Still one 4x10 parking
                # lattice, one label per cell, no compact/label-only geometry.
                lattice_angle, lattice_options = self._strict_component_lattice_options(
                    indexes,
                    sites,
                    clip_region,
                    fill_region,
                    base_obstacles=fixed_obstacles,
                    candidate_limit=1,
                )
                if lattice_options:
                    angle, options = lattice_angle, lattice_options
            if not options:
                raise ValueError(
                    f"HP COVER shared ROAD strip tidak lengkap: 0/{len(indexes)} parcel valid; "
                    f"IDs {list(indexes)[:12]}. Tidak membuat compact/label-only fallback."
                )
            plans.append({
                "indexes": indexes,
                "angle": angle,
                "hint": ai_hint,
                "options": [options[0]],
            })
            planned += len(indexes)
            self._hp_cover_progress_checkpoint(pipeline_logger, planned, "planning")

        # Initial candidates are cheap. Build an overlap graph by local block.
        parents = list(range(len(plans)))
        def find(value):
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value
        def union(left, right):
            left, right = find(left), find(right)
            if left != right:
                parents[right] = left
        def plans_overlap(left, right):
            return any(
                self._strict_positive_overlap(first, second)
                for first in left["options"][0].values()
                for second in right["options"][0].values()
            )

        conflicted = set()
        for right in range(len(plans)):
            for left in range(right):
                if plans_overlap(plans[left], plans[right]):
                    union(left, right)
                    conflicted.add(left)
                    conflicted.add(right)

        selected = {
            index: parcel
            for plan in plans for index, parcel in plan["options"][0].items()
        }
        rotations = {
            index: plan["angle"]
            for plan in plans for index in plan["indexes"]
        }
        if not conflicted:
            return selected, rotations

        components = {}
        for position in conflicted:
            components.setdefault(find(position), []).append(position)
        for positions in components.values():
            position_set = set(positions)
            outside = [
                parcel
                for position, plan in enumerate(plans)
                if position not in position_set
                for parcel in plan["options"][0].values()
            ]
            component_plans = []
            for position in sorted(positions):
                original_plan = plans[position]
                indexes = original_plan["indexes"]
                # Only a block participating in positive-area collision gets extra
                # choices and an AI hint. Singleton/non-conflicting corridors never
                # invoke visual AI or a global candidate search.
                if len(indexes) >= 2:
                    angle, options, hint = self._strict_ai_strip_options(
                        indexes,
                        sites,
                        clip_region,
                        fill_region,
                        pipeline_logger=pipeline_logger,
                        base_obstacles=fixed_obstacles + outside,
                        candidate_limit=6,
                        dense_repack=True,
                    )
                else:
                    angle, options = self._strict_strip_options(
                        indexes,
                        sites,
                        clip_region,
                        fill_region,
                        base_obstacles=fixed_obstacles + outside,
                        candidate_limit=6,
                    )
                    hint = None
                if not options and len(indexes) >= 2:
                    # Conflict retry may block an otherwise valid local strip. Retry
                    # the same local members as a strict planar parking lattice; do
                    # not merge this component with its neighbours.
                    lattice_angle, lattice_options = self._strict_component_lattice_options(
                        indexes,
                        sites,
                        clip_region,
                        fill_region,
                        base_obstacles=fixed_obstacles + outside,
                        candidate_limit=6,
                    )
                    if lattice_options:
                        angle, options, hint = lattice_angle, lattice_options, None
                if not options:
                    raise ValueError(
                        "HP COVER conflict corridor tidak menemukan opsi lokal legal; "
                        f"IDs {list(indexes)[:24]}. Tidak membuat compact/label-only fallback."
                    )
                component_plans.append({
                    "indexes": indexes,
                    "angle": angle,
                    "hint": hint,
                    "options": options,
                })

            component_cells = self._select_strict_strip_candidates(
                component_plans, base_obstacles=fixed_obstacles + outside,
            )
            component_expected = {
                index for plan in component_plans for index in plan["indexes"]
            }
            lattice_angle = None
            if component_cells is None:
                # Perpendicular valid local strips can still collide at a junction.
                # Rebuild only that connected conflict as one planar 4x10 lattice.
                lattice_angle, lattice_options = self._strict_component_lattice_options(
                    tuple(sorted(component_expected)),
                    sites,
                    clip_region,
                    fill_region,
                    base_obstacles=fixed_obstacles + outside,
                    candidate_limit=1,
                )
                component_cells = lattice_options[0] if lattice_options else None
            if component_cells is None:
                component_ids = tuple(sorted(component_expected))
                raise ValueError(
                    "HP COVER conflict corridor tidak menemukan kombinasi planar legal; "
                    f"IDs {list(component_ids)[:24]}. Tidak membuat compact/label-only fallback."
                )
            if set(component_cells) != component_expected:
                raise ValueError("HP COVER conflict corridor kehilangan source ID.")
            if any(
                self._strict_positive_overlap(left, right)
                for right_index, right in component_cells.items()
                for left_index, left in component_cells.items()
                if left_index < right_index
            ):
                raise ValueError("HP COVER conflict corridor menghasilkan cell overlap.")

            if lattice_angle is not None:
                rotations.update({index: lattice_angle for index in component_expected})
                if pipeline_logger is not None:
                    pipeline_log(
                        pipeline_logger, "info",
                        f"[3/6] HP COVER conflict lattice: {len(component_expected)} HP, axis {lattice_angle:.1f}°.",
                    )
            else:
                for plan in component_plans:
                    matching = next(
                        (
                            option for option in plan["options"]
                            if all(
                                component_cells[index].equals_exact(option[index], 1e-7)
                                for index in plan["indexes"]
                            )
                        ),
                        None,
                    )
                    if matching is None:
                        raise ValueError("HP COVER selector kehilangan kepemilikan local corridor.")
                    rotations.update({index: plan["angle"] for index in plan["indexes"]})
            selected.update(component_cells)
            self._hp_cover_progress_checkpoint(
                pipeline_logger,
                sum(len(plan["indexes"]) for plan in component_plans),
                "conflict-lattice" if lattice_angle is not None else "conflict-selection",
            )
        if set(selected) != expected:
            raise ValueError("HP COVER global plan kehilangan source ID.")
        if any(
            self._strict_positive_overlap(left, right)
            for right_index, right in selected.items()
            for left_index, left in selected.items()
            if left_index < right_index
        ):
            raise ValueError("HP COVER global plan menghasilkan final cell overlap.")
        return selected, rotations

    def _strict_component_lattice_options(
        self, indexes, sites, clip_region, fill_region, base_obstacles=None,
        candidate_limit=1, angle_override=None, layout_hint=None, axis_lines=None,
    ):
        """Return the largest complete legal lattice tier for a dense corridor."""
        # Preserve the exact axis object selected by the lattice builder. The
        # caller must carry that identity into final relocation validation.
        self._last_strict_component_axis_line = None
        self._last_strict_component_axis_side = None
        last_angle = 0.0
        for parcel_size in HP_COVER_PARCEL_SIZE_TIERS:
            last_angle, options = self._strict_component_lattice_options_for_size(
                indexes, sites, clip_region, fill_region,
                base_obstacles=base_obstacles,
                candidate_limit=candidate_limit,
                angle_override=angle_override,
                layout_hint=layout_hint,
                parcel_size=parcel_size,
                axis_lines=axis_lines,
            )
            if options:
                return last_angle, options
        return last_angle, []

    def _strict_component_lattice_options_for_size(
        self, indexes, sites, clip_region, fill_region, base_obstacles=None,
        candidate_limit=1, angle_override=None, layout_hint=None,
        parcel_size=(4.0, 10.0), axis_lines=None,
    ):
        """Build one dense-corridor lattice using an exact policy tier."""
        indexes = tuple(sorted(indexes))
        if not indexes:
            return 0.0, []
        roads = list(axis_lines) if axis_lines is not None else self._hp_cover_axis_lines(indexes, sites)[0]
        if not roads:
            return 0.0, []
        source_points = [ShapelyPoint(sites[index]) for index in indexes]
        reference = MultiPoint(source_points).centroid
        road = min(roads, key=lambda line: line.distance(reference))
        self._last_strict_component_axis_line = road
        angle = self._polygon_label_rotation(clip_region) if clip_region is not None and not clip_region.is_empty else 0.0
        if road.distance(reference) <= 36.0:
            position = road.project(reference)
            epsilon = min(2.0, max(.25, road.length * .01))
            point_a = road.interpolate(max(0.0, position - epsilon))
            point_b = road.interpolate(min(road.length, position + epsilon))
            dx, dy = point_b.x - point_a.x, point_b.y - point_a.y
            if dx * dx + dy * dy > 1e-9:
                angle = math.degrees(math.atan2(dy, dx))
        if angle_override is not None:
            try:
                angle = float(angle_override)
            except (TypeError, ValueError):
                pass
        angle = self._readable_angle(angle)
        radians = math.radians(angle)
        tx, ty = math.cos(radians), math.sin(radians)
        base_nx, base_ny = -ty, tx
        road_at = road.interpolate(road.project(reference))
        signed = (reference.x - road_at.x) * base_nx + (reference.y - road_at.y) * base_ny
        side = 1.0 if signed >= 0 else -1.0
        hint = dict(layout_hint or {})
        try:
            requested_side = float(hint.get("road_side", 0.0) or 0.0)
        except (TypeError, ValueError):
            requested_side = 0.0
        if requested_side in (-1.0, 1.0):
            side = requested_side
        self._last_strict_component_axis_side = side
        nx, ny = base_nx * side, base_ny * side
        entries = [
            (index, point, point.x * tx + point.y * ty, point.x * nx + point.y * ny)
            for index, point in zip(indexes, source_points)
        ]
        road_solid = self._home_parcel_road_solid()
        obstacles = [parcel for parcel in (base_obstacles or []) if parcel is not None and not parcel.is_empty]
        clip_cover = clip_region.buffer(1e-6) if fill_region and clip_region is not None and not clip_region.is_empty else None
        try:
            width, depth = (float(value) for value in parcel_size)
        except (TypeError, ValueError):
            return angle, []
        if (width, depth) not in HP_COVER_PARCEL_SIZE_TIERS:
            return angle, []
        min_u, max_u = min(item[2] for item in entries), max(item[2] for item in entries)
        min_v, max_v = min(item[3] for item in entries), max(item[3] for item in entries)
        front_values = []
        for _index, point, _u, _v in entries:
            try:
                edge = nearest_points(point, road_solid.boundary)[1] if road_solid is not None and not road_solid.is_empty else road.interpolate(road.project(point))
            except Exception:
                edge = road.interpolate(road.project(point))
            front_values.append(edge.x * nx + edge.y * ny)
        road_base_v = max(front_values) if front_values else min_v - depth / 2.0
        if min_v - road_base_v > 13.0:
            road_base_v = min_v - depth / 2.0
        # ROAD-front first. If that cannot cover every source within the immutable
        # 12m presentation limit, a centered local parking lattice is still ROAD-
        # oriented and remains hard-excluded from the actual ROAD footprint.
        base_v_choices = [road_base_v, min_v - depth / 2.0, min_v - depth]
        base_u = math.floor((min_u - width) / width) * width
        column_count = max(1, int(math.ceil((max_u - base_u) / width)) + 2)
        best = None

        for base_v in dict.fromkeys(round(value, 6) for value in base_v_choices):
            row_count = max(1, int(math.ceil((max_v - base_v) / depth)) + 2)
            slots = []
            for row_number in range(row_count):
                for column_number in range(column_count):
                    left = base_u + column_number * width
                    bottom = base_v + row_number * depth
                    def xy(u, v):
                        return (tx * u + nx * v, ty * u + ny * v)
                    cell = Polygon([
                        xy(left, bottom), xy(left + width, bottom),
                        xy(left + width, bottom + depth), xy(left, bottom + depth),
                    ])
                    if road_solid is not None and not road_solid.is_empty and cell.intersection(road_solid).area > 1e-7:
                        continue
                    if clip_cover is not None and not clip_cover.covers(cell):
                        continue
                    if any(cell.intersection(obstacle).area > 1e-7 for obstacle in obstacles):
                        continue
                    slots.append(cell)
            if len(slots) < len(entries):
                continue

            choices = {}
            for source_position, (_index, point, _u, _v) in enumerate(entries):
                choices[source_position] = [
                    slot_position for _distance, slot_position in sorted(
                        (point.distance(cell), slot_position)
                        for slot_position, cell in enumerate(slots)
                        if point.distance(cell) <= 12.0 + 1e-6
                    )
                ]
                if not choices[source_position]:
                    choices = {}
                    break
            if not choices:
                continue

            # Deterministic augmenting-path matching. This is polynomial and runs
            # only inside one failed local corridor, never over all 121 blocks.
            slot_owner = {}
            def claim(source_position, seen):
                for slot_position in choices[source_position]:
                    if slot_position in seen:
                        continue
                    seen.add(slot_position)
                    prior = slot_owner.get(slot_position)
                    if prior is None or claim(prior, seen):
                        slot_owner[slot_position] = source_position
                        return True
                return False
            ordered = sorted(choices, key=lambda position: (len(choices[position]), entries[position][0]))
            if not all(claim(source_position, set()) for source_position in ordered):
                continue
            option = {
                entries[source_position][0]: slots[slot_position]
                for slot_position, source_position in slot_owner.items()
            }
            if len(option) != len(entries):
                continue
            distances = [entries[position][1].distance(option[entries[position][0]]) for position in range(len(entries))]
            score = (max(distances), sum(distances), abs(base_v - road_base_v))
            if best is None or score < best[0]:
                best = (score, option)
        return angle, [best[1]] if best is not None else []

    def _select_hp_local_candidates(self, plans, fixed):
        """Cache candidate conflicts once; solve domains with bitsets, not repeated overlays."""
        from shapely import area as geometry_area, intersection as geometry_intersection
        tokens = []
        domains = []
        for plan in plans:
            domain = 0
            for option in plan["options"]:
                if set(option) != set(plan["indexes"]):
                    return None
                parcels = list(option.values())
                if any(p is None or p.is_empty or not p.is_valid for p in parcels):
                    return None
                if any(self._strict_positive_overlap(p, q) for i, p in enumerate(parcels) for q in parcels[i + 1:]):
                    continue
                if any(self._strict_positive_overlap(p, q) for p in parcels for q in fixed):
                    continue
                domain |= 1 << len(tokens)
                tokens.append(option)
            if not domain:
                return None
            domains.append(domain)
        geometries = [p for option in tokens for p in option.values()]
        token_by_geometry = [n for n, option in enumerate(tokens) for _ in option.values()]
        plan_by_token = [n for n, domain in enumerate(domains) for _ in range(domain.bit_count())]
        tree = STRtree(geometries)
        conflicts = [0] * len(tokens)
        neighbours = [set() for _ in plans]
        # Native installed Shapely vector overlay, same positive-area oracle1e-7.
        for geometry_index, p in enumerate(geometries):
            left = token_by_geometry[geometry_index]
            # STRtree position, not object identity: options may reuse one Polygon.
            near = [int(j) for j in tree.query(p) if token_by_geometry[int(j)] > left
                    and plan_by_token[token_by_geometry[int(j)]] != plan_by_token[left]]
            if not near:
                continue
            areas = geometry_area(geometry_intersection(p, [geometries[j] for j in near]))
            for j, value in zip(near, areas):
                if value <= 1e-7:
                    continue
                right = token_by_geometry[j]
                conflicts[left] |= 1 << right
                conflicts[right] |= 1 << left
                a, b = plan_by_token[left], plan_by_token[right]
                neighbours[a].add(b)
                neighbours[b].add(a)
        def bits(value):
            while value:
                bit = value & -value
                yield bit.bit_length() - 1
                value ^= bit
        def propagate(current, changed):
            todo = list(changed)
            while todo:
                left = todo.pop()
                for right in neighbours[left]:
                    before = current[right]
                    legal = 0
                    for token in bits(before):
                        if current[left] & ~conflicts[token]:
                            legal |= 1 << token
                    if not legal:
                        return False
                    if legal != before:
                        current[right] = legal
                        todo.append(right)
            return True
        nodes = 0
        def solve(current):
            nonlocal nodes
            remaining = [i for i, domain in enumerate(current) if domain.bit_count() > 1]
            if not remaining:
                return current
            if nodes >= 500000:
                return None
            index = min(remaining, key=lambda i: (current[i].bit_count(), -len(neighbours[i]), min(plans[i]["indexes"])))
            for token in bits(current[index]):
                nodes += 1
                chosen = list(current)
                chosen[index] = 1 << token
                if propagate(chosen, [index]):
                    result = solve(chosen)
                    if result is not None:
                        return result
                if nodes >= 500000:
                    break
            return None
        current = list(domains)
        result = solve(current) if propagate(current, range(len(plans))) else None
        self._hp_candidate_selector_stats = {"plans": len(plans), "options": len(tokens), "nodes": nodes,
                                             "success": result is not None, "cached_geometry_count": len(geometries)}
        if result is None:
            return None
        selected = {}
        for domain in result:
            selected.update(tokens[domain.bit_length() - 1])
        return selected if set(selected) == {i for plan in plans for i in plan["indexes"]} else None

    def _select_strict_strip_candidates(self, candidate_sets, base_obstacles=None):
        """Select local strip options without positive-area overlap."""
        fixed = [parcel for parcel in (base_obstacles or []) if parcel is not None and not parcel.is_empty]
        plans = list(candidate_sets)
        if not plans or any(not plan["options"] for plan in plans):
            return None
        if STRtree is not None and all(len(plan["indexes"]) == 1 for plan in plans):
            return self._select_hp_local_candidates(plans, fixed)
        selected = []
        nodes = 0
        node_budget = 500000

        def compatible(option, occupied):
            return not any(
                self._strict_positive_overlap(parcel, other)
                for parcel in option.values() for other in occupied
            )

        def search(remaining, occupied):
            nonlocal nodes
            if not remaining:
                return True
            if nodes >= node_budget:
                return False
            ranked = []
            for position, plan in enumerate(remaining):
                legal = [option for option in plan["options"] if compatible(option, occupied)]
                if not legal:
                    return False
                ranked.append((len(legal), len(plan["indexes"]), min(plan["indexes"]), position, legal))
            ranked.sort(key=lambda item: item[:4])
            _count, _size, _first, position, legal = ranked[0]
            plan = remaining[position]
            rest = remaining[:position] + remaining[position + 1:]
            for option in legal:
                nodes += 1
                selected.append((plan, option))
                if search(rest, occupied + list(option.values())):
                    return True
                selected.pop()
            return False

        if not search(plans, fixed):
            return None
        result = {}
        for _plan, option in selected:
            for index, parcel in option.items():
                if index in result:
                    return None
                result[index] = parcel
        expected = {index for plan in plans for index in plan["indexes"]}
        return result if set(result) == expected else None

    @staticmethod
    def _merge_dense_parcel_blocks(parcels, rotation_by_index=None):
        """Dissolve only HP cells with positive-area overlap.

        Edge-touching parking bays stay individual. Overlapping dense cells become
        one clean HP COVER block while each source ID keeps its own label.
        """
        items = [
            (index, parcel) for index, parcel in parcels.items()
            if parcel is not None and not parcel.is_empty
        ]
        parent = list(range(len(items)))

        def find(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left, right):
            left, right = find(left), find(right)
            if left != right:
                parent[right] = left

        for left, (_left_id, left_parcel) in enumerate(items):
            for right in range(left):
                right_index, right_parcel = items[right]
                if rotation_by_index:
                    left_angle = float(rotation_by_index.get(_left_id, 0.0))
                    right_angle = float(rotation_by_index.get(right_index, 0.0))
                    axis_delta = abs(((left_angle - right_angle + 90.0) % 180.0) - 90.0)
                    if axis_delta > 5.0:
                        continue
                intersection = left_parcel.intersection(right_parcel)
                # Shared edges are valid parking-bay dividers; merge only
                # genuine positive-area overlap. ponytail: no outer-block
                # dissolve for edge-touching cells; keep per-HP separators.
                if intersection.area > 1e-7:
                    union(left, right)

        groups = {}
        for position, (index, parcel) in enumerate(items):
            groups.setdefault(find(position), []).append((index, parcel))
        blocks = []
        for members in groups.values():
            member_ids = tuple(sorted(index for index, _parcel in members))
            block = unary_union([parcel for _index, parcel in members]).buffer(0)
            if block.is_empty or not isinstance(block, Polygon):
                raise ValueError(f"HP COVER dense block invalid: IDs {member_ids[:12]}")
            blocks.append((member_ids, block))
        return blocks

    def _commit_strict_strip_parcels(
        self, indexes, strip_parcels, site_points, pending, box_attrs, text_attrs,
        rotation_by_index=None, centers_by_index=None,
    ):
        """Draw prevalidated cells; ROAD-only center and visible label cap is 8m."""
        missing = [index for index in indexes if index not in strip_parcels]
        if missing:
            raise ValueError(f"HP COVER global ROAD plan missing IDs: {missing[:12]}")
        road_solid = self._home_parcel_road_solid()
        rotation_by_index = rotation_by_index or {}
        selected = {index: strip_parcels[index] for index in indexes}
        centers_by_index = centers_by_index or {}
        relief_contexts = self._hp_same_side_relief_contexts(pending)
        for index, parcel in selected.items():
            item = pending[index]
            source = ShapelyPoint(item["projected"])
            center = ShapelyPoint(centers_by_index.get(index, item["projected"]))
            shift = center.distance(source)
            if shift > 8.0 + 1e-7:
                raise ValueError(f"HP ROAD-only center shift exceeds8m: ID={index}, shift={shift:g}m")
            if shift > 1e-6 and (item.get("hp_uncover") or index not in relief_contexts):
                raise ValueError(f"HP center shifted without local ROAD/density relief permission: ID={index}")
            if shift > 1e-6 and not self._hp_center_stays_on_side((center.x, center.y), relief_contexts[index]):
                raise ValueError(f"HP local relief crossed its source ROAD side: ID={index}")
            if shift > 1e-6 and not self._hp_relief_path_clear(item["projected"], (center.x, center.y), road_solid):
                raise ValueError(f"HP local relief reentered/crossed ROAD: ID={index}")
            if parcel is None or parcel.is_empty or parcel.centroid.distance(center) > 1e-6:
                raise ValueError(f"HP parcel center does not match visible label center: ID={index}")
            site = site_points[index]
            if parcel is None or parcel.is_empty:
                raise ValueError(f"HP COVER global ROAD plan parcel {index} kosong.")
            # Explicit label-only fallback: skip normal ROAD/relocation gates.
            if index in getattr(self, "_last_label_only_indexes", set()):
                continue
            if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                raise ValueError(f"HP COVER parcel {index} overlap ROAD setelah global plan.")
            if (
                index not in getattr(self, "_last_relaxed_4x8_indexes", set())
                and not parcel.buffer(1e-6).covers(site)
            ):
                road_lines = [
                    line for line in getattr(
                        self, "_last_hp_relocation_roads", self.road_centerlines
                    )
                    if line is not None and not line.is_empty
                ]
                max_relocation = self._strict_relocation_limit(site, road_lines)
                if parcel.distance(site) > max_relocation + 1e-6:
                    raise ValueError(
                        f"HP COVER parcel {index} melewati batas relokasi {max_relocation:g}m."
                    )

        blocks = self._merge_dense_parcel_blocks(selected, rotation_by_index=rotation_by_index)
        staged = []
        drawn = 0
        for member_ids, block in blocks:
            # Explicit final label-only fallback: no parcel/separator geometry.
            # Emit one text anchor per failed HP, then continue.
            if member_ids and all(index in getattr(self, "_last_label_only_indexes", set()) for index in member_ids):
                for index in member_ids:
                    item = pending[index]
                    anchor = site_points[index]
                    rotation = 0.0
                    label_start = len(self.output_entities)
                    item_text_attrs = (
                        {"layer": KML_UNCOVER_LAYER, "color": KML_UNCOVER_COLOR, "style": "EVA_BOLD"}
                        if item.get("hp_uncover") else text_attrs
                    )
                    if item.get("hp_uncover"):
                        self._ensure_layer(KML_UNCOVER_LAYER, color=KML_UNCOVER_COLOR)
                    self._add_centered_text(
                        str(item["text"]), (anchor.x, anchor.y), KML_HOME_NUMBER_HEIGHT_METERS,
                        dxfattribs=item_text_attrs, rotation=rotation,
                    )
                    self.register_fdt_entities(
                        item.get("fdt_number"), self.output_entities[label_start:],
                    )
                    self._hp_cover_drawn_indexes.add(index)
                    drawn += 1
                continue
            if road_solid is not None and not road_solid.is_empty and block.intersection(road_solid).area > 1e-7:
                raise ValueError(f"HP COVER dense block overlap ROAD: IDs {member_ids[:12]}")
            if any(self._strict_positive_overlap(block, old) for old in self.home_number_boxes + staged):
                raise ValueError(f"HP COVER dense block overlap: IDs {member_ids[:12]}")
            has_uncover = any(pending[index].get("hp_uncover") for index in member_ids)
            if has_uncover:
                self._ensure_layer(KML_UNCOVER_LAYER, color=KML_UNCOVER_COLOR)
            block_start = len(self.output_entities)
            if not has_uncover:
                self._add_polyline(list(block.exterior.coords), close=True, dxfattribs=box_attrs)
            block_entities = self.output_entities[block_start:]
            self.home_number_boxes.append(block)
            self.home_number_box_uncover.append(all(pending[index].get("hp_uncover") for index in member_ids))
            staged.append(block)
            separator_entities = {}
            separator_attrs = dict(box_attrs)
            if any(not pending[index].get("hp_uncover") for index in member_ids):
                separator_attrs["layer"] = self._ensure_layer(
                    "HP_COVER_SEPARATOR", color=int(separator_attrs.get("color", 7) or 7)
                )
            for index in member_ids:
                item = pending[index]
                self._record_home_number_cell(selected[index], item)
                cell_start = len(self.output_entities)
                if has_uncover:
                    cell_attrs = (
                        {"layer": KML_UNCOVER_LAYER, "color": KML_UNCOVER_COLOR}
                        if item.get("hp_uncover") else box_attrs
                    )
                    self._add_polyline(list(selected[index].exterior.coords), close=True, dxfattribs=cell_attrs)
                if not item.get("hp_uncover"):
                    self._add_polyline(
                        list(selected[index].exterior.coords), close=True,
                        dxfattribs=separator_attrs,
                    )
                separator_entities[index] = self.output_entities[cell_start:]
            for index in member_ids:
                item = pending[index]
                anchor = ShapelyPoint(centers_by_index.get(index, item["projected"]))
                if not selected[index].buffer(1e-6).covers(anchor):
                    raise ValueError(f"HP fixed-anchor label outside own cell: ID={index}")
                rotation = self._polygon_label_rotation(selected[index])
                label_start = len(self.output_entities)
                item_text_attrs = (
                    {"layer": KML_UNCOVER_LAYER, "color": KML_UNCOVER_COLOR, "style": "EVA_BOLD"}
                    if item.get("hp_uncover") else text_attrs
                )
                self._add_centered_text(
                    str(item["text"]), (anchor.x, anchor.y), KML_HOME_NUMBER_HEIGHT_METERS,
                    dxfattribs=item_text_attrs, rotation=rotation,
                )
                self.register_fdt_entities(
                    item.get("fdt_number"),
                    block_entities + separator_entities.get(index, []) + self.output_entities[label_start:],
                )
                self._hp_cover_drawn_indexes.add(index)
                drawn += 1
        return drawn

    @staticmethod
    def _shared_cell_dividers(values, minimum=4.0, maximum=13.0, epsilon=1e-5):
        """Return shared cell dividers owning every sorted source coordinate.

        Midpoints are preferred, but constraints win: each interval is 4–13m and
        every source stays inside its one parking-bay cell.
        """
        values = [float(value) for value in values]
        if not values:
            return []
        if any(right - left <= 2.0 * epsilon for left, right in zip(values, values[1:])):
            return None
        count = len(values)
        domains = [(values[0] - maximum, values[0] - epsilon)]
        domains.extend((left + epsilon, right - epsilon) for left, right in zip(values, values[1:]))
        domains.append((values[-1] + epsilon, values[-1] + maximum))
        lower = [0.0] * (count + 1)
        upper = [0.0] * (count + 1)
        lower[0], upper[0] = domains[0]
        for position in range(1, count + 1):
            domain_low, domain_high = domains[position]
            lower[position] = max(domain_low, lower[position - 1] + minimum)
            upper[position] = min(domain_high, upper[position - 1] + maximum)
            if lower[position] > upper[position] + 1e-8:
                return None
        dividers = [0.0] * (count + 1)
        dividers[-1] = min(max(values[-1] + minimum / 2.0, lower[-1]), upper[-1])
        for position in range(count - 1, -1, -1):
            domain_low, domain_high = domains[position]
            target = values[position] - minimum / 2.0 if position == 0 else (values[position - 1] + values[position]) / 2.0
            low = max(domain_low, dividers[position + 1] - maximum)
            high = min(domain_high, dividers[position + 1] - minimum)
            if low > high + 1e-8:
                return None
            dividers[position] = min(max(target, low), high)
        if any(
            right - left < minimum - 1e-6 or right - left > maximum + 1e-6
            for left, right in zip(dividers, dividers[1:])
        ):
            return None
        if any(not (left + epsilon >= divider_left and right - epsilon <= divider_right)
               for left, right, divider_left, divider_right in zip(values, values, dividers, dividers[1:])):
            return None
        return dividers

    def _dense_conflict_grid_slots(self, indexes, source_points, tx, ty, nx, ny):
        """Return a shared-edge 4x10 parking grid for one dense conflict component.

        This is not a compact fallback. It is the strict multi-row form of a ROAD
        strip when one source row cannot physically hold every 4m-wide bay.
        """
        count = len(indexes)
        if count < 2:
            return []
        width, depth = 4.0, 10.0
        entries = [
            (index, point, point.x * tx + point.y * ty, point.x * nx + point.y * ny)
            for index, point in zip(indexes, source_points)
        ]
        mean_u = sum(item[2] for item in entries) / count
        min_v = min(item[3] for item in entries)
        best = None

        for row_count in range(2, min(4, count) + 1):
            column_count = int(math.ceil(count / row_count))
            start_u = mean_u - column_count * width / 2.0
            # Front-anchor first row beside the nearest source/ROAD side. Never
            # center stacked rows around sources: that puts lower bays through ROAD
            # and forces an impossible whole-grid shift.
            for start_v in (min_v - depth / 2.0, min_v - depth, min_v):
                slots = []
                for row in range(row_count):
                    for column in range(column_count):
                        left = start_u + column * width
                        bottom = start_v + row * depth
                        def xy(u, v):
                            return (tx * u + nx * v, ty * u + ny * v)
                        slots.append(Polygon([
                            xy(left, bottom), xy(left + width, bottom),
                            xy(left + width, bottom + depth), xy(left, bottom + depth),
                        ]))

                choices = {}
                for source_position, (_index, point, _u, _v) in enumerate(entries):
                    choices[source_position] = [
                        slot_position for _distance, slot_position in sorted(
                            (point.distance(slot), slot_position)
                            for slot_position, slot in enumerate(slots)
                            if point.distance(slot) <= 12.0 + 1e-6
                        )
                    ]
                    if not choices[source_position]:
                        choices = {}
                        break
                if not choices:
                    continue

                # Deterministic Kuhn matching. Components are capped by local conflict
                # topology; no global exponential candidate search remains.
                owner = {}
                def claim(source_position, seen):
                    for slot_position in choices[source_position]:
                        if slot_position in seen:
                            continue
                        seen.add(slot_position)
                        prior = owner.get(slot_position)
                        if prior is None or claim(prior, seen):
                            owner[slot_position] = source_position
                            return True
                    return False
                ordered = sorted(choices, key=lambda value: (len(choices[value]), entries[value][0]))
                if not all(claim(source_position, set()) for source_position in ordered):
                    continue
                cells = {
                    entries[source_position][0]: slots[slot_position]
                    for slot_position, source_position in owner.items()
                }
                if len(cells) != count:
                    continue
                distances = [
                    entries[source_position][1].distance(cells[entries[source_position][0]])
                    for source_position in range(count)
                ]
                score = (max(distances), sum(distances), row_count * column_count - count, row_count)
                if best is None or score < best[0]:
                    best = (score, cells)
        if best is None:
            return []
        return [
            (index, point, best[1][index])
            for index, point, _u, _v in entries
        ]

    def _road_strip_cells(self, indexes, sites, clip_region, angle, layout_hint=None, require_clip=True, obstacles=None, candidate_limit=1, return_candidates=False, dense_repack=False):
        """Strict ROAD-front shared parcels. No compact/Voronoi/label fallback."""
        if not indexes:
            return {}
        roads = [road for road in self.road_centerlines if road is not None and not road.is_empty and road.length > 2]
        if not roads:
            return {}
        hint = layout_hint or {}
        road_solid = self._home_parcel_road_solid()
        if clip_region is None or clip_region.is_empty:
            clip_region = MultiPoint([ShapelyPoint(sites[index]) for index in indexes]).buffer(
                18.0, cap_style=3, join_style=2
            )
            if road_solid is not None and not road_solid.is_empty:
                clip_region = clip_region.difference(road_solid)

        # Axis is AI-bounded by caller. Choose the normal sign once from the
        # source group versus its nearest ROAD; increasing v always moves out.
        radians = math.radians(float(angle))
        tx, ty = math.cos(radians), math.sin(radians)
        base_nx, base_ny = -ty, tx
        source_points = [ShapelyPoint(sites[index]) for index in indexes]
        center = MultiPoint(source_points).centroid
        road = min(roads, key=lambda candidate: candidate.distance(center))
        road_at = road.interpolate(road.project(center))
        signed = (center.x - road_at.x) * base_nx + (center.y - road_at.y) * base_ny
        side = 1.0 if signed >= 0 else -1.0
        # Global strict planner may request the opposite frontage only when the
        # source-side candidate has zero legal strips (e.g. source lies inside
        # a ROAD footprint). Geometry still validates ROAD/relocation normally.
        requested_side = hint.get("road_side")
        try:
            requested_side = float(requested_side)
        except (TypeError, ValueError):
            requested_side = 0.0
        if requested_side in (-1.0, 1.0):
            side = requested_side
        nx, ny = base_nx * side, base_ny * side

        along_hint = max(-12.0, min(12.0, float(hint.get('along_shift_m', 0.0) or 0.0)))
        normal_hint = max(-12.0, min(12.0, float(hint.get('normal_shift_m', 0.0) or 0.0)))
        hint_size = math.hypot(along_hint, normal_hint)
        if hint_size > 12.0:
            scale = 12.0 / hint_size
            along_hint *= scale
            normal_hint *= scale
        depth = max(10.0, min(13.0, 10.0 * float(hint.get('depth_scale', 1.0) or 1.0)))
        width_cap = max(4.0, min(13.0, 13.0 * float(hint.get('width_scale', 1.0) or 1.0)))

        entries = []
        fronts = []
        edge_v_by_index = {}
        for index, point in zip(indexes, source_points):
            u = point.x * tx + point.y * ty
            v = point.x * nx + point.y * ny
            entries.append((index, point, u, v))
            if road_solid is not None and not road_solid.is_empty:
                try:
                    edge = nearest_points(point, road_solid.boundary)[1]
                except Exception:
                    edge = road.interpolate(road.project(point))
            else:
                edge = road.interpolate(road.project(point))
            edge_v = edge.x * nx + edge.y * ny
            fronts.append(edge_v)
            edge_v_by_index[index] = edge_v
        entries.sort(key=lambda item: (item[3], item[2], item[0]))

        # Road-front rows. First row touches the outward ROAD edge. Later rows
        # share exactly one complete divider with preceding row.
        rows = []
        current = []
        row_min_v = None
        for item in entries:
            if current and item[3] - row_min_v > 7.5:
                rows.append(current)
                current = []
                row_min_v = None
            if row_min_v is None:
                row_min_v = item[3]
            current.append(item)
        if current:
            rows.append(current)

        # Use highest outward road-boundary scalar. It prevents a curved ROAD
        # corner from entering an otherwise legal front-row rectangle.
        # Outward normal increases away from ROAD; nearest ROAD edge is the
        # minimum boundary scalar. Using max(fronts) starts rows behind source
        # anchors on multi-row corridors and breaks the 12m relocation gate.
        # First row starts at the outermost ROAD edge. Using the innermost
        # edge cuts front bays through ROAD on curved/width-varying segments.
        road_front = max(fronts) if fronts else min(item[3] for item in entries)
        # ROAD frontage is impossible when nearest source lies more than one
        # parcel cap beyond ROAD. Keep ROAD tangent as axis, but start a local
        # strip around source anchors. This is a strict ROAD-oriented parcel,
        # not a compact/label-only fallback.
        nearest_source_v = min(item[3] for item in entries)
        local_road_gap = nearest_source_v - road_front
        if local_road_gap > 13.0:
            road_front = nearest_source_v - depth / 2.0
        raw_slots = []
        front = road_front
        for row_number, row in enumerate(rows):
            row = sorted(row, key=lambda item: (item[2], item[0]))
            min_source_v = min(item[3] for item in row)
            max_source_v = max(item[3] for item in row)
            # ROAD frontage belongs to this local row. A single global ROAD
            # edge can be tens of metres away on a curved corridor.
            row_front = max(edge_v_by_index[item[0]] for item in row)
            if min_source_v - row_front > 13.0 + 1e-6:
                row_front = min_source_v - depth / 2.0
            # Later parking rows attach to the previous row's back edge. Resetting
            # to each row's nearest ROAD edge caused positive-area separator overlap.
            front = max(front, row_front)
            # Grow toward source but never exceed 13m. Source may sit just
            # outside shifted DXF parcel when final source-to-parcel distance
            # remains <=12m; KMZ coordinate stays immutable.
            back = min(front + 13.0, max(front + depth, max_source_v + .01))
            runs = []
            run_start = 0
            for pos in range(1, len(row) + 1):
                if pos == len(row) or row[pos][2] - row[pos - 1][2] > 13.0:
                    runs.append(row[run_start:pos])
                    run_start = pos
            for run in runs:
                source_us = [item[2] for item in run]
                # Exact source-owned dividers. Midpoints are only preferred;
                # the interval solver moves dividers asymmetrically when needed
                # to retain every HP in a 4–13m shared-edge cell.
                boundaries = self._shared_cell_dividers(source_us, 4.0, width_cap)
                if boundaries is None:
                    continue
                for position, (index, point, _u, _v) in enumerate(run):
                    left, right = boundaries[position], boundaries[position + 1]
                    def xy(u, v):
                        return (tx * u + nx * v, ty * u + ny * v)
                    raw_slots.append((index, point, Polygon([
                        xy(left, front), xy(right, front), xy(right, back), xy(left, back),
                    ])))
            front = back

        def raw_slots_overlap(slots):
            return any(
                self._strict_positive_overlap(left[2], right[2])
                for right_position, right in enumerate(slots)
                for left in slots[:right_position]
            )

        # A final HP COVER cell must never rely on outer-hull dissolve to hide
        # internal overlap. Repack the entire affected local corridor as one
        # shared-edge parking grid before global selection/commit.
        if len(raw_slots) != len(indexes) or raw_slots_overlap(raw_slots):
            if not dense_repack:
                return {}
            raw_slots = self._dense_conflict_grid_slots(indexes, source_points, tx, ty, nx, ny)
            if len(raw_slots) != len(indexes) or raw_slots_overlap(raw_slots):
                return {}
        obstacles = [
            parcel for parcel in (self.home_number_boxes if obstacles is None else obstacles)
            if parcel is not None and not parcel.is_empty
        ]

        # Whole-corridor translation only. Never shift individual slots.
        options = []
        for along_step in range(-12, 13):
            for normal_step in range(0, 13):
                along = along_hint + float(along_step)
                normal = normal_hint + float(normal_step)
                if math.hypot(along, normal) <= 12.0 + 1e-9:
                    options.append((along, normal))
        options.sort(key=lambda item: (
            math.hypot(item[0] - along_hint, item[1] - normal_hint),
            abs(item[0]), item[1],
        ))
        accepted = []
        for along, normal in options:
            candidates = [
                (index, point, _translate(parcel, xoff=tx * along + nx * normal, yoff=ty * along + ny * normal))
                for index, point, parcel in raw_slots
            ]
            legal = True
            for pos, (_index, point, parcel) in enumerate(candidates):
                if parcel.is_empty or parcel.area < 40.0 - 1e-6 or parcel.area > 169.0 + 1e-6:
                    legal = False; break
                if require_clip and (clip_region is None or clip_region.is_empty or not clip_region.buffer(1e-6).covers(parcel)):
                    legal = False; break
                if road_solid is not None and not road_solid.is_empty and parcel.intersection(road_solid).area > 1e-7:
                    legal = False; break
                source_in_road = (
                    road_solid is not None and not road_solid.is_empty
                    and road_solid.buffer(1e-6).covers(point)
                )
                # KMZ coordinates stay immutable. DXF parcel+label may move
                # together within the explicit 12m presentation limit; raw
                # sources inside ROAD use the same bounded exception.
                if not parcel.buffer(1e-6).covers(point) and parcel.distance(point) > 12.0 + 1e-6:
                    legal = False; break
                if any(parcel.intersection(existing).area > 1e-7 for existing in obstacles):
                    legal = False; break
            if legal and any(
                self._strict_positive_overlap(parcel, other)
                for candidate_position, (_candidate_index, _candidate_point, other) in enumerate(candidates)
                for _previous_index, _previous_point, parcel in candidates[:candidate_position]
            ):
                legal = False
            if legal:
                accepted.append({index: parcel for index, _point, parcel in candidates})
                if len(accepted) >= max(1, int(candidate_limit)):
                    break
        if return_candidates:
            return accepted
        return accepted[0] if accepted else {}

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

    def _fit_compact_parcel_ladder(self, anchor, allowed, angle):
        """Return one intact compact parcel; never draw clipped fragments."""
        if allowed is None or allowed.is_empty:
            return None
        site = anchor if isinstance(anchor, ShapelyPoint) else ShapelyPoint(anchor)
        radians = math.radians(float(angle))
        tx, ty = math.cos(radians), math.sin(radians)
        nx, ny = -ty, tx
        # User floor: 4x10. Smaller legal fallback only after every 4m-wide
        # module fails; ceiling remains 13x13.
        modules = (
            (13.0, 13.0), (10.0, 13.0), (8.0, 13.0), (6.0, 13.0),
            (4.0, 10.0), (4.0, 9.0), (4.0, 8.0), (4.0, 7.0), (4.0, 6.0),
            (3.0, 10.0), (3.0, 9.0), (2.0, 10.0),
        )
        for width, depth in modules:
            hw, hd = width / 2.0, depth / 2.0
            parcel = Polygon([
                (site.x-tx*hw-nx*hd, site.y-ty*hw-ny*hd),
                (site.x+tx*hw-nx*hd, site.y+ty*hw-ny*hd),
                (site.x+tx*hw+nx*hd, site.y+ty*hw+ny*hd),
                (site.x-tx*hw+nx*hd, site.y-tx*0+ty*hw+ny*hd),
            ])
            # Correct final corner separately to keep explicit readable axes.
            parcel = Polygon([
                (site.x-tx*hw-nx*hd, site.y-ty*hw-ny*hd),
                (site.x+tx*hw-nx*hd, site.y+ty*hw-ny*hd),
                (site.x+tx*hw+nx*hd, site.y+ty*hw+ny*hd),
                (site.x-tx*hw+nx*hd, site.y-ty*hw+ny*hd),
            ])
            if allowed.buffer(1e-6).covers(parcel):
                return parcel
        return None

    def _fit_compact_parcel_ladder(self, anchor, allowed, angle):
        """Return one intact compact parcel; never draw clipped fragments."""
        if allowed is None or allowed.is_empty:
            return None
        site = anchor if isinstance(anchor, ShapelyPoint) else ShapelyPoint(anchor)
        radians = math.radians(float(angle))
        tx, ty = math.cos(radians), math.sin(radians)
        nx, ny = -ty, tx
        # User floor: 4x10. Smaller legal fallback only after every 4m-wide
        # module fails; ceiling remains 13x13.
        modules = (
            (13.0, 13.0), (10.0, 13.0), (8.0, 13.0), (6.0, 13.0),
            (4.0, 10.0), (4.0, 9.0), (4.0, 8.0), (4.0, 7.0), (4.0, 6.0),
            (3.0, 10.0), (3.0, 9.0), (2.0, 10.0),
        )
        for width, depth in modules:
            hw, hd = width / 2.0, depth / 2.0
            parcel = Polygon([
                (site.x-tx*hw-nx*hd, site.y-ty*hw-ny*hd),
                (site.x+tx*hw-nx*hd, site.y+ty*hw-ny*hd),
                (site.x+tx*hw+nx*hd, site.y+ty*hw+ny*hd),
                (site.x-tx*hw+nx*hd, site.y-ty*hw+ny*hd),
            ])
            if allowed.buffer(1e-6).covers(parcel):
                return parcel
        return None

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
        if not self.pending_home_numbers and not self.pending_uncover_home_numbers:
            return

        if not KML_HOME_PARCEL_ENABLED:
            raise RuntimeError("HP fixed-anchor engine tidak tersedia.")

        cover_pending = self.pending_home_numbers
        # Both classes share one allocator to prevent overlapping parcels;
        # the source folder controls each emitted DXF layer and label color.
        self.pending_home_numbers = cover_pending + [
            {**item, "hp_uncover": True} for item in self.pending_uncover_home_numbers
        ]
        entity_start = len(self.output_entities)
        parcel_start = len(self.home_number_boxes)
        cell_start = len(self.home_number_cells)
        marker_names = ("_hp_cover_drawn_indexes", "_last_label_only_indexes", "_last_relaxed_4x8_indexes")
        markers_before = {name: set(getattr(self, name)) for name in marker_names if hasattr(self, name)}
        modelspace_before = {entity.dxf.handle for entity in self.modelspace}
        detected_before = set(self.detected_fdt_numbers)
        stats_before = dict(self.stats)
        layers_before = {layer.dxf.name: layer.dxfattribs() for layer in self.doc.layers}
        fdt_lengths = {key: len(value) for key, value in self.fdt_output_entities.items()}
        try:
            self._finalize_home_numbers_as_parcels(pipeline_logger)
        except Exception:
            for entity in list(self.modelspace):
                if entity.dxf.handle not in modelspace_before:
                    self.modelspace.delete_entity(entity)
            del self.output_entities[entity_start:]
            del self.home_number_boxes[parcel_start:]
            del self.home_number_box_uncover[parcel_start:]
            del self.home_number_cells[cell_start:]
            for name in marker_names:
                if name in markers_before:
                    setattr(self, name, markers_before[name])
                elif hasattr(self, name):
                    delattr(self, name)
            self.detected_fdt_numbers.clear()
            self.detected_fdt_numbers.update(detected_before)
            self.stats.clear()
            self.stats.update(stats_before)
            for layer in list(self.doc.layers):
                if layer.dxf.name not in layers_before:
                    self.doc.layers.remove(layer.dxf.name)
                else:
                    layer.dxf.update(layers_before[layer.dxf.name])
            for key in list(self.fdt_output_entities):
                previous = fdt_lengths.get(key)
                if previous is None:
                    del self.fdt_output_entities[key]
                else:
                    del self.fdt_output_entities[key][previous:]
            raise
        finally:
            self.pending_home_numbers = cover_pending
        return

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
            self.home_number_box_uncover.append(False)
            self._record_home_number_cell(clipped_box, pending)
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
            self.pending_home_numbers[-1]["fat_owner"] = kml_fat_owner_key(folder_stack or [], source_scope=getattr(self, "_fat_source_scope", ()))
            self.kml_detail_stats["home_number_points"] += 1
            self.stats[KML_HOME_NUMBER_LAYER] += 1
        elif is_hp_uncover_folder(folder_name):
            preview_name = self._queue_uncover_home_number(projected, name, fdt_number=fdt_number)
            self.kml_detail_stats["hp_uncover_points"] += 1
            self.stats[KML_UNCOVER_LAYER] += 1
        elif normalized_folder_name(folder_name or "") == KML_FAT_LAYER:
            fat_code = extract_fat_code_from_path(folder_stack or [], name)
            self._queue_kml_fat_icon(projected, fat_code, name, style, fdt_number=fdt_number,
                                     folder_stack=folder_stack)
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
            self.pending_slack_icons.append({"projected": projected, "style": style, "name": name,
                                             "group": self._pole_line_group(folder_stack)})
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
            self._ensure_layer(str(handhole_spec["layer"]), int(handhole_spec.get("color") or KML_HANDHOLE_COLOR))
            self.stats[str(handhole_spec["layer"])] += 1
        elif is_twey_folder(folder_name):
            self._queue_kml_twey_icon(projected, name, style, fdt_number=fdt_number)
            self.kml_detail_stats["twey_blocks"] += 1
            self.stats[KML_TWEY_LAYER] += 1
            preview_name = name or "T-WAY"
        elif pole_spec:
            self._add_kml_pole_icon(projected, pole_spec, name, style, fdt_number=fdt_number)
            self._roadside_poles.append({
                "source": projected,
                "target": projected,
                "position_index": len(self.pole_positions) - 1,
                "icon": self.output_entities[-1],
                "label": self.pending_pole_ids[-1] if name.strip() else None,
                "group": self._pole_line_group(folder_stack),
            })
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
        if polyline_entity is not None:
            self._pole_follow_lines.append({
                "entity": polyline_entity,
                "source": projected,
                "group": self._pole_line_group(folder_stack),
                "cable_kind": ("distribution" if is_distribution_cable_folder(folder_stack or [])
                               else "sling" if line_layer == KML_SLING_WIRE_LAYER else None),
            })
        if line_spec and is_distribution_cable_folder(folder_stack or []):
            self._add_distribution_cable_distance_labels(coordinates, projected)
        if polyline_entity is not None and line_spec:
            draw_order = str(line_spec.get("draw_order") or "")
            if draw_order == "subduct":
                self.subduct_line_entities.append(polyline_entity)
            elif draw_order == "top_cable":
                self.top_cable_line_entities.append(polyline_entity)
            # Source-owned pole/line reconciliation attaches eligible endpoints.
            # ponytail: removed global 5m endpoint snap; add a route-aware fallback only if an actual unattached route is proven.
        self.kml_detail_stats["line_features"] += 1
        midpoint = LineString(projected).interpolate(0.5, normalized=True)
        if not line_layer:
            self._add_kml_label(name, (float(midpoint.x), float(midpoint.y)), style)
        self.stats["KML"] += 1
        if layer_name:
            self.stats[layer_name] += 1
        self._add_kml_preview_feature("LineString", coordinates, name)


    def finalize_boundary_fat_polygons(self, pipeline_logger=None) -> None:
        pending = self.pending_boundary_fat_polygons
        if not pending:
            return

        owners = {}
        for index, item in enumerate(pending):
            key = item.get("fat_owner")
            if key is not None and key[-1] is not None:
                if key in owners:
                    raise FatAreaGeometryError(f"Duplicate BOUNDARY FAT owner: {key}")
                owners[key] = index

        cells_by_fat = defaultdict(list)
        uncover_cells = []
        expected = len(self.pending_home_numbers) + len(self.pending_uncover_home_numbers)
        if len(self.home_number_cells) != expected:
            raise FatAreaGeometryError(f"FAT AREA cell identity incomplete: {len(self.home_number_cells)}/{expected}")
        for cell in self.home_number_cells:
            polygon = cell["polygon"]
            if cell["hp_uncover"]:
                uncover_cells.append(polygon)
                continue
            key = cell.get("fat_owner")
            if key is not None and key[-1] is not None:
                if key not in owners:
                    raise FatAreaGeometryError(f"HP {cell['text']} has no BOUNDARY FAT for source owner {key}")
                owner_index = owners[key]
            else:
                # Legacy unbucketed input: resolve once from immutable source, never from moved cells.
                source = ShapelyPoint(cell["source"])
                containing = [i for i, item in enumerate(pending)
                              if (key is None or (item.get("fat_owner") or ())[:-1] == key[:-1])
                              and item["polygon"].buffer(1e-6).covers(source)]
                if len(containing) > 1:
                    raise FatAreaGeometryError(f"Ambiguous source FAT for HP {cell['text']}")
                if not containing:
                    # Unbound COVER must be excluded from every FAT, never silently claimed.
                    cells_by_fat[-1].append(polygon)
                    continue
                owner_index = containing[0]
            cells_by_fat[owner_index].append(polygon)

        plans = {}
        for index, item in enumerate(pending):
            own_cells = cells_by_fat[index]
            if not own_cells:
                continue
            own_union = unary_union(own_cells)
            # ponytail: whole HP footprints; bounded connections, no parcel relocation.
            inside = own_union
            if inside.is_empty or not inside.is_valid:
                raise FatAreaGeometryError(f"FAT AREA {item['name']} has invalid owned HP cells")
            if any(cell.is_empty or not cell.is_valid for cell in own_cells):
                raise FatAreaGeometryError(f"FAT AREA {item['name']} loses an owned HP cell")
            foreign_cells = [cell for owner, cells in cells_by_fat.items() if owner != index for cell in cells]
            foreign = unary_union(foreign_cells + uncover_cells)
            if not foreign.is_empty and own_union.intersection(foreign).area > 1e-7:
                raise FatAreaGeometryError(f"FAT AREA {item['name']} has overlapping foreign/UNCOVER HP cells")
            plans[index] = (inside, own_union.convex_hull, foreign)

        # Reserve connected HP seeds jointly before any envelope can trap a later owner.
        order = sorted(plans, key=lambda i: (plans[i][1].area / plans[i][0].area, i))
        # ponytail: three priority passes, existing bounded visibility helper; no global path solver.
        for attempt in range(3):
            seeds = {}
            for index in order:
                inside, envelope, foreign = plans[index]
                excluded = unary_union([foreign] + list(seeds.values()))
                seed = _connect_fat_components(inside, excluded, inside)
                clipped = seed.intersection(envelope).buffer(0)
                if isinstance(clipped, Polygon) and inside.difference(clipped).area <= 1e-7:
                    seed = clipped
                if (not isinstance(seed, Polygon) or not seed.is_valid
                        or inside.difference(seed).area > 1e-7 or seed.intersection(excluded).area > 1e-7):
                    break
                seeds[index] = seed
            else:
                break
            if attempt == 2 or order[0] == index:
                raise FatAreaGeometryError(f"FAT AREA {pending[index]['name']} cannot remain one connected polygon without claiming foreign HP (bounded joint seed reservation)")
            order.remove(index)
            order.insert(0, index)

        final_geometries = []
        for index, item in enumerate(pending):
            if index not in plans:
                final_geometries.append(Polygon())
                continue
            inside, envelope, foreign = plans[index]
            excluded = unary_union([foreign] + [g for owner, g in seeds.items() if owner != index] + final_geometries)
            carve = excluded.buffer(0.2, join_style=2).difference(inside)
            grown = envelope.difference(carve).union(seeds[index]).buffer(0)
            # Omit only empty-envelope scraps; every whole owned HP stays in its connected seed.
            final_geom = unary_union([part for part in _fat_polygon_parts(grown)
                                      if part.intersection(seeds[index]).area > 1e-7])
            if final_geom.is_empty or not final_geom.is_valid or not isinstance(final_geom, Polygon):
                raise FatAreaGeometryError(f"FAT AREA {item['name']} cannot remain one connected polygon without claiming foreign HP")
            # Fill only cosmetic empty-space gaps; protected foreign/other-seed holes remain holes.
            holes = [ring.coords for ring in final_geom.interiors
                     if not excluded.is_empty and Polygon(ring.coords).intersection(excluded).area > 1e-7]
            final_geom = Polygon(final_geom.exterior.coords, holes)
            # Restore exact owned footprints after high-coordinate overlay roundoff;
            # keep the original whole-cell, connected-Polygon and foreign guards.
            final_geom = final_geom.union(inside).buffer(0)
            # Unary union can itself lose micro-wedges at large projected coordinates.
            # Restore from each immutable ownership cell, not only its merged proxy.
            for _round in range(2):
                for cell in cells_by_fat[index]:
                    if cell.difference(final_geom).area > 1e-7:
                        final_geom = final_geom.union(cell).buffer(0)
            if not isinstance(final_geom, Polygon) or not final_geom.is_valid:
                raise FatAreaGeometryError(f"FAT AREA {item['name']} numerical ownership union is not connected")
            if (inside.difference(final_geom).area > 1e-7 or
                    any(cell.difference(final_geom).area > 1e-7 for cell in cells_by_fat[index])):
                raise FatAreaGeometryError(f"FAT AREA {item['name']} misses an owned HP cell")
            if not excluded.is_empty and final_geom.intersection(excluded).area > 1e-7:
                raise FatAreaGeometryError(f"FAT AREA {item['name']} intersects foreign/UNCOVER HP cells")
            final_geometries.append(final_geom)

        # One advisory call for the complete drawing, never one request per HP/FAT.
        summary = {
            "fat_count": len(plans), "hp_count": sum(len(cells_by_fat[i]) for i in plans),
            "vertex_count": sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
                                for p in final_geometries if not p.is_empty),
        }
        # Show AI only already-verified cut options, not raw coordinates or authority to edit.
        summary["safe_cut_options"] = []
        for tolerance in (.01, .05, .1, .2, .5):
            removed = 0
            for index, polygon in enumerate(final_geometries):
                if polygon.is_empty:
                    continue
                foreign = unary_union([plans[index][2]] + [p for i, p in enumerate(final_geometries) if i != index])
                trimmed = _trim_fat_boundary(polygon, plans[index][0], foreign, tolerance)
                if trimmed is not None:
                    removed += len(polygon.exterior.coords) + sum(len(r.coords) for r in polygon.interiors)
                    removed -= len(trimmed.exterior.coords) + sum(len(r.coords) for r in trimmed.interiors)
            if removed:
                summary["safe_cut_options"].append({"simplify_tolerance_m": tolerance, "removed_vertices": removed,
                                                   "whole_hp_missing_m2": 0, "foreign_overlap_m2": 0})
        advice = suggest_dxf_fat_boundary_trim(summary) if summary["safe_cut_options"] else None
        self.fat_boundary_trim_stats = {"recommended": bool(advice), "applied": 0, "removed_vertices": 0}
        if advice:
            for index, polygon in enumerate(final_geometries):
                if polygon.is_empty:
                    continue
                foreign = unary_union([plans[index][2]] + [p for i, p in enumerate(final_geometries) if i != index])
                trimmed = _trim_fat_boundary(polygon, plans[index][0], foreign, advice["simplify_tolerance_m"])
                if trimmed is not None:
                    count = lambda p: len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
                    self.fat_boundary_trim_stats["removed_vertices"] += count(polygon) - count(trimmed)
                    self.fat_boundary_trim_stats["applied"] += 1
                    final_geometries[index] = trimmed
            pipeline_log(pipeline_logger, "info", "FAT AREA AI trim recommendation accepted: "
                         f"tolerance {advice['simplify_tolerance_m']:.2f}m, confidence {advice['confidence']:.2f}; "
                         f"applied {self.fat_boundary_trim_stats['applied']} FAT, "
                         f"removed {self.fat_boundary_trim_stats['removed_vertices']} vertices; full HP/foreign guards retained.")

        if "EVA_FAT_OWNER" not in self.doc.appids:
            self.doc.appids.new("EVA_FAT_OWNER")
        for index, (item, poly) in enumerate(zip(pending, final_geometries)):
            if poly.is_empty:
                continue
            attrs = item["attrs"]
            outer = self._add_polyline(list(poly.exterior.coords), close=True, dxfattribs=attrs)
            outer.set_xdata("EVA_FAT_OWNER", [(1071, index), (1000, "OUTER")])
            self.kml_detail_stats["polygon_features"] += 1
            for inner in poly.interiors:
                hole = self._add_polyline(list(inner.coords), close=True, dxfattribs=attrs)
                hole.set_xdata("EVA_FAT_OWNER", [(1071, index), (1000, "HOLE")])
            self.stats["KML"] += 1
            self.stats[KML_FAT_AREA_LAYER] += 1
            self._add_kml_preview_feature("Polygon", [item["outer_coordinates"]] + item["valid_inner_rings"], "")

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
            self.pending_boundary_fat_polygons.append({
                "polygon": polygon_for_validation,
                "projected_outer": projected_outer,
                "projected_inner_rings": projected_inner_rings,
                "attrs": attrs,
                "name": name,
                "fat_owner": kml_fat_owner_key(folder_stack or [], name, getattr(self, "_fat_source_scope", ())),
                "outer_coordinates": outer_coordinates,
                "valid_inner_rings": valid_inner_rings,
            })
            return

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
        # Keep native viewport/font resources for the rebased scope templates.
        if not self.preserve_native_template:
            self.doc.dxfversion = OUTPUT_DXF_VERSION
        self._turn_all_layers_visible()
        self.apply_cable_draw_order()
        if not self.preserve_native_template:
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
        """Return the paper-space tab matching the active scope."""
        scope = normalize_scope_type_name(self.scope_type)
        if scope == "Subfeeder":
            return "Subfeeder"
        if scope == "Feeder":
            return "Feeder"
        return TEMPLATE_LAYOUT_NAME

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
        """Style a viewport entity as a non-plotting frame."""
        try:
            viewport.dxf.layer = TEMPLATE_VIEWPORT_LAYER
            viewport.dxf.color = WHITE_ACI_COLOR
            viewport.dxf.true_color = WHITE_TRUE_COLOR
            # Entity-level plot flag: 0 = do not plot.
            viewport.dxf.plot = 0
        except Exception as exc:
            LOGGER.debug("Viewport tidak bisa diset putih: %s", exc)

    def _paper_viewport_center_size(self) -> tuple[tuple[float, float], tuple[float, float]]:
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
        if self.preserve_native_template:
            # page_setup() deletes the template viewports and their style links.
            layout.dxf.plot_configuration_file = "DWG To PDF.pc3"
            layout.dxf.plot_layout_flags |= 512
            return
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
        active_route_label = "TOTAL DUCT ROUTE" if is_ug else "TOTAL AERIAL ROUTE"
        total_route = self._design_summary_override_int("TOTAL ROUTE LENGTH", distribution_route)
        active_route = self._design_summary_override_int(active_route_label, total_route)
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
            active_route_label: active_route,
            "TOTAL ROUTE LENGTH": total_route,
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
                    values["TOTAL TRENCHING"] = distribution_route
                    LOGGER.warning(
                        "Feeder UG TOTAL TRENCHING has no BOQ NRO/design override source; "
                        "using KML distribution-route fallback (%s m). Real trenching should come "
                        "from Feeder BOQ NRO trenching quantity cells.",
                        distribution_route,
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

        # Rendering only: remove terminal cross-bars; keep full buffer/clip as the
        # authoritative ROAD footprint for HP exclusion and collision checks.
        self._road_open_ends = []
        self._road_end_tangents = []
        self._road_buffer_tree = STRtree(self.road_buffers) if STRtree is not None and len(self.road_buffers) > 1 else None
        self._road_buffer_indexes = {id(area): index for index, area in enumerate(self.road_buffers)}
        for source_index, centerline in enumerate(self._road_buffer_centerlines):
            visible = centerline
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                visible = visible.intersection(self.road_clip_geometry)
            parts = [visible] if isinstance(visible, LineString) else list(getattr(visible, "geoms", []))
            for part in parts:
                if not isinstance(part, LineString) or part.length <= 0 or part.is_ring:
                    continue
                coords = list(part.coords)
                for end, near in ((coords[0], coords[1]), (coords[-1], coords[-2])):
                    dx, dy = near[0] - end[0], near[1] - end[1]
                    length = math.hypot(dx, dy)
                    if length:
                        endpoint = ShapelyPoint(end)
                        if self._road_buffer_tree is not None:
                            others = self._query_indexed_geometries(self._road_buffer_tree, self.road_buffers, endpoint)
                        else:
                            others = self.road_buffers
                        if any(
                            self._road_buffer_indexes[id(area)] != source_index
                            and area.covers(endpoint)
                            and not area.equals(self.road_buffers[source_index])
                            for area in others
                        ):
                            continue
                        self._road_open_ends.append(endpoint)
                        self._road_end_tangents.append((dx / length, dy / length))
        self._road_clip_bands = {}
        self._road_source_line_tree = STRtree(self._road_buffer_centerlines) if STRtree is not None and self._road_buffer_centerlines else None

        if self.road_buffers:
            road_union = unary_union(self.road_buffers)
            road_union = road_union.buffer(0)
            if self.road_clip_geometry is not None and not self.road_clip_geometry.is_empty:
                road_union = road_union.intersection(self.road_clip_geometry)
            # Keep rendered ROAD footprint identical to the planner hard-exclusion
            # geometry. Simplifying here can move/expand boundaries after HP cells
            # pass the raw ROAD gate, creating false visual ROAD overlaps.
            road_union = road_union.buffer(0)
            # Way endpoints buried in intersections are not visible ROAD tips.
            edge = road_union.boundary
            exposed = [index for index, point in enumerate(self._road_open_ends) if edge.distance(point) <= 1e-4]
            self._road_end_tangents = [self._road_end_tangents[index] for index in exposed]
            self._road_open_ends = [self._road_open_ends[index] for index in exposed]
            self._road_open_end_tree = STRtree(self._road_open_ends) if STRtree is not None and self._road_open_ends else None
            self._road_end_indexes = {id(point): index for index, point in enumerate(self._road_open_ends)}
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

    def _add_road_ring(self, points, inset: float = 0.0) -> None:
        """Omit only cross-bars at exposed source/clip ends, not road sides."""
        coords = list(points)
        if coords[0] == coords[-1]:
            coords.pop()
        removed = set()
        ends = getattr(self, "_road_open_ends", [])
        tree = getattr(self, "_road_open_end_tree", None)
        tolerance = inset + 1e-5
        clip = self.road_clip_geometry
        clip_edge_band = None
        if clip is not None and not clip.is_empty:
            bands = self._road_clip_bands
            if inset not in bands:
                bands[inset] = clip.boundary.buffer(inset + 1e-5)
            clip_edge_band = bands[inset]
        for index, (a, b) in enumerate(zip(coords, coords[1:] + coords[:1])):
            segment = LineString([a, b])
            if segment.length <= 1e-8:
                continue
            on_clip = clip_edge_band is not None and segment.difference(clip_edge_band).length < 1e-5
            if on_clip:
                # A partial-width clip can cut ROAD without reaching centerline.
                midpoint = segment.interpolate(0.5, normalized=True)
                lines = self._road_buffer_centerlines
                line_tree = self._road_source_line_tree
                if line_tree is not None:
                    near = line_tree.nearest(midpoint)
                    line = near if hasattr(near, "geom_type") else lines[int(near)]
                else:
                    line = min(lines, key=lambda item: item.distance(midpoint)) if lines else None
                if line is not None:
                    station = line.project(midpoint)
                    left = line.interpolate(max(0.0, station - 0.1))
                    right = line.interpolate(min(line.length, station + 0.1))
                    tx, ty = right.x - left.x, right.y - left.y
                    length = math.hypot(tx, ty)
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    if length and abs(tx * dx + ty * dy) < 0.98 * length * segment.length:
                        removed.add(index)
                        continue
            candidates = self._query_indexed_geometries(tree, ends, segment.buffer(tolerance)) if tree is not None else ends
            for point in candidates:
                if point.distance(segment) > inset + 1e-5:
                    continue
                tx, ty = self._road_end_tangents[self._road_end_indexes[id(point)]]
                dx, dy = b[0] - a[0], b[1] - a[1]
                on_clip = clip_edge_band is not None and segment.difference(clip_edge_band).length < 1e-5
                if not on_clip and abs(tx * dx + ty * dy) > 0.7 * segment.length:
                    continue  # a longitudinal side, not a transverse cap
                side_a = tx * (a[1] - point.y) - ty * (a[0] - point.x)
                side_b = tx * (b[1] - point.y) - ty * (b[0] - point.x)
                if side_a * side_b < -1e-8 or (
                    min(abs(side_a), abs(side_b)) <= 1e-5
                    and max(abs(side_a), abs(side_b)) > 1e-5
                    and abs((a[0] - point.x) * tx + (a[1] - point.y) * ty) <= tolerance
                    and abs((b[0] - point.x) * tx + (b[1] - point.y) * ty) <= tolerance
                ):
                    removed.add(index)
                    break
        if not removed:
            self._add_polyline(coords, close=True, dxfattribs={"layer": "ROAD"})
            return
        # Start after a removed edge so wraparound remains one continuous run.
        start = next(iter(removed)) + 1
        run = [coords[start % len(coords)]]
        for step in range(len(coords)):
            index = (start + step) % len(coords)
            following = coords[(index + 1) % len(coords)]
            if index in removed:
                if len(run) >= 2:
                    self._add_polyline(run, close=False, dxfattribs={"layer": "ROAD"})
                run = [following]
            else:
                run.append(following)

    def _add_polygon_boundary(self, polygon: Polygon) -> None:
        if polygon.area < ROAD_MIN_POLYGON_AREA_SQUARE_METERS:
            return

        exterior = list(polygon.exterior.coords)
        if len(exterior) >= 4:
            self._add_road_ring(exterior)

        for interior in polygon.interiors:
            interior_points = list(interior.coords)
            if len(interior_points) >= 4:
                self._add_road_ring(interior_points)

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
                self._add_road_ring(inner_exterior, ROAD_INNER_LINE_OFFSET_METERS)
            for interior in inner_polygon.interiors:
                interior_points = list(interior.coords)
                if len(interior_points) >= 4:
                    self._add_road_ring(interior_points, ROAD_INNER_LINE_OFFSET_METERS)


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
    document_scope: tuple[str, ...] = (),
) -> int:
    feature_count = 0

    for child_index, child in enumerate(list(element)):
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
                document_scope + (f"{child_index}:{direct_child_text_by_name(child, 'name')}",),
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
                document_scope,
            )
        elif child_name == "Placemark":
            standard_folder = nearest_standard_folder(folder_stack)
            if not standard_folder or (standard_folder.startswith("EXISTING HH ") and handler.network_type != "UG"):
                continue
            key = kml_folder_key(file_name, standard_folder)
            if filter_enabled and key not in selected_kml_folders and standard_folder not in selected_kml_folders:
                continue
            name = direct_child_text_by_name(child, "name")
            style = resolve_kml_style(direct_child_text_by_name(child, "styleUrl"), styles, style_maps)
            previous_scope = getattr(handler, "_fat_source_scope", ())
            handler._fat_source_scope = (file_name, *document_scope)
            try:
                feature_count += parse_kml_placemark(child, handler, name, style, standard_folder, folder_stack)
            finally:
                handler._fat_source_scope = previous_scope

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
            # Cluster KMZ may only contain HP points. Build a local query boundary
            # so province ROAD cache never loads full-region geometry and HP strips
            # still receive real nearby ROAD instead of a no-ROAD fallback.
            local_geometries = _collect_auto_boundary_geometries_from_inputs(input_paths, target_crs)
            local_boundary = _auto_boundary_from_geometries(
                local_geometries, auto_boundary_buffer_meters_for_scope(handler.scope_type)
            )
            if local_boundary is not None:
                handler.set_road_clip_geometry(local_boundary)
                pipeline_log(
                    pipeline_logger, "info",
                    "No BOUNDARY CLUSTER: using local KMZ geometry buffer for cached ROAD query.",
                )
            else:
                pipeline_log(pipeline_logger, "warning", "ROAD clipping enabled, but no local KML geometry was found.")
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

    handler.finalize_roadside_poles_and_lines(pipeline_logger=pipeline_logger)
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
    cover_count = len(handler.pending_home_numbers)
    uncover_count = len(handler.pending_uncover_home_numbers)
    pipeline_log(
        pipeline_logger,
        "info",
        f"[3/6] HP dari KMZ: {cover_count:,} COVER, {uncover_count:,} UNCOVER; "
        "merencanakan parcel bersama tanpa mengubah kelas sumber.",
    )
    handler.finalize_home_numbers(pipeline_logger=pipeline_logger)
    handler.finalize_boundary_fat_polygons(pipeline_logger=pipeline_logger)
    pipeline_log(
        pipeline_logger,
        "success",
        f"[3/6] HP parcel selesai: {cover_count:,} COVER (HOME_NUMBER), "
        f"{uncover_count:,} UNCOVER (HP_UNCOVER) dalam "
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

