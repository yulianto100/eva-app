import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from pyproj import Transformer

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
KML_NS = {"kml": KML_NAMESPACE}
DEFAULT_TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


@dataclass(frozen=True)
class PlacemarkRecord:
    name: str
    folder_stack: tuple[str, ...]
    placemark: ET.Element


def normalized_folder_name(folder_name: str | None) -> str:
    return re.sub(r"\s+", " ", (folder_name or "").strip()).upper()


def parse_coordinate_string(text: str | None) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    for token in re.split(r"\s+", (text or "").strip()):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            coordinates.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return coordinates


def project_point(lon: float, lat: float, transformer: Transformer | None = None) -> tuple[float, float]:
    active_transformer = transformer or DEFAULT_TRANSFORMER
    x, y = active_transformer.transform(lon, lat)
    return float(x), float(y)


def project_coordinates(
    coordinates: Iterable[tuple[float, float]],
    transformer: Transformer | None = None,
) -> list[tuple[float, float]]:
    return [project_point(lon, lat, transformer) for lon, lat in coordinates]


def point_distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def linestring_distance(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(point_distance(first, second) for first, second in zip(points, points[1:], strict=False))


def load_kml_root(path: str | Path) -> ET.Element:
    source_path = Path(path)
    if source_path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(source_path) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError(f"KMZ tidak berisi file KML: {source_path.name}")
            data = archive.read(kml_names[0])
    else:
        data = source_path.read_bytes()
    return ET.fromstring(data)


def iter_placemarks(root: ET.Element) -> Iterator[PlacemarkRecord]:
    yield from _iter_container(root, ())


def _iter_container(element: ET.Element, folder_stack: tuple[str, ...]) -> Iterator[PlacemarkRecord]:
    for child in list(element):
        tag = _local_name(child.tag)
        if tag in {"Document", "Folder"}:
            name = _child_text(child, "name")
            next_stack = (*folder_stack, name) if tag == "Folder" and name else folder_stack
            yield from _iter_container(child, next_stack)
        elif tag == "Placemark":
            yield PlacemarkRecord(
                name=_child_text(child, "name"),
                folder_stack=folder_stack,
                placemark=child,
            )


def placemark_coordinates(placemark: ET.Element) -> list[list[tuple[float, float]]]:
    coordinate_sets: list[list[tuple[float, float]]] = []
    for coordinate_node in placemark.findall(".//kml:coordinates", KML_NS):
        parsed = parse_coordinate_string(coordinate_node.text)
        if parsed:
            coordinate_sets.append(parsed)
    return coordinate_sets


def extract_fat_code(value: str | None) -> str | None:
    match = re.search(r"\b([A-Z]\d{1,2})\b", value or "", re.IGNORECASE)
    return match.group(1).upper() if match else None


def extract_fat_code_from_path(folder_stack: Sequence[str], name: str = "") -> str | None:
    for value in [name, *reversed(folder_stack)]:
        code = extract_fat_code(value)
        if code:
            return code
    return None


def extract_fdt_code(value: str | None) -> str | None:
    match = re.search(r"\bFDT\s*[-_ ]*(\d+)\b", value or "", re.IGNORECASE)
    return f"FDT {int(match.group(1))}" if match else None


def classify_pole_provider(name: str | None) -> str | None:
    normalized = normalized_folder_name(name)
    if any(token in normalized for token in ("LINKNET", "LN", "FM")):
        return "LN"
    if "MORATEL" in normalized or "MTI" in normalized:
        return "MTI"
    if any(token in normalized for token in ("TELKOM", "TELECOM", "TELEKOM", "TLKOM", "TLKM")):
        return "TLKM"
    if "MR" in normalized or "EMR" in normalized:
        return "MR"
    return None


def parse_cable_spec(name: str | None) -> dict[str, int | None]:
    text = name or ""
    core_match = re.search(r"\b(\d+)\s*C\b", text, re.IGNORECASE)
    tube_match = re.search(r"\b(\d+)\s*T\b", text, re.IGNORECASE)
    length_match = re.search(r"\b(\d+(?:\.\d+)?)\s*M\b", text, re.IGNORECASE)
    return {
        "core_count": int(core_match.group(1)) if core_match else None,
        "tube_count": int(tube_match.group(1)) if tube_match else None,
        "length_meter": int(float(length_match.group(1))) if length_match else None,
    }


def _child_text(element: ET.Element, child_name: str) -> str:
    child = element.find(f"kml:{child_name}", KML_NS)
    return (child.text or "").strip() if child is not None else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
