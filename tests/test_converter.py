import logging
import math
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon as ShapelyPolygon
from shapely.ops import unary_union


ezdxf = pytest.importorskip("ezdxf")
pytest.importorskip("osmium")
pytest.importorskip("pyproj")
from ezdxf import bbox

from converter import (
    DISTRIBUTION_CABLE_CORE_LAYER_COLORS,
    DXF_LAYERS,
    FAT_CALLOUT_BLOCK_NAME,
    FAT_CALLOUT_LAYER,
    FDT_DESIGN_SUMMARY_CLEAR_BOUNDS,
    FDT_LAYOUT_PREFIX,
    KML_DISTRIBUTION_CABLE_WIDTH,
    KML_FAT_ATTR_TAG,
    KML_FAT_COVERAGE_ATTR_TAG,
    KML_FAT_AREA_COLOR,
    KML_FAT_AREA_LAYER,
    KML_FAT_AREA_LINETYPE,
    KML_FAT_BLOCK_NAME,
    KML_FAT_CODE_HEIGHT_METERS,
    KML_FAT_ICON_HEIGHT_METERS,
    KML_FAT_ICON_WIDTH_METERS,
    KML_FAT_LAYER,
    KML_FDT_ATTR_TAG,
    KML_FDT_BLOCK_NAME,
    KML_FDT_CAPACITY_TRUE_COLORS,
    KML_FDT_LAYER,
    KML_FDT_ICON_HEIGHT_METERS,
    KML_FDT_ICON_WIDTH_METERS,
    KML_HOME_NUMBER_BOX_LAYER,
    KML_HOME_NUMBER_LAYER,
    KML_HOME_NUMBER_HEIGHT_METERS,
    KML_HOME_NUMBER_TEXT_CLEARANCE_METERS,
    KML_HANDHOLE_20_BLOCK_NAME,
    KML_HANDHOLE_20_COLOR,
    KML_HANDHOLE_20_LAYER,
    KML_HANDHOLE_40_BLOCK_NAME,
    KML_HANDHOLE_40_LAYER,
    KML_HANDHOLE_80_BLOCK_NAME,
    KML_HANDHOLE_80_LAYER,
    KML_JOINT_CLOSURE_BLOCK_NAME,
    KML_JOINT_CLOSURE_COLOR,
    KML_JOINT_CLOSURE_LAYER,
    KML_OLT_BLOCK_NAME,
    KML_OLT_LAYER,
    KML_DISTANCE_LABEL_COLOR,
    KML_DISTANCE_LABEL_LAYER,
    KML_POLE_BLOCK_PREFIX,
    KML_POLE_ICON_SIZE_METERS,
    KML_POLE_ID_HEIGHT_METERS,
    KML_SLACK_HANGER_BLOCK_NAME,
    KML_SUBDUCT_32_LINEWEIGHT,
    KML_SUBDUCT_32_WIDTH,
    KML_TWEY_BLOCK_NAME,
    KML_TWEY_ICON_SIZE_METERS,
    KML_TWEY_LAYER,
    OSMToDXFHandler,
    STANDARD_KML_FOLDERS,
    TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_CENTER,
    TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_SIZE,
    TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_CENTER,
    TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_SIZE,
    TEMPLATE_LAYER,
    TEMPLATE_LAYOUT_NAME,
    classify_tags,
    convert_osm_to_dxf,
    distribution_cable_core_layer,
    distribution_cable_summary_key,
    estimate_road_half_width_meters,
    extract_fat_code_from_path,
    kml_handhole_source_code,
    kml_pole_folder_spec,
    project_point,
    true_color_to_aci,
)


def data_entities(doc):
    return [entity for entity in doc.modelspace() if entity.dxf.layer in DXF_LAYERS]


def xy_points(entity):
    if entity.dxftype() == "LWPOLYLINE":
        return [(point[0], point[1]) for point in entity.get_points("xy")]
    if entity.dxftype() == "POLYLINE":
        return [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in entity.vertices]
    return []

def is_kml_fdt_insert(entity):
    return entity.dxftype() == "INSERT" and str(entity.dxf.name).startswith(KML_FDT_BLOCK_NAME)

def fdt_block_true_color(doc, insert):
    block = doc.blocks.get(insert.dxf.name)
    line = next(entity for entity in block if entity.dxftype() == "LINE")
    return line.dxf.true_color

def fdt_block_color(doc, insert):
    block = doc.blocks.get(insert.dxf.name)
    line = next(entity for entity in block if entity.dxftype() == "LINE")
    return line.dxf.color

def text_collision_box(entity):
    height = float(entity.dxf.height)
    text = str(entity.dxf.text)
    width = max(len(text), 1) * height * 0.72 * 1.15
    box_height = height * 1.8
    half_width = width / 2
    half_height = box_height / 2
    angle = math.radians(float(entity.dxf.rotation or 0))
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    cx = float(entity.dxf.insert.x)
    cy = float(entity.dxf.insert.y)
    points = []
    for dx, dy in [(-half_width, -half_height), (half_width, -half_height), (half_width, half_height), (-half_width, half_height)]:
        points.append((cx + dx * cos_angle - dy * sin_angle, cy + dx * sin_angle + dy * cos_angle))
    return ShapelyPolygon(points)


def assert_layout_viewport_frames_output(doc, entities):
    points = [point for entity in entities for point in xy_points(entity)]
    assert points
    viewports = [
        entity
        for entity in doc.layouts.get(TEMPLATE_LAYOUT_NAME)
        if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("status", 0) or 0) > 1
    ]
    assert viewports
    viewport = viewports[-1]
    assert float(viewport.dxf.width) > 0
    assert float(viewport.dxf.height) > 0

def layout_callout_texts(layout):
    texts = []
    for entity in layout:
        if entity.dxftype() == "TEXT" and entity.dxf.layer == FAT_CALLOUT_LAYER:
            texts.append(entity.dxf.text)
        if entity.dxftype() == "INSERT" and entity.dxf.layer == FAT_CALLOUT_LAYER:
            texts.extend(attrib.dxf.text for attrib in entity.attribs)
    return texts


def entity_paper_rect(entity):
    extent = bbox.extents([entity])
    assert extent.has_data
    return (
        float(extent.extmin.x),
        float(extent.extmin.y),
        float(extent.extmax.x),
        float(extent.extmax.y),
    )


def active_viewport_rect(layout):
    viewports = [
        entity
        for entity in layout
        if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("status", 0) or 0) > 1
    ]
    assert viewports
    viewport = viewports[-1]
    center = viewport.dxf.center
    width = float(viewport.dxf.width)
    height = float(viewport.dxf.height)
    return (
        float(center.x) - width / 2,
        float(center.y) - height / 2,
        float(center.x) + width / 2,
        float(center.y) + height / 2,
    )


def rects_overlap(first, second, padding=0.0):
    min_x, min_y, max_x, max_y = first
    other_min_x, other_min_y, other_max_x, other_max_y = second
    return (
        min_x < other_max_x + padding
        and max_x > other_min_x - padding
        and min_y < other_max_y + padding
        and max_y > other_min_y - padding
    )


def assert_layout_callouts_avoid_design_summary(layout):
    for entity in layout:
        if entity.dxf.layer != FAT_CALLOUT_LAYER or entity.dxftype() not in {"INSERT", "LINE", "LWPOLYLINE"}:
            continue
        assert not rects_overlap(entity_paper_rect(entity), FDT_DESIGN_SUMMARY_CLEAR_BOUNDS)


def standard_kml(placemarks_by_folder: dict[str, str]) -> str:
    folders = []
    for folder in STANDARD_KML_FOLDERS:
        placemark = placemarks_by_folder.get(folder, "")
        folders.append(f"<Folder><name>{folder}</name>{placemark}</Folder>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        + "".join(folders)
        + "</Document></kml>"
    )


def standard_kml_with_boundary_cluster(boundary_placemark: str) -> str:
    return standard_kml({}).replace(
        "</Document></kml>",
        f"<Folder><name>BOUNDARY CLUSTER</name>{boundary_placemark}</Folder></Document></kml>",
    )


def auto_boundary_source_kml() -> str:
    return standard_kml(
        {
            "NEW POLE 7-3": """
<Placemark>
  <name>Pole A</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
""",
            "DISTRIBUTION CABLE": """
<Placemark>
  <name>Cable A</name>
  <LineString>
    <coordinates>106.8000,-6.2000,0 106.8005,-6.2000,0</coordinates>
  </LineString>
</Placemark>
""",
            "BOUNDARY FAT": """
<Placemark>
  <name>FAT Area</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.8007,-6.1999,0 106.8010,-6.1999,0 106.8010,-6.2002,0 106.8007,-6.2002,0 106.8007,-6.1999,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
""",
        }
    )


def auto_boundary_kml_with_boundary_cluster() -> str:
    boundary_placemark = """
<Placemark>
  <name>Feeder Clip</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.7998,-6.1998,0 106.8008,-6.1998,0 106.8008,-6.2002,0 106.7998,-6.2002,0 106.7998,-6.1998,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
"""
    return auto_boundary_source_kml().replace(
        "</Document></kml>",
        f"<Folder><name>BOUNDARY CLUSTER</name>{boundary_placemark}</Folder></Document></kml>",
    )


def auto_boundary_roads_osm() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.7999"/>
  <node id="2" lat="-6.2000" lon="106.8006"/>
  <node id="3" lat="-6.2050" lon="106.7999"/>
  <node id="4" lat="-6.2050" lon="106.8006"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
"""


def fdt_layout_callout_kml(fdt_count: int = 1) -> str:
    fdt_points = []
    line_folders = []
    for index in range(1, fdt_count + 1):
        suffix = 14 + index
        line_letter = chr(ord("A") + index - 1)
        lon_offset = (index - 1) * 0.003
        fdt_points.append(
            f"""
        <Placemark>
          <name>TDMCR.{suffix:03d}</name>
          <Point><coordinates>{106.8000 + lon_offset},-6.2000,0</coordinates></Point>
        </Placemark>
"""
        )
        line_folders.append(
            f"""
      <Folder>
        <name>LINE {line_letter} - FDT {index}</name>
        <Folder>
          <name>DISTRIBUTION CABLE</name>
          <Placemark>
            <name>TDMCR.{suffix:03d} - CABLE LINE {line_letter} (FO 24C/2T) - UG - 120m</name>
            <LineString><coordinates>{106.8000 + lon_offset},-6.2000,0 {106.8010 + lon_offset},-6.2000,0</coordinates></LineString>
          </Placemark>
        </Folder>
        <Folder>
          <name>FAT</name>
          <Placemark>
            <name>TDMCR.{suffix:03d}.{line_letter}01</name>
            <Point><coordinates>{106.8007 + lon_offset},-6.2000,0</coordinates></Point>
          </Placemark>
        </Folder>
      </Folder>
"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        '<Folder><name>FDT</name>'
        + "".join(fdt_points)
        + "</Folder>"
        + "".join(line_folders)
        + "</Document></kml>"
    )


class CapturePipelineLogger:
    def __init__(self):
        self.entries = []
        self.progress_updates = []

    def info(self, message: str) -> None:
        self.entries.append(("INFO", message))

    def success(self, message: str) -> None:
        self.entries.append(("SUCCESS", message))

    def warn(self, message: str) -> None:
        self.entries.append(("WARNING", message))

    def error(self, message: str) -> None:
        self.entries.append(("ERROR", message))

    def progress(self, progress: int, message: str) -> None:
        self.progress_updates.append((progress, message))


def test_classify_tags_priority():
    assert classify_tags({"highway": "residential"}) == "ROAD"
    assert classify_tags({"building": "yes"}) == "BUILDING"
    assert classify_tags({"waterway": "river"}) == "WATERWAY"
    assert classify_tags({"railway": "rail"}) == "RAILWAY"
    assert classify_tags({"natural": "water"}) == "WATER_AREA"
    assert classify_tags({"landuse": "reservoir"}) == "WATER_AREA"
    assert classify_tags({"landuse": "forest"}) == "LANDUSE"
    assert classify_tags({"leisure": "park"}) == "LEISURE"
    assert classify_tags({"name": "Unknown"}) == "OTHER"
    assert DXF_LAYERS["ROAD"] == 8


def test_project_point_uses_web_mercator():
    x, y = project_point(0, 0)
    assert x == pytest.approx(0)
    assert y == pytest.approx(0)


@pytest.mark.slow
def test_convert_minimal_osm_to_dxf(tmp_path):
    input_path = tmp_path / "sample.osm"
    output_path = tmp_path / "sample.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8010"/>
  <node id="3" lat="-6.2010" lon="106.8010"/>
  <node id="4" lat="-6.2010" lon="106.8000"/>
  <node id="5" lat="-6.2020" lon="106.8000"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
  </way>
  <way id="11">
    <nd ref="4"/><nd ref="5"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path)
    doc = ezdxf.readfile(output_path)

    assert output_path.exists()
    assert doc.dxfversion == "AC1015"
    assert not any(entity.dxf.layer == "TITLE_BLOCK" for entity in doc.modelspace())
    assert any(entity.dxf.layer == "TITLE_BLOCK" for entity in doc.layouts.get(TEMPLATE_LAYOUT_NAME))
    assert sum(entity.dxftype() == "VIEWPORT" for entity in doc.layouts.get(TEMPLATE_LAYOUT_NAME)) == 2
    assert result["stats"]["BUILDING"] == 1
    assert result["stats"]["ROAD"] == 1
    assert result["processed_ways"] == 2
    assert len(result["preview"]["features"]) == 2


@pytest.mark.slow
def test_single_fdt_fat_callouts_are_removed_from_output(tmp_path):
    input_path = tmp_path / "single-fdt.kml"
    output_path = tmp_path / "single-fdt.dxf"
    input_path.write_text(fdt_layout_callout_kml(1), encoding="utf-8")

    convert_osm_to_dxf(
        input_path,
        output_path,
        selected_layers=["KML"],
        selected_kml_folders=["FDT", "FAT", "DISTRIBUTION CABLE"],
        road_clip_boundary=False,
        network_type="UG",
    )
    doc = ezdxf.readfile(output_path)
    cluster_layout = doc.layouts.get(TEMPLATE_LAYOUT_NAME)
    callout_texts = layout_callout_texts(cluster_layout)
    callout_lines = [
        entity
        for entity in cluster_layout
        if entity.dxftype() == "LINE" and entity.dxf.layer == FAT_CALLOUT_LAYER
    ]
    callout_blocks = [
        entity
        for entity in cluster_layout
        if entity.dxftype() == "INSERT" and entity.dxf.layer == FAT_CALLOUT_LAYER and entity.dxf.name == FAT_CALLOUT_BLOCK_NAME
    ]

    # FAT_Info layer dan callouts harus tidak ada di output
    assert callout_texts == []
    assert not callout_lines
    assert not callout_blocks
    assert FAT_CALLOUT_LAYER not in [layer.dxf.name for layer in doc.layers]

@pytest.mark.slow
def test_multi_fdt_fat_callouts_are_removed_from_output(tmp_path):
    input_path = tmp_path / "multi-fdt.kml"
    output_path = tmp_path / "multi-fdt.dxf"
    input_path.write_text(fdt_layout_callout_kml(2), encoding="utf-8")

    convert_osm_to_dxf(
        input_path,
        output_path,
        selected_layers=["KML"],
        selected_kml_folders=["FDT", "FAT", "DISTRIBUTION CABLE"],
        road_clip_boundary=False,
        network_type="UG",
    )
    doc = ezdxf.readfile(output_path)
    cluster_layout = doc.layouts.get(TEMPLATE_LAYOUT_NAME)
    cluster_callout_texts = layout_callout_texts(cluster_layout)

    # FAT_Info callouts harus tidak ada di semua layout
    assert cluster_callout_texts == []
    assert FAT_CALLOUT_LAYER not in [layer.dxf.name for layer in doc.layers]

@pytest.mark.slow
def test_convert_only_selected_layers(tmp_path):
    input_path = tmp_path / "sample.osm"
    output_path = tmp_path / "roads-only.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8010"/>
  <node id="3" lat="-6.2010" lon="106.8010"/>
  <node id="4" lat="-6.2010" lon="106.8000"/>
  <node id="5" lat="-6.2020" lon="106.8000"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
  </way>
  <way id="11">
    <nd ref="4"/><nd ref="5"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)
    entity_layers = {entity.dxf.layer for entity in entities}

    assert result["stats"]["ROAD"] == 1
    assert result["stats"]["BUILDING"] == 0
    assert entity_layers == {"ROAD"}
    assert len(entities) == 1
    assert [feature["properties"]["layer"] for feature in result["preview"]["features"]] == ["ROAD"]


@pytest.mark.slow
def test_road_offset_uses_osm_width_tag(tmp_path):
    input_path = tmp_path / "sample.osm"
    output_path = tmp_path / "width-road.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8010"/>
  <way id="11">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="width" v="10"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)
    points = xy_points(entities[0])
    y_values = [point[1] for point in points]

    assert result["stats"]["ROAD"] == 1
    assert len(entities) == 1
    assert max(y_values) - min(y_values) > 0
    assert_layout_viewport_frames_output(doc, entities)


def test_estimate_road_width_falls_back_to_lanes_and_highway_type():
    assert estimate_road_half_width_meters({"width": "12 m"}) == pytest.approx(6)
    assert estimate_road_half_width_meters({"lanes": "2"}) == pytest.approx(3.7)
    assert estimate_road_half_width_meters({"highway": "service", "service": "driveway"}) == pytest.approx(1.6)
    assert estimate_road_half_width_meters({"highway": "primary"}) == pytest.approx(6)


@pytest.mark.slow
def test_distribution_cable_adds_distance_labels_on_jarak_layer(tmp_path):
    kml_input = tmp_path / "distribution-cable-distance.kml"
    output_path = tmp_path / "distribution-cable-distance.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "DISTRIBUTION CABLE": """
<Folder>
  <name>LINE A - FDT 1</name>
  <Folder>
    <name>DISTRIBUTION CABLE</name>
    <Placemark>
      <name>PSGMB034.062 - CABLE LINE A - ( FO 24C/2T ) - AE - 22M</name>
      <LineString>
        <coordinates>106.8000,-6.2000,0 106.8002,-6.2000,0 106.8002,-6.2002,0</coordinates>
      </LineString>
    </Placemark>
  </Folder>
</Folder>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["DISTRIBUTION CABLE"])
    doc = ezdxf.readfile(output_path)
    distance_labels = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_DISTANCE_LABEL_LAYER
    ]

    assert result["stats"][KML_DISTANCE_LABEL_LAYER] == 2
    assert result["kml_details"]["distance_labels"] == 2
    assert KML_DISTANCE_LABEL_LAYER in doc.layers
    assert doc.layers.get(KML_DISTANCE_LABEL_LAYER).color == KML_DISTANCE_LABEL_COLOR
    assert [entity.dxf.text for entity in distance_labels] == ["22m", "22m"]
    assert all(entity.dxf.color == KML_DISTANCE_LABEL_COLOR for entity in distance_labels)
    assert all(float(entity.dxf.height) > 0 for entity in distance_labels)

@pytest.mark.slow
def test_distribution_cable_distance_labels_skip_short_and_near_duplicate_segments(tmp_path):
    kml_input = tmp_path / "distribution-cable-distance-filter.kml"
    output_path = tmp_path / "distribution-cable-distance-filter.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "DISTRIBUTION CABLE": """
<Folder>
  <name>LINE A - FDT 1</name>
  <Folder>
    <name>DISTRIBUTION CABLE</name>
    <Placemark>
      <name>CABLE FORWARD FO 24C/2T</name>
      <LineString>
        <coordinates>106.8000,-6.2000,0 106.8001,-6.2000,0</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>CABLE REVERSE FO 24C/2T</name>
      <LineString>
        <coordinates>106.8001,-6.2000,0 106.8000,-6.2000,0</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>CABLE TINY FO 24C/2T</name>
      <LineString>
        <coordinates>106.8010,-6.2000,0 106.80102,-6.2000,0</coordinates>
      </LineString>
    </Placemark>
  </Folder>
</Folder>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["DISTRIBUTION CABLE"])
    doc = ezdxf.readfile(output_path)
    distance_labels = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_DISTANCE_LABEL_LAYER
    ]

    assert result["stats"][KML_DISTANCE_LABEL_LAYER] == 1
    assert result["kml_details"]["distance_labels"] == 1
    assert [entity.dxf.text for entity in distance_labels] == ["11m"]

@pytest.mark.parametrize(
    ("folder_name", "label", "color", "bottom"),
    [
        ("NEW POLE 9-4", 'NEW POLE 9M 4"', 1, '4"'),
        ("NEW POLE 7-4", 'NEW POLE 7M 4"', 3, '4"'),
        ("NEW POLE 7-3", 'NEW POLE 7M 3"', 4, '3"'),
        ("NEW POLE 7-2.5", 'NEW POLE 7M 2.5"', 6, "2.5'"),
        ("EXISTING POLE EMR 7-3", "EXISTING POLE", 1, None),
    ],
)
def test_kml_pole_folder_specs_match_standard_legend(folder_name, label, color, bottom):
    spec = kml_pole_folder_spec(folder_name)

    assert spec is not None
    assert spec["label"] == label
    assert spec["color"] == color
    if bottom:
        assert spec["bottom"] == bottom


@pytest.mark.slow
def test_crossing_roads_are_merged_into_clean_boundary(tmp_path):
    input_path = tmp_path / "crossing.osm"
    output_path = tmp_path / "clean-road.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <node id="3" lat="-6.2010" lon="106.8010"/>
  <node id="4" lat="-6.1990" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="width" v="8"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="residential"/>
    <tag k="width" v="8"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)

    assert result["stats"]["ROAD"] == 2
    assert len(entities) == 1
    assert entities[0].is_closed


@pytest.mark.slow
def test_minor_road_noise_is_filtered_from_clean_road_output(tmp_path):
    input_path = tmp_path / "noise.osm"
    output_path = tmp_path / "clean-noise.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <node id="3" lat="-6.2002" lon="106.8010"/>
  <node id="4" lat="-6.2003" lon="106.8010"/>
  <node id="5" lat="-6.2005" lon="106.8010"/>
  <node id="6" lat="-6.2006" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="width" v="8"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="service"/>
    <tag k="service" v="driveway"/>
  </way>
  <way id="12">
    <nd ref="5"/><nd ref="6"/>
    <tag k="highway" v="footway"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)

    assert result["stats"]["ROAD"] == 1
    assert len(entities) == 1


@pytest.mark.slow
def test_convert_multiple_osm_files_into_one_dxf(tmp_path):
    first_input = tmp_path / "roads.osm"
    second_input = tmp_path / "buildings.osm"
    output_path = tmp_path / "merged.dxf"

    first_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    second_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="101" lat="-6.2010" lon="106.8000"/>
  <node id="102" lat="-6.2010" lon="106.8010"/>
  <node id="103" lat="-6.2020" lon="106.8010"/>
  <node id="104" lat="-6.2020" lon="106.8000"/>
  <way id="110">
    <nd ref="101"/><nd ref="102"/><nd ref="103"/><nd ref="104"/><nd ref="101"/>
    <tag k="building" v="yes"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf([first_input, second_input], output_path)
    doc = ezdxf.readfile(output_path)
    entity_layers = {entity.dxf.layer for entity in data_entities(doc)}

    assert output_path.exists()
    assert result["input_count"] == 2
    assert result["stats"]["ROAD"] == 1
    assert result["stats"]["BUILDING"] == 1
    assert entity_layers == {"ROAD", "BUILDING"}


@pytest.mark.slow
def test_convert_osm_and_kml_into_one_dxf(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "boundary.kml"
    output_path = tmp_path / "osm-kml-merged.dxf"

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml(
            {
                "BOUNDARY FAT": """
<Placemark>
  <name>Area KML</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.8000,-6.2010,0 106.8010,-6.2010,0 106.8010,-6.2020,0 106.8000,-6.2020,0 106.8000,-6.2010,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf([osm_input, kml_input], output_path)
    doc = ezdxf.readfile(output_path)
    entity_layers = {entity.dxf.layer for entity in data_entities(doc)}
    fat_area_entities = [
        entity
        for entity in doc.modelspace()
        if entity.dxf.layer == KML_FAT_AREA_LAYER and entity.dxftype() == "POLYLINE"
    ]

    assert result["input_count"] == 2
    assert result["stats"]["ROAD"] == 1
    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FAT_AREA_LAYER] == 1
    assert "ROAD" in entity_layers
    assert fat_area_entities
    assert not doc.layers.get(KML_FAT_AREA_LAYER).is_off()
    assert not doc.layers.get(KML_FAT_AREA_LAYER).is_frozen()
    assert doc.layers.get(KML_FAT_AREA_LAYER).color == KML_FAT_AREA_COLOR
    assert doc.layers.get(KML_FAT_AREA_LAYER).dxf.linetype == KML_FAT_AREA_LINETYPE
    assert all(entity.dxf.color == 256 for entity in fat_area_entities)
    assert all(entity.dxf.linetype.upper() == "BYLAYER" for entity in fat_area_entities)
    assert not any(
        entity.dxftype() == "TEXT" and entity.dxf.text == "Area KML"
        for entity in doc.modelspace()
    )


@pytest.mark.auto_boundary
def test_feeder_auto_boundary_buffer_used_when_no_boundary_cluster(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "feeder.kml"
    output_path = tmp_path / "feeder-auto-boundary.dxf"
    logger = CapturePipelineLogger()

    osm_input.write_text(auto_boundary_roads_osm(), encoding="utf-8")
    kml_input.write_text(auto_boundary_source_kml(), encoding="utf-8")

    result = convert_osm_to_dxf(
        [osm_input, kml_input],
        output_path,
        selected_layers=["ROAD"],
        road_offset_meters=0,
        dxf_template_path=tmp_path / "missing-template.dxf",
        scope_type="Feeder",
        pipeline_logger=logger,
    )
    doc = ezdxf.readfile(output_path)
    road_entities = [entity for entity in data_entities(doc) if entity.dxf.layer == "ROAD"]
    messages = "\n".join(message for _severity, message in logger.entries)

    assert result["stats"]["ROAD"] == 2
    assert len(road_entities) == 1
    assert "Feeder scope auto-buffer: 50m around 3 KML geometries." in messages


@pytest.mark.auto_boundary
def test_cluster_scope_does_not_use_auto_boundary_buffer(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "cluster.kml"
    output_path = tmp_path / "cluster-no-auto-boundary.dxf"
    logger = CapturePipelineLogger()

    osm_input.write_text(auto_boundary_roads_osm(), encoding="utf-8")
    kml_input.write_text(auto_boundary_source_kml(), encoding="utf-8")

    result = convert_osm_to_dxf(
        [osm_input, kml_input],
        output_path,
        selected_layers=["ROAD"],
        road_offset_meters=0,
        dxf_template_path=tmp_path / "missing-template.dxf",
        scope_type="Cluster",
        pipeline_logger=logger,
    )
    doc = ezdxf.readfile(output_path)
    road_entities = [entity for entity in data_entities(doc) if entity.dxf.layer == "ROAD"]
    messages = "\n".join(message for _severity, message in logger.entries)

    assert result["stats"]["ROAD"] == 2
    assert len(road_entities) == 2
    assert "auto-buffer" not in messages.lower()


@pytest.mark.auto_boundary
def test_feeder_with_explicit_boundary_cluster_does_not_use_auto_buffer(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "feeder-boundary.kml"
    output_path = tmp_path / "feeder-explicit-boundary.dxf"
    logger = CapturePipelineLogger()

    osm_input.write_text(auto_boundary_roads_osm(), encoding="utf-8")
    kml_input.write_text(auto_boundary_kml_with_boundary_cluster(), encoding="utf-8")

    result = convert_osm_to_dxf(
        [osm_input, kml_input],
        output_path,
        selected_layers=["ROAD"],
        road_offset_meters=0,
        dxf_template_path=tmp_path / "missing-template.dxf",
        scope_type="Feeder",
        pipeline_logger=logger,
    )
    doc = ezdxf.readfile(output_path)
    road_entities = [entity for entity in data_entities(doc) if entity.dxf.layer == "ROAD"]
    messages = "\n".join(message for _severity, message in logger.entries)

    assert result["stats"]["ROAD"] == 2
    assert len(road_entities) == 1
    assert "Loaded 1 BOUNDARY CLUSTER clipping geometry source(s)." in messages
    assert "auto-buffer" not in messages.lower()


@pytest.mark.slow
def test_roads_outside_boundary_cluster_are_not_exported(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "cluster.kml"
    output_path = tmp_path / "cluster-filtered.dxf"

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <node id="3" lat="-6.2100" lon="106.8000"/>
  <node id="4" lat="-6.2100" lon="106.8020"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml_with_boundary_cluster(
            """
<Placemark>
  <name>Cluster A</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.7995,-6.1995,0 106.8025,-6.1995,0 106.8025,-6.2005,0 106.7995,-6.2005,0 106.7995,-6.1995,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
"""
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf([osm_input, kml_input], output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    road_entities = [entity for entity in data_entities(doc) if entity.dxf.layer == "ROAD"]

    assert result["stats"]["ROAD"] == 2
    assert len(road_entities) == 1


@pytest.mark.slow
def test_road_boundary_cluster_clip_can_be_disabled(tmp_path):
    osm_input = tmp_path / "roads.osm"
    kml_input = tmp_path / "cluster.kml"
    output_path = tmp_path / "cluster-not-filtered.dxf"

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <node id="3" lat="-6.2100" lon="106.8000"/>
  <node id="4" lat="-6.2100" lon="106.8020"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml_with_boundary_cluster(
            """
<Placemark>
  <name>Cluster A</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.7995,-6.1995,0 106.8025,-6.1995,0 106.8025,-6.2005,0 106.7995,-6.2005,0 106.7995,-6.1995,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
"""
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(
        [osm_input, kml_input],
        output_path,
        selected_layers=["ROAD"],
        road_clip_boundary=False,
    )
    doc = ezdxf.readfile(output_path)
    road_entities = [entity for entity in data_entities(doc) if entity.dxf.layer == "ROAD"]

    assert result["stats"]["ROAD"] == 2
    assert len(road_entities) == 2

@pytest.mark.slow
def test_named_osm_road_gets_road_name_label(tmp_path):
    input_path = tmp_path / "named-road.osm"
    output_path = tmp_path / "named-road.dxf"
    input_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2010" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Jl. Industri Sempu Garden"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(input_path, output_path, selected_layers=["ROAD"])
    doc = ezdxf.readfile(output_path)
    road_labels = [entity for entity in doc.modelspace() if entity.dxftype() == "TEXT" and entity.dxf.layer == "ROAD"]

    assert result["stats"]["ROAD"] == 1
    assert any(entity.dxf.text == "JL. INDUSTRI SEMPU GARDEN" for entity in road_labels)
    assert any(abs(entity.dxf.rotation) > 1 for entity in road_labels)

@pytest.mark.slow
def test_pipeline_logger_splits_cad_generation_stages(tmp_path):
    osm_input = tmp_path / "named-road.osm"
    kml_input = tmp_path / "mixed-kml.kml"
    output_path = tmp_path / "mixed-kml.dxf"
    logger = CapturePipelineLogger()

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Jl. Test Logger"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml(
            {
                "FAT": """
<Placemark>
  <name>FAT-01</name>
  <Point><coordinates>106.8005,-6.2002,0</coordinates></Point>
</Placemark>
""",
                "HP COVER": """
<Placemark>
  <name>HP-01</name>
  <Point><coordinates>106.8007,-6.2002,0</coordinates></Point>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    convert_osm_to_dxf(
        [osm_input, kml_input],
        output_path,
        selected_kml_folders=["FAT", "HP COVER"],
        pipeline_logger=logger,
    )
    messages = [message for _severity, message in logger.entries]

    assert any("Parsing KML/KMZ and generating KML icons/CAD blocks" in message for message in messages)
    assert any("KML detail from mixed-kml.kml" in message and "HOME_NUMBER queued" in message for message in messages)
    assert any("[2/6] Generating road labels" in message for message in messages)
    assert any("[3/6] HOME_NUMBER sub-step: building ROAD avoidance geometry" in message for message in messages)
    assert any("[3/6] HOME_NUMBER crowding scan complete" in message for message in messages)
    assert all("Generating road labels, HOME_NUMBER boxes, KML icons, and CAD blocks" not in message for message in messages)

@pytest.mark.slow
def test_convert_kmz_into_kml_layer(tmp_path):
    kmz_input = tmp_path / "line.kmz"
    output_path = tmp_path / "line.dxf"
    kml_content = standard_kml(
        {
            "DISTRIBUTION CABLE": """
<Placemark>
  <name>Line KML</name>
  <LineString>
    <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
  </LineString>
</Placemark>
"""
        }
    )
    with zipfile.ZipFile(kmz_input, "w") as archive:
        archive.writestr("doc.kml", kml_content)

    result = convert_osm_to_dxf(kmz_input, output_path)
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert {entity.dxftype() for entity in entities} == {"POLYLINE", "TEXT"}
    assert {entity.dxf.layer for entity in entities} == {"KML"}


@pytest.mark.slow
def test_kmz_doc_kml_in_folder_and_common_xml_repair(tmp_path):
    kmz_input = tmp_path / "nested-doc.kmz"
    output_path = tmp_path / "nested-doc.dxf"
    kml_content = standard_kml(
        {
            "FAT": """
<Placemark>
  <name>PSGMB034.B03 & Area</name>
  <Point>
    <coordinates>106.8000,-6.2000,0</coordinates>
  </Point>
</Placemark>
"""
        }
    )
    with zipfile.ZipFile(kmz_input, "w") as archive:
        archive.writestr("project/doc.kml", kml_content)

    result = convert_osm_to_dxf(kmz_input, output_path, selected_kml_folders=["FAT"])
    doc = ezdxf.readfile(output_path)
    fat_inserts = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    ]

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FAT_LAYER] == 1
    assert result["kml_features"] == 1
    assert len(fat_inserts) == 1
    assert fat_inserts[0].dxf.layer == KML_FAT_LAYER
    assert {(attrib.dxf.tag, attrib.dxf.text, attrib.dxf.layer) for attrib in fat_inserts[0].attribs} == {
        (KML_FAT_ATTR_TAG, "B03", KML_FAT_LAYER),
        (KML_FAT_COVERAGE_ATTR_TAG, "0", KML_FAT_LAYER),
    }


@pytest.mark.slow
def test_feeder_joint_closure_block_geometry(tmp_path):
    handler = OSMToDXFHandler(
        tmp_path / "feeder-jc.dxf",
        selected_layers=["KML"],
        dxf_template_path=Path("dxf_templates") / "Template_Feeder_AE.dxf",
        scope_type="Feeder",
    )

    handler._ensure_kml_joint_closure_block()

    assert KML_JOINT_CLOSURE_BLOCK_NAME in handler.doc.blocks
    block = handler.doc.blocks.get(KML_JOINT_CLOSURE_BLOCK_NAME)
    entities = list(block)
    assert [entity.dxftype() for entity in entities].count("CIRCLE") == 1
    assert [entity.dxftype() for entity in entities].count("POLYLINE") == 2
    hatches = [entity for entity in entities if entity.dxftype() == "HATCH"]
    assert len(hatches) == 2
    for entity in entities:
        assert entity.dxf.layer == KML_JOINT_CLOSURE_LAYER
        assert entity.dxf.color == KML_JOINT_CLOSURE_COLOR
    assert all(hatch.has_solid_fill for hatch in hatches)


@pytest.mark.slow
def test_feeder_snap_to_nearest_pole_respects_radius(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "snap.dxf", selected_layers=["KML"], scope_type="Feeder")
    handler.pole_positions = [(100.0, 100.0)]

    assert handler._snap_to_nearest_pole((103.0, 102.0)) == (100.0, 100.0)
    assert handler._snap_to_nearest_pole((110.0, 110.0)) == (110.0, 110.0)

    handler.pole_positions = []
    assert handler._snap_to_nearest_pole((103.0, 102.0)) == (103.0, 102.0)


@pytest.mark.slow
def test_layout_name_for_scope_and_feeder_layout_rename(tmp_path):
    cluster_handler = OSMToDXFHandler(tmp_path / "cluster.dxf", selected_layers=["KML"], scope_type="Cluster")
    feeder_handler = OSMToDXFHandler(tmp_path / "feeder.dxf", selected_layers=["KML"], scope_type="Feeder")

    assert cluster_handler._layout_name_for_scope() == "Cluster"
    assert feeder_handler._layout_name_for_scope() == "Feeder"

    doc = ezdxf.new("R2000")
    doc.layouts.rename("Layout1", TEMPLATE_LAYOUT_NAME)
    template_path = tmp_path / "cluster-layout-template.dxf"
    doc.saveas(template_path)

    handler = OSMToDXFHandler(
        tmp_path / "renamed.dxf",
        selected_layers=["KML"],
        dxf_template_path=template_path,
        scope_type="Feeder",
    )
    layout = handler.ensure_template_layout()
    assert layout.name == "Feeder"
    auditor = handler.doc.audit()
    assert not auditor.errors
    saved_path = tmp_path / "renamed-output.dxf"
    handler.doc.saveas(saved_path)

    saved_doc = ezdxf.readfile(saved_path)
    assert saved_doc.layout_names() == ["Model", "Feeder"]


@pytest.mark.slow
def test_feeder_template_paper_viewport_bounds_are_measured():
    expected = {
        "Template_Feeder_AE.dxf": (
            TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_CENTER,
            TEMPLATE_FEEDER_AE_PAPER_VIEWPORT_SIZE,
        ),
        "Template_Feeder_UG.dxf": (
            TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_CENTER,
            TEMPLATE_FEEDER_UG_PAPER_VIEWPORT_SIZE,
        ),
    }
    for filename, (expected_center, expected_size) in expected.items():
        path = Path("dxf_templates") / filename
        doc = ezdxf.readfile(path)
        layout = doc.layouts.get("Cluster")
        paper_viewports = [
            entity
            for entity in layout
            if entity.dxftype() == "VIEWPORT" and int(entity.dxf.get("status", 0) or 0) <= 1
        ]
        assert paper_viewports
        viewport = paper_viewports[0]
        center = (float(viewport.dxf.center.x), float(viewport.dxf.center.y))
        size = (float(viewport.dxf.width), float(viewport.dxf.height))
        print(f"{filename}: paper viewport center={center}, size={size}")
        assert center == pytest.approx(expected_center)
        assert size == pytest.approx(expected_size)


@pytest.mark.slow
def test_feeder_ug_summary_warns_for_missing_boring_and_uses_trenching_fallback(tmp_path, caplog):
    handler = OSMToDXFHandler(
        tmp_path / "feeder-ug-summary.dxf",
        selected_layers=["KML"],
        network_type="UG",
        scope_type="Feeder",
    )
    handler.dxf_summary_lengths["DISTRIBUTION_CABLE"] = 42.1

    with caplog.at_level(logging.WARNING, logger="converter"):
        values = handler.dxf_design_summary_values()

    assert values["TOTAL TRENCHING"] == 43
    assert values["TOTAL BORING"] == 0
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "TOTAL TRENCHING has no BOQ NRO/design override source" in messages
    assert "TOTAL BORING has no BOQ NRO/design override" in messages


@pytest.mark.slow
def test_feeder_summary_overrides_and_ae_boring_zero_without_warning(tmp_path, caplog):
    ug_handler = OSMToDXFHandler(
        tmp_path / "feeder-ug-overrides.dxf",
        selected_layers=["KML"],
        network_type="UG",
        scope_type="Feeder",
        design_summary_overrides={
            "TOTAL TRENCHING": "30,2",
            "TOTAL BORING": 12.1,
            "(FO CABLE AERIAL 24/2T)": 147,
        },
    )
    ug_handler.dxf_summary_lengths["FO_24/2T"] = 100

    with caplog.at_level(logging.WARNING, logger="converter"):
        values = ug_handler.dxf_design_summary_values()

    assert values["TOTAL TRENCHING"] == 31
    assert values["TOTAL BORING"] == 13
    assert values["(FO CABLE AERIAL 24/2T)"] == 147
    assert not caplog.records

    caplog.clear()
    ae_handler = OSMToDXFHandler(
        tmp_path / "feeder-ae-summary.dxf",
        selected_layers=["KML"],
        network_type="AE",
        scope_type="Feeder",
    )
    with caplog.at_level(logging.WARNING, logger="converter"):
        values = ae_handler.dxf_design_summary_values()

    assert values["TOTAL TRENCHING"] == 0
    assert values["TOTAL BORING"] == 0
    assert not caplog.records


@pytest.mark.slow
def test_convert_uses_original_filename_for_cluster_name(tmp_path, monkeypatch):
    osm_input = tmp_path / "map.osm"
    standardized_input = tmp_path / "9fcc48ae32094cc98051eadb4943ce02_STANDARDIZED_1.kmz"
    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest"></osm>
""",
        encoding="utf-8",
    )
    with zipfile.ZipFile(standardized_input, "w") as archive:
        archive.writestr(
            "doc.kml",
            standard_kml({
                "FAT": "<Placemark><name>A01</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>"
            }),
        )
    output_path = tmp_path / "original-name.dxf"
    captured = {}
    original_set_cluster_name = OSMToDXFHandler.set_cluster_name

    def capture_cluster_name(self, cluster_name):
        original_set_cluster_name(self, cluster_name)
        captured["cluster_name"] = self.cluster_name

    monkeypatch.setattr(OSMToDXFHandler, "set_cluster_name", capture_cluster_name)

    convert_osm_to_dxf(
        [osm_input, standardized_input],
        output_path,
        road_clip_boundary=False,
        original_filenames=["MAP.osm", "BKS004877 - SF WARINGINJAYA RW 06 BEKASI.kmz"],
    )

    assert captured["cluster_name"] == "SF WARINGINJAYA RW 06 BEKASI"
    doc = ezdxf.readfile(output_path)
    layout_texts = [
        str(entity.dxf.text)
        for entity in doc.layouts.get(TEMPLATE_LAYOUT_NAME)
        if entity.dxftype() == "TEXT"
    ]
    assert "SF WARINGINJAYA RW 06 BEKASI" in layout_texts
    assert "SF_WARINGINJAYA_RW_06_BEKASI" not in layout_texts

@pytest.mark.slow
def test_fat_child_folder_code_uses_editable_fat_block(tmp_path):
    kml_input = tmp_path / "fat-child-folder.kml"
    output_path = tmp_path / "fat-child-folder.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FAT": """
<Folder>
  <name>FAT A04</name>
  <Placemark>
    <name>Node without code</name>
    <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
  </Placemark>
</Folder>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FAT"])
    doc = ezdxf.readfile(output_path)
    fat_insert = next(
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    )

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FAT_LAYER] == 1
    assert fat_insert.dxf.layer == KML_FAT_LAYER
    assert [attrib.dxf.text for attrib in fat_insert.attribs if attrib.dxf.tag == KML_FAT_ATTR_TAG] == ["A04"]


@pytest.mark.slow
def test_fat_icon_uses_compact_block_size(tmp_path):
    kml_input = tmp_path / "fat-compact.kml"
    output_path = tmp_path / "fat-compact.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FAT": """
<Placemark>
  <name>A13</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FAT"])
    doc = ezdxf.readfile(output_path)
    block = doc.blocks.get(KML_FAT_BLOCK_NAME)
    line_points = [
        (float(entity.dxf.start.x), float(entity.dxf.start.y))
        for entity in block
        if entity.dxftype() == "LINE"
    ] + [
        (float(entity.dxf.end.x), float(entity.dxf.end.y))
        for entity in block
        if entity.dxftype() == "LINE"
    ]
    attdef = next(entity for entity in block if entity.dxftype() == "ATTDEF" and entity.dxf.tag == KML_FAT_ATTR_TAG)
    min_x = min(x for x, _y in line_points)
    max_x = max(x for x, _y in line_points)
    min_y = min(y for _x, y in line_points)
    max_y = max(y for _x, y in line_points)

    assert max_x - min_x == pytest.approx(KML_FAT_ICON_WIDTH_METERS)
    assert max_y - min_y == pytest.approx(KML_FAT_ICON_HEIGHT_METERS)
    assert attdef.dxf.height == pytest.approx(KML_FAT_CODE_HEIGHT_METERS)
    assert KML_FAT_ICON_WIDTH_METERS < 5.0
    assert KML_FAT_ICON_HEIGHT_METERS < 3.5


@pytest.mark.slow
def test_fat_icon_is_placed_above_pole_symbol(tmp_path):
    kml_input = tmp_path / "fat-and-pole.kml"
    output_path = tmp_path / "fat-and-pole.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FAT": """
<Placemark>
  <name>B09</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
""",
                "NEW POLE 7-3": """
<Placemark>
  <name>Pole 01</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FAT", "NEW POLE 7-3"])
    doc = ezdxf.readfile(output_path)
    fat_insert = next(
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    )
    pole_insert = next(
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name.startswith(KML_POLE_BLOCK_PREFIX)
    )

    assert result["stats"]["KML"] == 2
    assert fat_insert.dxf.insert.y > pole_insert.dxf.insert.y

@pytest.mark.slow
def test_fdt_folder_is_exported_as_kml_layer(tmp_path):
    kml_input = tmp_path / "fdt.kml"
    output_path = tmp_path / "fdt.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FDT": """
<Placemark>
  <name>FDT-01</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FDT"])
    doc = ezdxf.readfile(output_path)
    fdt_insert = next(
        entity
        for entity in doc.modelspace()
        if is_kml_fdt_insert(entity)
    )
    fdt_labels = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_FDT_LAYER and entity.dxf.text == "FDT-01"
    ]

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FDT_LAYER] == 1
    assert result["kml_features"] == 1
    assert fdt_insert.dxf.layer == KML_FDT_LAYER
    assert [(attrib.dxf.tag, attrib.dxf.text, attrib.dxf.layer) for attrib in fdt_insert.attribs] == [
        (KML_FDT_ATTR_TAG, "48", KML_FDT_LAYER)
    ]
    assert fdt_labels
    assert fdt_block_color(doc, fdt_insert) == true_color_to_aci(KML_FDT_CAPACITY_TRUE_COLORS[48])
    assert all(label.dxf.color == true_color_to_aci(KML_FDT_CAPACITY_TRUE_COLORS[48]) for label in fdt_labels)
    assert all(label.dxf.insert.y < fdt_insert.dxf.insert.y for label in fdt_labels)
    assert any(entity.dxftype() == "CIRCLE" for entity in doc.blocks.get(fdt_insert.dxf.name))


@pytest.mark.slow
def test_fdt_icon_color_uses_kmz_style_before_capacity_fallback(tmp_path):
    kml_input = tmp_path / "fdt-style-color.kml"
    output_path = tmp_path / "fdt-style-color.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Style id="eva_fdt_72c"><IconStyle><color>ff000055</color></IconStyle></Style>
  <Style id="custom_fdt"><IconStyle><color>ffff00aa</color></IconStyle></Style>
  <Folder><name>FDT</name>
    <Placemark><name>CKUR.072</name><styleUrl>#eva_fdt_72c</styleUrl>
      <description>FDT Capacity: 72 CORE</description>
      <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
    </Placemark>
    <Placemark><name>CKUR.024</name><styleUrl>#custom_fdt</styleUrl>
      <description>FDT Capacity: 24 CORE</description>
      <Point><coordinates>106.8010,-6.2000,0</coordinates></Point>
    </Placemark>
  </Folder>
</Document></kml>""",
        encoding="utf-8",
    )

    convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FDT"])
    doc = ezdxf.readfile(output_path)
    fdt_inserts = [
        entity for entity in doc.modelspace()
        if is_kml_fdt_insert(entity)
    ]
    fdt_labels = {
        entity.dxf.text: entity for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_FDT_LAYER
    }

    by_capacity = {
        next(attrib.dxf.text for attrib in insert.attribs if attrib.dxf.tag == KML_FDT_ATTR_TAG): insert
        for insert in fdt_inserts
    }
    assert fdt_block_color(doc, by_capacity["72"]) == true_color_to_aci(0x550000)
    assert fdt_block_color(doc, by_capacity["24"]) == true_color_to_aci(0xAA00FF)
    assert fdt_labels["CKUR.072"].dxf.color == true_color_to_aci(0x550000)
    assert fdt_labels["CKUR.024"].dxf.color == true_color_to_aci(0xAA00FF)


@pytest.mark.slow
def test_fdt_and_fat_icons_show_capacity_and_hp_coverage(tmp_path):
    kml_input = tmp_path / "fdt-fat-capacity.kml"
    output_path = tmp_path / "fdt-fat-capacity.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Folder><name>FDT</name><Placemark><name>CKUR.044</name>
    <description>FDT Capacity: 72 CORE</description>
    <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
  </Placemark></Folder>
  <Folder><name>LINE A - FDT 1</name>
    <Folder><name>FAT</name><Placemark><name>MR.JYS.CKUR.044.A10</name>
      <Point><coordinates>106.8002,-6.2000,0</coordinates></Point>
    </Placemark></Folder>
    <Folder><name>HP COVER</name><Folder><name>A10</name>
      <Placemark><name>HP-01</name><Point><coordinates>106.80021,-6.2000,0</coordinates></Point></Placemark>
      <Placemark><name>HP-02</name><Point><coordinates>106.80022,-6.2000,0</coordinates></Point></Placemark>
      <Placemark><name>HP-03</name><Point><coordinates>106.80023,-6.2000,0</coordinates></Point></Placemark>
    </Folder></Folder>
  </Folder>
</Document></kml>""",
        encoding="utf-8",
    )

    convert_osm_to_dxf(kml_input, output_path)
    doc = ezdxf.readfile(output_path)
    fdt_insert = next(
        entity for entity in doc.modelspace()
        if is_kml_fdt_insert(entity)
    )
    fat_insert = next(
        entity for entity in doc.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    )

    assert {attrib.dxf.tag: attrib.dxf.text for attrib in fdt_insert.attribs} == {
        KML_FDT_ATTR_TAG: "72"
    }
    assert {attrib.dxf.tag: attrib.dxf.text for attrib in fat_insert.attribs} == {
        KML_FAT_COVERAGE_ATTR_TAG: "3",
        KML_FAT_ATTR_TAG: "A10",
    }


def test_ug_fdt_icon_has_capacity_without_aerial_circle(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "ug-fdt.dxf", selected_layers=["KML"], network_type="UG")
    handler._queue_kml_fdt_icon((1000.0, 1000.0), "FDT.001", "FDT Capacity: 144 CORE")
    handler.finalize_fdt_icons()

    insert = next(
        entity for entity in handler.modelspace
        if is_kml_fdt_insert(entity)
    )
    block = handler.doc.blocks.get(insert.dxf.name)
    assert {attrib.dxf.tag: attrib.dxf.text for attrib in insert.attribs} == {
        KML_FDT_ATTR_TAG: "144"
    }
    assert not any(entity.dxftype() == "CIRCLE" for entity in block)

@pytest.mark.slow
def test_fdt_icon_is_placed_above_pole_symbol(tmp_path):
    kml_input = tmp_path / "fdt-and-pole.kml"
    output_path = tmp_path / "fdt-and-pole.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FDT": """
<Placemark>
  <name>PSGMB034.064</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
""",
                "NEW POLE 7-3": """
<Placemark>
  <name>NP7 3&quot;</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["FDT", "NEW POLE 7-3"])
    doc = ezdxf.readfile(output_path)
    fdt_insert = next(
        entity
        for entity in doc.modelspace()
        if is_kml_fdt_insert(entity)
    )
    pole_insert = next(
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name.startswith(KML_POLE_BLOCK_PREFIX)
    )
    fdt_label = next(
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_FDT_LAYER and entity.dxf.text == "PSGMB034.064"
    )

    assert result["stats"]["KML"] == 2
    assert result["stats"][KML_FDT_LAYER] == 1
    assert fdt_insert.dxf.insert.y > pole_insert.dxf.insert.y
    assert fdt_label.dxf.insert.y > fdt_insert.dxf.insert.y


@pytest.mark.slow
def test_hp_cover_uses_home_number_layer_and_avoids_road(tmp_path):
    osm_input = tmp_path / "road.osm"
    kml_input = tmp_path / "hp-cover.kml"
    output_path = tmp_path / "hp-cover.dxf"

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.2000" lon="106.8020"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml(
            {
                "HP COVER": """
<Placemark>
  <name>NN-17</name>
  <Point><coordinates>106.8010,-6.200035,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf([osm_input, kml_input], output_path)
    doc = ezdxf.readfile(output_path)
    home_texts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    ]
    home_boxes = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]
    road_polygons = [
        ShapelyPolygon(xy_points(entity))
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "ROAD"
    ]
    home_geometry = unary_union([ShapelyPolygon(xy_points(entity)) for entity in home_boxes])

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_HOME_NUMBER_LAYER] == 1
    assert [entity.dxf.text for entity in home_texts] == ["NN-17"]
    assert home_texts[0].dxf.height < KML_POLE_ID_HEIGHT_METERS
    assert home_boxes[0].dxf.color == 256
    assert doc.layers.get(KML_HOME_NUMBER_BOX_LAYER).color == DXF_LAYERS["ROAD"]
    assert not doc.layers.get(KML_HOME_NUMBER_BOX_LAYER).is_off()
    assert road_polygons
    assert all(home_geometry.intersection(road_polygon).area == pytest.approx(0) for road_polygon in road_polygons)

@pytest.mark.slow
def test_hp_cover_box_ignores_fat_area_size_and_stays_capped(tmp_path):
    kml_input = tmp_path / "hp-cover-fat-area.kml"
    output_path = tmp_path / "hp-cover-fat-area.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "BOUNDARY FAT": """
<Placemark>
  <name>Home Area</name>
  <Polygon>
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>
          106.8000,-6.2000,0 106.8010,-6.2000,0 106.8010,-6.2010,0 106.8000,-6.2010,0 106.8000,-6.2000,0
        </coordinates>
      </LinearRing>
    </outerBoundaryIs>
  </Polygon>
</Placemark>
""",
                "HP COVER": """
<Placemark>
  <name>NN-69</name>
  <Point><coordinates>106.8005,-6.2005,0</coordinates></Point>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["BOUNDARY FAT", "HP COVER"])
    doc = ezdxf.readfile(output_path)
    home_box = next(
        ShapelyPolygon(xy_points(entity))
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    )
    fat_area = next(
        ShapelyPolygon(xy_points(entity))
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_FAT_AREA_LAYER
    )

    assert result["stats"]["KML"] == 2
    assert result["stats"][KML_HOME_NUMBER_LAYER] == 1
    assert home_box.within(fat_area.buffer(1e-6))
    assert home_box.area < fat_area.area * 0.25

@pytest.mark.slow
def test_hp_cover_preserves_kmz_coordinate_when_inside_fat_area(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "anchor.dxf", selected_layers=["KML"])
    projected = project_point(106.8005, -6.2005, handler.transformer)
    fat_area = ShapelyPolygon(
        [
            (projected[0] - 30, projected[1] - 30),
            (projected[0] + 30, projected[1] - 30),
            (projected[0] + 30, projected[1] + 30),
            (projected[0] - 30, projected[1] + 30),
        ]
    )
    handler.fat_area_geometries.append(fat_area)
    handler._queue_home_number(projected, "HP-88")

    handler.finalize_home_numbers()
    home_text = next(
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    )
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]

    assert home_text.dxf.insert.x == pytest.approx(projected[0])
    assert home_text.dxf.insert.y == pytest.approx(projected[1])
    assert home_boxes
    assert all(fat_area.buffer(1e-6).covers(box) for box in home_boxes)


@pytest.mark.slow
def test_hp_cover_labels_follow_longest_parcel_axis_and_are_centered(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "manual-apd-home-layout.dxf", selected_layers=["KML"])
    road_line = LineString([(980.0, 1000.0), (1040.0, 1000.0)])
    road_area = road_line.buffer(3.0, cap_style=2, join_style=2)
    fat_area = ShapelyPolygon(
        [
            (985.0, 1004.0),
            (1035.0, 1004.0),
            (1035.0, 1022.0),
            (985.0, 1022.0),
        ]
    )
    handler.road_centerlines.append(road_line)
    handler.road_buffers.append(road_area)
    handler.fat_area_geometries.append(fat_area)
    expected_points = [(1000.0, 1010.0), (1006.0, 1010.0), (1012.0, 1010.0)]
    for index, point in enumerate(expected_points, start=1):
        handler._queue_home_number(point, f"NN-{index:03d}")

    handler.finalize_home_numbers()
    home_texts = [
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    ]
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]

    assert len(home_texts) == len(home_boxes) == 3
    assert all(float(text.dxf.height) == pytest.approx(1.0) for text in home_texts)
    for text, box in zip(home_texts, home_boxes):
        center = box.centroid
        assert text.dxf.insert.x == pytest.approx(center.x)
        assert text.dxf.insert.y == pytest.approx(center.y)
        expected_rotation = handler._polygon_label_rotation(box)
        assert float(text.dxf.rotation or 0.0) == pytest.approx(expected_rotation)
        rectangle = box.minimum_rotated_rectangle
        coords = list(rectangle.exterior.coords)
        edge_lengths = [
            math.hypot(end[0] - start[0], end[1] - start[1])
            for start, end in zip(coords, coords[1:])
        ]
        assert max(edge_lengths) / min(length for length in edge_lengths if length > 1e-6) >= 1.5
        assert box.intersection(road_area).area == pytest.approx(0)
    for first_index, first_box in enumerate(home_boxes):
        for second_box in home_boxes[first_index + 1:]:
            assert first_box.intersection(second_box).area == pytest.approx(0)


@pytest.mark.slow
def test_hp_cover_basic_map_is_not_clipped_by_tight_fat_area(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "tight-fat.dxf", selected_layers=["KML"])
    projected = (1000.0, 1000.0)
    fat_area = ShapelyPolygon(
        [
            (projected[0] - 2.0, projected[1] - 2.0),
            (projected[0] + 2.0, projected[1] - 2.0),
            (projected[0] + 2.0, projected[1] + 2.0),
            (projected[0] - 2.0, projected[1] + 2.0),
        ]
    )
    handler.fat_area_geometries.append(fat_area)
    handler._queue_home_number(projected, "HP-99")

    handler.finalize_home_numbers()
    home_text = next(
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    )
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]

    assert home_text.dxf.insert.x == pytest.approx(projected[0])
    assert home_text.dxf.insert.y == pytest.approx(projected[1])
    assert home_boxes
    assert any(not fat_area.buffer(1e-6).covers(box) for box in home_boxes)
    assert unary_union(home_boxes).area > fat_area.area

@pytest.mark.slow
def test_hp_cover_can_shift_from_road_as_fallback_inside_fat_area(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "road-shift.dxf", selected_layers=["KML"])
    projected = (1000.0, 1000.0)
    fat_area = ShapelyPolygon(
        [
            (projected[0] - 30.0, projected[1] - 30.0),
            (projected[0] + 30.0, projected[1] - 30.0),
            (projected[0] + 30.0, projected[1] + 30.0),
            (projected[0] - 30.0, projected[1] + 30.0),
        ]
    )
    road_line = LineString([(projected[0] - 30.0, projected[1]), (projected[0] + 30.0, projected[1])])
    road_area = road_line.buffer(3.0, cap_style=2, join_style=2)
    handler.fat_area_geometries.append(fat_area)
    handler.road_centerlines.append(road_line)
    handler.road_buffers.append(road_area)
    handler._queue_home_number(projected, "HP-100")

    handler.finalize_home_numbers()
    home_text = next(
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    )
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]
    home_geometry = unary_union(home_boxes)

    assert home_text.dxf.insert.y > projected[1]
    assert fat_area.buffer(1e-6).covers(home_geometry)
    assert home_geometry.intersection(road_area).area == pytest.approx(0)

@pytest.mark.slow
def test_hp_cover_home_number_text_shifts_out_of_road(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "text-road-clearance.dxf", selected_layers=["KML"])
    projected = (1000.0, 1001.8)
    road_line = LineString([(970.0, 1000.0), (1030.0, 1000.0)])
    road_area = road_line.buffer(2.0, cap_style=2, join_style=2)
    handler.road_centerlines.append(road_line)

    placement = handler._home_number_placement(
        projected,
        "A16/7",
        road_geometry=road_area,
        occupied_geometry=None,
        text_occupied_geometry=None,
        boundary_geometry=None,
        is_crowded=False,
    )
    width, height = handler._home_number_box_size("A16/7")

    assert placement["center"][1] > projected[1]
    assert placement["text_box"].intersection(road_area).area == pytest.approx(0)
    assert math.hypot(placement["center"][0] - projected[0], placement["center"][1] - projected[1]) <= max(width, height)

@pytest.mark.slow
def test_hp_cover_basic_map_shifts_away_from_existing_home_number_text(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "box-text-clearance.dxf", selected_layers=["KML"])
    projected = (1000.0, 1000.0)
    prior_text = handler._home_number_text_polygon(
        projected,
        "A16/5",
        90.0,
        KML_HOME_NUMBER_HEIGHT_METERS,
    )
    width, height = handler._home_number_box_size("A16/6")

    placement = handler._home_number_placement(
        projected,
        "A16/6",
        road_geometry=None,
        occupied_geometry=None,
        text_occupied_geometry=prior_text,
        boundary_geometry=None,
        is_crowded=False,
    )
    shifted_distance = math.hypot(
        placement["center"][0] - projected[0],
        placement["center"][1] - projected[1],
    )

    assert shifted_distance > 0
    assert shifted_distance <= max(width, height)
    assert placement["base_box"].intersection(
        prior_text.buffer(KML_HOME_NUMBER_TEXT_CLEARANCE_METERS)
    ).area == pytest.approx(0)

@pytest.mark.slow
def test_hp_cover_home_number_boxes_do_not_overlap(tmp_path):
    kml_input = tmp_path / "hp-cover-overlap.kml"
    output_path = tmp_path / "hp-cover-overlap.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "HP COVER": """
<Placemark>
  <name>B2/13</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
<Placemark>
  <name>B2/14</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["HP COVER"])
    doc = ezdxf.readfile(output_path)
    home_texts = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    ]
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]

    assert result["stats"]["KML"] == 2
    assert result["stats"][KML_HOME_NUMBER_LAYER] == 2
    assert len(home_texts) == 2
    assert text_collision_box(home_texts[0]).intersection(text_collision_box(home_texts[1])).area == pytest.approx(0)
    for first_index, first_box in enumerate(home_boxes):
        for second_box in home_boxes[first_index + 1:]:
            assert first_box.intersection(second_box).area == pytest.approx(0)

@pytest.mark.slow
def test_crowded_hp_cover_boxes_rotate_with_nearest_road(tmp_path):
    osm_input = tmp_path / "diagonal-road.osm"
    kml_input = tmp_path / "crowded-hp-cover.kml"
    output_path = tmp_path / "crowded-hp-cover.dxf"

    osm_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="pytest">
  <node id="1" lat="-6.2000" lon="106.8000"/>
  <node id="2" lat="-6.1990" lon="106.8010"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )
    kml_input.write_text(
        standard_kml(
            {
                "HP COVER": """
<Placemark>
  <name>B2/21</name>
  <Point><coordinates>106.80045,-6.19970,0</coordinates></Point>
</Placemark>
<Placemark>
  <name>B2/22</name>
  <Point><coordinates>106.80047,-6.19968,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf([osm_input, kml_input], output_path)
    doc = ezdxf.readfile(output_path)
    home_texts = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    ]
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]
    rotations = [float(entity.dxf.rotation or 0) for entity in home_texts]

    assert result["stats"][KML_HOME_NUMBER_LAYER] == 2
    assert len(home_texts) == 2
    assert all(10 < abs(rotation) < 80 for rotation in rotations)
    for first_index, first_box in enumerate(home_boxes):
        for second_box in home_boxes[first_index + 1:]:
            assert first_box.intersection(second_box).area == pytest.approx(0)

@pytest.mark.slow
def test_ug_hp_cover_inside_fat_area_uses_parcel_axis_and_center(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "ug-hp-cover-majority.dxf", selected_layers=["KML"], network_type="UG")
    road_line = LineString([(990.0, 1000.0), (1060.0, 1000.0)])
    road_geometry = road_line.buffer(0.4, cap_style=2, join_style=2)
    handler.road_centerlines.append(road_line)
    handler.road_buffers.append(road_geometry)
    handler.fat_area_geometries.append(
        ShapelyPolygon(
            [
                (990.0, 980.0),
                (1060.0, 980.0),
                (1060.0, 1020.0),
                (990.0, 1020.0),
            ]
        )
    )
    for text, point in [
        ("1", (1000.0, 1002.0)),
        ("2", (1015.0, 1002.0)),
        ("3", (1030.0, 1002.0)),
        ("4", (1045.0, 998.0)),
    ]:
        handler._queue_home_number(point, text)

    handler.finalize_home_numbers()
    home_texts = [
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    ]
    home_boxes = [
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    ]

    assert len(home_texts) == len(home_boxes) == 4
    for text, box in zip(home_texts, home_boxes):
        assert text.dxf.insert.x == pytest.approx(box.centroid.x)
        assert text.dxf.insert.y == pytest.approx(box.centroid.y)
        assert float(text.dxf.rotation or 0.0) == pytest.approx(handler._polygon_label_rotation(box))

@pytest.mark.slow
def test_ug_dxf_design_summary_uses_underground_totals(tmp_path):
    handler = OSMToDXFHandler(
        tmp_path / "ug-summary.dxf",
        selected_layers=["KML"],
        network_type="UG",
        design_summary_overrides={"TOTAL CABLE LOOP HOLDER/HANGER": 28},
    )
    handler.dxf_summary_counts["FAT"] = 7
    handler.dxf_summary_counts["FDT"] = 1
    handler.dxf_summary_counts["NEW HH 40X40X60"] = 3
    handler.dxf_summary_lengths["DISTRIBUTION_CABLE"] = 321.2

    values = handler.dxf_design_summary_values()

    assert values["TOTAL FAT 16 PORT (AERIAL)"] == 0
    assert values["TOTAL FAT 16 PORT (PEDESTAL)"] == 7
    assert values["TOTAL FDT (48C)"] == 0
    assert values["TOTAL FDT (144C)"] == 1
    assert values["TOTAL HANDHOLE 40X40X60"] == 3
    assert values["TOTAL AERIAL ROUTE"] == 0
    assert values["TOTAL DUCT ROUTE"] == 322
    assert values["TOTAL ROUTE LENGTH"] == 322
    assert values["TOTAL CABLE LOOP HOLDER/HANGER"] == 28

@pytest.mark.slow
def test_ug_dxf_design_summary_renames_fdt_label(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "ug-summary-label.dxf", selected_layers=["KML"], network_type="UG")
    text = handler.modelspace.add_text("TOTAL FDT (48C)", dxfattribs={"layer": "DESIGN_SUMMARY"})

    handler._rename_ug_design_summary_labels(handler.modelspace)

    assert text.dxf.text == "TOTAL FDT (144C)"

@pytest.mark.slow
def test_ug_hp_cover_uses_parcel_axis_instead_of_nearby_line(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "ug-hp-cover-nearest-line.dxf", selected_layers=["KML"], network_type="UG")
    projected = (1000.0, 1000.0)
    far_road = LineString([(970.0, 1060.0), (1030.0, 1060.0)])
    nearest_line = LineString([(970.0, 990.0), (1030.0, 990.0)])
    handler.road_centerlines.append(far_road)
    handler.kml_cable_centerlines.append(nearest_line)
    handler.kml_line_centerlines.append(nearest_line)
    handler.fat_area_geometries.append(
        ShapelyPolygon(
            [
                (970.0, 980.0),
                (1030.0, 980.0),
                (1030.0, 1020.0),
                (970.0, 1020.0),
            ]
        )
    )
    handler._queue_home_number(projected, "1")

    handler.finalize_home_numbers()
    home_text = next(
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    )
    home_box = next(
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    )

    assert home_text.dxf.insert.x == pytest.approx(home_box.centroid.x)
    assert home_text.dxf.insert.y == pytest.approx(home_box.centroid.y)
    assert float(home_text.dxf.rotation or 0.0) == pytest.approx(handler._polygon_label_rotation(home_box))

@pytest.mark.slow
def test_ug_hp_cover_centers_label_in_generated_parcel(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "ug-hp-cover-fixed-point.dxf", selected_layers=["KML"], network_type="UG")
    projected = (1000.0, 1008.0)
    road_line = LineString([(970.0, 1000.0), (1030.0, 1000.0)])
    road_area = road_line.buffer(3.0, cap_style=2, join_style=2)
    handler.road_centerlines.append(road_line)
    handler.road_buffers.append(road_area)
    handler.fat_area_geometries.append(
        ShapelyPolygon(
            [
                (970.0, 1004.0),
                (1030.0, 1004.0),
                (1030.0, 1020.0),
                (970.0, 1020.0),
            ]
        )
    )
    handler._queue_home_number(projected, "A16/7")

    handler.finalize_home_numbers()
    home_text = next(
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "TEXT" and entity.dxf.layer == KML_HOME_NUMBER_LAYER
    )
    home_box = next(
        ShapelyPolygon(xy_points(entity))
        for entity in handler.modelspace
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER
    )

    assert home_text.dxf.insert.x == pytest.approx(home_box.centroid.x)
    assert home_text.dxf.insert.y == pytest.approx(home_box.centroid.y)
    assert float(home_text.dxf.rotation or 0.0) == pytest.approx(handler._polygon_label_rotation(home_box))

@pytest.mark.slow
def test_slack_hanger_folder_uses_double_loop_block(tmp_path):
    kml_input = tmp_path / "slack-hanger.kml"
    output_path = tmp_path / "slack-hanger.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "SLACK HANGER": """
<Placemark>
  <name>Slack 01</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["SLACK HANGER"])
    doc = ezdxf.readfile(output_path)
    slack_insert = next(
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_SLACK_HANGER_BLOCK_NAME
    )
    slack_block = doc.blocks.get(KML_SLACK_HANGER_BLOCK_NAME)

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert slack_insert.dxf.layer == "KML"
    assert slack_insert.dxf.color == 1
    assert sum(1 for entity in slack_block if entity.dxftype() == "POLYLINE") == 2
    assert not any(entity.dxftype() == "TEXT" and entity.dxf.text == "Slack 01" for entity in doc.modelspace())

@pytest.mark.slow
def test_new_pole_folder_uses_matching_pole_symbol_and_pole_id_layer(tmp_path):
    kml_input = tmp_path / "new-pole.kml"
    output_path = tmp_path / "new-pole.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "NEW POLE 7-2.5": """
<Placemark>
  <name>Ignored Placemark Name</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["NEW POLE 7-2.5"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)
    pole_insert = next(
        entity
        for entity in entities
        if entity.dxftype() == "INSERT" and entity.dxf.name.startswith(KML_POLE_BLOCK_PREFIX)
    )
    pole_block = doc.blocks.get(pole_insert.dxf.name)
    block_texts = [entity.dxf.text for entity in pole_block if entity.dxftype() == "TEXT"]
    pole_id_text = next(
        entity
        for entity in entities
        if entity.dxftype() == "TEXT" and entity.dxf.text == "Ignored Placemark Name" and entity.dxf.layer == "POLE_ID"
    )

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert {entity.dxftype() for entity in entities} == {"INSERT", "TEXT"}
    assert pole_insert.dxf.layer == "KML"
    assert pole_insert.dxf.color == 6
    assert set(block_texts) == {"NP7", "2.5'"}
    assert any(entity.dxftype() == "CIRCLE" and entity.dxf.color == 6 for entity in pole_block)
    assert pole_id_text.dxf.color == 1
    assert KML_POLE_ID_HEIGHT_METERS == pytest.approx(0.92)
    assert pole_id_text.dxf.height < 1.25

@pytest.mark.slow
def test_pole_id_is_centered_below_icon_and_rotates_with_nearest_kml_line(tmp_path):
    kml_input = tmp_path / "new-pole-on-line.kml"
    output_path = tmp_path / "new-pole-on-line.dxf"
    line_start = (106.7995, -6.2005)
    line_end = (106.8005, -6.1995)
    pole_point = (106.8000, -6.2000)
    kml_input.write_text(
        standard_kml(
            {
                "NEW POLE 7-2.5": f"""
<Placemark>
  <name>NP.PSGMB034-P010</name>
  <Point><coordinates>{pole_point[0]},{pole_point[1]},0</coordinates></Point>
</Placemark>
""",
                "DISTRIBUTION CABLE": f"""
<Placemark>
  <name>FO 24C</name>
  <LineString><coordinates>{line_start[0]},{line_start[1]},0 {line_end[0]},{line_end[1]},0</coordinates></LineString>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=["NEW POLE 7-2.5", "DISTRIBUTION CABLE"],
    )
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)
    pole_insert = next(
        entity
        for entity in entities
        if entity.dxftype() == "INSERT" and entity.dxf.name.startswith(KML_POLE_BLOCK_PREFIX)
    )
    pole_id_text = next(
        entity
        for entity in entities
        if entity.dxftype() == "TEXT" and entity.dxf.text == "NP.PSGMB034-P010" and entity.dxf.layer == "POLE_ID"
    )
    start_x, start_y = project_point(*line_start)
    end_x, end_y = project_point(*line_end)
    expected_rotation = math.degrees(math.atan2(end_y - start_y, end_x - start_x))
    while expected_rotation > 90:
        expected_rotation -= 180
    while expected_rotation < -90:
        expected_rotation += 180

    assert pole_id_text.dxf.insert.x == pytest.approx(pole_insert.dxf.insert.x)
    assert pole_id_text.dxf.insert.y < pole_insert.dxf.insert.y - (KML_POLE_ICON_SIZE_METERS / 2)
    assert pole_id_text.dxf.rotation == pytest.approx(expected_rotation, abs=0.25)

@pytest.mark.parametrize(
    ("folder_stack", "name", "expected"),
    [
        (["FAT", "FAT A04"], "FAT node", "A04"),
        (["FAT", "PSGMB034.B03"], "FAT node", "B03"),
        (["FAT"], "PSGMB034.C07", "C07"),
        (["FAT"], "Manual FAT", "Manual FAT"),
    ],
)
def test_extract_fat_code_from_folder_path_or_name(folder_stack, name, expected):
    assert extract_fat_code_from_path(folder_stack, name) == expected

@pytest.mark.slow
def test_existing_pole_folders_use_crossed_circle_symbol(tmp_path):
    kml_input = tmp_path / "existing-pole.kml"
    output_path = tmp_path / "existing-pole.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "EXISTING POLE PARTNER 9-4": """
<Placemark>
  <name>Existing 01</name>
  <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
</Placemark>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["EXISTING POLE PARTNER 9-4"])
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)
    pole_insert = next(
        entity
        for entity in entities
        if entity.dxftype() == "INSERT" and entity.dxf.name.startswith(KML_POLE_BLOCK_PREFIX)
    )
    pole_block = doc.blocks.get(pole_insert.dxf.name)

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert {entity.dxftype() for entity in entities} == {"INSERT", "TEXT"}
    assert pole_insert.dxf.layer == "KML"
    assert pole_insert.dxf.color == 1
    assert any(entity.dxftype() == "CIRCLE" and entity.dxf.color == 1 for entity in pole_block)
    assert sum(1 for entity in pole_block if entity.dxftype() == "LINE") == 2
    assert any(
        entity.dxftype() == "TEXT"
        and entity.dxf.text == "Existing 01"
        and entity.dxf.layer == "POLE_ID"
        and entity.dxf.color == 1
        for entity in entities
    )

@pytest.mark.slow
def test_kml_folder_filter_exports_only_selected_folder(tmp_path):
    kml_input = tmp_path / "folders.kml"
    output_path = tmp_path / "filtered.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "FAT": """
<Placemark>
  <name>Line A</name>
  <LineString>
    <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
  </LineString>
</Placemark>
""",
                "HP COVER": """
<Placemark>
  <name>Line B</name>
  <LineString>
    <coordinates>106.8020,-6.2000,0 106.8030,-6.2010,0</coordinates>
  </LineString>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=["FAT"],
    )
    doc = ezdxf.readfile(output_path)
    entities = data_entities(doc)

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert {entity.dxftype() for entity in entities} == {"POLYLINE", "TEXT"}


@pytest.mark.slow
def test_distribution_cable_uses_core_layer_from_line_name_and_width(tmp_path):
    kml_input = tmp_path / "distribution-cable.kml"
    output_path = tmp_path / "distribution-cable.dxf"
    kml_content = standard_kml(
        {
            "DISTRIBUTION CABLE": """
<Folder>
  <name>LINE A - FDT 1</name>
  <Folder>
    <name>DISTRIBUTION CABLE</name>
    <Placemark>
      <name>PSGMB034.062 - CABLE LINE A - ( FO 24C/2T ) - AE - 1044M</name>
      <styleUrl>#cableStyle</styleUrl>
      <LineString>
        <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
      </LineString>
    </Placemark>
  </Folder>
</Folder>
"""
        }
    ).replace(
        "<Document>",
        '<Document><Style id="cableStyle"><LineStyle><color>ff00ff00</color><width>1</width></LineStyle></Style>',
    )
    kml_input.write_text(kml_content, encoding="utf-8")

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["DISTRIBUTION CABLE"])
    doc = ezdxf.readfile(output_path)
    cable_entities = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "FO_24_CORE"
    ]
    kml_text = [entity for entity in doc.modelspace() if entity.dxftype() == "TEXT" and entity.dxf.layer == "KML"]

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert result["stats"]["FO_24_CORE"] == 1
    assert result["stats"].get("LINE_A_FDT_1", 0) == 0
    assert "FO_24_CORE" in doc.layers
    assert len(cable_entities) == 1
    assert cable_entities[0].dxftype() == "POLYLINE"
    assert all(vertex.dxf.start_width == pytest.approx(KML_DISTRIBUTION_CABLE_WIDTH) for vertex in cable_entities[0].vertices)
    assert not any("PSGMB034.062" in entity.dxf.text for entity in kml_text)


@pytest.mark.parametrize(
    ("cable_name", "expected_layer", "expected_summary"),
    [
        ("12C", "FO_12_CORE", "12/1T"),
        ("24C", "FO_24_CORE", "24/2T"),
        ("36C", "FO_36_CORE", "36/3T"),
        ("48C", "FO_48_CORE", "48/4T"),
        ("72C", "FO_72_CORE", "72/6T"),
        ("96C", "FO_96_CORE", "96/8T"),
        ("144C", "FO_144_CORE", "144/12T"),
        ("288C", "FO_288_CORE", "288/24T"),
        ("FO 24C", "FO_24_CORE", "24/2T"),
        ("FO 36C", "FO_36_CORE", "36/3T"),
        ("FO 48C", "FO_48_CORE", "48/4T"),
    ],
)
def test_distribution_cable_short_core_names_map_to_existing_rules(cable_name, expected_layer, expected_summary):
    assert distribution_cable_core_layer(cable_name) == expected_layer
    assert distribution_cable_core_layer("CABLE LINE A", f"DISTRIBUTION CABLE {cable_name}") == expected_layer
    assert distribution_cable_summary_key(cable_name) == expected_summary
    assert distribution_cable_summary_key("CABLE LINE A", f"DISTRIBUTION CABLE {cable_name}") == expected_summary

@pytest.mark.parametrize(
    ("cable_name", "expected_layer"),
    [
        ("BACKBONE FO 12C/1T FEEDER", "FO_12_CORE"),
        ("BACKBONE FO 24C/2T FEEDER", "FO_24_CORE"),
        ("BACKBONE FO 36C/3T FEEDER", "FO_36_CORE"),
        ("BACKBONE FO 48C/4T FEEDER", "FO_48_CORE"),
        ("BACKBONE FO 72C/6T FEEDER", "FO_72_CORE"),
        ("BACKBONE 24C FEEDER", "FO_24_CORE"),
        ("BACKBONE 36C FEEDER", "FO_36_CORE"),
        ("BACKBONE 48C FEEDER", "FO_48_CORE"),
        ("BACKBONE FO 96C/8T FEEDER", "FO_96_CORE"),
        ("BACKBONE FO 144C/12T FEEDER", "FO_144_CORE"),
        ("BACKBONE FO 288C/24T FEEDER", "FO_288_CORE"),
    ],
)
@pytest.mark.slow
def test_distribution_cable_core_layer_mapping(tmp_path, cable_name, expected_layer):
    kml_input = tmp_path / f"{expected_layer}.kml"
    output_path = tmp_path / f"{expected_layer}.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "DISTRIBUTION CABLE": f"""
<Folder>
  <name>LINE SHOULD NOT BECOME LAYER</name>
  <Placemark>
    <name>{cable_name}</name>
    <LineString>
      <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
    </LineString>
  </Placemark>
</Folder>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["DISTRIBUTION CABLE"])
    doc = ezdxf.readfile(output_path)
    cable_entities = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == expected_layer
    ]

    assert result["stats"][expected_layer] == 1
    assert result["stats"].get("LINE_SHOULD_NOT_BECOME_LAYER", 0) == 0
    assert len(cable_entities) == 1
    assert doc.layers.get(expected_layer).color == DISTRIBUTION_CABLE_CORE_LAYER_COLORS[expected_layer]
    assert all(entity.dxf.color == 256 for entity in cable_entities)


@pytest.mark.slow
def test_distribution_cable_without_core_pattern_keeps_parent_folder_layer(tmp_path):
    kml_input = tmp_path / "distribution-cable-fallback.kml"
    output_path = tmp_path / "distribution-cable-fallback.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "DISTRIBUTION CABLE": """
<Folder>
  <name>LINE A - FDT 1</name>
  <Placemark>
    <name>CABLE LINE WITHOUT CORE PATTERN</name>
    <LineString>
      <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
    </LineString>
  </Placemark>
</Folder>
"""
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["DISTRIBUTION CABLE"])
    doc = ezdxf.readfile(output_path)
    cable_entities = [
        entity
        for entity in doc.modelspace()
        if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "LINE_A_FDT_1"
    ]

    assert result["stats"]["LINE_A_FDT_1"] == 1
    assert len(cable_entities) == 1


@pytest.mark.slow
def test_sling_wire_uses_sling_wire_layer_width_and_kmz_color(tmp_path):
    kml_input = tmp_path / "sling-wire.kml"
    output_path = tmp_path / "sling-wire.dxf"
    kml_content = standard_kml(
        {
            "SLING WIRE": """
<Placemark>
  <name>Sling Wire 01</name>
  <styleUrl>#slingStyle</styleUrl>
  <LineString>
    <coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates>
  </LineString>
</Placemark>
"""
        }
    ).replace(
        "<Document>",
        '<Document><Style id="slingStyle"><LineStyle><color>ff0000ff</color><width>1</width></LineStyle></Style>',
    )
    kml_input.write_text(kml_content, encoding="utf-8")

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["SLING WIRE"])
    doc = ezdxf.readfile(output_path)
    sling_entities = [
        entity for entity in doc.modelspace() if entity.dxf.layer == "SLING_WIRE" and entity.dxftype() == "POLYLINE"
    ]
    kml_text = [entity for entity in doc.modelspace() if entity.dxftype() == "TEXT" and entity.dxf.layer == "KML"]

    assert result["stats"]["KML"] == 1
    assert result["kml_features"] == 1
    assert result["stats"]["SLING_WIRE"] == 1
    assert "SLING_WIRE" in doc.layers
    assert doc.layers.get("SLING_WIRE").color == 1
    assert len(sling_entities) == 1
    assert all(vertex.dxf.start_width == pytest.approx(KML_DISTRIBUTION_CABLE_WIDTH) for vertex in sling_entities[0].vertices)
    assert not any("Sling Wire 01" in entity.dxf.text for entity in kml_text)


@pytest.mark.slow
def test_distribution_sling_and_tali_redraw_above_subduct(tmp_path):
    kml_input = tmp_path / "cable-redraw-order.kml"
    output_path = tmp_path / "cable-redraw-order.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>LINE A</name>
      <Folder>
        <name>TALI PANCING</name>
        <Placemark>
          <name>Tali top line</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>SLING WIRE</name>
        <Placemark>
          <name>Sling top line</name>
          <LineString><coordinates>106.8000,-6.2001,0 106.8010,-6.2011,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>DISTRIBUTION CABLE</name>
        <Placemark>
          <name>CABLE LINE A - FO 24C/2T</name>
          <LineString><coordinates>106.8000,-6.2002,0 106.8010,-6.2012,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>SUBDUCT 32/28</name>
        <Placemark>
          <name>Subduct bottom line</name>
          <LineString><coordinates>106.8000,-6.2003,0 106.8010,-6.2013,0</coordinates></LineString>
        </Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=["TALI PANCING", "SLING WIRE", "DISTRIBUTION CABLE", "SUBDUCT 32/28"],
    )
    doc = ezdxf.readfile(output_path)
    ordered_layers = [
        entity.dxf.layer
        for entity in doc.modelspace().entities_in_redraw_order()
        if entity.dxftype() == "POLYLINE"
    ]
    subduct_index = max(index for index, layer in enumerate(ordered_layers) if layer == "SUBDUCT_32_28")

    for layer_name in ("TALI_PANCING", "SLING_WIRE", "FO_24_CORE"):
        assert min(index for index, layer in enumerate(ordered_layers) if layer == layer_name) > subduct_index



def test_olt_icon_block_matches_fdt_size_and_faces_nearest_road(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "olt.dxf", selected_layers=["KML"])
    handler.road_centerlines.append(LineString([(12.0, -5.0), (12.0, 5.0)]))

    handler._queue_kml_olt_icon((0.0, 0.0), "OLT TEST")
    handler.finalize_olt_icons()

    inserts = [
        entity
        for entity in handler.modelspace
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_OLT_BLOCK_NAME
    ]
    assert len(inserts) == 1
    assert inserts[0].dxf.layer == KML_OLT_LAYER
    assert float(inserts[0].dxf.rotation) == pytest.approx(90.0)

    block = handler.doc.blocks.get(KML_OLT_BLOCK_NAME)
    line_points = [
        point
        for entity in block
        if entity.dxftype() == "LINE"
        for point in [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
    ]
    assert max(point[0] for point in line_points) - min(point[0] for point in line_points) == pytest.approx(KML_FDT_ICON_WIDTH_METERS)
    assert max(point[1] for point in line_points) - min(point[1] for point in line_points) == pytest.approx(KML_FDT_ICON_HEIGHT_METERS)
    assert any(entity.dxftype() == "CIRCLE" for entity in block)
    assert sum(1 for entity in block if entity.dxftype() == "LWPOLYLINE") >= 3

@pytest.mark.slow
def test_ug_folders_use_subduct_layers_and_handhole_twey_blocks(tmp_path):
    kml_input = tmp_path / "ug-folders.kml"
    output_path = tmp_path / "ug-folders.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>LINE A</name>
      <Folder>
        <name>SUBDUCT 32/28</name>
        <Placemark>
          <name>43</name>
          <LineString><coordinates>106.8000,-6.2000,0 106.8010,-6.2010,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>TALI PANICNG</name>
        <Placemark>
          <name>Tali typo</name>
          <LineString><coordinates>106.8050,-6.2050,0 106.8052,-6.2052,0</coordinates></LineString>
        </Placemark>
      </Folder>
      <Folder>
        <name>NEW HH 20X20X20</name>
        <Placemark><name>EXT.MR.LCK.S01.P005</name><Point><coordinates>106.7999,-6.2001,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>NEW HH 40X40X60</name>
        <Placemark><name>EXT.MR.LCK.S01.P003</name><Point><coordinates>106.8000,-6.2001,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>NEW HH 80X80X130</name>
        <Placemark><name>EXT.MR.LCK.S01.P001</name><Point><coordinates>106.8001,-6.2001,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>FAT</name>
        <Placemark><name>A01</name><Point><coordinates>106.8002,-6.2001,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>TWEY</name>
        <Placemark><name>Reducer</name><Point><coordinates>106.8005,-6.1997,0</coordinates></Point></Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=["SUBDUCT 32/28", "TALI PANCING", "NEW HH 20X20X20", "NEW HH 40X40X60", "NEW HH 80X80X130", "FAT", "TWEY"],
    )
    doc = ezdxf.readfile(output_path)
    subduct_entities = [
        entity for entity in doc.modelspace() if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "SUBDUCT_32_28"
    ]
    tali_entities = [
        entity for entity in doc.modelspace() if entity.dxftype() == "POLYLINE" and entity.dxf.layer == "TALI_PANCING"
    ]
    hh40_inserts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_HANDHOLE_40_BLOCK_NAME
    ]
    hh20_inserts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_HANDHOLE_20_BLOCK_NAME
    ]
    hh80_inserts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_HANDHOLE_80_BLOCK_NAME
    ]
    fat_inserts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    ]
    twey_inserts = [
        entity for entity in doc.modelspace() if entity.dxftype() == "INSERT" and entity.dxf.name == KML_TWEY_BLOCK_NAME
    ]

    assert result["stats"]["KML"] == 7
    assert result["stats"]["SUBDUCT_32_28"] == 1
    assert result["stats"]["TALI_PANCING"] == 1
    assert result["stats"][KML_HANDHOLE_20_LAYER] == 1
    assert result["stats"][KML_HANDHOLE_40_LAYER] == 1
    assert result["stats"][KML_HANDHOLE_80_LAYER] == 1
    assert result["stats"][KML_FAT_LAYER] == 1
    assert result["stats"][KML_TWEY_LAYER] == 1
    assert len(subduct_entities) == 1
    assert subduct_entities[0].dxf.lineweight == KML_SUBDUCT_32_LINEWEIGHT
    assert all(vertex.dxf.start_width == pytest.approx(KML_SUBDUCT_32_WIDTH) for vertex in subduct_entities[0].vertices)
    assert len(tali_entities) == 1
    assert len(hh20_inserts) == 1
    assert len(hh40_inserts) == 1
    assert len(hh80_inserts) == 1
    assert len(fat_inserts) == 1
    assert len(twey_inserts) == 1
    handhole_label_texts = [
        entity.dxf.text
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.layer in {KML_HANDHOLE_20_LAYER, KML_HANDHOLE_40_LAYER, KML_HANDHOLE_80_LAYER}
    ]
    assert not any("HANDHOLE" in text or "20x20x20" in text or "40x40x60" in text or "80x80x130" in text for text in handhole_label_texts)
    assert "MR.LCK.S01.HH001" in handhole_label_texts
    assert "MR.LCK.S01.HH002" in handhole_label_texts
    assert "MR.LCK.S01.HHPIT001" in handhole_label_texts
    assert kml_handhole_source_code("MR.LCK.S01.HH001") == "LCK.S01"
    assert kml_handhole_source_code("EXT.MR.LCK.S01.HH001") == "LCK.S01"
    assert hh20_inserts[0].dxf.layer == KML_HANDHOLE_20_LAYER
    assert hh20_inserts[0].dxf.color == KML_HANDHOLE_20_COLOR
    assert hh40_inserts[0].dxf.layer == KML_HANDHOLE_40_LAYER
    assert hh80_inserts[0].dxf.layer == KML_HANDHOLE_80_LAYER
    assert twey_inserts[0].dxf.layer == KML_TWEY_LAYER
    twey_source = project_point(106.8005, -6.1997)
    assert twey_inserts[0].dxf.insert.x == pytest.approx(twey_source[0])
    assert twey_inserts[0].dxf.insert.y == pytest.approx(twey_source[1])
    assert abs(float(twey_inserts[0].dxf.rotation)) > 1
    assert abs(float(hh20_inserts[0].dxf.rotation)) > 1
    assert abs(float(hh40_inserts[0].dxf.rotation)) > 1
    assert abs(float(hh80_inserts[0].dxf.rotation)) > 1
    assert abs(float(fat_inserts[0].dxf.rotation)) > 1
    twey_block = doc.blocks.get(KML_TWEY_BLOCK_NAME)
    line_points = [
        point
        for entity in twey_block
        if entity.dxftype() == "LINE"
        for point in [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
    ]
    assert max(point[0] for point in line_points) - min(point[0] for point in line_points) == pytest.approx(KML_TWEY_ICON_SIZE_METERS)
    hh20_block = doc.blocks.get(KML_HANDHOLE_20_BLOCK_NAME)
    hh20_line_points = [
        point
        for entity in hh20_block
        if entity.dxftype() == "LINE"
        for point in [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
    ]
    assert max(point[0] for point in hh20_line_points) - min(point[0] for point in hh20_line_points) == pytest.approx(KML_TWEY_ICON_SIZE_METERS)
    assert all(entity.dxftype() != "TEXT" for entity in hh20_block)
    assert KML_TWEY_ICON_SIZE_METERS < 3.0


@pytest.mark.slow
def test_existing_hh_dxf_labels_renumber_without_changing_icon_paths(tmp_path):
    kml_input = tmp_path / "existing-hh.kml"
    output_path = tmp_path / "existing-hh.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>LINE A</name>
      <Folder>
        <name>EXISTING HH 40X40X60</name>
        <Placemark><name>EXT.MR.PTPB09.P003</name><Point><coordinates>106.8003,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>EXISTING HH 20X20X20</name>
        <Placemark><name>EXT.MR.PTPB09.P005</name><Point><coordinates>106.8005,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
      <Folder>
        <name>EXISTING HH 80X80X130</name>
        <Placemark><name>EXT.MR.PTPB09.P001</name><Point><coordinates>106.8001,-6.2000,0</coordinates></Point></Placemark>
      </Folder>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=[
            "EXISTING HH 20X20X20",
            "EXISTING HH 40X40X60",
            "EXISTING HH 80X80X130",
        ],
        network_type="UG",
    )
    doc = ezdxf.readfile(output_path)
    labels = sorted(
        entity.dxf.text
        for entity in doc.modelspace()
        if entity.dxftype() == "TEXT" and entity.dxf.text.startswith("EXT.MR.PTPB09.")
    )

    assert labels == sorted(
        [
            "EXT.MR.PTPB09.HH001",
            "EXT.MR.PTPB09.HH002",
            "EXT.MR.PTPB09.HHPIT001",
        ]
    )
    assert result["kml_details"]["handhole_blocks"] == 2
    assert result["kml_details"]["generic_icons"] == 1


@pytest.mark.slow
def test_fat_and_fdt_icons_redraw_above_handhole(tmp_path):
    kml_input = tmp_path / "fat-fdt-above-hh.kml"
    output_path = tmp_path / "fat-fdt-above-hh.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "NEW HH 40X40X60": """
<Placemark><name>HH40</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
""",
                "FAT": """
<Placemark><name>A01</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
""",
                "FDT": """
<Placemark><name>FDT-1</name><Point><coordinates>106.8000,-6.2000,0</coordinates></Point></Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    convert_osm_to_dxf(
        kml_input,
        output_path,
        selected_kml_folders=["NEW HH 40X40X60", "FAT", "FDT"],
        network_type="UG",
    )
    doc = ezdxf.readfile(output_path)
    ordered_entities = list(doc.modelspace().entities_in_redraw_order())
    handhole_index = max(
        index
        for index, entity in enumerate(ordered_entities)
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_HANDHOLE_40_BLOCK_NAME
    )
    fat_index = min(
        index
        for index, entity in enumerate(ordered_entities)
        if entity.dxftype() == "INSERT" and entity.dxf.name == KML_FAT_BLOCK_NAME
    )
    fdt_index = min(
        index
        for index, entity in enumerate(ordered_entities)
        if is_kml_fdt_insert(entity)
    )

    assert fat_index > handhole_index
    assert fdt_index > handhole_index

@pytest.mark.slow
def test_kml_nested_child_folder_counts_as_nearest_standard_folder(tmp_path):
    kml_input = tmp_path / "nested-standard.kml"
    output_path = tmp_path / "nested-standard.dxf"
    kml_input.write_text(
        standard_kml(
            {
                "HP COVER": """
<Folder>
  <name>A01</name>
  <Placemark>
    <name>Homepass</name>
    <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
  </Placemark>
</Folder>
""",
                "FAT": """
<Placemark>
  <name>FAT skipped</name>
  <Point><coordinates>106.8010,-6.2010,0</coordinates></Point>
</Placemark>
""",
            }
        ),
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path, selected_kml_folders=["HP COVER"])
    doc = ezdxf.readfile(output_path)
    home_texts = [entity for entity in doc.modelspace() if entity.dxf.layer == KML_HOME_NUMBER_LAYER]
    home_boxes = [entity for entity in doc.modelspace() if entity.dxf.layer == KML_HOME_NUMBER_BOX_LAYER]

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_HOME_NUMBER_LAYER] == 1
    assert result["kml_features"] == 1
    assert {entity.dxftype() for entity in home_texts} == {"TEXT"}
    assert any(entity.dxftype() == "POLYLINE" for entity in home_boxes)
    assert any(entity.dxftype() == "TEXT" and entity.dxf.text == "Homepass" for entity in home_texts)


@pytest.mark.slow
def test_kml_missing_standard_folders_is_allowed(tmp_path):
    kml_input = tmp_path / "partial.kml"
    output_path = tmp_path / "partial.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>FAT</name>
      <Placemark>
        <name>FAT-01</name>
        <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
      </Placemark>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path)

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FAT_LAYER] == 1
    assert result["kml_features"] == 1

@pytest.mark.slow
def test_kml_typo_standard_folder_is_ignored(tmp_path):
    kml_input = tmp_path / "typo-folder.kml"
    output_path = tmp_path / "typo-folder.dxf"
    kml_input.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Folder>
      <name>EXISTING POLE EMR 7-2.5.</name>
      <Placemark>
        <name>Typo Pole</name>
        <Point><coordinates>106.8000,-6.2000,0</coordinates></Point>
      </Placemark>
    </Folder>
    <Folder>
      <name>FAT</name>
      <Placemark>
        <name>FAT-OK</name>
        <Point><coordinates>106.8010,-6.2010,0</coordinates></Point>
      </Placemark>
    </Folder>
  </Document>
</kml>
""",
        encoding="utf-8",
    )

    result = convert_osm_to_dxf(kml_input, output_path)

    assert result["stats"]["KML"] == 1
    assert result["stats"][KML_FAT_LAYER] == 1
    assert result["kml_features"] == 1
