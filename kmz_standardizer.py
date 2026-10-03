from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

from boq import (
    DISTRIBUTION_CABLE_ROW_MAP,
    DISTRIBUTION_CABLE_TOTAL_ROW_MAP,
    calculate_linestring_length_meter,
    extract_cluster_name_from_kmz_filename,
    line_length_with_fallback,
    placemark_linestrings,
    target_cell,
)
from converter import (
    canonical_standard_folder,
    direct_child_by_name,
    direct_child_text_by_name,
    local_name,
    nearest_standard_folder,
    parse_kml_coordinates,
    parse_kml_xml,
)

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
RULES_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "rules.json"


def load_rules_config() -> dict:
    if not RULES_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def partner_pattern_from_aliases(aliases: dict[str, str]) -> str:
    keys = sorted({re.escape(key) for key in aliases if key}, key=len, reverse=True)
    return "|".join(keys) or "MR"


_RULES_CONFIG = load_rules_config()
_KMZ_RULES = _RULES_CONFIG.get("kmz_standardizer") or {}


def _rule_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def kmz_rule_list(key: str, default: list[str]) -> list[str]:
    value = _KMZ_RULES.get(key)
    if isinstance(value, list):
        return [_rule_text(item).upper() for item in value if _rule_text(item)]
    return list(default)


def kmz_rule_set(key: str, default: set[str]) -> set[str]:
    value = _KMZ_RULES.get(key)
    if isinstance(value, list):
        return {_rule_text(item).upper() for item in value if _rule_text(item)}
    return set(default)


def kmz_rule_map(key: str, default: dict) -> dict:
    value = _KMZ_RULES.get(key)
    if isinstance(value, dict):
        return {_rule_text(k).upper(): v for k, v in value.items() if _rule_text(k)}
    return dict(default)


FDT_ORDER_PATTERN = re.compile(r"\bFDT\s*[-_ ]*(\d+)\b", re.IGNORECASE)
FDT_CODE_PATTERN = re.compile(r"\b[A-Z][A-Z0-9-]*\.\d{3}\b", re.IGNORECASE)
FDT_CAPACITY_DESCRIPTION_PATTERN = re.compile(
    r"\bFDT\b\s*(?:CAPACITY|KAPASITAS|CAP\.?|TYPE|TIPE|[:=_/\-])?\s*(24|48|72|144)\s*C?\b",
    re.IGNORECASE,
)
# Loose capacity pattern: "<digits>C" or "<digits> CORE(S)" anywhere in the
# string. Used as a secondary check on FDT names like "FDT 01 / 72C" or
# "FDT 2 / 48 CORE" where the order number sits between FDT and the capacity.
FDT_CAPACITY_LOOSE_PATTERN = re.compile(
    r"(?<![A-Z0-9])(24|48|72|96|144|288)\s*(?:C|CORE|CORES)(?![A-Z0-9])",
    re.IGNORECASE,
)
PROJECT_CODE_PREFIX_PATTERN = re.compile(r"^\s*[A-Z]{2,}\d+\s*[-_ ]+", re.IGNORECASE)
OLT_CODE_PATTERN = re.compile(r"\b[A-Z]{2,}(?:\.[A-Z0-9]+)+\b", re.IGNORECASE)
GOOGLE_SHEETS_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
FDT_PLACEHOLDER_PREFIX_PATTERN = re.compile(r"^XXX\.XXX(?=\s+-|\s*$)", re.IGNORECASE)
MR_PLACEHOLDER_POLE_PATTERN = re.compile(r"\bMR\.XXX\.(P\d+)\b", re.IGNORECASE)
PLN_POLE_NAME_PATTERN = re.compile(r"(?<![A-Z0-9])PLN(?![A-Z0-9])", re.IGNORECASE)
EXT_MR_POLE_PATTERN = re.compile(r"\bEXT\.MR\.(P\d+)\b", re.IGNORECASE)
DEFAULT_EXT_PARTNER_ALIASES = {
    "EMR": "MR",
    "MR": "MR",
    "FM": "LN",
    "TEL": "TLKM",
    "TLK": "TLKM",
    "TELKOM": "TLKM",
    "TELECOM": "TLKM",
    "TLKOM": "TLKM",
    "TELEKOM": "TLKM",
    "TLKM": "TLKM",
    "LINKNET": "LN",
    "LN": "LN",
    "MORATEL": "MTI",
    "MTI": "MTI",
    "PLN": "PLN",
}
DEFAULT_EXT_PARTNER_TARGET_FOLDER = {
    "MR": "EXISTING POLE EMR 7-4",
    "MTI": "EXISTING POLE PARTNER 7-4",
    "LN": "EXISTING POLE PARTNER 7-4",
    "TLKM": "EXISTING POLE PARTNER 7-4",
    "PLN": "EXISTING POLE PARTNER 7-4",
}
EXT_PARTNER_ALIASES = {
    **DEFAULT_EXT_PARTNER_ALIASES,
    **{str(k).upper(): str(v).upper() for k, v in (_RULES_CONFIG.get("ext_partner_aliases") or {}).items()},
}
EXT_PARTNER_TARGET_FOLDER = {
    **DEFAULT_EXT_PARTNER_TARGET_FOLDER,
    **{str(k).upper(): str(v) for k, v in (_RULES_CONFIG.get("ext_partner_target_folder") or {}).items()},
}
_EXT_PARTNER_PATTERN_TEXT = partner_pattern_from_aliases(EXT_PARTNER_ALIASES)
_STANDARD_EXT_PARTNERS_PATTERN_TEXT = partner_pattern_from_aliases({partner: partner for partner in EXT_PARTNER_TARGET_FOLDER})
BARE_EXT_PARTNER_PATTERN = re.compile(
    rf"^\s*EXT[\.\s_-]*(?P<partner>{_EXT_PARTNER_PATTERN_TEXT})\s*\.?\s*$",
    re.IGNORECASE,
)
EXT_PARTNER_POLE_PATTERN = re.compile(
    rf"\bEXT\.(?P<partner>{_EXT_PARTNER_PATTERN_TEXT})"
    r"(?:\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*))?\.(?P<pole>P\d+)\b",
    re.IGNORECASE,
)
EXT_PARTNER_BARE_PATTERN = re.compile(
    rf"\b(?:EXT[\.\s_-]*)?(?P<partner>{_EXT_PARTNER_PATTERN_TEXT})\b"
    r"(?:[\.\s_-]+(?P<cluster>[A-Z0-9-]+))?\b",
    re.IGNORECASE,
)
PARTNER_DOT_POLE_PATTERN = re.compile(
    rf"(?<![A-Z0-9])(?P<partner>{_EXT_PARTNER_PATTERN_TEXT})"
    r"\s*\.\s*P?(?P<number>\d+)(?![A-Z0-9])",
    re.IGNORECASE,
)

def canonical_ext_partner(partner: str | None) -> str | None:
    normalized = normalize_text(partner).upper()
    return EXT_PARTNER_ALIASES.get(normalized)

def bare_ext_partner_classification(name: str | None) -> tuple[str, str] | None:
    match = BARE_EXT_PARTNER_PATTERN.match(str(name or ""))
    if not match:
        return None
    partner = canonical_ext_partner(match.group("partner"))
    if not partner:
        return None
    target_folder = EXT_PARTNER_TARGET_FOLDER.get(partner)
    if not target_folder:
        return None
    return partner, target_folder

def canonical_ext_partner_from_name(name: str | None) -> str | None:
    text = normalize_text(name)
    for pattern in (BARE_EXT_PARTNER_PATTERN, EXT_PARTNER_POLE_PATTERN, PARTNER_DOT_POLE_PATTERN):
        match = pattern.search(text)
        if match:
            partner = canonical_ext_partner(match.group("partner"))
            if partner:
                return partner
    return None

STANDARD_POLE_NUMBER_PATTERN = re.compile(
    rf"\b(?:MR|EXT\.(?:{_STANDARD_EXT_PARTNERS_PATTERN_TEXT}))\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)\.P(?P<number>\d+)\b",
    re.IGNORECASE,
)
MR_EXPANDED_POLE_PATTERN = re.compile(
    r"\bMR\.(?P<cluster>[A-Z0-9-]+)\.(?P<section>[A-Z0-9-]+)\.P(?P<number>\d+)(?=\b|\.)",
    re.IGNORECASE,
)
HC_POLE_PATTERN = re.compile(r"(?<![A-Z0-9])HC(?![A-Z0-9])|\.HC\b", re.IGNORECASE)
HC_AFTER_POLE_PATTERN = re.compile(r"\bP\d+\.HC\b", re.IGNORECASE)
EXISTING_EMR_BARE_PATTERN = re.compile(r"(?<![A-Z0-9])EXISTING(?:[\s._-]+(?P<cluster>[A-Z0-9-]+))?", re.IGNORECASE)
FAT_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])(?:FAT\s*)?([A-E]\d{2})(?![A-Z0-9])", re.IGNORECASE)
HP_COVER_NAME_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:NN|UMKM|TOKO|RUKO|TK|INDOMART|ALFAMART|WARUNG|KIOS|KOST|KOS|MUSHOLA|MASJID|GEREJA|SEKOLAH|POS|RUMAH|\?-)",
    re.IGNORECASE,
)
CABLE_CAPACITY_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:FO\s*)?(12|24|36|48|72|96|144|288)\s*C(?:\s*/\s*(1|2|3|4|6|8|12|24)\s*T)?(?![A-Z0-9])",
    re.IGNORECASE,
)
# Default tube count per core count, used when the cable name only specifies
# cores (e.g. "FO 24C") without an explicit tube designation ("/2T").
_DEFAULT_TUBE_BY_CORE = {
    "12": "1", "24": "2", "36": "3", "48": "4", "72": "6",
    "96": "8", "144": "12", "288": "24",
}
LINE_ORDER_PATTERN = re.compile(r"\bLINE\s*[-_ ]*([A-Z])\b", re.IGNORECASE)
TWAY_ALIAS_PATTERN = re.compile(r"T\s*-?\s*W(?:A|E)Y", re.IGNORECASE)
TWAY_FOLDER_ALIASES = frozenset({"T-WAY", "TWAY", "TWEY", "T-WEY"})
_single_fdt_mode = False
NETWORK_TYPE_PATTERN = re.compile(r"(?<![A-Z0-9])(AE|UG)(?![A-Z0-9])", re.IGNORECASE)
TOTAL_ROUTE_PATTERN = re.compile(r"TOTAL\s+ROUTE\s*:?\s*(\d+(?:[.,]\d+)?)\s*M\b", re.IGNORECASE)
TOTAL_SLACK_PATTERN = re.compile(r"TOTAL\s+SLACK\s*:?\s*(\d+)\s*UNIT\b", re.IGNORECASE)
WRAPPER_FOLDER_PATTERN = re.compile(r"^CLUSTER\s+CODE(?:\.(?:KMZ|KML))?$", re.IGNORECASE)
PROJECT_CODE_PATTERN = re.compile(r"\b[A-Z]{2,}\d{4,}\b", re.IGNORECASE)
FOLDER_METADATA_CHILDREN = {"name", "visibility", "open", "description", "Snippet", "styleUrl"}
LINE_SCOPED_LINE_FOLDERS = kmz_rule_set("line_scoped_line_folders", {"SLING WIRE", "SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"})
AUTO_SLING_WIRE_MAX_LENGTH_M = kmz_rule_map("sling_wire_limits", {"auto_sling_wire_max_length_m": 80, "auto_sling_wire_min_fdt_distance_m": 80}).get("auto_sling_wire_max_length_m", 80)
AUTO_SLING_WIRE_MIN_FDT_DISTANCE_M = kmz_rule_map("sling_wire_limits", {"auto_sling_wire_max_length_m": 80, "auto_sling_wire_min_fdt_distance_m": 80}).get("auto_sling_wire_min_fdt_distance_m", 80)
# Root T-WAY markers are normally drawn on a SUBDUCT route. Keep a bounded
# physical association so a marker near an unrelated Distribution Cable cannot
# invent a LINE relationship.
TWAY_SUBDUCT_ROUTE_LINK_MAX_METER = 50.0
DEFAULT_LINE_COMMON_FOLDERS = [
    "BOUNDARY",
    "BOUNDARY FAT",
    "FAT",
    "HP COVER",
    "HP UNCOVER",
    "DISTRIBUTION CABLE",
    "SLACK HANGER",
]
LINE_COMMON_FOLDERS = kmz_rule_list("line_common_folders", DEFAULT_LINE_COMMON_FOLDERS)
DEFAULT_LINE_AE_FOLDERS = [
    "EXISTING POLE EMR 7-2.5",
    "NEW POLE 6-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "NEW POLE 7-2.5",
    "NEW POLE 7-3",
    "NEW POLE 7-4",
    "NEW POLE 9-4",
    "NEW POLE 12-12",
    "NEW POLE ULIN 8-3",
    "DROP WIRE",
    "SLING WIRE",
]
LINE_AE_FOLDERS = kmz_rule_list("line_ae_folders", DEFAULT_LINE_AE_FOLDERS)
DEFAULT_LINE_UG_FOLDERS = [
    "EXISTING HH 40X40X60",
    "EXISTING HH 20X20X20",
    "NEW HH 40X40X60",
    "NEW HH 20X20X20",
    "SUBDUCT 40/34",
    "SUBDUCT 32/28",
    "DISTRIBUTION CABLE",
    "SLACK HANGER",
    "SLING WIRE",
    "OPEN TRENCHING",
    "SEWER",
    "BORING MANUAL",
    "BORING MACHINE",
]
LINE_UG_FOLDERS = kmz_rule_list("line_ug_folders", DEFAULT_LINE_UG_FOLDERS)
LINE_STANDARD_FOLDERS = LINE_COMMON_FOLDERS + LINE_AE_FOLDERS + LINE_UG_FOLDERS
AUTO_LINE_STANDARD_EXCLUDE_FOLDERS = kmz_rule_set("auto_line_standard_exclude_folders", {"BOUNDARY"})

# Feeder scope folder structure (flat, no LINE/FDT nesting)
SUBFEEDER_SF_PATTERN = re.compile(r"\bSF\b", re.IGNORECASE)
FEEDER_AREA_CODE_PATTERN = re.compile(r"(?:^|[^A-Z0-9])F[A-Z]{3}\d*(?=[^A-Z0-9]|$)", re.IGNORECASE)
DEFAULT_FEEDER_AE_FOLDERS = [
    "OLT",
    "CABLE",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 7-5",
    "EXISTING POLE EMR 9-5",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "NEW POLE 7-5",
    "NEW POLE 7-4",
    "NEW POLE 9-5",
    "NEW POLE 9-4",
    "NEW POLE 6-2.5",
    "NEW POLE 12-12",
    "NEW POLE ULIN 8-3",
    "JOINT CLOSURE",
    "SLACK HANGER",
    "HANDHOLE 80X80X130",
]
FEEDER_AE_FOLDERS = kmz_rule_list("feeder_ae_folders", DEFAULT_FEEDER_AE_FOLDERS)
DEFAULT_FEEDER_UG_FOLDERS = [
    "OLT",
    "CABLE",
    "JOINT CLOSURE",
    "EXISTING HH 80X80X130",
    "NEW HH 80X80X130",
    "SUBDUCT 40/34",
    "SUBDUCT 32/28",
    "SUBDUCT 3RD PARTY",
    "PVC 4INCH",
    "GALVANIS",
    "DROP WIRE",
    "SLACK HANGER",
    "SEWER",
    "ASPHALT",
    "CUT TRACK",
    "CROSSING REL KERETA",
    "SUBMARINE",
    "OPEN TRENCHING",
    "BORING MANUAL",
    "BORING MACHINE",
]
FEEDER_UG_FOLDERS = kmz_rule_list("feeder_ug_folders", DEFAULT_FEEDER_UG_FOLDERS)
DEFAULT_SUBFEEDER_AE_FOLDERS = [
    "JOINT CLOSURE",
    "EXISTING POLE EMR 7-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 7-5",
    "EXISTING POLE EMR 9-5",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "NEW POLE 7-5",
    "NEW POLE 9-5",
    "NEW POLE 7-4",
    "NEW POLE 9-4",
    "NEW POLE 6-2.5",
    "NEW POLE 12-12",
    "NEW POLE ULIN 8-3",
    "CABLE",
    "SLACK HANGER",
]
SUBFEEDER_AE_FOLDERS = kmz_rule_list("subfeeder_ae_folders", DEFAULT_SUBFEEDER_AE_FOLDERS)
DEFAULT_SUBFEEDER_UG_FOLDERS = [
    "OPEN TRENCHING",
    "BORING MACHINE",
    "BORING MANUAL",
    "SEWER",
    "JOINT CLOSURE",
    "CABLE",
    "EXISTING HH 80X80X130",
    "EXISTING HH 40X40X60",
    "NEW HH 80X80X130",
    "NEW HH 40X40X60",
    "SUBDUCT 40/34",
    "SUBDUCT 32/28",
    "SUBDUCT 3RD PARTY",
    "PVC 4INCH",
    "GALVANIS",
    "DROP WIRE",
    "SLACK HANGER",
    "ASPHALT",
    "CUT TRACK",
    "CROSSING REL KERETA",
    "SUBMARINE",
]
SUBFEEDER_UG_FOLDERS = kmz_rule_list("subfeeder_ug_folders", DEFAULT_SUBFEEDER_UG_FOLDERS)
# FEEDER/SUBFEEDER output follows the supplied flat templates exactly. FDT is
# a Cluster-only folder and must never be created in either feeder scope.
FEEDER_OPTIONAL_FOLDERS = kmz_rule_list("feeder_optional_folders", [])
FEEDER_STANDARD_FOLDERS = list(
    dict.fromkeys(FEEDER_AE_FOLDERS + FEEDER_UG_FOLDERS + FEEDER_OPTIONAL_FOLDERS)
)
FEEDER_STANDARD_FOLDER_SET = set(FEEDER_STANDARD_FOLDERS)

# Joint Closure capacity detection
JOINT_CLOSURE_CAPACITY_PATTERN = re.compile(r"\b(24|36|48|72|96|144|288)\s*C?\b", re.IGNORECASE)
JOINT_CLOSURE_NEW_PATTERN = re.compile(r"\bNEW\b", re.IGNORECASE)
JOINT_CLOSURE_EXT_PATTERN = re.compile(r"\bEXT\b", re.IGNORECASE)

GOOGLE_KML_SHAPES = "http://maps.google.com/mapfiles/kml/shapes"
GOOGLE_KML_PADDLE = "http://maps.google.com/mapfiles/kml/paddle"

@dataclass(frozen=True)
class KmzStyleSpec:
    style_id: str
    color: str
    icon_href: str | None = None
    icon_scale: str = "1"
    line_width: str | None = None
    line_opacity: str = "ff"
    poly_fill_opacity: str | None = None
    poly_fill: str | None = None
    poly_outline: str | None = None
    list_icon_href: str | None = None

    @property
    def style_url(self) -> str:
        return f"#{self.style_id}"

STYLE_SPECS: dict[str, KmzStyleSpec] = {
    "olt": KmzStyleSpec("eva_olt", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/ranger_station.png", icon_scale="0.8"),
    "fdt_288c": KmzStyleSpec("eva_fdt_288c", "#FFAA00", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "fdt_144c": KmzStyleSpec("eva_fdt_144c", "#FFFF00", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "fdt_96c": KmzStyleSpec("eva_fdt_96c", "#FF0000", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "fdt_72c": KmzStyleSpec("eva_fdt_72c", "#550000", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "fdt_48c": KmzStyleSpec("eva_fdt_48c", "#AA00FF", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "fdt_ext": KmzStyleSpec("eva_fdt_ext", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/cross-hairs.png", icon_scale="0.8"),
    "pole_9m_4inch": KmzStyleSpec("eva_pole_9m_4inch", "#FF0000", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_7m_4inch": KmzStyleSpec("eva_pole_7m_4inch", "#00FF00", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_7m_3inch": KmzStyleSpec("eva_pole_7m_3inch", "#00FFFF", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_7m_25inch": KmzStyleSpec("eva_pole_7m_25inch", "#AA00FF", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_6m_25inch": KmzStyleSpec("eva_pole_6m_25inch", "#FFFF00", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_12m_12inch": KmzStyleSpec("eva_pole_12m_12inch", "#550000", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_ulin_8m_3inch": KmzStyleSpec("eva_pole_ulin_8m_3inch", "#0000FF", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "existing_pole": KmzStyleSpec("eva_existing_pole", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "fat_8c": KmzStyleSpec("eva_fat_8c", "#00FF00", f"{GOOGLE_KML_SHAPES}/triangle.png", icon_scale="0.8"),
    "fat_16c": KmzStyleSpec("eva_fat_16c", "#FFFF00", f"{GOOGLE_KML_SHAPES}/triangle.png", icon_scale="0.8"),
    "fat_24c": KmzStyleSpec("eva_fat_24c", "#FF0000", f"{GOOGLE_KML_SHAPES}/triangle.png", icon_scale="0.8"),
    "fat_32c": KmzStyleSpec("eva_fat_32c", "#FF00FF", f"{GOOGLE_KML_SHAPES}/triangle.png", icon_scale="0.8"),
    "fat_48c": KmzStyleSpec("eva_fat_48c", "#AA00FF", f"{GOOGLE_KML_SHAPES}/triangle.png", icon_scale="0.8"),
    "slack_hanger": KmzStyleSpec("eva_slack_hanger", "#FF0000", f"{GOOGLE_KML_SHAPES}/target.png", icon_scale="0.8"),
    "hp_cover": KmzStyleSpec("eva_hp_cover", "#00FF00", f"{GOOGLE_KML_SHAPES}/homegardenbusiness.png", icon_scale="0.8"),
    "hp_uncover": KmzStyleSpec("eva_hp_uncover", "#FF0000", f"{GOOGLE_KML_SHAPES}/homegardenbusiness.png", icon_scale="0.8"),
    "handhole_120": KmzStyleSpec("eva_handhole_120x120x130", "#00FFFF", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "handhole_100": KmzStyleSpec("eva_handhole_100x100x130", "#FF00FF", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "handhole_80": KmzStyleSpec("eva_handhole_80x80x130", "#FFAA00", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "existing_handhole": KmzStyleSpec("eva_existing_handhole", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "pedestal_handhole": KmzStyleSpec("eva_pedestal_handhole", "#FFFFFF", "http://maps.google.com/mapfiles/kml/paddle/grn-blank.png", icon_scale="0.8"),
    "handhole_40": KmzStyleSpec("eva_handhole_40x40x60", "#0000FF", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "handhole_20": KmzStyleSpec("eva_handhole_20x20x20", "#00FF00", f"{GOOGLE_KML_SHAPES}/square.png", icon_scale="0.8"),
    "twey": KmzStyleSpec("eva_twey", "#FFFF00", f"{GOOGLE_KML_SHAPES}/open-diamond.png", icon_scale="0.8"),
    "distribution_cable_12c_1t": KmzStyleSpec("eva_distribution_cable_12c_1t", "#00AAFF", line_width="3"),
    "distribution_cable_24c_2t": KmzStyleSpec("eva_distribution_cable_24c_2t", "#00FF00", line_width="3"),
    "distribution_cable_36c_3t": KmzStyleSpec("eva_distribution_cable_36c_3t", "#FF00FF", line_width="3"),
    "distribution_cable_48c_4t": KmzStyleSpec("eva_distribution_cable_48c_4t", "#AA00FF", line_width="3"),
    "distribution_cable_72c_6t": KmzStyleSpec("eva_distribution_cable_72c_6t", "#550000", line_width="3"),
    "distribution_cable_96c_8t": KmzStyleSpec("eva_distribution_cable_96c_8t", "#FF0000", line_width="3"),
    "distribution_cable_144c_12t": KmzStyleSpec("eva_distribution_cable_144c_12t", "#FFFF00", line_width="3"),
    "distribution_cable_288c_24t": KmzStyleSpec("eva_distribution_cable_288c_24t", "#FFAA00", line_width="3"),
    "sling_wire": KmzStyleSpec("eva_sling_wire", "#00FFFF", line_width="3"),
    "drop_wire": KmzStyleSpec("eva_drop_wire", "#3D3D3D", line_width="3"),
    "asphalt": KmzStyleSpec("eva_asphalt", "#000000", line_width="20", line_opacity="4d"),
    "boring": KmzStyleSpec("eva_boring", "#FF0000", line_width="20", line_opacity="4d"),
    "cut_track": KmzStyleSpec("eva_cut_track", "#00AAFF", line_width="20", line_opacity="4d"),
    "crossing_rel_kereta": KmzStyleSpec("eva_crossing_rel_kereta", "#550000", line_width="20", line_opacity="4d"),
    "open_trenching": KmzStyleSpec("eva_open_trenching", "#00FF00", line_width="20", line_opacity="4d"),
    "sewer": KmzStyleSpec("eva_sewer", "#00FFFF", line_width="20", line_opacity="4d"),
    "submarine": KmzStyleSpec("eva_submarine", "#FFAA00", line_width="20", line_opacity="4d"),
    "galvanis": KmzStyleSpec("eva_galvanis", "#550000", line_width="10", line_opacity="4d"),
    "subduct_40_34": KmzStyleSpec("eva_subduct_40_34", "#FF0000", line_width="10", line_opacity="4d"),
    "subduct_32_28": KmzStyleSpec("eva_subduct_32_28", "#FF00FF", line_width="10", line_opacity="4d"),
    "subduct_3rd_party": KmzStyleSpec("eva_subduct_3rd_party", "#FFFFFF", line_width="10", line_opacity="4d"),
    "pvc_4inch": KmzStyleSpec("eva_pvc_4inch", "#FFFF00", line_width="10", line_opacity="4d"),
    "tali_pancing": KmzStyleSpec("eva_tali_pancing", "#AAAA7F", line_width="3"),
    "boundary_polygon": KmzStyleSpec(
        "eva_boundary_polygon",
        "#FFFFFF",
        line_width="1.0",
        line_opacity="ff",
        poly_fill_opacity="4d",
        poly_fill="1",
        poly_outline="1",
    ),
    "fat_area_polygon": KmzStyleSpec(
        "eva_fat_area_polygon",
        "#00FFFF",
        line_width="1.0",
        line_opacity="ff",
        poly_fill_opacity="4d",
        poly_fill="1",
        poly_outline="1",
    ),
    # Joint Closure styles (Feeder)
    "joint_closure_new_24c": KmzStyleSpec("eva_jc_new_24c", "#00FF00", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_36c": KmzStyleSpec("eva_jc_new_36c", "#FF00FF", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_48c": KmzStyleSpec("eva_jc_new_48c", "#AA00FF", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_72c": KmzStyleSpec("eva_jc_new_72c", "#550000", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_96c": KmzStyleSpec("eva_jc_new_96c", "#FF0000", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_144c": KmzStyleSpec("eva_jc_new_144c", "#FFFF00", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_new_288c": KmzStyleSpec("eva_jc_new_288c", "#FFAA00", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "joint_closure_ext": KmzStyleSpec("eva_jc_ext", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/forbidden.png", icon_scale="0.8"),
    "slack_hanger_ext": KmzStyleSpec("eva_slack_hanger_ext", "#FFFFFF", f"{GOOGLE_KML_SHAPES}/target.png", icon_scale="0.8"),
    # Feeder pole styles (additional sizes)
    "pole_7m_5inch": KmzStyleSpec("eva_pole_7m_5inch", "#FF8800", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
    "pole_9m_5inch": KmzStyleSpec("eva_pole_9m_5inch", "#880000", f"{GOOGLE_KML_SHAPES}/placemark_circle.png", icon_scale="0.8"),
}


def apply_style_overrides() -> None:
    overrides = _RULES_CONFIG.get("kmz_styles") or {}
    if not isinstance(overrides, dict):
        return
    for key, values in overrides.items():
        style_key = _rule_text(key)
        if not style_key or style_key not in STYLE_SPECS or not isinstance(values, dict):
            continue
        spec = STYLE_SPECS[style_key]
        STYLE_SPECS[style_key] = replace(
            spec,
            color=str(values.get("color") or spec.color),
            icon_href=values.get("icon_href", spec.icon_href),
            icon_scale=str(values.get("icon_scale") or spec.icon_scale),
            line_width=str(values.get("line_width")) if values.get("line_width") is not None else spec.line_width,
            line_opacity=str(values.get("line_opacity") or spec.line_opacity),
            poly_fill_opacity=str(values.get("poly_fill_opacity")) if values.get("poly_fill_opacity") is not None else spec.poly_fill_opacity,
            poly_fill=str(values.get("poly_fill")) if values.get("poly_fill") is not None else spec.poly_fill,
            poly_outline=str(values.get("poly_outline")) if values.get("poly_outline") is not None else spec.poly_outline,
            list_icon_href=values.get("list_icon_href", spec.list_icon_href),
        )


apply_style_overrides()
OBSOLETE_STYLE_IDS = {"eva_boundary_fat"}
FAT_AREA_POLYGON_FOLDERS = {"BOUNDARY FAT", "FAT AREA", "COVERAGE", "AREA"}
BOUNDARY_CLUSTER_FOLDER_NAMES = {"BOUNDARY CLUSTER"}
BOUNDARY_CLUSTER_FOLDER_ALIASES = {*BOUNDARY_CLUSTER_FOLDER_NAMES, "BOUNDRY CLUSTER"}
BOUNDARY_POLYGON_FOLDERS = {"BOUNDARY", *FAT_AREA_POLYGON_FOLDERS, *BOUNDARY_CLUSTER_FOLDER_NAMES}

DEFAULT_NEW_POLE_STYLE_BY_FOLDER = {
    "NEW POLE 9-4": "pole_9m_4inch",
    "NEW POLE 6-2.5": "pole_6m_25inch",
    "NEW POLE 12-12": "pole_12m_12inch",
    "NEW POLE ULIN 8-3": "pole_ulin_8m_3inch",
    "NEW POLE 7-4": "pole_7m_4inch",
    "NEW POLE 7-3": "pole_7m_3inch",
    "NEW POLE 7-2.5": "pole_7m_25inch",
    "NEW POLE 7-5": "pole_7m_5inch",
    "NEW POLE 9-5": "pole_9m_5inch",
}
NEW_POLE_STYLE_BY_FOLDER = kmz_rule_map("new_pole_style_by_folder", DEFAULT_NEW_POLE_STYLE_BY_FOLDER)

DEFAULT_DISTRIBUTION_CABLE_STYLE_BY_CAPACITY = {
    "12C/1T": "distribution_cable_12c_1t",
    "24C/2T": "distribution_cable_24c_2t",
    "36C/3T": "distribution_cable_36c_3t",
    "48C/4T": "distribution_cable_48c_4t",
    "72C/6T": "distribution_cable_72c_6t",
    "96C/8T": "distribution_cable_96c_8t",
    "144C/12T": "distribution_cable_144c_12t",
    "288C/24T": "distribution_cable_288c_24t",
}
DISTRIBUTION_CABLE_STYLE_BY_CAPACITY = kmz_rule_map("distribution_cable_style_by_capacity", DEFAULT_DISTRIBUTION_CABLE_STYLE_BY_CAPACITY)


@dataclass
class FdtCodingRecord:
    sheet_name: str
    row_number: int
    site_name: str
    fdt_order: int | None
    fdt_id: str
    fdt_code: str
    index_code: str
    cluster_initial: str
    area: str = ""
    olt_initial: str = ""
    hub_id: str = ""
    olt_hostname: str = ""
    olt_name_initial: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FdtPlacemarkRef:
    element: ET.Element
    folder_stack: list[str]
    source_order: int
    current_name: str

@dataclass
class OrganizerPlacemarkRef:
    parent: ET.Element
    element: ET.Element
    folder_stack: list[str]

@dataclass
class OrganizerFdtRef:
    order: int
    point: tuple[float, float]

@dataclass
class OrganizerCableRef:
    element: ET.Element
    line_order: str
    fdt_order: int
    capacity: str
    coordinates: list[list[tuple[float, float]]]

@dataclass
class OrganizerFatAreaRef:
    name: str
    fat_code: str
    line_order: str
    fdt_order: int
    polygon: list[tuple[float, float]]


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def contains_tway_alias(value: object) -> bool:
    """True for the approved T-WAY spellings anywhere in a placemark label."""
    return bool(TWAY_ALIAS_PATTERN.search(normalize_text(value)))


def is_tway_folder_alias(value: object) -> bool:
    return normalize_text(value).upper() in TWAY_FOLDER_ALIASES


def normalize_folder_name(name: str) -> str:
    """Normalize folder name: commas→dashes between digits, uppercase, clean whitespace."""
    normalized = re.sub(r"(\d)\s*,\s*(\d)", r"\1-\2", str(name or ""))
    return normalize_text(normalized).upper()


def canonical_folder_match(source_folder: str, network_type: str | None, scope_type: str | None = None) -> str | None:
    """Return canonical folder name if source_folder matches, else None.
    Uses normalize_folder_name to handle commas→dashes."""
    from converter import canonical_standard_folder
    norm_source = normalize_folder_name(source_folder)
    # UG vendor templates often spell SUBDUCT as SUB DUCT. Normalize
    # aliases before folder routing and style selection.
    ug_aliases = {
        "SUB DUCT 32/28": "SUBDUCT 32/28",
        "SUB DUCT 40/34": "SUBDUCT 40/34",
        "SUB DUCT 3RD PARTY": "SUBDUCT 3RD PARTY",
        "GALVANIZED": "GALVANIS",
        "PVC 4 INCH": "PVC 4INCH",
        "T-WAY": "T-WAY",
        "TWAY": "T-WAY",
        "T-WEY": "T-WAY",
        "TWEY": "T-WAY",
    }
    alias = ug_aliases.get(normalize_text(source_folder).upper())
    if alias:
        return alias
    # First try standard converter resolution
    canonical = canonical_standard_folder(source_folder)
    if canonical:
        return canonical
    # Fallback: check against standard folders by normalized name
    # TALI PANCING is a dynamic line folder: recognize source instances,
    # but never auto-create it as part of every template line.
    all_folders = set(LINE_STANDARD_FOLDERS + FEEDER_STANDARD_FOLDERS + ["TALI PANCING"])
    for folder in all_folders:
        if normalize_folder_name(folder) == norm_source:
            return folder
    return None


def _extract_canonical_folder_from_stack(folder_stack: Sequence[str], *prefixes: str) -> str | None:
    """Extract first matching canonical folder from folder_stack based on prefixes."""
    for folder_name in reversed(folder_stack):
        normalized = normalize_folder_name(folder_name)
        for prefix in prefixes:
            if normalized.startswith(prefix):
                # Attempt to find a full canonical match from the normalized string
                all_folders = set(LINE_STANDARD_FOLDERS + FEEDER_STANDARD_FOLDERS)
                best_match = None
                for canonical in all_folders:
                    if canonical.startswith(prefix) and canonical in normalized:
                        if best_match is None or len(canonical) > len(best_match):
                            best_match = canonical
                if best_match:
                    return best_match
    return None


def normalize_lookup_text(value: object) -> str:
    text = normalize_text(value).upper()
    text = PROJECT_CODE_PREFIX_PATTERN.sub("", text)
    text = re.sub(r"\bFDT\s*[-_ ]*\d+\b", "", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def comparable_olt_catalog_name(value: object) -> str:
    text = normalize_text(value).upper()
    text = re.sub(r"^OLT\s+", "", text)
    text = re.sub(r"^MINI\s+OLT\s+", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\b\d+\s*$", "", text)
    return re.sub(r"[^A-Z0-9]+", "", text)


def project_text_blob(root: ET.Element, project_names: Sequence[str]) -> str:
    parts = list(project_names)
    return " ".join(normalize_text(part) for part in parts if normalize_text(part))


def feeder_olt_placemark_names(root: ET.Element) -> list[str]:
    names: list[str] = []

    def walk(element: ET.Element, folder_stack: list[str]) -> None:
        for child in list(element):
            child_name = local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
                walk(child, next_stack)
                continue
            if child_name != "Placemark" or not placemark_has_point(child):
                continue
            name = normalize_text(direct_child_text_by_name(child, "name"))
            in_olt_folder = nearest_standard_folder(folder_stack) == "OLT"
            if name and (re.search(r"\bOLT\b", name, re.IGNORECASE) or in_olt_folder):
                names.append(name)

    walk(root, [])
    return names


def feeder_source_olt_reference(project_names: Sequence[str]) -> str:
    """Extract the originating OLT before the project SEGMENT marker.

    A feeder title may contain both endpoints, for example
    ``OLT_SEGARA_CITY_SEGMENT_3_TO_OLT_MEDAN_SATRIA``. Only the OLT before
    ``SEGMENT_3`` is the source used for feeder coding.
    """
    pattern = re.compile(
        r"\bOLT\s+(?P<name>.+?)\s+SEG(?:MENT|MEN)?\s*\d{1,3}\b",
        re.IGNORECASE,
    )
    for project_name in project_names:
        normalized_project_name = re.sub(r"[_-]+", " ", normalize_text(project_name))
        normalized_project_name = normalize_text(normalized_project_name)
        match = pattern.search(normalized_project_name)
        if not match:
            continue
        olt_name = re.sub(r"[\s_-]+", " ", match.group("name")).strip()
        if olt_name:
            return f"OLT {olt_name}"
    return ""


def feeder_olt_lookup(root: ET.Element, project_names: Sequence[str]) -> dict | None:
    source_olt = feeder_source_olt_reference(project_names)
    if source_olt:
        matched = lookup_olt_catalog_from_text(source_olt)
        if matched:
            return matched
    for name in feeder_olt_placemark_names(root):
        matched = lookup_olt_catalog_from_text(name)
        if matched:
            return matched
    return lookup_olt_catalog_from_text(" ".join(normalize_text(part) for part in project_names if normalize_text(part)))


def feeder_segment_initial(project_names: Sequence[str]) -> str:
    text = " ".join(normalize_text(name).upper() for name in project_names if normalize_text(name))
    match = re.search(r"\bSEG(?:MENT|MEN)?\s*[_\- ]*(\d{1,3})\b", text, re.IGNORECASE)
    if not match:
        return ""
    return f"S{int(match.group(1)):02d}"


def feeder_segment_number(project_names: Sequence[str]) -> int | None:
    segment_initial = feeder_segment_initial(project_names)
    if not segment_initial:
        return None
    return int(segment_initial[1:])


def feeder_pole_record(
    record: FdtCodingRecord | None,
    olt_match: dict | None,
    project_names: Sequence[str],
    is_subfeeder: bool = False,
) -> FdtCodingRecord:
    olt_initial = normalize_text((olt_match or {}).get("olt_initial") or "").upper()
    segment_initial = feeder_segment_initial(project_names)
    base_record = record or FdtCodingRecord(
        sheet_name="Database",
        row_number=0,
        site_name="",
        fdt_order=None,
        fdt_id="",
        fdt_code="",
        index_code="",
        cluster_initial="",
        olt_initial="",
        hub_id="",
        olt_hostname="",
        olt_name_initial="",
    )
    if is_subfeeder:
        # Subfeeder uses the matched coding record directly: OLT initial plus
        # the coding-FDT prefix, never a project SEGMENT token.
        return base_record
    if not olt_initial and not segment_initial:
        # Don't return base_record (FDT coding) - feeder must use OLT.db only
        return replace(base_record, olt_initial="", cluster_initial="")
    return replace(
        base_record,
        olt_initial=olt_initial,
        olt_name_initial=olt_initial,
        cluster_initial=segment_initial or "",
    )


def lookup_olt_catalog_from_text(text: str) -> dict | None:
    normalized_text = normalize_text(text).upper()
    data_dir = Path(__file__).resolve().parent / "data"
    db_path = data_dir / "fdt_coding.db"
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fdt_coding_olt_catalog'"
            ).fetchone()
            if table_exists:
                rows = conn.execute(
                    """
                    SELECT olt_code,
                           olt_name_initial AS olt_initial,
                           olt_initial AS olt_name,
                           olt_hostname,
                           olt_name_initial
                    FROM fdt_coding_olt_catalog
                    ORDER BY olt_code
                    """
                ).fetchall()
                for match in OLT_CODE_PATTERN.finditer(normalized_text):
                    code = match.group(0).upper()
                    row = next((candidate for candidate in rows if normalize_text(candidate["olt_code"]).upper() == code), None)
                    if row:
                        return dict(row)
                comparable_text = comparable_olt_catalog_name(normalized_text)
                for row in rows:
                    for name in (row["olt_name"], row["olt_initial"], row["olt_hostname"]):
                        clean_name = comparable_olt_catalog_name(name)
                        if clean_name and clean_name in comparable_text:
                            return dict(row)

    # Backward-compatible fallback for installations that still have OLT.db.
    legacy_db_path = data_dir / "OLT.db"
    if not legacy_db_path.exists():
        return None
    with sqlite3.connect(legacy_db_path) as conn:
        conn.row_factory = sqlite3.Row
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'olt_catalog'"
        ).fetchone()
        if not table_exists:
            return None
        for match in OLT_CODE_PATTERN.finditer(normalized_text):
            row = conn.execute(
                "SELECT olt_code, olt_initial, olt_name, olt_hostname FROM olt_catalog WHERE olt_code = ? LIMIT 1",
                (match.group(0).upper(),),
            ).fetchone()
            if row:
                return dict(row)
        candidates = conn.execute(
            "SELECT olt_code, olt_initial, olt_name, olt_hostname FROM olt_catalog ORDER BY olt_code"
        ).fetchall()
    for row in candidates:
        names = [row["olt_name"], row["olt_initial"]]
        for name in names:
            clean_name = comparable_olt_catalog_name(name)
            if clean_name and clean_name in comparable_olt_catalog_name(normalized_text):
                return dict(row)
    return None


def normalize_header(value: object) -> str:
    text = normalize_text(value).upper()
    text = text.replace("/", " ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_fdt_order(value: object) -> int | None:
    match = FDT_ORDER_PATTERN.search(str(value or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def format_excel_identifier(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"{value:03d}"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):03d}"
    text_value = normalize_text(value)
    if re.fullmatch(r"\d+", text_value):
        return f"{int(text_value):03d}"
    return text_value


def looks_like_fdt_code(value: object) -> bool:
    return bool(FDT_CODE_PATTERN.search(str(value or "")))


def spreadsheet_download_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if "docs.google.com" in parsed.netloc and "/spreadsheets/d/" in parsed.path:
        match = GOOGLE_SHEETS_ID_PATTERN.search(parsed.path)
        if match:
            query = parse_qs(parsed.query)
            fragment_query = parse_qs(parsed.fragment)
            export_query = {"format": "xlsx"}
            gid_values = query.get("gid", fragment_query.get("gid", []))
            if gid_values:
                export_query["gid"] = gid_values[0]
            return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?{urlencode(export_query)}"
    return source_url

def spreadsheet_download_candidates(source_url: str) -> list[str]:
    candidates = [spreadsheet_download_url(source_url)]
    parsed = urlparse(source_url)
    if "docs.google.com" in parsed.netloc and "/spreadsheets/d/" in parsed.path:
        match = GOOGLE_SHEETS_ID_PATTERN.search(parsed.path)
        if match:
            file_id = match.group(1)
            candidates.append(f"https://drive.google.com/uc?export=download&id={file_id}")
    else:
        candidates.append(source_url)
    return [url for index, url in enumerate(candidates) if url and url not in candidates[:index]]


def download_coding_workbook(source_url: str, output_path: str | Path, timeout: int = 12) -> Path:
    target_path = Path(output_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in spreadsheet_download_candidates(source_url):
        try:
            request = Request(
                url,
                headers={"User-Agent": "EVA KMZ Standardizer/1.0"},
            )
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            if len(payload) < 512:
                raise ValueError("payload terlalu kecil atau kosong")
            if not payload.startswith(b"PK"):
                raise ValueError("payload bukan file XLSX valid")
            target_path.write_bytes(payload)
            return target_path
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise ValueError("Gagal download spreadsheet coding FDT. " + " | ".join(errors))


def find_header_columns(row_values: Sequence[object]) -> dict[str, int] | None:
    headers = {index: normalize_header(value) for index, value in enumerate(row_values, start=1)}
    site_col = None
    fdt_no_col = None
    fdt_code_col = None
    index_col = None
    cluster_col = None
    olt_initial_col = None
    hub_id_col = None
    hub_id_fallback_col = None
    olt_hostname_col = None

    for column, header in headers.items():
        if not header:
            continue
        if site_col is None and ("SITE NAME" in header or "AREA NAMA KOMPLEK" in header):
            site_col = column
        if fdt_no_col is None and "NO FDT ID" in header:
            fdt_no_col = column
        if cluster_col is None and "INISIAL NAME CLUSTER" in header:
            cluster_col = column
        if index_col is None and "INDEX FDT" in header:
            index_col = column
        if olt_initial_col is None and ("INISIAL" in header and "OLT" in header):
            olt_initial_col = column
        if olt_initial_col is None and header == "INISIAL NAME OLT":
            olt_initial_col = column
        if hub_id_col is None and (
            header in {"HUB ID", "OLT CODE", "OLTCODE"}
            or "IP OLT" in header
            or ("CODING NUMBER" in header and "HUB" in header and "OLT" in header)
            or "INITIAL REGION" in header
        ):
            hub_id_col = column
        if olt_hostname_col is None and header in {"OLT HOSTNAME", "OLTHOSTNAME"}:
            olt_hostname_col = column
        if hub_id_fallback_col is None and ("NO HUB" in header or ("HUB" in header and "NO" in header)):
            hub_id_fallback_col = column
        if fdt_code_col is None:
            if "CODING FDT" in header:
                fdt_code_col = column
            elif header == "FDT ID":
                fdt_code_col = column

    if site_col and fdt_code_col:
        return {
            "site": site_col,
            "fdt_no": fdt_no_col or 0,
            "fdt_code": fdt_code_col,
            "index": index_col or 0,
            "cluster": cluster_col or 0,
            "olt_initial": olt_initial_col or 0,
            "hub_id": hub_id_col or hub_id_fallback_col or 0,
            "olt_hostname": olt_hostname_col or 0,
        }
    return None


def detect_header_layout(worksheet) -> tuple[int, dict[str, int]] | None:
    max_header_row = min(25, worksheet.max_row or 0)
    for row_number in range(1, max_header_row + 1):
        values = [worksheet.cell(row_number, column).value for column in range(1, (worksheet.max_column or 0) + 1)]
        columns = find_header_columns(values)
        if columns:
            return row_number, columns
    return None


def parse_fdt_coding_workbook(path: str | Path) -> list[FdtCodingRecord]:
    workbook = load_workbook(path, data_only=True, read_only=False)
    records: list[FdtCodingRecord] = []

    excluded_sheet_names = {"CREATE EMAIL SENDING"}

    for worksheet in workbook.worksheets:
        if normalize_text(worksheet.title).upper() in excluded_sheet_names:
            continue
        layout = detect_header_layout(worksheet)
        if not layout:
            continue
        header_row, columns = layout

        # Scan rows above header AND a few rows below header for sheet-level OLT initial and HUB ID
        # (OLT info is often in rows between header and first data row)
        sheet_olt_initial = ""
        sheet_hub_id = ""
        # Strategy: find the OLT row (contains "OLT" in site name column or has HUB ID pattern)
        # Then read OLT initial and HUB ID from that row using detected columns
        olt_scan_start = max(1, header_row - 5)
        olt_scan_end = min(header_row + 10, (worksheet.max_row or 0) + 1)
        for scan_row in range(olt_scan_start, olt_scan_end):
            # Skip the header row itself
            if scan_row == header_row:
                continue
            # Check if this row has OLT initial in the olt_initial column
            if columns.get("olt_initial", 0) > 0 and not sheet_olt_initial:
                val = normalize_text(worksheet.cell(scan_row, columns["olt_initial"]).value)
                if val and 2 <= len(val) <= 10 and re.match(r"[A-Z]+\d*$", val, re.IGNORECASE):
                    sheet_olt_initial = val.upper()
            # Check if this row has HUB ID in the hub_id column
            if columns.get("hub_id", 0) > 0 and not sheet_hub_id:
                val = normalize_text(worksheet.cell(scan_row, columns["hub_id"]).value)
                if val and re.match(r"[A-Z]{2,}\.\d+\.\d+", val, re.IGNORECASE):
                    sheet_hub_id = val
            # Also scan all cells for HUB ID pattern if column not detected
            if not sheet_hub_id:
                for scan_col in range(1, min((worksheet.max_column or 0) + 1, 10)):
                    val = normalize_text(worksheet.cell(scan_row, scan_col).value)
                    if val and re.match(r"[A-Z]{2,}\.\d+\.\d{3,}", val, re.IGNORECASE):
                        sheet_hub_id = val
                        break
        # Fallback: scan columns near cluster column for OLT initial
        # OLT initial is typically a short (2-4 char) value that repeats in every data row
        if not sheet_olt_initial:
            candidate_cols = []
            # Check columns around the cluster column and site column
            check_cols = set()
            if columns.get("cluster") and columns["cluster"] > 0:
                for offset in range(-2, 3):
                    col = columns["cluster"] + offset
                    if col > 0 and col != columns.get("cluster") and col != columns.get("site") and col != columns.get("fdt_code"):
                        check_cols.add(col)
            # Also check all columns up to 15
            for col in range(1, min(16, (worksheet.max_column or 0) + 1)):
                if col not in {columns.get("site"), columns.get("fdt_code"), columns.get("fdt_no"), columns.get("cluster"), columns.get("index")}:
                    check_cols.add(col)
            for col in sorted(check_cols):
                values = []
                for scan_row in range(header_row + 1, min(header_row + 10, (worksheet.max_row or 0) + 1)):
                    val = normalize_text(worksheet.cell(scan_row, col).value)
                    if val:
                        values.append(val.upper())
                if not values:
                    continue
                # OLT initial: same short value in most rows
                most_common = max(set(values), key=values.count)
                if 2 <= len(most_common) <= 5 and values.count(most_common) >= len(values) * 0.8 and most_common.isalpha():
                    sheet_olt_initial = most_common
                    break

        for row_number in range(header_row + 1, (worksheet.max_row or 0) + 1):
            site_name = normalize_text(worksheet.cell(row_number, columns["site"]).value)
            fdt_code = normalize_text(worksheet.cell(row_number, columns["fdt_code"]).value)
            fdt_id = format_excel_identifier(worksheet.cell(row_number, columns["fdt_no"]).value) if columns["fdt_no"] else ""
            index_code = normalize_text(worksheet.cell(row_number, columns["index"]).value) if columns["index"] else ""
            cluster_initial = normalize_text(worksheet.cell(row_number, columns["cluster"]).value) if columns["cluster"] else ""
            if not site_name:
                continue
            if not looks_like_fdt_code(fdt_code):
                fdt_code = cluster_initial
            if not fdt_code:
                continue
            olt_initial = ""
            if columns.get("olt_initial", 0) > 0:
                olt_initial = normalize_text(worksheet.cell(row_number, columns["olt_initial"]).value)
            hub_id = ""
            if columns.get("hub_id", 0) > 0:
                hub_id = normalize_text(worksheet.cell(row_number, columns["hub_id"]).value)
            olt_hostname = ""
            if columns.get("olt_hostname", 0) > 0:
                olt_hostname = normalize_text(worksheet.cell(row_number, columns["olt_hostname"]).value)
            if hub_id and not looks_like_hub_id(hub_id):
                hub_id = ""
            # Use sheet-level values as fallback
            if not olt_initial:
                olt_initial = sheet_olt_initial
            if not hub_id:
                hub_id = sheet_hub_id
            records.append(
                FdtCodingRecord(
                    sheet_name=worksheet.title,
                    row_number=row_number,
                    site_name=site_name,
                    fdt_order=extract_fdt_order(site_name),
                    fdt_id=fdt_id,
                    fdt_code=fdt_code.upper(),
                    index_code=index_code,
                    cluster_initial=cluster_initial,
                    olt_initial=olt_initial.upper() if olt_initial else "",
                    hub_id=hub_id,
                    olt_hostname=olt_hostname.upper() if olt_hostname else "",
                    olt_name_initial=olt_initial.upper() if olt_initial else "",
                )
            )

    return records


def lookup_score(project_name: str, site_name: str) -> float:
    project_key = normalize_lookup_text(project_name)
    site_key = normalize_lookup_text(site_name)
    if not project_key or not site_key:
        return 0.0
    if project_key == site_key:
        return 1.0
    if project_key in site_key or site_key in project_key:
        shorter = min(len(project_key), len(site_key))
        longer = max(len(project_key), len(site_key))
        coverage = shorter / max(longer, 1)
        if shorter < 6 or coverage < 0.2:
            return 0.0
        return 0.72 + (0.27 * coverage)
    return difflib.SequenceMatcher(None, project_key, site_key).ratio()


def select_project_records(records: Sequence[FdtCodingRecord], project_names: Sequence[str]) -> tuple[list[FdtCodingRecord], str, float]:
    best_name = ""
    best_key = ""
    best_score = 0.0

    for project_name in project_names:
        for record in records:
            score = lookup_score(project_name, record.site_name)
            if score > best_score:
                best_name = project_name
                best_key = normalize_lookup_text(record.site_name)
                best_score = score

    if not best_key or best_score < 0.72:
        return [], best_name, best_score

    matched = [
        record
        for record in records
        if normalize_lookup_text(record.site_name) == best_key
    ]
    matched.sort(key=lambda item: (item.fdt_order if item.fdt_order is not None else 9999, item.row_number))
    return matched, best_name, best_score


def subfeeder_lookup_key(value: object) -> str:
    key = normalize_lookup_text(value)
    key = re.sub(r"\b(?:SF|SUBFEEDER|SUB\s+FEEDER|FEEDER|ASPLAN|AS\s+PLAN|KMZ|KML)\b", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def select_records_by_substring(records: Sequence[FdtCodingRecord], keyword: str) -> list[FdtCodingRecord]:
    key = subfeeder_lookup_key(keyword)
    if not key:
        return []
    tokens = key.split()
    best_site_key = ""
    best_score = 0
    for r in records:
        site_key = normalize_lookup_text(r.site_name)
        haystack = f"{subfeeder_lookup_key(r.site_name)} {subfeeder_lookup_key(r.cluster_initial)} {subfeeder_lookup_key(r.fdt_code)}"
        if all(token in haystack for token in tokens):
            score = sum(1 for token in tokens if token in haystack)
            if score > best_score:
                best_score = score
                best_site_key = site_key
    if not best_site_key:
        return []
    results = [r for r in records if normalize_lookup_text(r.site_name) == best_site_key]
    results.sort(key=lambda item: (item.fdt_order if item.fdt_order is not None else 9999, item.row_number))
    return results


def _fdt_sharing_code_from_name(value: str | None) -> str:
    """Return shared FDT code from a placemark name.
    
    For HH names like SGCF.008.HH053, strips the .HHxxx suffix first
    to extract the parent FDT code (SGCF.008).
    """
    text = normalize_text(value).upper()
    if not text:
        return ""
    # Strip .HHxxx or .HANDHOLE suffix to get parent FDT code
    stripped = re.sub(r"\.HH\d+$", "", text)
    stripped = re.sub(r"\.HANDHOLE\d*$", "", stripped)
    code = existing_fdt_code_from_name(stripped)
    if not code or code.startswith("XXX"):
        return ""
    return code


def _shared_fdt_codes_for_fallback(
    root: ET.Element,
    initial_fdt_placemarks: Sequence[FdtPlacemarkRef],
) -> list[str]:
    """Resolve fallback FDT codes without treating unrelated HP codes as FDTs.

    Prefer placemarks already classified as FDT by source folder context. Only
    scan the whole KMZ for sharing codes when those actual FDT placemarks are
    missing or placeholders (for legacy FDT-sharing files with HH-coded names).
    """
    actual_codes: list[str] = []
    for placemark in initial_fdt_placemarks:
        code = _fdt_sharing_code_from_name(placemark.current_name)
        if code and code not in actual_codes:
            actual_codes.append(code)
    if actual_codes:
        return actual_codes

    sharing_codes: list[str] = []
    for placemark in root.iter():
        if local_name(placemark.tag) != "Placemark":
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name")).upper()
        if ".HH" not in name and "HANDHOLE" not in name:
            continue
        code = _fdt_sharing_code_from_name(name)
        if code and code not in sharing_codes:
            sharing_codes.append(code)
    return sharing_codes


def _fdt_sharing_records_from_codes(codes: Sequence[str], site_name: str) -> list[FdtCodingRecord]:
    seen: list[str] = []
    for code in codes:
        code = normalize_text(code).upper()
        if code and code not in seen and not code.startswith("XXX"):
            seen.append(code)
    return [
        FdtCodingRecord(
            sheet_name="FDT_SHARING_KMZ",
            row_number=order,
            site_name=site_name,
            fdt_order=order,
            fdt_id="",
            fdt_code=code,
            index_code=code,
            cluster_initial=code,
        )
        for order, code in enumerate(seen, start=1)
    ]


def default_fdt_coding_records(fdt_placemarks: Sequence[FdtPlacemarkRef], project_name: str) -> list[FdtCodingRecord]:
    site_name = normalize_project_folder_name(project_name) or "Default XXX"
    sharing_codes: list[str] = []
    for placemark in fdt_placemarks:
        name = normalize_text(direct_child_text_by_name(placemark.element, "name"))
        code = _fdt_sharing_code_from_name(name)
        if code and code not in sharing_codes:
            sharing_codes.append(code)

    sharing_records = _fdt_sharing_records_from_codes(sharing_codes, site_name)
    if sharing_records:
        return sharing_records

    count = max(len(fdt_placemarks), 1)
    return [
        FdtCodingRecord(
            sheet_name="DEFAULT",
            row_number=0,
            site_name=site_name,
            fdt_order=order,
            fdt_id="",
            fdt_code="XXX",
            index_code="XXX",
            cluster_initial="XXX",
        )
        for order in range(1, count + 1)
    ]


def kml_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{") and "}" in root.tag:
        return root.tag[1:].split("}", 1)[0]
    return KML_NAMESPACE


def kml_tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}" if namespace else name


def ensure_direct_child(element: ET.Element, name: str, namespace: str) -> ET.Element:
    child = direct_child_by_name(element, name)
    if child is not None:
        return child
    child = ET.Element(kml_tag(namespace, name))
    element.insert(0, child)
    return child

def ensure_style_url_child(placemark: ET.Element, namespace: str) -> ET.Element:
    child = direct_child_by_name(placemark, "styleUrl")
    if child is not None:
        return child

    child = ET.Element(kml_tag(namespace, "styleUrl"))
    insert_at = 0
    for index, existing in enumerate(list(placemark)):
        if local_name(existing.tag) in {"name", "visibility", "open", "description"}:
            insert_at = index + 1
    placemark.insert(insert_at, child)
    return child

def rgb_hex_to_kml_color(value: str) -> str:
    return rgb_hex_to_kml_color_with_opacity(value, "ff")

def rgb_hex_to_kml_color_with_opacity(value: str, opacity: str) -> str:
    normalized = re.sub(r"[^0-9A-F]", "", str(value or "").upper())
    if len(normalized) != 6:
        raise ValueError(f"Warna RGB tidak valid: {value}")
    alpha = re.sub(r"[^0-9A-F]", "", str(opacity or "ff").upper())[:2].rjust(2, "F")
    red, green, blue = normalized[0:2], normalized[2:4], normalized[4:6]
    return f"{alpha}{blue}{green}{red}".lower()

def document_element(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if local_name(element.tag) == "Document":
            return element
    return root

def add_text_child(parent: ET.Element, namespace: str, name: str, text: str) -> ET.Element:
    child = ET.SubElement(parent, kml_tag(namespace, name))
    child.text = text
    return child

def build_standard_style(spec: KmzStyleSpec, namespace: str) -> ET.Element:
    style = ET.Element(kml_tag(namespace, "Style"), {"id": spec.style_id})
    if spec.icon_href:
        icon_style = ET.SubElement(style, kml_tag(namespace, "IconStyle"))
        add_text_child(icon_style, namespace, "color", rgb_hex_to_kml_color(spec.color))
        add_text_child(icon_style, namespace, "scale", spec.icon_scale)
        icon = ET.SubElement(icon_style, kml_tag(namespace, "Icon"))
        add_text_child(icon, namespace, "href", spec.icon_href)
    if spec.line_width:
        line_style = ET.SubElement(style, kml_tag(namespace, "LineStyle"))
        add_text_child(line_style, namespace, "color", rgb_hex_to_kml_color_with_opacity(spec.color, spec.line_opacity))
        add_text_child(line_style, namespace, "width", spec.line_width)
    if spec.poly_fill_opacity is not None:
        poly_style = ET.SubElement(style, kml_tag(namespace, "PolyStyle"))
        add_text_child(poly_style, namespace, "color", rgb_hex_to_kml_color_with_opacity(spec.color, spec.poly_fill_opacity))
        if spec.poly_fill is not None:
            add_text_child(poly_style, namespace, "fill", spec.poly_fill)
        if spec.poly_outline is not None:
            add_text_child(poly_style, namespace, "outline", spec.poly_outline)
    if spec.list_icon_href:
        list_style = ET.SubElement(style, kml_tag(namespace, "ListStyle"))
        item_icon = ET.SubElement(list_style, kml_tag(namespace, "ItemIcon"))
        add_text_child(item_icon, namespace, "state", "open closed")
        add_text_child(item_icon, namespace, "href", spec.list_icon_href)
    return style

def ensure_standard_style_definitions(root: ET.Element, namespace: str) -> None:
    document = document_element(root)
    style_ids = {spec.style_id for spec in STYLE_SPECS.values()}
    for child in list(document):
        if local_name(child.tag) == "Style" and child.attrib.get("id") in style_ids | OBSOLETE_STYLE_IDS:
            document.remove(child)

    insert_at = 0
    for index, child in enumerate(list(document)):
        if local_name(child.tag) in {"name", "visibility", "open", "Style", "StyleMap"}:
            insert_at = index + 1

    for spec in STYLE_SPECS.values():
        document.insert(insert_at, build_standard_style(spec, namespace))
        insert_at += 1

def remove_inline_styles(
    placemark: ET.Element,
    *,
    preserve_extension_payload: bool = False,
) -> int:
    removed = 0
    for child in list(placemark):
        local = local_name(child.tag)
        # Remove inline Style and StyleMap (we use external styles)
        if local in {"Style", "StyleMap"}:
            placemark.remove(child)
            removed += 1
        # Sling routing owns the original Placemark. Its extension payload must
        # survive while only visual inline styles are replaced.
        elif preserve_extension_payload:
            continue
        # Remove atom:link elements that cause hyperlink styling in Google Earth
        elif local == "link" and "atom" in (child.tag or ""):
            placemark.remove(child)
            removed += 1
        # Remove ExtendedData with atom:link or href attributes
        elif local == "ExtendedData":
            for sub in list(child):
                if "atom" in (sub.tag or "") or local_name(sub.tag) == "link":
                    child.remove(sub)
    return removed

MAPMARKER_EMPTY_DESCRIPTION_PATTERN = re.compile(
    r"<pre\s+id=[\"']com\.exlyo\.mapmarker\.description_p_tag[\"']\s*>\s*</pre>",
    re.IGNORECASE,
)


def looks_like_empty_mapmarker_description(value: str | None) -> bool:
    text = normalize_text(value or "")
    if not text:
        return False
    return bool(MAPMARKER_EMPTY_DESCRIPTION_PATTERN.fullmatch(text))


def remove_empty_mapmarker_descriptions(element: ET.Element) -> int:
    removed = 0
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Placemark":
            for grandchild in list(child):
                if local_name(grandchild.tag) == "description" and looks_like_empty_mapmarker_description(grandchild.text):
                    child.remove(grandchild)
                    removed += 1
        elif child_name in {"Document", "Folder"}:
            removed += remove_empty_mapmarker_descriptions(child)
    return removed

def remove_non_rule_descriptions(element: ET.Element, folder_stack: list[str] | None = None) -> int:
    """Remove stale user descriptions while preserving descriptions generated by EVA rules."""
    folder_stack = folder_stack or []
    removed = 0
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            folder_name = normalize_text(direct_child_text_by_name(child, "name")) if child_name == "Folder" else ""
            removed += remove_non_rule_descriptions(child, folder_stack + ([folder_name] if folder_name else []))
            continue
        if child_name != "Placemark":
            continue
        nearest_folder = nearest_standard_folder(folder_stack)
        if nearest_folder in {"DISTRIBUTION CABLE", "CABLE"}:
            continue
        description = direct_child_by_name(child, "description")
        if description is None:
            continue
        if nearest_folder == "FDT" and detect_fdt_capacity_from_description(description.text):
            continue
        child.remove(description)
        removed += 1
    return removed

def normalized_style_url(value: str | None) -> str:
    normalized = normalize_text(value)
    if normalized and not normalized.startswith("#") and "://" not in normalized:
        normalized = f"#{normalized}"
    return normalized

def placemark_has_geometry(placemark: ET.Element, geometry_names: set[str] | None = None) -> bool:
    targets = geometry_names or {"Point", "LineString", "Polygon"}
    return any(local_name(element.tag) in targets for element in placemark.iter())

def placemark_has_point(placemark: ET.Element) -> bool:
    return placemark_has_geometry(placemark, {"Point"})

def placemark_has_line(placemark: ET.Element) -> bool:
    return placemark_has_geometry(placemark, {"LineString"})

LEGACY_FAT_FDT_NAME_PATTERN = re.compile(r"\bFDT\s*[-_ ]*(?P<fdt>\d+)\s*[._ -]+(?P<fat>[A-Z]\d{2})\b", re.IGNORECASE)

def looks_like_fdt_placemark(placemark: ET.Element, folder_stack: Sequence[str]) -> bool:
    if not placemark_has_point(placemark):
        return False
    normalized_stack = [normalize_folder_name(folder) for folder in folder_stack]
    if any(folder == "HOMEPASS" for folder in normalized_stack):
        return False
    source_folder = nearest_standard_folder(folder_stack)
    if source_folder in {"FAT", "SLACK HANGER"}:
        return False

    name = normalize_text(direct_child_text_by_name(placemark, "name"))
    if LEGACY_FAT_FDT_NAME_PATTERN.search(name):
        return False
    description = normalize_text(direct_child_text_by_name(placemark, "description"))
    style_url = normalized_style_url(direct_child_text_by_name(placemark, "styleUrl")).upper()
    search_text = f"{name} {description}"
    upper_text = search_text.upper()

    if find_fat_code(name) and "FDT" not in upper_text:
        return False
    if STANDARD_POLE_NUMBER_PATTERN.search(name) or EXT_PARTNER_BARE_PATTERN.search(name):
        return False
    if MR_EXPANDED_POLE_PATTERN.search(name):
        return False
    if HP_COVER_NAME_PATTERN.search(name):
        return False
    if "HH" in upper_text or "HANDHOLE" in upper_text or "PEDESTAL" in upper_text:
        # An explicit FDT identity may contain vendor HH80 notes; a coded HH
        # without the FDT prefix must never become an FDT anchor.
        if not (source_folder == "FDT" and re.match(
            r"^FDT\s*\d+\s+[A-Z][A-Z0-9-]*\.\d{2,3}\b", name, re.IGNORECASE
        )):
            return False
    if re.search(r"\b(?:FAT|SLACK|HANGER|TWAY|TWEY|T-WAY|T-WEY)\b", upper_text):
        return False

    if re.search(r"\bFDT\b", upper_text):
        return True
    if nearest_standard_folder(folder_stack) == "FDT" and name.upper() in {"EXT", "XXX", "FDT", "FDT XXX"}:
        return True

    has_fdt_code = looks_like_fdt_code(name) and not find_fat_code(name)
    if not has_fdt_code:
        return False

    has_fdt_context = (
        bool(extract_fdt_order(" ".join(folder_stack)))
        or nearest_standard_folder(folder_stack) == "FDT"
        or "FDT" in style_url
        or "CROSS-HAIRS" in style_url
    )
    return has_fdt_context

def extract_parent_fdt_order(folder_stack: Sequence[str]) -> int:
    for folder_name in reversed(folder_stack):
        order = extract_fdt_order(folder_name)
        if order:
            return order
    return 1

FAT_LEGACY_FDT_NAME_PATTERN = LEGACY_FAT_FDT_NAME_PATTERN

def standardize_fat_placemark_names(
    element: ET.Element,
    folder_stack: list[str],
    records: Sequence[FdtCodingRecord],
    namespace: str,
    changes: list[dict],
) -> None:
    def _extract_fat_suffix(name: str) -> str | None:
        text = normalize_text(name).upper()
        if not text:
            return None
        # Preferred modern forms: XXX-1.A01, FDT 1 A01, FDT-1_A01, etc.
        modern = re.search(r"(?:^|\b)(?:[A-Z0-9]+[._ -]*)?(?:FDT\s*[-_ ]*\d+|XXX-\d+|RK-\d+|RR-\d+|TK-\d+)?[._ -]+(?P<fat>[A-Z]\d{2})\b", text)
        if modern:
            return modern.group("fat").upper()
        # Raw suffix-only fallback: A01 / B02 / C03, etc.
        raw = re.search(r"\b(?P<fat>[A-Z]\d{2})\b", text)
        if raw:
            return raw.group("fat").upper()
        return None

    def _base_cluster_key(name: str) -> str:
        text = normalize_text(name).upper()
        text = re.sub(r"\s*-\s*FDT\s*\d+\b", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _best_record_for_fat(folder_stack: list[str], records: Sequence[FdtCodingRecord]) -> FdtCodingRecord | None:
        order = extract_parent_fdt_order(folder_stack)
        if order is not None:
            rec = record_for_fdt_order(records, order)
            if rec:
                return rec
        # Fallback: choose the record whose site_name base matches the folder stack base.
        base_stack = next((normalize_text(n).upper() for n in reversed(folder_stack) if "FDT" not in normalize_text(n).upper()), "")
        base_stack = _base_cluster_key(base_stack)
        best = None
        best_score = 0
        for rec in records:
            base_site = _base_cluster_key(rec.site_name)
            if not base_site:
                continue
            score = (base_stack in base_site) + (base_site in base_stack) + (base_stack == base_site)
            if score > best_score:
                best_score = score
                best = rec
        return best

    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
            standardize_fat_placemark_names(child, next_stack, records, namespace, changes)
            continue
        if child_name != "Placemark" or nearest_standard_folder(folder_stack) != "FAT":
            continue
        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        suffix = _extract_fat_suffix(old_name)
        if not suffix:
            continue
        record = _best_record_for_fat(folder_stack, records)
        fdt_code = (normalize_text(record.fdt_code).upper() if record and normalize_text(record.fdt_code) else None)
        new_name = f"{fdt_code}.{suffix}" if fdt_code else suffix
        if old_name == new_name:
            continue
        name_element.text = new_name
        changes.append({"from": old_name, "to": new_name, "folder": "FAT"})

def extract_parent_line_order(folder_stack: Sequence[str], *values: str | None) -> str:
    for value in [*reversed(folder_stack), *values]:
        text = normalize_text(value)
        if not text.upper().startswith("LINE"):
            continue
        match = LINE_ORDER_PATTERN.search(text)
        if match:
            return match.group(1).upper()
    return "A"

def extract_line_folder_order(folder_stack: Sequence[str]) -> str | None:
    for value in reversed(folder_stack):
        text = normalize_text(value)
        if not text.upper().startswith("LINE"):
            continue
        match = LINE_ORDER_PATTERN.search(text)
        if match:
            return match.group(1).upper()
    return None

def format_line_folder_name(line_order: str | None, fdt_order: int | None) -> str:
    line_order = (line_order or "A").upper()
    fdt_order = max(1, int(fdt_order or 1))
    return f"LINE {line_order} - FDT {fdt_order}"

def is_line_folder_name(value: str | None) -> bool:
    text = normalize_text(value)
    if not text.upper().startswith("LINE"):
        return False
    # Final EVA output must use LINE X - FDT N. Accept legacy plain LINE X too so old inputs can be normalized/reordered.
    return re.fullmatch(r"LINE\s+[A-Z](?:\s*-\s*FDT\s+\d+)?", text.upper()) is not None

def current_line_folder_name(folder_stack: Sequence[str], line_order: str | None, fdt_order: int | None) -> str:
    return format_line_folder_name(line_order, fdt_order)



# ── AI safe fallback helpers ──
def _ai_nearest_cable_context(point, cable_refs, limit=5):
    """Rank cable refs by distance to point, return top N as dicts."""
    if point is None:
        return []
    ranked = []
    for ref in cable_refs or []:
        coords = getattr(ref, 'coordinates', None)
        if not coords:
            continue
        try:
            dist = min(((point[0]-lon)**2 + (point[1]-lat)**2) for lon, lat in coords)
        except Exception:
            continue
        ranked.append((dist, ref))
    ranked.sort(key=lambda x: x[0])
    return [{'line_order': getattr(r, 'line_order', None), 'fdt_order': getattr(r, 'fdt_order', None), 'name': getattr(r, 'name', '')} for _, r in ranked[:limit]]


def _ai_nearest_fat_codes(point, fat_areas, limit=6):
    """Rank FAT areas by centroid distance, return top N codes."""
    if point is None:
        return []
    ranked = []
    for ref in fat_areas or []:
        poly = getattr(ref, 'polygon', None)
        if not poly:
            continue
        center = polygon_centroid(poly)
        if center is None:
            continue
        dist = (point[0]-center[0])**2 + (point[1]-center[1])**2
        code = getattr(ref, 'fat_code', None) or getattr(ref, 'name', None)
        if code:
            ranked.append((dist, code))
    ranked.sort(key=lambda x: x[0])
    seen = []
    for _, code in ranked:
        if code not in seen:
            seen.append(code)
        if len(seen) >= limit:
            break
    return seen


def _ai_kmz_target_fallback(*, placemark, name, folder_stack, point, source_folder, line_prefix, cable_refs, fat_areas):
    """Ask AI to suggest folder_path for a placemark that fell through all deterministic rules."""
    if not ai_assist_enabled():
        return None
    suggestion = suggest_kmz_target(
        placemark_name=name,
        folder_stack=list(folder_stack or []),
        point=point,
        nearest_fat_codes=_ai_nearest_fat_codes(point, fat_areas),
        nearest_cables=_ai_nearest_cable_context(point, cable_refs),
        candidate_line_prefix=line_prefix,
        source_folder=source_folder,
    )
    if not suggestion:
        return None
    path = suggestion.get('folder_path') or []
    if not path:
        return None
    # Safety: if HP COVER without FAT code, add one
    if len(path) >= 2 and path[1] == 'HP COVER' and len(path) == 2:
        nearest = _ai_nearest_fat_codes(point, fat_areas, limit=1)
        if nearest:
            path = [path[0], path[1], hp_cover_fat_folder_name(nearest[0])]
    # Safety: if BOUNDARY FAT, rename polygon to FAT code
    if len(path) >= 2 and path[1] == 'BOUNDARY FAT':
        nearest = _ai_nearest_fat_codes(point, fat_areas, limit=1)
        if nearest:
            name_element = direct_child_by_name(placemark, 'name')
            if name_element is not None:
                current = (name_element.text or '').strip().lower()
                if current in ('', 'face', name.lower()):
                    name_element.text = nearest[0]
    return path or None

def line_standard_folders_for_network(network_type: str | None) -> list[str]:
    """Return standard subfolder display order inside each LINE folder.

    Template order for Cluster AE/UG:
    BOUNDARY FAT, FAT, HP COVER, HP UNCOVER -> network-specific infra/poles -> DISTRIBUTION CABLE, SLACK HANGER, SLING WIRE.
    Keep BOUNDARY in source constants for legacy parsing, but omit it from the
    output order because it is not part of the supplied LINE templates.
    """
    normalized = normalize_text(network_type).upper()
    first = [folder for folder in LINE_COMMON_FOLDERS if folder in {"BOUNDARY FAT", "FAT", "HP COVER", "HP UNCOVER"}]
    tail = [folder for folder in LINE_COMMON_FOLDERS if folder in {"DISTRIBUTION CABLE", "SLACK HANGER"}]
    if normalized == "UG":
        return first + [f for f in LINE_UG_FOLDERS if f not in set(first)]
    return first + [f for f in LINE_AE_FOLDERS if f not in set(first) and f not in set(tail) and f != "SLING WIRE"] + tail + ["SLING WIRE"]

def feeder_standard_folders_for_network(network_type: str | None, scope_type: str | None = "Feeder", extra_folders: Sequence[str] | None = None) -> list[str]:
    """Return the exact Feeder/Subfeeder folder order from the selected template."""
    normalized = normalize_text(network_type).upper()
    if normalize_scope_type(scope_type) == "Subfeeder":
        base = list(SUBFEEDER_UG_FOLDERS if normalized == "UG" else SUBFEEDER_AE_FOLDERS)
    else:
        base = list(FEEDER_UG_FOLDERS if normalized == "UG" else FEEDER_AE_FOLDERS)
    return base


def feeder_extra_folders_from_source(folder_stack: Sequence[str], target_folder_name: str | None, scope_type: str | None) -> list[str]:
    """Canonical cross-network folders actually present/needed in feeder source."""
    extras: list[str] = []
    allowed = feeder_standard_folder_set_for_scope(scope_type)
    for raw in [*folder_stack, target_folder_name or ""]:
        canonical = canonical_folder_match(raw, None, scope_type) if raw else None
        if canonical and canonical in allowed and canonical not in FEEDER_OPTIONAL_FOLDERS and canonical not in extras:
            extras.append(canonical)
    return extras

def feeder_standard_folder_set_for_scope(scope_type: str | None) -> set[str]:
    if normalize_scope_type(scope_type) == "Subfeeder":
        return {folder for folder in FEEDER_STANDARD_FOLDER_SET if folder != "OLT"}
    return FEEDER_STANDARD_FOLDER_SET

def is_subfeeder_project(project_name: str | None) -> bool:
    """Detect if project name indicates Subfeeder scope (contains SF marker)."""
    return bool(SUBFEEDER_SF_PATTERN.search(project_name or ""))


def is_feeder_project(project_name: str | None) -> bool:
    """Detect Feeder scope from F + 3-letter area code, optionally followed by digits."""
    text = project_name or ""
    return bool(FEEDER_AREA_CODE_PATTERN.search(text))


def normalize_scope_type(scope_type: str | None) -> str | None:
    if scope_type is None:
        return None
    raw = str(scope_type).strip().lower()
    if raw in {"feeder", "subfeeder", "sub feeder", "sub-feeder"}:
        return "Subfeeder" if raw != "feeder" else "Feeder"
    if raw == "cluster":
        return "Cluster"
    return None


def is_feeder_like_scope(scope_type: str | None) -> bool:
    return normalize_scope_type(scope_type) in {"Feeder", "Subfeeder"}


def is_cluster_ug_scope(scope_type: str | None, network_type: str | None) -> bool:
    """Enable LINE-local SUBDUCT/T-WAY/TALI rules only for Cluster UG."""
    return (
        normalize_scope_type(scope_type) == "Cluster"
        and normalize_text(network_type).upper() == "UG"
    )

def fat_code_from_folder_stack(folder_stack: Sequence[str]) -> str | None:
    """Extract FAT code (e.g. A01, B09) from folder stack if present."""
    for folder_name in reversed(folder_stack):
        normalized = normalize_text(folder_name).upper()
        fat_code = find_fat_code(normalized)
        if fat_code:
            return fat_code
    return None

def fat_area_for_code(fat_code: str, fat_areas: Sequence[OrganizerFatAreaRef]) -> OrganizerFatAreaRef | None:
    """Find fat area ref by code."""
    for area in fat_areas:
        if area.fat_code and area.fat_code.upper() == fat_code.upper():
            return area
    return None

def child_folder_name_after_standard_folder(folder_stack: Sequence[str], source_folder: str) -> str | None:
    for index, folder_name in enumerate(folder_stack):
        if canonical_standard_folder(folder_name) != source_folder:
            continue
        if index + 1 >= len(folder_stack):
            return None
        child_name = normalize_text(folder_stack[index + 1])
        if canonical_standard_folder(child_name) or is_line_folder_name(child_name):
            return None
        return child_name or None
    return None

def detect_distribution_cable_capacity(*values: str | None) -> str | None:
    search_text = " ".join(value or "" for value in values)
    match = CABLE_CAPACITY_PATTERN.search(search_text)
    if not match:
        return None
    core = match.group(1).upper()
    tube = _DEFAULT_TUBE_BY_CORE.get(core, "2")
    return f"{core}C/{tube.upper()}T"

def detect_fdt_capacity_from_description(description: str | None) -> int | None:
    match = FDT_CAPACITY_DESCRIPTION_PATTERN.search(description or "")
    if match:
        return int(match.group(1))
    # Fallback: cek pattern "48 CORE", "72 CORE", "144 CORE" tanpa prefix FDT
    bare_match = re.search(r"\b(24|48|72|144)\s*(?:CORE|C)\b", description or "", re.IGNORECASE)
    return int(bare_match.group(1)) if bare_match else None


def preserve_fdt_capacity_metadata(
    placemark: ET.Element,
    folder_stack: Sequence[str],
    namespace: str,
) -> int | None:
    """Persist an FDT capacity found in source folders before reorganization."""
    existing_description = normalize_text(direct_child_text_by_name(placemark, "description"))
    existing_capacity = detect_fdt_capacity_from_description(existing_description)
    if existing_capacity:
        return existing_capacity
    name = normalize_text(direct_child_text_by_name(placemark, "name"))
    match = FDT_CAPACITY_LOOSE_PATTERN.search(" ".join([name, *folder_stack]))
    if not match:
        return None
    capacity = int(match.group(1))
    description_element = ensure_direct_child(placemark, "description", namespace)
    capacity_note = f"FDT Capacity: {capacity} CORE"
    description_element.text = (
        f"{existing_description}\n{capacity_note}"
        if existing_description
        else capacity_note
    )
    return capacity

def fdt_capacity_for_fat_count(fat_count: int) -> int:
    return 48 if fat_count <= 20 else 72

def distribution_capacity_for_fat_count(fat_count: int) -> str:
    if fat_count <= 10:
        return "24C/2T"
    if fat_count <= 15:
        return "36C/3T"
    return "48C/4T"

def detect_network_type(*values: str | None) -> str:
    search_text = " ".join(value or "" for value in values)
    match = NETWORK_TYPE_PATTERN.search(search_text)
    return match.group(1).upper() if match else "AE"

def number_from_text(pattern: re.Pattern, value: str | None) -> float | None:
    match = pattern.search(value or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None

def rounded_meter(value: float | int | None) -> int:
    if value is None:
        return 0
    return max(0, int(math.floor(float(value) + 0.5)))

def route_length_meter_for_placemark(placemark: ET.Element) -> int:
    description = direct_child_text_by_name(placemark, "description")
    described_route = number_from_text(TOTAL_ROUTE_PATTERN, description)
    if described_route is not None:
        return rounded_meter(described_route)
    return rounded_meter(calculate_linestring_length_meter(placemark_linestrings(placemark)))

def existing_fdt_code_from_name(name: str | None) -> str:
    match = FDT_CODE_PATTERN.search(name or "")
    return match.group(0).upper() if match else ""

def explicit_line_order(*values: str | None) -> str | None:
    for value in values:
        match = LINE_ORDER_PATTERN.search(value or "")
        if match:
            return match.group(1).upper()
        short_match = re.search(r"(?<![A-Z0-9])L([A-E])(?![A-Z0-9])", value or "", re.IGNORECASE)
        if short_match:
            return short_match.group(1).upper()
        fat_code = find_fat_code(value)
        if fat_code:
            return fat_code[0].upper()
    return None

def find_fat_code(value: str | None) -> str | None:
    match = FAT_CODE_PATTERN.search(value or "")
    return match.group(1).upper() if match else None

def hp_cover_fat_folder_name(value: str | None) -> str:
    fat_code = find_fat_code(value)
    if fat_code:
        return fat_code
    return normalize_text(value) or "UNKNOWN"

def placemark_point_coordinate(placemark: ET.Element) -> tuple[float, float] | None:
    for point in placemark.iter():
        if local_name(point.tag) != "Point":
            continue
        coordinate_element = direct_child_by_name(point, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if coordinates:
            return coordinates[0]
    return None

def placemark_polygon_coordinates(placemark: ET.Element) -> list[tuple[float, float]]:
    for ring in placemark.iter():
        if local_name(ring.tag) != "LinearRing":
            continue
        coordinate_element = direct_child_by_name(ring, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if len(coordinates) >= 4:
            return coordinates
    return []

def linestrings_to_closed_polygon(linestrings: Sequence[Sequence[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Best-effort chain of separate KML LineString placemarks into one closed polygon."""
    segments = [list(line) for line in linestrings if len(line) >= 2]
    if not segments:
        return []
    chain = segments.pop(0)
    tolerance_sq = 1e-16

    def close(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return point_distance_sq(a, b) <= tolerance_sq

    while segments:
        start = chain[0]
        end = chain[-1]
        best_index = None
        best_reverse = False
        best_prepend = False
        best_distance = float("inf")
        for idx, segment in enumerate(segments):
            candidates = [
                (point_distance_sq(end, segment[0]), False, False),
                (point_distance_sq(end, segment[-1]), True, False),
                (point_distance_sq(start, segment[-1]), False, True),
                (point_distance_sq(start, segment[0]), True, True),
            ]
            distance, reverse, prepend = min(candidates, key=lambda item: item[0])
            if distance < best_distance:
                best_distance = distance
                best_index = idx
                best_reverse = reverse
                best_prepend = prepend
        if best_index is None:
            break
        segment = segments.pop(best_index)
        if best_reverse:
            segment = list(reversed(segment))
        if best_prepend:
            chain = segment[:-1] + chain if close(segment[-1], chain[0]) else segment + chain
        else:
            chain = chain + segment[1:] if close(chain[-1], segment[0]) else chain + segment

    if len(chain) < 3:
        return []
    if not close(chain[0], chain[-1]):
        chain.append(chain[0])
    if len(chain) < 4:
        return []
    return chain

def point_distance_sq(first: tuple[float, float], second: tuple[float, float]) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2

def point_segment_distance_sq(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator <= 0:
        return point_distance_sq(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator))
    projected = (start[0] + t * dx, start[1] + t * dy)
    return point_distance_sq(point, projected)

def point_linestring_distance_sq(point: tuple[float, float], coordinates: Sequence[tuple[float, float]]) -> float:
    if not coordinates:
        return float("inf")
    if len(coordinates) == 1:
        return point_distance_sq(point, coordinates[0])
    return min(point_segment_distance_sq(point, start, end) for start, end in zip(coordinates, coordinates[1:]))

def point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    previous_x, previous_y = polygon[-1]
    for current_x, current_y in polygon:
        crosses = (current_y > y) != (previous_y > y)
        if crosses:
            x_intersect = (previous_x - current_x) * (y - current_y) / ((previous_y - current_y) or 1e-12) + current_x
            if x < x_intersect:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Check if line segment p1-p2 crosses segment p3-p4 (proper intersection)."""
    d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
    d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
    cross = d1x * d2y - d1y * d2x
    if abs(cross) < 1e-12:
        return False
    t = ((p3[0] - p1[0]) * d2y - (p3[1] - p1[1]) * d2x) / cross
    u = ((p3[0] - p1[0]) * d1y - (p3[1] - p1[1]) * d1x) / cross
    return 0 < t < 1 and 0 < u < 1


def linestring_intersects_polygon(
    linestring: Sequence[tuple[float, float]],
    polygon: Sequence[tuple[float, float]],
) -> bool:
    """Check if a linestring crosses through or touches a polygon."""
    if not linestring or len(polygon) < 3:
        return False
    for point in linestring:
        if point_in_polygon(point, polygon):
            return True
    n = len(polygon)
    for i in range(n):
        p3 = polygon[i]
        p4 = polygon[(i + 1) % n]
        for j in range(len(linestring) - 1):
            if _segments_cross(linestring[j], linestring[j + 1], p3, p4):
                return True
    return False


def cable_intersects_polygon(
    cable_coords: Sequence[Sequence[tuple[float, float]]],
    polygon: Sequence[tuple[float, float]],
) -> bool:
    """Check if any segment of a cable's linestrings intersects the polygon."""
    for linestring in cable_coords:
        if linestring_intersects_polygon(linestring, polygon):
            return True
    return False

def point_polygon_distance_sq(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> float:
    if not polygon:
        return float("inf")
    if point_in_polygon(point, polygon):
        return 0.0
    if len(polygon) == 1:
        return point_distance_sq(point, polygon[0])
    edges = zip(polygon, [*polygon[1:], polygon[0]])
    return min(point_segment_distance_sq(point, start, end) for start, end in edges)

def polygon_centroid(polygon: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    if not polygon:
        return None
    return (
        sum(point[0] for point in polygon) / len(polygon),
        sum(point[1] for point in polygon) / len(polygon),
    )

def format_distribution_description(route_meter: int, fat_slack_count: int, total_meter: int) -> str:
    slack_count = 1 + max(0, int(fat_slack_count))
    return "\n".join(
        [
            f"Total Route : {route_meter} m",
            f"Total Slack : {slack_count} unit (1 slack FDT & {fat_slack_count} slack FAT) @20 m",
            "Toleransi : 5%",
            f"Total Cable Lenght : {total_meter}m",
        ]
    )


def format_feeder_distribution_description(route_meter: int, slack_count: int, total_meter: int) -> str:
    """Format CABLE description for Feeder scope (simpler format, no FDT/FAT breakdown)."""
    return "\n".join(
        [
            f"Total Route : {route_meter} m",
            f"Total Slack : {slack_count} unit @20 m",
            "Toleransi : 5%",
            f"Total Cable Lenght : {total_meter}m",
        ]
    )


def candidate_project_names(root: ET.Element, source_path: str | Path) -> list[str]:
    names: list[str] = []
    document = next((element for element in root.iter() if local_name(element.tag) == "Document"), None)
    if document is not None:
        document_name = direct_child_text_by_name(document, "name")
        if document_name:
            names.append(document_name)
        for child in list(document):
            if local_name(child.tag) == "Folder":
                folder_name = direct_child_text_by_name(child, "name")
                if folder_name:
                    names.append(folder_name)
                    break
    names.append(extract_cluster_name_from_kmz_filename(source_path))
    names.append(Path(source_path).stem)
    return [name for index, name in enumerate(names) if name and name not in names[:index]]

def normalize_project_folder_name(value: str | None) -> str:
    name = normalize_text(value)
    name = re.sub(r"^[0-9a-f]{32}_\d+_", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[_\s]+", " ", name).strip()
    name = re.sub(r"\s*-\s*", " - ", name)
    return normalize_text(name).upper()

def source_project_folder_name(source_path: str | Path) -> str:
    name = normalize_project_folder_name(Path(source_path).stem)
    as_plan_match = re.search(
        r"\b([A-Z]{2,}\d{4,})\b\s+\d{10,14}\s+\1\s+(.+)$",
        name,
        re.IGNORECASE,
    )
    if as_plan_match:
        return f"{as_plan_match.group(1).upper()} - {normalize_text(as_plan_match.group(2)).upper()}"

    direct_match = re.match(r"^([A-Z]{2,}\d{4,})\s*-\s*(.+)$", name, re.IGNORECASE)
    if direct_match:
        return f"{direct_match.group(1).upper()} - {normalize_text(direct_match.group(2)).upper()}"

    loose_match = re.search(r"\b([A-Z]{2,}\d{4,})\b\s+(.+)$", name, re.IGNORECASE)
    if loose_match:
        return f"{loose_match.group(1).upper()} - {normalize_text(loose_match.group(2)).upper()}"
    return name

def is_project_folder_candidate(name: str | None) -> bool:
    normalized = normalize_project_folder_name(name)
    if not normalized:
        return False
    if WRAPPER_FOLDER_PATTERN.match(normalized):
        return False
    if canonical_standard_folder(normalized) or is_line_folder_name(normalized):
        return False
    if re.fullmatch(r"FDT\s*[-_ ]*\d+", normalized, re.IGNORECASE):
        return False
    return True

def preferred_project_folder_name(project_names: Sequence[str], source_path: str | Path) -> str:
    source_name = source_project_folder_name(source_path)
    candidates = [source_name, *project_names]
    for candidate in candidates:
        normalized = normalize_project_folder_name(candidate)
        if is_project_folder_candidate(normalized) and PROJECT_CODE_PATTERN.search(normalized):
            return normalized
    for candidate in candidates:
        normalized = normalize_project_folder_name(candidate)
        if is_project_folder_candidate(normalized):
            return normalized
    return source_name or "PROJECT"


def read_kmz_root_and_kml_name(path: str | Path) -> tuple[ET.Element, str]:
    path = Path(path)
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError(f"KMZ tidak berisi file KML: {path.name}")
            preferred_name = next((name for name in kml_names if Path(name).name.lower() == "doc.kml"), kml_names[0])
            with archive.open(preferred_name) as file:
                return parse_kml_xml(file.read(), f"{path.name}/{preferred_name}"), preferred_name
    return parse_kml_xml(path.read_bytes(), path.name), "doc.kml"

def folder_has_standard_descendant(folder: ET.Element) -> bool:
    for descendant in folder.iter():
        if local_name(descendant.tag) != "Folder":
            continue
        folder_name = normalize_text(direct_child_text_by_name(descendant, "name"))
        if canonical_standard_folder(folder_name) or is_line_folder_name(folder_name):
            return True
    return False

def unwrap_wrapper_folders(element: ET.Element, changes: list[dict]) -> None:
    index = 0
    while index < len(list(element)):
        child = list(element)[index]
        if local_name(child.tag) not in {"Document", "Folder"}:
            index += 1
            continue

        unwrap_wrapper_folders(child, changes)
        if local_name(child.tag) != "Folder":
            index += 1
            continue

        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        if not WRAPPER_FOLDER_PATTERN.match(folder_name) or not folder_has_standard_descendant(child):
            index += 1
            continue

        movable_children = [
            grandchild
            for grandchild in list(child)
            if local_name(grandchild.tag) not in {"name", "visibility", "open", "description"}
        ]
        element.remove(child)
        for offset, grandchild in enumerate(movable_children):
            element.insert(index + offset, grandchild)
        changes.append({"from": folder_name, "to": "unwrapped", "children_moved": len(movable_children)})
        index += len(movable_children)


def is_boundary_cluster_folder_name(folder_name: str | None) -> bool:
    return normalize_text(folder_name).upper() in BOUNDARY_CLUSTER_FOLDER_ALIASES


def direct_child_folder_by_name(parent: ET.Element, folder_name: str) -> ET.Element | None:
    target = normalize_text(folder_name).upper()
    for child in list(parent):
        if local_name(child.tag) != "Folder":
            continue
        if normalize_text(direct_child_text_by_name(child, "name")).upper() == target:
            return child
    return None

def ensure_folder(parent: ET.Element, namespace: str, folder_name: str) -> ET.Element:
    existing = direct_child_folder_by_name(parent, folder_name)
    if existing is not None:
        return existing
    folder = ET.Element(kml_tag(namespace, "Folder"))
    add_text_child(folder, namespace, "name", folder_name)
    parent.append(folder)
    return folder

def find_project_container(root: ET.Element, namespace: str, project_folder_name: str) -> ET.Element:
    document = document_element(root)
    project_folder_name = normalize_project_folder_name(project_folder_name) or "PROJECT"
    project_folder = direct_child_folder_by_name(document, project_folder_name)
    if project_folder is not None:
        return project_folder

    wrapper = next(
        (
            child
            for child in list(document)
            if local_name(child.tag) == "Folder"
            and WRAPPER_FOLDER_PATTERN.match(normalize_text(direct_child_text_by_name(child, "name")))
        ),
        None,
    )
    if wrapper is not None:
        ensure_direct_child(wrapper, "name", namespace).text = project_folder_name
        return wrapper
    return ensure_folder(document, namespace, project_folder_name)

def folder_fdt_order(folder_name: str, default: int = 1) -> int:
    return extract_fdt_order(folder_name) or default

def ensure_line_folder(project_folder: ET.Element, namespace: str, line_order: str, fdt_order: int) -> ET.Element:
    line_order = (line_order or "A").upper()
    fdt_order = max(1, int(fdt_order or 1))
    expected_name = format_line_folder_name(line_order, fdt_order)
    for child in list(project_folder):
        if local_name(child.tag) != "Folder":
            continue
        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        if folder_name == expected_name:
            return child
        # Check for equivalent line folder
        m = re.fullmatch(r"LINE\s+([A-Z])(?:\s*-\s*FDT\s+(\d+))?", folder_name.upper())
        if m:
            if m.group(1) == line_order and int(m.group(2) or 1) == fdt_order:
                return child
    return ensure_folder(project_folder, namespace, expected_name)

def ensure_standard_target_folder(project_folder: ET.Element, namespace: str, path: Sequence[str]) -> ET.Element:
    current = project_folder
    for folder_name in path:
        current = ensure_folder(current, namespace, folder_name)
    return current

def rebuild_raw_line_wrapper_folders(parent: ET.Element, namespace: str, changes: list[dict]) -> int:
    """Move legacy wrappers like 'FAT LINE A&B' into freshly-created standard
    LINE folders instead of preserving/renaming the original wrapper."""
    rebuilt = 0
    for child in list(parent):
        if local_name(child.tag) != "Folder":
            continue
        rebuilt += rebuild_raw_line_wrapper_folders(child, namespace, changes)
        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        if not folder_name or is_line_folder_name(folder_name):
            continue
        line_match = LINE_ORDER_PATTERN.search(folder_name)
        if not line_match:
            continue
        line_order = line_match.group(1).upper()
        fdt_order = folder_fdt_order(folder_name) or 1
        target_folder = ensure_line_folder(parent, namespace, line_order, fdt_order)
        moved = merge_folder_contents(child, target_folder, namespace)
        parent.remove(child)
        rebuilt += 1
        changes.append({
            "from": folder_name,
            "to": format_line_folder_name(line_order, fdt_order),
            "children_moved": moved,
            "reason": "raw line wrapper rebuilt into standard folder",
        })
    return rebuilt

def merge_folder_contents(source_folder: ET.Element, target_folder: ET.Element, namespace: str) -> int:
    moved = 0
    for child in list(source_folder):
        child_name = local_name(child.tag)
        if child_name in FOLDER_METADATA_CHILDREN:
            continue

        source_folder.remove(child)
        moved += 1
        if child_name != "Folder":
            target_folder.append(child)
            continue

        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        canonical = canonical_standard_folder(folder_name)

        if is_project_folder_candidate(folder_name) and folder_has_standard_descendant(child):
            moved += merge_folder_contents(child, target_folder, namespace)
            continue

        if folder_name.upper().startswith("LINE") and LINE_ORDER_PATTERN.search(folder_name):
            line_order = explicit_line_order(folder_name) or "A"
            line_folder = ensure_line_folder(target_folder, namespace, line_order, folder_fdt_order(folder_name))
            moved += merge_folder_contents(child, line_folder, namespace)
            continue

        if canonical:
            ensure_direct_child(child, "name", namespace).text = canonical
            standard_folder = ensure_folder(target_folder, namespace, canonical)
            moved += merge_folder_contents(child, standard_folder, namespace)
            continue

        existing = direct_child_folder_by_name(target_folder, folder_name)
        if existing is not None:
            moved += merge_folder_contents(child, existing, namespace)
            continue

        target_folder.append(child)
    return moved

def should_consolidate_document_folder(folder: ET.Element, project_folder_name: str) -> bool:
    folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
    if is_boundary_cluster_folder_name(folder_name):
        return True
    if canonical_standard_folder(folder_name) or (folder_name.upper().startswith("LINE") and LINE_ORDER_PATTERN.search(folder_name)):
        return True
    normalized_name = normalize_project_folder_name(folder_name)
    normalized_project = normalize_project_folder_name(project_folder_name)
    if WRAPPER_FOLDER_PATTERN.match(normalized_name):
        return folder_has_standard_descendant(folder)
    if normalized_name == normalized_project:
        return True
    return is_project_folder_candidate(normalized_name) and folder_has_standard_descendant(folder)

def consolidate_document_project_folders(
    root: ET.Element,
    namespace: str,
    project_folder: ET.Element,
    project_folder_name: str,
    changes: list[dict],
) -> None:
    document = document_element(root)
    for child in list(document):
        if child is project_folder or local_name(child.tag) != "Folder":
            continue
        if not should_consolidate_document_folder(child, project_folder_name):
            continue

        source_name = normalize_text(direct_child_text_by_name(child, "name"))
        if is_boundary_cluster_folder_name(source_name):
            existing = direct_child_folder_by_name(project_folder, source_name)
            document.remove(child)
            if existing is not None:
                moved = merge_folder_contents(child, existing, namespace)
            else:
                project_folder.append(child)
                moved = 1
            changes.append({"name": source_name, "from": "Document", "to": project_folder_name, "children_moved": moved})
            continue
        moved = merge_folder_contents(child, project_folder, namespace)
        document.remove(child)
        changes.append({"name": source_name, "from": "Document", "to": project_folder_name, "children_moved": moved})

def prune_empty_folders(
    element: ET.Element,
    keep_ids: set[int] | None = None,
    preserved_standard_folders: set[str] | None = None,
    folder_stack: list[str] | None = None,
    aggressive: bool = False,
) -> None:
    keep_ids = keep_ids or set()
    folder_stack = folder_stack or []
    for child in list(element):
        folder_name = normalize_text(direct_child_text_by_name(child, "name")) if local_name(child.tag) == "Folder" else ""
        if local_name(child.tag) in {"Document", "Folder"}:
            next_stack = folder_stack + ([folder_name] if local_name(child.tag) == "Folder" else [])
            prune_empty_folders(child, keep_ids, preserved_standard_folders, next_stack, aggressive)
        if local_name(child.tag) != "Folder" or id(child) in keep_ids:
            continue
        # Check if folder is empty (only metadata children, no placemarks or non-empty subfolders)
        if not all(local_name(grandchild.tag) in FOLDER_METADATA_CHILDREN for grandchild in list(child)):
            continue
        canonical = canonical_standard_folder(folder_name)
        # A source TALI PANCING folder is an explicit opt-out from generating
        # clones, including an intentionally empty folder.
        if canonical == "TALI PANCING":
            continue
        is_line_folder = is_line_folder_name(folder_name)
        is_fdt_group = bool(re.fullmatch(r"FDT\s*[-_ ]*\d+", folder_name, re.IGNORECASE))
        if aggressive:
            # Aggressive mode: hapus folder input lama yang sudah kosong setelah reorganisasi.
            # Preserve standard folders yang merupakan child langsung dari LINE standar baru
            # (LINE folders yang direct child dari project folder).
            # Old input pattern: FDT N > LINE A > standard folders
            # New output pattern: LINE A - FDT N > standard folders (atau LINE A > standard folders jika 1 FDT)
            is_inside_fdt_group = any(
                re.fullmatch(r"FDT\s*[-_ ]*\d+", parent_name or "", re.IGNORECASE)
                for parent_name in folder_stack
            )
            if is_inside_fdt_group:
                # Folder di dalam FDT group lama ??? hapus
                element.remove(child)
            elif is_line_folder or is_fdt_group:
                # LINE folder atau FDT group folder yang kosong ??? hapus
                element.remove(child)
            elif not canonical:
                # Non-standard folder kosong ??? hapus
                element.remove(child)
            # else: standard folder di luar FDT group (di dalam LINE standar baru) ??? preserve
        else:
            # Normal mode: preserve LINE folders, FDT groups, dan standard folders di dalam LINE parent
            if is_line_folder:
                continue
            has_line_parent = any(is_line_folder_name(parent_name) for parent_name in folder_stack)
            if (
                canonical
                and (preserved_standard_folders is None or canonical in preserved_standard_folders)
                and (canonical == "FDT" or has_line_parent)
            ):
                continue
            element.remove(child)

def ensure_line_standard_folders(project_folder: ET.Element, namespace: str, network_type: str | None = "AE") -> int:
    created = 0
    standard_folders = [
        folder_name
        for folder_name in line_standard_folders_for_network(network_type)
        if folder_name not in AUTO_LINE_STANDARD_EXCLUDE_FOLDERS
    ]
    for child in list(project_folder):
        if local_name(child.tag) != "Folder":
            continue
        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        if not is_line_folder_name(folder_name):
            continue
        for standard_folder_name in standard_folders:
            before_count = len(list(child))
            ensure_folder(child, namespace, standard_folder_name)
            if len(list(child)) > before_count:
                created += 1
    return created

def is_untitled_hp_placemark_name(raw_name: str | None) -> bool:
    normalized = normalize_text(raw_name).upper()
    return not normalized or normalized in {
        "UNTITLED",
        "UNTITLED PLACEMARK",
        "NN-UNTITLED",
        "NN-UNTITLED PLACEMARK",
    }


def _normalize_hp_cover_placemark_name(raw_name: str) -> str:
    name = normalize_text(raw_name).upper()
    if not name or is_untitled_hp_placemark_name(name):
        return name
    # Block + house number is numbered, even if an older run added NN-.
    numbered_block = name.removeprefix("NN-")
    if re.fullmatch(r"(?!TK-|RK-)[A-Z]{1,2}-\d{1,4}/\d{1,4}[A-Z]?", numbered_block):
        return numbered_block
    # Existing NN- labels are otherwise authoritative; never reclassify them.
    if name.startswith("NN-"):
        return name
    stripped = name.strip()

    # TK/RK unit labels get NN- when the source does not already have it.
    if re.fullmatch(r"(?:TK|RK)[\s/–\-.]*\d+[A-Z]?", stripped):
        return f"NN-{stripped}"

    # Infrastructure / block / lot / unit patterns: never use NN-
    is_block_code = (
        stripped.startswith(("MR.", "EXT.", "FDT", "FAT", "XXX-", "BLK", "BLOCK", "SLACK", "HANGER", "KAV", "KAVLING", "LOT"))
        or "SLACK" in stripped
        or "HANGER" in stripped
        or bool(re.fullmatch(r"\d+([A-Z]|/\d+|\-\d+)?", stripped))
        or bool(re.fullmatch(r"[A-Z]{1,4}\d+[A-Z]?", stripped))
        or bool(re.fullmatch(r"[A-Z]{1,4}[\s/–\-\.]+\d+[A-Z]?", stripped))
        or bool(re.fullmatch(r"[A-Z]{1,4}\d+[\s/–\-\.]+[A-Z0-9/–\-\.]+", stripped))
        or bool(re.fullmatch(r"[A-Z]{1,4}\d+\.\d+", stripped))
        or bool(re.fullmatch(r"(BLOK|BLOCK|KAV|KAVLING|LOT)[\s/–\-\.]+[A-Z0-9/–\-\.]+", stripped))
        or bool(re.fullmatch(r"[A-Z]{1,2}", stripped))
    )

    if is_block_code:
        return stripped

    # Genuine place / customer names: retain or add NN- prefix
    if re.search(r"[A-Z]", stripped):
        return f"NN-{stripped}"
    return stripped


def normalize_hp_cover_child_folders(hp_cover_folder: ET.Element, namespace: str, changes: list[dict]) -> None:
    def _normalize_placemark_names_in(element: ET.Element) -> None:
        for pm in list(element.iter(f"{{{namespace}}}Placemark")):
            name_el = direct_child_by_name(pm, "name")
            if name_el is None or name_el.text is None:
                continue
            normalized = _normalize_hp_cover_placemark_name(name_el.text)
            if name_el.text != normalized:
                name_el.text = normalized

    by_name: dict[str, ET.Element] = {}
    for child in list(hp_cover_folder):
        if local_name(child.tag) != "Folder":
            continue
        old_name = normalize_text(direct_child_text_by_name(child, "name"))
        new_name = hp_cover_fat_folder_name(old_name)
        if old_name != new_name:
            ensure_direct_child(child, "name", namespace).text = new_name
            changes.append({"from": old_name, "to": new_name, "folder": "HP COVER"})

        key = new_name.upper()
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = child
            continue

        moved = merge_folder_contents(child, existing, namespace)
        hp_cover_folder.remove(child)
        changes.append({"from": new_name, "to": key, "folder": "HP COVER", "children_moved": moved})

    children = list(hp_cover_folder)
    metadata_children = [child for child in children if local_name(child.tag) in FOLDER_METADATA_CHILDREN]
    folder_children = [child for child in children if local_name(child.tag) == "Folder"]
    other_children = [
        child
        for child in children
        if local_name(child.tag) not in FOLDER_METADATA_CHILDREN and local_name(child.tag) != "Folder"
    ]
    folder_children.sort(key=lambda child: normalize_text(direct_child_text_by_name(child, "name")).upper())
    hp_cover_folder[:] = metadata_children + folder_children + other_children
    _normalize_placemark_names_in(hp_cover_folder)


def normalize_untitled_hp_placemark_names(
    element: ET.Element,
    namespace: str,
    changes: list[dict],
) -> int:
    """Assign one collision-free NN-number sequence across HP COVER/UNCOVER."""
    records: list[tuple[ET.Element, ET.Element | None, str, str, str]] = []

    def walk(current: ET.Element, hp_scope: str | None = None) -> None:
        for child in list(current):
            child_kind = local_name(child.tag)
            if child_kind in {"Document", "Folder"}:
                next_scope = hp_scope
                if child_kind == "Folder":
                    folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                    canonical = canonical_standard_folder(folder_name)
                    if canonical in {"HP COVER", "HP UNCOVER"}:
                        next_scope = canonical
                walk(child, next_scope)
                continue
            if child_kind != "Placemark" or hp_scope not in {"HP COVER", "HP UNCOVER"}:
                continue

            name_element = direct_child_by_name(child, "name")
            raw_name = name_element.text if name_element is not None else ""
            records.append((child, name_element, hp_scope, raw_name or "", normalize_text(raw_name).upper()))

    walk(element)
    pending: list[tuple[ET.Element, ET.Element | None, str, str]] = []
    reserved_numbers: set[int] = set()
    for placemark, name_element, hp_scope, raw_name, normalized in records:
        if is_untitled_hp_placemark_name(normalized):
            pending.append((placemark, name_element, hp_scope, raw_name))
            continue
        numeric_match = re.fullmatch(r"NN-\s*0*(\d+)", normalized)
        if not numeric_match:
            continue
        number = int(numeric_match.group(1))
        if number in reserved_numbers:
            pending.append((placemark, name_element, hp_scope, raw_name))
        else:
            reserved_numbers.add(number)

    next_number = 1
    for placemark, name_element, hp_scope, old_name in pending:
        while next_number in reserved_numbers:
            next_number += 1
        candidate = f"NN-{next_number}"
        if name_element is None:
            name_element = ET.SubElement(placemark, f"{{{namespace}}}name")
        name_element.text = candidate
        reserved_numbers.add(next_number)
        changes.append({"from": old_name, "to": candidate, "folder": hp_scope})
        next_number += 1
    return len(pending)


def number_duplicate_hp_place_names(element: ET.Element, namespace: str, changes: list[dict]) -> int:
    """Number only repeated place labels across both HP folders; keep source geometry."""
    places: list[tuple[ET.Element, str, str, str]] = []

    def walk(current: ET.Element, hp_scope: str | None = None) -> None:
        for child in list(current):
            kind = local_name(child.tag)
            if kind in {"Document", "Folder"}:
                scope = hp_scope
                if kind == "Folder":
                    folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                    canonical = canonical_standard_folder(folder_name)
                    if canonical in {"HP COVER", "HP UNCOVER"}:
                        scope = canonical
                walk(child, scope)
            elif kind == "Placemark" and hp_scope in {"HP COVER", "HP UNCOVER"}:
                name_el = direct_child_by_name(child, "name")
                if name_el is None or is_untitled_hp_placemark_name(name_el.text):
                    continue
                raw = name_el.text or ""
                name = _normalize_hp_cover_placemark_name(raw)
                if raw != name and re.fullmatch(r"(?!TK-|RK-)[A-Z]{1,2}-\d{1,4}/\d{1,4}[A-Z]?", name):
                    name_el.text = name  # Also correct HP UNCOVER after final routing.
                    changes.append({"from": raw, "to": name, "folder": hp_scope})
                suffix = name[3:] if name.startswith("NN-") else ""
                # NN-n, existing block/lot codes and TK/RK are not place names.
                if (not suffix or re.fullmatch(r"(?:TK|RK)[\s/–\-.]*\d+[A-Z]?", suffix)
                    or _normalize_hp_cover_placemark_name(suffix) != name):
                    continue
                places.append((name_el, name, hp_scope, raw))

    walk(element)
    counts = Counter(name for _, name, _, _ in places)
    used_names = set(counts)
    seen: set[str] = set()
    renamed = 0
    for name_el, name, scope, raw in places:
        if counts[name] < 2:
            continue  # A unique existing label is authoritative.
        parent, separator, last = name.rpartition("-")
        numbered = bool(separator and last.isdigit())
        if numbered:
            if name not in seen:
                seen.add(name)  # Preserve the first already-numbered placemark.
                continue
            base, number = parent, int(last) + 1
        else:
            base, number = name, 1  # All unsuffixed duplicates get -1, -2, etc.
        while f"{base}-{number}" in used_names:
            number += 1
        new_name = f"{base}-{number}"
        name_el.text = new_name
        changes.append({"from": raw, "to": new_name, "folder": scope})
        used_names.add(new_name)
        renamed += 1
    return renamed


def normalize_hp_cover_folders(
    element: ET.Element,
    namespace: str,
    changes: list[dict],
) -> None:
    def normalize_cover_structure(current: ET.Element) -> None:
        for child in list(current):
            if local_name(child.tag) != "Folder":
                continue
            folder_name = normalize_text(direct_child_text_by_name(child, "name"))
            if canonical_standard_folder(folder_name) == "HP COVER":
                normalize_hp_cover_child_folders(child, namespace, changes)
            normalize_cover_structure(child)

    normalize_cover_structure(element)
    normalize_untitled_hp_placemark_names(element, namespace, changes)

HP_COVER_MAX_PER_FAT = 16
FAT_AREA_INFER_MAX_HP = 20
HP_COVER_OVERFLOW_REASSIGN_RADIUS_METER = 5.0
HP_COVER_UNCOVER_PREFERRED_PATTERN = re.compile(
    r"\b(?:TK|PAUD|FASUM|FASILITAS\s+UMUM|SEKOLAH|SD|SMP|SMA|SMK|MASJID|MUSHOLLA|MUSALA|GEREJA|KANTOR|POS|BALAI|RUKO|TOKO)\b",
    re.IGNORECASE,
)

def looks_like_uncover_preferred_hp(name: str | None) -> bool:
    return bool(HP_COVER_UNCOVER_PREFERRED_PATTERN.search(normalize_text(name)))

def point_polygon_distance_meter(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> float:
    if not polygon:
        return float("inf")
    if point_in_polygon(point, polygon):
        return 0.0
    if len(polygon) == 1:
        return _haversine_distance_meter(point, polygon[0])
    edges = zip(polygon, [*polygon[1:], polygon[0]])
    return min(_point_to_segment_distance_meter(point, start, end) for start, end in edges)

def point_linestring_progress_meter(point: tuple[float, float], coordinates: Sequence[tuple[float, float]]) -> float:
    if not coordinates:
        return 0.0
    if len(coordinates) == 1:
        return 0.0
    best_progress = 0.0
    best_distance = float("inf")
    progress = 0.0
    for start, end in zip(coordinates, coordinates[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        denominator = dx * dx + dy * dy
        segment_length = _haversine_distance_meter(start, end)
        if denominator <= 0:
            distance = _haversine_distance_meter(point, start)
            segment_progress = 0.0
        else:
            t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator))
            projected = (start[0] + t * dx, start[1] + t * dy)
            distance = _haversine_distance_meter(point, projected)
            segment_progress = segment_length * t
        if distance < best_distance:
            best_distance = distance
            best_progress = progress + segment_progress
        progress += segment_length
    return best_progress

def polygon_cable_progress_meter(polygon: Sequence[tuple[float, float]], cable_refs: Sequence[OrganizerCableRef]) -> float:
    center = polygon_centroid(polygon)
    if center is None or not cable_refs:
        return 0.0
    best_progress = 0.0
    best_distance = float("inf")
    for ref in cable_refs:
        for line in ref.coordinates:
            distance = point_linestring_distance_sq(center, line)
            if distance < best_distance:
                best_distance = distance
                best_progress = point_linestring_progress_meter(center, line)
    return best_progress

def iter_direct_placemarks(folder: ET.Element) -> list[ET.Element]:
    return [child for child in list(folder) if local_name(child.tag) == "Placemark"]

def fat_area_matches_folder(area: OrganizerFatAreaRef, line_folder_name: str, fat_folder_name: str) -> bool:
    fat_code = hp_cover_fat_folder_name(fat_folder_name).upper()
    area_code = hp_cover_fat_folder_name(area.fat_code or area.name).upper()
    return area_code == fat_code



def _convert_fat_area_lines_to_polygons(
    project_folder: ET.Element,
    namespace: str,
    fat_areas,
    cable_refs,
    organizer_changes: list[dict],
    kml_root: ET.Element | None = None,
) -> int:
    """Post-organize greedy assignment of boundary lines to FAT points.

    Rules:
    - collect candidate boundary line placemarks from any existing BOUNDARY FAT/FAT AREA under project
    - for each final LINE X - FDT Y folder, collect FAT point placemarks
    - process FAT points from outermost first (farthest from line FAT centroid)
    - greedily assign nearest unused boundary line to each FAT point
    - convert assigned line -> polygon, rename to FAT code, move into line's BOUNDARY FAT
    """
    assigned_count = 0

    def _collect_line_folders(elem: ET.Element) -> list[ET.Element]:
        found = []
        def _walk(node: ET.Element):
            for child in list(node):
                if local_name(child.tag) == "Folder":
                    name = normalize_text(direct_child_text_by_name(child, "name") or "")
                    if LINE_ORDER_PATTERN.search(name):
                        found.append(child)
                    _walk(child)
                elif local_name(child.tag) in ("Document", "kml"):
                    _walk(child)
        _walk(elem)
        return found

    def _collect_fat_points(line_folder: ET.Element):
        pts = []
        fat_folder = direct_child_folder_by_name(line_folder, "FAT")
        if fat_folder is None:
            return pts
        for child in list(fat_folder):
            if local_name(child.tag) == "Placemark" and placemark_has_point(child):
                coord = placemark_point_coordinate(child)
                if coord:
                    name = normalize_text(direct_child_text_by_name(child, "name") or "")
                    pts.append((child, coord, name))
            elif local_name(child.tag) == "Folder":
                for sub in list(child):
                    if local_name(sub.tag) == "Placemark" and placemark_has_point(sub):
                        coord = placemark_point_coordinate(sub)
                        if coord:
                            name = normalize_text(direct_child_text_by_name(sub, "name") or "")
                            pts.append((sub, coord, name))
        return pts

    def _centroid(points):
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def _dist_sq(a, b):
        return (a[0]-b[0])**2 + (a[1]-b[1])**2

    def _placemark_parent(root: ET.Element, target: ET.Element):
        for parent in root.iter():
            for child in list(parent):
                if child is target:
                    return parent
        return None

    def _remove_from_parent(root: ET.Element, target: ET.Element):
        parent = _placemark_parent(root, target)
        if parent is not None:
            parent.remove(target)
            return True
        return False

    def _convert_to_polygon(pm: ET.Element):
        linestrings = placemark_linestrings(pm)
        if not linestrings:
            return False
        polygon = linestrings_to_closed_polygon(linestrings)
        if not polygon:
            return False
        for child in list(pm):
            if local_name(child.tag) in ("LineString", "Line", "Geometry", "MultiGeometry"):
                pm.remove(child)
        poly_elem = ET.SubElement(pm, f"{{{namespace}}}Polygon")
        outer = ET.SubElement(poly_elem, f"{{{namespace}}}outerBoundaryIs")
        ring = ET.SubElement(outer, f"{{{namespace}}}LinearRing")
        coords = ET.SubElement(ring, f"{{{namespace}}}coordinates")
        coords.text = " ".join(f"{lon},{lat},0" for lon, lat in polygon)
        return True

    def _candidate_center(pm: ET.Element):
        if placemark_has_line(pm):
            lines = placemark_linestrings(pm)
            polygon = linestrings_to_closed_polygon(lines)
            if polygon:
                return polygon_centroid(polygon)
        poly = placemark_polygon_coordinates(pm)
        if poly:
            return polygon_centroid(poly)
        return None

    # 1) collect candidate raw boundary placemarks from anywhere under line folders / global folders
    # Scope lock: candidate under LINE X - FDT Y may only be assigned to the same LINE X - FDT Y.
    candidates = []
    seen_ids = set()
    _search_root = kml_root if kml_root is not None else project_folder
    parent_by_id = {}
    for parent in _search_root.iter():
        for child in list(parent):
            parent_by_id[id(child)] = parent

    def _ancestor_line_name(node: ET.Element | None) -> str | None:
        cur = node
        while cur is not None:
            if local_name(cur.tag) == "Folder":
                nm = normalize_text(direct_child_text_by_name(cur, "name") or "")
                if LINE_ORDER_PATTERN.search(nm):
                    return nm
            cur = parent_by_id.get(id(cur))
        return None

    for folder in list(_search_root.iter()):
        if local_name(folder.tag) != "Folder":
            continue
        fname = normalize_text(direct_child_text_by_name(folder, "name") or "")
        if fname not in ("BOUNDARY FAT", "FAT AREA"):
            continue
        source_line_name = _ancestor_line_name(folder)
        for pm in list(folder):
            if local_name(pm.tag) != "Placemark":
                continue
            if not (placemark_has_line(pm) or placemark_polygon_coordinates(pm)):
                continue
            center = _candidate_center(pm)
            if center is None:
                continue
            pid = id(pm)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            candidates.append({"pm": pm, "center": center, "source_folder": folder, "source_line_name": source_line_name, "used": False})


    # 2) process each line folder: farthest FAT first
    for line_folder in _collect_line_folders(project_folder):
        line_name = normalize_text(direct_child_text_by_name(line_folder, "name") or "")
        fat_points = _collect_fat_points(line_folder)
        if not fat_points:
            continue
        line_centroid = _centroid([coord for _, coord, _ in fat_points])
        if line_centroid is None:
            continue

        fat_points_sorted = sorted(
            fat_points,
            key=lambda item: _dist_sq(item[1], line_centroid),
            reverse=True,
        )
        target_boundary = ensure_folder(line_folder, namespace, "BOUNDARY FAT")
        existing_names = {
            normalize_text(direct_child_text_by_name(pm, "name") or "")
            for pm in list(target_boundary)
            if local_name(pm.tag) == "Placemark"
        }

        for _fat_pm, fat_coord, fat_name in fat_points_sorted:
            fat_code = find_fat_code(fat_name) or fat_name
            if fat_code in existing_names:
                continue

            best = None
            best_d = float("inf")
            # Preserve a source boundary that already carries this FAT code.
            # Greedy nearest-neighbour assignment is only safe for unnamed
            # boundaries; otherwise adjacent FAT polygons can rotate between
            # A02/A03/A04 and corrupt downstream HPDB parent FAT values.
            preferred_named = []
            for cand in candidates:
                if cand["used"]:
                    continue
                cand_line = cand.get("source_line_name")
                if cand_line and cand_line != line_name:
                    continue
                cand_name = normalize_text(direct_child_text_by_name(cand["pm"], "name") or "")
                if (find_fat_code(cand_name) or "").upper() == str(fat_code).upper():
                    preferred_named.append(cand)
            search_candidates = preferred_named or candidates
            for cand in candidates:
                if search_candidates is not candidates and cand not in search_candidates:
                    continue
                if cand["used"]:
                    continue
                cand_line = cand.get("source_line_name")
                if cand_line and cand_line != line_name:
                    continue
                if not preferred_named:
                    cand_name = normalize_text(direct_child_text_by_name(cand["pm"], "name") or "")
                    if find_fat_code(cand_name):
                        continue
                d = _dist_sq(fat_coord, cand["center"])
                if d < best_d:
                    best_d = d
                    best = cand
            if best is None:
                continue

            pm = best["pm"]
            if placemark_has_line(pm) and not placemark_polygon_coordinates(pm):
                _convert_to_polygon(pm)
            name_elem = direct_child_by_name(pm, "name")
            old_name = normalize_text(direct_child_text_by_name(pm, "name") or "") or "line"
            if name_elem is not None:
                name_elem.text = fat_code
            _remove_from_parent(_search_root, pm)
            target_boundary.append(pm)
            existing_names.add(fat_code)
            best["used"] = True
            assigned_count += 1
            organizer_changes.append({
                "type": "boundary_fat_greedy_assign",
                "from": old_name,
                "to": " / ".join([line_name, "BOUNDARY FAT", fat_code]),
                "reason": "Greedy assign nearest unused boundary line to farthest FAT point in line",
            })

    return assigned_count


SLACK_HANGER_FAT_DEDUPE_RADIUS_METER = 1.0


def create_missing_slack_hanger_from_fat(
    root: ET.Element,
    namespace: str,
    changes: list[dict],
    *,
    per_fat_dedupe: bool = False,
) -> int:
    """Clone FAT points into Slack Hanger.

    ``per_fat_dedupe=True`` covers each FAT independently within its physical
    LINE/FDT. The default preserves the legacy folder-empty behavior.

    Supports both:
    - LINE/FAT/<Placemark>
    - LINE/FAT/A01/<Placemark>
    """
    import copy

    created = 0

    def _fat_point_placemarks(fat_folder: ET.Element) -> list[ET.Element]:
        placemarks: list[ET.Element] = []
        for child in list(fat_folder):
            if local_name(child.tag) == "Placemark":
                if placemark_has_point(child):
                    placemarks.append(child)
            elif local_name(child.tag) == "Folder":
                for sub in list(child):
                    if local_name(sub.tag) == "Placemark":
                        if placemark_has_point(sub):
                            placemarks.append(sub)
        return placemarks

    def _slack_point_coordinates(slack_folder: ET.Element | None) -> list[tuple[float, float]]:
        if slack_folder is None:
            return []
        points: list[tuple[float, float]] = []
        for child in slack_folder.iter():
            if local_name(child.tag) != "Placemark" or not placemark_has_point(child):
                continue
            point = placemark_point_coordinate(child)
            if point is not None:
                points.append(point)
        return points

    def _walk(elem: ET.Element):
        nonlocal created
        for child in list(elem):
            child_name = normalize_text(direct_child_text_by_name(child, "name") or "")
            if local_name(child.tag) == "Folder":
                if LINE_ORDER_PATTERN.search(child_name):
                    fat_folder = direct_child_folder_by_name(child, "FAT")
                    if fat_folder is not None:
                        fat_placemarks = _fat_point_placemarks(fat_folder)
                        slack_folder = direct_child_folder_by_name(child, "SLACK HANGER")
                        existing_slack_points = _slack_point_coordinates(slack_folder)
                        legacy_folder_had_slack = bool(
                            slack_folder is not None
                            and any(
                                local_name(item.tag) == "Placemark" and placemark_has_point(item)
                                for item in list(slack_folder)
                            )
                        )
                        created_for_line = 0
                        for placemark in fat_placemarks:
                            fat_point = placemark_point_coordinate(placemark)
                            if fat_point is None:
                                continue
                            if not per_fat_dedupe and legacy_folder_had_slack:
                                continue
                            if per_fat_dedupe and any(
                                _haversine_distance_meter(fat_point, slack_point)
                                <= SLACK_HANGER_FAT_DEDUPE_RADIUS_METER
                                for slack_point in existing_slack_points
                            ):
                                continue
                            if slack_folder is None:
                                slack_folder = ensure_folder(child, namespace, "SLACK HANGER")
                            cloned = copy.deepcopy(placemark)
                            name_element = direct_child_by_name(cloned, "name")
                            if name_element is not None:
                                name_element.text = "SLACK HANGER"
                            slack_folder.append(cloned)
                            existing_slack_points.append(fat_point)
                            created += 1
                            created_for_line += 1
                        if created_for_line:
                            changes.append({
                                "type": "slack_hanger_from_fat",
                                "from": " / ".join([child_name, "FAT"]),
                                "to": " / ".join([child_name, "SLACK HANGER"]),
                                "count": created_for_line,
                                "reason": (
                                    "FAT point had no Slack Hanger within 1 m in the same LINE/FDT."
                                    if per_fat_dedupe
                                    else "SLACK HANGER folder missing or empty; cloned FAT point placemarks"
                                ),
                            })
                # ALWAYS recurse into Folder children, whether they matched or not
                _walk(child)
            elif local_name(child.tag) in ("Document", "kml"):
                # Also recurse into Document/kml elements
                _walk(child)

    _walk(root)
    return created

def ai_qc_standardize_result(
    root: ET.Element,
    namespace: str,
    input_path: str,
    output_path: str,
    original_kmz_path: str,
    organizer_changes: list[dict],
    *,
    ignore_sling_owned: bool = False,
) -> dict:
    """AI QC: verify conversion result against standard rules, revert if needed.

    Rules checked:
    1. Every LINE X - FDT Y must have: FAT, SLACK HANGER, (optional) BOUNDARY FAT
    2. BOUNDARY FAT must be Polygon, not LineString
    3. Every FAT point must have corresponding SLACK HANGER
    4. No empty LINE folders
    5. Naming must follow FAT code pattern

    Returns: {"pass": bool, "fixes_applied": int, "issues": list[str]}
    """
    import copy

    issues = []
    fixes_applied = 0

    # Find project folder
    project_folder = None
    for child in root:
        if local_name(child.tag) == "Folder":
            project_folder = child
            break
    if project_folder is None:
        return {"pass": True, "fixes_applied": 0, "issues": []}

    def _line_folders(elem, sling_owned: bool = False):
        found = []
        for child in list(elem):
            if local_name(child.tag) != "Folder":
                continue
            name = normalize_text(direct_child_text_by_name(child, "name") or "")
            child_sling_owned = sling_owned or is_sling_wire_folder_name(name)
            if ignore_sling_owned and child_sling_owned:
                continue
            if LINE_ORDER_PATTERN.search(name):
                found.append(child)
            found.extend(_line_folders(child, child_sling_owned))
        return found

    def _placemark_count(folder, geom_type=None):
        count = 0
        for child in list(folder):
            if local_name(child.tag) != "Placemark":
                continue
            if geom_type == "point" and not placemark_has_point(child):
                continue
            if geom_type == "polygon" and not placemark_polygon_coordinates(child):
                continue
            if geom_type == "line" and not placemark_has_line(child):
                continue
            count += 1
        return count

    for line_folder in _line_folders(project_folder):
        line_name = normalize_text(direct_child_text_by_name(line_folder, "name") or "")

        # Rule 1: Must have FAT folder with points
        fat_folder = direct_child_folder_by_name(line_folder, "FAT")
        if fat_folder is None:
            issues.append(f"{line_name}: missing FAT folder")
            continue
        fat_points = _placemark_count(fat_folder, "point")
        if fat_points == 0:
            issues.append(f"{line_name}: FAT folder empty")
            continue

        # Rule 2: Must have SLACK HANGER folder with points
        slack_folder = direct_child_folder_by_name(line_folder, "SLACK HANGER")
        if slack_folder is None:
            issues.append(f"{line_name}: missing SLACK HANGER folder")
            continue
        slack_points = _placemark_count(slack_folder, "point")
        if slack_points == 0:
            issues.append(f"{line_name}: SLACK HANGER empty")
            continue

        # Rule 3: SLACK HANGER count must match FAT count
        if slack_points != fat_points:
            issues.append(f"{line_name}: SLACK HANGER ({slack_points}) != FAT ({fat_points})")

        # Rule 4: BOUNDARY FAT must be Polygon, not LineString
        bf_folder = direct_child_folder_by_name(line_folder, "BOUNDARY FAT")
        if bf_folder is not None:
            for pm in list(bf_folder):
                if local_name(pm.tag) != "Placemark":
                    continue
                if placemark_has_line(pm) and not placemark_polygon_coordinates(pm):
                    issues.append(f"{line_name}: BOUNDARY FAT has LineString instead of Polygon")

        # Rule 5: No duplicate FAT codes in BOUNDARY FAT
        if bf_folder is not None:
            bf_names = []
            for pm in list(bf_folder):
                if local_name(pm.tag) != "Placemark":
                    continue
                pm_name = normalize_text(direct_child_text_by_name(pm, "name") or "")
                if pm_name in bf_names:
                    issues.append(f"{line_name}: duplicate BOUNDARY FAT name '{pm_name}'")
                bf_names.append(pm_name)

    # Auto-fix: if SLACK HANGER empty but FAT has points, re-run slack generation
    for issue in issues:
        if "SLACK HANGER empty" in issue:
            line_name = issue.split(":")[0]
            for line_folder in _line_folders(project_folder):
                if normalize_text(direct_child_text_by_name(line_folder, "name") or "") == line_name:
                    fat_folder = direct_child_folder_by_name(line_folder, "FAT")
                    slack_folder = direct_child_folder_by_name(line_folder, "SLACK HANGER")
                    if fat_folder is not None and slack_folder is not None:
                        existing_slack = _placemark_count(slack_folder, "point")
                        if existing_slack == 0:
                            fat_placemarks = []
                            for child in list(fat_folder):
                                if local_name(child.tag) == "Placemark" and placemark_has_point(child):
                                    fat_placemarks.append(child)
                                elif local_name(child.tag) == "Folder":
                                    for sub in list(child):
                                        if local_name(sub.tag) == "Placemark" and placemark_has_point(sub):
                                            fat_placemarks.append(sub)
                            for placemark in fat_placemarks:
                                cloned = copy.deepcopy(placemark)
                                name_element = direct_child_by_name(cloned, "name")
                                if name_element is not None:
                                    name_element.text = "SLACK HANGER"
                                slack_folder.append(cloned)
                                fixes_applied += 1
                    break

    return {
        "pass": len(issues) == 0 or fixes_applied > 0,
        "fixes_applied": fixes_applied,
        "issues": issues,
    }


def move_hp_cover_overflow_outside_boundary(
    project_folder: ET.Element,
    namespace: str,
    fat_areas: Sequence[OrganizerFatAreaRef],
    changes: list[dict],
) -> int:
    moved = 0
    for line_folder in list(project_folder):
        if local_name(line_folder.tag) != "Folder":
            continue
        line_folder_name = normalize_text(direct_child_text_by_name(line_folder, "name"))
        if not LINE_ORDER_PATTERN.search(line_folder_name):
            continue
        hp_cover_folder = direct_child_folder_by_name(line_folder, "HP COVER")
        if hp_cover_folder is None:
            continue
        hp_uncover_folder = ensure_folder(line_folder, namespace, "HP UNCOVER")
        hp_cover_counts: dict[str, int] = {}
        hp_cover_folders: dict[str, ET.Element] = {}
        for folder in list(hp_cover_folder):
            if local_name(folder.tag) != "Folder":
                continue
            folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
            key = hp_cover_fat_folder_name(folder_name).upper()
            hp_cover_counts[key] = len(iter_direct_placemarks(folder))
            hp_cover_folders[key] = folder
        for fat_folder in list(hp_cover_folder):
            if local_name(fat_folder.tag) != "Folder":
                continue
            fat_folder_name = normalize_text(direct_child_text_by_name(fat_folder, "name"))
            fat_folder_key = hp_cover_fat_folder_name(fat_folder_name).upper()
            placemarks = iter_direct_placemarks(fat_folder)
            if len(placemarks) <= HP_COVER_MAX_PER_FAT:
                continue
            matching_areas = [area for area in fat_areas if fat_area_matches_folder(area, line_folder_name, fat_folder_name)]
            if not matching_areas:
                continue
            for placemark in list(placemarks):
                if len(iter_direct_placemarks(fat_folder)) <= HP_COVER_MAX_PER_FAT:
                    break
                name = normalize_text(direct_child_text_by_name(placemark, "name"))
                if not looks_like_uncover_preferred_hp(name):
                    continue
                fat_folder.remove(placemark)
                hp_uncover_folder.append(placemark)
                hp_cover_counts[fat_folder_key] = max(0, hp_cover_counts.get(fat_folder_key, len(placemarks)) - 1)
                moved += 1
                changes.append({
                    "name": name,
                    "from": " / ".join([line_folder_name, "HP COVER", fat_folder_name]),
                    "to": " / ".join([line_folder_name, "HP UNCOVER"]),
                    "reason": f"HP COVER per FAT exceeds {HP_COVER_MAX_PER_FAT}; public/facility HP moved to uncover",
                })
            placemarks = iter_direct_placemarks(fat_folder)
            if len(placemarks) <= HP_COVER_MAX_PER_FAT:
                continue
            for placemark in list(placemarks):
                point = placemark_point_coordinate(placemark)
                if point is None or any(point_in_polygon(point, area.polygon) for area in matching_areas):
                    continue
                name = normalize_text(direct_child_text_by_name(placemark, "name"))
                fat_folder.remove(placemark)
                hp_cover_counts[fat_folder_key] = max(0, hp_cover_counts.get(fat_folder_key, len(placemarks)) - 1)
                target_area = next(
                    (
                        area for area in sorted(
                            fat_areas,
                            key=lambda item: point_polygon_distance_meter(point, item.polygon),
                        )
                        if hp_cover_fat_folder_name(area.fat_code or area.name).upper() != fat_folder_key
                        and point_polygon_distance_meter(point, area.polygon) <= HP_COVER_OVERFLOW_REASSIGN_RADIUS_METER
                        and hp_cover_counts.get(hp_cover_fat_folder_name(area.fat_code or area.name).upper(), 0) < HP_COVER_MAX_PER_FAT
                    ),
                    None,
                )
                if target_area:
                    target_key = hp_cover_fat_folder_name(target_area.fat_code or target_area.name).upper()
                    target_folder = hp_cover_folders.get(target_key)
                    if target_folder is None:
                        target_folder = ensure_folder(hp_cover_folder, namespace, hp_cover_fat_folder_name(target_area.fat_code or target_area.name))
                        hp_cover_folders[target_key] = target_folder
                    target_folder.append(placemark)
                    hp_cover_counts[target_key] = hp_cover_counts.get(target_key, 0) + 1
                    target_path = " / ".join([line_folder_name, "HP COVER", hp_cover_fat_folder_name(target_area.fat_code or target_area.name)])
                    reason = f"HP COVER per FAT exceeds {HP_COVER_MAX_PER_FAT}; reassigned to nearest FAT within {HP_COVER_OVERFLOW_REASSIGN_RADIUS_METER:g}m that is not full"
                else:
                    hp_uncover_folder.append(placemark)
                    target_path = " / ".join([line_folder_name, "HP UNCOVER"])
                    reason = f"HP COVER per FAT exceeds {HP_COVER_MAX_PER_FAT} and no nearby FAT within {HP_COVER_OVERFLOW_REASSIGN_RADIUS_METER:g}m has available capacity"
                moved += 1
                changes.append({
                    "name": name,
                    "from": " / ".join([line_folder_name, "HP COVER", fat_folder_name]),
                    "to": target_path,
                    "reason": reason,
                })
    return moved


def assign_all_hp_cover_to_boundary(
    project_folder: ET.Element,
    namespace: str,
    fat_areas: Sequence[OrganizerFatAreaRef],
    changes: list[dict],
) -> int:
    """Route HP COVER points into BOUNDARY FAT buckets by containment.

    A containing polygon wins across LINE/FDT boundaries; the legacy same-line
    nearest-polygon fallback remains for points outside every polygon.
    """
    all_areas = [area for area in fat_areas if area.polygon]
    moved = 0
    for line_folder in list(project_folder):
        if local_name(line_folder.tag) != "Folder":
            continue
        line_name = normalize_text(direct_child_text_by_name(line_folder, "name"))
        if not LINE_ORDER_PATTERN.search(line_name):
            continue
        hp_cover = direct_child_folder_by_name(line_folder, "HP COVER")
        if hp_cover is None:
            continue
        line_areas = [
            area for area in all_areas
            if format_line_folder_name(area.line_order, area.fdt_order) == line_name
        ]
        if not line_areas:
            continue

        parents: list[tuple[ET.Element, ET.Element]] = []
        def collect(parent: ET.Element) -> None:
            for child in list(parent):
                kind = local_name(child.tag)
                if kind == "Placemark" and placemark_has_point(child):
                    parents.append((parent, child))
                elif kind == "Folder":
                    collect(child)
        collect(hp_cover)

        targets: dict[tuple[str, str], ET.Element] = {}
        for source_parent, placemark in parents:
            # A named HP COVER bucket is authoritative input. Do not spatially
            # reassign its points after the source has already grouped them
            # under FAT A01/A02/...; HPDB must see the same parent FAT.
            source_bucket = normalize_text(direct_child_text_by_name(source_parent, "name") or "")
            if find_fat_code(source_bucket):
                continue
            point = placemark_point_coordinate(placemark)
            if point is None:
                continue
            inside = [area for area in all_areas if point_in_polygon(point, area.polygon)]
            target_area = min(
                inside or line_areas,
                key=lambda area: (
                    hp_cover_fat_folder_name(area.fat_code or area.name),
                    area.line_order,
                    area.fdt_order,
                    point_polygon_distance_meter(point, area.polygon),
                ),
            )
            bucket_name = hp_cover_fat_folder_name(target_area.fat_code or target_area.name)
            # Never produce a generic HP bucket when a named BOUNDARY FAT exists.
            if not bucket_name or bucket_name.upper() in {"UNCATEGORIZED", "UNKNOWN", "UNCLASSIFIED"}:
                continue
            target_line_name = format_line_folder_name(target_area.line_order, target_area.fdt_order)
            key = (target_line_name, bucket_name.upper())
            target = targets.get(key)
            if target is None:
                target_hp_cover = hp_cover
                if target_line_name != line_name:
                    target_line = ensure_line_folder(project_folder, namespace, target_area.line_order, target_area.fdt_order)
                    target_hp_cover = ensure_folder(target_line, namespace, "HP COVER")
                target = direct_child_folder_by_name(target_hp_cover, bucket_name)
                if target is None:
                    target = ensure_folder(target_hp_cover, namespace, bucket_name)
                targets[key] = target
            if source_parent is target:
                continue
            source_parent.remove(placemark)
            target.append(placemark)
            moved += 1
            changes.append({
                "name": normalize_text(direct_child_text_by_name(placemark, "name")),
                "from": " / ".join([line_name, "HP COVER"]),
                "to": " / ".join([target_line_name, "HP COVER", bucket_name]),
                "reason": "HP COVER routed by containing BOUNDARY FAT polygon",
            })

        # Empty legacy bucket folders may be removed; no placemark is removed.
        for child in list(hp_cover):
            if local_name(child.tag) != "Folder":
                continue
            if any(local_name(x.tag) == "Placemark" for x in list(child)):
                continue
            if any(local_name(x.tag) == "Folder" for x in list(child)):
                continue
            hp_cover.remove(child)
    return moved

def assign_uncategorized_hp_cover_to_boundary(
    project_folder: ET.Element,
    namespace: str,
    fat_areas: Sequence[OrganizerFatAreaRef],
    changes: list[dict],
) -> int:
    """Move HP COVER/UNCATEGORIZED points into same-line HP COVER/<FAT_CODE> buckets.

    Legacy KMZs can already contain HP COVER/UNCATEGORIZED. If this folder name is
    treated as a FAT bucket, spatial assignment never runs. Use BOUNDARY FAT geometry:
    point inside polygon wins; otherwise nearest same-line FAT wins. No UNCATEGORIZED remains.
    """
    moved = 0
    for line_folder in list(project_folder):
        if local_name(line_folder.tag) != "Folder":
            continue
        line_folder_name = normalize_text(direct_child_text_by_name(line_folder, "name"))
        if not LINE_ORDER_PATTERN.search(line_folder_name):
            continue
        hp_cover_folder = direct_child_folder_by_name(line_folder, "HP COVER")
        if hp_cover_folder is None:
            continue
        uncat_folder = None
        for folder in list(hp_cover_folder):
            if local_name(folder.tag) != "Folder":
                continue
            folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
            if folder_name.upper() in {"UNCATEGORIZED", "UNKNOWN", "UNCLASSIFIED"}:
                uncat_folder = folder
                break
        if uncat_folder is None:
            continue

        line_fat_areas = [
            area for area in fat_areas
            if format_line_folder_name(area.line_order, area.fdt_order) == line_folder_name
        ]
        if not line_fat_areas:
            continue

        hp_cover_folders: dict[str, ET.Element] = {}
        for folder in list(hp_cover_folder):
            if local_name(folder.tag) != "Folder" or folder is uncat_folder:
                continue
            folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
            key = hp_cover_fat_folder_name(folder_name).upper()
            hp_cover_folders[key] = folder

        for placemark in list(iter_direct_placemarks(uncat_folder)):
            point = placemark_point_coordinate(placemark)
            if point is None:
                continue
            containing = [area for area in line_fat_areas if point_in_polygon(point, area.polygon)]
            if containing:
                target_area = sorted(containing, key=lambda area: point_polygon_distance_meter(point, area.polygon))[0]
            else:
                target_area = sorted(line_fat_areas, key=lambda area: point_polygon_distance_meter(point, area.polygon))[0]
            target_name = hp_cover_fat_folder_name(target_area.fat_code or target_area.name)
            if target_name.upper() in {"UNCATEGORIZED", "UNKNOWN", "UNCLASSIFIED"}:
                continue
            target_key = target_name.upper()
            target_folder = hp_cover_folders.get(target_key)
            if target_folder is None:
                target_folder = ensure_folder(hp_cover_folder, namespace, target_name)
                hp_cover_folders[target_key] = target_folder
            uncat_folder.remove(placemark)
            target_folder.append(placemark)
            moved += 1
            changes.append({
                "name": normalize_text(direct_child_text_by_name(placemark, "name")),
                "from": " / ".join([line_folder_name, "HP COVER", "UNCATEGORIZED"]),
                "to": " / ".join([line_folder_name, "HP COVER", target_name]),
                "reason": "HP COVER/UNCATEGORIZED assigned to same-line BOUNDARY FAT",
            })

        if not iter_direct_placemarks(uncat_folder) and not any(local_name(child.tag) == "Folder" for child in list(uncat_folder)):
            hp_cover_folder.remove(uncat_folder)
    return moved


def rescue_hp_uncover_inside_available_boundary(
    project_folder: ET.Element,
    namespace: str,
    fat_areas: Sequence[OrganizerFatAreaRef],
    changes: list[dict],
) -> int:
    """Move HP UNCOVER points back to HP COVER when they are inside a same-line BOUNDARY FAT with capacity."""
    moved = 0
    for line_folder in list(project_folder):
        if local_name(line_folder.tag) != "Folder":
            continue
        line_folder_name = normalize_text(direct_child_text_by_name(line_folder, "name"))
        if not LINE_ORDER_PATTERN.search(line_folder_name):
            continue
        hp_uncover_folder = direct_child_folder_by_name(line_folder, "HP UNCOVER")
        hp_cover_folder = ensure_folder(line_folder, namespace, "HP COVER")
        if hp_uncover_folder is None:
            continue

        hp_cover_counts: dict[str, int] = {}
        hp_cover_folders: dict[str, ET.Element] = {}
        for folder in list(hp_cover_folder):
            if local_name(folder.tag) != "Folder":
                continue
            folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
            key = hp_cover_fat_folder_name(folder_name).upper()
            hp_cover_counts[key] = len(iter_direct_placemarks(folder))
            hp_cover_folders[key] = folder

        line_fat_areas = [
            area for area in fat_areas
            if format_line_folder_name(area.line_order, area.fdt_order) == line_folder_name
        ]
        if not line_fat_areas:
            continue

        for placemark in list(iter_direct_placemarks(hp_uncover_folder)):
            point = placemark_point_coordinate(placemark)
            if point is None:
                continue
            containing = [area for area in line_fat_areas if point_in_polygon(point, area.polygon)]
            if not containing:
                continue
            target_area = next(
                (
                    area for area in sorted(containing, key=lambda item: point_polygon_distance_meter(point, item.polygon))
                    if hp_cover_counts.get(hp_cover_fat_folder_name(area.fat_code or area.name).upper(), 0) < HP_COVER_MAX_PER_FAT
                ),
                None,
            )
            if target_area is None:
                continue
            target_name = hp_cover_fat_folder_name(target_area.fat_code or target_area.name)
            target_key = target_name.upper()
            target_folder = hp_cover_folders.get(target_key)
            if target_folder is None:
                target_folder = ensure_folder(hp_cover_folder, namespace, target_name)
                hp_cover_folders[target_key] = target_folder
                hp_cover_counts[target_key] = 0
            hp_uncover_folder.remove(placemark)
            target_folder.append(placemark)
            hp_cover_counts[target_key] = hp_cover_counts.get(target_key, 0) + 1
            moved += 1
            name = normalize_text(direct_child_text_by_name(placemark, "name"))
            changes.append({
                "name": name,
                "from": " / ".join([line_folder_name, "HP UNCOVER"]),
                "to": " / ".join([line_folder_name, "HP COVER", target_name]),
                "reason": "HP UNCOVER point is inside BOUNDARY FAT polygon with available capacity",
            })
    return moved

def collect_organizer_placemarks(element: ET.Element, folder_stack: list[str]) -> list[OrganizerPlacemarkRef]:
    refs: list[OrganizerPlacemarkRef] = []
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
            refs.extend(collect_organizer_placemarks(child, next_stack))
        elif child_name == "Placemark":
            refs.append(OrganizerPlacemarkRef(parent=element, element=child, folder_stack=list(folder_stack)))
    return refs


SLING_ANCHOR_TAG = "{urn:eva:internal}sling-anchor"


def is_sling_wire_folder_name(value: object) -> bool:
    compact = re.sub(r"[^A-Z0-9]+", "", normalize_folder_name(str(value or "")))
    return compact in {"SLINGWIRE", "STEELWIRE"}


def quarantine_cluster_ae_sling_folders(root: ET.Element) -> list[dict]:
    """Detach complete Sling subtrees so generic standardizer passes cannot consume them."""
    quarantined: list[dict] = []

    def walk(parent: ET.Element, folder_stack: list[str]) -> None:
        for child in list(parent):
            child_name = local_name(child.tag)
            if child_name not in {"Document", "Folder"}:
                continue
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = [*folder_stack, folder_name]
                if is_sling_wire_folder_name(folder_name):
                    anchor = ET.Element(SLING_ANCHOR_TAG, {"token": str(len(quarantined))})
                    index = list(parent).index(child)
                    parent.remove(child)
                    parent.insert(index, anchor)
                    quarantined.append(
                        {
                            "anchor": anchor,
                            "folder": child,
                            "source_path": tuple(next_stack),
                        }
                    )
                    continue
            walk(child, next_stack)

    walk(root, [])
    return quarantined


def _element_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        if any(child is target for child in list(parent)):
            return parent
    return None


def restore_quarantined_sling_folders(root: ET.Element, quarantined: Sequence[dict]) -> None:
    for item in quarantined:
        anchor = item["anchor"]
        parent = _element_parent(root, anchor)
        if parent is None:
            raise ValueError("Internal Sling anchor hilang selama standardisasi Cluster AE.")
        index = list(parent).index(anchor)
        parent.remove(anchor)
        parent.insert(index, item["folder"])


def collect_organizer_fdt_refs(
    placemark_refs: Sequence[OrganizerPlacemarkRef],
    records: Sequence[FdtCodingRecord],
) -> list[OrganizerFdtRef]:
    """Collect physical FDT ordinals from placemark order, not coding IDs.

    ``FdtCodingRecord.fdt_order`` may contain a spreadsheet/site ID such as
    156.  That value is useful for coding/BOQ lookup only; it must never become
    the physical parent-folder ordinal.  Explicit ``FDT N`` source labels stay
    authoritative.  Otherwise the ordered FDT placemarks are FDT 1, FDT 2, ...
    """
    refs: list[OrganizerFdtRef] = []
    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        point = placemark_point_coordinate(placemark)
        if point is None or not looks_like_fdt_placemark(placemark, placemark_ref.folder_stack):
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        explicit_order = extract_fdt_order(" ".join([*placemark_ref.folder_stack, name]))
        order = explicit_order if explicit_order is not None else len(refs) + 1
        refs.append(OrganizerFdtRef(order=order, point=point))
    return refs

def nearest_endpoint_fdt_order(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    fdt_refs: Sequence[OrganizerFdtRef],
) -> int:
    endpoints = [point for line in linestrings if line for point in (line[0], line[-1])]
    if not endpoints or not fdt_refs:
        return 1
    return min(fdt_refs, key=lambda ref: min(point_distance_sq(endpoint, ref.point) for endpoint in endpoints)).order

def nearest_start_fdt_order(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    fdt_refs: Sequence[OrganizerFdtRef],
) -> int:
    start_points = [line[0] for line in linestrings if line]
    if not start_points or not fdt_refs:
        return 1
    return min(fdt_refs, key=lambda ref: min(point_distance_sq(point, ref.point) for point in start_points)).order

def endpoint_fat_area(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    fat_areas: Sequence[OrganizerFatAreaRef],
) -> OrganizerFatAreaRef | None:
    end_points = [line[-1] for line in linestrings if line]
    if not end_points or not fat_areas:
        return None
    for point in end_points:
        contained = containing_fat_area(point, fat_areas)
        if contained:
            return contained
    return min(fat_areas, key=lambda area: min(point_polygon_distance_sq(point, area.polygon) for point in end_points))

def collect_organizer_cable_refs(
    placemark_refs: Sequence[OrganizerPlacemarkRef],
    fdt_refs: Sequence[OrganizerFdtRef],
    fat_areas: Sequence[OrganizerFatAreaRef] = (),
    prefer_parent_owner: bool = False,
) -> list[OrganizerCableRef]:
    refs: list[OrganizerCableRef] = []
    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        if not placemark_has_line(placemark):
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        description = direct_child_text_by_name(placemark, "description")
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        raw_source_folder = normalize_folder_name(placemark_ref.folder_stack[-1] if placemark_ref.folder_stack else "")
        compact_source_folder = re.sub(r"[^A-Z0-9]+", "", raw_source_folder)
        if source_folder == "SLING WIRE" or compact_source_folder in {"SLINGWIRE", "STEELWIRE"}:
            continue
        cable_folder_context = (
            source_folder in {"DISTRIBUTION CABLE", "CABLE"}
            or compact_source_folder in {"CABLE", "DISTRIBUTIONCABLE", "LINECABLE", "CABLELINE"}
        )
        # Never infer cable capacity from an ancestor such as "FDT 1 72C";
        # doing so turns unrelated Sling Wire paths into distribution cables.
        capacity = detect_distribution_cable_capacity(name, description, raw_source_folder if cable_folder_context else "")
        if not capacity and not cable_folder_context:
            continue
        capacity = capacity or "UNKNOWN"
        linestrings = placemark_linestrings(placemark)
        if not linestrings:
            continue
        fat_area = endpoint_fat_area(linestrings, fat_areas)
        if prefer_parent_owner:
            parent_line_order = extract_line_folder_order(placemark_ref.folder_stack)
            parent_fdt_order = next(
                (
                    extract_fdt_order(folder)
                    for folder in reversed(placemark_ref.folder_stack)
                    if extract_fdt_order(folder) is not None
                ),
                None,
            )
        else:
            parent_line_order = None
            parent_fdt_order = None
        fdt_order = parent_fdt_order or extract_fdt_order(" ".join([*placemark_ref.folder_stack, name])) or nearest_start_fdt_order(linestrings, fdt_refs)
        line_order = parent_line_order or explicit_line_order(name, description, *reversed(placemark_ref.folder_stack))
        if not line_order and fat_area:
            line_order = explicit_line_order(fat_area.fat_code, fat_area.name)
        if not line_order:
            # Ambiguous geometry must not invent LINE B/C/D/E from traversal
            # order. Spatial FAT matching above is authoritative; otherwise A
            # is the only safe fallback.
            line_order = "A"
        refs.append(
            OrganizerCableRef(
                element=placemark,
                line_order=line_order,
                fdt_order=fdt_order,
                capacity=capacity,
                coordinates=[list(line) for line in linestrings],
            )
        )
    return refs

def nearest_cable_ref(point: tuple[float, float], cable_refs: Sequence[OrganizerCableRef]) -> OrganizerCableRef | None:
    if not cable_refs:
        return None
    return min(cable_refs, key=lambda ref: min(point_linestring_distance_sq(point, line) for line in ref.coordinates))

def nearest_cable_ref_for_linestring(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    cable_refs: Sequence[OrganizerCableRef],
) -> OrganizerCableRef | None:
    points = [point for line in linestrings for point in line]
    if not points or not cable_refs:
        return None
    return min(
        cable_refs,
        key=lambda ref: min(
            point_linestring_distance_sq(point, cable_line)
            for point in points
            for cable_line in ref.coordinates
        ),
    )


def _segment_distance_orientation(
    start: tuple[float, float],
    end: tuple[float, float],
    point: tuple[float, float],
) -> int:
    first = (end[0] - start[0]) * (point[1] - start[1])
    second = (end[1] - start[1]) * (point[0] - start[0])
    cross = first - second
    tolerance = max(abs(first), abs(second), 1e-300) * 1e-12
    if abs(cross) <= tolerance:
        return 0
    return 1 if cross > 0 else -1


def _point_on_segment_for_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    tolerance = max(abs(end[0] - start[0]), abs(end[1] - start[1]), 1.0) * 1e-12
    return (
        min(start[0], end[0]) - tolerance <= point[0] <= max(start[0], end[0]) + tolerance
        and min(start[1], end[1]) - tolerance <= point[1] <= max(start[1], end[1]) + tolerance
    )


def _segments_intersect_for_distance(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    first_a = _segment_distance_orientation(first_start, first_end, second_start)
    first_b = _segment_distance_orientation(first_start, first_end, second_end)
    second_a = _segment_distance_orientation(second_start, second_end, first_start)
    second_b = _segment_distance_orientation(second_start, second_end, first_end)
    if first_a * first_b < 0 and second_a * second_b < 0:
        return True
    return (
        (first_a == 0 and _point_on_segment_for_distance(second_start, first_start, first_end))
        or (first_b == 0 and _point_on_segment_for_distance(second_end, first_start, first_end))
        or (second_a == 0 and _point_on_segment_for_distance(first_start, second_start, second_end))
        or (second_b == 0 and _point_on_segment_for_distance(first_end, second_start, second_end))
    )


def segment_distance_sq(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> float:
    if _segments_intersect_for_distance(first_start, first_end, second_start, second_end):
        return 0.0
    return min(
        point_segment_distance_sq(first_start, second_start, second_end),
        point_segment_distance_sq(first_end, second_start, second_end),
        point_segment_distance_sq(second_start, first_start, first_end),
        point_segment_distance_sq(second_end, first_start, first_end),
    )


def linestring_distance_sq(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> float:
    if not first or not second:
        return float("inf")
    if len(first) == 1:
        return point_linestring_distance_sq(first[0], second)
    if len(second) == 1:
        return point_linestring_distance_sq(second[0], first)
    return min(
        segment_distance_sq(first_start, first_end, second_start, second_end)
        for first_start, first_end in zip(first, first[1:])
        for second_start, second_end in zip(second, second[1:])
    )


def nearest_cable_ref_for_sling_linestring(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    cable_refs: Sequence[OrganizerCableRef],
) -> OrganizerCableRef | None:
    if not any(linestrings) or not cable_refs:
        return None

    def candidate_key(ref: OrganizerCableRef) -> tuple:
        distance = min(
            linestring_distance_sq(sling_line, cable_line)
            for sling_line in linestrings
            if sling_line
            for cable_line in ref.coordinates
            if cable_line
        )
        geometry_key = tuple(tuple(line) for line in ref.coordinates)
        return distance, int(ref.fdt_order or 1), normalize_text(ref.line_order).upper(), geometry_key

    return min(cable_refs, key=candidate_key)


def collect_cluster_ae_distribution_cable_refs(root: ET.Element) -> list[OrganizerCableRef]:
    """Collect final Distribution Cable geometry from explicit physical LINE parents."""
    cable_refs: list[OrganizerCableRef] = []
    for placemark_ref in collect_organizer_placemarks(root, []):
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        if source_folder not in {"DISTRIBUTION CABLE", "CABLE"}:
            continue
        linestrings = placemark_linestrings(placemark_ref.element)
        line_order = extract_line_folder_order(placemark_ref.folder_stack)
        if not linestrings or not line_order:
            continue
        fdt_order = next(
            (
                extract_fdt_order(folder)
                for folder in reversed(placemark_ref.folder_stack)
                if extract_fdt_order(folder) is not None
            ),
            1,
        )
        cable_refs.append(
            OrganizerCableRef(
                element=placemark_ref.element,
                line_order=line_order,
                fdt_order=fdt_order,
                capacity="UNKNOWN",
                coordinates=[list(line) for line in linestrings],
            )
        )
    return cable_refs


def route_quarantined_cluster_ae_sling(
    root: ET.Element,
    namespace: str,
    project_folder_name: str,
    quarantined: Sequence[dict],
    organizer_changes: list[dict],
    name_changes: list[dict],
    style_changes: list[dict],
) -> tuple[int, int]:
    """Restore Sling subtrees, then route each LineString to its nearest final cable."""
    cable_refs = collect_cluster_ae_distribution_cable_refs(root)
    restore_quarantined_sling_folders(root, quarantined)
    for item in quarantined:
        folder_name = ensure_direct_child(item["folder"], "name", namespace)
        old_folder_name = normalize_text(folder_name.text)
        if old_folder_name != "SLING WIRE":
            folder_name.text = "SLING WIRE"
            organizer_changes.append(
                {
                    "name": old_folder_name,
                    "from": " / ".join(item["source_path"]),
                    "to": "SLING WIRE",
                    "reason": "Cluster AE Sling folder alias canonicalized before routing.",
                }
            )
    project_folder = find_project_container(root, namespace, project_folder_name)
    moved = 0
    renamed = 0

    def direct_line_placemarks(parent: ET.Element):
        """Yield only branch placemarks owned directly by the quarantined Sling wrapper.

        Nested folders are source-owned metadata subtrees. Their Point, Polygon,
        MultiGeometry, and LineString descendants must keep first-pass ancestry
        and payload byte-semantics; they are never branch-routing candidates.
        """
        for child in list(parent):
            if local_name(child.tag) == "Placemark" and placemark_has_line(child):
                yield parent, child

    for item in quarantined:
        source_path = " / ".join(item["source_path"])
        for source_parent, placemark in list(direct_line_placemarks(item["folder"])):
            linestrings = placemark_linestrings(placemark)
            if not linestrings:
                continue
            name_element = ensure_direct_child(placemark, "name", namespace)
            old_name = normalize_text(name_element.text)
            if old_name != "SLING WIRE":
                name_element.text = "SLING WIRE"
                renamed += 1
                name_changes.append({"from": old_name, "to": "SLING WIRE", "folder": "SLING WIRE"})
            apply_standard_style(
                placemark,
                "sling_wire",
                namespace,
                "SLING WIRE",
                {},
                style_changes,
                preserve_extension_payload=True,
            )
            cable_ref = nearest_cable_ref_for_sling_linestring(linestrings, cable_refs)
            if cable_ref is None:
                continue
            target_path = [
                format_line_folder_name(cable_ref.line_order, cable_ref.fdt_order),
                "SLING WIRE",
            ]
            target_folder = target_folder_for_path(project_folder, namespace, target_path)
            source_line = extract_line_folder_order(item["source_path"])
            source_fdt = next(
                (
                    extract_fdt_order(folder)
                    for folder in reversed(item["source_path"])
                    if extract_fdt_order(folder) is not None
                ),
                1,
            )
            same_owner = (
                source_line == cable_ref.line_order
                and source_fdt == cable_ref.fdt_order
            )
            if source_parent is not target_folder:
                source_parent.remove(placemark)
                target_folder.append(placemark)
                if not same_owner:
                    moved += 1
                    organizer_changes.append(
                        {
                            "name": normalize_text(direct_child_text_by_name(placemark, "name")),
                            "from": source_path,
                            "to": " / ".join(target_path),
                            "reason": "Cluster AE Sling follows nearest Distribution Cable geometry.",
                        }
                    )

    def consolidate_line_sling_folders(parent: ET.Element) -> None:
        for child in list(parent):
            child_name = local_name(child.tag)
            if child_name == "Document":
                consolidate_line_sling_folders(child)
            elif child_name == "Folder" and not is_sling_wire_folder_name(
                direct_child_text_by_name(child, "name")
            ):
                consolidate_line_sling_folders(child)
        if local_name(parent.tag) != "Folder" or not is_line_folder_name(
            direct_child_text_by_name(parent, "name")
        ):
            return
        folders = [
            child
            for child in list(parent)
            if local_name(child.tag) == "Folder"
            and normalize_text(direct_child_text_by_name(child, "name")).upper() == "SLING WIRE"
        ]
        if len(folders) < 2:
            return

        def non_line_count(folder: ET.Element) -> int:
            return sum(
                1
                for descendant in folder.iter()
                if local_name(descendant.tag) == "Placemark"
                and not placemark_has_line(descendant)
            )

        target = max(
            enumerate(folders),
            key=lambda item: (non_line_count(item[1]), -item[0]),
        )[1]
        for source in folders:
            if source is target:
                continue
            for child in list(source):
                if local_name(child.tag) in FOLDER_METADATA_CHILDREN:
                    continue
                source.remove(child)
                target.append(child)
            parent.remove(source)

    # Consolidate only direct physical LINE wrappers. Nested Sling folders are
    # source metadata and must remain byte-semantic, including empty wrappers.
    consolidate_line_sling_folders(root)
    # A legacy LINE wrapper may be flattened while its quarantined Sling anchor
    # is still inside it. Restore then routes branches into the new LINE, leaving
    # a name-only Sling ghost directly under the project. Keep any real payload.
    for item in quarantined:
        folder = item["folder"]
        if (extract_line_folder_order(item["source_path"][:-1]) is not None
                and _element_parent(root, folder) is project_folder
                and not folder.attrib
                and not normalize_text(folder.text)
                and not normalize_text(folder.tail)
                and len(folder) == 1
                and local_name(folder[0].tag) == "name"
                and not folder[0].attrib
                and not folder[0]
                and not normalize_text(folder[0].tail)):
            project_folder.remove(folder)
    return moved, renamed


def placemark_geometry_samples(placemark: ET.Element) -> list[list[tuple[float, float]]]:
    """Return geometry points/paths usable for nearest-line matching."""
    samples: list[list[tuple[float, float]]] = []
    point = placemark_point_coordinate(placemark)
    if point is not None:
        samples.append([point])
    samples.extend(placemark_linestrings(placemark))
    polygon = placemark_polygon_coordinates(placemark)
    if polygon:
        samples.append(polygon)
    return samples


def is_cluster_subduct_source_folder(folder_name: str) -> bool:
    """Recognize Cluster SUBDUCT source folders before they are rehomed."""
    canonical = canonical_folder_match(folder_name, "UG", "Cluster")
    return bool(canonical and canonical.startswith("SUBDUCT "))


def descendant_placemarks(element: ET.Element) -> list[ET.Element]:
    placemarks: list[ET.Element] = []
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Placemark":
            placemarks.append(child)
        elif child_name in {"Document", "Folder"}:
            placemarks.extend(descendant_placemarks(child))
    return placemarks


def nearest_subduct_route_cable_ref(
    placemark: ET.Element,
    route_placemarks: Sequence[ET.Element],
    cable_refs: Sequence[OrganizerCableRef],
    max_distance_meter: float | None = None,
) -> OrganizerCableRef | None:
    """Resolve geometry through its nearest physical SUBDUCT route."""
    marker_points = [point for line in placemark_geometry_samples(placemark) for point in line]
    candidates: list[tuple[float, OrganizerCableRef]] = []
    for route_placemark in route_placemarks:
        route_lines = [line for line in placemark_linestrings(route_placemark) if line]
        cable_ref = nearest_cable_ref_for_linestring(route_lines, cable_refs)
        if not marker_points or cable_ref is None:
            continue
        distance_meter = min(
            _haversine_distance_meter(marker_point, route_line[0])
            if len(route_line) == 1
            else min(
                _point_to_segment_distance_meter(marker_point, start, end)
                for start, end in zip(route_line, route_line[1:])
            )
            for marker_point in marker_points
            for route_line in route_lines
        )
        candidates.append((distance_meter, cable_ref))
    if not candidates:
        return None
    distance_meter, cable_ref = min(candidates, key=lambda item: item[0])
    if max_distance_meter is not None and distance_meter > max_distance_meter:
        return None
    return cable_ref


def collect_tway_subduct_cable_refs(
    root: ET.Element,
    cable_refs: Sequence[OrganizerCableRef],
) -> dict[int, OrganizerCableRef]:
    """Map T-WAY through a source SUBDUCT route, never directly through cable proximity."""
    refs_by_placemark: dict[int, OrganizerCableRef] = {}
    if not cable_refs:
        return refs_by_placemark

    placemark_refs = collect_organizer_placemarks(root, [])
    tway_refs = [
        placemark_ref
        for placemark_ref in placemark_refs
        if contains_tway_alias(direct_child_text_by_name(placemark_ref.element, "name"))
        or any(is_tway_folder_alias(folder) for folder in placemark_ref.folder_stack)
    ]
    tway_ids = {id(placemark_ref.element) for placemark_ref in tway_refs}

    # A marker physically inside a named source SUBDUCT has an explicit route
    # context. This relation wins even if another route runs closer nearby.
    source_groups: list[tuple[list[ET.Element], list[ET.Element]]] = []

    def walk(element: ET.Element) -> None:
        for child in list(element):
            child_name = local_name(child.tag)
            if child_name not in {"Document", "Folder"}:
                continue
            if child_name == "Folder" and is_cluster_subduct_source_folder(
                normalize_text(direct_child_text_by_name(child, "name"))
            ):
                descendants = descendant_placemarks(child)
                markers = [placemark for placemark in descendants if id(placemark) in tway_ids]
                routes = [
                    placemark
                    for placemark in descendants
                    if id(placemark) not in tway_ids and placemark_linestrings(placemark)
                ]
                # Keep every source SUBDUCT route. Root T-WAY markers have no
                # folder context and must still resolve through this physical
                # route; markers inside this folder get local-route priority.
                if routes:
                    source_groups.append((markers, routes))
            walk(child)

    walk(root)
    all_routes: list[ET.Element] = []
    route_ids: set[int] = set()
    for markers, routes in source_groups:
        for marker in markers:
            cable_ref = nearest_subduct_route_cable_ref(marker, routes, cable_refs)
            if cable_ref is not None:
                refs_by_placemark[id(marker)] = cable_ref
        for route in routes:
            if id(route) not in route_ids:
                route_ids.add(id(route))
                all_routes.append(route)

    # Root T-WAY has no folder context. Associate it only when it is physically
    # close to a real SUBDUCT route; a nearby Distribution Cable alone is never
    # sufficient to assign a LINE.
    for placemark_ref in tway_refs:
        placemark = placemark_ref.element
        if id(placemark) in refs_by_placemark:
            continue
        cable_ref = nearest_subduct_route_cable_ref(
            placemark,
            all_routes,
            cable_refs,
            TWAY_SUBDUCT_ROUTE_LINK_MAX_METER,
        )
        if cable_ref is not None:
            refs_by_placemark[id(placemark)] = cable_ref
    return refs_by_placemark


def collect_tali_pancing_subduct_cable_refs(
    root: ET.Element,
    cable_refs: Sequence[OrganizerCableRef],
) -> dict[int, OrganizerCableRef]:
    """Keep an existing TALI PANCING LineString with its sibling SUBDUCT LINE."""
    refs_by_placemark: dict[int, OrganizerCableRef] = {}
    if not cable_refs:
        return refs_by_placemark

    def route_placemarks(folder: ET.Element) -> list[ET.Element]:
        return [
            placemark
            for placemark in descendant_placemarks(folder)
            if placemark_linestrings(placemark)
            and not contains_tway_alias(direct_child_text_by_name(placemark, "name"))
        ]

    def walk(parent: ET.Element) -> None:
        folders = [child for child in list(parent) if local_name(child.tag) == "Folder"]
        subduct_folders = [
            child
            for child in folders
            if is_cluster_subduct_source_folder(normalize_text(direct_child_text_by_name(child, "name")))
        ]
        all_routes: list[ET.Element] = []
        subduct_40_34_routes: list[ET.Element] = []
        for subduct in subduct_folders:
            routes = route_placemarks(subduct)
            all_routes.extend(routes)
            if canonical_folder_match(
                normalize_text(direct_child_text_by_name(subduct, "name")), "UG", "Cluster"
            ) == "SUBDUCT 40/34":
                subduct_40_34_routes.extend(routes)
        linked_routes = subduct_40_34_routes or all_routes
        for tali_pancing in folders:
            if canonical_folder_match(
                normalize_text(direct_child_text_by_name(tali_pancing, "name")), "UG", "Cluster"
            ) != "TALI PANCING":
                continue
            for placemark in descendant_placemarks(tali_pancing):
                if not placemark_linestrings(placemark):
                    continue
                cable_ref = nearest_subduct_route_cable_ref(placemark, linked_routes, cable_refs)
                if cable_ref is not None:
                    refs_by_placemark[id(placemark)] = cable_ref
        for child in list(parent):
            if local_name(child.tag) in {"Document", "Folder"}:
                walk(child)

    walk(root)
    return refs_by_placemark


def min_linestring_fdt_distance_meter(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    fdt_refs: Sequence[OrganizerFdtRef],
) -> float:
    points = [point for line in linestrings for point in line]
    if not points or not fdt_refs:
        return 0.0
    return min(calculate_linestring_length_meter([[point, ref.point]]) for point in points for ref in fdt_refs)

def looks_like_auto_sling_wire_line(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    fdt_refs: Sequence[OrganizerFdtRef],
    cable_refs: Sequence[OrganizerCableRef],
    network_type: str | None,
) -> bool:
    if normalize_text(network_type).upper() != "AE" or not cable_refs:
        return False
    length_meter = calculate_linestring_length_meter(linestrings)
    if 0 < length_meter <= AUTO_SLING_WIRE_MAX_LENGTH_M:
        return True
    return min_linestring_fdt_distance_meter(linestrings, fdt_refs) >= AUTO_SLING_WIRE_MIN_FDT_DISTANCE_M

def infer_line_fdt_for_point(
    point: tuple[float, float] | None,
    folder_stack: Sequence[str],
    name: str,
    cable_refs: Sequence[OrganizerCableRef],
) -> tuple[str, int]:
    line_order = extract_line_folder_order(folder_stack) or explicit_line_order(name)
    fdt_order = extract_fdt_order(" ".join([*folder_stack, name]))
    nearest = nearest_cable_ref(point, cable_refs) if point is not None else None
    return line_order or (nearest.line_order if nearest else "A"), fdt_order or (nearest.fdt_order if nearest else 1)

def collect_organizer_fat_areas(
    placemark_refs: Sequence[OrganizerPlacemarkRef],
    cable_refs: Sequence[OrganizerCableRef],
) -> list[OrganizerFatAreaRef]:
    refs: list[OrganizerFatAreaRef] = []
    used_fat_codes: set[str] = set()
    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        polygon = placemark_polygon_coordinates(placemark)
        if not polygon:
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        area_boundary_context = has_area_boundary_context(placemark_ref.folder_stack)
        fat_code = find_fat_code(name)
        if not fat_code and source_folder not in ("BOUNDARY FAT", "COVERAGE") and not area_boundary_context:
            continue
        intersecting_cable = None
        if cable_refs:
            for cref in cable_refs:
                if cable_intersects_polygon(cref.coordinates, polygon):
                    intersecting_cable = cref
                    break
        if not intersecting_cable and not fat_code:
            # keep for greedy boundary assignment even without cable intersection
            pass
        if intersecting_cable:
            line_order = intersecting_cable.line_order
            fdt_order = intersecting_cable.fdt_order
        else:
            center = polygon_centroid(polygon)
            line_order, fdt_order = infer_line_fdt_for_point(center, placemark_ref.folder_stack, name, cable_refs)
        refs.append(
            OrganizerFatAreaRef(
                name=name or fat_code or "FAT AREA",
                fat_code=fat_code or name or "FAT",
                line_order=line_order,
                fdt_order=fdt_order,
                polygon=polygon,
            )
        )
        used_fat_codes.add(hp_cover_fat_folder_name(fat_code or name).upper())

    fat_points: list[tuple[str, str, tuple[float, float], OrganizerPlacemarkRef]] = []
    hp_points: list[tuple[float, float]] = []
    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        point = placemark_point_coordinate(placemark)
        if point is None:
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        fat_code = find_fat_code(name)
        if fat_code and (source_folder == "FAT" or "FAT" in " ".join(placemark_ref.folder_stack).upper() or looks_like_fdt_code(name)):
            fat_points.append((fat_code, name, point, placemark_ref))
        if source_folder == "HP COVER" or has_hp_cover_folder_context(placemark_ref.folder_stack) or looks_like_hp_cover_name(name):
            hp_points.append(point)

    raw_polygon_refs: list[tuple[OrganizerPlacemarkRef, list[tuple[float, float]], int]] = []
    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        polygon = placemark_polygon_coordinates(placemark)
        if not polygon:
            continue
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        area_boundary_context = has_area_boundary_context(placemark_ref.folder_stack)
        if find_fat_code(name) or source_folder in ("BOUNDARY FAT", "COVERAGE"):
            continue
        hp_count = sum(1 for point in hp_points if point_in_polygon(point, polygon))
        if hp_count > FAT_AREA_INFER_MAX_HP:
            name_element = direct_child_by_name(placemark, "name")
            if name_element is not None:
                name_element.text = "BOUNDARY CLUSTER"
            continue
        if 1 <= hp_count <= FAT_AREA_INFER_MAX_HP:
            raw_polygon_refs.append((placemark_ref, polygon, hp_count))

    line_area_groups: dict[tuple[str, ...], tuple[OrganizerPlacemarkRef, list[list[tuple[float, float]]]]] = {}
    for placemark_ref in placemark_refs:
        source_folder = nearest_standard_folder(placemark_ref.folder_stack)
        raw_stack = " ".join(normalize_text(item).upper() for item in placemark_ref.folder_stack)
        if source_folder not in ("BOUNDARY FAT", "COVERAGE") and "FAT AREA" not in raw_stack and "COVERAGE" not in raw_stack and not has_area_boundary_context(placemark_ref.folder_stack):
            continue
        linestrings = placemark_linestrings(placemark_ref.element)
        if not linestrings:
            continue
        key = tuple(placemark_ref.folder_stack)
        if key not in line_area_groups:
            line_area_groups[key] = (placemark_ref, [])
        line_area_groups[key][1].extend(linestrings)

    for placemark_ref, linestrings in line_area_groups.values():
        polygon = linestrings_to_closed_polygon(linestrings)
        if not polygon:
            continue
        has_cable = False
        if cable_refs:
            for cref in cable_refs:
                if cable_intersects_polygon(cref.coordinates, polygon):
                    has_cable = True
                    break
        # keep even without cable intersection for greedy assignment
        
        hp_count = sum(1 for point in hp_points if point_in_polygon(point, polygon))
        if hp_count > FAT_AREA_INFER_MAX_HP:
            name_element = direct_child_by_name(placemark_ref.element, "name")
            if name_element is not None:
                name_element.text = "BOUNDARY CLUSTER"
            continue
        if 1 <= hp_count <= FAT_AREA_INFER_MAX_HP:
            raw_polygon_refs.append((placemark_ref, polygon, hp_count))

    for placemark_ref, polygon, _hp_count in sorted(raw_polygon_refs, key=lambda item: (polygon_cable_progress_meter(item[1], cable_refs), item[2]), reverse=True):
        intersecting_cable = None
        if cable_refs:
            for cref in cable_refs:
                if cable_intersects_polygon(cref.coordinates, polygon):
                    intersecting_cable = cref
                    break
        if not intersecting_cable:
            continue
        center = polygon_centroid(polygon)
        if center is None or not fat_points:
            continue
        fat_match = min(
            (
                item for item in fat_points
                if hp_cover_fat_folder_name(item[0]).upper() not in used_fat_codes
            ),
            key=lambda item: point_polygon_distance_sq(item[2], polygon),
            default=None,
        )
        if fat_match is None:
            continue
        fat_code, fat_name, fat_point, fat_ref = fat_match
        fat_key = hp_cover_fat_folder_name(fat_code).upper()
        line_order, fdt_order = infer_line_fdt_for_point(fat_point, fat_ref.folder_stack, fat_name, cable_refs)
        refs.append(
            OrganizerFatAreaRef(
                name=fat_name or fat_code,
                fat_code=fat_code,
                line_order=line_order,
                fdt_order=fdt_order,
                polygon=polygon,
            )
        )
        name_element = direct_child_by_name(placemark_ref.element, "name")
        if name_element is not None:
            name_element.text = fat_code
        used_fat_codes.add(fat_key)
    return refs

def containing_fat_area(point: tuple[float, float], fat_areas: Sequence[OrganizerFatAreaRef]) -> OrganizerFatAreaRef | None:
    matches = [area for area in fat_areas if point_in_polygon(point, area.polygon)]
    return matches[0] if matches else None

def nearest_fat_area(point: tuple[float, float], fat_areas: Sequence[OrganizerFatAreaRef]) -> OrganizerFatAreaRef | None:
    if not fat_areas:
        return None
    contained = containing_fat_area(point, fat_areas)
    if contained:
        return contained
    return min(fat_areas, key=lambda area: point_polygon_distance_sq(point, area.polygon))

def looks_like_hp_cover_name(name: str) -> bool:
    normalized = normalize_text(name).upper()
    if not normalized:
        return False
    if HP_COVER_NAME_PATTERN.search(normalized):
        return True
    return bool(re.search(r"\d", normalized) and not STANDARD_POLE_NUMBER_PATTERN.search(normalized) and not find_fat_code(normalized))

def has_pole_folder_context(folder_stack: Sequence[str]) -> bool:
    return any("POLE" in normalize_text(folder_name).upper() for folder_name in folder_stack)

def has_named_folder_context(folder_stack: Sequence[str], *folder_names: str) -> bool:
    targets = {normalize_text(folder_name).upper() for folder_name in folder_names}
    return any(normalize_text(folder_name).upper() in targets for folder_name in folder_stack)

def folder_stack_contains_text(folder_stack: Sequence[str], *needles: str) -> bool:
    haystack = " ".join(normalize_text(folder_name).upper() for folder_name in folder_stack)
    return any(needle.upper() in haystack for needle in needles)

def has_area_boundary_context(folder_stack: Sequence[str]) -> bool:
    """Source KMZs may store FAT boundary polygons under a plain AREA folder.
    Treat AREA like FAT AREA/COVERAGE so polygons survive conversion and route to BOUNDARY FAT.
    """
    return any(normalize_text(folder_name).upper() in {"AREA", "FAT AREA", "BOUNDARY FAT", "COVERAGE"} for folder_name in folder_stack)

def first_linestring_point(linestrings: Sequence[Sequence[tuple[float, float]]]) -> tuple[float, float] | None:
    for line in linestrings:
        if line:
            return line[0]
    return None

def has_hp_cover_folder_context(folder_stack: Sequence[str]) -> bool:
    return any("HP" in normalize_text(folder_name).upper() or "HOME" in normalize_text(folder_name).upper() for folder_name in folder_stack)

PLAIN_MR_POLE_NAME_PATTERN = re.compile(
    r"(?<![A-Z0-9])MR(?:\.[A-Z0-9-]+)+\.P\d+\b",
    re.IGNORECASE,
)


def is_plain_mr_pole_name(name: str) -> bool:
    normalized = normalize_text(name)
    return bool(
        PLAIN_MR_POLE_NAME_PATTERN.search(normalized)
        and not re.search(r"(?<![A-Z0-9])EXT\.", normalized, re.IGNORECASE)
    )


def looks_like_partner_pole_name(name: str) -> bool:
    normalized = normalize_text(name)
    if is_plain_mr_pole_name(normalized):
        return False
    return bool(
        EXT_PARTNER_POLE_PATTERN.search(normalized)
        or PARTNER_DOT_POLE_PATTERN.search(normalized)
        or EXT_PARTNER_BARE_PATTERN.search(normalized)
        or re.search(r"\bEXT[\.\s_-]*(?:EMR|MR|TLK|TLKM|LN|MTI|PLN)\b", normalized, re.IGNORECASE)
    )

def looks_like_existing_emr_pole_name(name: str) -> bool:
    normalized = normalize_text(name)
    return bool(EXISTING_EMR_BARE_PATTERN.search(normalized))


def _resolve_new_pole_folder(folder_stack: Sequence[str], source_folder: str | None) -> str:
    """Resolve NEW POLE folder from source context, preserving source hierarchy."""
    if source_folder and source_folder.startswith("NEW POLE"):
        return source_folder
    extracted = _extract_canonical_folder_from_stack(folder_stack, "NEW POLE")
    return extracted or "NEW POLE 7-4"


def _resolve_existing_pole_folder(folder_stack: Sequence[str], source_folder: str | None, pole_type: str = "EMR") -> str:
    """Resolve EXISTING POLE folder from source context. pole_type is 'EMR' or 'PARTNER'."""
    prefix = f"EXISTING POLE {pole_type}"
    if source_folder and source_folder.startswith(prefix):
        return source_folder
    extracted = _extract_canonical_folder_from_stack(folder_stack, "EXISTING POLE")
    if extracted and pole_type in extracted:
        return extracted
    return f"{prefix} 7-4"

def _telkom_existing_pole_folder(name: str, source_folder: str | None, network_type: str | None) -> str | None:
    """Correct Telkom ownership, retaining supported pole size and NEW/EXT kind."""
    if normalize_text(network_type).upper() != "AE" or not source_folder or not source_folder.startswith("EXISTING POLE "):
        return None
    text = normalize_text(name)
    match = EXT_PARTNER_POLE_PATTERN.match(text) or BARE_EXT_PARTNER_PATTERN.fullmatch(text) or PARTNER_DOT_POLE_PATTERN.fullmatch(text)
    partner = canonical_ext_partner(match.group("partner")) if match else canonical_ext_partner(text)
    if partner != "TLKM":
        return None
    target = source_folder.replace("EXISTING POLE EMR ", "EXISTING POLE PARTNER ", 1)
    return target if target in LINE_STANDARD_FOLDERS + FEEDER_STANDARD_FOLDERS else None


def _resolve_new_hh_folder(folder_stack: Sequence[str], source_folder: str | None, size: str) -> str:
    """Resolve NEW HH folder from source context, preserving source hierarchy."""
    prefix = "NEW HH"
    if source_folder and source_folder.startswith(prefix) and size in source_folder:
        return source_folder
    extracted_hh = _extract_canonical_folder_from_stack(folder_stack, "NEW HH")
    return extracted_hh or f"NEW HH {size}"

def organizer_target_path(
    placemark_ref: OrganizerPlacemarkRef,
    cable_refs: Sequence[OrganizerCableRef],
    fat_areas: Sequence[OrganizerFatAreaRef],
    fdt_refs: Sequence[OrganizerFdtRef],
    network_type: str | None = "AE",
    tway_subduct_cable_refs: dict[int, OrganizerCableRef] | None = None,
    tali_pancing_subduct_cable_refs: dict[int, OrganizerCableRef] | None = None,
    line_local_ug: bool = False,
    cluster_ae_sling: bool = False,
) -> list[str] | None:
    placemark = placemark_ref.element
    name = normalize_text(direct_child_text_by_name(placemark, "name"))
    description = direct_child_text_by_name(placemark, "description")
    folder_stack = placemark_ref.folder_stack
    source_folder = nearest_standard_folder(folder_stack)
    pole_folder_context = has_pole_folder_context(folder_stack)
    hp_cover_folder_context = has_hp_cover_folder_context(folder_stack)
    point = placemark_point_coordinate(placemark)

    # LINE-local route logic is strictly Cluster UG. Other scopes retain the
    # historical project-level T-WAY behavior.
    if contains_tway_alias(name) or any(is_tway_folder_alias(folder) for folder in folder_stack):
        if line_local_ug:
            source_line = extract_line_folder_order(folder_stack)
            if source_line:
                source_fdt = extract_fdt_order(" ".join(folder_stack)) or 1
                return [format_line_folder_name(source_line, source_fdt), "T-WAY"]
            linked_cable = (tway_subduct_cable_refs or {}).get(id(placemark))
            if linked_cable is not None:
                return [format_line_folder_name(linked_cable.line_order, linked_cable.fdt_order), "T-WAY"]
        return ["T-WAY"]

    if point is not None:
        is_legacy_fat_name = bool(LEGACY_FAT_FDT_NAME_PATTERN.search(name))
        upper_name = name.upper()
        # Folder ownership is authoritative for source slack markers. Their
        # legacy label can equal the FDT code, which must not reclassify the
        # marker as a second FDT before the canonical-folder pass below.
        if source_folder == "SLACK HANGER":
            line_order, fdt_order = infer_line_fdt_for_point(
                point, folder_stack, name, cable_refs
            )
            return [
                current_line_folder_name(folder_stack, line_order, fdt_order),
                "SLACK HANGER",
            ]
        # HH placemarks are sometimes misplaced under source FDT folder.
        # Route them as handholes first; do not classify as FDT.
        if "HH" in upper_name or "HANDHOLE" in upper_name or "PEDESTAL" in upper_name:
            line_order, fdt_order = infer_line_fdt_for_point(point, folder_stack, name, cable_refs)
            line_prefix = current_line_folder_name(folder_stack, line_order, fdt_order)
            if "EXT" in upper_name:
                if (
                    has_named_folder_context(folder_stack, "NEW HH 80X80X130")
                    or has_named_folder_context(folder_stack, "EXISTING HH 80X80X130")
                    or source_folder in {"NEW HH 80X80X130", "EXISTING HH 80X80X130"}
                    or "80X80X130" in upper_name
                ):
                    return [line_prefix, "EXISTING HH 80X80X130"]
                return [line_prefix, "EXISTING HH 40X40X60"]
            if has_named_folder_context(folder_stack, "NEW HH 80X80X130") or source_folder == "NEW HH 80X80X130":
                return [line_prefix, "NEW HH 80X80X130"]
            if "20" in upper_name:
                return [line_prefix, _resolve_new_hh_folder(folder_stack, source_folder, "20X20X20")]
            if "80" in upper_name:
                return [line_prefix, "NEW HH 80X80X130"]
            return [line_prefix, _resolve_new_hh_folder(folder_stack, source_folder, "40X40X60")]
        if not (source_folder == "FAT" and is_legacy_fat_name) and looks_like_fdt_placemark(placemark, folder_stack):
            return ["FDT"]
        if source_folder == "FDT":
            return ["FDT"]

    line_order, fdt_order = infer_line_fdt_for_point(point, folder_stack, name, cable_refs)
    line_prefix = current_line_folder_name(folder_stack, line_order, fdt_order)
    legacy_line_match = next((m for m in [re.search(r"\bLINE\s+([A-E])\b", normalize_text(x), re.IGNORECASE) for x in folder_stack] if m), None)
    legacy_fdt_match = next((m for m in [re.search(r"\bFDT\s*[-_ ]*(\d+)\b", normalize_text(x), re.IGNORECASE) for x in folder_stack] if m), None)
    if legacy_line_match:
        line_order = legacy_line_match.group(1).upper()
    if legacy_fdt_match:
        try:
            fdt_order = int(legacy_fdt_match.group(1))
        except Exception:
            pass
    line_prefix = current_line_folder_name(folder_stack, line_order, fdt_order)

    # Cluster UG route hierarchy: SUBDUCT determines its nearest Distribution
    # Cable LINE. Existing TALI PANCING stays in the same derived LINE.
    if folder_stack:
        source_folder_name = folder_stack[-1]
        canonical_match = canonical_folder_match(source_folder_name, network_type, "Cluster")
        if canonical_match:
            if point is not None and not placemark_has_line(placemark) and not placemark_polygon_coordinates(placemark):
                canonical_match = _telkom_existing_pole_folder(name, canonical_match, network_type) or canonical_match
            if canonical_match == "CABLE":
                canonical_match = "DISTRIBUTION CABLE"
            if cluster_ae_sling and canonical_match == "DISTRIBUTION CABLE" and placemark_has_line(placemark):
                cable_ref = next((item for item in cable_refs if item.element is placemark), None)
                if cable_ref is not None:
                    return [
                        format_line_folder_name(cable_ref.line_order, cable_ref.fdt_order),
                        canonical_match,
                    ]
            if line_local_ug and canonical_match.startswith("SUBDUCT ") and placemark_has_line(placemark):
                source_line = extract_line_folder_order(folder_stack)
                if source_line:
                    source_fdt = extract_fdt_order(" ".join(folder_stack)) or 1
                    return [format_line_folder_name(source_line, source_fdt), canonical_match]
                subduct_cable = nearest_cable_ref_for_linestring(placemark_linestrings(placemark), cable_refs)
                if subduct_cable is not None:
                    return [
                        format_line_folder_name(subduct_cable.line_order, subduct_cable.fdt_order),
                        canonical_match,
                    ]
                # A raw SUBDUCT with no Distribution Cable has no truthful LINE.
                # Preserve it rather than inventing the generic LINE A fallback.
                return None
            if line_local_ug and canonical_match == "TALI PANCING" and placemark_has_line(placemark):
                source_line = extract_line_folder_order(folder_stack)
                if source_line:
                    source_fdt = extract_fdt_order(" ".join(folder_stack)) or 1
                    return [format_line_folder_name(source_line, source_fdt), canonical_match]
                linked_cable = (tali_pancing_subduct_cable_refs or {}).get(id(placemark))
                if linked_cable is not None:
                    return [
                        format_line_folder_name(linked_cable.line_order, linked_cable.fdt_order), canonical_match]
                # Raw TALI PANCING with no local LINE or SUBDUCT/Cable relation
                # stays untouched rather than inheriting a synthetic LINE.
                return None
            return [line_prefix, canonical_match]

    if placemark_has_line(placemark):
        linestrings = placemark_linestrings(placemark)
        if is_boundary_cluster_folder_name(name):
            return ["BOUNDARY CLUSTER"]
        # Guard: FAT AREA / BOUNDARY FAT LineStrings must NOT be routed.
        if source_folder == "BOUNDARY FAT" and not placemark_polygon_coordinates(placemark):
            return None
        raw_source_folder = normalize_folder_name(folder_stack[-1] if folder_stack else "")
        compact_source_folder = re.sub(r"[^A-Z0-9]+", "", raw_source_folder)
        if source_folder == "SLING WIRE" or compact_source_folder in {"SLINGWIRE", "STEELWIRE"}:
            start_point = first_linestring_point(linestrings)
            ref = nearest_cable_ref(start_point, cable_refs) if start_point else nearest_cable_ref_for_linestring(linestrings, cable_refs)
            if ref:
                return [format_line_folder_name(ref.line_order, ref.fdt_order), "SLING WIRE"]
            return [line_prefix, "SLING WIRE"]
        capacity = detect_distribution_cable_capacity(name, description)
        if capacity or folder_stack_contains_text(folder_stack, "LINE CABLE", "CABLE LINE"):
            ref = next((item for item in cable_refs if item.element is placemark), None)
            line_order = ref.line_order if ref else line_order
            fdt_order = ref.fdt_order if ref else fdt_order
            line_prefix = current_line_folder_name(folder_stack, line_order, fdt_order)
            return [line_prefix, "DISTRIBUTION CABLE"]
        if folder_stack_contains_text(folder_stack, "STEEL WIRE"):
            start_point = first_linestring_point(linestrings)
            ref = nearest_cable_ref(start_point, cable_refs) if start_point else None
            if ref:
                return [format_line_folder_name(ref.line_order, ref.fdt_order), "SLING WIRE"]
            return [line_prefix, "SLING WIRE"]
        if source_folder in LINE_SCOPED_LINE_FOLDERS or re.search(r"\bSLING\b|\bSTRAND\b", name, re.IGNORECASE):
            return [line_prefix, source_folder if source_folder in LINE_SCOPED_LINE_FOLDERS else "SLING WIRE"]
        if looks_like_auto_sling_wire_line(linestrings, fdt_refs, cable_refs, network_type):
            start_point = first_linestring_point(linestrings)
            ref = nearest_cable_ref(start_point, cable_refs) if start_point else nearest_cable_ref_for_linestring(linestrings, cable_refs)
            if ref:
                return [format_line_folder_name(ref.line_order, ref.fdt_order), "SLING WIRE"]
        if source_folder in LINE_STANDARD_FOLDERS and source_folder != "FDT":
            return [line_prefix, source_folder]
        return None

    polygon = placemark_polygon_coordinates(placemark)
    if polygon:
        fat_code = find_fat_code(name)
        folder_fat_code = fat_code_from_folder_stack(folder_stack)
        raw_stack = " ".join(normalize_text(item).upper() for item in folder_stack)
        if is_boundary_cluster_folder_name(name):
            return ["BOUNDARY CLUSTER"]
        area_boundary_context = has_area_boundary_context(folder_stack)
        if folder_fat_code and ("COVERAGE" in raw_stack or "FAT AREA" in raw_stack or area_boundary_context or source_folder == "BOUNDARY FAT"):
            name_element = direct_child_by_name(placemark, "name")
            if name_element is not None:
                name_element.text = folder_fat_code
            return [line_prefix, "BOUNDARY FAT"]
        if fat_code or source_folder in ("BOUNDARY FAT", "COVERAGE") or "COVERAGE" in raw_stack or "FAT AREA" in raw_stack or area_boundary_context:
            center = polygon_centroid(polygon)
            line_order, fdt_order = infer_line_fdt_for_point(center, folder_stack, name, cable_refs)
            if not fat_code and center:
                matched_area = nearest_fat_area(center, fat_areas)
                if matched_area and matched_area.fat_code:
                    name_element = direct_child_by_name(placemark, "name")
                    if name_element is not None:
                        name_element.text = matched_area.fat_code
            return [current_line_folder_name(folder_stack, line_order, fdt_order), "BOUNDARY FAT"]
        if source_folder in LINE_STANDARD_FOLDERS and source_folder != "FDT":
            return [line_prefix, source_folder]
        return None

    if point is None:
        return None

    upper_name = name.upper()

    # HC is a pole type marker, not part of the standardized placemark label.
    # Its folder classification is authoritative even when the source point was
    # accidentally placed in another canonical folder such as HP COVER or
    # NEW POLE 7-4.
    if has_named_folder_context(folder_stack, "HC") or HC_POLE_PATTERN.search(name):
        hc_line_prefix = line_prefix
        if not fdt_order:
            matched_area = nearest_fat_area(point, fat_areas)
            if matched_area:
                hc_line_prefix = format_line_folder_name(matched_area.line_order, matched_area.fdt_order)
        return [hc_line_prefix, "NEW POLE 7-3"]

    # Source-standard rule: if placemark is already inside a valid standard folder,
    # keep it there. Only non-standard folders get routed by name/geometry.
    # Example: NEW POLE 7,3 normalizes to NEW POLE 7-3; existing NEW POLE 7-3 stays there.
    if source_folder in LINE_STANDARD_FOLDERS and source_folder != "FDT":
        if source_folder == "HP COVER":
            folder_fat_code = fat_code_from_folder_stack(folder_stack)
            if folder_fat_code:
                fat_line_prefix = current_line_folder_name(folder_stack, folder_fat_code[0], fdt_order)
                return [fat_line_prefix, "HP COVER", hp_cover_fat_folder_name(folder_fat_code)]
            hp_folder_name = child_folder_name_after_standard_folder(folder_stack, "HP COVER")
            if hp_folder_name and find_fat_code(hp_folder_name):
                return [line_prefix, "HP COVER", hp_cover_fat_folder_name(hp_folder_name)]
            fat_area = nearest_fat_area(point, fat_areas)
            if fat_area:
                return [format_line_folder_name(fat_area.line_order, fat_area.fdt_order), "HP COVER", hp_cover_fat_folder_name(fat_area.fat_code or fat_area.name)]
            return [line_prefix, "HP UNCOVER"]
        return [line_prefix, _telkom_existing_pole_folder(name, source_folder, network_type) or source_folder]

    if has_named_folder_context(folder_stack, "NEW POLE"):
        return [line_prefix, _resolve_new_pole_folder(folder_stack, source_folder)]
    fat_code = find_fat_code(name)
    fat_folder_context = "FAT" in " ".join(folder_stack).upper()
    if fat_code and source_folder != "HP COVER" and (source_folder == "FAT" or fat_folder_context or "FAT" in upper_name or looks_like_fdt_code(name)):
        return [current_line_folder_name(folder_stack, fat_code[0].upper(), fdt_order), "FAT"]
    if "SLACK" in upper_name or "HANGER" in upper_name:
        return [line_prefix, "SLACK HANGER"]
    if source_folder in {"SLACK HANGER", *NEW_POLE_STYLE_BY_FOLDER.keys()}:
        return [line_prefix, source_folder]
    if "TWEY" in upper_name or "T-WAY" in upper_name or "T-WEY" in upper_name:
        return ["T-WAY"]
    if "HH" in upper_name or "HANDHOLE" in upper_name or "PEDESTAL" in upper_name:
        if "EXT" in upper_name:
            if (
                has_named_folder_context(folder_stack, "NEW HH 80X80X130")
                or has_named_folder_context(folder_stack, "EXISTING HH 80X80X130")
                or source_folder in {"NEW HH 80X80X130", "EXISTING HH 80X80X130"}
                or "80X80X130" in upper_name
            ):
                return [line_prefix, "EXISTING HH 80X80X130"]
            return [line_prefix, "EXISTING HH 40X40X60"]
        if has_named_folder_context(folder_stack, "NEW HH 80X80X130") or source_folder == "NEW HH 80X80X130":
            return [line_prefix, "NEW HH 80X80X130"]
        if "20" in upper_name:
            return [line_prefix, _resolve_new_hh_folder(folder_stack, source_folder, "20X20X20")]
        if "80" in upper_name:
            return [line_prefix, "NEW HH 80X80X130"]
        return [line_prefix, _resolve_new_hh_folder(folder_stack, source_folder, "40X40X60")]
    bare_ext_classification = bare_ext_partner_classification(name)
    if bare_ext_classification:
        return [line_prefix, bare_ext_classification[1]]
    partner = canonical_ext_partner_from_name(name)
    if partner and partner in EXT_PARTNER_TARGET_FOLDER:
        return [line_prefix, EXT_PARTNER_TARGET_FOLDER[partner]]
    # Pole override: source folder can be wrong. Name is authoritative.
    # HC poles stay NEW POLE 7-3. Plain MR.*.Pxx without EXT is NEW POLE 7-4.
    if HC_POLE_PATTERN.search(name):
        return [line_prefix, "NEW POLE 7-3"]
    if re.search(r"\bMR\.[A-Z0-9-]+\.P\d+\b", upper_name) and "EXT" not in upper_name:
        return [line_prefix, _resolve_new_pole_folder(folder_stack, source_folder)]
    if re.search(r"\bEXT\.MR\.", upper_name):
        return [line_prefix, _resolve_existing_pole_folder(folder_stack, source_folder, "EMR")]
    if re.search(r"\bEXT\.(?:TLKM|LN|MTI)\.", upper_name) or looks_like_partner_pole_name(name):
        return [line_prefix, _resolve_existing_pole_folder(folder_stack, source_folder, "PARTNER")]
    if source_folder and source_folder.startswith("EXISTING POLE"):
        if 'ext' not in name.lower() and 'mitra' not in name.lower():
             return [line_prefix, _resolve_new_pole_folder(folder_stack, source_folder)]
        return [line_prefix, _resolve_existing_pole_folder(folder_stack, source_folder, "EMR")] # default to EMR
    if MR_EXPANDED_POLE_PATTERN.search(name):
        return [line_prefix, _resolve_new_pole_folder(folder_stack, source_folder)]
    if looks_like_existing_emr_pole_name(name):
        return [line_prefix, _resolve_existing_pole_folder(folder_stack, source_folder, "EMR")]
    if pole_folder_context:
        return [line_prefix, "NEW POLE 7-3"]

    if source_folder == "HP COVER" or hp_cover_folder_context or looks_like_hp_cover_name(name):
        folder_fat_code = fat_code_from_folder_stack(folder_stack)
        if folder_fat_code:
            # Folder context is authoritative. Same FAT code can exist under different FDTs,
            # so do not reassign line/fdt from global spatial/fat_area lookup here.
            fat_line_prefix = current_line_folder_name(folder_stack, folder_fat_code[0], fdt_order)
            return [fat_line_prefix, "HP COVER", hp_cover_fat_folder_name(folder_fat_code)]
        fat_area = nearest_fat_area(point, fat_areas)
        if fat_area:
            return [format_line_folder_name(fat_area.line_order, fat_area.fdt_order), "HP COVER", hp_cover_fat_folder_name(fat_area.fat_code or fat_area.name)]
        return [line_prefix, "HP UNCOVER"]
    return None

def target_folder_for_path(project_folder: ET.Element, namespace: str, path: Sequence[str]) -> ET.Element:
    if len(path) >= 2 and is_line_folder_name(path[0]):
        line_order = explicit_line_order(path[0]) or "A"
        fdt_order = extract_fdt_order(path[0]) or 1
        line_folder = ensure_line_folder(project_folder, namespace, line_order, fdt_order)
        return ensure_standard_target_folder(line_folder, namespace, path[1:])
    return ensure_standard_target_folder(project_folder, namespace, path)

def organize_kmz_placemarks(
    root: ET.Element,
    records: Sequence[FdtCodingRecord],
    namespace: str,
    changes: list[dict],
    project_folder_name: str,
    network_type: str | None = "AE",
    line_local_ug: bool = False,
    cluster_ae_sling: bool = False,
) -> None:
    project_folder = find_project_container(root, namespace, project_folder_name)
    placemark_refs = collect_organizer_placemarks(root, [])
    fdt_refs = collect_organizer_fdt_refs(placemark_refs, records)
    preliminary_fat_areas = collect_organizer_fat_areas(placemark_refs, [])
    cable_refs = collect_organizer_cable_refs(
        placemark_refs,
        fdt_refs,
        preliminary_fat_areas,
        prefer_parent_owner=cluster_ae_sling,
    )
    fat_areas = collect_organizer_fat_areas(placemark_refs, cable_refs)
    cable_refs = collect_organizer_cable_refs(
        placemark_refs,
        fdt_refs,
        fat_areas,
        prefer_parent_owner=cluster_ae_sling,
    )
    tway_subduct_cable_refs = (
        collect_tway_subduct_cable_refs(root, cable_refs) if line_local_ug else {}
    )
    tali_pancing_subduct_cable_refs = (
        collect_tali_pancing_subduct_cable_refs(root, cable_refs) if line_local_ug else {}
    )

    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        old_folder = " / ".join(placemark_ref.folder_stack) or "Document"
        target_path = organizer_target_path(
            placemark_ref,
            cable_refs,
            fat_areas,
            fdt_refs,
            network_type,
            tway_subduct_cable_refs,
            tali_pancing_subduct_cable_refs,
            line_local_ug,
            cluster_ae_sling,
        )
        if not target_path:
            continue
        if target_path == ["FDT"]:
            preserve_fdt_capacity_metadata(
                placemark,
                placemark_ref.folder_stack,
                namespace,
            )
        target_folder = target_folder_for_path(project_folder, namespace, target_path)
        if placemark_ref.parent is target_folder:
            continue
        placemark = placemark_ref.element
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        old_folder = " / ".join(placemark_ref.folder_stack) or "Document"
        placemark_ref.parent.remove(placemark)
        target_folder.append(placemark)
        changes.append({"name": name, "from": old_folder, "to": " / ".join(target_path)})
    preserved_folders = (set(line_standard_folders_for_network(network_type)) - AUTO_LINE_STANDARD_EXCLUDE_FOLDERS) | {"FDT"}
    # Convert FAT AREA LineStrings -> Polygons BEFORE consolidate/prune
    _convert_fat_area_lines_to_polygons(project_folder, namespace, fat_areas, cable_refs, changes, kml_root=root)
    consolidate_document_project_folders(root, namespace, project_folder, project_folder_name, changes)
    project_folder = find_project_container(root, namespace, project_folder_name)
    prune_empty_folders(root, {id(project_folder)}, preserved_folders | {"BOUNDARY CLUSTER", "FDT"})
    project_folder = find_project_container(root, namespace, project_folder_name)
    ensure_folder(project_folder, namespace, "BOUNDARY CLUSTER")
    ensure_folder(project_folder, namespace, "FDT")
    _reorder_cluster_parent_folders(project_folder, namespace, network_type)
    normalize_hp_cover_folders(project_folder, namespace, changes)
    # All HP COVER points go to named BOUNDARY FAT buckets. Do not demote
    # points to HP UNCOVER because of an arbitrary capacity threshold.
    assign_all_hp_cover_to_boundary(project_folder, namespace, fat_areas, changes)


def clone_distribution_cable_lines_to_empty_subduct_32_28(
    root: ET.Element,
    namespace: str,
    changes: list[dict],
) -> int:
    """Fill an empty LINE-local SUBDUCT 32/28 from that LINE's cable only."""
    import copy

    created = 0

    def line_placemarks(folder: ET.Element) -> list[ET.Element]:
        found: list[ET.Element] = []
        for child in list(folder):
            child_name = local_name(child.tag)
            if child_name == "Placemark" and placemark_has_line(child):
                found.append(child)
            elif child_name in {"Document", "Folder"}:
                found.extend(line_placemarks(child))
        return found

    def walk(parent: ET.Element, stack: list[str]) -> None:
        nonlocal created
        for child in list(parent):
            if local_name(child.tag) not in {"Document", "Folder"}:
                continue
            child_stack = stack
            if local_name(child.tag) == "Folder":
                child_name = normalize_text(direct_child_text_by_name(child, "name"))
                child_stack = [*stack, child_name]
                if is_line_folder_name(child_name):
                    cable_folder = direct_child_folder_by_name(child, "DISTRIBUTION CABLE")
                    subduct_folder = direct_child_folder_by_name(child, "SUBDUCT 32/28")
                    # Any source geometry means the local SUBDUCT is intentional.
                    # Never replace, augment, or resolve it through another LINE.
                    if cable_folder is not None and not (subduct_folder and descendant_placemarks(subduct_folder)):
                        cable_lines = line_placemarks(cable_folder)
                        if cable_lines:
                            target = subduct_folder or ensure_folder(child, namespace, "SUBDUCT 32/28")
                            for placemark in cable_lines:
                                target.append(copy.deepcopy(placemark))
                            created += len(cable_lines)
                            changes.append({
                                "type": "subduct_32_28_from_distribution_cable",
                                "from": " / ".join([*child_stack, "DISTRIBUTION CABLE"]),
                                "to": " / ".join([*child_stack, "SUBDUCT 32/28"]),
                                "count": len(cable_lines),
                                "reason": "SUBDUCT 32/28 empty; cloned LineStrings from same LINE Distribution Cable.",
                            })
            walk(child, child_stack)

    walk(root, [])
    return created


def clone_subduct_40_34_lines_to_tali_pancing(
    root: ET.Element,
    namespace: str,
    changes: list[dict],
) -> tuple[int, int]:
    """Pair LINE-local SUBDUCT 40/34 and TALI PANCING LineStrings in Cluster UG."""
    import copy

    tali_created = subduct_created = 0

    def source_lines(folder: ET.Element | None) -> list[ET.Element]:
        if folder is None:
            return []
        found: list[ET.Element] = []
        for child in list(folder):
            child_name = local_name(child.tag)
            if child_name == "Placemark" and placemark_has_line(child):
                found.append(child)
            elif child_name in {"Document", "Folder"}:
                child_folder = normalize_text(direct_child_text_by_name(child, "name")) if child_name == "Folder" else ""
                # A nested LINE or another SUBDUCT owns its own geometry.
                if not (is_line_folder_name(child_folder) or child_folder.upper().startswith("SUBDUCT") or child_folder.upper() == "TALI PANCING"):
                    found.extend(source_lines(child))
        return found

    def walk(parent: ET.Element, stack: list[str]) -> None:
        nonlocal tali_created, subduct_created
        for child in list(parent):
            if local_name(child.tag) not in {"Document", "Folder"}:
                continue
            child_stack = stack
            if local_name(child.tag) == "Folder":
                child_name = normalize_text(direct_child_text_by_name(child, "name"))
                child_stack = [*stack, child_name]
                if is_line_folder_name(child_name):
                    subduct = direct_child_folder_by_name(child, "SUBDUCT 40/34")
                    tali_pancing = direct_child_folder_by_name(child, "TALI PANCING")
                    subduct_lines, tali_lines = source_lines(subduct), source_lines(tali_pancing)
                    # A missing folder and an empty folder both lack LineStrings.
                    # SUBDUCT 32/28 never participates; never cross LINE boundaries.
                    if bool(subduct_lines) != bool(tali_lines):
                        from_name, to_name, lines = (
                            ("SUBDUCT 40/34", "TALI PANCING", subduct_lines) if subduct_lines
                            else ("TALI PANCING", "SUBDUCT 40/34", tali_lines)
                        )
                        target = ensure_folder(child, namespace, to_name)
                        for placemark in lines:
                            clone = copy.deepcopy(placemark)
                            ensure_direct_child(clone, "name", namespace).text = (
                                "TALI PANCING" if subduct_lines else "SUBDUCT"
                            )
                            target.append(clone)
                        if subduct_lines:
                            tali_created += len(lines)
                        else:
                            subduct_created += len(lines)
                        changes.append({
                            "type": f"{'tali_pancing_from_subduct_40_34' if subduct_lines else 'subduct_40_34_from_tali_pancing'}",
                            "from": " / ".join([*child_stack, from_name]),
                            "to": " / ".join([*child_stack, to_name]),
                            "count": len(lines),
                            "reason": "Cluster UG LINE-local peer has no LineStrings; cloned from the populated folder.",
                        })
            walk(child, child_stack)

    walk(root, [])
    return tali_created, subduct_created


def _reorder_cluster_parent_folders(project_folder: ET.Element, namespace: str, network_type: str | None = 'AE') -> int:
    """Reorder direct children of project folder: BOUNDARY CLUSTER -> FDT -> LINE folders -> others."""
    def _sort_key(child):
        name = normalize_text(direct_child_text_by_name(child, "name") or "").upper()
        if name == "BOUNDARY CLUSTER":
            return (0, name)
        if name == "FDT":
            return (1, name)
        if is_line_folder_name(name):
            match = re.fullmatch(r"LINE\s+([A-Z])(?:\s*-\s*FDT\s+(\d+))?", name, re.IGNORECASE)
            if match:
                line_letter = match.group(1).upper()
                fdt_num = int(match.group(2) or 1)
                # User-required order: per FDT, then per line.
                # Example: LINE A - FDT 1, LINE B - FDT 1, LINE A - FDT 2, LINE B - FDT 2.
                return (2, fdt_num, line_letter)
            return (2, 9999, name)
        return (3, name)
    children = list(project_folder)
    metadata = [c for c in children if local_name(c.tag) in FOLDER_METADATA_CHILDREN]
    folders = [c for c in children if local_name(c.tag) == "Folder"]
    other = [c for c in children if c not in metadata and c not in folders]
    if not folders:
        return 0
    folders.sort(key=_sort_key)
    project_folder[:] = metadata + folders + other
    return 1

def _fat_code_sort_key(name: str | None) -> tuple:
    text = normalize_text(name or "").upper()
    code = find_fat_code(text)
    if code:
        m = re.fullmatch(r"([A-Z]+)0*(\d+)", code)
        if m:
            return (0, m.group(1), int(m.group(2)), code)
        return (0, code)
    m = re.search(r"([A-Z]+)[._\-\s]*(\d+)", text)
    if m:
        return (1, m.group(1), int(m.group(2)), text)
    return (9, text)

def _sort_folder_contents_by_fat_code(folder: ET.Element) -> int:
    children = list(folder)
    metadata = [c for c in children if local_name(c.tag) in FOLDER_METADATA_CHILDREN]
    folders = [c for c in children if local_name(c.tag) == "Folder"]
    placemarks = [c for c in children if local_name(c.tag) == "Placemark"]
    other = [c for c in children if c not in metadata and c not in folders and c not in placemarks]
    if not folders and not placemarks:
        return 0
    folders.sort(key=lambda child: _fat_code_sort_key(direct_child_text_by_name(child, "name")))
    placemarks.sort(key=lambda child: _fat_code_sort_key(direct_child_text_by_name(child, "name")))
    folder[:] = metadata + folders + placemarks + other
    return 1


def reorder_cluster_line_children(project_folder: ET.Element, namespace: str, network_type: str | None = 'AE') -> int:
    """Reorder direct subfolders inside each LINE/FDT folder to match standard template order."""
    standard_order = [
        name
        for name in line_standard_folders_for_network(network_type)
        if name not in AUTO_LINE_STANDARD_EXCLUDE_FOLDERS
    ] + ['FDT']
    # HH80 folders are dynamic in Cluster UG: order them before the matching
    # HH40 group without creating empty HH80 folders on every LINE.
    if normalize_text(network_type).upper() == "UG":
        if "EXISTING HH 80X80X130" not in standard_order:
            hh40_index = standard_order.index("EXISTING HH 40X40X60")
            standard_order.insert(hh40_index, "EXISTING HH 80X80X130")
        if "NEW HH 80X80X130" not in standard_order:
            hh40_index = standard_order.index("NEW HH 40X40X60")
            standard_order.insert(hh40_index, "NEW HH 80X80X130")
    order_map = {name.upper(): idx for idx, name in enumerate(standard_order)}
    reordered = 0
    for line_folder in list(project_folder):
        if local_name(line_folder.tag) != 'Folder':
            continue
        line_name = normalize_text(direct_child_text_by_name(line_folder, 'name'))
        if not line_name or not is_line_folder_name(line_name):
            continue
        children = list(line_folder)
        metadata_children = [child for child in children if local_name(child.tag) in FOLDER_METADATA_CHILDREN]
        folder_children = [child for child in children if local_name(child.tag) == 'Folder']
        non_metadata_non_folders = [child for child in children if child not in metadata_children and child not in folder_children]
        if not folder_children:
            continue
        folder_children.sort(key=lambda child: order_map.get(normalize_text(direct_child_text_by_name(child, 'name')).upper(), 9999))
        for standard_folder in folder_children:
            standard_name = normalize_text(direct_child_text_by_name(standard_folder, 'name')).upper()
            if standard_name in {'BOUNDARY FAT', 'FAT', 'HP COVER'}:
                reordered += _sort_folder_contents_by_fat_code(standard_folder)
        line_folder[:] = metadata_children + folder_children + non_metadata_non_folders
        reordered += 1
    return reordered




def reorder_feeder_project_children(project_folder: ET.Element, namespace: str, scope_type: str | None = 'Feeder', network_type: str | None = 'AE', extra_folders: Sequence[str] | None = None) -> int:
    """Reorder direct subfolders inside the Feeder/Subfeeder project folder to match standard template order."""
    standard_order = [name for name in feeder_standard_folders_for_network(network_type, scope_type, extra_folders) if name not in FEEDER_OPTIONAL_FOLDERS]
    order_map = {name.upper(): idx for idx, name in enumerate(standard_order)}
    children = list(project_folder)
    metadata_children = [child for child in children if local_name(child.tag) in FOLDER_METADATA_CHILDREN]
    folder_children = [child for child in children if local_name(child.tag) == 'Folder']
    non_metadata_non_folders = [child for child in children if child not in metadata_children and child not in folder_children]
    if not folder_children:
        return 0
    folder_children.sort(key=lambda child: order_map.get(normalize_text(direct_child_text_by_name(child, 'name')).upper(), 9999))
    project_folder[:] = metadata_children + folder_children + non_metadata_non_folders
    return 1
def organize_feeder_placemarks(
    root: ET.Element,
    records: Sequence[FdtCodingRecord],
    namespace: str,
    changes: list[dict],
    project_folder_name: str,
    network_type: str | None = "AE",
    scope_type: str | None = "Feeder",
) -> None:
    """Organize placemarks for Feeder scope ??? flat structure, no LINE/FDT nesting."""
    project_folder = find_project_container(root, namespace, project_folder_name)
    standard_folder_set = feeder_standard_folder_set_for_scope(scope_type)
    placemark_refs = collect_organizer_placemarks(root, [])
    extra_standard_folders: list[str] = []

    for placemark_ref in placemark_refs:
        placemark = placemark_ref.element
        name = normalize_text(direct_child_text_by_name(placemark, "name"))
        upper_name = name.upper()
        folder_stack = placemark_ref.folder_stack
        old_folder = " / ".join(folder_stack) or "Document"
        source_folder = nearest_standard_folder(folder_stack)
        target_folder_name: str | None = None

        # T-WAY is project-level across all scopes and geometries.
        if contains_tway_alias(name) or any(is_tway_folder_alias(folder) for folder in folder_stack):
            target_folder_name = "T-WAY"

        # Try to match source folder to a canonical folder first
        if target_folder_name is None and folder_stack:
            source_folder_name = folder_stack[-1]
            canonical_match = canonical_folder_match(source_folder_name, network_type, scope_type)
            if canonical_match:
                target_folder_name = canonical_match
                if (normalize_scope_type(scope_type) == "Subfeeder"
                        and normalize_text(network_type).upper() == "UG"
                        and target_folder_name == "FDT" and placemark_has_point(placemark)
                        and re.search(r"(?<![A-Z0-9])(?:HH\d*|HANDHOLE|PEDESTAL)(?![A-Z0-9])", upper_name)
                        and not looks_like_fdt_placemark(placemark, folder_stack)):
                    # Route vendor HH before FDT geometry indexing; keep real
                    # FDT anchors available until the final Subfeeder cleanup.
                    existing = bool(re.search(r"(?<![A-Z0-9])(?:EXT|EXISTING)(?![A-Z0-9])", upper_name))
                    size = "80X80X130" if re.search(r"(?<![A-Z0-9])(?:HH\s*80|80X80X130)(?![A-Z0-9])", upper_name) else "40X40X60"
                    target_folder_name = f"{'EXISTING' if existing else 'NEW'} HH {size}"
                if normalize_scope_type(scope_type) == "Feeder" and target_folder_name == "FDT":
                    target_folder_name = "__PROJECT__"
                elif normalize_scope_type(scope_type) == "Feeder" and normalize_text(network_type).upper() == "UG":
                    if target_folder_name.startswith("EXISTING HH"):
                        target_folder_name = "EXISTING HH 80X80X130"
                    elif target_folder_name.startswith("NEW HH") or target_folder_name == "HANDHOLE 80X80X130":
                        target_folder_name = "NEW HH 80X80X130"

        if (target_folder_name is None and normalize_scope_type(scope_type) == "Subfeeder"
                and normalize_text(network_type).upper() == "AE"
                and placemark_has_point(placemark)):
            # A neutral wrapper cannot turn a Subfeeder Slack/HP point into a
            # pole just because its label resembles a pole identifier.
            for part in reversed(folder_stack):
                owner = canonical_folder_match(part, network_type, scope_type)
                if owner in {"SLACK HANGER", "JOINT CLOSURE", "HP COVER", "HP UNCOVER"}:
                    target_folder_name = owner
                    break

        if (placemark_has_point(placemark) and not placemark_has_line(placemark)
                and not placemark_polygon_coordinates(placemark)
                and not (normalize_text(network_type).upper() == "AE"
                         and any(is_sling_wire_folder_name(part) for part in folder_stack))):
            target_folder_name = _telkom_existing_pole_folder(name, target_folder_name or source_folder, network_type) or target_folder_name

        # Classify placemark into Feeder standard folder
        if target_folder_name is None:
            if placemark_has_line(placemark):
                capacity = detect_distribution_cable_capacity(name, direct_child_text_by_name(placemark, "description"), " ".join(folder_stack))
                if capacity or source_folder == "DISTRIBUTION CABLE" or source_folder == "CABLE":
                    target_folder_name = "CABLE"
                elif source_folder == "SLACK HANGER" or "SLACK" in upper_name or "HANGER" in upper_name:
                    target_folder_name = "SLACK HANGER"
                elif source_folder in {"SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"}:
                    target_folder_name = source_folder
                elif "BORING" in f"{' '.join(folder_stack)} {upper_name}".upper():
                    boring_context = f"{' '.join(folder_stack)} {upper_name}".upper()
                    target_folder_name = "BORING MACHINE" if "MACHINE" in boring_context or "MESIN" in boring_context else "BORING MANUAL"
            elif placemark_has_point(placemark):
                ext_partner_classification = bare_ext_partner_classification(name)
                ext_partner = canonical_ext_partner_from_name(name)
                if ext_partner_classification:
                    target_folder_name = ext_partner_classification[1]
                elif ext_partner and ext_partner in EXT_PARTNER_TARGET_FOLDER:
                    target_folder_name = EXT_PARTNER_TARGET_FOLDER[ext_partner]
                elif normalize_scope_type(scope_type) == "Feeder" and re.search(r"\bOLT\b", upper_name) and lookup_olt_catalog_from_text(name):
                    target_folder_name = "OLT"
                elif "JOINT" in upper_name or "CLOSUR" in upper_name or "JC" in upper_name or source_folder == "JOINT CLOSURE":
                    target_folder_name = "JOINT CLOSURE"
                elif source_folder == "FDT" and normalize_scope_type(scope_type) == "Feeder":
                    # Preserve an unexpected source placemark without preserving
                    # the non-standard FDT folder itself.
                    target_folder_name = "__PROJECT__"
                elif source_folder and source_folder.startswith("EXISTING POLE"):
                    target_folder_name = source_folder
                elif source_folder and source_folder.startswith("NEW POLE"):
                    target_folder_name = source_folder
                elif re.search(r"\bMR\.[A-Z0-9-]+\.P\d+\b", upper_name) or MR_EXPANDED_POLE_PATTERN.search(name):
                    target_folder_name = _resolve_new_pole_folder(folder_stack, source_folder)
                elif re.search(r"\bEXT\.MR\.", upper_name):
                    target_folder_name = _resolve_existing_pole_folder(folder_stack, source_folder, "EMR")
                elif looks_like_partner_pole_name(name):
                    target_folder_name = _resolve_existing_pole_folder(folder_stack, source_folder, "PARTNER")
                elif has_pole_folder_context(folder_stack):
                    target_folder_name = _resolve_new_pole_folder(folder_stack, source_folder)
                elif "SLACK" in upper_name or "HANGER" in upper_name:
                    target_folder_name = "SLACK HANGER"
                elif "HH" in upper_name or "HANDHOLE" in upper_name or "PEDESTAL" in upper_name:
                    if "EXT" in upper_name:
                        explicit_ug_hh80 = (normalize_scope_type(scope_type) == "Subfeeder"
                                            and normalize_text(network_type).upper() == "UG"
                                            and re.search(r"(?<![A-Z0-9])(?:HH\s*80|80X80X130)(?![A-Z0-9])", upper_name))
                        target_folder_name = "EXISTING HH 80X80X130" if normalize_scope_type(scope_type) == "Feeder" or explicit_ug_hh80 else "EXISTING HH 40X40X60"
                    elif "20" in upper_name:
                        target_folder_name = _resolve_new_hh_folder(folder_stack, source_folder, "20X20X20")
                    elif "40" in upper_name:
                        target_folder_name = _resolve_new_hh_folder(folder_stack, source_folder, "40X40X60")
                    else:
                        target_folder_name = "NEW HH 80X80X130" if normalize_scope_type(scope_type) == "Feeder" else _resolve_new_hh_folder(folder_stack, source_folder, "40X40X60")
                elif source_folder in standard_folder_set:
                    target_folder_name = source_folder

        if not target_folder_name:
            continue

        for extra_folder in feeder_extra_folders_from_source(folder_stack, target_folder_name, scope_type):
            if extra_folder not in extra_standard_folders:
                extra_standard_folders.append(extra_folder)

        # Ensure target folder exists directly under project folder
        if target_folder_name == "OLT":
            olt_match = lookup_olt_catalog_from_text(name)
            if olt_match:
                name_element = ensure_direct_child(placemark, "name", namespace)
                name_element.text = normalize_text(olt_match.get("olt_name") or name)
                name = normalize_text(name_element.text)
        target = project_folder if target_folder_name == "__PROJECT__" else ensure_folder(project_folder, namespace, target_folder_name)
        if placemark_ref.parent is target:
            continue

        old_folder = " / ".join(folder_stack) or "Document"
        placemark_ref.parent.remove(placemark)
        target.append(placemark)
        changes.append({"name": name, "from": old_folder, "to": target_folder_name})

    # Consolidate and create standard folders
    consolidate_document_project_folders(root, namespace, project_folder, project_folder_name, changes)
    prune_empty_folders(root, {id(project_folder)}, standard_folder_set)
    # Ensure only the folders defined by the selected standard template exist.
    for folder_name in feeder_standard_folders_for_network(network_type, scope_type, extra_standard_folders):
        ensure_folder(project_folder, namespace, folder_name)
    reorder_feeder_project_children(project_folder, namespace, scope_type, network_type, extra_standard_folders)

def walk_kml_for_standardization(
    element: ET.Element,
    folder_stack: list[str],
    namespace: str,
    folder_changes: list[dict],
    fdt_placemarks: list[FdtPlacemarkRef],
    source_folder_stack: list[str] | None = None,
) -> None:
    source_folder_stack = list(folder_stack) if source_folder_stack is None else source_folder_stack
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            next_source_stack = source_folder_stack
            if child_name == "Folder":
                name_element = ensure_direct_child(child, "name", namespace)
                original_name = normalize_text(name_element.text)
                canonical = (
                    "BOUNDARY CLUSTER"
                    if is_boundary_cluster_folder_name(original_name)
                    else canonical_standard_folder(original_name)
                )
                if (
                    re.search(r"\bFDT\s*[-_ ]*\d+", original_name, re.IGNORECASE)
                    and FDT_CAPACITY_LOOSE_PATTERN.search(original_name)
                ):
                    source_stack_with_folder = source_folder_stack + [original_name]
                    for folder_child in list(child):
                        if (
                            local_name(folder_child.tag) == "Placemark"
                            and placemark_has_point(folder_child)
                        ):
                            preserve_fdt_capacity_metadata(
                                folder_child,
                                source_stack_with_folder,
                                namespace,
                            )
                if canonical and canonical != original_name:
                    name_element.text = canonical
                    folder_changes.append({"from": original_name, "to": canonical})
                next_stack = folder_stack + [canonical or original_name]
                next_source_stack = source_folder_stack + [original_name]
            walk_kml_for_standardization(
                child,
                next_stack,
                namespace,
                folder_changes,
                fdt_placemarks,
                next_source_stack,
            )
            continue

        if (
            child_name == "Placemark"
            and nearest_standard_folder(folder_stack) == "FDT"
            and looks_like_fdt_placemark(child, folder_stack)
        ):
            current_name = direct_child_text_by_name(child, "name")
            fdt_placemarks.append(
                FdtPlacemarkRef(
                    element=child,
                    folder_stack=list(source_folder_stack),
                    source_order=extract_fdt_order(" ".join([*source_folder_stack, current_name])) or len(fdt_placemarks) + 1,
                    current_name=current_name,
                )
            )


def record_for_fdt_order(records: Sequence[FdtCodingRecord], order: int) -> FdtCodingRecord | None:
    """Resolve a physical FDT ordinal to its coding record.

    ``order`` is always the generated physical position (1-based). Never
    compare it with ``FdtCodingRecord.fdt_order``: that field may be a source
    spreadsheet/BOQ ID such as 156, not the physical FDT position.
    """
    if 1 <= order <= len(records):
        return records[order - 1]
    return None


def record_for_fdt_code(records: Sequence[FdtCodingRecord], fdt_code: str) -> FdtCodingRecord | None:
    """Find the coding record whose fdt_code matches the given code (case-insensitive)."""
    target = normalize_text(fdt_code).upper()
    if not target:
        return None
    for record in records:
        if normalize_text(record.fdt_code).upper() == target:
            return record
    return None


def record_for_fdt_placemark(
    records: Sequence[FdtCodingRecord],
    placemark: FdtPlacemarkRef,
) -> FdtCodingRecord | None:
    """Resolve coding by the placemark code; ordinal remains physical-only."""
    source_code = existing_fdt_code_from_name(placemark.current_name)
    by_code = record_for_fdt_code(records, source_code)
    if by_code is not None:
        return by_code
    return record_for_fdt_order(records, placemark.source_order)


def record_cluster_initial(record: FdtCodingRecord) -> str:
    cluster_initial = normalize_text(record.cluster_initial).upper()
    if cluster_initial:
        return cluster_initial
    return normalize_text(record.fdt_code).split(".", 1)[0].upper()


def record_olt_initial(record: FdtCodingRecord) -> str:
    """Get OLT initial from record (for Feeder pole naming).
    
    Prioritizes olt_name_initial (short code like 'TMN') over olt_initial (full name like 'OLT TAMAN NAROGONG').
    """
    if record.olt_name_initial:
        return normalize_text(record.olt_name_initial).upper()
    return normalize_text(record.olt_initial).upper()


def record_hub_id(record: FdtCodingRecord) -> str:
    """Get HUB ID from record (for Feeder CABLE naming)."""
    return normalize_text(record.hub_id)


def feeder_pole_prefix(record: FdtCodingRecord) -> str:
    """Build Feeder pole prefix: {OLT Initial}.{Cluster Initial}"""
    olt = record_olt_initial(record)
    cluster = record_cluster_initial(record)
    if olt and cluster:
        return f"{olt}.{cluster}"
    if cluster:
        return cluster
    return "XXX"


def subfeeder_pole_prefix(record: FdtCodingRecord) -> str:
    """Build Subfeeder pole prefix from OLT initial and FDT coding."""
    olt = record_olt_initial(record)
    # cluster_initial is the canonical coding label; preserve every dot in it.
    coding_fdt = record_cluster_initial(record)
    if not olt or olt == "XXX" or not coding_fdt or coding_fdt == "XXX":
        raise ValueError("Subfeeder pole naming membutuhkan INISIAL OLT dan CODING FDT yang valid.")
    return f"{olt}.{coding_fdt}"


def feeder_scope_tag(scope_type: str | None) -> str | None:
    normalized = normalize_scope_type(scope_type)
    if normalized == "Subfeeder":
        return None
    return "MF"


def standard_external_partner_pole_name(name: str, cluster_initial: str, fdt_id: str = "") -> str:
    def replace(match: re.Match) -> str:
        partner = normalize_text(match.group("partner")).upper()
        standard_partner = EXT_PARTNER_ALIASES.get(partner, partner)
        existing_cluster_raw = normalize_text(match.group("cluster")).upper() if match.group("cluster") else None
        pole = normalize_text(match.group("pole")).upper()
        # Numeric coding segments are identity, not disposable FDT IDs.
        cluster = existing_cluster_raw or cluster_initial
        if fdt_id and cluster != cluster_initial:
            cluster = cluster.removesuffix(f".{normalize_text(fdt_id)}")
        if cluster:
            return f"EXT.{standard_partner}.{cluster}.{pole}"
        return f"EXT.{standard_partner}.{pole}"

    return EXT_PARTNER_POLE_PATTERN.sub(replace, name)

def pole_number_seed_name(name: str, cluster_initial: str) -> str:
    seeded = MR_PLACEHOLDER_POLE_PATTERN.sub(f"MR.{cluster_initial}.\\1", name)
    seeded = EXT_MR_POLE_PATTERN.sub(f"EXT.MR.{cluster_initial}.\\1", seeded)
    return standard_external_partner_pole_name(seeded, cluster_initial)

def collect_used_project_pole_numbers(element: ET.Element, cluster_initial: str) -> dict[str, set[int]]:
    used_numbers: dict[str, set[int]] = {}
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            child_used = collect_used_project_pole_numbers(child, cluster_initial)
            for cluster, numbers in child_used.items():
                used_numbers.setdefault(cluster, set()).update(numbers)
            continue
        if child_name != "Placemark":
            continue

        name = pole_number_seed_name(normalize_text(direct_child_text_by_name(child, "name")), cluster_initial)
        for match in STANDARD_POLE_NUMBER_PATTERN.finditer(name):
            cluster = normalize_text(match.group("cluster")).upper()
            try:
                number = int(match.group("number"))
            except (TypeError, ValueError):
                continue
            if cluster and number > 0:
                used_numbers.setdefault(cluster, set()).add(number)
    return used_numbers

def next_project_pole_code(cluster: str, used_pole_numbers: dict[str, set[int]]) -> str:
    cluster = normalize_text(cluster).upper()
    numbers = used_pole_numbers.setdefault(cluster, set())
    number = 1
    while number in numbers:
        number += 1
    numbers.add(number)
    return f"P{number:03d}"


def handhole_source_code(name: str | None) -> str:
    parts = [part for part in normalize_text(name).upper().split(".") if part]
    if len(parts) < 2 or not re.fullmatch(r"(?:P\d+|HH(?:PIT)?\d+)", parts[-1], re.IGNORECASE):
        return ""
    if parts[:2] == ["EXT", "MR"]:
        start = 2
    elif parts[0] == "MR":
        start = 1
    else:
        start = 0
    code_parts = parts[start:-1]
    if not code_parts or any(not re.fullmatch(r"[A-Z0-9-]+", part) for part in code_parts):
        return ""
    return ".".join(code_parts)


def hh80_owner_for_fdt_name(fdt_name: str | None) -> str:
    """Return EXT when the serving FDT is explicitly external."""
    return "EXT" if re.match(r"^\s*EXT(?:[.\s_-]|$)", normalize_text(fdt_name), re.IGNORECASE) else "NEW"


def hh80_name_for_fdt(fdt_name: str | None, fallback_code: str | None, number: int) -> str:
    """Build the HH80 anchor identity from its serving FDT."""
    code = existing_fdt_code_from_name(fdt_name) or normalize_text(fallback_code).upper()
    code = re.sub(r"^EXT[.\s_-]+", "", code, flags=re.IGNORECASE).strip(".")
    if not code:
        code = "XXX"
    prefix = "EXT.MR" if hh80_owner_for_fdt_name(fdt_name) == "EXT" else "MR"
    return f"{prefix}.{code}.HH{max(1, int(number)):03d}"


def handhole_sequence_spec(folder_name: str | None) -> tuple[str, str, int] | None:
    folder = normalize_text(folder_name).upper()
    if folder.startswith("NEW HH"):
        owner = "NEW"
    elif folder == "HANDHOLE 80X80X130":
        owner = "NEW"
    elif folder.startswith("EXISTING HH") or folder.startswith("EXT HH"):
        owner = "EXT"
    else:
        return None
    if "20X20X20" in folder:
        return (owner, "HHPIT", 2)
    if "80X80X130" in folder:
        return (owner, "HH", 0)
    if "40X40X60" in folder:
        return (owner, "HH", 1)
    return (owner, "HH", 3)


def collect_handhole_placemarks(
    element: ET.Element,
    folder_stack: list[str],
    collected: list[dict] | None = None,
) -> list[dict]:
    if collected is None:
        collected = []
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
            collect_handhole_placemarks(child, next_stack, collected)
            continue
        if child_name != "Placemark" or not placemark_has_point(child):
            continue
        folder_name = nearest_standard_folder(folder_stack)
        sequence_spec = handhole_sequence_spec(folder_name)
        if not sequence_spec and folder_stack:
            folder_name = normalize_text(folder_stack[-1])
            sequence_spec = handhole_sequence_spec(folder_name)
        if sequence_spec:
            owner, prefix, priority = sequence_spec
            collected.append(
                {
                    "element": child,
                    "folder": folder_name,
                    "owner": owner,
                    "prefix": prefix,
                    "priority": priority,
                    "order": len(collected),
                }
            )
    return collected


def standardize_handhole_placemark_names(
    element: ET.Element,
    record: FdtCodingRecord | None,
    namespace: str,
    changes: list[dict],
    scope_type: str | None = None,
) -> None:
    entries = collect_handhole_placemarks(element, [])
    entries.sort(
        key=lambda entry: (
            str(entry["owner"]),
            int(entry["priority"]),
            int(entry["order"]),
        )
    )
    counters: dict[tuple[str, str, str], int] = {}
    default_code = (
        feeder_pole_prefix(record)
        if record and normalize_scope_type(scope_type) == "Feeder"
        else record_cluster_initial(record) if record else ""
    )

    for entry in entries:
        placemark = entry["element"]
        name_element = ensure_direct_child(placemark, "name", namespace)
        old_name = normalize_text(name_element.text)
        code = default_code or handhole_source_code(old_name)
        if not code:
            continue
        owner = str(entry["owner"])
        prefix = str(entry["prefix"])
        counter_key = (owner, code, prefix)
        counters[counter_key] = counters.get(counter_key, 0) + 1
        name_prefix = "EXT.MR" if owner == "EXT" else "MR"
        new_name = f"{name_prefix}.{code}.{prefix}{counters[counter_key]:03d}"
        if new_name == old_name:
            continue
        name_element.text = new_name
        changes.append(
            {
                "from": old_name,
                "to": new_name,
                "folder": entry["folder"],
                "source_sheet": record.sheet_name if record else "",
                "source_row": record.row_number if record else None,
                "site_name": record.site_name if record else "",
            }
        )


def _ug_direct_point_placemarks(folder: ET.Element) -> list[ET.Element]:
    return [x for x in list(folder) if local_name(x.tag) == 'Placemark' and placemark_has_point(x)]
def _ug_cable_order_key(point, cable_lines):
    best=(10**6, float('inf'), float('inf'))
    for cable_index, line in enumerate(cable_lines or []):
        progress=0.0
        for start,end in zip(line, line[1:]):
            distance=_point_to_segment_distance_meter(point, start, end)
            along=_project_distance_along_segment(point, start, end)
            candidate=(cable_index, distance, progress+along)
            if (candidate[1], candidate[0], candidate[2]) < (best[1], best[0], best[2]):
                best=candidate
            progress+=_haversine_distance_meter(start, end)
    return (best[0], best[2])
def _ug_line_distribution_lines(line_folder):
    lines=[]
    def walk(elem, active=False):
        name=normalize_text(direct_child_text_by_name(elem, 'name') or '')
        is_active=active or name == 'DISTRIBUTION CABLE'
        for child in list(elem):
            tag=local_name(child.tag)
            if tag == 'Folder':
                walk(child, is_active)
            elif is_active and tag == 'Placemark' and placemark_has_line(child):
                coords=[]
                for node in child.iter():
                    if local_name(node.tag) == 'LineString':
                        text=direct_child_text_by_name(node, 'coordinates') or ''
                        part=parse_kml_coordinates(text)
                        if len(part)>=2:
                            lines.append(part)
    walk(line_folder)
    return lines

def ensure_ug_hh80_fdt_only(root, namespace, changes, record=None, fdt_placemarks=None):
    """Ensure every UG FDT has an HH80 anchor within 1m.

    HH80 is an FDT-anchor minimum, not an exclusive-placement cleanup rule.
    The serving FDT controls HH80 ownership: ``EXT.*`` FDTs use ``EXT.MR``;
    normal FDTs use ``MR``. Source HH80 placemarks stay at their coordinates.
    """
    import copy

    fallback_code = (record_cluster_initial(record) if record else "") or "LPCCDS"
    created = 0

    def direct_points(folder):
        return [child for child in list(folder) if local_name(child.tag) == "Placemark" and placemark_has_point(child)]

    def collect_line_folders(element):
        found = []
        for child in list(element):
            if local_name(child.tag) not in {"Document", "Folder"}:
                continue
            if local_name(child.tag) == "Folder":
                name = normalize_text(direct_child_text_by_name(child, "name") or "")
                if LINE_ORDER_PATTERN.search(name):
                    found.append((name, child))
            found.extend(collect_line_folders(child))
        return found

    line_folders = collect_line_folders(root)

    def hh80_folders(line_folder):
        wanted = {"NEW HH 80X80X130", "EXISTING HH 80X80X130"}
        return [
            child
            for child in list(line_folder)
            if local_name(child.tag) == "Folder"
            and normalize_text(direct_child_text_by_name(child, "name")).upper() in wanted
        ]

    def hh80_folder(line_folder, owner):
        folder_name = "EXISTING HH 80X80X130" if owner == "EXT" else "NEW HH 80X80X130"
        return direct_child_folder_by_name(line_folder, folder_name) or ensure_folder(
            line_folder, namespace, folder_name
        )

    def nearest_line_for_fdt(fdt_point, folder_stack):
        for stack_name in reversed(folder_stack):
            wanted = normalize_text(stack_name)
            for line_name, line_folder in line_folders:
                if wanted == line_name:
                    return line_name, line_folder

        anchored = []
        for line_name, line_folder in line_folders:
            for folder in hh80_folders(line_folder):
                for handhole in direct_points(folder):
                    handhole_point = placemark_point_coordinate(handhole)
                    if handhole_point is not None:
                        distance = _haversine_distance_meter(fdt_point, handhole_point)
                        if distance <= 1.0:
                            anchored.append((distance, line_name, line_folder))
        if anchored:
            _, line_name, line_folder = min(anchored, key=lambda item: (item[0], item[1]))
            return line_name, line_folder

        cable_candidates = []
        for line_name, line_folder in line_folders:
            for cable in _ug_line_distribution_lines(line_folder):
                for start, end in zip(cable, cable[1:]):
                    cable_candidates.append((_point_to_segment_distance_meter(fdt_point, start, end), line_name, line_folder))
        if cable_candidates:
            _, line_name, line_folder = min(cable_candidates, key=lambda item: (item[0], item[1]))
            return line_name, line_folder

        return min(line_folders, key=lambda item: item[0]) if line_folders else ("", None)

    def hh_sequence(line_folder, preferred_name=None):
        if preferred_name:
            match = re.search(r"\.HH(\d+)$", normalize_text(preferred_name), re.IGNORECASE)
            if match:
                return int(match.group(1))
        used = set()
        for folder in hh80_folders(line_folder):
            for handhole in direct_points(folder):
                match = re.search(r"\.HH(\d+)$", normalize_text(direct_child_text_by_name(handhole, "name")), re.IGNORECASE)
                if match:
                    used.add(int(match.group(1)))
        number = 1
        while number in used:
            number += 1
        return number

    def apply_hh80_style(placemark, owner):
        if owner == "EXT":
            apply_standard_style(placemark, "existing_handhole", namespace, "EXISTING HH 80X80X130", {"ext": True}, changes)
        else:
            apply_standard_style(placemark, "handhole_80", namespace, "NEW HH 80X80X130", {}, changes)

    entries = list(fdt_placemarks or [])
    if not entries:
        for ref in collect_organizer_placemarks(root, []):
            if placemark_has_point(ref.element) and looks_like_fdt_placemark(ref.element, ref.folder_stack):
                entries.append(FdtPlacemarkRef(
                    element=ref.element,
                    folder_stack=list(ref.folder_stack),
                    source_order=len(entries) + 1,
                    current_name=direct_child_text_by_name(ref.element, "name"),
                ))

    for fdt_ref in entries:
        fdt_pm = fdt_ref.element
        fdt_point = placemark_point_coordinate(fdt_pm)
        if fdt_point is None:
            continue
        fdt_name = normalize_text(direct_child_text_by_name(fdt_pm, "name"))
        line_name, line_folder = nearest_line_for_fdt(fdt_point, fdt_ref.folder_stack)
        if line_folder is None:
            changes.append({
                "type": "hh80_anchor_unresolved",
                "from": fdt_name,
                "reason": "No LINE folder available for FDT HH80 anchor.",
            })
            continue

        owner = hh80_owner_for_fdt_name(fdt_name)
        target_folder = hh80_folder(line_folder, owner)
        nearby = [
            (folder, handhole)
            for folder in hh80_folders(line_folder)
            for handhole in direct_points(folder)
            if (handhole_point := placemark_point_coordinate(handhole)) is not None
            and _haversine_distance_meter(fdt_point, handhole_point) <= 1.0
        ]
        if nearby:
            source_folder, anchor = min(
                nearby,
                key=lambda item: _haversine_distance_meter(fdt_point, placemark_point_coordinate(item[1])),
            )
            if source_folder is not target_folder:
                source_name = normalize_text(direct_child_text_by_name(source_folder, "name"))
                source_folder.remove(anchor)
                target_folder.append(anchor)
                changes.append({
                    "type": "move_hh80_to_owner_folder",
                    "from": source_name,
                    "to": "EXISTING HH 80X80X130" if owner == "EXT" else "NEW HH 80X80X130",
                    "line": line_name,
                    "reason": "HH80 folder ownership follows the serving FDT.",
                })
            name_el = ensure_direct_child(anchor, "name", namespace)
            old_name = normalize_text(name_el.text)
            new_name = hh80_name_for_fdt(fdt_name, fallback_code, hh_sequence(line_folder, old_name))
            if old_name != new_name:
                name_el.text = new_name
                changes.append({
                    "type": "align_hh80_owner_to_fdt",
                    "from": old_name,
                    "to": new_name,
                    "line": line_name,
                    "reason": "HH80 at FDT anchor inherits EXT ownership from serving FDT.",
                })
            apply_hh80_style(anchor, owner)
            continue

        anchor = copy.deepcopy(fdt_pm)
        name_el = ensure_direct_child(anchor, "name", namespace)
        name_el.text = hh80_name_for_fdt(fdt_name, fallback_code, hh_sequence(line_folder))
        target_folder.append(anchor)
        apply_hh80_style(anchor, owner)
        created += 1
        changes.append({
            "type": "auto_hh80_from_fdt",
            "from": fdt_name,
            "to": name_el.text,
            "line": line_name,
            "reason": "No HH80 within 1m of FDT; auto-created at FDT anchor.",
        })

    return created

def ensure_ug_hh40_and_order_names(root, namespace, changes, record=None) -> int:
    created=0
    def walk(elem):
        nonlocal created
        if local_name(elem.tag)!='Folder':
            for child in list(elem): walk(child)
            return
        line_name=normalize_text(direct_child_text_by_name(elem,'name') or '')
        if LINE_ORDER_PATTERN.search(line_name):
            fat_folder=direct_child_folder_by_name(elem,'FAT')
            hh40_folder=direct_child_folder_by_name(elem,'NEW HH 40X40X60')
            if fat_folder is not None:
                if hh40_folder is None: hh40_folder=ensure_folder(elem,namespace,'NEW HH 40X40X60')
                existing_hh40_folder=direct_child_folder_by_name(elem,'EXISTING HH 40X40X60')
                hh40_anchor_folders=[hh40_folder]
                if existing_hh40_folder is not None:
                    hh40_anchor_folders.append(existing_hh40_folder)
                for fat in _ug_direct_point_placemarks(fat_folder):
                    fat_pt=placemark_point_coordinate(fat)
                    if fat_pt is None: continue
                    if any(
                        placemark_point_coordinate(h) is not None
                        and _haversine_distance_meter(fat_pt, placemark_point_coordinate(h)) <= 1.0
                        for folder in hh40_anchor_folders
                        for h in _ug_direct_point_placemarks(folder)
                    ):
                        continue
                    import copy
                    clone=copy.deepcopy(fat)
                    name_el=ensure_direct_child(clone,'name',namespace); name_el.text='AUTO HH40'
                    hh40_folder.append(clone); created+=1
                    changes.append({'type':'ug_auto_hh40_from_fat','from':line_name+' / FAT','to':line_name+' / NEW HH 40X40X60','reason':'No HH40 within 1.0m of FAT; auto-created beside FAT.'})
            lines=_ug_line_distribution_lines(elem)
            entries=[]
            for folder_name,suffix in (('NEW HH 40X40X60','HH'),('NEW HH 20X20X20','HHPIT')):
                folder=direct_child_folder_by_name(elem,folder_name)
                if folder is None: continue
                for pm in _ug_direct_point_placemarks(folder):
                    point=placemark_point_coordinate(pm)
                    if point is not None: entries.append((folder_name,suffix,pm,point))
            counters={'HH':0,'HHPIT':0}
            for folder_name,suffix,pm,point in sorted(entries, key=lambda x: (x[1], *_ug_cable_order_key(x[3], lines))):
                counters[suffix]+=1
                new_name='MR.'+((record_cluster_initial(record) if record else 'LPCCDS') or 'LPCCDS')+'.'+suffix+str(counters[suffix]).zfill(3)
                name_el=ensure_direct_child(pm,'name',namespace)
                old=normalize_text(name_el.text)
                if old!=new_name:
                    name_el.text=new_name
                    changes.append({'type':'ug_handhole_distribution_order','from':old,'to':new_name,'folder':folder_name,'reason':'Numbered by nearest distribution-cable progress.'})
        for child in list(elem): walk(child)
    walk(root)
    return created

def standard_bare_ext_partner_pole_name(
    name: str,
    cluster_initial: str,
    used_pole_numbers: dict[str, set[int]],
    assigned_pole_code: str | None = None,
    counter_key: str | None = None,
) -> str:
    if STANDARD_POLE_NUMBER_PATTERN.search(name):
        return name
    match = BARE_EXT_PARTNER_PATTERN.match(str(name or ""))
    if not match:
        return name
    partner = canonical_ext_partner(match.group("partner"))
    if not partner or not cluster_initial:
        return name
    pole = assigned_pole_code or next_project_pole_code(counter_key or cluster_initial, used_pole_numbers)
    return f"EXT.{partner}.{cluster_initial}.{pole}"

def standard_bare_external_partner_pole_name(
    name: str,
    cluster_initial: str,
    used_pole_numbers: dict[str, set[int]],
    assigned_pole_code: str | None = None,
    counter_key: str | None = None,
) -> str:
    if STANDARD_POLE_NUMBER_PATTERN.search(name):
        return name
    dot_pole_match = PARTNER_DOT_POLE_PATTERN.search(name)
    if dot_pole_match:
        partner = canonical_ext_partner(dot_pole_match.group("partner"))
        if partner and cluster_initial:
            pole = assigned_pole_code or next_project_pole_code(counter_key or cluster_initial, used_pole_numbers)
            return f"EXT.{partner}.{cluster_initial}.{pole}"
    updated = standard_bare_ext_partner_pole_name(name, cluster_initial, used_pole_numbers, assigned_pole_code, counter_key)
    if updated != name:
        return updated

    match = EXT_PARTNER_BARE_PATTERN.search(name)
    if not match:
        return name

    partner = canonical_ext_partner(match.group("partner"))
    existing_cluster = normalize_text(match.group("cluster")).upper()
    cluster = existing_cluster or cluster_initial
    if not partner or not cluster:
        return name

    pole = assigned_pole_code or next_project_pole_code(counter_key or cluster, used_pole_numbers)
    return f"EXT.{partner}.{cluster}.{pole}"

def standard_existing_emr_pole_name(
    name: str,
    cluster_initial: str,
    used_pole_numbers: dict[str, set[int]],
    assigned_pole_code: str | None = None,
    counter_key: str | None = None,
) -> str:
    if STANDARD_POLE_NUMBER_PATTERN.search(name):
        return name
    if not cluster_initial or not EXISTING_EMR_BARE_PATTERN.search(name):
        return name
    pole = assigned_pole_code or next_project_pole_code(counter_key or cluster_initial, used_pole_numbers)
    return f"EXT.MR.{cluster_initial}.{pole}"


def _pole_kind_for_folder(folder_name: str | None) -> str | None:
    folder = normalize_text(folder_name).upper()
    if folder.startswith("NEW POLE"):
        return "NEW"
    if folder.startswith("EXISTING POLE"):
        return "EXT"
    return None


def _pole_counter_key(record: FdtCodingRecord, kind: str, is_feeder: bool) -> str:
    # Both Cluster and Feeder use separate NEW/EXT counters. Keys are
    # {cluster_initial}:NEW and {cluster_initial}:EXT for Cluster, and
    # {OLT}.{cluster_initial}:NEW / :EXT for Feeder. NEW poles share one
    # counter across all NEW POLE size folders; EXT poles share another.
    if is_feeder:
        return f"{feeder_pole_prefix(record)}:{kind}"
    return f"{record_cluster_initial(record)}:{kind}"


def _collect_pole_placemarks(
    element: ET.Element,
    folder_stack: list[str],
    *,
    ignore_sling_owned: bool = False,
) -> list[dict]:
    poles: list[dict] = []
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                if ignore_sling_owned and is_sling_wire_folder_name(folder_name):
                    continue
                next_stack = folder_stack + [folder_name]
            poles.extend(
                _collect_pole_placemarks(
                    child,
                    next_stack,
                    ignore_sling_owned=ignore_sling_owned,
                )
            )
            continue
        if child_name != "Placemark":
            continue
        kind = _pole_kind_for_folder(nearest_standard_folder(folder_stack))
        point = placemark_point_coordinate(child)
        # Include every pole placemark (even ones whose name already matches
        # STANDARD_POLE_NUMBER_PATTERN). The path walker re-numbers them
        # along the cable path, so we must consider all of them ??? otherwise
        # poles with pre-existing valid names retain their numbers and the
        # output looks like the random tree order (e.g. P033 first instead
        # of P001).
        if kind and point:
            parent_fdt = next(
                (
                    extract_fdt_order(folder)
                    for folder in reversed(folder_stack)
                    if extract_fdt_order(folder) is not None
                ),
                None,
            )
            poles.append({
                "id": id(child),
                "element": child,
                "point": point,
                "kind": kind,
                "order": len(poles),
                "line": extract_line_folder_order(folder_stack),
                "fdt": parent_fdt,
            })
    return poles


def _seed_kind_pole_numbers(element: ET.Element, folder_stack: list[str], used_pole_numbers: dict[str, set[int]], record: FdtCodingRecord, is_feeder: bool) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
            _seed_kind_pole_numbers(child, next_stack, used_pole_numbers, record, is_feeder)
            continue
        if child_name != "Placemark":
            continue
        kind = _pole_kind_for_folder(nearest_standard_folder(folder_stack))
        if not kind:
            continue
        name = pole_number_seed_name(normalize_text(direct_child_text_by_name(child, "name")), record_cluster_initial(record))
        for match in STANDARD_POLE_NUMBER_PATTERN.finditer(name):
            try:
                number = int(match.group("number"))
            except (TypeError, ValueError):
                continue
            if number > 0:
                used_pole_numbers.setdefault(_pole_counter_key(record, kind, is_feeder), set()).add(number)


def _iter_cluster_cable_placemarks(
    element: ET.Element,
    folder_stack: list[str],
    *,
    ignore_sling_owned: bool = False,
) -> list[dict]:
    cables: list[dict] = []
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                if ignore_sling_owned and is_sling_wire_folder_name(folder_name):
                    continue
                next_stack = folder_stack + [folder_name]
            cables.extend(
                _iter_cluster_cable_placemarks(
                    child,
                    next_stack,
                    ignore_sling_owned=ignore_sling_owned,
                )
            )
            continue
        if child_name != "Placemark" or nearest_standard_folder(folder_stack) not in {"DISTRIBUTION CABLE", "CABLE"}:
            continue
        lines = placemark_linestrings(child)
        if lines:
            name = direct_child_text_by_name(child, "name")
            cables.append({
                "element": child,
                "name": name,
                "lines": lines,
                "order": len(cables),
                "line": extract_parent_line_order(folder_stack, name),
                "fdt": extract_parent_fdt_order(folder_stack),
            })
    return cables


def _iter_cluster_sling_placemarks(element: ET.Element, folder_stack: list[str]) -> list[dict]:
    """Collect SLING WIRE LineStrings; each is a branch from Distribution Cable."""
    slings: list[dict] = []
    for child in list(element):
        tag = local_name(child.tag)
        if tag in {"Document", "Folder"}:
            next_stack = folder_stack + ([normalize_text(direct_child_text_by_name(child, "name"))] if tag == "Folder" else [])
            slings.extend(_iter_cluster_sling_placemarks(child, next_stack))
            continue
        if tag != "Placemark" or nearest_standard_folder(folder_stack) != "SLING WIRE":
            continue
        lines = placemark_linestrings(child)
        if lines:
            parent_fdt = next(
                (
                    extract_fdt_order(folder)
                    for folder in reversed(folder_stack)
                    if extract_fdt_order(folder) is not None
                ),
                None,
            )
            slings.append({
                "element": child,
                "lines": lines,
                "order": len(slings),
                "line": extract_line_folder_order(folder_stack),
                "fdt": parent_fdt,
            })
    return slings


def _line_distance_and_progress(point: tuple[float, float], lines: Sequence[Sequence[tuple[float, float]]]) -> tuple[float, float]:
    """Nearest distance and along-path progress across a KML multi-LineString."""
    best_distance, best_along = float("inf"), 0.0
    running = 0.0
    for line in lines or []:
        if len(line) < 2:
            continue
        for start, end in zip(line, line[1:]):
            length = _haversine_distance_meter(start, end)
            distance = _point_to_segment_distance_meter(point, start, end)
            if distance < best_distance:
                best_distance = distance
                best_along = running + _project_distance_along_segment(point, start, end)
            running += length
    return best_distance, best_along


def _cluster_pole_order_with_sling(
    cables: Sequence[dict], poles: Sequence[dict], slings: Sequence[dict], record: FdtCodingRecord,
    used_pole_numbers: dict[str, set[int]], fdt_entries: Sequence[dict] | None = None,
    strict_parent_fdt_order: bool = False,
) -> dict[int, str]:
    """Walk trunk Distribution Cable, inserting each SLING WIRE at its physical tap point.

    A pole is assigned to a sling only when that sling is materially closer than
    the trunk. This preserves main-cable poles and prevents branch projections
    from scrambling their order.
    """
    branch_radius = 30.0
    items = []
    sling_meta = []

    def same_physical_owner(item: dict, cable: dict) -> bool:
        """Strict Cluster AE ownership is source/final parent LINE+FDT, not proximity."""
        item_fdt = item.get("fdt")
        cable_fdt = cable.get("fdt")
        if item_fdt is not None and cable_fdt is not None and int(item_fdt) != int(cable_fdt):
            return False
        item_line = normalize_text(item.get("line")).upper()
        cable_line = normalize_text(cable.get("line")).upper()
        if item_line and cable_line and item_line != cable_line:
            return False
        return True

    for sling in slings:
        lines = [list(line) for line in sling.get("lines", []) if line]
        if not lines:
            continue
        if strict_parent_fdt_order:
            best = (float("inf"), 0, 0.0, False)
            # A branch can be drawn endpoint-to-tap. Detect the physical tap from
            # both outer endpoints, then orient progress from tap toward branch end.
            for ci, cable in enumerate(cables):
                if not same_physical_owner(sling, cable):
                    continue
                for reverse_branch, endpoint in ((False, lines[0][0]), (True, lines[-1][-1])):
                    distance, along = _line_distance_and_progress(endpoint, cable.get("lines", []))
                    candidate = (distance, ci, along, reverse_branch)
                    if candidate < best:
                        best = candidate
            if best[0] <= branch_radius:
                oriented_lines = [list(reversed(line)) for line in reversed(lines)] if best[3] else lines
                sling_meta.append(({**sling, "lines": oriented_lines}, best[1], best[2]))
            continue

        # Baseline behavior outside the explicit Cluster AE final pass.
        best = (float("inf"), 0, 0.0)
        endpoints = [line[0] for line in lines] + [line[-1] for line in lines]
        for ci, cable in enumerate(cables):
            for endpoint in endpoints:
                distance, along = _line_distance_and_progress(endpoint, cable.get("lines", []))
                if distance < best[0]:
                    best = (distance, ci, along)
        if best[0] <= branch_radius:
            sling_meta.append((sling, best[1], best[2]))
    def pole_owner_matches(pole: dict, cable: dict) -> bool:
        # Physical FDT is authoritative. Storage LINE is deliberately ignored.
        return int(pole.get("fdt") or 1) == int(cable.get("fdt") or 1)

    for pole in poles:
        allowed_cable_indexes = [
            ci for ci, cable in enumerate(cables)
            if not strict_parent_fdt_order or pole_owner_matches(pole, cable)
        ]
        trunk_candidates = [
            (ci, _line_distance_and_progress(pole["point"], cables[ci].get("lines", [])))
            for ci in allowed_cable_indexes
        ]
        if trunk_candidates:
            nearest_distance = min(item[1][0] for item in trunk_candidates)
            # Only numerical distance ties prefer the earlier LINE. A broad
            # survey band can steal a unique later-LINE pole near a crossing.
            trunk_idx, (trunk_dist, trunk_along) = min(
                (item for item in trunk_candidates if item[1][0] <= nearest_distance + (1e-6 if strict_parent_fdt_order else 0.0)),
                key=lambda item: item[0],
            )
        else:
            trunk_idx, (trunk_dist, trunk_along) = -1, (float("inf"), 0.0)
        chosen = None
        for sling, cable_idx, tap_along in sling_meta:
            if strict_parent_fdt_order and not pole_owner_matches(pole, cables[cable_idx]):
                continue
            sling_dist, sling_along = _line_distance_and_progress(pole["point"], sling.get("lines", []))
            # Must truly belong to branch, not merely sit near its tap.
            if sling_dist <= branch_radius and sling_dist + 1.0 < trunk_dist:
                candidate = (sling_dist, cable_idx, tap_along, sling_along)
                if chosen is None or candidate < chosen:
                    chosen = candidate
        if chosen:
            _distance, cable_idx, tap_along, sling_along = chosen
            key = (cable_idx, tap_along, 1, sling_along, int(pole["order"]))
        elif trunk_idx >= 0 and (trunk_dist <= branch_radius or strict_parent_fdt_order):
            # Parent LINE/FDT remains authoritative even when a source pole is
            # drawn farther than the normal association radius from its own cable.
            key = (trunk_idx, trunk_along, 0, 0.0, int(pole["order"]))
        else:
            key = (999999, 0.0, 9, 0.0, int(pole["order"]))
        items.append((key, pole))
    items.sort(key=lambda item: item[0])
    assigned = {}
    if strict_parent_fdt_order:
        # Reserve the first number of each FDT/kind at its physical root,
        # never at another FDT's closer pole or at a shared-prefix storage LINE.
        seeds = {}
        for _key, pole in items:
            owner = (int(pole.get("fdt") or 1), pole["kind"])
            owner_cables = [c for c in cables if pole_owner_matches(pole, c)]
            if owner_cables:
                root = owner_cables[0].get("root")
                if root is not None:
                    candidate = (_haversine_distance_meter(pole["point"], root), pole["point"], pole["id"])
                    if owner not in seeds or candidate < seeds[owner]:
                        seeds[owner] = candidate
        items.sort(key=lambda item: (
            int(item[1].get("fdt") or 1),
            0 if seeds.get((int(item[1].get("fdt") or 1), item[1]["kind"]), (None, None, None))[2] == item[1]["id"] else 1,
            item[0][:-1], item[1]["point"],
            item[1]["element"].get("id", ""),
        ))
        for _key, pole in items:
            counter = _pole_counter_key(record, pole["kind"], False)
            assigned[pole["id"]] = next_project_pole_code(counter, used_pole_numbers)
        return assigned

    # Baseline behavior outside the explicit Cluster AE final pass.
    first_by_kind = {}
    if fdt_entries:
        for _key, pole in items:
            counter = _pole_counter_key(record, pole["kind"], False)
            distance = min(_haversine_distance_meter(pole["point"], (entry["lon"], entry["lat"])) for entry in fdt_entries)
            if counter not in first_by_kind or distance < first_by_kind[counter][0]:
                first_by_kind[counter] = (distance, pole)
    for counter, (_distance, pole) in first_by_kind.items():
        assigned[pole["id"]] = next_project_pole_code(counter, used_pole_numbers)
    for _key, pole in items:
        if pole["id"] in assigned:
            continue
        counter = _pole_counter_key(record, pole["kind"], False)
        assigned[pole["id"]] = next_project_pole_code(counter, used_pole_numbers)
    return assigned

def _project_distance_along_segment(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
) -> float:
    mid_lat = (seg_start[1] + seg_end[1]) / 2.0
    mlat = math.cos(math.radians(mid_lat))
    px = (point[0] - seg_start[0]) * 111320.0 * mlat
    py = (point[1] - seg_start[1]) * 111320.0
    ex = (seg_end[0] - seg_start[0]) * 111320.0 * mlat
    ey = (seg_end[1] - seg_start[1]) * 111320.0
    denom = ex * ex + ey * ey
    if denom <= 1e-9:
        return 0.0
    t = max(0.0, min(1.0, (px * ex + py * ey) / denom))
    return math.sqrt(ex * ex + ey * ey) * t


def _nearest_fdt_code_for_sort(cable: dict, fdt_entries: Sequence[dict]) -> int:
    """Return a sort key that buckets a cable to the FDT it physically
    connects to. Direction-independent.

    Per user's rule, cables are walked in ascending FDT order (cable_265
    before cable_268). The sort key is the *minimum* FDT order among any
    FDT close to either endpoint of the cable. So Cable_265 ??? which has
    FDT-265 at one end and FDT-266 at the other ??? sorts to 265.

    Falls back to "closest FDT" order if no FDT sits within fdt_radius_meter
    of either endpoint, then to original tree order if no FDT info exists.
    """
    if not fdt_entries:
        return int(cable.get("order", 0)) + 100000
    target = _target_fdt_for_cable(cable, fdt_entries)
    if target is not None:
        code_order = extract_fdt_order(target.get("code", ""))
        if not code_order:
            match = re.search(r"\.(\d{1,3})\b", str(target.get("code", "")))
            code_order = int(match.group(1)) if match else None
        if code_order is not None:
            return code_order
    lines = cable.get("lines") or []
    if not lines or not lines[0] or not lines[-1]:
        return int(cable.get("order", 0)) + 100000
    endpoints = [lines[0][0], lines[-1][-1]]

    fdt_radius_meter = 30.0
    in_radius_orders: list[int] = []
    closest_order: int | None = None
    closest_dist = float("inf")
    for entry in fdt_entries:
        fdt_pt = (entry["lon"], entry["lat"])
        order_value = extract_fdt_order(entry.get("code", ""))
        if not order_value:
            match = re.search(r"\.(\d{1,3})\b", str(entry.get("code", "")))
            order_value = int(match.group(1)) if match else None
        for ep in endpoints:
            d = _haversine_distance_meter(ep, fdt_pt)
            if order_value is not None and d <= fdt_radius_meter:
                in_radius_orders.append(order_value)
            if d < closest_dist:
                closest_dist = d
                if order_value is not None:
                    closest_order = order_value
    if in_radius_orders:
        return min(in_radius_orders)
    if closest_order is not None:
        return closest_order
    return int(cable.get("order", 0)) + 100000


def _target_fdt_for_cable(cable: dict, fdt_entries: Sequence[dict]) -> dict | None:
    """Return the FDT this cable physically connects to.

    A cable's target FDT is whichever FDT has the smallest distance to any
    of the cable's endpoints (start or end). Considering both endpoints
    means the result does not depend on the direction the user drew the
    linestring ??? drawn forward or reversed, the same FDT is identified.

    This is used by orientation logic so that a cable belonging to FDT-268
    is checked against FDT-268 specifically, not whichever FDT happens to
    be closest in the multi-FDT case (which can flip orientation backwards
    when a neighbouring FDT sits closer to the cable's away-end).
    """
    if not fdt_entries:
        return None

    cable_element = cable.get("element")
    element_name = direct_child_text_by_name(cable_element, "name") if cable_element is not None else ""
    cable_name = normalize_text(cable.get("name") or element_name)
    code_matches = [match.group(0).upper() for match in FDT_CODE_PATTERN.finditer(cable_name)]
    if code_matches:
        for code in code_matches:
            for entry in fdt_entries:
                if normalize_text(entry.get("code")).upper() == code:
                    return entry
        wanted_orders = {extract_fdt_order(code) for code in code_matches}
        wanted_orders.discard(None)
        if wanted_orders:
            for entry in fdt_entries:
                if extract_fdt_order(entry.get("code", "")) in wanted_orders:
                    return entry

    lines = cable.get("lines") or []
    if not lines:
        return None
    start = lines[0][0] if lines[0] else None
    end = lines[-1][-1] if lines[-1] else None
    best_fdt: dict | None = None
    best_dist = float("inf")
    for entry in fdt_entries:
        fdt_pt = (entry["lon"], entry["lat"])
        d_start = _haversine_distance_meter(start, fdt_pt) if start else float("inf")
        d_end = _haversine_distance_meter(end, fdt_pt) if end else float("inf")
        d = min(d_start, d_end)
        if d < best_dist:
            best_dist = d
            best_fdt = entry
    return best_fdt


def _walk_cables_assign_pole_numbers(
    cables: Sequence[dict],
    poles: Sequence[dict],
    record: FdtCodingRecord,
    used_pole_numbers: dict[str, set[int]],
    is_feeder: bool,
    radius_meter: float = 30.0,
    endpoint_prefer_radius_meter: float | None = None,
    cable_order_tolerance_meter: float | None = None,
    fdt_entries: Sequence[dict] | None = None,
    shared_counter_key: str | None = None,
    strict_parent_fdt_order: bool = False,
) -> dict[int, str]:
    """Assign sequential pole codes (P001, P002, ...) by walking cables in
    order. Approach:
      1. For every pole, find the SINGLE nearest cable (within radius) and
         compute its along-cable distance on that cable. Each pole therefore
         belongs to at most one cable ??? no ambiguous claims when cables
         overlap or branch.
      2. Group poles by their assigned cable, then iterate cables in the
         input order. Within each cable, poles are sorted by along-distance
         and given sequential numbers from the appropriate counter (NEW or
         EXT, depending on the pole's folder).
      3. Poles that match no cable are appended as leftovers in tree order
         using the same counter ??? keeping the sequence contiguous.
    """
    # Step 1: determine the best cable for every pole. In the strict Cluster
    # AE final pass, source/final parent LINE+FDT is authoritative: geometry may
    # order a pole only among cables owned by that exact parent bucket.
    cable_index_map: dict[int, int] = {}  # pole id -> cable index in `cables`
    along_map: dict[int, float] = {}      # pole id -> along-distance on its cable

    def strict_owner_matches(pole: dict, cable: dict) -> bool:
        if not strict_parent_fdt_order:
            return True
        pole_fdt = pole.get("fdt")
        cable_fdt = cable.get("fdt")
        if pole_fdt is not None and cable_fdt is not None and int(pole_fdt) != int(cable_fdt):
            return False
        pole_line = normalize_text(pole.get("line")).upper()
        cable_line = normalize_text(cable.get("line")).upper()
        return not (pole_line and cable_line and pole_line != cable_line)

    owner_cable_indexes: dict[int, list[int]] = {}
    for pole in poles:
        owner_cable_indexes[pole["id"]] = [
            index for index, cable in enumerate(cables) if strict_owner_matches(pole, cable)
        ]
        best_cable_idx = -1
        best_distance = float("inf")
        best_along = 0.0
        cable_candidates: list[tuple[int, float, float]] = []
        for cable_idx, cable in enumerate(cables):
            if not strict_owner_matches(pole, cable):
                continue
            running = 0.0
            min_dist_for_this_cable = float("inf")
            min_along_for_this_cable = 0.0
            for line in cable.get("lines") or []:
                if len(line) < 2:
                    continue
                for start, end in zip(line, line[1:]):
                    seg_len = _haversine_distance_meter(start, end)
                    distance = _point_to_segment_distance_meter(pole["point"], start, end)
                    if distance < min_dist_for_this_cable:
                        min_dist_for_this_cable = distance
                        min_along_for_this_cable = running + _project_distance_along_segment(pole["point"], start, end)
                    running += seg_len
            if min_dist_for_this_cable <= radius_meter:
                cable_candidates.append((cable_idx, min_dist_for_this_cable, min_along_for_this_cable))
                if min_dist_for_this_cable < best_distance:
                    best_distance = min_dist_for_this_cable
                    best_cable_idx = cable_idx
                    best_along = min_along_for_this_cable
        if cable_order_tolerance_meter is not None and cable_candidates:
            tolerated = [candidate for candidate in cable_candidates if candidate[1] <= best_distance + cable_order_tolerance_meter]
            preferred_idx, preferred_distance, preferred_along = min(tolerated, key=lambda item: item[0])
            best_cable_idx = preferred_idx
            best_distance = preferred_distance
            best_along = preferred_along
        if endpoint_prefer_radius_meter is not None:
            endpoint_candidates: list[int] = []
            for cable_idx, cable in enumerate(cables):
                if not strict_owner_matches(pole, cable):
                    continue
                for line in cable.get("lines") or []:
                    if not line:
                        continue
                    if (
                        _haversine_distance_meter(pole["point"], line[0]) <= endpoint_prefer_radius_meter
                        or _haversine_distance_meter(pole["point"], line[-1]) <= endpoint_prefer_radius_meter
                    ):
                        endpoint_candidates.append(cable_idx)
                        break
            if endpoint_candidates:
                start_candidates: list[int] = []
                end_candidates: list[int] = []
                for cable_idx in endpoint_candidates:
                    for line in cables[cable_idx].get("lines") or []:
                        if not line:
                            continue
                        if _haversine_distance_meter(pole["point"], line[0]) <= endpoint_prefer_radius_meter:
                            start_candidates.append(cable_idx)
                            break
                        if _haversine_distance_meter(pole["point"], line[-1]) <= endpoint_prefer_radius_meter:
                            end_candidates.append(cable_idx)
                            break
                preferred_idx = min(start_candidates or end_candidates or endpoint_candidates)
                best_cable_idx = preferred_idx
                preferred_lines = cables[preferred_idx].get("lines") or []
                best_along = 0.0
                if start_candidates and preferred_idx in start_candidates:
                    best_along = 0.0
                else:
                    running = 0.0
                    min_dist_for_preferred = float("inf")
                    for line in preferred_lines:
                        if len(line) < 2:
                            continue
                        for start, end in zip(line, line[1:]):
                            seg_len = _haversine_distance_meter(start, end)
                            distance = _point_to_segment_distance_meter(pole["point"], start, end)
                            if distance < min_dist_for_preferred:
                                min_dist_for_preferred = distance
                                best_along = running + _project_distance_along_segment(pole["point"], start, end)
                            running += seg_len
        if best_cable_idx >= 0:
            cable_index_map[pole["id"]] = best_cable_idx
            along_map[pole["id"]] = best_along

    # Step 2: assign codes per cable. The strict Cluster AE final pass trusts
    # physical parent-FDT cable order; other callers retain baseline seeding.
    assigned: dict[int, str] = {}
    fdt_nearest_by_kind: dict[str, int] = {}
    if fdt_entries and not is_feeder and not strict_parent_fdt_order:
        kind_poles: dict[str, list[tuple[float, int]]] = {}
        for pole in poles:
            if pole["id"] not in cable_index_map and pole["id"] not in along_map:
                continue
            counter_key = shared_counter_key or _pole_counter_key(record, pole["kind"], is_feeder)
            min_distance = min(
                _haversine_distance_meter(pole["point"], (entry["lon"], entry["lat"]))
                for entry in fdt_entries
            )
            kind_poles.setdefault(counter_key, []).append((min_distance, pole["id"]))
        for counter_key, candidates in kind_poles.items():
            candidates.sort()
            if candidates:
                fdt_nearest_by_kind[counter_key] = candidates[0][1]
    for counter_key, pole_id in fdt_nearest_by_kind.items():
        assigned[pole_id] = next_project_pole_code(counter_key, used_pole_numbers)

    # Walk cables in order and assign every remaining pole. Strict Cluster AE
    # completes each parent bucket—including poles >radius from its cable—before
    # advancing to the next FDT/LINE owner.
    strict_leftovers_done: set[int] = set()
    for cable_idx in range(len(cables)):
        cable = cables[cable_idx]
        cable_poles = [p for p in poles if cable_index_map.get(p["id"]) == cable_idx]
        cable_poles.sort(key=lambda p: (along_map[p["id"]], int(p["order"])))
        for pole in cable_poles:
            if pole["id"] in assigned:
                continue  # already assigned as FDT-nearest
            counter_key = shared_counter_key or _pole_counter_key(record, pole["kind"], is_feeder)
            assigned[pole["id"]] = next_project_pole_code(counter_key, used_pole_numbers)
        if strict_parent_fdt_order:
            owner_leftovers = [
                pole for pole in poles
                if pole["id"] not in assigned
                and pole["id"] not in strict_leftovers_done
                and strict_owner_matches(pole, cable)
            ]
            owner_leftovers.sort(key=lambda pole: int(pole["order"]))
            for pole in owner_leftovers:
                counter_key = shared_counter_key or _pole_counter_key(record, pole["kind"], is_feeder)
                assigned[pole["id"]] = next_project_pole_code(counter_key, used_pole_numbers)
                strict_leftovers_done.add(pole["id"])

    # Step 3: poles without any identifiable parent cable remain deterministic.
    leftover = [p for p in poles if p["id"] not in assigned]
    leftover.sort(key=lambda p: (
        int(p.get("fdt") or 999999),
        normalize_text(p.get("line")).upper(),
        int(p["order"]),
    ))
    for pole in leftover:
        counter_key = shared_counter_key or _pole_counter_key(record, pole["kind"], is_feeder)
        assigned[pole["id"]] = next_project_pole_code(counter_key, used_pole_numbers)

    return assigned


def _orient_subfeeder_cable_to_fdt(cable: dict, fdt_entries: Sequence[dict]) -> dict:
    """Return a shallow copy of `cable` with linestrings reversed if the
    cable's last vertex is closer to its target FDT than its first vertex.

    For Subfeeder, cables physically run from the FDT outward. If the user
    drew the linestring in the opposite direction (vertex order = away???FDT),
    along-distance projection would order poles backwards. Reversing the
    line so start = FDT-end fixes the ordering.

    Distance is measured to the cable's own target FDT (the FDT closest to
    either endpoint), not to whichever FDT happens to be globally closest.
    This avoids spurious flips when a neighbouring FDT sits closer to the
    cable's away-end than the correct FDT does to its near-end.
    """
    lines = cable.get("lines") or []
    if not lines or not fdt_entries:
        return cable

    target_fdt = _target_fdt_for_cable(cable, fdt_entries)
    if target_fdt is None:
        return cable
    target_pt = (target_fdt["lon"], target_fdt["lat"])

    first_point = lines[0][0]
    last_point = lines[-1][-1]
    start_to_fdt = _haversine_distance_meter(first_point, target_pt)
    end_to_fdt = _haversine_distance_meter(last_point, target_pt)

    # Reverse only when the end is materially closer to the target FDT than
    # the start. A small tolerance avoids flipping when both endpoints are
    # roughly equidistant (e.g. cable that loops back near the FDT).
    if end_to_fdt < start_to_fdt - 1.0:
        reversed_lines = [list(reversed(line)) for line in reversed(lines)]
        return {**cable, "lines": reversed_lines}
    return cable


def _orient_subfeeder_cable_chain(
    cables: Sequence[dict],
    fdt_entries: Sequence[dict],
    junction_radius_meter: float = 10.0,
    fdt_radius_meter: float = 30.0,
) -> list[dict]:
    """Orient each Subfeeder cable so its first vertex sits on the
    "upstream" side, applying the user's explicit endpoint rules:

      Rule 1: If an endpoint has ONLY an FDT nearby (no other cable joining
              at that point), that endpoint is the cable's START.
      Rule 2: If an endpoint has BOTH an FDT and another cable joining,
              that endpoint is the cable's END (and the other cable's
              START is at the same FDT).
      Rule 3: If an endpoint has NO FDT nearby, that endpoint is the
              cable's END.

    Concretely, for each cable we compute, at start and end:
      - is_fdt_endpoint: is there an FDT within fdt_radius_meter?
      - is_junction:     is there ANOTHER cable's endpoint within
                         junction_radius_meter?

    Decision matrix (per endpoint):
      FDT-only         ??? score +2 (preferred START)
      FDT+junction     ??? score -2 (preferred END)
      no FDT           ??? score -1 (preferred END)
      no FDT, junction ??? score  0 (neutral; chain propagation handles)

    The endpoint with the higher score becomes the START. Ties are broken
    by chain propagation in pass 2 (an oriented neighbour determines the
    direction).
    """
    if not cables or not fdt_entries:
        return list(cables)

    def points_close(a: tuple[float, float], b: tuple[float, float], radius: float) -> bool:
        return _haversine_distance_meter(a, b) <= radius

    def fdt_at_endpoint(point: tuple[float, float]) -> dict | None:
        """Return the FDT entry within fdt_radius_meter of `point`, or None."""
        best = None
        best_dist = float("inf")
        for entry in fdt_entries:
            d = _haversine_distance_meter(point, (entry["lon"], entry["lat"]))
            if d <= fdt_radius_meter and d < best_dist:
                best_dist = d
                best = entry
        return best

    def endpoint_has_fdt(point: tuple[float, float]) -> bool:
        return fdt_at_endpoint(point) is not None

    # First pass: collect every cable's start/end points so we can
    # detect junctions (another cable's endpoint near ours).
    cable_state: list[dict] = []
    for idx, cable in enumerate(cables):
        lines = cable.get("lines") or []
        if not lines or not lines[0] or not lines[-1]:
            cable_state.append({"idx": idx, "cable": cable, "oriented": True, "lines": lines})
            continue
        start_pt = lines[0][0]
        end_pt = lines[-1][-1]
        cable_state.append({
            "idx": idx,
            "cable": cable,
            "oriented": False,
            "lines": lines,
            "start": start_pt,
            "end": end_pt,
            "target_fdt": _target_fdt_for_cable(cable, fdt_entries),
            "start_fdt_entry": fdt_at_endpoint(start_pt),
            "end_fdt_entry": fdt_at_endpoint(end_pt),
        })

    def fdt_id(entry: dict | None) -> object:
        # FDTs are identified by code (preferred) or by coordinate tuple
        # if no code is provided. Used to detect FDT-mediated junctions.
        if entry is None:
            return None
        return entry.get("code") or (round(entry["lon"], 7), round(entry["lat"], 7))

    def has_junction_with_other(point: tuple[float, float], my_idx: int, my_endpoint_fdt: dict | None) -> bool:
        my_fdt_id = fdt_id(my_endpoint_fdt)
        for other in cable_state:
            if other["idx"] == my_idx or "start" not in other:
                continue
            # Direct junction: endpoints physically close.
            if points_close(point, other["start"], junction_radius_meter):
                return True
            if points_close(point, other["end"], junction_radius_meter):
                return True
            # FDT-mediated junction: another cable also has an endpoint
            # at the SAME FDT we're sitting at, even if not within the junction radius of
            # us. Two cables meeting through an FDT IS a junction.
            if my_fdt_id is not None:
                if fdt_id(other.get("start_fdt_entry")) == my_fdt_id:
                    return True
                if fdt_id(other.get("end_fdt_entry")) == my_fdt_id:
                    return True
        return False

    def endpoint_score(point: tuple[float, float], my_idx: int, my_endpoint_fdt: dict | None, target_fdt: dict | None) -> int:
        has_fdt = my_endpoint_fdt is not None
        has_junction = has_junction_with_other(point, my_idx, my_endpoint_fdt)
        if has_fdt and not has_junction:
            return 4   # Rule 1: FDT-only (no other line) -> START
        if has_fdt and fdt_id(my_endpoint_fdt) == fdt_id(target_fdt):
            return 3   # Target FDT is a fallback START hint, below FDT-only.
        if has_fdt and has_junction:
            return -2  # Rule 2: FDT + junction -> END
        if not has_fdt and not has_junction:
            return -1  # Rule 3 (terminal stub): no FDT ??? END
        return 0       # mid-chain junction with no FDT: defer to chain propagation

    # Annotate each cable with endpoint scores.
    for state in cable_state:
        if "start" not in state:
            continue
        start_fdt = state.get("start_fdt_entry")
        end_fdt = state.get("end_fdt_entry")
        state["start_score"] = endpoint_score(state["start"], state["idx"], start_fdt, state.get("target_fdt"))
        state["end_score"] = endpoint_score(state["end"], state["idx"], end_fdt, state.get("target_fdt"))

    def reverse_lines(lines):
        return [list(reversed(line)) for line in reversed(lines)]

    def orient(state: dict, fdt_at_start: bool) -> None:
        if state.get("oriented"):
            return
        state["oriented"] = True
        if not fdt_at_start:
            state["lines"] = reverse_lines(state["lines"])
            state["start"], state["end"] = state["end"], state["start"]
            state["start_score"], state["end_score"] = state["end_score"], state["start_score"]

    # Pass 1: orient by score difference. Higher-scored endpoint becomes
    # the start. If scores are equal, defer to pass 2 (chain propagation).
    for state in cable_state:
        if state.get("oriented") or "start" not in state:
            continue
        s_score = state["start_score"]
        e_score = state["end_score"]
        if s_score > e_score:
            orient(state, fdt_at_start=True)
        elif e_score > s_score:
            orient(state, fdt_at_start=False)
        # equal: leave for pass 2.

    # Pass 2: propagate orientation along chains. For an unoriented cable,
    # find an oriented neighbour and align: if our START endpoint matches
    # the neighbour's END (downstream side) ??? fdt_at_start=True (we
    # continue the path forward). If our START matches the neighbour's
    # START ??? fdt_at_start=False (we should run in the opposite direction).
    #
    # Two endpoints "match" if either:
    #   (a) they sit within junction_radius_meter of each other, OR
    #   (b) they share the same FDT (FDT-mediated junction).
    def endpoints_meet(my_pt: tuple[float, float], my_fdt: dict | None,
                       other_pt: tuple[float, float], other_fdt: dict | None) -> bool:
        if points_close(my_pt, other_pt, junction_radius_meter):
            return True
        my_id = fdt_id(my_fdt)
        if my_id is not None and my_id == fdt_id(other_fdt):
            return True
        return False

    def orientation_votes_from_neighbours(state: dict) -> set[bool]:
        votes: set[bool] = set()
        my_start_fdt = fdt_at_endpoint(state["start"])
        my_end_fdt = fdt_at_endpoint(state["end"])
        for other in cable_state:
            if not other.get("oriented") or "start" not in other:
                continue
            other_start_fdt = fdt_at_endpoint(other["start"])
            other_end_fdt = fdt_at_endpoint(other["end"])

            # If our endpoint meets an already-oriented END, that endpoint
            # becomes our START. If it meets an already-oriented START, our
            # opposite endpoint becomes START. Collect all endpoint votes
            # first so collisions do not pick an arbitrary first match.
            if endpoints_meet(state["start"], my_start_fdt, other["end"], other_end_fdt):
                votes.add(True)
            if endpoints_meet(state["end"], my_end_fdt, other["end"], other_end_fdt):
                votes.add(False)
            if endpoints_meet(state["start"], my_start_fdt, other["start"], other_start_fdt):
                votes.add(False)
            if endpoints_meet(state["end"], my_end_fdt, other["start"], other_start_fdt):
                votes.add(True)
        return votes

    progress = True
    while progress:
        progress = False
        for state in cable_state:
            if state.get("oriented") or "start" not in state:
                continue
            votes = orientation_votes_from_neighbours(state)
            if len(votes) == 1:
                orient(state, fdt_at_start=next(iter(votes)))
                progress = True

    # Pass 3: orphan cables (no FDT, no chain neighbour, scores still tied
    # at 0/0 or unreachable) keep original direction.
    for state in cable_state:
        if not state.get("oriented"):
            orient(state, fdt_at_start=True)

    # Build the oriented cables list.
    result: list[dict] = []
    for state in cable_state:
        cable = state["cable"]
        result.append({**cable, "lines": state["lines"]})
    return result


def _order_oriented_subfeeder_cables(
    cables: Sequence[dict],
    fdt_entries: Sequence[dict],
    junction_radius_meter: float = 10.0,
    fdt_radius_meter: float = 30.0,
) -> list[dict]:
    """Order already-oriented Subfeeder cables after START/END is known.

    Orientation must be resolved first; then numbering walks from the cable
    whose START sits at the smallest FDT, following END-to-START links where
    possible. This prevents P001 from landing in the middle just because the
    KML tree order or target bucket order was different.
    """
    if not cables:
        return []

    def points_close(a: tuple[float, float], b: tuple[float, float], radius: float) -> bool:
        return _haversine_distance_meter(a, b) <= radius

    def fdt_at_endpoint(point: tuple[float, float]) -> dict | None:
        best = None
        best_dist = float("inf")
        for entry in fdt_entries:
            d = _haversine_distance_meter(point, (entry["lon"], entry["lat"]))
            if d <= fdt_radius_meter and d < best_dist:
                best_dist = d
                best = entry
        return best

    def fdt_id(entry: dict | None) -> object:
        if entry is None:
            return None
        return entry.get("code") or (round(entry["lon"], 7), round(entry["lat"], 7))

    def fdt_order(entry: dict | None) -> int:
        if entry is None:
            return 999999
        code_order = extract_fdt_order(entry.get("code", ""))
        if code_order:
            return code_order
        match = re.search(r"\.(\d{1,3})\b", str(entry.get("code", "")))
        return int(match.group(1)) if match else 999999

    def endpoints_meet(a: tuple[float, float], a_fdt: dict | None, b: tuple[float, float], b_fdt: dict | None) -> bool:
        if points_close(a, b, junction_radius_meter):
            return True
        a_id = fdt_id(a_fdt)
        return a_id is not None and a_id == fdt_id(b_fdt)

    states: list[dict] = []
    for idx, cable in enumerate(cables):
        lines = cable.get("lines") or []
        if not lines or not lines[0] or not lines[-1]:
            states.append({"idx": idx, "cable": cable, "start": None, "end": None, "start_fdt": None, "end_fdt": None})
            continue
        start = lines[0][0]
        end = lines[-1][-1]
        states.append({
            "idx": idx,
            "cable": cable,
            "start": start,
            "end": end,
            "start_fdt": fdt_at_endpoint(start),
            "end_fdt": fdt_at_endpoint(end),
        })

    unvisited = set(range(len(states)))
    ordered: list[dict] = []

    def seed_key(idx: int) -> tuple[int, int, int]:
        state = states[idx]
        start_order = fdt_order(state.get("start_fdt"))
        target_order = _nearest_fdt_code_for_sort(state["cable"], fdt_entries) if fdt_entries else 999999
        return (start_order, target_order, int(state["cable"].get("order", state["idx"])))

    def next_from(current: dict) -> int | None:
        candidates = []
        for idx in unvisited:
            state = states[idx]
            if current.get("end") is None or state.get("start") is None:
                continue
            if endpoints_meet(current["end"], current.get("end_fdt"), state["start"], state.get("start_fdt")):
                candidates.append(idx)
        if not candidates:
            return None
        return min(candidates, key=seed_key)

    while unvisited:
        current_idx = min(unvisited, key=seed_key)
        while current_idx is not None and current_idx in unvisited:
            unvisited.remove(current_idx)
            current = states[current_idx]
            ordered.append(current["cable"])
            current_idx = next_from(current)

    return ordered


def _compute_feeder_pole_path_order(
    element: ET.Element,
    record: FdtCodingRecord,
    used_pole_numbers: dict[str, set[int]],
    fdt_entries: Sequence[dict] | None = None,
    is_subfeeder: bool = False,
) -> dict[int, str]:
    poles = _collect_pole_placemarks(element, [])
    cables = _iter_cluster_cable_placemarks(element, [])
    # Subfeeder cables physically start at the FDT. Orient cables so that
    # within a chain, the first vertex points toward the FDT side. Cables
    # with one endpoint near an FDT but the other free are stubs from FDT;
    # cables that connect to another cable propagate orientation from
    # whichever end belongs to the upstream FDT-anchored neighbour.
    if is_subfeeder and fdt_entries:
        cables = _orient_subfeeder_cable_chain(cables, fdt_entries)
        cables = _order_oriented_subfeeder_cables(cables, fdt_entries)
    elif fdt_entries:
        cables = sorted(cables, key=lambda cable: (_nearest_fdt_code_for_sort(cable, fdt_entries), cable["order"]))
    endpoint_prefer_radius = 10.0 if is_subfeeder else None
    cable_order_tolerance = 10.0 if is_subfeeder else None
    return _walk_cables_assign_pole_numbers(
        cables,
        poles,
        record,
        used_pole_numbers,
        True,
        endpoint_prefer_radius_meter=endpoint_prefer_radius,
        cable_order_tolerance_meter=cable_order_tolerance,
        shared_counter_key=f"{subfeeder_pole_prefix(record)}:ALL" if is_subfeeder else None,
    )


def _collect_fdt_points_by_name(
    element: ET.Element,
    *,
    ignore_sling_owned: bool = False,
) -> list[dict]:
    """Fallback FDT point finder that doesn't depend on folder names."""
    fdt_entries: list[dict] = []

    def walk(current: ET.Element) -> None:
        for child in list(current):
            child_name = local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                if (
                    ignore_sling_owned
                    and child_name == "Folder"
                    and is_sling_wire_folder_name(direct_child_text_by_name(child, "name"))
                ):
                    continue
                walk(child)
                continue
            if child_name != "Placemark":
                continue
            coord = placemark_point_coordinate(child)
            if not coord:
                continue
            name = normalize_text(direct_child_text_by_name(child, "name") or "")
            name_upper = name.upper()
            if name_upper.startswith("FDT") or FDT_CODE_PATTERN.search(name):
                code_match = FDT_CODE_PATTERN.search(name)
                code = code_match.group(0).upper() if code_match else name_upper
                fdt_entries.append({"code": code, "lon": coord[0], "lat": coord[1]})

    walk(element)
    return fdt_entries


def _cluster_distribution_walk(cables: Sequence[dict], fdt_entries: Sequence[dict]) -> list[dict]:
    """Virtual FDT-rooted A/B/C traversal; source LineStrings stay untouched.

    Connect endpoints to prior routes (including segment interiors), not to
    straight-line FDT distance: a returning branch may end nearer the FDT.
    ponytail: endpoint-connected routes only; ambiguous loops need explicit topology.
    """
    result = []
    for fdt in sorted({int(cable.get("fdt") or 1) for cable in cables}):
        parts = [
            {**cable, "lines": [list(line)]}
            for cable in cables if int(cable.get("fdt") or 1) == fdt
            for line in cable.get("lines", []) if len(line) >= 2
        ]
        if not parts:
            continue
        first_line = min(cable["line"] for cable in parts)
        endpoints = [point for cable in parts if cable["line"] == first_line
                     for point in (cable["lines"][0][0], cable["lines"][0][-1])]
        roots = [(entry["lon"], entry["lat"]) for entry in fdt_entries]
        root = min(roots, key=lambda point: (min(_haversine_distance_meter(point, ep)
                   for ep in endpoints), point)) if roots else min(endpoints)
        visited = []

        def connection(point):
            candidates = [(_haversine_distance_meter(point, root), 0.0)]
            for prior in visited:
                distance, along = _line_distance_and_progress(point, prior["lines"])
                candidates.append((distance, prior["root_progress"] + along))
            distance = min(item[0] for item in candidates)
            # Near-coincident survey endpoints are one tap. Break ties by
            # rooted progress so shared A/B prefixes start at the FDT.
            near = [item for item in candidates if item[0] <= distance + 1.0]
            return min(near, key=lambda item: (item[1], item[0]))

        for letter in sorted({cable["line"] for cable in parts}):
            pending = [cable for cable in parts if cable["line"] == letter]
            while pending:
                options = []
                for index, cable in enumerate(pending):
                    line = cable["lines"][0]
                    signature = min(tuple(line), tuple(reversed(line)))
                    for reverse, endpoint in ((False, line[0]), (True, line[-1])):
                        distance, progress = connection(endpoint)
                        options.append(((distance, progress, signature, endpoint), index, reverse))
                _key, index, reverse = min(options)
                cable = pending.pop(index)
                line = cable["lines"][0]
                if reverse:
                    line = list(reversed(line))
                distance, progress = connection(line[0])
                cable = {**cable, "lines": [line], "root": root,
                         "root_progress": progress + distance}
                visited.append(cable)
                result.append(cable)
    return result


def _compute_cluster_pole_path_order(
    element: ET.Element,
    record: FdtCodingRecord,
    used_pole_numbers: dict[str, set[int]],
    *,
    strict_parent_fdt_order: bool = False,
) -> dict[int, str]:
    poles = _collect_pole_placemarks(
        element,
        [],
        ignore_sling_owned=strict_parent_fdt_order,
    )
    cables = sorted(
        _iter_cluster_cable_placemarks(
            element,
            [],
            ignore_sling_owned=strict_parent_fdt_order,
        ),
        key=lambda cable: (cable["fdt"], cable["line"], cable["order"]),
    )
    # Orient each cable so its first vertex points toward its FDT placemark.
    # This ensures P001 starts from the FDT side regardless of how the user
    # drew the linestring direction in the KMZ.
    fdt_entries, _slack = _collect_feeder_geometry_indexes(
        element,
        ignore_sling_owned=strict_parent_fdt_order,
    )
    # Fallback: if FDT placemarks aren't in a canonical "FDT" folder yet
    # (common for Cluster scope where organize runs AFTER pole naming),
    # scan all placemarks by name pattern.
    if not fdt_entries:
        fdt_entries = _collect_fdt_points_by_name(
            element,
            ignore_sling_owned=strict_parent_fdt_order,
        )
    if fdt_entries:
        cables = _orient_cluster_cables_to_fdt(cables, fdt_entries)
        # Within each (fdt, line) group, walk the cable touching the FDT
        # first so the pole physically nearest the FDT placemark becomes
        # P001. After orientation, each cable's first vertex sits on its
        # FDT side, so ordering by that vertex's distance to the nearest
        # FDT produces the trunk-first ??? branch-last sequence the user
        # expects (instead of arbitrary KMZ tree order, which previously
        # let a near-FDT pole get a high number like P019).
        def _start_distance_to_fdt(cable: dict) -> float:
            lines = cable.get("lines") or []
            if not lines or not lines[0]:
                return float("inf")
            start_pt = lines[0][0]
            return min(
                _haversine_distance_meter(start_pt, (entry["lon"], entry["lat"]))
                for entry in fdt_entries
            )

        cables = sorted(
            cables,
            key=lambda cable: (
                cable["fdt"],
                cable["line"],
                _start_distance_to_fdt(cable),
                cable["order"],
            ),
        )
    slings = _iter_cluster_sling_placemarks(element, [])
    if strict_parent_fdt_order:
        # Storage LINE is not traversal ownership. Rebuild virtual routes from
        # the owning FDT and prior cable endpoints; never edit source geometry.
        cables = _cluster_distribution_walk(cables, fdt_entries)
        return _cluster_pole_order_with_sling(
            cables, poles, slings, record, used_pole_numbers, fdt_entries, True,
        )
    if slings:
        return _cluster_pole_order_with_sling(
            cables,
            poles,
            slings,
            record,
            used_pole_numbers,
            fdt_entries,
            strict_parent_fdt_order,
        )
    return _walk_cables_assign_pole_numbers(
        cables,
        poles,
        record,
        used_pole_numbers,
        False,
        fdt_entries=fdt_entries,
        strict_parent_fdt_order=strict_parent_fdt_order,
    )


def _orient_cluster_cables_to_fdt(
    cables: list[dict],
    fdt_entries: list[dict],
) -> list[dict]:
    """Orient each Cluster distribution cable so its first vertex sits on
    the FDT side (closer to its own FDT placemark). This makes P001 always
    start from the FDT end regardless of the user's drawing direction.

    Each cable carries a `fdt` key (FDT order from its parent folder). We
    find the FDT placemark whose code contains that order number, and
    reverse the cable's linestring if its last vertex is closer to that FDT
    than its first vertex.
    """
    result: list[dict] = []
    for cable in cables:
        lines = cable.get("lines") or []
        if not lines or not lines[0] or not lines[-1]:
            result.append(cable)
            continue

        cable_fdt_order = cable.get("fdt", 0)
        # Find FDT entries matching this cable's FDT order.
        target_fdts = [
            entry for entry in fdt_entries
            if extract_fdt_order(entry.get("code", "")) == cable_fdt_order
        ]
        if not target_fdts:
            # Fallback: use any FDT (nearest to either endpoint).
            target_fdts = fdt_entries

        if not target_fdts:
            result.append(cable)
            continue

        start_pt = lines[0][0]
        end_pt = lines[-1][-1]

        def min_dist_to_targets(point):
            return min(
                _haversine_distance_meter(point, (entry["lon"], entry["lat"]))
                for entry in target_fdts
            )

        start_to_fdt = min_dist_to_targets(start_pt)
        end_to_fdt = min_dist_to_targets(end_pt)

        if end_to_fdt < start_to_fdt - 1.0:
            # End is closer to FDT ??? reverse so start is FDT side.
            reversed_lines = [list(reversed(line)) for line in reversed(lines)]
            result.append({**cable, "lines": reversed_lines})
        else:
            result.append(cable)
    return result

def standard_project_placemark_name(
    name: str,
    record: FdtCodingRecord,
    used_pole_numbers: dict[str, set[int]],
    is_feeder: bool = False,
    assigned_pole_code: str | None = None,
    pole_kind: str | None = None,
    feeder_scope_label: str | None = None,
) -> str:
    fdt_code = normalize_text(record.fdt_code).upper()
    cluster_initial = record_cluster_initial(record)
    # For Feeder: pole prefix includes OLT initial
    pole_prefix = feeder_pole_prefix(record) if is_feeder else cluster_initial
    if not pole_prefix or (not is_feeder and not fdt_code):
        return name
    feeder_bare_prefix = f"{feeder_scope_label}.{pole_prefix}" if (is_feeder and feeder_scope_label) else pole_prefix

    normalized_name = normalize_text(name)
    pole_candidate = bool(
        pole_kind
        or assigned_pole_code
        or STANDARD_POLE_NUMBER_PATTERN.search(normalized_name)
        or MR_EXPANDED_POLE_PATTERN.search(normalized_name)
        or BARE_EXT_PARTNER_PATTERN.fullmatch(normalized_name)
        or PARTNER_DOT_POLE_PATTERN.search(normalized_name)
        or EXISTING_EMR_BARE_PATTERN.search(normalized_name)
        or HC_POLE_PATTERN.search(normalized_name)
        or normalized_name.upper() in {"MR", "NEW", "NEW POLE", "NP", "NEWP", "NEW P", "HC"}
    )

    updated = FDT_PLACEHOLDER_PREFIX_PATTERN.sub(fdt_code, name, count=1)
    # Normalize EXT.EMR.xxx ??? EXT.MR.xxx (EMR is alternate form)
    updated = re.sub(r"\bEXT\.POLE\.EMR\b", "EXT.MR", updated, flags=re.IGNORECASE)
    updated = re.sub(r"\bEXT\.EMR\.", "EXT.MR.", updated, flags=re.IGNORECASE)
    updated = MR_PLACEHOLDER_POLE_PATTERN.sub(f"MR.{feeder_bare_prefix}.\\1", updated)
    updated = EXT_MR_POLE_PATTERN.sub(f"EXT.MR.{feeder_bare_prefix}.\\1", updated)
    if pole_kind == "NEW" and not STANDARD_POLE_NUMBER_PATTERN.search(updated) and normalize_text(updated).upper() in {"MR", "NEW", "NEW POLE", "NP", "NEWP", "NEW P", "HC"}:
        counter_key = pole_prefix if is_feeder else f"{pole_prefix}:NEW"
        pole_code = assigned_pole_code or next_project_pole_code(counter_key, used_pole_numbers)
        updated = f"MR.{feeder_bare_prefix}.{pole_code}"
    if pole_candidate:
        updated = standard_external_partner_pole_name(updated, feeder_bare_prefix, record.fdt_id)
        ext_counter_key = pole_prefix if is_feeder else f"{pole_prefix}:EXT"
        updated = standard_bare_ext_partner_pole_name(updated, feeder_bare_prefix, used_pole_numbers, assigned_pole_code, ext_counter_key)
        updated = standard_bare_external_partner_pole_name(updated, feeder_bare_prefix, used_pole_numbers, assigned_pole_code, ext_counter_key)
        updated = standard_existing_emr_pole_name(updated, feeder_bare_prefix, used_pole_numbers, assigned_pole_code, ext_counter_key)

    # Folder ownership wins over the loose partner-name regex. A new pole
    # inside NEW POLE must stay MR even when EXT_PARTNER_BARE_PATTERN sees
    # the leading "MR" as a partner token (for example MR.MF.NBK.S11.P001).
    if pole_kind == "NEW":
        new_pole_code = assigned_pole_code
        if not new_pole_code:
            pole_match = re.findall(r"(?<![A-Z0-9])P(\d+)\b", normalized_name, flags=re.IGNORECASE)
            if pole_match:
                new_pole_code = f"P{int(pole_match[-1]):03d}"
        if new_pole_code:
            updated = f"MR.{feeder_bare_prefix}.{new_pole_code}"

    # For Feeder: rewrite existing standard pole names to insert OLT initial
    # AND reassign sequential pole numbers (avoid duplicates)
    if is_feeder:
        olt = record_olt_initial(record)
        if olt:
            # In Feeder, every pole in the project must use the same cluster
            # code from the coding record (e.g. "PVMJ14"). Input KMZs often
            # contain mixed cluster codes (PVMJ14, VMJ14, VMJ11, ...) which
            # must all be normalized to the canonical cluster.
            canonical_cluster = cluster_initial
            # Track NEW vs EXT pole numbers separately so the user gets:
            #   NEW POLE: P001, P002, ... starting fresh
            #   EXT POLE: P001, P002, ... independent series
            new_pole_key = f"{olt}.{canonical_cluster}:NEW"
            ext_pole_key = f"{olt}.{canonical_cluster}:EXT"

            # Match existing patterns like MR.{cluster}.Pxxx, EXT.MR.{cluster}.Pxxx, EXT.LN.{cluster}.Pxxx
            # and insert OLT initial: MR.{olt}.{cluster}.Pxxx with fresh sequential pole number
            scope_prefix = f"{feeder_scope_label}." if feeder_scope_label else ""
            def insert_olt_mr(match: re.Match) -> str:
                pole_code = assigned_pole_code or next_project_pole_code(new_pole_key, used_pole_numbers)
                return f"MR.{scope_prefix}{olt}.{canonical_cluster}.{pole_code}"

            def insert_olt_ext(match: re.Match) -> str:
                raw_partner = match.group("partner") or "MR"
                partner = EXT_PARTNER_ALIASES.get(raw_partner.upper(), raw_partner.upper())
                pole_code = assigned_pole_code or next_project_pole_code(ext_pole_key, used_pole_numbers)
                return f"EXT.{partner}.{scope_prefix}{olt}.{canonical_cluster}.{pole_code}"

            # Pattern: MR.cluster.Pxxx (not preceded by EXT.) ??? also matches
            # already-OLT-prefixed forms like MR.OLT.CLUSTER.Pxxx because the
            # canonical cluster from the record overrides whatever was there.
            updated = re.sub(
                r"(?<!EXT\.)\bMR\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)\.P(?P<number>\d+)\b",
                insert_olt_mr,
                updated,
                flags=re.IGNORECASE,
            )
            # Pattern: EXT.{partner}.cluster.Pxxx ??? same idea: replace cluster
            # with canonical, give a fresh sequential EXT pole number.
            updated = re.sub(
                r"\bEXT\.(?:POLE\.)?(?P<partner>MR|EMR|TLKM|LN|MTI|PLN)\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)\.P(?P<number>\d+)\b",
                insert_olt_ext,
                updated,
                flags=re.IGNORECASE,
            )
    else:
        # Cluster scope: when a path-order pole code is assigned, it must
        # OVERRIDE any existing P-number on already-standard names so the
        # numbering follows the cable path from the FDT (e.g. an input pole
        # named EXT.TLKM.CBS05.P054 gets renumbered to its path position).
        if assigned_pole_code:
            canonical_cluster = cluster_initial

            def cluster_renumber_mr(match: re.Match) -> str:
                return f"MR.{canonical_cluster}.{assigned_pole_code}"

            def cluster_renumber_ext(match: re.Match) -> str:
                partner = match.group(1).upper()
                return f"EXT.{partner}.{canonical_cluster}.{assigned_pole_code}"

            # EXT.{partner}.{cluster}.Pxxx  ??? renumber to path-order code.
            updated = re.sub(
                r"\bEXT\.(MR|TLKM|LN|MTI|PLN)\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)\.P(?P<number>\d+)\b",
                cluster_renumber_ext,
                updated,
                flags=re.IGNORECASE,
            )
            # MR.{cluster}.Pxxx (not preceded by EXT.) ??? renumber.
            updated = re.sub(
                r"(?<!EXT\.)\bMR\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)\.P(?P<number>\d+)\b",
                cluster_renumber_mr,
                updated,
                flags=re.IGNORECASE,
            )

    return updated


SUBFEEDER_EMR_SLACK_KEYWORD_PATTERN = re.compile(
    r"SLACK|KOILAN|COILAN|KOIL",
    re.IGNORECASE,
)
SUBFEEDER_EMR_SLACK_DEDUPE_RADIUS_METER = 5.0


def create_subfeeder_slack_hanger_from_emr_keywords(
    root: ET.Element,
    namespace: str,
    changes: list[dict],
    scope_type: str | None,
    project_folder_name: str,
) -> int:
    """Clone keyword-marked Subfeeder EMR poles when no Slack exists within 5 m."""
    if normalize_scope_type(scope_type) != "Subfeeder":
        return 0

    import copy

    project_folder = find_project_container(root, namespace, project_folder_name)
    if project_folder is None:
        return 0

    source_points: list[tuple[ET.Element, tuple[float, float], str, str]] = []
    existing_slack_points: list[tuple[float, float]] = []

    def collect(element: ET.Element, folder_stack: list[str]) -> None:
        for child in list(element):
            child_name = local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = [*folder_stack, normalize_text(direct_child_text_by_name(child, "name"))]
                collect(child, next_stack)
                continue
            if child_name != "Placemark" or not placemark_has_point(child):
                continue
            point = placemark_point_coordinate(child)
            if point is None:
                continue
            source_folder = nearest_standard_folder(folder_stack)
            if source_folder == "SLACK HANGER":
                existing_slack_points.append(point)
                continue
            if not (source_folder and source_folder.startswith("EXISTING POLE EMR")):
                continue
            original_name = normalize_text(direct_child_text_by_name(child, "name"))
            if SUBFEEDER_EMR_SLACK_KEYWORD_PATTERN.search(original_name):
                source_points.append((child, point, original_name, source_folder))

    collect(project_folder, [])
    if not source_points:
        return 0

    slack_folder = direct_child_folder_by_name(project_folder, "SLACK HANGER")
    created = 0
    for placemark, point, original_name, source_folder in source_points:
        if any(
            _haversine_distance_meter(point, existing_point)
            <= SUBFEEDER_EMR_SLACK_DEDUPE_RADIUS_METER
            for existing_point in existing_slack_points
        ):
            continue
        if slack_folder is None:
            slack_folder = ensure_folder(project_folder, namespace, "SLACK HANGER")
        cloned = copy.deepcopy(placemark)
        ensure_direct_child(cloned, "name", namespace).text = "SLACK HANGER"
        remove_inline_styles(cloned)
        ensure_style_url_child(cloned, namespace).text = STYLE_SPECS["slack_hanger"].style_url
        slack_folder.append(cloned)
        existing_slack_points.append(point)
        created += 1
        changes.append(
            {
                "type": "subfeeder_slack_hanger_from_emr_keyword",
                "from": source_folder,
                "to": "SLACK HANGER",
                "source_name": original_name,
                "distance_meter": 0.0,
                "reason": "No Slack Hanger exists within 5 m of keyword-marked EXISTING POLE EMR.",
            }
        )
    return created


def standardize_project_placemark_names(
    element: ET.Element,
    folder_stack: list[str],
    record: FdtCodingRecord | None,
    namespace: str,
    changes: list[dict],
    used_pole_numbers: dict[str, set[int]] | None = None,
    is_feeder: bool = False,
    pole_path_order: dict[int, str] | None = None,
    scope_type: str | None = None,
) -> None:
    if not record:
        return
    is_subfeeder = is_feeder and normalize_scope_type(scope_type) == "Subfeeder"
    pole_prefix = (
        subfeeder_pole_prefix(record)
        if is_subfeeder
        else feeder_pole_prefix(record) if is_feeder else record_cluster_initial(record)
    )
    if used_pole_numbers is None:
        # For both Feeder AND Cluster we now rewrite pole numbers along the
        # cable path from scratch. Seeding the counter with numbers from the
        # input KMZ would make `next_project_pole_code` skip slots and break
        # the contiguous P001, P002, ... sequence ??? so start empty.
        used_pole_numbers = {}
    if pole_path_order is None:
        if is_feeder:
            fdt_entries, _slack_entries = _collect_feeder_geometry_indexes(element)
            is_subfeeder = normalize_scope_type(scope_type) == "Subfeeder"
            pole_path_order = None
            if is_subfeeder and not fdt_entries:
                # On reconversion the FDT anchors are intentionally absent.
                # Retain an already-complete canonical sequence; raw or duplicate
                # pole names still follow the normal cable-based allocator.
                poles = _collect_pole_placemarks(element, [])
                cables = _iter_cluster_cable_placemarks(element, [])
                prefix = subfeeder_pole_prefix(record)
                existing = []
                for pole in poles:
                    label = normalize_text(direct_child_text_by_name(pole["element"], "name"))
                    match = STANDARD_POLE_NUMBER_PATTERN.fullmatch(label)
                    if (not match or match.group("cluster").upper() != prefix
                            or (pole["kind"] == "NEW" and not label.upper().startswith("MR."))
                            or (pole["kind"] != "NEW" and not label.upper().startswith("EXT."))):
                        break
                    number = int(match.group("number"))
                    if label != label.upper() or number < 1 or label.rsplit(".P", 1)[-1] != f"{number:03d}":
                        break
                    existing.append((pole["id"], number))
                else:
                    if (poles and cables and len(existing) == len(poles)
                            and sorted(n for _ident, n in existing) == list(range(1, len(poles) + 1))
                            and all("SUBFEEDER CABLE" in cable["name"].upper() for cable in cables)):
                        # FDT-less: labels identify cable direction only; rank poles by geometry.
                        # ponytail: ambiguous/unnumbered cables use the existing allocator.
                        def cable_order(cable):
                            match = re.search(r" - [A-Z0-9.]+-(\d+)\.\d+ - SUBFEEDER CABLE\b", cable["name"], re.I)
                            return int(match.group(1)) if match else None

                        orders = [cable_order(cable) for cable in cables]
                        if (all(order is not None for order in orders)
                                and len(set(orders)) == len(cables)):
                            ordered = [cable for _, cable in sorted(zip(orders, cables), key=lambda pair: pair[0])]
                            named = dict(existing)
                            oriented = []
                            for cable in ordered:
                                on_cable = sorted(
                                    (_line_distance_and_progress(pole["point"], cable["lines"])[1], named[pole["id"]])
                                    for pole in poles
                                    if _line_distance_and_progress(pole["point"], cable["lines"])[0] <= 1.0
                                )
                                if len(on_cable) < 2:
                                    break
                                rising = sum(a[1] < b[1] for a, b in zip(on_cable, on_cable[1:]))
                                falling = len(on_cable) - 1 - rising
                                if rising == falling:
                                    break  # ambiguous path: never preserve labels on a tie
                                if falling > rising:
                                    cable = {**cable, "lines": [list(reversed(line)) for line in reversed(cable["lines"])]}
                                oriented.append(cable)
                            if len(oriented) == len(cables):
                                geometry_order = _walk_cables_assign_pole_numbers(
                                    oriented, poles, record, {}, True,
                                    endpoint_prefer_radius_meter=10.0,
                                    cable_order_tolerance_meter=10.0,
                                    shared_counter_key=f"{prefix}:ALL",
                                )
                                if len(geometry_order) == len(poles):
                                    canonical = {ident: f"P{number:03d}" for ident, number in existing}
                                    pole_path_order = canonical if geometry_order == canonical else geometry_order
            if pole_path_order is None:
                pole_path_order = _compute_feeder_pole_path_order(
                    element,
                    record,
                    used_pole_numbers,
                    fdt_entries,
                    is_subfeeder=is_subfeeder,
                )
        else:
            # Cluster scope: when distribution cables exist, compute
            # path-order numbering so poles are numbered P001, P002, ...
            # along each cable from the FDT side, overriding any
            # pre-existing P-number on already-standard names. The walk uses
            # the (empty) `used_pole_numbers` counter so the sequence stays
            # contiguous.
            #
            # When there are NO cables, there is no path to follow ??? fall
            # back to the legacy behaviour: preserve existing standard pole
            # numbers and only assign the next available number to bare
            # partner poles. This requires seeding the counter from the
            # numbers already present in the KMZ.
            cluster_cables = _iter_cluster_cable_placemarks(element, [])
            if cluster_cables:
                pole_path_order = _compute_cluster_pole_path_order(
                    element,
                    record,
                    used_pole_numbers,
                )
            else:
                _seed_kind_pole_numbers(element, [], used_pole_numbers, record, is_feeder)
                pole_path_order = {}

    scope_tag = feeder_scope_tag(scope_type) if is_feeder else None
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            standardize_project_placemark_names(child, next_stack, record, namespace, changes, used_pole_numbers, is_feeder, pole_path_order, scope_type)
            continue

        if child_name != "Placemark":
            continue

        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        pole_folder = nearest_standard_folder(folder_stack)
        pole_kind = _pole_kind_for_folder(pole_folder)
        assigned_pole_code = pole_path_order.get(id(child))
        new_name = standard_project_placemark_name(old_name, record, used_pole_numbers, is_feeder, assigned_pole_code, pole_kind, scope_tag)
        force_subfeeder_emr_name = (
            normalize_scope_type(scope_type) == "Subfeeder"
            and placemark_has_point(child)
            and bool(pole_folder and pole_folder.startswith("EXISTING POLE EMR"))
            and bool(assigned_pole_code)
        )
        if force_subfeeder_emr_name:
            new_name = f"EXT.MR.{pole_prefix}.{assigned_pole_code}"
        if new_name != old_name:
            name_element.text = new_name
            changes.append(
                {
                    "from": old_name,
                    "to": new_name,
                    "folder": pole_folder or (folder_stack[-1] if folder_stack else ""),
                    "source_sheet": record.sheet_name,
                    "source_row": record.row_number,
                    "site_name": record.site_name,
                }
            )


def _rehome_cluster_poles_to_physical_lines(
    element: ET.Element,
    namespace: str,
    changes: list[dict],
) -> int:
    """Store Cluster AE poles under their physical Distribution Cable/SLING LINE."""
    cables = _iter_cluster_cable_placemarks(element, [], ignore_sling_owned=True)
    slings = _iter_cluster_sling_placemarks(element, [])
    if not cables and not slings:
        return 0

    poles = []

    def collect(current, stack, owner):
        for child in list(current):
            tag = local_name(child.tag)
            if tag in {"Document", "Folder"}:
                next_stack = stack
                next_owner = owner
                if tag == "Folder":
                    name = normalize_text(direct_child_text_by_name(child, "name"))
                    next_stack = [*stack, name]
                    if is_sling_wire_folder_name(name):
                        continue
                    if is_line_folder_name(name):
                        next_owner = (child, current, explicit_line_order(name) or "A", extract_fdt_order(name) or 1)
                collect(child, next_stack, next_owner)
                continue
            if tag != "Placemark" or owner is None:
                continue
            pole_folder = nearest_standard_folder(stack)
            point = placemark_point_coordinate(child)
            if _pole_kind_for_folder(pole_folder) and point:
                poles.append((current, child, pole_folder, owner, point))

    collect(element, [], None)
    moved = 0
    for source, placemark, pole_folder, (_line, project, old_line, fdt), point in poles:
        trunk_candidates = []
        for cable in cables:
            if int(cable.get("fdt") or 1) != int(fdt):
                continue
            distance, _along = _line_distance_and_progress(point, cable.get("lines", []))
            trunk_candidates.append((distance, normalize_text(cable.get("line")).upper(), int(cable.get("order", 0))))
        trunk_distance, target_line, _order = min(trunk_candidates, default=(float("inf"), "", 0))

        branch_candidates = []
        for sling in slings:
            if int(sling.get("fdt") or 1) != int(fdt):
                continue
            distance, _along = _line_distance_and_progress(point, sling.get("lines", []))
            branch_candidates.append((distance, normalize_text(sling.get("line")).upper(), int(sling.get("order", 0))))
        branch_distance, branch_line, _branch_order = min(branch_candidates, default=(float("inf"), "", 0))
        if branch_line and branch_distance <= 30.0 and branch_distance + 1.0 < trunk_distance:
            target_line = branch_line

        if not target_line or target_line == old_line:
            continue
        target = ensure_standard_target_folder(
            ensure_line_folder(project, namespace, target_line, fdt),
            namespace,
            [pole_folder],
        )
        source.remove(placemark)
        target.append(placemark)
        moved += 1
        changes.append({
            "type": "cluster_ae_pole_physical_line",
            "name": normalize_text(direct_child_text_by_name(placemark, "name")),
            "from": format_line_folder_name(old_line, fdt),
            "to": format_line_folder_name(target_line, fdt),
            "folder": pole_folder,
            "reason": "Pole disimpan pada LINE Distribution Cable atau SLING fisik terdekat.",
        })
    return moved


def standardize_cluster_new_poles_after_organization(
    element: ET.Element,
    record: FdtCodingRecord | None,
    namespace: str,
    changes: list[dict],
    *,
    strict_parent_fdt_order: bool = False,
) -> None:
    """Give every organized Cluster NEW pole one cable-path sequence.

    Cluster source files commonly keep ordinary poles directly under a raw
    ``POLE`` folder and HC poles under ``POLE/HC``.  The first naming pass
    runs before organization, so those two groups cannot yet participate in
    the same path walk.  Once organization has placed them in NEW POLE 7-4
    and NEW POLE 7-3, recompute one shared NEW counter and replace the whole
    label with ``MR.<cluster>.Pxxx``.  The HC marker remains represented by
    its 7-3 folder/style and is intentionally omitted from the label.
    """
    if not record or not _iter_cluster_cable_placemarks(element, []):
        return

    cluster_initial = record_cluster_initial(record)
    if not cluster_initial:
        return

    pole_path_order = _compute_cluster_pole_path_order(
        element,
        record,
        {},
        strict_parent_fdt_order=strict_parent_fdt_order,
    )
    if not pole_path_order:
        return

    def walk(current: ET.Element, folder_stack: list[str]) -> None:
        for child in list(current):
            child_name = local_name(child.tag)
            if child_name in {"Document", "Folder"}:
                next_stack = folder_stack
                if child_name == "Folder":
                    next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
                walk(child, next_stack)
                continue
            if child_name != "Placemark":
                continue

            assigned_pole_code = pole_path_order.get(id(child))
            pole_folder = nearest_standard_folder(folder_stack)
            pole_kind = _pole_kind_for_folder(pole_folder)
            if not assigned_pole_code or not pole_kind or (pole_kind != "NEW" and not strict_parent_fdt_order):
                continue

            name_element = ensure_direct_child(child, "name", namespace)
            old_name = normalize_text(name_element.text)
            new_name = (f"MR.{cluster_initial}.{assigned_pole_code}" if pole_kind == "NEW"
                        else standard_project_placemark_name(old_name, record, {}, False, assigned_pole_code, pole_kind))
            if new_name == old_name:
                continue
            name_element.text = new_name
            changes.append(
                {
                    "from": old_name,
                    "to": new_name,
                    "folder": pole_folder or (folder_stack[-1] if folder_stack else ""),
                    "source_sheet": record.sheet_name,
                    "source_row": record.row_number,
                    "site_name": record.site_name,
                }
            )

    walk(element, [])


def standardize_slack_hanger_placemark_names(
    element: ET.Element,
    folder_stack: list[str],
    namespace: str,
    changes: list[dict],
) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            standardize_slack_hanger_placemark_names(child, next_stack, namespace, changes)
            continue

        if child_name != "Placemark" or nearest_standard_folder(folder_stack) != "SLACK HANGER":
            continue

        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        if old_name == "SLACK HANGER":
            continue
        is_ext = re.search(r"\bEXT(?:\s|\.)", old_name, re.IGNORECASE) or looks_like_partner_pole_name(old_name) or looks_like_existing_emr_pole_name(old_name)
        if is_ext:
            name_element.text = "EXT SLACK HANGER"
            changes.append({"from": old_name, "to": "EXT SLACK HANGER", "folder": "SLACK HANGER"})
        else:
            name_element.text = "SLACK HANGER"
            changes.append({"from": old_name, "to": "SLACK HANGER", "folder": "SLACK HANGER"})


def standardize_sling_wire_placemark_names(
    element: ET.Element,
    folder_stack: list[str],
    namespace: str,
    changes: list[dict],
) -> int:
    """Name only LineString placemarks under SLING WIRE folders literally."""
    renamed = 0
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            renamed += standardize_sling_wire_placemark_names(child, next_stack, namespace, changes)
            continue

        if (
            child_name != "Placemark"
            or nearest_standard_folder(folder_stack) != "SLING WIRE"
            or not placemark_has_line(child)
        ):
            continue

        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        if old_name == "SLING WIRE":
            continue
        name_element.text = "SLING WIRE"
        changes.append({"from": old_name, "to": "SLING WIRE", "folder": "SLING WIRE"})
        renamed += 1
    return renamed


def standardize_subduct_placemark_names(
    element: ET.Element,
    folder_stack: list[str],
    namespace: str,
    changes: list[dict],
) -> int:
    """Name every LineString inside a SUBDUCT folder literally SUBDUCT."""
    renamed = 0
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                next_stack = folder_stack + [normalize_text(direct_child_text_by_name(child, "name"))]
            renamed += standardize_subduct_placemark_names(
                child, next_stack, namespace, changes
            )
            continue

        inside_subduct = any(
            normalize_text(folder).upper().startswith("SUBDUCT")
            for folder in folder_stack
        )
        if child_name != "Placemark" or not inside_subduct or not placemark_has_line(child):
            continue

        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        if old_name == "SUBDUCT":
            continue
        name_element.text = "SUBDUCT"
        changes.append({"from": old_name, "to": "SUBDUCT", "folder": folder_stack[-1]})
        renamed += 1
    return renamed


def standardize_fdt_placemarks(
    fdt_placemarks: Sequence[FdtPlacemarkRef],
    records: Sequence[FdtCodingRecord],
    namespace: str,
) -> tuple[list[dict], list[str]]:
    changes: list[dict] = []
    warnings: list[str] = []

    if not fdt_placemarks:
        return changes, warnings
    if not records:
        warnings.append("Kode FDT tidak ditemukan di spreadsheet untuk project ini.")
        return changes, warnings

    for placemark in fdt_placemarks:
        # Resolve the coding by source code first. The physical ordinal is
        # only the placemark position (1, 2, ...), never the numeric suffix
        # inside a code such as BRON03.156.
        record = record_for_fdt_placemark(records, placemark)
        if not record:
            warnings.append(f"Tidak ada kode FDT spreadsheet untuk FDT urutan {placemark.source_order}.")
            continue
        name_element = ensure_direct_child(placemark.element, "name", namespace)
        old_name = normalize_text(name_element.text) or "FDT"

        # Capture capacity hint from the old name (e.g. "FDT 01 / 72C" or
        # "FDT 2 / 48 CORE") BEFORE renaming, so the FDT styling pass can
        # still pick the right colour after the name becomes the bare FDT
        # code (e.g. "PVMJ14.265").
        captured_capacity: int | None = None
        capacity_context = " ".join([old_name, *placemark.folder_stack])
        loose_match = FDT_CAPACITY_LOOSE_PATTERN.search(capacity_context)
        if loose_match:
            captured_capacity = int(loose_match.group(1))
        if captured_capacity:
            description_element = ensure_direct_child(placemark.element, "description", namespace)
            existing_description = normalize_text(description_element.text or "")
            existing_capacity = detect_fdt_capacity_from_description(existing_description)
            if existing_capacity != captured_capacity:
                capacity_note = f"FDT Capacity: {captured_capacity} CORE"
                if existing_description:
                    description_element.text = f"{existing_description}\n{capacity_note}"
                else:
                    description_element.text = capacity_note

        ext_fdt = bool(re.search(r"\bEXT\b", old_name, re.IGNORECASE))
        target_fdt_name = f"EXT.{record.fdt_code}" if ext_fdt else record.fdt_code
        if old_name != target_fdt_name:
            name_element.text = target_fdt_name
            changes.append(
                {
                    "from": old_name,
                    "to": target_fdt_name,
                    "fdt_order": placemark.source_order,
                    "source_sheet": record.sheet_name,
                    "source_row": record.row_number,
                    "site_name": record.site_name,
                }
            )

    return changes, warnings


SLACK_HANGER_FDT_DEDUPE_RADIUS_METER = 1.0


def create_project_slack_from_fdt(
    project: ET.Element, namespace: str, fdt_placemarks: Sequence[FdtPlacemarkRef],
    changes: list[dict], source_fdt_named_points: set[int],
) -> int:
    """Cover Subfeeder FDT points without changing any existing Slack markers."""
    from uuid import uuid4

    # A contextual LINE folder must not suppress uncovered project FDTs.
    slack = []
    used_ids = {item.get("id") for item in project.iter() if item.get("id")}
    for folder in project.iter():
        if local_name(folder.tag) != "Folder" or canonical_standard_folder(
            normalize_text(direct_child_text_by_name(folder, "name"))
        ) != "SLACK HANGER":
            continue
        slack.extend(point for item in folder.iter() if local_name(item.tag) == "Placemark"
                     and id(item) not in source_fdt_named_points
                     and normalize_text(direct_child_text_by_name(item, "name")) in {"SLACK HANGER", "EXT SLACK HANGER"}
                     if (point := placemark_point_coordinate(item)) is not None)
    target = None
    created = 0
    for ref in fdt_placemarks:
        label = ref.current_name
        point = placemark_point_coordinate(ref.element)
        if point is None or any(_haversine_distance_meter(point, other) <= 1.0 for other in slack):
            continue
        if target is None:
            target = ensure_folder(project, namespace, "SLACK HANGER")
        geometry = next((item for item in ref.element.iter() if local_name(item.tag) == "Point"), None)
        if geometry is None:
            continue
        marker_id = f"eva_slack_hanger_{uuid4().hex}"
        while marker_id in used_ids:
            marker_id = f"eva_slack_hanger_{uuid4().hex}"
        used_ids.add(marker_id)
        cloned = ET.Element(kml_tag(namespace, "Placemark"), {"id": marker_id})
        ET.SubElement(cloned, kml_tag(namespace, "name")).text = "SLACK HANGER"
        ET.SubElement(cloned, kml_tag(namespace, "styleUrl")).text = STYLE_SPECS["slack_hanger"].style_url
        cloned_point = ET.SubElement(cloned, kml_tag(namespace, "Point"))
        for item in geometry:
            if local_name(item.tag) in {"extrude", "altitudeMode", "coordinates"}:
                ET.SubElement(cloned_point, item.tag).text = item.text
        target.append(cloned)
        slack.append(point)
        created += 1
        changes.append({"type": "slack_hanger_from_fdt", "from": label,
                        "to": "SLACK HANGER", "reason": "No Slack within 1 m of flat Subfeeder FDT."})
    return created


def create_missing_slack_hanger_from_fdt(
    root: ET.Element,
    namespace: str,
    fdt_placemarks: Sequence[FdtPlacemarkRef],
    changes: list[dict],
) -> int:
    """Ensure exactly one slack hanger near each FDT point in its served LINE.

    Repeat standardization can leave both a legacy top-level LINE and an
    organized project/LINE wrapper. Search every matching SLACK HANGER folder
    before cloning, so an existing source marker prevents a cross-wrapper
    duplicate. If an earlier conversion already produced duplicate markers,
    preserve the earliest/shallowest source marker and remove only the extras
    within the tight FDT radius. Slack markers elsewhere are untouched.
    """
    import copy

    line_folders: dict[int, list[dict]] = {}
    slack_points: dict[int, list[dict]] = {}
    fdt_order_pattern = re.compile(r"\bFDT\s*[-_ ]*(\d+)\b", re.IGNORECASE)
    visit_order = 0

    def walk(
        element: ET.Element,
        folder_stack: list[str],
        active_line: tuple[str, int, ET.Element] | None = None,
    ) -> None:
        nonlocal visit_order
        for child in list(element):
            tag_name = local_name(child.tag)
            if tag_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name") or "")
                next_stack = [*folder_stack, folder_name]
                next_line = active_line
                line_match = LINE_ORDER_PATTERN.match(folder_name)
                if line_match:
                    line_letter = line_match.group(1).upper()
                    fdt_order_match = fdt_order_pattern.search(folder_name)
                    fdt_order = int(fdt_order_match.group(1)) if fdt_order_match else 1
                    next_line = (line_letter, fdt_order, child)
                    if line_letter == "A":
                        line_folders.setdefault(fdt_order, []).append(
                            {
                                "name": folder_name,
                                "folder": child,
                                "depth": len(next_stack),
                                "visit_order": visit_order,
                            }
                        )
                        visit_order += 1

                if (
                    next_line is not None
                    and next_line[0] == "A"
                    and canonical_standard_folder(folder_name) == "SLACK HANGER"
                ):
                    fdt_order = next_line[1]
                    for placemark in list(child):
                        if local_name(placemark.tag) != "Placemark" or not placemark_has_point(placemark):
                            continue
                        point = placemark_point_coordinate(placemark)
                        if point is None:
                            continue
                        slack_points.setdefault(fdt_order, []).append(
                            {
                                "element": placemark,
                                "parent": child,
                                "point": point,
                                "path": " / ".join(next_stack),
                                "depth": len(next_stack),
                                "visit_order": visit_order,
                            }
                        )
                        visit_order += 1
                walk(child, next_stack, next_line)
            elif tag_name in {"Document", "kml"}:
                walk(child, folder_stack, active_line)

    walk(root, [])
    created = 0
    processed_fdt_points: set[tuple[int, str]] = set()
    for fdt_ref in fdt_placemarks:
        source = fdt_ref.element
        source_point = placemark_point_coordinate(source)
        if source_point is None:
            continue
        fdt_order = max(1, int(fdt_ref.source_order or 1))
        source_key = f"{source_point[0]:.8f},{source_point[1]:.8f}"
        processed_key = (fdt_order, source_key)
        if processed_key in processed_fdt_points:
            continue
        processed_fdt_points.add(processed_key)

        nearby = [
            item
            for item in slack_points.get(fdt_order, [])
            if _haversine_distance_meter(source_point, item["point"])
            <= SLACK_HANGER_FDT_DEDUPE_RADIUS_METER
        ]
        if nearby:
            keeper = min(nearby, key=lambda item: (item["depth"], item["visit_order"]))
            for duplicate in nearby:
                if duplicate is keeper:
                    continue
                duplicate["parent"].remove(duplicate["element"])
                slack_points[fdt_order].remove(duplicate)
                changes.append(
                    {
                        "type": "dedupe_slack_hanger_near_fdt",
                        "from": duplicate["path"],
                        "to": keeper["path"],
                        "fdt_order": fdt_order,
                        "distance_meter": round(
                            _haversine_distance_meter(source_point, duplicate["point"]), 3
                        ),
                        "reason": "Duplicate slack hanger within 1 m of the same FDT point.",
                    }
                )
            continue

        candidates = line_folders.get(fdt_order, [])
        if not candidates:
            continue
        target = max(
            candidates,
            key=lambda item: (item["depth"], -item["visit_order"]),
        )
        line_name = target["name"]
        line_folder = target["folder"]
        slack_folder = direct_child_folder_by_name(line_folder, "SLACK HANGER")
        if slack_folder is None:
            slack_folder = ensure_folder(line_folder, namespace, "SLACK HANGER")
        cloned = copy.deepcopy(source)
        name_element = ensure_direct_child(cloned, "name", namespace)
        name_element.text = "SLACK HANGER"
        slack_folder.append(cloned)
        slack_points.setdefault(fdt_order, []).append(
            {
                "element": cloned,
                "parent": slack_folder,
                "point": source_point,
                "path": " / ".join([line_name, "SLACK HANGER"]),
                "depth": target["depth"] + 1,
                "visit_order": visit_order,
            }
        )
        visit_order += 1
        created += 1
        changes.append(
            {
                "type": "slack_hanger_from_fdt",
                "from": "FDT",
                "to": " / ".join([line_name, "SLACK HANGER"]),
                "fdt_order": fdt_order,
                "reason": "No slack hanger exists within 1 m of the FDT point.",
            }
        )
    return created


def collect_fat_counts_by_fdt(element: ET.Element, folder_stack: list[str]) -> Counter[int]:
    counts: Counter[int] = Counter()
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            counts.update(collect_fat_counts_by_fdt(child, next_stack))
            continue

        if child_name != "Placemark":
            continue
        if nearest_standard_folder(folder_stack) != "FAT":
            continue
        if not placemark_has_geometry(child):
            continue
        counts[extract_parent_fdt_order(folder_stack)] += 1
    return counts

def collect_fat_counts_by_line_fdt(element: ET.Element, folder_stack: list[str]) -> Counter[tuple[str, int]]:
    counts: Counter[tuple[str, int]] = Counter()
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            counts.update(collect_fat_counts_by_line_fdt(child, next_stack))
            continue

        if child_name != "Placemark":
            continue
        if nearest_standard_folder(folder_stack) != "FAT":
            continue
        if not placemark_has_geometry(child):
            continue
        line_order = extract_parent_line_order(folder_stack, direct_child_text_by_name(child, "name"))
        fdt_order = extract_parent_fdt_order(folder_stack)
        counts[(line_order, fdt_order)] += 1
    return counts

def collect_distribution_cable_boq_totals(
    element: ET.Element,
    folder_stack: list[str],
    fat_counts_by_line_fdt: Counter[tuple[str, int]] | None = None,
) -> dict[tuple[str, int, str], dict]:
    fat_counts_by_line_fdt = fat_counts_by_line_fdt or Counter()
    totals: dict[tuple[str, int, str], dict] = {}
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            child_totals = collect_distribution_cable_boq_totals(child, next_stack, fat_counts_by_line_fdt)
            for key, value in child_totals.items():
                target = totals.setdefault(
                    key,
                    {
                        "length": 0.0,
                        "cell": value.get("cell", ""),
                        "row": value.get("row"),
                        "route_row": value.get("route_row"),
                    },
                )
                target["length"] = float(target.get("length", 0.0) or 0.0) + float(value.get("length", 0.0) or 0.0)
            continue

        if child_name != "Placemark":
            continue
        if nearest_standard_folder(folder_stack) not in {"DISTRIBUTION CABLE", "CABLE"}:
            continue
        if not placemark_has_line(child):
            continue

        name = direct_child_text_by_name(child, "name")
        description = direct_child_text_by_name(child, "description")
        line_order = extract_parent_line_order(folder_stack, name)
        fdt_order = extract_parent_fdt_order(folder_stack)
        capacity = detect_distribution_cable_capacity(name, description, " ".join(folder_stack))
        if not capacity:
            continue

        length = line_length_with_fallback(placemark_linestrings(child), name, description)
        if length is None:
            continue

        route_row = DISTRIBUTION_CABLE_ROW_MAP.get(f"{line_order}|{capacity}")
        total_row = DISTRIBUTION_CABLE_TOTAL_ROW_MAP.get(f"{line_order}|{capacity}")
        key = (line_order, fdt_order, capacity)
        target = totals.setdefault(
            key,
            {
                "length": 0.0,
                "cell": target_cell(total_row, fdt_order) if total_row else "",
                "row": total_row,
                "route_row": route_row,
            },
        )
        target["length"] = float(target.get("length", 0.0) or 0.0) + float(length)

    for (line_order, fdt_order, _capacity), value in totals.items():
        route_quantity = int(math.ceil(float(value.get("length", 0.0) or 0.0)))
        fat_slack_count = int(fat_counts_by_line_fdt.get((line_order, fdt_order), 0) or 0)
        fdt_slack_count = 1 if fat_slack_count >= 1 else 0
        total_quantity = 0
        if route_quantity > 0:
            total_quantity = int(math.ceil((route_quantity + (fat_slack_count + fdt_slack_count) * 20) * 1.05))
        value["route_quantity"] = route_quantity
        value["fat_slack_count"] = fat_slack_count
        value["fdt_slack_count"] = fdt_slack_count
        value["quantity"] = total_quantity
    return totals

def _collect_feeder_geometry_indexes(
    root: ET.Element,
    *,
    ignore_sling_owned: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Collect FDT placemarks (with code from name) and SLACK HANGER points
    from the standardized KMZ tree. Used by the Feeder cable renamer to
    snap each cable to its nearest FDT and to its own slack-hanger set.

    Returns (fdt_entries, slack_entries):
      fdt_entries: list of {"code": str, "lon": float, "lat": float}
      slack_entries: list of {"id": int (id of element), "lon": float, "lat": float}

    The walk preserves the *nearest standard ancestor folder* so a placemark
    nested inside ``FDT > FDT 01`` still classifies under "FDT".
    """
    fdt_entries: list[dict] = []
    slack_entries: list[dict] = []

    def walk(elem: ET.Element, ancestor_standard_folder: str | None) -> None:
        for ch in list(elem):
            ch_name = local_name(ch.tag)
            if ch_name in {"Document", "Folder"}:
                next_ancestor = ancestor_standard_folder
                if ch_name == "Folder":
                    folder_text = normalize_text(direct_child_text_by_name(ch, "name") or "")
                    if ignore_sling_owned and is_sling_wire_folder_name(folder_text):
                        continue
                    canonical = canonical_standard_folder(folder_text)
                    if canonical:
                        next_ancestor = canonical
                walk(ch, next_ancestor)
                continue
            if ch_name != "Placemark":
                continue
            coord = placemark_point_coordinate(ch)
            if not coord:
                continue
            placemark_name = normalize_text(direct_child_text_by_name(ch, "name") or "")
            folder_upper = (ancestor_standard_folder or "").upper()
            if folder_upper == "FDT":
                # FDT code is in the placemark name (e.g., "PVMJ14.265")
                code_match = FDT_CODE_PATTERN.search(placemark_name)
                code = code_match.group(0).upper() if code_match else placemark_name.upper()
                fdt_entries.append({"code": code, "lon": coord[0], "lat": coord[1]})
            elif folder_upper == "SLACK HANGER":
                slack_entries.append({"id": id(ch), "lon": coord[0], "lat": coord[1],
                                      "fdt_coverage": (ch.get("id") or "").startswith("eva_slack_hanger_")})

    walk(root, None)
    return fdt_entries, slack_entries


def _haversine_distance_meter(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Distance in meters between two (lon, lat) points using GEOD."""
    try:
        _az12, _az21, distance = GEOD.inv(p1[0], p1[1], p2[0], p2[1])
        return float(distance) if math.isfinite(distance) else float("inf")
    except Exception:
        lon1, lat1 = map(math.radians, p1)
        lon2, lat2 = map(math.radians, p2)
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _point_to_segment_distance_meter(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
) -> float:
    """Approximate distance from a point to a lon/lat segment in meters using
    a planar approximation (good enough for short cable spans, < few km)."""
    # Convert lon/lat to a local planar approximation (meters per degree)
    # using the segment midpoint as origin. Adequate for fiber project scale.
    mid_lat = (seg_start[1] + seg_end[1]) / 2.0
    mlat = math.cos(math.radians(mid_lat))

    def to_xy(p: tuple[float, float]) -> tuple[float, float]:
        return (
            (p[0] - seg_start[0]) * 111320.0 * mlat,
            (p[1] - seg_start[1]) * 111320.0,
        )

    px, py = to_xy(point)
    sx, sy = 0.0, 0.0
    ex, ey = to_xy(seg_end)
    dx = ex - sx
    dy = ey - sy
    denom = dx * dx + dy * dy
    if denom <= 1e-9:
        return math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
    t = ((px - sx) * dx + (py - sy) * dy) / denom
    t = max(0.0, min(1.0, t))
    cx = sx + t * dx
    cy = sy + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _slack_count_for_cable(
    cable_lines: Sequence[Sequence[tuple[float, float]]],
    slack_entries: Sequence[dict],
    used_slack_ids: set[int],
    radius_meter: float = 15.0,
    exclude_fdt_coverage: bool = False,
) -> int:
    """Count slack-hanger placemarks that lie on this cable's path (within
    radius_meter of any segment), excluding any slack already counted for a
    previous cable. Updates `used_slack_ids` in place.
    """
    matched = 0
    for slack in slack_entries:
        if slack["id"] in used_slack_ids or (exclude_fdt_coverage and slack.get("fdt_coverage")):
            continue
        slack_point = (slack["lon"], slack["lat"])
        for line in cable_lines:
            if len(line) < 2:
                continue
            min_distance = float("inf")
            for first, second in zip(line, line[1:]):
                d = _point_to_segment_distance_meter(slack_point, first, second)
                if d < min_distance:
                    min_distance = d
                if min_distance <= radius_meter:
                    break
            if min_distance <= radius_meter:
                used_slack_ids.add(slack["id"])
                matched += 1
                break
    return matched


def _resolve_feeder_cable_record(
    cable_start: tuple[float, float],
    fdt_entries: Sequence[dict],
    used_fdt_codes: set[str],
    records: Sequence[FdtCodingRecord],
    fallback_record: FdtCodingRecord | None,
) -> tuple[FdtCodingRecord | None, str]:
    """Pick the closest FDT to the cable start that has not been used yet.
    Returns (record, fdt_code). Falls back to nearest by distance if all
    nearby FDTs are exhausted, and to the fallback_record if no FDT geometry
    is available at all.
    """
    if not fdt_entries:
        if fallback_record:
            return fallback_record, normalize_text(fallback_record.fdt_code).upper()
        return None, ""

    candidates = []
    for entry in fdt_entries:
        d = _haversine_distance_meter(cable_start, (entry["lon"], entry["lat"]))
        candidates.append((d, entry))
    candidates.sort(key=lambda item: item[0])

    chosen_code = ""
    for _distance, entry in candidates:
        code = entry["code"]
        if code and code not in used_fdt_codes:
            chosen_code = code
            break

    if not chosen_code:
        chosen_code = candidates[0][1]["code"]

    record = record_for_fdt_code(records, chosen_code) or fallback_record
    return record, chosen_code



def _subfeeder_cable_fdt_code_map(root: ET.Element, fdt_entries: Sequence[dict]) -> dict[int, str]:
    if not fdt_entries:
        return {}
    cables = _iter_cluster_cable_placemarks(root, [])
    if not cables:
        return {}
    ordered_cables = _order_oriented_subfeeder_cables(_orient_subfeeder_cable_chain(cables, fdt_entries), fdt_entries)

    def fdt_order(entry: dict) -> int:
        code_order = extract_fdt_order(entry.get("code", ""))
        if code_order:
            return code_order
        match = re.search(r"\.(\d{1,3})\b", str(entry.get("code", "")))
        return int(match.group(1)) if match else 999999

    sorted_fdts = sorted(fdt_entries, key=fdt_order)
    fdt_codes = [normalize_text(entry.get("code")).upper() for entry in sorted_fdts]
    fdt_codes = [code for code in fdt_codes if code and not code.startswith("XXX")]
    if len(set(fdt_codes)) < min(len(ordered_cables), len(fdt_codes)):
        fdt_codes = []

    if not fdt_codes:
        seen_codes: set[str] = set()
        for cable in sorted(cables, key=lambda item: int(item.get("order", 0))):
            name = normalize_text(cable.get("name") or "")
            matches = [match.group(0).upper() for match in FDT_CODE_PATTERN.finditer(name) if not match.group(0).upper().startswith("BKS.")]
            for code in matches:
                if code not in seen_codes:
                    seen_codes.add(code)
                    fdt_codes.append(code)
        fdt_codes.sort(key=lambda code: extract_fdt_order(code) or 999999)

    mapping: dict[int, str] = {}
    for index, cable in enumerate(ordered_cables):
        element = cable.get("element")
        if element is None:
            continue
        code = fdt_codes[index] if index < len(fdt_codes) else ""
        if not code:
            target = _target_fdt_for_cable(cable, fdt_entries)
            code = normalize_text(target.get("code") if target else "").upper()
        if code and not code.startswith("XXX"):
            mapping[id(element)] = code
    return mapping

def standardize_distribution_cable_placemarks(
    element: ET.Element,
    folder_stack: list[str],
    records: Sequence[FdtCodingRecord],
    fat_counts_by_line_fdt: Counter[tuple[str, int]],
    fat_counts_by_fdt: Counter[int],
    distribution_boq_totals: dict[tuple[str, int, str], dict],
    namespace: str,
    changes: list[dict],
    is_feeder: bool = False,
    feeder_slack_count: int = 0,
    feeder_context: dict | None = None,
    scope_type: str | None = None,
    network_type: str | None = None,
) -> None:
    """Standardize CABLE / DISTRIBUTION CABLE placemark names + descriptions.

    For Feeder/Subfeeder, `feeder_context` carries shared state across cable
    placemarks: precomputed FDT/slack indexes, and tracking of FDT codes /
    slack-hanger ids already consumed by a previous cable. Pass the same dict
    to every recursive call.
    """
    if is_feeder and feeder_context is None:
        feeder_context = {
            "fdt_entries": [],
            "slack_entries": [],
            "used_fdt_codes": set(),
            "used_slack_ids": set(),
            "segment_index": 0,
            "cable_fdt_code_by_id": {},
        }

    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            standardize_distribution_cable_placemarks(
                child,
                next_stack,
                records,
                fat_counts_by_line_fdt,
                fat_counts_by_fdt,
                distribution_boq_totals,
                namespace,
                changes,
                is_feeder,
                feeder_slack_count,
                feeder_context,
                scope_type,
                network_type,
            )
            continue

        if child_name != "Placemark":
            continue
        source_folder = nearest_standard_folder(folder_stack)
        if source_folder != "DISTRIBUTION CABLE" and source_folder != "CABLE":
            continue
        if not placemark_has_line(child):
            continue

        name_element = ensure_direct_child(child, "name", namespace)
        old_name = normalize_text(name_element.text)
        old_description = direct_child_text_by_name(child, "description")
        line_order = extract_parent_line_order(folder_stack, old_name)
        fdt_order = extract_parent_fdt_order(folder_stack)
        existing_fdt_code_in_name = existing_fdt_code_from_name(old_name)
        if normalize_scope_type(scope_type) == "Subfeeder":
            # The generic search finds the leading HUB token (BKS.100) first.
            # On reconversion without FDT points, read the cable's FDT field.
            existing_subfeeder_code = re.match(
                r"^\s*\S+\s+-\s+([A-Z][A-Z0-9.-]*\.\d{3})\s+-\s+SUBFEEDER\s+CABLE\b",
                old_name, re.IGNORECASE,
            )
            if existing_subfeeder_code:
                existing_fdt_code_in_name = existing_subfeeder_code.group(1).upper()
        if is_feeder:
            # Geometry-driven FDT lookup: pick nearest unused FDT to the
            # cable start. Falls back to the FDT code already in the name,
            # then to the fdt_order-positional record.
            cable_lines = placemark_linestrings(child)
            cable_start = cable_lines[0][0] if cable_lines and cable_lines[0] else None
            fallback_record = (
                record_for_fdt_code(records, existing_fdt_code_in_name)
                or record_for_fdt_order(records, fdt_order)
            )
            mapped_fdt_code = normalize_text(feeder_context.get("cable_fdt_code_by_id", {}).get(id(child), "")).upper()
            if mapped_fdt_code:
                record = record_for_fdt_code(records, mapped_fdt_code) or fallback_record
                fdt_code = mapped_fdt_code
            elif existing_fdt_code_in_name and existing_fdt_code_in_name not in feeder_context["used_fdt_codes"]:
                record = record_for_fdt_code(records, existing_fdt_code_in_name) or fallback_record
                # A Subfeeder reconversion no longer has FDT points. Retain the
                # already assigned multi-FDT cable identity instead of falling
                # back to the first coding row for every cable.
                if (normalize_scope_type(scope_type) == "Subfeeder"
                        and not feeder_context["fdt_entries"]
                        and re.search(r"-\d+\.\d{3}$", existing_fdt_code_in_name)):
                    fdt_code = existing_fdt_code_in_name
                else:
                    fdt_code = normalize_text(record.fdt_code if record else existing_fdt_code_in_name).upper()
            else:
                record, snapped_code = _resolve_feeder_cable_record(
                    cable_start,
                    feeder_context["fdt_entries"],
                    feeder_context["used_fdt_codes"],
                    records,
                    fallback_record,
                ) if cable_start else (fallback_record, "")
                fdt_code = normalize_text(record.fdt_code if record else snapped_code).upper()
            if fdt_code:
                feeder_context["used_fdt_codes"].add(fdt_code)
        else:
            record = record_for_fdt_order(records, fdt_order)
            fdt_code = normalize_text(record.fdt_code if record else existing_fdt_code_in_name).upper()

        capacity = detect_distribution_cable_capacity(old_name, old_description, " ".join(folder_stack))
        if not fdt_code:
            continue
        if not capacity and not is_feeder:
            fallback_fat_count = fat_counts_by_line_fdt.get((line_order, fdt_order), 0) or fat_counts_by_fdt.get(fdt_order, 0)
            if fallback_fat_count:
                capacity = distribution_capacity_for_fat_count(fallback_fat_count)
        if not capacity:
            continue

        # Feeder geometry is authoritative; vendor descriptions may use '.' as a thousands separator.
        route_geometry_meter = (calculate_linestring_length_meter(placemark_linestrings(child))
                                if normalize_scope_type(scope_type) == "Feeder" else 0)
        route_meter = rounded_meter(route_geometry_meter) if route_geometry_meter > 0 else route_length_meter_for_placemark(child)
        if route_meter <= 0:
            continue
        described_slack = number_from_text(TOTAL_SLACK_PATTERN, old_description)
        if is_feeder:
            # Slack count = slack-hanger placemarks lying on this cable's
            # path, excluding ones already attributed to a previous cable.
            cable_lines = cable_lines if 'cable_lines' in locals() else placemark_linestrings(child)
            line_fat_count = _slack_count_for_cable(
                cable_lines,
                feeder_context["slack_entries"],
                feeder_context["used_slack_ids"],
                exclude_fdt_coverage=normalize_scope_type(scope_type) == "Subfeeder",
            )
        else:
            line_fat_count = fat_counts_by_line_fdt.get((line_order, fdt_order), 0)
            if not line_fat_count and described_slack and described_slack > 1:
                line_fat_count = int(described_slack) - 1
            if not line_fat_count:
                line_fat_count = fat_counts_by_fdt.get(fdt_order, 0)

        if is_feeder:
            slack_meter = line_fat_count * 20
        else:
            slack_meter = (1 + line_fat_count) * 20
        fallback_total_meter = rounded_meter(((route_geometry_meter if route_geometry_meter > 0 else route_meter)
                                              + slack_meter) * 1.05) if normalize_scope_type(scope_type) == "Feeder" else rounded_meter((route_meter + slack_meter) * 1.05)
        # For Feeder, the BOQ totals lookup uses (line, fdt, capacity) but
        # Feeder cables live in a flat folder, so the lookup key is unreliable
        # and would return the same total for every cable in the project. Use
        # the per-cable computed fallback (route + slack tolerance) instead.
        if is_feeder:
            boq_total: dict = {}
            description_total_meter = fallback_total_meter
        else:
            boq_total = distribution_boq_totals.get((line_order, fdt_order, capacity), {})
            description_total_meter = int(boq_total.get("quantity") or fallback_total_meter)
        # The conversion selection is authoritative. Legacy UG source files often
        # carry stale AE tokens in cable names/descriptions.
        selected_network_type = normalize_text(network_type).upper()
        cable_network_type = (
            selected_network_type
            if selected_network_type in {"AE", "UG"}
            else detect_network_type(old_name, old_description, " ".join(folder_stack))
        )
        hub_id = record_hub_id(record) if record else ""
        if is_feeder:
            is_subfeeder_scope = normalize_scope_type(scope_type) == "Subfeeder"
            if is_subfeeder_scope:
                olt_code = hub_id or "XXX.XXX.XXXX"
                new_name = f"{olt_code} - {fdt_code} - SUBFEEDER CABLE (FO {capacity}) - {cable_network_type} - {description_total_meter}m"
            else:
                matched_olt_code = normalize_text(feeder_context.get("matched_olt_code", "")).upper() if feeder_context else ""
                olt_code = matched_olt_code or "XXX.XXX.XXXX"
                segment_num = feeder_context.get("project_segment_number") if feeder_context else None
                if not segment_num:
                    feeder_context["segment_index"] = feeder_context.get("segment_index", 0) + 1
                    segment_num = feeder_context["segment_index"]
                new_name = f"{olt_code} - MAINFEEDER CABLE SEGMENT {segment_num} (FO {capacity}) - {cable_network_type} - {description_total_meter}m"
            new_description = format_feeder_distribution_description(route_meter, line_fat_count, description_total_meter)
        else:
            new_name = f"{fdt_code} - CABLE LINE {line_order} (FO {capacity}) - {cable_network_type} - {description_total_meter}m"
            new_description = format_distribution_description(route_meter, line_fat_count, description_total_meter)

        description_element = ensure_direct_child(child, "description", namespace)
        if old_name != new_name or old_description != new_description:
            name_element.text = new_name
            description_element.text = new_description
            changes.append(
                {
                    "from": old_name,
                    "to": new_name,
                    "folder": "CABLE" if is_feeder else "DISTRIBUTION CABLE",
                    "line": line_order,
                    "fdt_order": fdt_order,
                    "route_meter": route_meter,
                    "fat_slack_count": line_fat_count,
                    "name_total_meter": description_total_meter,
                    "fallback_total_meter": fallback_total_meter,
                    "description_total_meter": description_total_meter,
                    "boq_cell": boq_total.get("cell", ""),
                }
            )

def style_key_for_placemark(
    placemark: ET.Element,
    folder_stack: Sequence[str],
    source_folder: str | None,
    fdt_order_by_placemark: dict[int, int],
    fat_counts_by_fdt: Counter[int],
) -> tuple[str | None, dict]:
    if not source_folder or not placemark_has_geometry(placemark):
        return None, {}

    name = direct_child_text_by_name(placemark, "name")
    if source_folder == "FDT" and placemark_has_point(placemark):
        if re.match(r"^EXT\.", normalize_text(name or ""), re.IGNORECASE):
            return "fdt_ext", {"ext": True}
        fdt_order = fdt_order_by_placemark.get(id(placemark), 1)
        fat_count = fat_counts_by_fdt.get(fdt_order, 0)
        if not fat_count and len(fdt_order_by_placemark) == 1:
            fat_count = sum(fat_counts_by_fdt.values())
        description = direct_child_text_by_name(placemark, "description")
        # Cek capacity dari description dulu, lalu dari nama, lalu fallback ke fat count
        description_capacity = detect_fdt_capacity_from_description(description)
        name_capacity = detect_fdt_capacity_from_description(name) if not description_capacity else None
        capacity = description_capacity or name_capacity or fdt_capacity_for_fat_count(fat_count)
        metadata = {"fdt_order": fdt_order, "fat_count": fat_count, "capacity": capacity}
        if description_capacity:
            metadata["capacity_source"] = "description"
        elif name_capacity:
            metadata["capacity_source"] = "name"
        style_capacity = capacity if f"fdt_{capacity}c" in STYLE_SPECS else fdt_capacity_for_fat_count(fat_count)
        return f"fdt_{style_capacity}c", metadata

    if source_folder == "FAT" and placemark_has_point(placemark):
        # A project/FAT code can contain bare numbers (for example
        # KRGPPK-8.059.A01); only an explicit 8C/16C/... token is capacity.
        capacity_match = re.search(r"\b(8|16|24|32|48)\s*(?:CORE|C)\b", " ".join([name or "", direct_child_text_by_name(placemark, "description") or ""]), re.IGNORECASE)
        capacity = int(capacity_match.group(1)) if capacity_match else 16
        return f"fat_{capacity}c", {"capacity": capacity}

    if source_folder == "OLT" and placemark_has_point(placemark):
        return "olt", {}

    if source_folder == "JOINT CLOSURE" and placemark_has_point(placemark):
        is_ext = JOINT_CLOSURE_EXT_PATTERN.search(name)
        if is_ext:
            return "joint_closure_ext", {"type": "ext"}
        cap_match = JOINT_CLOSURE_CAPACITY_PATTERN.search(name)
        capacity = int(cap_match.group(1)) if cap_match else 72
        return f"joint_closure_new_{capacity}c", {"type": "new", "capacity": capacity}

    if source_folder in NEW_POLE_STYLE_BY_FOLDER and placemark_has_point(placemark):
        return NEW_POLE_STYLE_BY_FOLDER[source_folder], {}

    if source_folder.startswith("EXISTING POLE") and placemark_has_point(placemark):
        return "existing_pole", {}

    if source_folder == "HP COVER" and placemark_has_point(placemark):
        return "hp_cover", {}

    if source_folder == "HP UNCOVER" and placemark_has_point(placemark):
        return "hp_uncover", {}

    if source_folder == "SLACK HANGER" and placemark_has_point(placemark):
        if looks_like_partner_pole_name(name) or looks_like_existing_emr_pole_name(name) or re.search(r"\bEXT(?:\s|\.)", (name or ""), re.IGNORECASE):
            return "slack_hanger_ext", {"ext": True}
        return "slack_hanger", {}

    if source_folder.startswith("EXISTING HH") and placemark_has_point(placemark):
        return "existing_handhole", {"ext": True}

    if source_folder == "NEW HH 120X120X130" and placemark_has_point(placemark):
        return "handhole_120", {}

    if source_folder == "NEW HH 100X100X130" and placemark_has_point(placemark):
        return "handhole_100", {}

    if source_folder == "NEW HH 80X80X130" and placemark_has_point(placemark):
        return "handhole_80", {}

    if source_folder == "NEW HH 40X40X60" and placemark_has_point(placemark):
        return "handhole_40", {}

    if source_folder == "NEW HH 20X20X20" and placemark_has_point(placemark):
        return "handhole_20", {}

    if source_folder == "PEDESTAL HANDHOLE" and placemark_has_point(placemark):
        return "pedestal_handhole", {}

    # converter.canonical_standard_folder() retains legacy TWEY internally;
    # output folders are normalized to literal T-WAY in the final pass.
    if source_folder in {"T-WAY", "TWEY"} and placemark_has_point(placemark):
        return "twey", {}

    if source_folder in {"DISTRIBUTION CABLE", "CABLE"} and placemark_has_line(placemark):
        capacity = detect_distribution_cable_capacity(name, " ".join(folder_stack))
        if capacity:
            return DISTRIBUTION_CABLE_STYLE_BY_CAPACITY.get(capacity), {"capacity": capacity}
        return None, {}

    if source_folder == "SLING WIRE" and placemark_has_line(placemark):
        return "sling_wire", {}

    if source_folder == "DROP WIRE" and placemark_has_line(placemark):
        return "drop_wire", {}

    if source_folder == "ASPHALT" and placemark_has_line(placemark):
        return "asphalt", {}

    if source_folder in {"BORING", "BORING MANUAL", "BORING MACHINE"} and placemark_has_line(placemark):
        return "boring", {}

    if source_folder == "CUT TRACK" and placemark_has_line(placemark):
        return "cut_track", {}

    if source_folder == "CROSSING REL KERETA" and placemark_has_line(placemark):
        return "crossing_rel_kereta", {}

    if source_folder == "OPEN TRENCHING" and placemark_has_line(placemark):
        return "open_trenching", {}

    if source_folder == "SEWER" and placemark_has_line(placemark):
        return "sewer", {}

    if source_folder == "SUBMARINE" and placemark_has_line(placemark):
        return "submarine", {}

    if source_folder == "GALVANIS" and placemark_has_line(placemark):
        return "galvanis", {}

    if source_folder == "SUBDUCT 40/34" and placemark_has_line(placemark):
        return "subduct_40_34", {}

    if source_folder == "SUBDUCT 32/28" and placemark_has_line(placemark):
        return "subduct_32_28", {}

    if source_folder == "SUBDUCT 3RD PARTY" and placemark_has_line(placemark):
        return "subduct_3rd_party", {}

    if source_folder == "PVC 4INCH" and placemark_has_line(placemark):
        return "pvc_4inch", {}

    if source_folder == "TALI PANCING" and placemark_has_line(placemark):
        return "tali_pancing", {}

    return None, {}

def apply_standard_style(
    placemark: ET.Element,
    style_key: str,
    namespace: str,
    source_folder: str,
    metadata: dict,
    changes: list[dict],
    *,
    preserve_extension_payload: bool = False,
) -> None:
    spec = STYLE_SPECS.get(style_key)
    if not spec:
        return

    previous_style = normalized_style_url(direct_child_text_by_name(placemark, "styleUrl"))
    removed_inline = remove_inline_styles(
        placemark,
        preserve_extension_payload=preserve_extension_payload,
    )
    style_url_element = ensure_style_url_child(placemark, namespace)
    if previous_style == spec.style_url and not removed_inline:
        return

    style_url_element.text = spec.style_url
    change = {
        "name": direct_child_text_by_name(placemark, "name"),
        "folder": source_folder,
        "from": previous_style,
        "to": spec.style_url,
        "style_id": spec.style_id,
        "color": spec.color,
    }
    if removed_inline:
        change["inline_styles_removed"] = removed_inline
    change.update({key: value for key, value in metadata.items() if value not in (None, "")})
    changes.append(change)

def apply_boundary_fat_default_polygon_style(
    placemark: ET.Element,
    source_folder: str,
    namespace: str,
    changes: list[dict],
) -> None:
    normalized_folder = normalize_text(source_folder).upper()
    is_fat_area = normalized_folder in FAT_AREA_POLYGON_FOLDERS
    spec = STYLE_SPECS["fat_area_polygon" if is_fat_area else "boundary_polygon"]
    previous_style = normalized_style_url(direct_child_text_by_name(placemark, "styleUrl"))
    removed_inline = remove_inline_styles(placemark)
    style_url_element = ensure_style_url_child(placemark, namespace)

    if previous_style == spec.style_url and not removed_inline:
        return

    style_url_element.text = spec.style_url
    if previous_style or removed_inline or previous_style != spec.style_url:
        change = {
            "name": direct_child_text_by_name(placemark, "name"),
            "folder": source_folder,
            "from": previous_style,
            "to": spec.style_url,
            "style_id": spec.style_id,
            "color": spec.color,
            "line_opacity": "100%",
            "fill_opacity": "30%",
            "filled_outlined": True,
        }
        if removed_inline:
            change["inline_styles_removed"] = removed_inline
        changes.append(change)

def nearest_boundary_polygon_folder(folder_stack: list[str]) -> str:
    for folder_name in reversed(folder_stack):
        normalized = normalize_text(folder_name).upper()
        if normalized in BOUNDARY_POLYGON_FOLDERS:
            return normalized
    return ""

def standardize_kml_styles(
    element: ET.Element,
    folder_stack: list[str],
    namespace: str,
    fdt_order_by_placemark: dict[int, int],
    fat_counts_by_fdt: Counter[int],
    changes: list[dict],
) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name in {"Document", "Folder"}:
            next_stack = folder_stack
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name"))
                next_stack = folder_stack + [folder_name]
            standardize_kml_styles(child, next_stack, namespace, fdt_order_by_placemark, fat_counts_by_fdt, changes)
            continue

        if child_name != "Placemark":
            continue
        source_folder = nearest_standard_folder(folder_stack)
        if not source_folder:
            # converter.py does not classify every UG folder used by this
            # standardizer (notably EXISTING HH). Resolve against this module's
            # complete template so vendor icons cannot survive in valid folders.
            for folder_name in reversed(folder_stack):
                source_folder = canonical_folder_match(folder_name, None)
                if source_folder:
                    break
        # All canonical placemarks, including HH/UG assets, must use the
        # standard template style; never preserve vendor source icons.
        boundary_polygon_folder = nearest_boundary_polygon_folder(folder_stack)
        if boundary_polygon_folder and placemark_has_geometry(child, {"Polygon"}):
            apply_boundary_fat_default_polygon_style(child, boundary_polygon_folder, namespace, changes)
            continue
        style_key, metadata = style_key_for_placemark(
            child,
            folder_stack,
            source_folder,
            fdt_order_by_placemark,
            fat_counts_by_fdt,
        )
        if style_key and source_folder:
            apply_standard_style(child, style_key, namespace, source_folder, metadata, changes)


def force_tway_folder_and_placemark_names(root: ET.Element, namespace: str) -> int:
    """Use literal T-WAY output name and label every contained Placemark T-WAY."""
    renamed = 0

    def walk(element: ET.Element, inside_tway: bool = False) -> None:
        nonlocal renamed
        for child in list(element):
            child_name = local_name(child.tag)
            if child_name == "Folder":
                folder_name = normalize_text(direct_child_text_by_name(child, "name")).upper()
                is_tway_folder = is_tway_folder_alias(folder_name)
                child_inside_tway = inside_tway or is_tway_folder
                if is_tway_folder:
                    folder_name_element = ensure_direct_child(child, "name", namespace)
                    if normalize_text(folder_name_element.text) != "T-WAY":
                        folder_name_element.text = "T-WAY"
                walk(child, child_inside_tway)
            elif child_name == "Placemark" and inside_tway:
                placemark_name_element = ensure_direct_child(child, "name", namespace)
                if normalize_text(placemark_name_element.text) != "T-WAY":
                    placemark_name_element.text = "T-WAY"
                    renamed += 1
            elif child_name == "Document":
                walk(child, inside_tway)

    walk(root)
    return renamed


def serialize_kml(root: ET.Element, *, protect_sling_owned: bool = False) -> bytes:
    namespace = kml_namespace(root)
    if namespace:
        ET.register_namespace("", namespace)
    ET.register_namespace("gx", "http://www.google.com/kml/ext/2.2")
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("xal", "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0")
    # Register any namespaces found in the root element attributes
    for key, value in (root.attrib or {}).items():
        if key.startswith("{"):
            continue
        if key.startswith("xmlns:"):
            prefix = key.split(":", 1)[1]
            ET.register_namespace(prefix, value)
    # Final KMZ folder-name override.
    # If no physical LINE folder has FDT order > 1, collapse outside-Sling
    # "LINE X - FDT 1" labels. Nested Sling-owned metadata is never rewritten.
    line_fdt_orders: list[int] = []
    line_name_nodes: list[tuple[ET.Element, str]] = []

    def collect_line_names(element: ET.Element, sling_owned: bool = False) -> None:
        for folder in list(element):
            if local_name(folder.tag) not in {"Document", "Folder"}:
                continue
            child_sling_owned = sling_owned
            if local_name(folder.tag) == "Folder":
                original_name = normalize_text(direct_child_text_by_name(folder, "name"))
                child_sling_owned = sling_owned or is_sling_wire_folder_name(original_name)
                if not (protect_sling_owned and child_sling_owned):
                    match = re.fullmatch(r"LINE\s+([A-Z])\s*-\s*FDT\s+(\d+)", original_name.upper())
                    if match:
                        line_fdt_orders.append(int(match.group(2)))
                        name_element = direct_child_by_name(folder, "name")
                        if name_element is not None:
                            line_name_nodes.append((name_element, match.group(1)))
            if not (protect_sling_owned and child_sling_owned):
                collect_line_names(folder, child_sling_owned)

    collect_line_names(root)
    if line_fdt_orders and max(line_fdt_orders) == 1:
        for name_element, line_order in line_name_nodes:
            name_element.text = f"LINE {line_order}"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_kmz_with_replaced_kml(input_path: str | Path, output_path: str | Path, kml_name: str, kml_bytes: bytes) -> None:
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if input_path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(input_path) as source, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target:
            replaced = False
            for info in source.infolist():
                if info.filename == kml_name:
                    target.writestr(info.filename, kml_bytes)
                    replaced = True
                else:
                    target.writestr(info, source.read(info.filename))
            if not replaced:
                target.writestr(kml_name or "doc.kml", kml_bytes)
        return

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr(kml_name or "doc.kml", kml_bytes)


def consolidate_duplicate_line_folders(project_folder: ET.Element, namespace: str, changes: list[dict]) -> None:
    """Merge duplicate line folders and rename legacy variants."""
    # Collect all line folders by line_order and fdt_order
    line_groups = {}
    for child in list(project_folder):
        if local_name(child.tag) != "Folder":
            continue
        folder_name = normalize_text(direct_child_text_by_name(child, "name"))
        if not folder_name:
            continue
        
        m = re.fullmatch(r"LINE\s+([A-Z])\s*-\s*FDT\s+(\d+)", folder_name.upper())
        if m:
            key = (m.group(1), int(m.group(2)))
            line_groups.setdefault(key, []).append(child)
            continue
            
        m = re.fullmatch(r"LINE\s+([A-Z])(?:\s+FDT\s+(\d+))?", folder_name.upper())
        if m:
            key = (m.group(1), int(m.group(2) or 1))
            line_groups.setdefault(key, []).append(child)
            continue
            
        m = re.fullmatch(r"LINE\s+([A-Z])", folder_name.upper())
        if m:
            key = (m.group(1), 1)
            line_groups.setdefault(key, []).append(child)
            continue
    
    # Process each group
    for key, folders in line_groups.items():
        if len(folders) == 1:
            folder = folders[0]
            folder_name = normalize_text(direct_child_text_by_name(folder, "name"))
            correct_name = format_line_folder_name(key[0], key[1])
            if folder_name != correct_name:
                ensure_direct_child(folder, "name", namespace).text = correct_name
            continue
            
        # Multiple folders - merge into first one
        target = folders[0]
        correct_name = format_line_folder_name(key[0], key[1])
        ensure_direct_child(target, "name", namespace).text = correct_name
        
        for other in folders[1:]:
            moved = merge_folder_contents(other, target, namespace)
            project_folder.remove(other)
            other_name = normalize_text(direct_child_text_by_name(other, "name"))
            changes.append({
                "from": other_name,
                "to": correct_name,
                "children_moved": moved,
                "reason": "duplicate line folder consolidated"
            })


def _rehome_raw_fdt_boundary_cover(root: ET.Element, namespace: str) -> None:
    """Rehome stray FDT/BOUNDARY and FDT/COVER folders into standard LINE folders."""
    q=lambda tag:f'{{{namespace}}}{tag}'
    doc=root if root.tag==q('Document') else root.find(q('Document'))
    if doc is None:
        return

    def get_name(el):
        n=el.find(q('name')); return (n.text or '').strip() if n is not None and n.text else ''

    def get_or_create(parent, folder_name):
        for child in parent.findall(q('Folder')):
            if get_name(child)==folder_name:
                return child
        f=ET.SubElement(parent,q('Folder'))
        ET.SubElement(f,q('name')).text=folder_name
        return f

    project_container = None
    doc_folders = doc.findall(q('Folder'))
    for f in doc_folders:
        nm = get_name(f)
        if nm and not (nm.startswith('LINE ') or nm.upper().startswith('FDT ')):
            project_container = f
            break
    if project_container is None:
        project_container = doc

    line_folders=[]
    for f in project_container.findall(q('Folder')):
        name=get_name(f)
        if name and name.startswith('LINE ') and ' - FDT ' in name:
            line_folders.append((name,f))
    fdt_map={}
    for name,f in line_folders:
        m=re.search(r'FDT\s*(\d+)',name,re.IGNORECASE)
        if m:
            fdt_map.setdefault(int(m.group(1)),[]).append((name,f))

    to_process=[]
    for folder in project_container.findall(q('Folder')):
        name=get_name(folder)
        if not name:
            continue
        if name.upper().startswith('FDT '):
            for child in folder.findall(q('Folder')):
                child_name=get_name(child)
                if child_name and child_name.upper() in {'BOUNDARY','COVER'}:
                    to_process.append((folder,name,child,child_name,child_name.upper()))

    for fdt_folder,fdt_name,raw_folder,raw_name,raw_upper in to_process:
        m=re.search(r'FDT\s*(\d+)',fdt_name,re.IGNORECASE)
        if not m:
            continue
        targets=fdt_map.get(int(m.group(1)),[])
        if not targets:
            continue
        moved=False
        if raw_upper=='BOUNDARY':
            pms=list(raw_folder.findall(q('Placemark')))
            if not pms:
                continue
            if len(targets)==1:
                _,lf=targets[0]
                bf=get_or_create(lf,'BOUNDARY FAT')
                for pm in pms: bf.append(pm)
            else:
                for i,pm in enumerate(pms):
                    _,lf=targets[i%len(targets)]
                    bf=get_or_create(lf,'BOUNDARY FAT')
                    bf.append(pm)
            moved=True
        elif raw_upper=='COVER':
            pms=list(raw_folder.findall(q('Placemark')))
            if not pms:
                continue
            # COVER polygons belong in existing BOUNDARY FAT folder, not HP COVER.
            if len(targets)==1:
                _,lf=targets[0]
                bf=get_or_create(lf,'BOUNDARY FAT')
                for pm in pms:
                    bf.append(pm)
            else:
                for i,pm in enumerate(pms):
                    _,lf=targets[i%len(targets)]
                    bf=get_or_create(lf,'BOUNDARY FAT')
                    bf.append(pm)
            moved=True
        if moved:
            try:
                fdt_folder.remove(raw_folder)
            except Exception:
                pass

    def prune(el):
        for child in list(el.findall(q('Folder'))):
            prune(child)
        to_del=[child for child in el.findall(q('Folder')) if len(child)==0]
        for child in to_del:
            el.remove(child)
    prune(doc)


def _enforce_one_boundary_polygon_per_fat(root: ET.Element, namespace: str) -> None:
    """For each LINE X - FDT Y, assign nearest unused BOUNDARY FAT polygon to each FAT placemark and rename polygon to FAT code."""
    import math
    q=lambda tag:f'{{{namespace}}}{tag}'
    doc=root if root.tag==q('Document') else root.find(q('Document'))
    if doc is None:
        return

    def get_name(el):
        n=el.find(q('name'))
        return (n.text or '').strip() if n is not None and n.text else ''

    def set_name(el, text):
        n=el.find(q('name'))
        if n is None:
            n=ET.SubElement(el,q('name'))
        n.text=text

    def first_coord(el):
        c=el.find('.//'+q('coordinates'))
        if c is None or not c.text:
            return None
        first=c.text.strip().split()[0]
        parts=first.split(',')
        if len(parts)<2:
            parts=first.split()
        if len(parts)<2:
            return None
        try:
            return (float(parts[0]), float(parts[1]))
        except Exception:
            return None

    def polygon_centroid(pm):
        coords=[]
        c=pm.find('.//'+q('coordinates'))
        if c is None or not c.text:
            return first_coord(pm)
        for tok in c.text.strip().split():
            parts=tok.split(',')
            if len(parts)>=2:
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except Exception:
                    pass
        if not coords:
            return None
        return (sum(x for x,y in coords)/len(coords), sum(y for x,y in coords)/len(coords))

    def dist(a,b):
        return (a[0]-b[0])**2 + (a[1]-b[1])**2

    # project folder(s) under Document
    containers=[]
    for f in doc.findall(q('Folder')):
        # project container contains LINE folders; if doc directly contains LINE, use doc
        if any(is_line_folder_name(get_name(ch)) for ch in f.findall(q('Folder'))):
            containers.append(f)
    if not containers:
        containers=[doc]

    for container in containers:
        for line_folder in container.findall(q('Folder')):
            line_name=get_name(line_folder)
            if not is_line_folder_name(line_name):
                continue
            fat_folder=None
            boundary_folder=None
            for ch in line_folder.findall(q('Folder')):
                chn=get_name(ch)
                if chn=='FAT':
                    fat_folder=ch
                elif chn=='BOUNDARY FAT':
                    boundary_folder=ch
            if fat_folder is None or boundary_folder is None:
                continue
            fats=[]
            for pm in fat_folder.findall(q('Placemark')):
                nm=get_name(pm)
                pt=first_coord(pm)
                if nm and pt is not None:
                    fats.append((nm,pt,pm))
            polys=[]
            # flatten any existing subfolders too, but final output will be direct placemarks
            for pm in list(boundary_folder.findall(q('Placemark'))):
                cen=polygon_centroid(pm)
                if cen is not None:
                    polys.append((pm,cen))
            for sub in list(boundary_folder.findall(q('Folder'))):
                for pm in list(sub.findall(q('Placemark'))):
                    cen=polygon_centroid(pm)
                    if cen is not None:
                        polys.append((pm,cen))
            if not fats or not polys:
                continue
            assigned=[]
            used=set()
            # greedy: closest pair globally, prevents one polygon used twice
            candidates=[]
            for fi,(fname,fpt,fpm) in enumerate(fats):
                for pi,(poly,cen) in enumerate(polys):
                    candidates.append((dist(fpt,cen),fi,pi))
            candidates.sort(key=lambda x:x[0])
            used_fats=set(); used_polys=set()
            for _,fi,pi in candidates:
                if fi in used_fats or pi in used_polys:
                    continue
                used_fats.add(fi); used_polys.add(pi)
                assigned.append((find_fat_code(fats[fi][0]) or fats[fi][0], polys[pi][0]))
                if len(used_fats)==len(fats) or len(used_polys)==len(polys):
                    break
            # rebuild BOUNDARY FAT direct placemarks only for assigned polygons
            for child in list(boundary_folder):
                if child.tag!=q('name'):
                    boundary_folder.remove(child)
            for fat_name, poly_pm in assigned:
                set_name(poly_pm,fat_name)
                boundary_folder.append(poly_pm)

def _enforce_one_boundary_polygon_per_fat_global(root: ET.Element, namespace: str) -> None:
    """Globally assign every BOUNDARY FAT polygon to nearest FAT placemark, then move it to that FAT line folder.

    Fixes legacy KMZs where a polygon already sits under wrong LINE-FDT folder; per-line matching keeps it wrong.
    """
    q=lambda tag:f'{{{namespace}}}{tag}'
    doc=root if root.tag==q('Document') else root.find(q('Document'))
    if doc is None:
        return

    def get_name(el):
        n=el.find(q('name'))
        return (n.text or '').strip() if n is not None and n.text else ''

    def set_name(el, text):
        n=el.find(q('name'))
        if n is None:
            n=ET.SubElement(el,q('name'))
        n.text=text

    def coordinates(el):
        c=el.find('.//'+q('coordinates'))
        if c is None or not c.text:
            return []
        out=[]
        for tok in c.text.strip().split():
            parts=tok.split(',')
            if len(parts)>=2:
                try:
                    out.append((float(parts[0]), float(parts[1])))
                except Exception:
                    pass
        return out

    def centroid(el):
        coords=coordinates(el)
        if not coords:
            return None
        return (sum(x for x,y in coords)/len(coords), sum(y for x,y in coords)/len(coords))

    def first_coord(el):
        coords=coordinates(el)
        return coords[0] if coords else None

    def dist(a,b):
        return (a[0]-b[0])**2 + (a[1]-b[1])**2

    parent_by_id={}
    for parent in root.iter():
        for child in list(parent):
            parent_by_id[id(child)]=parent

    def remove_from_parent(el):
        par=parent_by_id.get(id(el))
        if par is not None:
            try:
                par.remove(el)
            except ValueError:
                pass

    def ancestor_line_folder(el):
        cur=el
        while cur is not None:
            if cur.tag==q('Folder'):
                nm=get_name(cur)
                if nm.startswith('LINE '):
                    return cur
            cur=parent_by_id.get(id(cur))
        return None

    def ancestor_line_name(el):
        lf=ancestor_line_folder(el)
        return get_name(lf) if lf is not None else ''

    # collect FAT placemarks with owning line folder
    fats=[]
    for folder in root.iter(q('Folder')):
        if get_name(folder)!='FAT':
            continue
        line_folder=ancestor_line_folder(folder)
        if line_folder is None:
            continue
        for pm in folder.findall(q('Placemark')):
            nm=get_name(pm)
            pt=first_coord(pm)
            if nm and pt is not None:
                fats.append({'name':nm, 'point':pt, 'line_folder':line_folder})
    if not fats:
        return

    # collect all boundary polygons/placemarks from all BOUNDARY FAT folders
    boundaries=[]
    boundary_folders=[]
    for folder in root.iter(q('Folder')):
        if get_name(folder)!='BOUNDARY FAT':
            continue
        boundary_folders.append(folder)
        for pm in list(folder.findall(q('Placemark'))):
            cen=centroid(pm)
            if cen is not None:
                boundaries.append({'pm':pm, 'center':cen, 'line_name': ancestor_line_name(folder)})
        for sub in list(folder.findall(q('Folder'))):
            for pm in list(sub.findall(q('Placemark'))):
                cen=centroid(pm)
                if cen is not None:
                    boundaries.append({'pm':pm, 'center':cen, 'line_name': ancestor_line_name(folder)})

    if not boundaries:
        return

    # Assign boundary polygons to FATs. Prefer exact FAT-code/name match so duplicate A01/B01 across FDTs are preserved,
    # then greedily assign nearest remaining one-to-one. Never drop unmatched BOUNDARY FAT polygons.
    def norm_fat(value):
        return (find_fat_code(value or '') or normalize_text(value or '')).upper()
    assignments=[]; used_b=set(); used_f=set()
    by_name={}
    for fi,f in enumerate(fats):
        by_name.setdefault(norm_fat(f['name']), []).append(fi)
    for bi,b in enumerate(boundaries):
        bname=norm_fat(get_name(b['pm']))
        candidates=[fi for fi in by_name.get(bname, []) if fi not in used_f]
        same_line=[fi for fi in candidates if get_name(fats[fi].get('line_folder')) == b.get('line_name')]
        if same_line:
            candidates=same_line
        if not candidates:
            continue
        fi=min(candidates, key=lambda idx: dist(b['center'], fats[idx]['point']))
        used_b.add(bi); used_f.add(fi); assignments.append((b['pm'], fats[fi]))
    pairs=[]
    for bi,b in enumerate(boundaries):
        if bi in used_b:
            continue
        for fi,f in enumerate(fats):
            if fi in used_f:
                continue
            pairs.append((dist(b['center'], f['point']), bi, fi))
    pairs.sort(key=lambda x:x[0])
    for _d,bi,fi in pairs:
        if bi in used_b or fi in used_f:
            continue
        used_b.add(bi); used_f.add(fi); assignments.append((boundaries[bi]['pm'], fats[fi]))
        if len(used_b)==len(boundaries) or len(used_f)==len(fats):
            break
    # Preserve extra polygons even when no FAT placemark matched. Keep original name and current BOUNDARY FAT folder.
    for bi,b in enumerate(boundaries):
        if bi not in used_b:
            assignments.append((b['pm'], None))

    # clear BOUNDARY FAT folders, then rehome assigned polygons into nearest FAT line folder
    for bf in boundary_folders:
        for child in list(bf):
            if child.tag != q('name'):
                bf.remove(child)

    for pm, fat in assignments:
        if fat is None:
            # unmatched polygon: preserve in its current BOUNDARY FAT folder; do not delete it
            continue
        set_name(pm, find_fat_code(fat['name']) or fat['name'])
        target_bf=None
        for ch in fat['line_folder'].findall(q('Folder')):
            if get_name(ch)=='BOUNDARY FAT':
                target_bf=ch
                break
        if target_bf is None:
            target_bf=ET.SubElement(fat['line_folder'], q('Folder'))
            ET.SubElement(target_bf,q('name')).text='BOUNDARY FAT'
        remove_from_parent(pm)
        target_bf.append(pm)

def _enforce_one_boundary_polygon_per_fat_scoped(root: ET.Element, namespace: str) -> None:
    """Pair boundary polygons only inside their owning LINE/FDT.

    Physical source placement is authoritative. Matching a FAT code globally can
    steal duplicate B06/B07/B08 polygons from a neighbouring LINE/FDT and the
    old global rebuild also dropped unmatched polygons. Preserve every polygon;
    only canonicalize its name when a same-line FAT owns it.
    """
    q = lambda tag: f"{{{namespace}}}{tag}"

    def get_name(element: ET.Element | None) -> str:
        if element is None:
            return ""
        name = direct_child_by_name(element, "name")
        return (name.text or "").strip() if name is not None and name.text else ""

    def set_name(element: ET.Element, value: str) -> None:
        name = direct_child_by_name(element, "name")
        if name is None:
            name = ET.SubElement(element, q("name"))
        name.text = value

    def clean_name(value: object) -> str:
        return re.sub(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]", "", str(value or "")).strip()

    def fat_code(value: object) -> str:
        cleaned = clean_name(value)
        match = find_fat_code(cleaned)
        return match.upper() if match else ""

    def coordinate_values(element: ET.Element) -> list[tuple[float, float]]:
        values: list[tuple[float, float]] = []
        for child in element.iter():
            if local_name(child.tag) != "coordinates" or not child.text:
                continue
            for token in child.text.strip().split():
                parts = token.split(",")
                if len(parts) < 2:
                    continue
                try:
                    values.append((float(parts[0]), float(parts[1])))
                except (TypeError, ValueError):
                    continue
        return values

    def center(element: ET.Element) -> tuple[float, float] | None:
        values = coordinate_values(element)
        if not values:
            return None
        return (sum(x for x, _ in values) / len(values), sum(y for _, y in values) / len(values))

    def distance(left: tuple[float, float], right: tuple[float, float]) -> float:
        return (left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2

    parent_by_id: dict[int, ET.Element] = {}

    def refresh_parents() -> None:
        parent_by_id.clear()
        for parent in root.iter():
            for child in list(parent):
                parent_by_id[id(child)] = parent

    def ancestor_line(element: ET.Element | None) -> ET.Element | None:
        current = element
        while current is not None:
            if local_name(current.tag) == "Folder" and is_line_folder_name(get_name(current)):
                return current
            current = parent_by_id.get(id(current))
        return None

    def move_same_line(element: ET.Element, target: ET.Element) -> None:
        parent = parent_by_id.get(id(element))
        if parent is target or parent is None:
            return
        parent.remove(element)
        target.append(element)
        refresh_parents()

    refresh_parents()
    line_folders = [
        folder
        for folder in root.iter(q("Folder"))
        if is_line_folder_name(get_name(folder))
    ]
    line_ids = {id(folder) for folder in line_folders}

    fats_by_line: dict[int, list[tuple[str, tuple[float, float]]]] = defaultdict(list)
    for folder in root.iter(q("Folder")):
        if clean_name(get_name(folder)).upper() != "FAT":
            continue
        line_folder = ancestor_line(folder)
        if line_folder is None:
            continue
        for placemark in folder.iter(q("Placemark")):
            point = center(placemark)
            code = fat_code(get_name(placemark))
            if point is not None and code:
                fats_by_line.setdefault(id(line_folder), []).append((code, point))

    boundary_records: list[dict] = []
    seen_boundary_ids: set[int] = set()
    for folder in root.iter(q("Folder")):
        if clean_name(get_name(folder)).upper() not in {"BOUNDARY FAT", "FAT AREA"}:
            continue
        line_folder = ancestor_line(folder)
        for placemark in folder.iter(q("Placemark")):
            marker_id = id(placemark)
            if marker_id in seen_boundary_ids:
                continue
            polygon_center = center(placemark)
            if polygon_center is None:
                continue
            seen_boundary_ids.add(marker_id)
            boundary_records.append({
                "pm": placemark,
                "line": line_folder,
                "line_id": id(line_folder) if line_folder is not None else None,
                "name": get_name(placemark),
                "center": polygon_center,
                "used": False,
            })

    # No line context means no truthful rehome target. Preserve the polygon in
    # its existing folder rather than inventing a LINE.
    for line_folder in line_folders:
        line_id = id(line_folder)
        target = direct_child_folder_by_name(line_folder, "BOUNDARY FAT")
        line_boundaries = [item for item in boundary_records if item["line_id"] == line_id]
        fats = fats_by_line.get(line_id, [])
        if target is None and line_boundaries:
            target = ensure_folder(line_folder, namespace, "BOUNDARY FAT")
            refresh_parents()
        if target is None:
            continue

        # First consume an already named boundary with the same FAT code in the
        # same LINE/FDT. Duplicate codes remain preserved as extra polygons.
        for code, fat_point in fats:
            named = [
                item for item in line_boundaries
                if not item["used"] and fat_code(item["name"]) == code
            ]
            if not named:
                continue
            selected = min(named, key=lambda item: distance(fat_point, item["center"]))
            selected["used"] = True
            set_name(selected["pm"], code)
            move_same_line(selected["pm"], target)

        # If a FAT has no named same-line polygon, use only unnamed/invalid
        # same-line candidates. Never consume a named polygon from another FAT.
        for code, fat_point in fats:
            if any(item["used"] and fat_code(item["name"]) == code for item in line_boundaries):
                continue
            unnamed = [
                item for item in line_boundaries
                if not item["used"] and not fat_code(item["name"])
            ]
            if not unnamed:
                continue
            selected = min(unnamed, key=lambda item: distance(fat_point, item["center"]))
            selected["used"] = True
            set_name(selected["pm"], code)
            move_same_line(selected["pm"], target)

        # Normalize folder depth without discarding unmatched polygons. This is
        # a same-line move only; global/cross-line rehome is forbidden.
        for item in line_boundaries:
            move_same_line(item["pm"], target)


# override legacy global final enforcer with the LINE/FDT-scoped implementation
_enforce_one_boundary_polygon_per_fat = _enforce_one_boundary_polygon_per_fat_scoped


def validate_standardized_kmz_result(
    root: ET.Element,
    namespace: str,
    network_type: str | None,
    scope_type: str | None,
    fdt_order_by_placemark: dict[int, int],
    fat_counts_by_fdt: Counter[int],
    *,
    ignore_sling_owned: bool = False,
) -> dict:
    """Post-convert QA for EVA output.

    Read-only validation deliberately runs *after* every standardization pass. It
    reports actionable issues without blocking/download-suppressing a converted KMZ.
    """
    scope = normalize_scope_type(scope_type) or "Cluster"
    network = normalize_text(network_type).upper() or "AE"
    errors: list[dict] = []
    warnings: list[dict] = []
    checks: list[dict] = []
    folder_names: list[str] = []
    folder_records: list[dict] = []
    # Duplicate keys include the full structural folder path so the report
    # locates the offending item (for example LINE A - FDT 1 / SLACK HANGER).
    seen_point: dict[tuple[str, str, str], list[str]] = {}
    seen_line: dict[tuple[str, str, str], list[str]] = {}
    # FAT/FDT/Slack/Pole duplicates are keyed by coordinate alone: the same
    # asset must never appear twice at one point even under a different name.
    seen_coord: dict[tuple[str, str], list[tuple[str, str]]] = {}
    line_items: list[tuple[str, str]] = []
    total_placemarks = 0
    styled_placemarks = 0

    def add(bucket: list[dict], code: str, message: str, *, folder: str = "", name: str = "", severity: str = "warning") -> None:
        bucket.append({"code": code, "message": message, "folder": folder, "name": name, "severity": severity})

    def folder_path(stack: Sequence[str], folder: str) -> str:
        return " / ".join([normalize_text(item) for item in [*stack, folder] if normalize_text(item)])

    def canonical_or_raw_folder(folder: str | None) -> str:
        raw = normalize_text(folder).upper()
        return normalize_text(canonical_standard_folder(raw) or raw).upper()

    def descendant_counts(folder_element: ET.Element) -> tuple[int, int, int]:
        counts = {"placemarks": 0, "polygons": 0, "lines": 0}

        def count(element: ET.Element, sling_owned: bool = False) -> None:
            for descendant in list(element):
                tag = local_name(descendant.tag)
                if tag == "Folder":
                    folder = normalize_text(direct_child_text_by_name(descendant, "name"))
                    child_sling_owned = sling_owned or is_sling_wire_folder_name(folder)
                    if ignore_sling_owned and child_sling_owned:
                        continue
                    count(descendant, child_sling_owned)
                elif tag == "Document":
                    count(descendant, sling_owned)
                elif tag == "Placemark":
                    counts["placemarks"] += 1
                    counts["polygons"] += sum(
                        1 for child in descendant.iter() if local_name(child.tag) == "Polygon"
                    )
                    counts["lines"] += sum(
                        1 for child in descendant.iter() if local_name(child.tag) == "LineString"
                    )

        count(folder_element)
        return counts["placemarks"], counts["polygons"], counts["lines"]

    def ambiguous_folder_hint(folder: str) -> str | None:
        raw = normalize_text(folder).upper()
        if not raw:
            return None
        candidates = {
            "BOUNDARY",
            "BOUNDARY FAT",
            "BOUNDARY CLUSTER",
            "FAT",
            "FDT",
            "CABLE",
            "DISTRIBUTION CABLE",
            *LINE_STANDARD_FOLDERS,
            *FEEDER_AE_FOLDERS,
            *FEEDER_UG_FOLDERS,
            *SUBFEEDER_AE_FOLDERS,
            *SUBFEEDER_UG_FOLDERS,
        }
        best_name = None
        best_score = 0.0
        for candidate in candidates:
            candidate = normalize_text(candidate).upper()
            if not candidate or candidate == raw:
                continue
            score = difflib.SequenceMatcher(None, raw, candidate).ratio()
            if candidate in raw or raw in candidate:
                score = max(score, 0.82)
            if score > best_score:
                best_name, best_score = candidate, score
        return best_name if best_score >= 0.78 else None

    def is_allowed_structural_child(folder: str, stack: Sequence[str]) -> bool:
        raw = normalize_text(folder).upper()
        parent = canonical_or_raw_folder(stack[-1]) if stack else ""
        # The first folder below Document is the project/container name, not an
        # asset folder. Do not report it as an unmapped folder.
        if not stack or WRAPPER_FOLDER_PATTERN.fullmatch(raw):
            return True
        known_folders = {
            normalize_text(item).upper()
            for item in (
                *LINE_STANDARD_FOLDERS,
                *FEEDER_AE_FOLDERS,
                *FEEDER_UG_FOLDERS,
                *SUBFEEDER_AE_FOLDERS,
                *SUBFEEDER_UG_FOLDERS,
                *BOUNDARY_POLYGON_FOLDERS,
            )
            if normalize_text(item)
        }
        if raw in known_folders:
            return True
        if canonical_standard_folder(raw):
            return True
        if is_line_folder_name(raw) or re.fullmatch(r"FDT\s*[-_ ]*\d+", raw, re.IGNORECASE):
            return True
        # These folders legitimately contain FAT/HP code buckets or source groups.
        return parent in {"BOUNDARY", "BOUNDARY FAT", "BOUNDARY CLUSTER", "FAT", "FDT", "HP COVER", "HP UNCOVER"}

    def expected_style_key(placemark: ET.Element, stack: Sequence[str], folder: str) -> str | None:
        if placemark_has_geometry(placemark, {"Polygon"}):
            boundary_folder = nearest_boundary_polygon_folder(list(stack))
            if boundary_folder:
                return "fat_area_polygon" if boundary_folder in FAT_AREA_POLYGON_FOLDERS else "boundary_polygon"
        style_key, _ = style_key_for_placemark(placemark, stack, folder, fdt_order_by_placemark, fat_counts_by_fdt)
        return style_key

    def is_coord_duplicate_family(folder: str | None) -> bool:
        raw = normalize_text(folder).upper()
        canonical = canonical_or_raw_folder(folder)
        if canonical in {"FAT", "FDT", "SLACK HANGER"}:
            return True
        return bool(re.search(r"(?:^|\b)(?:NEW|EXISTING)\s+POLE\b|^POLE$", f"{raw} {canonical}"))

    def coordinate_signature(placemark: ET.Element) -> str:
        values: list[str] = []
        for element in placemark.iter():
            if local_name(element.tag) == "coordinates":
                values.append(re.sub(r"\s+", " ", (element.text or "").strip()))
        return "|".join(values)

    def walk(element: ET.Element, stack: list[str]) -> None:
        nonlocal total_placemarks, styled_placemarks
        for child in list(element):
            tag = local_name(child.tag)
            if tag in {"Document", "Folder"}:
                next_stack = stack
                if tag == "Folder":
                    folder = normalize_text(direct_child_text_by_name(child, "name"))
                    if ignore_sling_owned and is_sling_wire_folder_name(folder):
                        continue
                    path = folder_path(stack, folder)
                    placemark_count, polygon_count, line_count = descendant_counts(child)
                    folder_names.append(folder)
                    folder_records.append({
                        "name": folder,
                        "canonical": canonical_or_raw_folder(folder),
                        "path": path or "Document",
                        "placemarks": placemark_count,
                        "polygons": polygon_count,
                        "lines": line_count,
                    })
                    if not folder:
                        add(warnings, "EMPTY_FOLDER_NAME", "Folder tidak memiliki nama.", folder=path or "Document", severity="warning")
                    elif re.search(r"\b(?:UNTITLED|UNNAMED|NO\s*NAME)\b", folder, re.IGNORECASE):
                        add(errors, "UNTITLED_FOLDER", "Folder memakai nama sementara; gunakan nama folder standar EVA.", folder=path, name=folder, severity="error")
                    else:
                        hint = ambiguous_folder_hint(folder)
                        canonical = canonical_standard_folder(folder)
                        allowed = is_allowed_structural_child(folder, stack)
                        if not canonical and not allowed:
                            if hint:
                                add(errors, "MISNAMED_FOLDER", f"Nama folder {folder} tidak sesuai; kemungkinan yang dimaksud {hint}.", folder=path, name=folder, severity="error")
                            elif placemark_count or polygon_count or line_count:
                                add(warnings, "UNMAPPED_ASSET_FOLDER", "Folder berisi geometry tetapi tidak dikenali sebagai folder asset EVA; item dapat terlewat.", folder=path, name=folder, severity="warning")
                    next_stack = stack + [folder]
                walk(child, next_stack)
                continue
            if tag != "Placemark":
                continue
            total_placemarks += 1
            name = normalize_text(direct_child_text_by_name(child, "name"))
            location = " / ".join([normalize_text(item) for item in stack if normalize_text(item)]) or "Document"
            if not name or re.search(r"\b(?:UNTITLED|UNNAMED|NO\s*NAME)\b", name, re.IGNORECASE):
                issue_code = "UNTITLED_LINE" if placemark_has_line(child) else "UNTITLED_PLACEMARK"
                item_kind = "LineString" if placemark_has_line(child) else "Placemark"
                add(errors, issue_code, f"{item_kind} belum memiliki nama valid; nama kosong/Untitled dapat membuat item terlewat.", folder=location, name=name or "(tanpa nama)", severity="error")
            folder = nearest_standard_folder(stack)
            if not folder:
                # Keep the name/folder issue above, but do not apply asset-specific
                # style/topology rules to vendor metadata outside EVA folders.
                continue
            issue_folder = location
            style_key = expected_style_key(child, stack, folder)
            actual_style = normalized_style_url(direct_child_text_by_name(child, "styleUrl"))
            if style_key:
                expected_spec = STYLE_SPECS.get(style_key)
                if expected_spec:
                    styled_placemarks += 1
                    if actual_style != expected_spec.style_url:
                        add(errors, "STYLE_MISMATCH", f"Style {actual_style or '-'}; harusnya {expected_spec.style_url}.", folder=issue_folder, name=name, severity="error")
            elif placemark_has_geometry(child):
                add(warnings, "STYLE_UNMAPPED", "Geometry belum punya rule style EVA.", folder=issue_folder, name=name)

            signature = coordinate_signature(child)
            if not signature:
                continue
            if placemark_has_point(child):
                if is_coord_duplicate_family(folder):
                    coord_key = (folder, signature)
                    seen_coord.setdefault(coord_key, []).append((issue_folder, name or "(tanpa nama)"))
                else:
                    key = (folder, name.upper(), signature)
                    seen_point.setdefault(key, []).append(issue_folder)
            elif placemark_has_line(child):
                key = (folder, name.upper(), signature)
                seen_line.setdefault(key, []).append(issue_folder)
                line_items.append((folder.upper(), name))

            if folder.upper().startswith("NEW POLE") and (
                re.search(r"\bXXX\b", name, re.IGNORECASE)
                or not STANDARD_POLE_NUMBER_PATTERN.search(name)
            ):
                add(
                    errors,
                    "POLE_NAME_FORMAT",
                    "New Pole tidak sesuai format MR.KODE.P001.",
                    folder=issue_folder,
                    name=name,
                    severity="error",
                )

            upper_folder = folder.upper()
            # Slack Hanger, Sling Wire, and Drop Wire are not AE-only: they
            # occur in the supplied UG templates too. Only explicit pole
            # folders are AE-exclusive in this lightweight topology gate.
            ug_folder = bool(re.search(r"HH|HANDHOLE|SUBDUCT|BORING|SEWER|TRENCH|SUBMARINE", upper_folder))
            ae_only_folder = bool(re.search(r"(?:^|\b)(?:NEW|EXISTING)\s+POLE\b|^POLE$", upper_folder))
            if network == "AE" and ug_folder and placemark_has_geometry(child):
                add(errors, "TOPOLOGY_MISMATCH", f"{folder} hanya sesuai topology UG.", folder=issue_folder, name=name, severity="error")
            elif network == "UG" and ae_only_folder and placemark_has_geometry(child):
                add(errors, "TOPOLOGY_MISMATCH", f"{folder} hanya sesuai topology AE.", folder=issue_folder, name=name, severity="error")

    walk(root, [])

    def tikor_text(signature: str) -> str:
        """KML coordinates are lon,lat[,alt]; show lat,lon and a maps link."""
        parts = [p for p in (signature.split("|")[0] or "").split(",") if p.strip()]
        if len(parts) < 2:
            return ""
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            return ""
        return "%.7f, %.7f" % (lat, lon)

    for (folder, name, signature), locations in seen_point.items():
        if len(locations) > 1:
            where = " + ".join(sorted(set(locations))) or folder
            tikor = tikor_text(signature)
            add(warnings, "DUPLICATE_POINT",
                f"Duplikat point nama+koordinat ({len(locations)} item) di {where}."
                + (f" Tikor: {tikor}." if tikor else "")
                + " Saran: hapus salah satu point dari folder yang tidak dipakai.",
                folder=where, name=name)
    for (folder, signature), entries in seen_coord.items():
        if len(entries) > 1:
            where = " + ".join(sorted({path for path, _ in entries})) or folder
            names = ", ".join(sorted({item for _, item in entries}))
            tikor = tikor_text(signature)
            add(warnings, "DUPLICATE_POINT",
                f"Duplikat {folder} di satu koordinat ({len(entries)} item): {names}."
                + (f" Tikor: {tikor}." if tikor else "")
                + " Saran: hapus salah satu item yang tidak dipakai.",
                folder=where, name=names)
    for (folder, name, signature), locations in seen_line.items():
        if len(locations) > 1:
            where = " + ".join(sorted(set(locations))) or folder
            tikor = tikor_text(signature)
            add(warnings, "DUPLICATE_LINE",
                f"Duplikat LineString nama+jalur ({len(locations)} item) di {where}."
                + (f" Titik awal: {tikor}." if tikor else "")
                + " Saran: hapus salah satu line dari folder yang tidak dipakai.",
                folder=where, name=name)

    def required_families_for(scope_name: str, topology: str) -> list[str]:
        """Required asset families from the user's Cluster/Feeder templates.

        Cluster AE: Pole, FAT, Boundary, FDT, Distribution Cable.
        Cluster UG: Handhole, FAT, Boundary, FDT, Distribution Cable.
        Feeder/Subfeeder AE: Cable, Pole.
        Feeder/Subfeeder UG: Handhole, Cable.
        """
        topology = topology.upper()
        if scope_name == "Cluster":
            if topology == "AE":
                return ["POLE", "FAT", "BOUNDARY", "FDT", "DISTRIBUTION CABLE"]
            if topology == "UG":
                return ["HANDHOLE", "FAT", "BOUNDARY", "FDT", "DISTRIBUTION CABLE"]
            return ["POLE", "HANDHOLE", "FAT", "BOUNDARY", "FDT", "DISTRIBUTION CABLE"]
        if topology == "AE":
            return ["CABLE", "POLE"]
        if topology == "UG":
            return ["HANDHOLE", "CABLE"]
        return ["HANDHOLE", "CABLE", "POLE"]

    def required_folder_family(folder_name: str | None) -> str | None:
        raw = normalize_text(folder_name).upper()
        canonical = canonical_or_raw_folder(raw)
        if canonical == "FAT":
            return "FAT"
        if canonical in {"BOUNDARY", "BOUNDARY FAT"} or raw in {"BOUNDARY", "BOUNDARY FAT", "BOUNDARY CLUSTER"}:
            return "BOUNDARY"
        if canonical == "FDT":
            return "FDT"
        if canonical == "DISTRIBUTION CABLE":
            return "DISTRIBUTION CABLE"
        if canonical == "CABLE":
            return "CABLE"
        if raw == "POLE" or re.search(r"\b(?:NEW|EXISTING)\s+POLE\b", raw):
            return "POLE"
        if re.search(r"(?:\bHH\b|HANDHOLE)", raw):
            return "HANDHOLE"
        return None

    required = required_families_for(scope, network)
    records_by_family: dict[str, list[dict]] = {}
    for record in folder_records:
        family = required_folder_family(record["name"])
        if family:
            records_by_family.setdefault(family, []).append(record)

    def family_count(record: dict, family: str) -> int:
        if family == "BOUNDARY":
            return int(record["polygons"])
        if family in {"CABLE", "DISTRIBUTION CABLE"}:
            return int(record["lines"])
        return int(record["placemarks"])

    missing: list[str] = []
    empty_required = 0
    empty_required_instances = 0
    present_required = 0
    nonempty_required = 0
    for family in required:
        records = records_by_family.get(family, [])
        if not records:
            missing.append(family)
            add(errors, "MISSING_FOLDER", f"Folder wajib {family} belum ada.", folder=family, severity="error")
            continue

        present_required += 1
        relevant_count = sum(family_count(record, family) for record in records)
        detail = {
            "BOUNDARY": "tidak memiliki Polygon",
            "CABLE": "tidak memiliki LineString",
            "DISTRIBUTION CABLE": "tidak memiliki LineString",
        }.get(family, "tidak memiliki Placemark")
        empty_records = [record for record in records if family_count(record, family) == 0]

        # Pole/Handhole are families represented by optional sized buckets. An
        # unused size bucket is harmless when another bucket contains assets.
        if family in {"POLE", "HANDHOLE"} and relevant_count:
            empty_records = []

        # BOUNDARY CLUSTER is a generated wrapper; BOUNDARY FAT/BOUNDARY holds
        # the actual polygons. Do not flag the empty wrapper when geometry exists.
        if family == "BOUNDARY" and relevant_count:
            empty_records = [
                record for record in empty_records
                if normalize_text(record["name"]).upper() != "BOUNDARY CLUSTER"
            ]

        if empty_records:
            empty_required += 1
        if not empty_records:
            nonempty_required += 1
            continue

        empty_required_instances += len(empty_records)
        code = "EMPTY_BOUNDARY_FOLDER" if family == "BOUNDARY" else "EMPTY_CABLE_FOLDER" if family in {"CABLE", "DISTRIBUTION CABLE"} else "EMPTY_REQUIRED_FOLDER"
        names = ", ".join(dict.fromkeys(record["name"] or family for record in empty_records))
        paths = "; ".join(dict.fromkeys(record["path"] for record in empty_records))
        qualifier = "seluruh folder" if not relevant_count else "folder"
        add(
            errors,
            code,
            f"Folder wajib {family} ada tetapi {qualifier} ({names}) kosong / {detail}.",
            folder=paths or family,
            name=family,
            severity="error",
        )

    # Legacy-compatible checks. These are read-only and run against the final
    # standardized tree, so the existing converter remains the authority for
    # edits while this report remains the authority for QA visibility.
    folder_set = {normalize_text(folder).upper() for folder in folder_names if normalize_text(folder)}
    cable_count = 0
    cable_bad_format = 0
    unnamed_count = 0
    for folder, name in line_items:
        if folder not in {"DISTRIBUTION CABLE", "CABLE"}:
            continue
        cable_count += 1
        has_core = bool(re.search(r"\b(?:FO\s*)?(?:12|24|36|48|72|96|120|144|288)\s*C?(?:\s*/\s*(?:1|2|3|4|6|8|12|24)\s*T)?\b", name, re.IGNORECASE))
        has_topology = bool(re.search(r"\b(?:AE|UG|AE&UG|HB)\b", name, re.IGNORECASE))
        has_meter = bool(re.search(r"\b\d+(?:[.,]\d+)?\s*m\b", name, re.IGNORECASE))
        if not (has_core and has_topology and has_meter):
            cable_bad_format += 1
            add(errors, "CABLE_FORMAT", "Nama cable wajib memuat core/tube, topology, dan panjang meter.", folder=folder, name=name, severity="error")
    for folder in folder_names:
        if not normalize_text(folder):
            unnamed_count += 1

    if unnamed_count:
        add(warnings, "EMPTY_FOLDER_NAME", f"{unnamed_count} folder tanpa nama.", severity="warning")
    project_name = normalize_text(direct_child_text_by_name(document_element(root), "name"))
    if scope == "Cluster" and not re.search(r"\b[A-Z]{3}\d{6}\b", project_name, re.IGNORECASE):
        add(errors, "CLUSTER_CODE", "CLUSTER CODE tidak ditemukan / format tidak terbaca; wajib ada untuk CLUSTER.", severity="error")

    all_issues = [*errors, *warnings]
    issue_by_code = Counter(issue.get("code", "UNKNOWN") for issue in all_issues)
    issue_by_folder = Counter(issue.get("folder", "(tanpa folder)") for issue in all_issues)
    error_status = "error" if errors else "warning" if warnings else "valid"

    checks.extend([
        {"label": "Scope", "value": scope, "status": "pass"},
        {"label": "Topology", "value": network, "status": "pass" if network in {"AE", "UG", "AE&UG"} else "warning"},
        {"label": "Placemark", "value": total_placemarks, "status": "pass" if total_placemarks else "error"},
        {"label": "Style EVA", "value": styled_placemarks, "status": "pass" if styled_placemarks else "warning"},
        {"label": "Folder wajib", "value": f"{nonempty_required}/{len(required)}", "status": "pass" if not missing and not empty_required else "error"},
        {"label": "Cable format", "value": f"{max(cable_count - cable_bad_format, 0)}/{cable_count}", "status": "pass" if not cable_bad_format else "error"},
        {"label": "Duplikat point", "value": sum(1 for values in seen_point.values() if len(values) > 1), "status": "pass" if not any(len(values) > 1 for values in seen_point.values()) else "warning"},
        {"label": "Duplikat line", "value": sum(1 for values in seen_line.values() if len(values) > 1), "status": "pass" if not any(len(values) > 1 for values in seen_line.values()) else "warning"},
    ])
    summary = {
        "placemarks": total_placemarks,
        "styled_placemarks": styled_placemarks,
        "required_folders": len(required),
        "missing_folders": len(missing),
        "present_required_folders": present_required,
        "nonempty_required_folders": nonempty_required,
        "empty_required_folders": empty_required,
        "empty_required_folder_instances": empty_required_instances,
        "duplicate_points": sum(1 for values in seen_point.values() if len(values) > 1),
        "duplicate_lines": sum(1 for values in seen_line.values() if len(values) > 1),
        "folder_count": len(folder_set),
        "cable_count": cable_count,
        "cable_bad_format": cable_bad_format,
        "item_count": total_placemarks,
        "info_count": len(checks),
    }
    return {
        "scope_type": scope,
        "network_type": network,
        "status": error_status,
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "issue_by_code": dict(issue_by_code),
        "issue_by_folder": dict(issue_by_folder),
        "summary": summary,
    }

def standardize_kmz_file(
    input_path: str | Path,
    coding_workbook_path: str | Path | None,
    output_path: str | Path,
    network_type: str | None = "AE",
    scope_type: str | None = None,
    coding_records: Sequence[FdtCodingRecord] | None = None,
    source_name: str | None = None,
) -> dict:
    input_path = Path(input_path)
    root, kml_name = read_kmz_root_and_kml_name(input_path)
    namespace = kml_namespace(root)
    # Original Point identity survives renaming (including notes under SLACK HANGER).
    source_fdt_named_points: set[int] = set()
    def remember_fdt_points(node: ET.Element, stack: tuple[str, ...] = ()) -> None:
        for child in node:
            kind = local_name(child.tag)
            if kind == "Folder":
                remember_fdt_points(child, (*stack, normalize_text(direct_child_text_by_name(child, "name"))))
            elif kind == "Document":
                remember_fdt_points(child, stack)
            elif kind == "Placemark" and placemark_has_point(child):
                label = normalize_text(direct_child_text_by_name(child, "name"))
                if (re.match(r"^(?:(?:NEW|EXISTING|EXT)[\s._-]+)?FDT\b", label, re.IGNORECASE)
                        and nearest_standard_folder(stack) not in {"FAT", "CABLE", "DISTRIBUTION CABLE"}):
                    source_fdt_named_points.add(id(child))
    remember_fdt_points(root)
    spreadsheet_warnings: list[str] = []
    if coding_records is not None:
        records = list(coding_records)
        if not records:
            spreadsheet_warnings.append("Database coding FDT kosong; memakai default XXX.")
    elif coding_workbook_path:
        try:
            records = parse_fdt_coding_workbook(coding_workbook_path)
        except Exception as exc:
            records = []
            spreadsheet_warnings.append(f"Spreadsheet coding FDT gagal dibaca; memakai default XXX. Detail: {exc}")
    else:
        records = []
        spreadsheet_warnings.append("Spreadsheet coding FDT belum tersedia; memakai default XXX.")
    lookup_source_path = source_name or input_path
    project_names = candidate_project_names(root, lookup_source_path)
    project_folder_name = preferred_project_folder_name(project_names, lookup_source_path)
    matched_records, matched_from, match_score = select_project_records(records, project_names)

    # Auto-detect scope type from project name if not explicitly set
    detected_scope = normalize_scope_type(scope_type) or ("Subfeeder" if is_subfeeder_project(project_folder_name) else "Feeder" if is_feeder_project(project_folder_name) else "Cluster")
    source_fdt_anchors: list[FdtPlacemarkRef] = []
    unrelated_source_projects: list[ET.Element] = []
    if detected_scope == "Subfeeder" and normalize_text(network_type).upper() in {"AE", "UG"}:
        document = document_element(root)
        expected = normalize_project_folder_name(project_folder_name)
        suffix = expected.partition(" - ")[2]
        source_projects = [child for child in document if local_name(child.tag) == "Folder" and (
            (label := normalize_project_folder_name(direct_child_text_by_name(child, "name"))) in {expected, "SUBFEEDER"}
            or label.startswith("ABD ") and label[4:] in {expected, suffix}
        )]
        if not source_projects:
            # Fallback: vendor root label (e.g. "APD SF KMZ ...") differs from filename identity.
            # Accept exactly one non-asset root Folder that is uncoded or carries the filename code.
            filename_codes = {m.group(0).upper() for m in PROJECT_CODE_PATTERN.finditer(expected)}
            has_loose_root_geometry = any(local_name(child.tag) == "Placemark" for child in document)
            if len(filename_codes) == 1 and not has_loose_root_geometry:
                def _eligible_source_root(child: ET.Element) -> bool:
                    if local_name(child.tag) != "Folder":
                        return False
                    label = normalize_project_folder_name(direct_child_text_by_name(child, "name"))
                    if not label or nearest_standard_folder((label,)) is not None:
                        return False
                    if re.match(r"^(?:LINE|FDT|CABLE|POLE|HP|FAT|SLACK|SLING)\b", label):
                        return False
                    codes = {m.group(0).upper() for m in PROJECT_CODE_PATTERN.finditer(label)}
                    return not codes or codes == filename_codes
                fallback_projects = [child for child in document if _eligible_source_root(child)]
                if len(fallback_projects) == 1:
                    source_projects = fallback_projects
        if len(source_projects) != 1:
            raise ValueError("Cannot identify one source Subfeeder project for FDT Slack")
        unrelated_source_projects = [child for child in document if local_name(child.tag) == "Folder"
                                     and child is not source_projects[0]
                                     and PROJECT_CODE_PATTERN.search(normalize_text(direct_child_text_by_name(child, "name")))]
        for child in unrelated_source_projects:
            document.remove(child)
        def capture_fdt(node: ET.Element, stack: tuple[str, ...] = ()) -> None:
            for child in node:
                kind = local_name(child.tag)
                if kind == "Folder":
                    capture_fdt(child, (*stack, normalize_text(direct_child_text_by_name(child, "name"))))
                elif kind == "Placemark" and placemark_has_point(child):
                    label = normalize_text(direct_child_text_by_name(child, "name"))
                    if (re.search(r"\b(?:SURVEY|NOTE)\b", label, re.I)
                            or any(local_name(meta.tag) == "Data" and meta.get("name") == "SURVEY_NOTE_NOT_EQUIPMENT"
                                   for meta in child.iter())
                            or nearest_standard_folder(stack) in {
                                "FAT", "CABLE", "DISTRIBUTION CABLE", "SLACK HANGER",
                            }):
                        continue
                    explicit = re.match(r"^(?:(?:NEW|EXISTING|EXT)[\s._-]+)?FDT(?:\b|[._-])", label, re.I)
                    if explicit or (nearest_standard_folder(stack) == "FDT" and looks_like_fdt_placemark(child, stack)):
                        # Snapshot before the organizer renames/moves Points and before cleanup deletes them.
                        frozen = ET.fromstring(ET.tostring(child))
                        source_fdt_anchors.append(FdtPlacemarkRef(frozen, list(stack), 1, label))
        capture_fdt(source_projects[0])
    if normalize_scope_type(detected_scope) == "Subfeeder":
        filename_cluster_name = extract_cluster_name_from_kmz_filename(lookup_source_path)
        sr = select_project_records(records, [filename_cluster_name])
        if not sr[0]:
            sr = (select_records_by_substring(records, filename_cluster_name), filename_cluster_name, 0.0)
        if sr[0]:
            matched_records = list(sr[0])
            matched_from = sr[1]
            match_score = sr[2]
        else:
            matched_records = []
            matched_from = filename_cluster_name
            match_score = 0.0
    is_feeder = is_feeder_like_scope(detected_scope)
    line_local_ug = is_cluster_ug_scope(detected_scope, network_type)
    cluster_ae_sling = (
        normalize_scope_type(detected_scope) == "Cluster"
        and normalize_text(network_type).upper() == "AE"
    )
    cluster_ae_fat_slack = (
        normalize_scope_type(detected_scope) == "Cluster"
        and normalize_text(network_type).upper() == "AE"
    )
    feeder_ae_sling = is_feeder and normalize_text(network_type).upper() == "AE"
    quarantined_sling = quarantine_cluster_ae_sling_folders(root) if cluster_ae_sling or feeder_ae_sling else []

    folder_changes: list[dict] = []
    initial_fdt_placemarks: list[FdtPlacemarkRef] = []
    walk_kml_for_standardization(root, [], namespace, folder_changes, initial_fdt_placemarks)
    wrapper_changes: list[dict] = []
    using_default_coding = False
    if not matched_records:
        sharing_codes = _shared_fdt_codes_for_fallback(root, initial_fdt_placemarks)
        sharing_records = _fdt_sharing_records_from_codes(sharing_codes, project_folder_name)
        matched_records = sharing_records or default_fdt_coding_records(initial_fdt_placemarks, project_folder_name)
        matched_from = matched_from or project_folder_name
        match_score = 0.0
        using_default_coding = True
    feeder_olt_match_for_project = feeder_olt_lookup(root, project_names) if is_feeder and normalize_scope_type(detected_scope) != "Subfeeder" else None
    if is_feeder:
        base_records = matched_records or [None]
        is_subfeeder_scope = normalize_scope_type(detected_scope) == "Subfeeder"
        matched_records = [
            feeder_pole_record(
                record,
                feeder_olt_match_for_project,
                project_names,
                is_subfeeder=is_subfeeder_scope,
            )
            for record in base_records
        ]
    matched_record = matched_records[0] if matched_records else None
    global _single_fdt_mode
    _single_fdt_mode = len(initial_fdt_placemarks) == 1

    name_changes: list[dict] = []
    # TL is a pole-owner alias, not a global name hint (Slack/FDT/HP also
    # consume the shared partner patterns). NEW poles retain their MR path.
    if normalize_text(network_type).upper() == "AE":
        for ref in collect_organizer_placemarks(root, []):
            pole = ref.element
            owner = nearest_standard_folder(ref.folder_stack)
            if (_pole_kind_for_folder(owner) != "EXT"
                    or any(is_sling_wire_folder_name(part) for part in ref.folder_stack)
                    or not placemark_has_point(pole) or placemark_has_line(pole)
                    or placemark_polygon_coordinates(pole)):
                continue
            name_element = direct_child_by_name(pole, "name")
            if name_element is not None:
                old_name = name_element.text or ""
                new_name = re.sub(r"^(\s*(?:EXT[.\s_-]*)?)TL(?=[.\s_-]|$)", r"\1TLKM", old_name, flags=re.IGNORECASE)
                if new_name != old_name:
                    name_element.text = new_name
                    name_changes.append({"from": old_name, "to": new_name, "folder": owner})
    organizer_changes: list[dict] = []
    subfeeder_emr_slack_created = 0
    if is_feeder:
        organize_feeder_placemarks(root, matched_records, namespace, organizer_changes, project_folder_name, network_type, detected_scope)
        # Capture the vendor keyword before pole names are rewritten. The helper
        # is explicitly Subfeeder-only and skips sources with Slack within 5 m.
        subfeeder_emr_slack_created = create_subfeeder_slack_hanger_from_emr_keywords(
            root,
            namespace,
            organizer_changes,
            detected_scope,
            project_folder_name,
        )
        # Feeder/Subfeeder pole numbering depends on standard pole folders.
        # Organize first, then rewrite every pole from a fresh counter so old
        # names like P012/P013 cannot survive or seed the sequence.
        standardize_project_placemark_names(root, [], matched_record, namespace, name_changes, is_feeder=is_feeder, scope_type=detected_scope)
    else:
        standardize_project_placemark_names(root, [], matched_record, namespace, name_changes, is_feeder=is_feeder, scope_type=detected_scope)
        organize_kmz_placemarks(
            root,
            matched_records,
            namespace,
            organizer_changes,
            project_folder_name,
            network_type,
            line_local_ug,
            cluster_ae_sling,
        )
        rebuild_raw_line_wrapper_folders(root, namespace, organizer_changes)
        
        # Add final pass to consolidate duplicate line folders (e.g. "LINE A" and "LINE A - FDT 1")
        if not is_feeder:
            project_folder = find_project_container(root, namespace, project_folder_name)
            if project_folder is not None:
                consolidate_duplicate_line_folders(project_folder, namespace, organizer_changes)

    unwrap_wrapper_folders(root, wrapper_changes)
    # Strictly Cluster UG. AE and Feeder/Subfeeder retain prior behavior.
    subduct_32_28_cloned = 0
    tali_pancing_cloned = subduct_40_34_cloned = 0
    if line_local_ug:
        project_folder = find_project_container(root, namespace, project_folder_name)
        if project_folder is not None:
            ensure_line_standard_folders(project_folder, namespace, network_type)
        subduct_32_28_cloned = clone_distribution_cable_lines_to_empty_subduct_32_28(
            root, namespace, organizer_changes
        )
        # Run after LINE-local SUBDUCT setup; before styles so copied lines
        # receive #eva_tali_pancing. Only SUBDUCT 40/34 is a TALI source.
        tali_pancing_cloned, subduct_40_34_cloned = clone_subduct_40_34_lines_to_tali_pancing(
            root, namespace, organizer_changes
        )

    post_organize_folder_changes: list[dict] = []
    fdt_placemarks: list[FdtPlacemarkRef] = []
    walk_kml_for_standardization(root, [], namespace, post_organize_folder_changes, fdt_placemarks)
    folder_changes.extend(post_organize_folder_changes)
    output_fdt_placemarks = list(fdt_placemarks)
    if normalize_scope_type(detected_scope) == "Feeder":
        # Mainfeeder flattens FDT folders; retain surviving source identities
        # for output names only, never feed them back into routing/style passes.
        live_elements = {id(element) for element in root.iter()}
        seen_elements = {id(ref.element) for ref in output_fdt_placemarks}
        output_fdt_placemarks.extend(
            ref for ref in initial_fdt_placemarks
            if id(ref.element) in live_elements and id(ref.element) not in seen_elements
        )
    if len(output_fdt_placemarks) > 1:
        # Derive conversion-only codes after physical routing. Keep the master
        # records/cluster initials intact and retain source-code-first mapping.
        output_records = []
        for order, record in enumerate(matched_records, 1):
            prefix, dot, fdt_id = record.fdt_code.rpartition(".")
            code = record.fdt_code
            if prefix and dot and fdt_id.isdigit() and not re.search(r"-\d+$", prefix):
                code = f"{prefix}-{order}.{fdt_id}"
            output_records.append(replace(record, fdt_code=code))
        output_by_record = {id(record): output for record, output in zip(matched_records, output_records)}
        for placemark in output_fdt_placemarks:
            record = record_for_fdt_code(matched_records, existing_fdt_code_from_name(placemark.current_name))
            output = output_by_record[id(record)] if record else record_for_fdt_placemark(output_records, placemark)
            if output is not None:
                placemark.current_name = output.fdt_code
        matched_records = output_records

    if not is_feeder:
        # Cluster AE Sling geometry is still quarantined here. Defer its one
        # authoritative NEW-pole pass until the original Sling tree is restored.
        if not cluster_ae_sling:
            standardize_cluster_new_poles_after_organization(root, matched_record, namespace, name_changes)
        _rehome_raw_fdt_boundary_cover(root, namespace)
        _enforce_one_boundary_polygon_per_fat(root, namespace)
        create_missing_slack_hanger_from_fat(
            root,
            namespace,
            organizer_changes,
            per_fat_dedupe=cluster_ae_fat_slack,
        )
        create_missing_slack_hanger_from_fdt(root, namespace, fdt_placemarks, organizer_changes)
    standardize_slack_hanger_placemark_names(root, [], namespace, name_changes)
    sling_wire_placemarks_renamed = standardize_sling_wire_placemark_names(
        root, [], namespace, name_changes
    )
    subduct_placemarks_renamed = standardize_subduct_placemark_names(
        root, [], namespace, name_changes
    )
    fdt_changes, warnings = standardize_fdt_placemarks(
        output_fdt_placemarks if len(output_fdt_placemarks) > 1 else fdt_placemarks,
        matched_records,
        namespace,
    )
    fat_name_changes: list[dict] = []
    standardize_fat_placemark_names(root, [], matched_records, namespace, fat_name_changes)
    name_changes.extend(fat_name_changes)
    if not is_feeder:
        # BOUNDARY FAT can be rehomed/deduplicated after the initial organizer
        # pass. Rebuild spatial refs from that final tree before routing HP so a
        # point follows its actual containing polygon, even across LINE/FDT.
        project_folder = find_project_container(root, namespace, project_folder_name)
        if project_folder is not None:
            final_placemark_refs = collect_organizer_placemarks(root, [])
            final_fat_areas = collect_organizer_fat_areas(final_placemark_refs, [])
            assign_all_hp_cover_to_boundary(project_folder, namespace, final_fat_areas, organizer_changes)
    # Route finalization can move HPs between COVER/UNCOVER. Number temporary
    # names only after that movement, using one collision-free sequence.
    normalize_untitled_hp_placemark_names(root, namespace, name_changes)
    number_duplicate_hp_place_names(root, namespace, name_changes)
    warnings = [*spreadsheet_warnings, *warnings]
    if using_default_coding:
        warnings.insert(0, "Coding FDT tidak match di spreadsheet; memakai default XXX.")
    fat_counts_by_fdt = collect_fat_counts_by_fdt(root, [])
    fat_counts_by_line_fdt = collect_fat_counts_by_line_fdt(root, [])
    distribution_boq_totals = collect_distribution_cable_boq_totals(root, [], fat_counts_by_line_fdt)
    # Count SLACK HANGER placemarks for Feeder cable description
    feeder_slack_count = 0
    if is_feeder:
        def _count_slack_in_folders(elem, in_slack: bool):
            nonlocal feeder_slack_count
            for ch in list(elem):
                ch_name = local_name(ch.tag)
                if ch_name in {"Document", "Folder"}:
                    sub_in_slack = in_slack
                    if ch_name == "Folder":
                        folder_name_text = normalize_text(direct_child_text_by_name(ch, "name") or "")
                        if canonical_standard_folder(folder_name_text) == "SLACK HANGER":
                            sub_in_slack = True
                    _count_slack_in_folders(ch, sub_in_slack)
                elif ch_name == "Placemark" and in_slack:
                    feeder_slack_count += 1
        _count_slack_in_folders(root, False)
    # Slack must exist before cable totals are calculated, including reconversion.
    if detected_scope == "Subfeeder" and normalize_text(network_type).upper() in {"AE", "UG"}:
        project = find_project_container(root, namespace, project_folder_name)
        if project is not None:
            create_project_slack_from_fdt(
                project, namespace, source_fdt_anchors, organizer_changes,
                source_fdt_named_points,
            )
    distribution_cable_changes: list[dict] = []
    feeder_cable_context: dict | None = None
    if is_feeder:
        fdt_entries, slack_entries = _collect_feeder_geometry_indexes(root)
        feeder_olt_match = feeder_olt_match_for_project
        feeder_cable_context = {
            "fdt_entries": fdt_entries,
            "slack_entries": slack_entries,
            "used_fdt_codes": set(),
            "used_slack_ids": set(),
            "cable_fdt_code_by_id": _subfeeder_cable_fdt_code_map(root, fdt_entries) if detected_scope == "Subfeeder" else {},
            "matched_olt_code": normalize_text((feeder_olt_match or {}).get("olt_code")).upper(),
            "matched_olt_hostname": normalize_text((feeder_olt_match or {}).get("olt_hostname")).upper(),
            "project_segment_number": feeder_segment_number(project_names),
        }
    standardize_distribution_cable_placemarks(
        root,
        [],
        matched_records,
        fat_counts_by_line_fdt,
        fat_counts_by_fdt,
        distribution_boq_totals,
        namespace,
        distribution_cable_changes,
        is_feeder,
        feeder_slack_count,
        feeder_cable_context,
        detected_scope,
        network_type,
    )
    ensure_standard_style_definitions(root, namespace)
    fdt_order_by_placemark = {id(placemark.element): placemark.source_order for placemark in fdt_placemarks}
    style_changes: list[dict] = []
    standardize_kml_styles(root, [], namespace, fdt_order_by_placemark, fat_counts_by_fdt, style_changes)
    description_removed = remove_empty_mapmarker_descriptions(root)
    description_removed += remove_non_rule_descriptions(root)

    # Final pass: hapus LINE folders dan FDT group folders yang kosong
    # Preserve all standard subfolders inside LINE (poles, cables, etc.) so aggressive prune
    # does not remove folders that were created by ensure_line_standard_folders but stayed empty.
    if detected_scope == "Cluster":
        _final_preserved = set(line_standard_folders_for_network(network_type)) | {"FDT", "BOUNDARY CLUSTER"}
    else:
        _final_preserved = set(feeder_standard_folders_for_network(network_type, detected_scope))
    prune_empty_folders(root, set(), _final_preserved, aggressive=True)



    # Final mandatory cluster parent folders: ensure after aggressive prune, right before write.
    final_project_folder = find_project_container(root, namespace, project_folder_name)
    
    # ── Normalise Document name + root folder name → CLUSTER_CODE - CLUSTER_NAME ──
    document = document_element(root)
    for child in list(document):
        if local_name(child.tag) == 'name':
            child.text = project_folder_name
            break
    if final_project_folder is not None:
        for child in list(final_project_folder):
            if local_name(child.tag) == 'name':
                if normalize_text(child.text or '') != normalize_text(project_folder_name):
                    child.text = project_folder_name
                break
    
    if detected_scope == "Cluster":
        ensure_folder(final_project_folder, namespace, "BOUNDARY CLUSTER")
        ensure_folder(final_project_folder, namespace, "FDT")
        _reorder_cluster_parent_folders(final_project_folder, namespace, network_type)
        ensure_line_standard_folders(final_project_folder, namespace, network_type)
        reorder_cluster_line_children(final_project_folder, namespace, network_type)
    else:
        for folder_name in feeder_standard_folders_for_network(network_type, detected_scope):
            ensure_folder(final_project_folder, namespace, folder_name)
        reorder_feeder_project_children(final_project_folder, namespace, detected_scope, network_type)
    # Within both NEW and EXISTING groups, HH80 and HH40 share one contiguous
    # HH sequence. Number HH80 first, then continue in HH40. HH20 uses its own
    # HHPIT sequence, and NEW/EXISTING counters remain independent.
    standardize_handhole_placemark_names(root, matched_record, namespace, name_changes, detected_scope)
    if normalize_text(network_type).upper() == "UG":
        ensure_ug_hh80_fdt_only(root, namespace, name_changes, matched_record, fdt_placemarks)
        ensure_ug_hh40_and_order_names(root, namespace, name_changes, matched_record)
        if detected_scope == "Cluster" and final_project_folder is not None:
            reorder_cluster_line_children(final_project_folder, namespace, network_type)
        # HH80/HH40 can be created after the first style pass. Re-run the
        # idempotent resolver so generated HH never retains cloned FAT/FDT icons.
        standardize_kml_styles(root, [], namespace, fdt_order_by_placemark, fat_counts_by_fdt, style_changes)

    # Final name pass: folder and every descendant Placemark use literal T-WAY.
    tway_placemarks_renamed = force_tway_folder_and_placemark_names(root, namespace)

    if feeder_ae_sling:
        restore_quarantined_sling_folders(root, quarantined_sling)
    if cluster_ae_sling and quarantined_sling:
        _sling_moves, _sling_renamed = route_quarantined_cluster_ae_sling(
            root,
            namespace,
            project_folder_name,
            quarantined_sling,
            organizer_changes,
            name_changes,
            style_changes,
        )
        sling_wire_placemarks_renamed += _sling_renamed

    if cluster_ae_sling:
        # Run once for every Cluster AE project, including projects with no
        # source Sling wrapper. Restored branches, when present, are visible.
        standardize_cluster_new_poles_after_organization(
            root,
            matched_record,
            namespace,
            name_changes,
            strict_parent_fdt_order=True,
        )
        _rehome_cluster_poles_to_physical_lines(root, namespace, organizer_changes)
        if final_project_folder is not None:
            ensure_line_standard_folders(final_project_folder, namespace, network_type)
            reorder_cluster_line_children(final_project_folder, namespace, network_type)

    qc_result = ai_qc_standardize_result(
        root,
        namespace,
        str(input_path),
        str(output_path),
        str(input_path),
        organizer_changes,
        ignore_sling_owned=cluster_ae_sling,
    )

    if detected_scope == "Subfeeder":
        # FDT points are useful for coding/cable routing, not Subfeeder output.
        # ponytail: preserve non-FDT descendants by lifting them one level;
        # add destination remapping only if a source embeds mixed asset folders.
        fdt_ids = {id(ref.element) for ref in (*initial_fdt_placemarks, *fdt_placemarks)}
        def remove_subfeeder_fdt(parent: ET.Element, stack: tuple[str, ...] = ()) -> None:
            for child in list(parent):
                kind = local_name(child.tag)
                if kind == "Folder":
                    name = normalize_text(direct_child_text_by_name(child, "name"))
                    remove_subfeeder_fdt(child, (*stack, name))
                    if re.match(r"^(?:(?:NEW|EXISTING|EXT)[\s._-]+)?FDT\b", name, re.IGNORECASE):
                        label = direct_child_by_name(child, "name")
                        if (child.attrib or normalize_text(child.text) or normalize_text(child.tail)
                                or label is None or label.attrib or list(label)
                                or normalize_text(label.tail)
                                or any(local_name(item.tag) not in {"name", "Folder", "Placemark"}
                                       for item in child)):
                            raise ValueError(f"FDT folder has metadata that cannot be discarded: {name}")
                        index = list(parent).index(child)
                        parent.remove(child)
                        for descendant in list(child):
                            if local_name(descendant.tag) in {"Folder", "Placemark"}:
                                parent.insert(index, descendant)
                                index += 1
                elif kind == "Placemark" and placemark_has_point(child):
                    source_folder = nearest_standard_folder(stack)
                    label = normalize_text(direct_child_text_by_name(child, "name"))
                    explicit_fdt_alias = re.match(
                        r"^(?:(?:NEW|EXISTING|EXT)[\s._-]+)?FDT\b", label, re.IGNORECASE,
                    )
                    if (id(child) in source_fdt_named_points
                            or id(child) in fdt_ids
                            or (source_folder == "FDT" and looks_like_fdt_placemark(child, stack))
                            or (explicit_fdt_alias and source_folder not in {
                                "FAT", "CABLE", "DISTRIBUTION CABLE",
                            })):
                        parent.remove(child)
                elif kind == "Document":
                    remove_subfeeder_fdt(child, stack)
        if final_project_folder is None:
            raise ValueError("Subfeeder project folder missing before FDT output cleanup")
        remove_subfeeder_fdt(final_project_folder)
        for sibling in unrelated_source_projects:
            document.append(sibling)

    write_kmz_with_replaced_kml(
        input_path,
        output_path,
        kml_name,
        serialize_kml(root, protect_sling_owned=cluster_ae_sling),
    )

    # Validate the serialized artifact, not only the pre-write in-memory tree.
    # This catches namespace/ZIP/writer changes before the report reaches the UI.
    final_root, _ = read_kmz_root_and_kml_name(output_path)
    final_namespace = kml_namespace(final_root)
    validation_report = validate_standardized_kmz_result(
        final_root,
        final_namespace,
        network_type,
        detected_scope,
        fdt_order_by_placemark,
        fat_counts_by_fdt,
        ignore_sling_owned=cluster_ae_sling,
    )
    for issue in validation_report["errors"]:
        warnings.append(f"POST-QA [{issue['code']}] {issue['message']}")
    for issue in validation_report["warnings"]:
        warnings.append(f"POST-QA [{issue['code']}] {issue['message']}")

    return {
        "validation_report": validation_report,
        "output_path": str(output_path),
        "project_candidates": project_names,
        "project_folder_name": project_folder_name,
        "scope_type": detected_scope,
        "network_type": normalize_text(network_type).upper() or "AE",
        "matched_project": matched_from,
        "match_score": round(match_score, 3),
        "matched_site": matched_record.site_name if matched_record else "",
        "matched_sheet": matched_record.sheet_name if matched_record else "",
        "matched_row": matched_record.row_number if matched_record else None,
        "default_fdt_coding": using_default_coding,
        "available_fdt_code_count": 0 if using_default_coding else len(matched_records),
        "available_fdt_codes": [record.to_dict() for record in matched_records],
        "fdt_placemarks": len(fdt_placemarks),
        "fdt_renamed": len(fdt_changes),
        "folder_renamed": len(folder_changes),
        "name_standardized": len(name_changes),
        "placemark_organized": len(organizer_changes),
        "distribution_cable_standardized": len(distribution_cable_changes),
        "style_standardized": len(style_changes),
        "tway_placemarks_renamed": tway_placemarks_renamed,
        "sling_wire_placemarks_renamed": sling_wire_placemarks_renamed,
        "subduct_placemarks_renamed": subduct_placemarks_renamed,
        "subduct_32_28_cloned": subduct_32_28_cloned,
        "tali_pancing_cloned": tali_pancing_cloned,
        "subduct_40_34_cloned": subduct_40_34_cloned,
        "subfeeder_emr_slack_created": subfeeder_emr_slack_created,
        "description_removed": description_removed,
        "folder_unwrapped": len(wrapper_changes),
        "fat_counts_by_fdt": dict(sorted(fat_counts_by_fdt.items())),
        "fat_counts_by_line_fdt": {f"{line}-{fdt}": count for (line, fdt), count in sorted(fat_counts_by_line_fdt.items())},
        "distribution_boq_totals": {
            f"{line}-{fdt}-{capacity}": {
                "quantity": int(value.get("quantity", 0) or 0),
                "cell": value.get("cell", ""),
            }
            for (line, fdt, capacity), value in sorted(distribution_boq_totals.items())
        },
        "fdt_changes": fdt_changes,
        "folder_changes": folder_changes,
        "name_changes": name_changes,
        "organizer_changes": organizer_changes,
        "distribution_cable_changes": distribution_cable_changes,
        "style_changes": style_changes,
        "wrapper_changes": wrapper_changes,
        "warnings": warnings,
        "qc_result": qc_result,
    }
