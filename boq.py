from __future__ import annotations

import json
import math
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from pyproj import Geod

import logging

_BOQ_LOGGER = logging.getLogger(__name__)


def _unwrap_formula_value(value):
    """Convert a `formulas` library result into a plain Python scalar.

    The library returns numpy/Array wrappers even for single-cell results.
    We unwrap to the inner Python value so it can be assigned directly to an
    openpyxl cell. None on failure.
    """
    if value is None:
        return None
    # Ranges-style object: has a `.value` that is a numpy 2D array
    inner = getattr(value, "value", None)
    if inner is not None:
        value = inner
    # Now value may be a numpy array or scalar
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return item_method()
        except (ValueError, TypeError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            data = tolist()
        except (ValueError, TypeError):
            data = None
        if data is None:
            return None
        # Flatten 2D 1x1
        while isinstance(data, list) and len(data) == 1:
            data = data[0]
        return data
    return value


def freeze_workbook_cached_values(output_path) -> int:
    """Evaluate every formula in the saved workbook and replace each formula
    cell with its evaluated literal value.

    This is required so that Excel "Protected View" displays values
    immediately, without the user having to click "Enable Editing" first.
    Excel's Protected View does not recalculate formulas ??? it only renders
    values that are already cached in the file. openpyxl does not write
    cached results, so we evaluate the formulas in Python and replace them.

    Returns the number of cells whose formula was replaced with a value.
    Errors are logged at WARNING level; the file is left as-is on failure.
    """
    output_path = Path(output_path)
    try:
        import formulas  # type: ignore
    except ImportError:
        _BOQ_LOGGER.warning(
            "formulas library not installed; BOQ output will require Enable Editing "
            "in Excel Protected View to display calculated values. "
            "Install with: pip install formulas"
        )
        return 0

    try:
        xl_model = formulas.ExcelModel().loads(str(output_path)).finish()
        solution = xl_model.calculate()
    except Exception as exc:
        _BOQ_LOGGER.warning(
            "Failed to evaluate workbook formulas in %s: %s. "
            "Output may show blank cells in Excel Protected View.",
            output_path.name,
            exc,
        )
        return 0

    workbook = load_workbook(output_path, data_only=False)
    fname = output_path.name
    # `formulas` library produces keys like:
    #   '[file.xlsx]SHEET NAME'!A1
    # The filename keeps its original case; the sheet name is uppercased.
    fname_for_key = fname

    cells_updated = 0
    for sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        sheet_upper = sheet_name.upper()
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type != "f":
                    continue
                target_key = f"'[{fname_for_key}]{sheet_upper}'!{cell.coordinate}"
                evaluated = solution.get(target_key)
                if evaluated is None:
                    # Fallbacks: filename uppercased, or no quotes
                    evaluated = solution.get(
                        f"'[{fname_for_key.upper()}]{sheet_upper}'!{cell.coordinate}"
                    ) or solution.get(
                        f"[{fname_for_key}]{sheet_upper}!{cell.coordinate}"
                    )
                if evaluated is None:
                    continue
                value = _unwrap_formula_value(evaluated)
                if value is None:
                    continue
                # Skip Excel error sentinels (#REF!, #DIV/0!, etc.) and "empty"
                if isinstance(value, str):
                    stripped = value.strip()
                    if not stripped or stripped.startswith("#") or stripped.lower() == "empty":
                        continue
                # Replace formula with its literal value. Excel Protected
                # View can render literals immediately.
                try:
                    cell.value = value
                    cells_updated += 1
                except (ValueError, TypeError) as exc:
                    _BOQ_LOGGER.debug(
                        "Could not write evaluated value for %s!%s: %s",
                        sheet_name,
                        cell.coordinate,
                        exc,
                    )

    if cells_updated:
        workbook.save(output_path)
        _BOQ_LOGGER.info(
            "Cached %d formula result(s) in %s for Protected-View display.",
            cells_updated,
            fname,
        )
    return cells_updated

from converter import (
    children_by_name,
    direct_child_by_name,
    direct_child_text_by_name,
    is_kml_path,
    local_name,
    normalize_input_paths,
    parse_kml_coordinates,
    read_kml_root,
)
BOQ_RULES_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "rules.json"


def load_boq_rules_config() -> dict:
    if not BOQ_RULES_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(BOQ_RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def boq_rule(scope: str) -> dict:
    return dict((load_boq_rules_config().get("boq") or {}).get(scope, {}))


def apply_boq_rule_cells(workbook, scope: str, pipeline_logger=None) -> None:
    rule = boq_rule(scope)
    bom = rule.get("bom") or {}
    nro = rule.get("nro") or {}
    bom_sheet = bom.get("sheet")
    if bom_sheet and bom_sheet in workbook.sheetnames:
        ws = workbook[bom_sheet]
        for key in ("accessory", "splicing"):
            cell = bom.get(f"{key}_cell")
            formula = bom.get(f"{key}_formula")
            if cell and formula:
                ws[cell] = formula
                pipeline_log(pipeline_logger, "INFO", f"BOQ rule {scope}: {cell} = {formula}")
    nro_sheet = nro.get("sheet")
    if nro_sheet and nro_sheet in workbook.sheetnames:
        ws = workbook[nro_sheet]
        label_cell = nro.get("name_label_cell")
        label = nro.get("name_label")
        if label_cell and label:
            ws[label_cell] = label
            pipeline_log(pipeline_logger, "INFO", f"BOQ rule {scope}: {label_cell} = {label}")


BOQ_SHEET_NAME = "BoM CLUSTER AE"
BOQ_SHEET_NAME_UG = "BoM CLUSTER UG"
BOQ_NRO_SHEET_NAME = "BoQ NRO Cluster"
BOQ_NRO_OLT_CELL = "O2"
BOQ_NRO_CLUSTER_CELL = "O3"
BOQ_NRO_HP_COVER_CELL = "O5"
BOQ_NRO_CABLE_LOOP_HOLDER_CELL = "D225"
BOQ_NRO_AE_CLEAR_CELLS = ("D162", "E162")
BOQ_NRO_AE_CLEAR_SERVICE_CELLS = tuple(f"E{row}" for row in range(225, 242))
BOQ_NRO_PREVIEW_HEADER_ROW = 6
BOQ_NRO_PREVIEW_COLUMNS = ("N", "B", "D", "E")

# Feeder BOQ constants
BOQ_FEEDER_SHEET_NAME = "BoM FEEDER or SUB FEEDER"
BOQ_FEEDER_NRO_SHEET_NAME = "BoQ NRO All Feeder"
BOQ_FEEDER_NRO_OLT_CELL = "O2"
BOQ_FEEDER_NRO_CLUSTER_CELL = "O3"
BOQ_FEEDER_NRO_CLEAR_SERVICE_CELLS = tuple(f"E{row}" for row in range(225, 242))

# Feeder BoM cell mapping (single column C, no FDT grid)
BOQ_FEEDER_CABLE_ROW_MAP = {
    "24C/2T": 2,
    "36C/3T": 3,
    "48C/4T": 4,
    "72C/6T": 5,
    "96C/8T": 6,
    "144C/12T": 7,
    "288C/24T": 8,
}
BOQ_FEEDER_CABLE_TOTAL_ROW_MAP = {
    "24C/2T": 9,
    "36C/3T": 10,
    "48C/4T": 11,
    "72C/6T": 12,
    "96C/8T": 13,
    "144C/12T": 14,
    "288C/24T": 15,
}
BOQ_FEEDER_SLACK_ROW_MAP = {
    "24C/2T": 16,
    "36C/3T": 17,
    "48C/4T": 18,
    "72C/6T": 19,
    "96C/8T": 20,
    "144C/12T": 21,
    "288C/24T": 22,
}
BOQ_FEEDER_JOINT_CLOSURE_INLINE_ROW_MAP = {
    24: 23,
    36: 24,
    48: 25,
    72: 26,
    96: 27,
    144: 28,
    288: 29,
}
BOQ_FEEDER_POLE_ROW_MAP = {
    "NEW POLE 7-4": 49,
    "NEW POLE 7-3": 50,
    "NEW POLE 7-2.5": 51,
    "NEW POLE 7-5": 52,
    "NEW POLE 9-4": 53,
    "NEW POLE 9-5": 54,
    "EXISTING POLE LN": 55,
    "EXISTING POLE TELKOM": 56,
    "EXISTING POLE PEMKOT": 57,
    "EXISTING POLE SIMS": 58,
    "EXISTING POLE EMR": 59,
}
BOQ_FEEDER_STRAND_WIRE_ROW = 48
BOQ_FEEDER_TOTAL_CORE_SF_ROW = 63
BOQ_FEEDER_TOTAL_CORE_PER_FDT = 5


def _is_feeder_like_scope(scope_type: str | None) -> bool:
    return str(scope_type or "Cluster").strip().lower() in {"feeder", "subfeeder", "sub feeder", "sub-feeder"}
BOQ_FEEDER_SUBDUCT_ROW = 66
BOQ_FEEDER_BORING_ROW = 65
BOQ_FEEDER_NRO_TRENCHING_QUANTITY_CELLS = ("D109", "E109", "D110", "E110")
BOQ_FEEDER_NRO_TRENCHING_TOTAL_CELLS = ("K109", "K110")
BOQ_FEEDER_NRO_BORING_QUANTITY_CELLS = tuple(
    f"{column}{row}" for row in range(112, 121) for column in ("D", "E")
)
BOQ_FEEDER_NRO_BORING_TOTAL_CELLS = tuple(f"K{row}" for row in range(112, 121))

FDT_COLUMN_MAP = {
    1: "C",
    2: "D",
    3: "E",
    4: "F",
    5: "G",
    6: "H",
    7: "I",
    8: "J",
    9: "K",
    10: "L",
}

DISTRIBUTION_CABLE_ROW_MAP = {
    "A|24C/2T": 2,
    "B|24C/2T": 3,
    "C|24C/2T": 4,
    "D|24C/2T": 5,
    "A|36C/3T": 6,
    "B|36C/3T": 7,
    "C|36C/3T": 8,
    "D|36C/3T": 9,
    "A|48C/4T": 10,
    "B|48C/4T": 11,
    "C|48C/4T": 12,
    "D|48C/4T": 13,
}
DISTRIBUTION_CABLE_TOTAL_ROW_MAP = {
    key: row + 22 for key, row in DISTRIBUTION_CABLE_ROW_MAP.items()
}

SLING_WIRE_ROW = 15

UG_SUBDUCT_ROW_MAP = {
    "SUBDUCT 32/28": {
        "item": "Sub Duct / HDPE 32/28 warna hitam, 3 strip ungu",
        "row": 108,
        "unit": "m",
    },
    "SUBDUCT 40/34": {
        "item": "Sub Duct / HDPE 40/34 warna Ungu, 3 Strip Putih",
        "row": 109,
        "unit": "m",
    },
}

UG_HANDHOLE_ROW_MAP = {
    "NEW HH 20X20X20": {
        "item": "HH pit : 20x20x20 cm",
        "row": 117,
        "unit": "unit",
    },
    "NEW HH 40X40X60": {
        "item": "FAT : 40 x 40 x 60 cm",
        "row": 116,
        "unit": "unit",
    },
    "NEW HH 80X80X130": {
        "item": "FDT : 80 x 80 x 130 cm",
        "row": 119,
        "unit": "unit",
    },
}

UG_TWEY_ROW = {
    "item": "Y Adaptor/ T-Wey",
    "row": 125,
    "unit": "pcs",
}

FDT_POLE_MOUNTED_COLOR_MAP = {
    "#AA00FF": {
        "item": "FDT Pole Mounted 48 core",
        "row": 38,
        "unit": "set",
    },
    "#550000": {
        "item": "FDT Pole Mounted 72 core",
        "row": 39,
        "unit": "set",
    },
    "#FF0000": {
        "item": "FDT Pole Mounted 96 core",
        "row": 40,
        "unit": "set",
    },
    "#FFFF00": {
        "item": "FDT Ground Mounted 144 core",
        "row": 41,
        "unit": "set",
    },
    "#FFAA00": {
        "item": "FDT 288 core (not available in current BOQ template)",
        "row": 0,
        "unit": "set",
    },
}

FAT_ROW_MAP = {
    "A": 45,
    "B": 46,
    "C": 47,
    "D": 48,
}

NEW_POLE_ROW_MAP = {
    "NEW POLE 7-4": {
        "item": '7m 4" (for Feeder Pole / FDT Pole)',
        "row": 75,
        "unit": "unit",
    },
    "NEW POLE 7-3": {
        "item": '7m 3" (for Distribution Pole)',
        "row": 76,
        "unit": "unit",
    },
    "NEW POLE 7-2.5": {
        "item": '7m 2.5" (for Home Connect)',
        "row": 77,
        "unit": "unit",
    },
    "NEW POLE 9-4": {
        "item": '9m 4" (for crossing roads)',
        "row": 79,
        "unit": "unit",
    },
    "NEW POLE 7-5": {
        "item": '7m 5" (Feeder)',
        "row": 0,  # not used in Cluster, only Feeder
        "unit": "unit",
    },
    "NEW POLE 9-5": {
        "item": '9m 5" (Feeder)',
        "row": 0,  # not used in Cluster, only Feeder
        "unit": "unit",
    },
}

EXISTING_POLE_PARTNER_TYPE_MAP = {
    "LN": {
        "item": "Existing Pole LN",
        "row": 80,
        "unit": "unit",
    },
    "MTI": {
        "item": "Existing Pole MTI",
        "row": 81,
        "unit": "unit",
    },
    "TLKM": {
        "item": "Existing Pole TELKOM",
        "row": 82,
        "unit": "unit",
    },
}

EXISTING_POLE_EMR_ROW = {
    "item": "Existing Pole MR",
    "row": 83,
    "unit": "unit",
}

BOQ_FOLDER_NAMES = [
    "OLT",
    "DISTRIBUTION CABLE",
    "CABLE",
    "JOINT CLOSURE",
    "HP COVER",
    "SLING WIRE",
    "FDT",
    "FAT",
    "NEW POLE 7-2.5",
    "NEW POLE 7-3",
    "NEW POLE 7-4",
    "NEW POLE 7-5",
    "NEW POLE 9-4",
    "NEW POLE 9-5",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "EXISTING POLE EMR 7-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 9-4",
    "SUBDUCT 32/28",
    "SUBDUCT 40/34",
    "TALI PANCING",
    "NEW HH 20X20X20",
    "NEW HH 40X40X60",
    "NEW HH 80X80X130",
    "TWEY",
]
FOLDER_NAME_ALIASES = {re.sub(r"\s+", " ", name.strip()).upper(): name for name in BOQ_FOLDER_NAMES}
FOLDER_NAME_ALIASES.update(
    {
        "TALI PANICNG": "TALI PANCING",
        "NEW HH 20 X 20 X 20": "NEW HH 20X20X20",
        "NEW HH 40 X 40 X 60": "NEW HH 40X40X60",
        "NEW HH 80 X 80 X 130": "NEW HH 80X80X130",
        "T-WAY": "TWEY",
        "T-WEY": "TWEY",
    }
)
NEW_POLE_FOLDERS = set(NEW_POLE_ROW_MAP)
EXISTING_POLE_PARTNER_FOLDERS = {"EXISTING POLE PARTNER 7-4", "EXISTING POLE PARTNER 9-4"}
EXISTING_POLE_EMR_FOLDERS = {
    "EXISTING POLE EMR 7-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 7-5",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE EMR 9-5",
}

LINE_TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])LINE\s*[-_\s]*([A-D])(?=$|[^A-Z0-9])", re.IGNORECASE)
FDT_TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])FDT\s*[-_\s]*(\d+)(?=$|[^A-Z0-9])", re.IGNORECASE)
LINE_ONLY_PATTERN = re.compile(r"^\s*LINE\s*[-_\s]*([A-D])\s*$", re.IGNORECASE)
CABLE_TYPE_PATTERNS = (
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?24\s*C(?:\s*/\s*2\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "24C/2T"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?36\s*C(?:\s*/\s*3\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "36C/3T"),
    (re.compile(r"(?<![A-Z0-9])(?:FO\s*)?48\s*C(?:\s*/\s*4\s*T)?(?!\s*/)(?![A-Z0-9])", re.IGNORECASE), "48C/4T"),
)
LENGTH_FALLBACK_PATTERNS = (
    re.compile(r"TOTAL\s+ROUTE\s*:\s*(\d+(?:[.,]\d+)?)\s*M\b", re.IGNORECASE),
    re.compile(r"(?<![A-Z0-9])(\d+(?:[.,]\d+)?)\s*M\b", re.IGNORECASE),
)
FDT_SUFFIX_PATTERN = re.compile(r"\.(\d+)\D*$")
PARTNER_LN_PATTERN = re.compile(r"(^|[^A-Z0-9])LN([^A-Z0-9]|$)", re.IGNORECASE)

GEOD = Geod(ellps="WGS84")


@dataclass
class BoqPreviewRow:
    source_folder: str
    parent_line_fdt: str
    item: str
    cable_type: str = ""
    quantity: int | float | None = None
    unit: str = ""
    target_cell: str = ""
    status: str = "Mapped"
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "source_folder": self.source_folder,
            "parent_line_fdt": self.parent_line_fdt,
            "item": self.item,
            "cable_type": self.cable_type,
            "quantity": self.quantity,
            "unit": self.unit,
            "target_cell": self.target_cell,
            "status": self.status,
            "message": self.message,
        }


@dataclass
class FdtPlacemark:
    name: str | None
    suffix_number: int | None
    source_order: int
    color: str | None
    style_url: str
    source_folder: str


@dataclass
class BoqContext:
    logger: object | None = None
    distribution_cable: dict[tuple[str, int, str], float] = field(default_factory=lambda: defaultdict(float))
    sling_wire: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    ug_subduct: dict[tuple[str, int], float] = field(default_factory=lambda: defaultdict(float))
    ug_handhole: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    ug_twey: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    fdt_placemarks: list[FdtPlacemark] = field(default_factory=list)
    fat: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    new_pole: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    existing_pole_partner: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    existing_pole_emr: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    hp_cover_count: int = 0
    olt_name: str = ""
    cluster_name: str = ""
    preview_rows: list[BoqPreviewRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    processed_files: int = 0
    raw_counts: dict[str, int | float] = field(
        default_factory=lambda: {
            "fdt_detected": 0,
            "fat_detected": 0,
            "pole_detected": 0,
            "ug_handhole_detected": 0,
            "ug_twey_detected": 0,
        }
    )


@dataclass
class BoqParseResult:
    cell_values: dict[str, int | float]
    nro_cell_values: dict[str, int | float | str]
    preview_rows: list[BoqPreviewRow]
    warnings: list[str]
    summary: dict
    context: "BoqContext | None" = None

    def to_dict(self) -> dict:
        return {
            "cell_values": self.cell_values,
            "nro_cell_values": self.nro_cell_values,
            "preview_rows": [row.to_dict() for row in self.preview_rows],
            "warnings": self.warnings,
            "summary": self.summary,
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


def normalized_folder_name(folder_name: str | None) -> str:
    return re.sub(r"\s+", " ", str(folder_name or "").strip()).upper()


def canonical_boq_folder(folder_name: str | None) -> str | None:
    return FOLDER_NAME_ALIASES.get(normalized_folder_name(folder_name))


def nearest_boq_folder(folder_stack: Sequence[str]) -> str | None:
    for folder_name in reversed(folder_stack):
        canonical = canonical_boq_folder(folder_name)
        if canonical:
            return canonical
    return None


def extract_cluster_name_from_kmz_filename(path: str | Path) -> str:
    stem = Path(path).stem
    stem = re.sub(r"^[0-9a-f]{32}_\d+_", "", stem, flags=re.IGNORECASE)
    normalized = re.sub(r"[_\s]+", " ", stem).strip()
    normalized = re.sub(r"\s*-\s*", " - ", normalized)
    if " - " in normalized:
        return (normalized.split(" - ", 1)[1].strip() or normalized).upper()
    return normalized.upper()

def extract_parent_line_fdt(folder_stack: Sequence[str]) -> tuple[str, int, str] | None:
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


def detect_cable_type(*values: str | None) -> str | None:
    search_text = " ".join(value or "" for value in values)
    for pattern, cable_type in CABLE_TYPE_PATTERNS:
        if pattern.search(search_text):
            return cable_type
    return None


def calculate_linestring_length_meter(linestrings: Sequence[Sequence[tuple[float, float]]]) -> float:
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


def extract_length_fallback_meter(*values: str | None) -> float | None:
    search_text = " ".join(value or "" for value in values)
    for pattern in LENGTH_FALLBACK_PATTERNS:
        match = pattern.search(search_text)
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", "."))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def line_length_with_fallback(
    linestrings: Sequence[Sequence[tuple[float, float]]],
    *fallback_texts: str | None,
) -> float | None:
    length = calculate_linestring_length_meter(linestrings)
    if length > 0:
        return length
    return extract_length_fallback_meter(*fallback_texts)


def extract_fdt_suffix_number(name: str | None) -> int | None:
    match = FDT_SUFFIX_PATTERN.search(name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_kml_color_to_rgb_hex(value: str | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        if value < 0 or value > 0xFFFFFF:
            return None
        return f"#{value:06X}"

    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if normalized.startswith("#"):
        normalized = normalized[1:]
    normalized = re.sub(r"[^0-9A-F]", "", normalized)
    if len(normalized) == 8:
        # KML stores colors as AABBGGRR.
        blue = normalized[2:4]
        green = normalized[4:6]
        red = normalized[6:8]
        return f"#{red}{green}{blue}"
    if len(normalized) == 6:
        return f"#{normalized}"
    return None


def detect_fdt_type_by_color(color: str | None) -> dict | None:
    normalized = normalize_kml_color_to_rgb_hex(color)
    return FDT_POLE_MOUNTED_COLOR_MAP.get(normalized or "")


def parse_style_element(style_element) -> dict[str, str | None]:
    if style_element is None:
        return {}

    icon_style = direct_child_by_name(style_element, "IconStyle")
    label_style = direct_child_by_name(style_element, "LabelStyle")
    line_style = direct_child_by_name(style_element, "LineStyle")
    poly_style = direct_child_by_name(style_element, "PolyStyle")
    return {
        "icon_color": normalize_kml_color_to_rgb_hex(direct_child_text_by_name(icon_style, "color")) if icon_style is not None else None,
        "label_color": normalize_kml_color_to_rgb_hex(direct_child_text_by_name(label_style, "color")) if label_style is not None else None,
        "line_color": normalize_kml_color_to_rgb_hex(direct_child_text_by_name(line_style, "color")) if line_style is not None else None,
        "poly_color": normalize_kml_color_to_rgb_hex(direct_child_text_by_name(poly_style, "color")) if poly_style is not None else None,
    }


def first_style_color(style: dict[str, str | None] | None) -> str | None:
    if not style:
        return None
    for key in ("icon_color", "label_color", "line_color", "poly_color"):
        color = normalize_kml_color_to_rgb_hex(style.get(key))
        if color:
            return color
    return None


def normalize_style_url(style_url: str | None) -> str:
    normalized = (style_url or "").strip()
    if normalized and not normalized.startswith("#") and "://" not in normalized:
        normalized = f"#{normalized}"
    return normalized


def parse_kml_boq_styles(root) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
    styles: dict[str, dict] = {}
    style_maps: dict[str, dict[str, str]] = {}

    for element in root.iter():
        element_name = local_name(element.tag)
        style_id = element.attrib.get("id")
        if not style_id:
            continue
        style_key = f"#{style_id}"

        if element_name == "Style":
            styles[style_key] = parse_style_element(element)
        elif element_name == "StyleMap":
            pairs = {}
            for pair in [child for child in list(element) if local_name(child.tag) == "Pair"]:
                key = direct_child_text_by_name(pair, "key").strip().lower()
                url = normalize_style_url(direct_child_text_by_name(pair, "styleUrl"))
                if key and url:
                    pairs[key] = url
            style_maps[style_key] = pairs

    return styles, style_maps


def resolve_kml_style_color(
    placemark,
    styles: dict[str, dict],
    style_maps: dict[str, dict[str, str]],
) -> tuple[str | None, str, bool]:
    inline_color = first_style_color(parse_style_element(direct_child_by_name(placemark, "Style")))
    if inline_color:
        return inline_color, "", False

    style_url = normalize_style_url(direct_child_text_by_name(placemark, "styleUrl"))
    if not style_url:
        return None, "", False

    resolved_url = style_url
    visited = set()
    while resolved_url in style_maps and resolved_url not in visited:
        visited.add(resolved_url)
        pairs = style_maps[resolved_url]
        resolved_url = pairs.get("normal") or pairs.get("highlight") or ""
        if not resolved_url:
            break

    if resolved_url in styles:
        return first_style_color(styles[resolved_url]), style_url, False
    return None, style_url, True


def target_cell(row: int, fdt: int) -> str:
    column = FDT_COLUMN_MAP.get(fdt)
    return f"{column}{row}" if column else ""


def placemark_linestrings(placemark) -> list[list[tuple[float, float]]]:
    linestrings = []
    for line in children_by_name(placemark, "LineString"):
        coordinate_element = direct_child_by_name(line, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if len(coordinates) >= 2:
            linestrings.append(coordinates)
    return linestrings


def count_valid_placemarks_in_folder(placemark) -> int:
    has_geometry = any(
        children_by_name(placemark, geometry_name)
        for geometry_name in ("Point", "LineString", "Polygon")
    )
    return 1 if has_geometry else 0


def has_valid_point_placemark(placemark) -> bool:
    for point in children_by_name(placemark, "Point"):
        coordinate_element = direct_child_by_name(point, "coordinates")
        coordinates = parse_kml_coordinates(coordinate_element.text if coordinate_element is not None else None)
        if coordinates:
            return True
    return False


def add_preview_warning(
    context: BoqContext,
    source_folder: str,
    parent_line_fdt: str,
    item: str,
    message: str,
    status: str = "Warning",
    cable_type: str = "",
    quantity: int | float | None = None,
    unit: str = "",
    target: str = "",
) -> None:
    context.warnings.append(message)
    context.preview_rows.append(
        BoqPreviewRow(
            source_folder=source_folder,
            parent_line_fdt=parent_line_fdt,
            item=item,
            cable_type=cable_type,
            quantity=quantity,
            unit=unit,
            target_cell=target,
            status=status,
            message=message,
        )
    )
    pipeline_log(context.logger, "WARNING", message)


def process_distribution_cable_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    name = direct_child_text_by_name(placemark, "name")
    description = direct_child_text_by_name(placemark, "description")
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    pipeline_log(context.logger, "INFO", f"Found DISTRIBUTION CABLE under {parent_label}")
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or "Distribution Cable",
            "DISTRIBUTION CABLE folder found but parent LINE/FDT invalid",
        )
        return
    line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or "Distribution Cable",
            "FDT number exceeds available BOQ columns.",
        )
        return

    cable_type = detect_cable_type(name, description, " ".join(folder_stack))
    if not cable_type:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or "Distribution Cable",
            "DISTRIBUTION CABLE folder found but cable type was not detected.",
            status="Unmapped",
        )
        return
    pipeline_log(context.logger, "INFO", f"Detected cable type FO {cable_type}")

    length = line_length_with_fallback(placemark_linestrings(placemark), name, description)
    if length is None:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            f"Line {line} - {cable_type}",
            "Distribution Cable geometry invalid and fallback length was not found.",
            status="Skipped",
            cable_type=cable_type,
        )
        return

    pipeline_log(context.logger, "INFO", f"Calculated route length: {math.ceil(length)} m")
    context.distribution_cable[(line, fdt, cable_type)] += length


def process_sling_wire_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    name = direct_child_text_by_name(placemark, "name")
    description = direct_child_text_by_name(placemark, "description")
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    pipeline_log(context.logger, "INFO", f"Found SLING WIRE under {parent_label}")
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or "Sling Wire",
            "SLING WIRE folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            "Rute Strand Wire",
            "FDT number exceeds available BOQ columns.",
        )
        return

    length = line_length_with_fallback(placemark_linestrings(placemark), name, description)
    if length is None:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            "Rute Strand Wire",
            "SLING WIRE geometry invalid and fallback length was not found.",
            status="Skipped",
        )
        return

    pipeline_log(context.logger, "INFO", f"Calculated sling wire subtotal: {math.ceil(length)} m")
    pipeline_log(context.logger, "INFO", f"Aggregating sling wire into FDT {fdt}")
    context.sling_wire[fdt] += length


def process_ug_subduct_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    name = direct_child_text_by_name(placemark, "name")
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    pipeline_log(context.logger, "INFO", f"Found {source_folder} under {parent_label}")
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or source_folder,
            f"{source_folder} folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            str(UG_SUBDUCT_ROW_MAP[source_folder]["item"]),
            "FDT number exceeds available BOQ columns.",
        )
        return

    length = calculate_linestring_length_meter(placemark_linestrings(placemark))
    if length <= 0:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or source_folder,
            f"{source_folder} geometry invalid. Panjang UG harus dihitung dari LineString, bukan angka di nama placemark.",
            status="Skipped",
        )
        return

    pipeline_log(context.logger, "INFO", f"Calculated {source_folder} subtotal: {math.ceil(length)} m")
    context.ug_subduct[(source_folder, fdt)] += length


def process_ug_handhole_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    if not has_valid_point_placemark(placemark):
        return
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            direct_child_text_by_name(placemark, "name") or source_folder,
            f"{source_folder} folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            str(UG_HANDHOLE_ROW_MAP[source_folder]["item"]),
            "FDT number exceeds available BOQ columns.",
        )
        return
    pipeline_log(context.logger, "INFO", f"Found {source_folder} point under {parent_label}")
    context.ug_handhole[(source_folder, fdt)] += 1
    context.raw_counts["ug_handhole_detected"] = int(context.raw_counts.get("ug_handhole_detected", 0)) + 1


def process_ug_twey_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    if not has_valid_point_placemark(placemark):
        return
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            direct_child_text_by_name(placemark, "name") or source_folder,
            "TWEY folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            str(UG_TWEY_ROW["item"]),
            "FDT number exceeds available BOQ columns.",
        )
        return
    pipeline_log(context.logger, "INFO", f"Found TWEY point under {parent_label}")
    context.ug_twey[fdt] += 1
    context.raw_counts["ug_twey_detected"] = int(context.raw_counts.get("ug_twey_detected", 0)) + 1


def process_hp_cover_placemark(context: BoqContext, placemark, source_folder: str) -> None:
    if not count_valid_placemarks_in_folder(placemark):
        return
    context.hp_cover_count += 1
    pipeline_log(context.logger, "INFO", "Found HP COVER placemark")

def parse_fdt_folder(
    context: BoqContext,
    placemark,
    source_folder: str,
    styles: dict[str, dict],
    style_maps: dict[str, dict[str, str]],
) -> None:
    name = direct_child_text_by_name(placemark, "name")
    if not count_valid_placemarks_in_folder(placemark):
        return

    if re.match(r"^EXT\.", str(name or "").strip(), re.IGNORECASE):
        pipeline_log(context.logger, "INFO", f"Skip EXT FDT from BOQ: {name}")
        return
    pipeline_log(context.logger, "INFO", f"Found FDT placemark: {name}")
    suffix_number = extract_fdt_suffix_number(name)
    if suffix_number is None:
        pipeline_log(
            context.logger,
            "INFO",
            f"FDT suffix missing for placemark {name or '-'}; assigning by folder order",
        )
    else:
        pipeline_log(context.logger, "INFO", f"Extracted FDT suffix: {suffix_number:03d}")

    color, style_url, unresolved_style = resolve_kml_style_color(placemark, styles, style_maps)
    if unresolved_style and not color:
        add_preview_warning(
            context,
            source_folder,
            "FDT pending",
            name or "FDT",
            f"Could not resolve styleUrl for FDT placemark {name or '-'}",
            status="Warning",
        )
    if color:
        pipeline_log(context.logger, "INFO", f"Detected FDT icon color: {color}")

    context.fdt_placemarks.append(
        FdtPlacemark(
            name=name,
            suffix_number=suffix_number,
            source_order=len(context.fdt_placemarks) + 1,
            color=color,
            style_url=style_url,
            source_folder=source_folder,
        )
    )


def process_fat_placemark(context: BoqContext, placemark, folder_stack: Sequence[str], source_folder: str) -> None:
    if not count_valid_placemarks_in_folder(placemark):
        return
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            direct_child_text_by_name(placemark, "name") or "FAT",
            "FAT folder found but parent LINE/FDT invalid",
        )
        return
    line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            f"Total FAT Line {line}",
            "FDT number exceeds available BOQ columns.",
        )
        return
    pipeline_log(context.logger, "INFO", f"Found FAT folder under {parent_label}")
    context.fat[(line, fdt)] += 1
    context.raw_counts["fat_detected"] = int(context.raw_counts.get("fat_detected", 0)) + 1


def process_new_pole_placemark(context: BoqContext, placemark, folder_stack: Sequence[str], source_folder: str) -> None:
    if not count_valid_placemarks_in_folder(placemark):
        return
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            direct_child_text_by_name(placemark, "name") or source_folder,
            "NEW POLE folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            NEW_POLE_ROW_MAP[source_folder]["item"],
            "FDT number exceeds available BOQ columns.",
        )
        return
    pipeline_log(context.logger, "INFO", f"Found {source_folder} under {parent_label}")
    context.new_pole[(source_folder, fdt)] += 1
    context.raw_counts["pole_detected"] = int(context.raw_counts.get("pole_detected", 0)) + 1
    pipeline_log(context.logger, "INFO", f"Aggregating {source_folder} into FDT {fdt}")


def detect_existing_partner_pole_type(name: str | None) -> str | None:
    upper_name = (name or "").upper()
    if "TLKM" in upper_name:
        return "TLKM"
    if "MTI" in upper_name:
        return "MTI"
    if PARTNER_LN_PATTERN.search(upper_name):
        return "LN"
    return None


def process_existing_pole_partner_placemark(
    context: BoqContext,
    placemark,
    folder_stack: Sequence[str],
    source_folder: str,
) -> None:
    if not count_valid_placemarks_in_folder(placemark):
        return
    name = direct_child_text_by_name(placemark, "name")
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or source_folder,
            "EXISTING POLE PARTNER folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or source_folder,
            "FDT number exceeds available BOQ columns.",
        )
        return

    pipeline_log(context.logger, "INFO", f"Found {source_folder} under {parent_label}")
    partner_type = detect_existing_partner_pole_type(name)
    if not partner_type:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            name or source_folder,
            f"EXISTING POLE PARTNER placemark type not recognized: {name or '-'} under {parent_label}",
            status="Unmapped",
        )
        return

    pipeline_log(context.logger, "INFO", f"Detected partner pole type {partner_type} from {name}")
    pipeline_log(context.logger, "INFO", f"Aggregating Existing Pole {partner_type} into FDT {fdt}")
    context.existing_pole_partner[(partner_type, fdt)] += 1
    context.raw_counts["pole_detected"] = int(context.raw_counts.get("pole_detected", 0)) + 1


def process_existing_pole_emr_placemark(context: BoqContext, placemark, folder_stack: Sequence[str], source_folder: str) -> None:
    if not count_valid_placemarks_in_folder(placemark):
        return
    parent = extract_parent_line_fdt(folder_stack)
    parent_label = parent[2] if parent else "Invalid parent"
    if not parent:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            direct_child_text_by_name(placemark, "name") or source_folder,
            "EXISTING POLE EMR folder found but parent LINE/FDT invalid",
        )
        return
    _line, fdt, parent_label = parent
    if fdt not in FDT_COLUMN_MAP:
        add_preview_warning(
            context,
            source_folder,
            parent_label,
            EXISTING_POLE_EMR_ROW["item"],
            "FDT number exceeds available BOQ columns.",
        )
        return

    pipeline_log(context.logger, "INFO", f"Found {source_folder} under {parent_label}")
    context.existing_pole_emr[fdt] += 1
    context.raw_counts["pole_detected"] = int(context.raw_counts.get("pole_detected", 0)) + 1
    pipeline_log(context.logger, "INFO", f"Aggregating into Existing Pole MR for FDT {fdt}")


def parse_kml_container_for_boq(
    element,
    folder_stack: list[str],
    context: BoqContext,
    styles: dict[str, dict],
    style_maps: dict[str, dict[str, str]],
) -> None:
    for child in list(element):
        child_name = local_name(child.tag)
        if child_name == "Document":
            parse_kml_container_for_boq(child, folder_stack, context, styles, style_maps)
        elif child_name == "Folder":
            folder_name = direct_child_text_by_name(child, "name") or "Tanpa Nama"
            parse_kml_container_for_boq(child, folder_stack + [folder_name], context, styles, style_maps)
        elif child_name == "Placemark":
            source_folder = nearest_boq_folder(folder_stack)
            if not source_folder:
                continue
            if source_folder == "DISTRIBUTION CABLE" or source_folder == "CABLE":
                process_distribution_cable_placemark(context, child, folder_stack, source_folder)
            elif source_folder == "HP COVER":
                process_hp_cover_placemark(context, child, source_folder)
            elif source_folder == "SLING WIRE":
                process_sling_wire_placemark(context, child, folder_stack, source_folder)
            elif source_folder in UG_SUBDUCT_ROW_MAP:
                process_ug_subduct_placemark(context, child, folder_stack, source_folder)
            elif source_folder in UG_HANDHOLE_ROW_MAP:
                process_ug_handhole_placemark(context, child, folder_stack, source_folder)
            elif source_folder == "TWEY":
                process_ug_twey_placemark(context, child, folder_stack, source_folder)
            elif source_folder == "FDT":
                parse_fdt_folder(context, child, source_folder, styles, style_maps)
            elif source_folder == "FAT":
                process_fat_placemark(context, child, folder_stack, source_folder)
            elif source_folder in NEW_POLE_FOLDERS:
                process_new_pole_placemark(context, child, folder_stack, source_folder)
            elif source_folder in EXISTING_POLE_PARTNER_FOLDERS:
                process_existing_pole_partner_placemark(context, child, folder_stack, source_folder)
            elif source_folder in EXISTING_POLE_EMR_FOLDERS:
                process_existing_pole_emr_placemark(context, child, folder_stack, source_folder)


def parse_kmz_structure(
    input_paths: str | Path | Iterable[str | Path],
    pipeline_logger=None,
    progress_callback=None,
    cluster_name_override: str | None = None,
    olt_name: str | None = None,
) -> BoqParseResult:
    paths = [path for path in normalize_input_paths(input_paths) if is_kml_path(path)]
    if not paths:
        raise ValueError("KML/KMZ file is required for BOQ export.")

    context = BoqContext(logger=pipeline_logger)
    context.cluster_name = str(cluster_name_override or "").strip() or extract_cluster_name_from_kmz_filename(paths[0])
    context.olt_name = str(olt_name or "").strip()
    pipeline_progress(progress_callback, pipeline_logger, 18, "Validating KML/KMZ")
    pipeline_log(pipeline_logger, "INFO", "Parsing KMZ/KML folder structure")
    pipeline_log(pipeline_logger, "INFO", f"Detected cluster name: {context.cluster_name}")
    if context.olt_name:
        pipeline_log(pipeline_logger, "INFO", f"Detected OLT name: {context.olt_name}")

    for path in paths:
        root = read_kml_root(path)
        styles, style_maps = parse_kml_boq_styles(root)
        context.processed_files += 1
        parse_kml_container_for_boq(root, [], context, styles, style_maps)

    return build_parse_result(context)


def write_cell_value(cell_values: dict[str, int | float], cell: str, value: int | float) -> None:
    if not cell:
        return
    cell_values[cell] = value


def aggregate_distribution_cable(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (line, fdt, cable_type), length in sorted(context.distribution_cable.items()):
        row = DISTRIBUTION_CABLE_ROW_MAP.get(f"{line}|{cable_type}")
        if row is None:
            add_preview_warning(
                context,
                "DISTRIBUTION CABLE",
                f"LINE {line} - FDT {fdt}",
                f"Line {line} - {cable_type}",
                f"DISTRIBUTION CABLE mapping not found for Line {line} cable {cable_type}",
                status="Unmapped",
                cable_type=cable_type,
                quantity=math.ceil(length),
                unit="m",
            )
            continue
        quantity = math.ceil(length)
        cell = target_cell(row, fdt)
        pipeline_log(context.logger, "INFO", f"Writing value to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, quantity)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="DISTRIBUTION CABLE",
                parent_line_fdt=f"LINE {line} - FDT {fdt}",
                item=f"Line {line} - {cable_type}",
                cable_type=cable_type,
                quantity=quantity,
                unit="m",
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_sling_wire(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for fdt, length in sorted(context.sling_wire.items()):
        quantity = math.ceil(length)
        cell = target_cell(SLING_WIRE_ROW, fdt)
        pipeline_log(context.logger, "INFO", f"Writing total sling wire FDT {fdt} to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, quantity)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="SLING WIRE",
                parent_line_fdt=f"FDT {fdt}",
                item="Rute Strand Wire",
                quantity=quantity,
                unit="m",
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_ug_subduct(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (source_folder, fdt), length in sorted(context.ug_subduct.items()):
        spec = UG_SUBDUCT_ROW_MAP[source_folder]
        quantity = math.ceil(length)
        cell = target_cell(int(spec["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Writing {source_folder} total FDT {fdt} to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, quantity)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder=source_folder,
                parent_line_fdt=f"FDT {fdt}",
                item=str(spec["item"]),
                quantity=quantity,
                unit=str(spec["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_ug_handhole(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (source_folder, fdt), count in sorted(context.ug_handhole.items()):
        spec = UG_HANDHOLE_ROW_MAP[source_folder]
        cell = target_cell(int(spec["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Writing {source_folder} total FDT {fdt} to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder=source_folder,
                parent_line_fdt=f"FDT {fdt}",
                item=str(spec["item"]),
                quantity=count,
                unit=str(spec["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_ug_twey(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for fdt, count in sorted(context.ug_twey.items()):
        cell = target_cell(int(UG_TWEY_ROW["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Writing TWEY total FDT {fdt} to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="TWEY",
                parent_line_fdt=f"FDT {fdt}",
                item=str(UG_TWEY_ROW["item"]),
                quantity=count,
                unit=str(UG_TWEY_ROW["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def assign_fdt_number_by_sorted_suffix(
    fdt_placemarks: Sequence[FdtPlacemark],
) -> list[tuple[int, int | None, list[FdtPlacemark]]]:
    grouped: dict[int, list[FdtPlacemark]] = defaultdict(list)
    without_suffix: list[FdtPlacemark] = []
    for placemark in fdt_placemarks:
        if placemark.suffix_number is None:
            without_suffix.append(placemark)
        else:
            grouped[placemark.suffix_number].append(placemark)

    assigned: list[tuple[int, int | None, list[FdtPlacemark]]] = []
    for suffix in sorted(grouped):
        assigned.append((len(assigned) + 1, suffix, grouped[suffix]))
    for placemark in sorted(without_suffix, key=lambda item: item.source_order):
        assigned.append((len(assigned) + 1, None, [placemark]))
    return assigned


def aggregate_fdt_pole_mounted(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    if not context.fdt_placemarks:
        return

    pipeline_log(context.logger, "INFO", "Found FDT folder")
    pipeline_log(context.logger, "INFO", "Sorting FDT placemarks by numeric suffix and folder order")
    assigned = assign_fdt_number_by_sorted_suffix(context.fdt_placemarks)
    context.raw_counts["fdt_detected"] = len(assigned)

    for fdt, suffix, placemarks in assigned:
        placemark_label = f"FDT suffix {suffix}" if suffix is not None else "FDT without suffix"
        if fdt > 10:
            for placemark in placemarks:
                add_preview_warning(
                    context,
                    placemark.source_folder,
                    f"FDT {fdt}",
                    placemark.name or placemark_label,
                    "FDT number exceeds available BOQ columns.",
                )
            continue

        for placemark in placemarks:
            pipeline_log(context.logger, "INFO", f"Assigned {placemark.name} as FDT {fdt}")
        if len(placemarks) > 1:
            add_preview_warning(
                context,
                "FDT",
                f"FDT {fdt}",
                placemarks[0].name or placemark_label,
                f"Duplicate FDT assignment detected for FDT {fdt}",
                status="Warning",
            )

        selected = next((placemark for placemark in placemarks if detect_fdt_type_by_color(placemark.color)), placemarks[0])
        fdt_type = detect_fdt_type_by_color(selected.color)
        normalized_color = normalize_kml_color_to_rgb_hex(selected.color) or "-"
        if not fdt_type:
            add_preview_warning(
                context,
                selected.source_folder,
                f"FDT {fdt}",
                selected.name or placemark_label,
                f"FDT placemark color not recognized: {selected.name or '-'} color {normalized_color} target FDT {fdt}",
                status="Unmapped",
            )
            continue

        pipeline_log(context.logger, "INFO", f"Mapped {normalized_color} to {fdt_type['item']}")
        row = int(fdt_type["row"])
        if row <= 0:
            add_preview_warning(
                context,
                selected.source_folder,
                f"FDT {fdt}",
                selected.name or placemark_label,
                f"{fdt_type['item']} recognized from color {normalized_color}, but current BOQ template has no target row.",
                status="Warning",
            )
            continue
        cell = target_cell(row, fdt)
        pipeline_log(context.logger, "INFO", f"Writing value 1 to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, 1)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder=selected.source_folder,
                parent_line_fdt=f"FDT {fdt}",
                item=str(fdt_type["item"]),
                quantity=1,
                unit=str(fdt_type["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )
        pipeline_log(context.logger, "SUCCESS", "FDT item written successfully")


def aggregate_fat(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (line, fdt), count in sorted(context.fat.items()):
        row = FAT_ROW_MAP[line]
        cell = target_cell(row, fdt)
        pipeline_log(context.logger, "INFO", f"Counted FAT placemarks: {count}")
        pipeline_log(context.logger, "INFO", f"Writing FAT count to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="FAT",
                parent_line_fdt=f"LINE {line} - FDT {fdt}",
                item=f"Total FAT Line {line}",
                quantity=count,
                unit="unit",
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_new_pole(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (source_folder, fdt), count in sorted(context.new_pole.items()):
        spec = NEW_POLE_ROW_MAP.get(source_folder)
        if not spec or int(spec.get("row", 0)) <= 0:
            # Skip pole types not mapped to Cluster (e.g. NEW POLE 7-5, 9-5 are Feeder-only)
            continue
        cell = target_cell(int(spec["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Counted {source_folder} placemarks: {count}")
        pipeline_log(context.logger, "INFO", f"Writing {source_folder} total to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder=source_folder,
                parent_line_fdt=f"FDT {fdt}",
                item=str(spec["item"]),
                quantity=count,
                unit=str(spec["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_existing_pole_partner(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for (partner_type, fdt), count in sorted(context.existing_pole_partner.items()):
        spec = EXISTING_POLE_PARTNER_TYPE_MAP[partner_type]
        cell = target_cell(int(spec["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Writing {spec['item']} total to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="EXISTING POLE PARTNER",
                parent_line_fdt=f"FDT {fdt}",
                item=str(spec["item"]),
                quantity=count,
                unit=str(spec["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_existing_pole_emr(context: BoqContext, cell_values: dict[str, int | float]) -> None:
    for fdt, count in sorted(context.existing_pole_emr.items()):
        cell = target_cell(int(EXISTING_POLE_EMR_ROW["row"]), fdt)
        pipeline_log(context.logger, "INFO", f"Writing Existing Pole MR total to {BOQ_SHEET_NAME}!{cell}")
        write_cell_value(cell_values, cell, count)
        context.preview_rows.append(
            BoqPreviewRow(
                source_folder="EXISTING POLE EMR",
                parent_line_fdt=f"FDT {fdt}",
                item=str(EXISTING_POLE_EMR_ROW["item"]),
                quantity=count,
                unit=str(EXISTING_POLE_EMR_ROW["unit"]),
                target_cell=cell,
                status="Mapped",
            )
        )


def aggregate_boq_nro_cluster(context: BoqContext) -> dict[str, int | float | str]:
    nro_cell_values: dict[str, int | float | str] = {}
    if context.olt_name:
        nro_cell_values[BOQ_NRO_OLT_CELL] = context.olt_name
        pipeline_log(context.logger, "INFO", f"Writing OLT name to {BOQ_NRO_SHEET_NAME}!{BOQ_NRO_OLT_CELL}")
    if context.cluster_name:
        nro_cell_values[BOQ_NRO_CLUSTER_CELL] = context.cluster_name
        pipeline_log(context.logger, "INFO", f"Writing cluster name to {BOQ_NRO_SHEET_NAME}!{BOQ_NRO_CLUSTER_CELL}")
    nro_cell_values[BOQ_NRO_HP_COVER_CELL] = context.hp_cover_count
    pipeline_log(context.logger, "INFO", f"Counted HP COVER placemarks: {context.hp_cover_count}")
    pipeline_log(context.logger, "INFO", f"Writing HP COVER total to {BOQ_NRO_SHEET_NAME}!{BOQ_NRO_HP_COVER_CELL}")
    context.preview_rows.append(
        BoqPreviewRow(
            source_folder="HP COVER",
            parent_line_fdt=BOQ_NRO_SHEET_NAME,
            item="Total HP COVER",
            quantity=context.hp_cover_count,
            unit="path",
            target_cell=BOQ_NRO_HP_COVER_CELL,
            status="Mapped",
        )
    )
    return nro_cell_values

def split_formula_args(value: str) -> list[str]:
    args = []
    current = []
    depth = 0
    in_quote = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            in_quote = not in_quote
        elif not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                index += 1
                continue
        current.append(char)
        index += 1
    args.append("".join(current).strip())
    return args

def strip_outer_parentheses(value: str) -> str:
    text = value.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps = True
        in_quote = False
        for index, char in enumerate(text):
            if char == "'":
                in_quote = not in_quote
            elif not in_quote:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(text) - 1:
                        wraps = False
                        break
        if not wraps:
            break
        text = text[1:-1].strip()
    return text

def split_sheet_reference(reference: str, current_sheet_name: str) -> tuple[str, str]:
    text = reference.replace("$", "")
    if "!" not in text:
        return current_sheet_name, text
    sheet_name, cell_reference = text.rsplit("!", 1)
    return sheet_name.strip("'"), cell_reference

def coerce_number(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return 0.0

def normalize_preview_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if not math.isfinite(float(value)):
        return None
    rounded = round(float(value), 6)
    if abs(rounded - int(rounded)) < 1e-9:
        return int(rounded)
    return rounded

def is_meaningful_preview_value(value) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return abs(float(value)) > 1e-9
    return True

def evaluate_condition(workbook, current_sheet_name: str, expression: str, cache: dict, stack: set) -> bool:
    for operator in ("<>", ">=", "<=", "=", ">", "<"):
        parts = expression.split(operator, 1)
        if len(parts) != 2:
            continue
        left = coerce_number(evaluate_formula_expression(workbook, current_sheet_name, parts[0], cache, stack))
        right = coerce_number(evaluate_formula_expression(workbook, current_sheet_name, parts[1], cache, stack))
        if operator == "<>":
            return left != right
        if operator == ">=":
            return left >= right
        if operator == "<=":
            return left <= right
        if operator == "=":
            return left == right
        if operator == ">":
            return left > right
        if operator == "<":
            return left < right
    return bool(coerce_number(evaluate_formula_expression(workbook, current_sheet_name, expression, cache, stack)))

def evaluate_range_argument_values(workbook, current_sheet_name: str, argument: str, cache: dict, stack: set) -> list[float] | None:
    argument = argument.strip()
    range_match = re.fullmatch(
        r"(?:(?:'(?P<quoted>[^']+)'|(?P<plain>[A-Za-z0-9 _-]+))!)?(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)",
        argument,
    )
    if not range_match:
        return None

    sheet_name = range_match.group("quoted") or range_match.group("plain") or current_sheet_name
    start = range_match.group("start").replace("$", "")
    end = range_match.group("end").replace("$", "")
    min_col, min_row, max_col, max_row = range_boundaries(f"{start}:{end}")
    values = []
    for row in range(min_row, max_row + 1):
        for column in range(min_col, max_col + 1):
            values.append(coerce_number(evaluate_cell_value(workbook, sheet_name, f"{get_column_letter(column)}{row}", cache, stack)))
    return values

def evaluate_sum_argument(workbook, current_sheet_name: str, argument: str, cache: dict, stack: set) -> float:
    range_values = evaluate_range_argument_values(workbook, current_sheet_name, argument, cache, stack)
    if range_values is not None:
        return sum(range_values)
    return coerce_number(evaluate_formula_expression(workbook, current_sheet_name, argument, cache, stack))

def evaluate_max_argument(workbook, current_sheet_name: str, argument: str, cache: dict, stack: set) -> float:
    range_values = evaluate_range_argument_values(workbook, current_sheet_name, argument, cache, stack)
    if range_values is not None:
        return max(range_values, default=0.0)
    return coerce_number(evaluate_formula_expression(workbook, current_sheet_name, argument, cache, stack))


def find_matching_parenthesis(value: str, open_index: int) -> int | None:
    depth = 0
    in_quote = False
    for index in range(open_index, len(value)):
        char = value[index]
        if char == "'":
            in_quote = not in_quote
        elif not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
    return None


def is_complete_formula_function(text: str, function_name: str) -> bool:
    function_name = function_name.upper()
    prefix = f"{function_name}("
    if not text.upper().startswith(prefix):
        return False
    return find_matching_parenthesis(text, len(function_name)) == len(text) - 1


def replace_embedded_formula_functions(workbook, current_sheet_name: str, expression: str, cache: dict, stack: set) -> str:
    index = 0
    output = []
    while index < len(expression):
        if expression[index] == "'":
            close_quote = expression.find("'", index + 1)
            if close_quote == -1:
                output.append(expression[index])
                index += 1
            else:
                output.append(expression[index:close_quote + 1])
                index = close_quote + 1
            continue

        matched = False
        for function_name in ("ROUNDUP", "SUM", "MAX"):
            end_name = index + len(function_name)
            previous = expression[index - 1] if index else ""
            if previous and (previous.isalnum() or previous == "_"):
                continue
            if expression[index:end_name].upper() != function_name or end_name >= len(expression) or expression[end_name] != "(":
                continue

            close_index = find_matching_parenthesis(expression, end_name)
            if close_index is None:
                continue

            args = split_formula_args(expression[end_name + 1:close_index])
            if function_name == "SUM":
                value = sum(evaluate_sum_argument(workbook, current_sheet_name, arg, cache, stack) for arg in args)
            elif function_name == "MAX":
                value = max((evaluate_max_argument(workbook, current_sheet_name, arg, cache, stack) for arg in args), default=0.0)
            else:
                number = coerce_number(evaluate_formula_expression(workbook, current_sheet_name, args[0], cache, stack)) if args else 0.0
                digits = int(coerce_number(evaluate_formula_expression(workbook, current_sheet_name, args[1], cache, stack))) if len(args) > 1 else 0
                factor = 10 ** digits
                value = math.ceil(number * factor) / factor

            output.append(str(coerce_number(value)))
            index = close_index + 1
            matched = True
            break

        if not matched:
            output.append(expression[index])
            index += 1

    return "".join(output)


def evaluate_formula_expression(workbook, current_sheet_name: str, expression: str, cache: dict, stack: set):
    text = strip_outer_parentheses(str(expression or "").strip())
    if text.startswith("="):
        text = strip_outer_parentheses(text[1:])
    if not text:
        return None

    upper_text = text.upper()
    if is_complete_formula_function(text, "IF"):
        args = split_formula_args(text[3:-1])
        if len(args) >= 3:
            return evaluate_formula_expression(
                workbook,
                current_sheet_name,
                args[1] if evaluate_condition(workbook, current_sheet_name, args[0], cache, stack) else args[2],
                cache,
                stack,
            )
    if is_complete_formula_function(text, "ROUNDUP"):
        args = split_formula_args(text[8:-1])
        number = coerce_number(evaluate_formula_expression(workbook, current_sheet_name, args[0], cache, stack)) if args else 0.0
        digits = int(coerce_number(evaluate_formula_expression(workbook, current_sheet_name, args[1], cache, stack))) if len(args) > 1 else 0
        factor = 10 ** digits
        return math.ceil(number * factor) / factor
    if is_complete_formula_function(text, "SUM"):
        return sum(evaluate_sum_argument(workbook, current_sheet_name, arg, cache, stack) for arg in split_formula_args(text[4:-1]))
    if is_complete_formula_function(text, "MAX"):
        return max((evaluate_max_argument(workbook, current_sheet_name, arg, cache, stack) for arg in split_formula_args(text[4:-1])), default=0.0)

    cell_ref_match = re.fullmatch(r"(?:(?:'[^']+'|[A-Za-z0-9 _-]+)!)?\$?[A-Z]{1,3}\$?\d+", text)
    if cell_ref_match:
        sheet_name, coordinate = split_sheet_reference(text, current_sheet_name)
        return evaluate_cell_value(workbook, sheet_name, coordinate, cache, stack)

    arithmetic = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", text)
    arithmetic = replace_embedded_formula_functions(workbook, current_sheet_name, arithmetic, cache, stack)

    def replace_reference(match: re.Match) -> str:
        reference = match.group(0)
        sheet_name, coordinate = split_sheet_reference(reference, current_sheet_name)
        return str(coerce_number(evaluate_cell_value(workbook, sheet_name, coordinate, cache, stack)))

    arithmetic = re.sub(
        r"(?<![A-Z0-9_])(?:(?:'[^']+'|[A-Za-z0-9 _-]+)!)?\$?[A-Z]{1,3}\$?\d+(?![A-Z0-9_])",
        replace_reference,
        arithmetic,
    )
    if not re.fullmatch(r"[0-9+\-*/().\s]+", arithmetic):
        return None
    try:
        return eval(arithmetic, {"__builtins__": {}}, {})
    except Exception:
        return None

def evaluate_cell_value(workbook, sheet_name: str, coordinate: str, cache: dict, stack: set):
    key = (sheet_name, coordinate.replace("$", ""))
    if key in cache:
        return cache[key]
    if key in stack:
        return None
    if sheet_name not in workbook.sheetnames:
        return None

    stack.add(key)
    value = workbook[sheet_name][coordinate.replace("$", "")].value
    if isinstance(value, str) and value.startswith("="):
        value = evaluate_formula_expression(workbook, sheet_name, value[1:], cache, stack)
    stack.remove(key)
    value = normalize_preview_number(value)
    cache[key] = value
    return value

def build_boq_nro_cluster_preview(workbook) -> dict:
    if BOQ_NRO_SHEET_NAME not in workbook.sheetnames:
        return {"columns": [], "rows": []}
    worksheet = workbook[BOQ_NRO_SHEET_NAME]
    cache: dict = {}
    columns = [
        {
            "key": column,
            "label": str(evaluate_cell_value(workbook, BOQ_NRO_SHEET_NAME, f"{column}{BOQ_NRO_PREVIEW_HEADER_ROW}", cache, set()) or column),
        }
        for column in BOQ_NRO_PREVIEW_COLUMNS
    ]
    rows = []
    for row_number in range(BOQ_NRO_PREVIEW_HEADER_ROW + 1, worksheet.max_row + 1):
        material_qty = evaluate_cell_value(workbook, BOQ_NRO_SHEET_NAME, f"D{row_number}", cache, set())
        service_qty = evaluate_cell_value(workbook, BOQ_NRO_SHEET_NAME, f"E{row_number}", cache, set())
        if not (is_meaningful_preview_value(material_qty) or is_meaningful_preview_value(service_qty)):
            continue
        row_values = {
            column: evaluate_cell_value(workbook, BOQ_NRO_SHEET_NAME, f"{column}{row_number}", cache, set())
            for column in BOQ_NRO_PREVIEW_COLUMNS
        }
        rows.append({"row": row_number, "values": row_values})
    return {"columns": columns, "rows": rows}

def build_boq_summary(context: BoqContext) -> dict:
    mapped_rows = [row for row in context.preview_rows if row.status == "Mapped"]
    warning_rows = [row for row in context.preview_rows if row.status in {"Warning", "Unmapped", "Skipped"}]
    distribution_total = sum(
        float(row.quantity or 0)
        for row in mapped_rows
        if row.source_folder in {"DISTRIBUTION CABLE", "CABLE"}
    )
    sling_total = sum(
        float(row.quantity or 0)
        for row in mapped_rows
        if row.source_folder == "SLING WIRE"
    )
    subduct_total = sum(
        float(row.quantity or 0)
        for row in mapped_rows
        if row.source_folder in UG_SUBDUCT_ROW_MAP
    )
    return {
        "total_mapped_boq_item": len(mapped_rows),
        "total_warning_unmapped": len(warning_rows),
        "total_distribution_cable_length": int(distribution_total),
        "total_sling_wire_length": int(sling_total),
        "total_subduct_length": int(subduct_total),
        "total_ug_handhole_detected": int(context.raw_counts.get("ug_handhole_detected", 0)),
        "total_ug_twey_detected": int(context.raw_counts.get("ug_twey_detected", 0)),
        "total_fdt_detected": int(context.raw_counts.get("fdt_detected", 0)),
        "total_fat_detected": int(context.raw_counts.get("fat_detected", 0)),
        "total_pole_detected": int(context.raw_counts.get("pole_detected", 0)),
        "total_hp_cover": int(context.hp_cover_count),
        "boq_items": len(mapped_rows),
        "boq_warnings": len(warning_rows),
        "fdt": int(context.raw_counts.get("fdt_detected", 0)),
        "fat": int(context.raw_counts.get("fat_detected", 0)),
        "pole": int(context.raw_counts.get("pole_detected", 0)),
        "cable_length": int(distribution_total),
        "sling_length": int(sling_total),
    }


def build_parse_result(context: BoqContext) -> BoqParseResult:
    cell_values: dict[str, int | float] = {}
    aggregate_distribution_cable(context, cell_values)
    aggregate_sling_wire(context, cell_values)
    aggregate_ug_subduct(context, cell_values)
    aggregate_fdt_pole_mounted(context, cell_values)
    aggregate_fat(context, cell_values)
    aggregate_new_pole(context, cell_values)
    aggregate_ug_handhole(context, cell_values)
    aggregate_ug_twey(context, cell_values)
    aggregate_existing_pole_partner(context, cell_values)
    aggregate_existing_pole_emr(context, cell_values)
    nro_cell_values = aggregate_boq_nro_cluster(context)
    return BoqParseResult(
        cell_values=cell_values,
        nro_cell_values=nro_cell_values,
        preview_rows=context.preview_rows,
        warnings=context.warnings,
        summary=build_boq_summary(context),
        context=context,
    )


def write_distribution_cable_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    for cell, value in parse_result.cell_values.items():
        worksheet[cell] = value


def write_sling_wire_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def write_fdt_pole_mounted_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def write_fat_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def write_new_pole_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def write_existing_pole_partner_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def write_existing_pole_emr_to_boq(worksheet, parse_result: BoqParseResult) -> None:
    write_distribution_cable_to_boq(worksheet, parse_result)


def is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))

def ceil_boq_number(value):
    if not is_finite_number(value):
        return value
    return int(math.ceil(float(value)))

def ceil_boq_decimal_text(value: str):
    decimal_match = re.fullmatch(r"\s*([+-]?\d+),(\d+)\s*", value)
    if not decimal_match:
        return None
    return int(math.ceil(float(f"{decimal_match.group(1)}.{decimal_match.group(2)}")))

def formula_already_rounds_up(formula: str) -> bool:
    body = str(formula or "").strip()
    if body.startswith("="):
        body = body[1:].strip()
    body = strip_outer_parentheses(body).upper()
    return body.startswith("ROUNDUP(") or body.startswith("CEILING(") or body.startswith("CEILING.MATH(")

def wrap_formula_roundup(formula: str) -> str:
    body = str(formula or "").strip()
    if not body.startswith("="):
        return formula
    expression = body[1:].strip()
    if not expression or formula_already_rounds_up(body):
        return body
    return f"=ROUNDUP({expression},0)"

def formula_current_result_is_numeric(workbook, sheet_name: str, formula: str, cache: dict) -> bool:
    body = str(formula or "").strip()
    if not body.startswith("="):
        return False
    try:
        value = evaluate_formula_expression(workbook, sheet_name, body[1:], cache, set())
    except Exception:
        return False
    return is_finite_number(value)

def normalize_boq_workbook_numbers(workbook, pipeline_logger=None) -> int:
    normalized_cells = 0
    formula_cache: dict = {}

    formula_values = {}
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                value = cell.value
                if not (isinstance(value, str) and value.startswith("=")):
                    continue
                try:
                    formula_values[(worksheet.title, cell.coordinate)] = evaluate_formula_expression(
                        workbook,
                        worksheet.title,
                        value[1:],
                        formula_cache,
                        set(),
                    )
                except Exception:
                    formula_values[(worksheet.title, cell.coordinate)] = None

    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    evaluated_value = formula_values.get((worksheet.title, cell.coordinate))
                    if is_finite_number(evaluated_value):
                        cell.value = ceil_boq_number(evaluated_value)
                        cell.number_format = "0"
                    elif isinstance(evaluated_value, str):
                        rounded_text_value = ceil_boq_decimal_text(evaluated_value)
                        if rounded_text_value is not None:
                            cell.value = rounded_text_value
                            cell.number_format = "0"
                        else:
                            cell.value = evaluated_value
                    else:
                        cell.value = None
                    normalized_cells += 1
                    continue
                if is_finite_number(value):
                    rounded_value = ceil_boq_number(value)
                    if value != rounded_value:
                        normalized_cells += 1
                    cell.value = rounded_value
                    cell.number_format = "0"
                    continue
                if isinstance(value, str):
                    rounded_text_value = ceil_boq_decimal_text(value)
                    if rounded_text_value is not None:
                        cell.value = rounded_text_value
                        cell.number_format = "0"
                        normalized_cells += 1
                        continue
    pipeline_log(
        pipeline_logger,
        "INFO",
        f"BOQ value cleanup complete: {normalized_cells} cell(s) finalized for Protected View.",
    )
    return normalized_cells

def clear_ae_nro_manual_cells(workbook, network_type: str | None = "AE", pipeline_logger=None) -> None:
    if str(network_type or "AE").strip().upper() != "AE":
        return
    if BOQ_NRO_SHEET_NAME not in workbook.sheetnames:
        return
    worksheet = workbook[BOQ_NRO_SHEET_NAME]
    cells_to_clear = BOQ_NRO_AE_CLEAR_CELLS + BOQ_NRO_AE_CLEAR_SERVICE_CELLS
    for cell in cells_to_clear:
        worksheet[cell] = None
    pipeline_log(
        pipeline_logger,
        "INFO",
        f"Cleared AE manual NRO cells: {BOQ_NRO_SHEET_NAME}!{', '.join(cells_to_clear)}",
    )


def clear_feeder_nro_service_cells(workbook, pipeline_logger=None) -> None:
    if BOQ_FEEDER_NRO_SHEET_NAME not in workbook.sheetnames:
        return
    worksheet = workbook[BOQ_FEEDER_NRO_SHEET_NAME]
    for cell in BOQ_FEEDER_NRO_CLEAR_SERVICE_CELLS:
        worksheet[cell] = None
    pipeline_log(
        pipeline_logger,
        "INFO",
        f"Cleared feeder NRO service cells: {BOQ_FEEDER_NRO_SHEET_NAME}!{', '.join(BOQ_FEEDER_NRO_CLEAR_SERVICE_CELLS)}",
    )


def _feeder_iter_kml_folders(element, namespace: str, folder_stack: list[str]):
    """Yield (folder_stack, placemark_element) for all placemarks under element."""
    for child in list(element):
        local = local_name(child.tag)
        if local in {"Document", "Folder"}:
            child_name = ""
            if local == "Folder":
                child_name = direct_child_text_by_name(child, "name") or ""
            new_stack = folder_stack + ([child_name] if local == "Folder" else [])
            yield from _feeder_iter_kml_folders(child, namespace, new_stack)
        elif local == "Placemark":
            yield folder_stack, child


def _feeder_folder_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _feeder_nearest_folder(folder_stack: list[str], target_set: set[str]) -> str:
    normalized_targets = {_feeder_folder_key(name) for name in target_set}
    for folder_name in reversed(folder_stack):
        normalized = _feeder_folder_key(folder_name)
        if normalized in normalized_targets:
            return normalized
    return ""


_FEEDER_CABLE_CAPACITY_PATTERN = re.compile(
    r"\b(?:FO\s*)?(\d{2,3})\s*C\s*/\s*(\d{1,2})\s*T\b", re.IGNORECASE
)
_FEEDER_SHORT_CABLE_CAPACITY_PATTERN = re.compile(
    r"\b(?:FO\s*)?(24|36|48|72|96|144|288)\s*C\b", re.IGNORECASE
)
_FEEDER_SHORT_CABLE_CAPACITY_MAP = {
    "24": "24C/2T",
    "36": "36C/3T",
    "48": "48C/4T",
}
_FEEDER_CORE_CAPACITY_MAP = {
    "24": "24C/2T",
    "36": "36C/3T",
    "48": "48C/4T",
    "72": "72C/6T",
    "96": "96C/8T",
    "144": "144C/12T",
    "288": "288C/24T",
}
_FEEDER_TOTAL_LENGTH_PATTERN = re.compile(
    r"TOTAL\s+CABLE\s+LENG(?:TH|HT)\s*:?\s*(\d+(?:[.,]\d+)?)\s*M\b", re.IGNORECASE
)
_FEEDER_TOTAL_ROUTE_PATTERN = re.compile(
    r"TOTAL\s+ROUTE\s*:?\s*(\d+(?:[.,]\d+)?)\s*M\b", re.IGNORECASE
)


def _feeder_capacity_from_text(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = _FEEDER_CABLE_CAPACITY_PATTERN.search(text)
        if match:
            return _FEEDER_CORE_CAPACITY_MAP.get(str(int(match.group(1))))
        match = _FEEDER_SHORT_CABLE_CAPACITY_PATTERN.search(text)
        if match:
            return _FEEDER_CORE_CAPACITY_MAP.get(match.group(1))
    return None


def _feeder_total_length_from_description(description: str | None) -> int:
    if not description:
        return 0
    match = _FEEDER_TOTAL_LENGTH_PATTERN.search(description)
    if match:
        return int(float(match.group(1).replace(",", ".")))
    return 0


def _feeder_route_length_from_description(description: str | None) -> int:
    if not description:
        return 0
    match = _FEEDER_TOTAL_ROUTE_PATTERN.search(description)
    if match:
        return int(float(match.group(1).replace(",", ".")))
    return 0


def _feeder_design_summary_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value).strip()
    if not text or text.startswith("="):
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _sum_feeder_design_summary_cells(worksheet, cells: Sequence[str]) -> int | None:
    total = 0.0
    found = False
    for cell in cells:
        value = _feeder_design_summary_number(worksheet[cell].value)
        if value is None:
            continue
        total += value
        found = True
    if not found:
        return None
    return int(math.ceil(total))


def _feeder_cable_design_summary_label(capacity: str) -> str:
    return f"(FO CABLE AERIAL {capacity.replace('C/', '/')})"


def _apply_feeder_bom_formula_inputs(worksheet, data: dict) -> None:
    for capacity, row in BOQ_FEEDER_CABLE_ROW_MAP.items():
        route_length = data["cable_route"].get(capacity, 0)
        worksheet[f"C{row}"] = math.ceil(route_length) if route_length else 0

    slack_count = int(data.get("slack_hanger_count", 0) or 0)
    for capacity, row in BOQ_FEEDER_SLACK_ROW_MAP.items():
        worksheet[f"C{row}"] = slack_count if data["cable_route"].get(capacity, 0) > 0 and slack_count > 0 else 0


def _feeder_formula_total_cable_length(capacity: str, data: dict) -> int:
    route_length = math.ceil(float(data["cable_route"].get(capacity, 0) or 0))
    if route_length <= 0:
        return 0
    slack_count = int(data.get("slack_hanger_count", 0) or 0)
    return int(math.ceil((route_length + (slack_count * 20)) * 1.05))


def calculate_feeder_boq_design_summary_values(
    input_paths: str | Path | Iterable[str | Path] | None,
    template_path: str | Path,
    network_type: str | None = "AE",
) -> dict[str, int]:
    """Return Feeder DXF design summary overrides from BOQ/KMZ sources."""
    normalized_network_type = str(network_type or "AE").strip().upper()
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError("BOQ Feeder template not found on server.")

    values: dict[str, int] = {}
    workbook = load_workbook(template_path, data_only=False)
    feeder_data = _feeder_collect_data(input_paths) if input_paths else None
    if feeder_data and BOQ_FEEDER_SHEET_NAME in workbook.sheetnames:
        worksheet = workbook[BOQ_FEEDER_SHEET_NAME]
        _apply_feeder_bom_formula_inputs(worksheet, feeder_data)
        cache: dict = {}
        for capacity, row in BOQ_FEEDER_CABLE_TOTAL_ROW_MAP.items():
            value = evaluate_cell_value(workbook, BOQ_FEEDER_SHEET_NAME, f"C{row}", cache, set())
            total = _feeder_design_summary_number(value)
            if total is None:
                total = _feeder_formula_total_cable_length(capacity, feeder_data)
            values[_feeder_cable_design_summary_label(capacity)] = int(math.ceil(float(total or 0)))

    if normalized_network_type != "UG":
        return values

    if BOQ_FEEDER_NRO_SHEET_NAME in workbook.sheetnames:
        worksheet = workbook[BOQ_FEEDER_NRO_SHEET_NAME]
        trenching = _sum_feeder_design_summary_cells(
            worksheet,
            BOQ_FEEDER_NRO_TRENCHING_QUANTITY_CELLS,
        )
        if trenching is None:
            trenching = _sum_feeder_design_summary_cells(
                worksheet,
                BOQ_FEEDER_NRO_TRENCHING_TOTAL_CELLS,
            )
        if trenching is not None:
            values["TOTAL TRENCHING"] = trenching

        boring = _sum_feeder_design_summary_cells(
            worksheet,
            BOQ_FEEDER_NRO_BORING_QUANTITY_CELLS,
        )
        if boring is None:
            boring = _sum_feeder_design_summary_cells(
                worksheet,
                BOQ_FEEDER_NRO_BORING_TOTAL_CELLS,
            )
        if boring is not None:
            values["TOTAL BORING"] = boring

    if "TOTAL BORING" not in values and BOQ_FEEDER_SHEET_NAME in workbook.sheetnames:
        worksheet = workbook[BOQ_FEEDER_SHEET_NAME]
        boring = _sum_feeder_design_summary_cells(worksheet, (f"C{BOQ_FEEDER_BORING_ROW}",))
        if boring is not None:
            values["TOTAL BORING"] = boring

    if "TOTAL BORING" not in values and feeder_data:
        boring_length = feeder_data.get("boring_total", 0)
        if boring_length:
            values["TOTAL BORING"] = int(math.ceil(float(boring_length)))

    return values


def _feeder_collect_data(input_paths) -> dict:
    """Collect Feeder-specific BOQ data directly from KMZ files."""
    from converter import read_kml_root, normalize_input_paths, is_kml_path

    paths = [path for path in normalize_input_paths(input_paths) if is_kml_path(path)]
    feeder_folder_set = {
        "OLT", "CABLE", "JOINT CLOSURE", "SLACK HANGER",
        "NEW POLE 7-2.5", "NEW POLE 7-3", "NEW POLE 7-4", "NEW POLE 7-5",
        "NEW POLE 9-4", "NEW POLE 9-5",
        "EXISTING POLE EMR 7-2.5", "EXISTING POLE EMR 7-3", "EXISTING POLE EMR 7-4",
        "EXISTING POLE EMR 7-5", "EXISTING POLE EMR 9-4", "EXISTING POLE EMR 9-5",
        "EXISTING POLE PARTNER 7-4", "EXISTING POLE PARTNER 9-4",
        "DISTRIBUTION CABLE", "SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING", "BORING",
    }

    cable_route_by_capacity: dict[str, float] = {}
    cable_total_length_by_capacity: dict[str, int] = {}
    slack_hanger_count = 0
    joint_closure_new_by_capacity: dict[int, int] = {}
    new_pole_counts: dict[str, int] = {}
    existing_emr_total = 0
    partner_counts: dict[str, int] = {"EXT.LN": 0, "EXT.TLKM": 0, "EXT.MR": 0, "EXT.MTI": 0}
    subduct_total = 0.0
    boring_total = 0.0

    for path in paths:
        try:
            root = read_kml_root(path)
        except Exception:
            continue
        ns = ""  # namespace not needed for our purposes
        for folder_stack, placemark in _feeder_iter_kml_folders(root, ns, []):
            source_folder = _feeder_nearest_folder(folder_stack, feeder_folder_set)
            if not source_folder:
                continue

            name = direct_child_text_by_name(placemark, "name") or ""
            description = direct_child_text_by_name(placemark, "description") or ""
            upper_name = name.upper()

            if source_folder in {"CABLE", "DISTRIBUTION CABLE"}:
                # Cable: extract capacity, route length, and total length
                linestrings = placemark_linestrings(placemark)
                if not linestrings:
                    continue
                capacity = _feeder_capacity_from_text(name, description, " ".join(folder_stack))
                if not capacity:
                    continue
                # Route length: prefer placemark geometry length, fallback to description
                geom_length = calculate_linestring_length_meter(linestrings)
                route_length = geom_length if geom_length > 0 else _feeder_route_length_from_description(description)
                cable_route_by_capacity[capacity] = cable_route_by_capacity.get(capacity, 0) + route_length
                # Total length from description "Total Cable Length: xxx m"
                total_length = _feeder_total_length_from_description(description)
                if not total_length:
                    # Fallback: extract from name "... - 526 m"
                    name_match = re.search(r"-\s*(\d+(?:[.,]\d+)?)\s*m\b", name, re.IGNORECASE)
                    if name_match:
                        total_length = int(float(name_match.group(1).replace(",", ".")))
                cable_total_length_by_capacity[capacity] = cable_total_length_by_capacity.get(capacity, 0) + total_length

            elif source_folder == "SLACK HANGER":
                slack_hanger_count += 1

            elif source_folder == "JOINT CLOSURE":
                # Only count NEW (skip EXT)
                if "EXT" in upper_name:
                    continue
                # Detect capacity (24, 36, 48, 72, 96, 144, 288)
                cap_match = re.search(r"\b(24|36|48|72|96|144|288)\s*C?\b", upper_name)
                if cap_match:
                    cap = int(cap_match.group(1))
                    joint_closure_new_by_capacity[cap] = joint_closure_new_by_capacity.get(cap, 0) + 1

            elif source_folder.startswith("NEW POLE"):
                new_pole_counts[source_folder] = new_pole_counts.get(source_folder, 0) + 1

            elif source_folder.startswith("EXISTING POLE EMR"):
                existing_emr_total += 1

            elif source_folder.startswith("EXISTING POLE PARTNER"):
                # Classify by name prefix (EXT.LN, EXT.TLKM, EXT.MR, EXT.MTI)
                if re.search(r"\bEXT\.LN\b", upper_name):
                    partner_counts["EXT.LN"] += 1
                elif re.search(r"\bEXT\.TLKM\b|\bEXT\.TELKOM\b", upper_name):
                    partner_counts["EXT.TLKM"] += 1
                elif re.search(r"\bEXT\.MTI\b", upper_name):
                    partner_counts["EXT.MTI"] += 1
                elif re.search(r"\bEXT\.MR\b", upper_name):
                    partner_counts["EXT.MR"] += 1

            elif source_folder in {"SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"}:
                linestrings = placemark_linestrings(placemark)
                if linestrings:
                    subduct_total += calculate_linestring_length_meter(linestrings) or 0

            elif source_folder == "BORING":
                linestrings = placemark_linestrings(placemark)
                if linestrings:
                    boring_total += calculate_linestring_length_meter(linestrings) or 0
                else:
                    boring_total += _feeder_route_length_from_description(description)

    return {
        "cable_route": cable_route_by_capacity,
        "cable_total_length": cable_total_length_by_capacity,
        "slack_hanger_count": slack_hanger_count,
        "joint_closure_new": joint_closure_new_by_capacity,
        "new_pole": new_pole_counts,
        "existing_emr_total": existing_emr_total,
        "partner_counts": partner_counts,
        "subduct_total": subduct_total,
        "boring_total": boring_total,
    }


def _append_feeder_preview_row(
    context: BoqContext,
    cell_values: dict[str, int | float],
    *,
    source_folder: str,
    item: str,
    quantity: int | float,
    unit: str,
    target_cell_name: str,
    cable_type: str = "",
) -> None:
    if target_cell_name:
        write_cell_value(cell_values, target_cell_name, quantity)
    context.preview_rows.append(
        BoqPreviewRow(
            source_folder=source_folder,
            parent_line_fdt="Feeder",
            item=item,
            cable_type=cable_type,
            quantity=quantity,
            unit=unit,
            target_cell=target_cell_name,
            status="Mapped",
        )
    )


def _feeder_total_core_sf(fdt_coding_count: int | None) -> int:
    try:
        count = int(fdt_coding_count or 0)
    except (TypeError, ValueError):
        count = 0
    return max(count, 0) * BOQ_FEEDER_TOTAL_CORE_PER_FDT

def build_feeder_parse_result(
    context: BoqContext,
    data: dict,
    network_type: str | None = "AE",
    fdt_coding_count: int | None = None,
) -> BoqParseResult:
    cell_values: dict[str, int | float] = {}

    for capacity, row in BOQ_FEEDER_CABLE_ROW_MAP.items():
        route_length = data["cable_route"].get(capacity, 0)
        if route_length:
            quantity = math.ceil(route_length)
            _append_feeder_preview_row(
                context,
                cell_values,
                source_folder="CABLE",
                item=f"Total Route {capacity}",
                cable_type=capacity,
                quantity=quantity,
                unit="m",
                target_cell_name=f"C{row}",
            )

    slack_count = int(data.get("slack_hanger_count", 0) or 0)
    for capacity, row in BOQ_FEEDER_SLACK_ROW_MAP.items():
        if data["cable_route"].get(capacity, 0) > 0 and slack_count > 0:
            _append_feeder_preview_row(
                context,
                cell_values,
                source_folder="SLACK HANGER",
                item=f"Total Slack {capacity}",
                cable_type=capacity,
                quantity=slack_count,
                unit="unit",
                target_cell_name=f"C{row}",
            )

    for capacity, row in BOQ_FEEDER_JOINT_CLOSURE_INLINE_ROW_MAP.items():
        count = int(data["joint_closure_new"].get(capacity, 0) or 0)
        if count:
            _append_feeder_preview_row(
                context,
                cell_values,
                source_folder="JOINT CLOSURE",
                item=f"Joint Closure NEW {capacity}C",
                cable_type=f"{capacity}C",
                quantity=count,
                unit="unit",
                target_cell_name=f"C{row}",
            )

    for pole_folder, row in BOQ_FEEDER_POLE_ROW_MAP.items():
        if pole_folder.startswith("NEW POLE"):
            count = int(data["new_pole"].get(pole_folder, 0) or 0)
            if count:
                _append_feeder_preview_row(
                    context,
                    cell_values,
                    source_folder=pole_folder,
                    item=pole_folder,
                    quantity=count,
                    unit="unit",
                    target_cell_name=f"C{row}",
                )

    partner_counts = data.get("partner_counts", {})
    partner_preview = (
        ("EXT.LN", "EXISTING POLE PARTNER 7-4", "Existing Pole LN", "C55"),
        ("EXT.TLKM", "EXISTING POLE PARTNER 7-4", "Existing Pole TELKOM", "C56"),
    )
    for key, source_folder, item, cell in partner_preview:
        count = int(partner_counts.get(key, 0) or 0)
        if count:
            _append_feeder_preview_row(
                context,
                cell_values,
                source_folder=source_folder,
                item=item,
                quantity=count,
                unit="unit",
                target_cell_name=cell,
            )

    emr_total = (
        int(data.get("existing_emr_total", 0) or 0)
        + int(partner_counts.get("EXT.MR", 0) or 0)
        + int(partner_counts.get("EXT.MTI", 0) or 0)
    )
    if emr_total:
        _append_feeder_preview_row(
            context,
            cell_values,
            source_folder="EXISTING POLE EMR",
            item="Existing Pole EMR/MTI",
            quantity=emr_total,
            unit="unit",
            target_cell_name="C59",
        )

    total_core_sf = _feeder_total_core_sf(fdt_coding_count)
    if total_core_sf:
        _append_feeder_preview_row(
            context,
            cell_values,
            source_folder="FDT CODING",
            item="Total Core SF",
            quantity=total_core_sf,
            unit="core",
            target_cell_name=f"C{BOQ_FEEDER_TOTAL_CORE_SF_ROW}",
        )

    if str(network_type or "").upper() == "UG" and data.get("subduct_total"):
        quantity = math.ceil(float(data["subduct_total"]))
        _append_feeder_preview_row(
            context,
            cell_values,
            source_folder="SUBDUCT 32/28",
            item="Subduct",
            quantity=quantity,
            unit="m",
            target_cell_name=f"C{BOQ_FEEDER_SUBDUCT_ROW}",
        )

    mapped_rows = [row for row in context.preview_rows if row.status == "Mapped"]
    cable_total = sum(float(row.quantity or 0) for row in mapped_rows if row.source_folder == "CABLE")
    subduct_total = sum(float(row.quantity or 0) for row in mapped_rows if row.source_folder in UG_SUBDUCT_ROW_MAP)
    pole_total = (
        sum(int(value or 0) for value in data.get("new_pole", {}).values())
        + int(data.get("existing_emr_total", 0) or 0)
        + sum(int(value or 0) for value in partner_counts.values())
    )
    context.raw_counts["pole_detected"] = pole_total

    summary = {
        "total_mapped_boq_item": len(mapped_rows),
        "total_warning_unmapped": 0,
        "total_distribution_cable_length": int(cable_total),
        "total_sling_wire_length": 0,
        "total_subduct_length": int(subduct_total),
        "total_ug_handhole_detected": 0,
        "total_ug_twey_detected": 0,
        "total_fdt_detected": 0,
        "total_fdt_coding_detected": total_core_sf // BOQ_FEEDER_TOTAL_CORE_PER_FDT if total_core_sf else 0,
        "total_fat_detected": 0,
        "total_pole_detected": pole_total,
        "total_core_sf": total_core_sf,
        "total_hp_cover": 0,
        "boq_items": len(mapped_rows),
        "boq_warnings": 0,
        "fdt": 0,
        "fat": 0,
        "pole": pole_total,
        "cable_length": int(cable_total),
        "sling_length": 0,
    }
    return BoqParseResult(
        cell_values=cell_values,
        nro_cell_values={},
        preview_rows=context.preview_rows,
        warnings=[],
        summary=summary,
        context=context,
    )


def parse_feeder_kmz_structure(
    input_paths: str | Path | Iterable[str | Path],
    pipeline_logger=None,
    progress_callback=None,
    cluster_name_override: str | None = None,
    olt_name: str | None = None,
    network_type: str | None = "AE",
    fdt_coding_count: int | None = None,
) -> BoqParseResult:
    paths = [path for path in normalize_input_paths(input_paths) if is_kml_path(path)]
    if not paths:
        raise ValueError("KML/KMZ file is required for BOQ export.")

    context = BoqContext(logger=pipeline_logger)
    context.cluster_name = str(cluster_name_override or "").strip() or extract_cluster_name_from_kmz_filename(paths[0])
    context.olt_name = str(olt_name or "").strip()
    context.processed_files = len(paths)
    pipeline_progress(progress_callback, pipeline_logger, 18, "Validating KML/KMZ")
    pipeline_log(pipeline_logger, "INFO", "Parsing Feeder KMZ/KML folder structure")
    pipeline_log(pipeline_logger, "INFO", f"Detected cluster name: {context.cluster_name}")
    if context.olt_name:
        pipeline_log(pipeline_logger, "INFO", f"Detected OLT name: {context.olt_name}")

    data = _feeder_collect_data(paths)
    return build_feeder_parse_result(context, data, network_type=network_type, fdt_coding_count=fdt_coding_count)


def write_boq_feeder(
    template_path: str | Path,
    output_path: str | Path,
    parse_result,
    input_paths=None,
    pipeline_logger=None,
    network_type: str | None = "AE",
    fdt_coding_count: int | None = None,
    scope_type: str | None = "Feeder",
) -> dict:
    """Write BOQ Feeder Excel ??? flat structure, single column C for quantities."""
    template_path = Path(template_path)
    output_path = Path(output_path)

    if not template_path.exists():
        raise FileNotFoundError("BOQ Feeder template not found on server.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, output_path)
    pipeline_log(pipeline_logger, "SUCCESS", "BOQ Feeder template loaded")

    workbook = load_workbook(output_path)
    if BOQ_FEEDER_SHEET_NAME not in workbook.sheetnames:
        raise ValueError(f'Sheet "{BOQ_FEEDER_SHEET_NAME}" not found in BOQ Feeder template.')

    worksheet = workbook[BOQ_FEEDER_SHEET_NAME]
    pipeline_log(pipeline_logger, "SUCCESS", f'Sheet "{BOQ_FEEDER_SHEET_NAME}" found')
    feeder_rule_scope = "feeder_ug" if str(network_type or "").upper() == "UG" else "feeder_ae"
    apply_boq_rule_cells(workbook, feeder_rule_scope, pipeline_logger=pipeline_logger)

    # Collect data directly from KMZ
    if input_paths:
        data = _feeder_collect_data(input_paths)
    else:
        pipeline_log(pipeline_logger, "WARNING", "No input paths for Feeder BOQ; using empty data.")
        data = {
            "cable_route": {}, "cable_total_length": {}, "slack_hanger_count": 0,
            "joint_closure_new": {}, "new_pole": {}, "existing_emr_total": 0,
            "partner_counts": {"EXT.LN": 0, "EXT.TLKM": 0, "EXT.MR": 0, "EXT.MTI": 0},
            "subduct_total": 0,
            "boring_total": 0,
        }

    # Rows 2-8: Total Route to FDT (panjang line per capacity, column C)
    for capacity, row in BOQ_FEEDER_CABLE_ROW_MAP.items():
        route_length = data["cable_route"].get(capacity, 0)
        if route_length:
            worksheet[f"C{row}"] = math.ceil(route_length)
            pipeline_log(pipeline_logger, "INFO", f"Total Route {capacity}: {math.ceil(route_length)}m ??? C{row}")

    # Rows 9-15: Total Panjang Kabel ??? sum of per-cable total lengths from
    # the KMZ. The template formula =ROUNDUP(((C2)+(C16*20))*1.05,0) computes
    # one big roundup for the whole capacity, which differs from the KMZ by
    # ~1 m because each cable is roundup-ed individually in the KMZ. Use the
    # KMZ-derived per-cable totals so BOQ matches KMZ exactly.
    cable_total_by_capacity = data.get("cable_total_length", {}) or {}
    for capacity, row in BOQ_FEEDER_CABLE_TOTAL_ROW_MAP.items():
        per_cable_total = int(cable_total_by_capacity.get(capacity, 0) or 0)
        if per_cable_total:
            worksheet[f"C{row}"] = per_cable_total
            pipeline_log(
                pipeline_logger,
                "INFO",
                f"Total Panjang Kabel {capacity}: {per_cable_total}m (sum of per-cable KMZ totals) ??? C{row}",
            )

    # Rows 16-22: Total Coil/Slack ??? total slack hanger count distributed per capacity
    # Each cable capacity that has data gets the slack count
    slack_count = data["slack_hanger_count"]
    for capacity, row in BOQ_FEEDER_SLACK_ROW_MAP.items():
        # Only fill if this capacity has cable data
        if data["cable_route"].get(capacity, 0) > 0 and slack_count > 0:
            worksheet[f"C{row}"] = slack_count
            pipeline_log(pipeline_logger, "INFO", f"Total Slack {capacity}: {slack_count} ??? C{row}")

    # Rows 23-29: Joint Closure (NEW only)
    for capacity, row in BOQ_FEEDER_JOINT_CLOSURE_INLINE_ROW_MAP.items():
        count = data["joint_closure_new"].get(capacity, 0)
        if count:
            worksheet[f"C{row}"] = count
            pipeline_log(pipeline_logger, "INFO", f"Joint Closure NEW {capacity}C: {count} ??? C{row}")

    # Rows 49-54: New Pole counts
    pole_row_mapping = {
        "NEW POLE 7-4": 49,
        "NEW POLE 7-3": 50,
        "NEW POLE 7-2.5": 51,
        "NEW POLE 7-5": 52,
        "NEW POLE 9-4": 53,
        "NEW POLE 9-5": 54,
    }
    for pole_folder, row in pole_row_mapping.items():
        count = data["new_pole"].get(pole_folder, 0)
        if count:
            worksheet[f"C{row}"] = count
            pipeline_log(pipeline_logger, "INFO", f"{pole_folder}: {count} ??? C{row}")

    # Rows 55, 56, 59: Existing pole partners by name prefix
    if data["partner_counts"]["EXT.LN"]:
        worksheet["C55"] = data["partner_counts"]["EXT.LN"]
        pipeline_log(pipeline_logger, "INFO", f"EXT.LN: {data['partner_counts']['EXT.LN']} ??? C55")
    if data["partner_counts"]["EXT.TLKM"]:
        worksheet["C56"] = data["partner_counts"]["EXT.TLKM"]
        pipeline_log(pipeline_logger, "INFO", f"EXT.TLKM: {data['partner_counts']['EXT.TLKM']} ??? C56")
    # Row 59: Existing Pole EMR ??? combine EXT.MR, EXT.MTI from partner folders + EXISTING POLE EMR folders
    emr_total = data["existing_emr_total"] + data["partner_counts"]["EXT.MR"] + data["partner_counts"]["EXT.MTI"]
    if emr_total:
        worksheet["C59"] = emr_total
        pipeline_log(pipeline_logger, "INFO", f"Existing Pole EMR/MTI: {emr_total} ??? C59")

    total_core_sf = _feeder_total_core_sf(fdt_coding_count)
    if not total_core_sf:
        parse_cell_values = getattr(parse_result, "cell_values", {}) or {}
        total_core_sf = int(parse_cell_values.get(f"C{BOQ_FEEDER_TOTAL_CORE_SF_ROW}") or 0)
    if total_core_sf:
        worksheet[f"C{BOQ_FEEDER_TOTAL_CORE_SF_ROW}"] = total_core_sf
        pipeline_log(
            pipeline_logger,
            "INFO",
            f"Total Core SF from FDT coding: {total_core_sf} ??? C{BOQ_FEEDER_TOTAL_CORE_SF_ROW}",
        )

    # Subduct (UG only)
    if str(network_type or "").upper() == "UG" and data["subduct_total"]:
        worksheet[f"C{BOQ_FEEDER_SUBDUCT_ROW}"] = math.ceil(data["subduct_total"])
        pipeline_log(pipeline_logger, "INFO", f"Subduct: {math.ceil(data['subduct_total'])}m ??? C{BOQ_FEEDER_SUBDUCT_ROW}")

    if str(scope_type or "").strip().lower() in {"subfeeder", "sub feeder", "sub-feeder"}:
        worksheet["C62"] = 0
        pipeline_log(pipeline_logger, "INFO", "Total Core MF/HF skipped for Subfeeder -> C62=0")
    else:
        cable_capacities = [k for k in data["cable_route"].keys() if re.match(r"\d+C", k)]
        if cable_capacities:
            primary_capacity = cable_capacities[0]
            capacity_number = int(re.search(r"(\d+)", primary_capacity).group(1))
            worksheet["C62"] = capacity_number
            pipeline_log(pipeline_logger, "INFO", f"Kabel capacity: {primary_capacity} -> {capacity_number} -> C62")

    # NRO sheet metadata
    if BOQ_FEEDER_NRO_SHEET_NAME in workbook.sheetnames:
        nro_ws = workbook[BOQ_FEEDER_NRO_SHEET_NAME]
        context = getattr(parse_result, "context", None)
        if context:
            if context.olt_name:
                nro_ws[BOQ_FEEDER_NRO_OLT_CELL] = context.olt_name
            if context.cluster_name:
                nro_ws[BOQ_FEEDER_NRO_CLUSTER_CELL] = context.cluster_name

    clear_feeder_nro_service_cells(workbook, pipeline_logger=pipeline_logger)

    workbook.calculation.calcMode = "auto"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True

    workbook.save(output_path)
    # Replace formulas with their evaluated values so Excel Protected View
    # shows the numbers immediately without "Enable Editing".
    freeze_workbook_cached_values(output_path)
    pipeline_log(pipeline_logger, "SUCCESS", "BOQ Feeder Excel generated successfully")
    return {"columns": [], "rows": []}



def _cluster_boq_sheet_name_for_network(network_type: str | None) -> str:
    return BOQ_SHEET_NAME_UG if str(network_type or "AE").strip().upper() == "UG" else BOQ_SHEET_NAME

def _resolve_cluster_boq_sheet(workbook, network_type):
    normalized = str(network_type or "AE").strip().upper()
    if normalized == "UG" and BOQ_SHEET_NAME_UG in workbook.sheetnames:
        return BOQ_SHEET_NAME_UG
    if BOQ_SHEET_NAME in workbook.sheetnames:
        return BOQ_SHEET_NAME
    for name in workbook.sheetnames:
        if name.startswith("BoM CLUSTER"):
            return name
    raise ValueError(
        f"No BoM CLUSTER sheet found (looked for '{BOQ_SHEET_NAME}' / '{BOQ_SHEET_NAME_UG}'). "
        f"Sheets: {workbook.sheetnames}"
    )


def write_boq_cluster_ae(
    template_path: str | Path,
    output_path: str | Path,
    parse_result: BoqParseResult,
    pipeline_logger=None,
    network_type: str | None = "AE",
) -> dict:
    template_path = Path(template_path)
    output_path = Path(output_path)

    if not template_path.exists():
        raise FileNotFoundError("BOQ template not found on server.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, output_path)
    pipeline_log(pipeline_logger, "SUCCESS", "BOQ template loaded")

    workbook = load_workbook(output_path)
    boq_sheet_name = _resolve_cluster_boq_sheet(workbook, network_type)
    if BOQ_NRO_SHEET_NAME not in workbook.sheetnames:
        raise ValueError(f'Sheet "{BOQ_NRO_SHEET_NAME}" not found in BOQ template.')

    populate_boq_workbook_values(workbook, parse_result, pipeline_logger=pipeline_logger, network_type=network_type, _boq_sheet_name=boq_sheet_name)
    apply_boq_rule_cells(workbook, "cluster", pipeline_logger=pipeline_logger)
    nro_preview = build_boq_nro_cluster_preview(workbook)

    try:
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
    except AttributeError:
        pass

    workbook.save(output_path)
    # Replace formulas with their evaluated values so Excel Protected View
    # shows the numbers immediately without "Enable Editing".
    freeze_workbook_cached_values(output_path)
    return nro_preview


def populate_boq_workbook_values(
    workbook,
    parse_result: BoqParseResult,
    pipeline_logger=None,
    network_type: str | None = "AE",
    _boq_sheet_name: str | None = None,
) -> None:
    boq_sheet_name = _boq_sheet_name or _resolve_cluster_boq_sheet(workbook, network_type)
    if BOQ_NRO_SHEET_NAME not in workbook.sheetnames:
        raise ValueError(f'Sheet "{BOQ_NRO_SHEET_NAME}" not found in BOQ template.')

    worksheet = workbook[boq_sheet_name]
    pipeline_log(pipeline_logger, "SUCCESS", f'Sheet "{boq_sheet_name}" found')
    for cell, value in parse_result.cell_values.items():
        worksheet[cell] = value

    nro_worksheet = workbook[BOQ_NRO_SHEET_NAME]
    pipeline_log(pipeline_logger, "SUCCESS", f'Sheet "{BOQ_NRO_SHEET_NAME}" found')
    for cell, value in parse_result.nro_cell_values.items():
        nro_worksheet[cell] = value

    normalize_boq_workbook_numbers(workbook, pipeline_logger=pipeline_logger)
    clear_ae_nro_manual_cells(workbook, network_type=network_type, pipeline_logger=pipeline_logger)


def calculate_boq_nro_cell_values(
    input_paths: str | Path | Iterable[str | Path],
    template_path: str | Path,
    cells: Iterable[str],
) -> dict[str, int | float | str | None]:
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError("BOQ template not found on server.")

    parse_result = parse_kmz_structure(input_paths)
    workbook = load_workbook(template_path)
    populate_boq_workbook_values(workbook, parse_result)
    worksheet = workbook[BOQ_NRO_SHEET_NAME]
    values: dict[str, int | float | str | None] = {}
    for cell in cells:
        coordinate = str(cell).strip().upper()
        if coordinate:
            values[coordinate] = normalize_preview_number(worksheet[coordinate].value)
    return values


def generate_boq_excel(
    input_paths: str | Path | Iterable[str | Path],
    output_path: str | Path,
    template_path: str | Path,
    pipeline_logger=None,
    progress_callback=None,
    cluster_name_override: str | None = None,
    olt_name: str | None = None,
    network_type: str | None = "AE",
    scope_type: str | None = "Cluster",
    fdt_coding_count: int | None = None,
) -> dict:
    started_at = time.perf_counter()
    is_feeder = _is_feeder_like_scope(scope_type)
    pipeline_progress(progress_callback, pipeline_logger, 12, "Reading input files")
    if is_feeder:
        parse_result = parse_feeder_kmz_structure(
            input_paths,
            pipeline_logger=pipeline_logger,
            progress_callback=progress_callback,
            cluster_name_override=cluster_name_override,
            olt_name=olt_name,
            network_type=network_type,
            fdt_coding_count=fdt_coding_count,
        )
    else:
        parse_result = parse_kmz_structure(
            input_paths,
            pipeline_logger=pipeline_logger,
            progress_callback=progress_callback,
            cluster_name_override=cluster_name_override,
            olt_name=olt_name,
        )

    pipeline_progress(progress_callback, pipeline_logger, 42, "Loading BOQ template")
    pipeline_progress(progress_callback, pipeline_logger, 48, f"Reading sheet {_cluster_boq_sheet_name_for_network(network_type)}")
    pipeline_progress(progress_callback, pipeline_logger, 55, "Calculating Distribution Cable")
    pipeline_progress(progress_callback, pipeline_logger, 61, "Calculating Sling Wire")
    pipeline_progress(progress_callback, pipeline_logger, 67, "Calculating FDT Pole Mounted")
    pipeline_progress(progress_callback, pipeline_logger, 73, "Calculating FAT")
    pipeline_progress(progress_callback, pipeline_logger, 78, "Calculating New Pole")
    pipeline_progress(progress_callback, pipeline_logger, 83, "Calculating Existing Pole Partner")
    pipeline_progress(progress_callback, pipeline_logger, 88, "Calculating Existing Pole EMR")
    pipeline_progress(progress_callback, pipeline_logger, 93, "Writing BOQ Excel")
    if is_feeder:
        nro_preview = write_boq_feeder(
            template_path,
            output_path,
            parse_result,
            input_paths=input_paths,
            pipeline_logger=pipeline_logger,
            network_type=network_type,
            fdt_coding_count=fdt_coding_count,
            scope_type=scope_type,
        )
    else:
        nro_preview = write_boq_cluster_ae(
            template_path,
            output_path,
            parse_result,
            pipeline_logger=pipeline_logger,
            network_type=network_type,
        )
    pipeline_progress(progress_callback, pipeline_logger, 96, "Preparing download")
    pipeline_log(pipeline_logger, "SUCCESS", "BOQ Excel generated successfully")

    output_path = Path(output_path)
    summary = {
        **parse_result.summary,
        "output_size": output_path.stat().st_size if output_path.exists() else 0,
        "duration_seconds": time.perf_counter() - started_at,
    }
    return {
        "output_path": str(output_path),
        "summary": summary,
        "preview_rows": [row.to_dict() for row in parse_result.preview_rows],
        "warnings": parse_result.warnings,
        "cell_values": parse_result.cell_values,
        "nro_cell_values": parse_result.nro_cell_values,
        "nro_preview": nro_preview,
    }
