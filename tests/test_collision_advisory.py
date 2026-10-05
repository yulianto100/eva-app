"""Operator policy: Distribution/Sling collisions warn; attachments remain mandatory.
Run: python test_collision_advisory.py /path/to/converter.py
Actual renderer AST only; no production app, schedulers or network.
"""
import ast
import hashlib
import itertools
import json
import math
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import ezdxf
from shapely import wkt
from shapely.geometry import LineString, Point as ShapelyPoint
from shapely.ops import substring
from shapely.strtree import STRtree

root = Path(__file__).resolve().parent
source = Path(sys.argv[1]).read_text()
tree = ast.parse(source)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OSMToDXFHandler")
methods = {"_nearest_vertex_pole", "_source_cable_contact", "_preview_pole_follow_lines", "_render_pole_follow_lines", "_validate_new_cable_crossings", "_space_unanchored_parallel_cables", "_cable_collision_warning"}
cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
cls.bases = []
log_function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pipeline_log")
namespace = {"__name__": "cable_regression", "ezdxf": ezdxf, "math": math,
             "defaultdict": defaultdict, "LineString": LineString, "ShapelyPoint": ShapelyPoint,
             "STRtree": STRtree, "substring": substring, "Point": tuple, "_distance": math.dist,
             "KML_POLE_ICON_SIZE_METERS": 4.0, "KML_ROAD_POLE_MAX_SHIFT_METERS": 8.0,
             "KML_TALI_PANCING_LAYER": "TALI_PANCING"}
exec(compile(ast.Module(body=[log_function, cls], type_ignores=[]), "<actual-converter-renderer>", "exec"), namespace)
Renderer = namespace["OSMToDXFHandler"]
fixture = json.loads((root / "replay-baseline" / "before-finalizer.json").read_text())
passed = 0
advisory_crossings = 0
for reverse_records, reverse_vertices, road_enabled in itertools.product((False, True), repeat=3):
    handler = Renderer()
    handler._roadside_poles = []
    road = wkt.loads(fixture["road_wkt"]) if road_enabled else None
    handler._road_avoidance_geometry = lambda: road
    for p in fixture["poles"]:
        scale = p["scale"]
        icon = SimpleNamespace(dxf=SimpleNamespace(get=lambda key, default=1.0, scale=scale: scale))
        handler._roadside_poles.append({**p, "icon": icon, "target": tuple(p["source"])})
    doc = ezdxf.new()
    records = fixture["records"][::-1] if reverse_records else fixture["records"]
    handler._pole_follow_lines = []
    for r in records:
        points = r["source"][::-1] if reverse_vertices else r["source"]
        entity = doc.modelspace().add_polyline2d(points, dxfattribs={"layer": r["layer"]})
        handler._pole_follow_lines.append({**r, "source": points, "entity": entity})
    warnings = []
    handler._render_pole_follow_lines(SimpleNamespace(warn=warnings.append))
    if not reverse_records and not reverse_vertices:
        assert any("New cable crossing" in message for message in warnings), warnings
    preview = handler._preview_pole_follow_lines()
    with tempfile.TemporaryDirectory(prefix="cable-advisory-regression-") as directory:
        file = Path(directory) / "result.dxf"
        doc.saveas(file)
        saved = ezdxf.readfile(file)
        assert not saved.audit().errors
        cables = list(saved.modelspace().query("POLYLINE"))
        assert len(cables) == len(records) == 6
        output_lines = []
        for i, (record, entity) in enumerate(zip(handler._pole_follow_lines, cables)):
            points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            assert points == list(preview[i].coords), "Preview/saved mismatch"
            assert len(points) == len(record["source"]), "Lost native visits"
            assert entity.dxf.layer == record["layer"], "Cable identity changed"
            output_lines.append(LineString(points))
            for origin, target in zip(record["source"], points):
                pole = handler._nearest_vertex_pole(origin)
                if pole is None:
                    assert tuple(origin) == target, "Unmatched vertex moved"
                else:
                    assert math.dist(target, pole["target"]) <= pole["radius"] + 1e-7, "Attachment outside pole"
        for i, j in itertools.combinations(range(6), 2):
            original = LineString(records[i]["source"]).intersection(LineString(records[j]["source"]))
            cross = output_lines[i].intersection(output_lines[j])
            advisory_crossings += int(original.is_empty and not cross.is_empty)
        assert all(tuple(p["target"]) == tuple(p["source"]) for p in handler._roadside_poles)
    passed += 1

# The allowance applies to these cable classes only, including same-class pairs.
for first_kind, second_kind in itertools.product(("distribution", "sling", None), repeat=2):
    handler = Renderer()
    doc = ezdxf.new()
    handler._pole_follow_lines = []
    for kind, source_points, output_points in [(first_kind, [(0, 0), (10, 0)], [(0, 0), (10, 0)]),
                                               (second_kind, [(5, 2), (5, 10)], [(5, -2), (5, 10)])]:
        handler._pole_follow_lines.append({"cable_kind": kind, "source": source_points,
                                          "entity": doc.modelspace().add_polyline2d(output_points)})
    should_allow = first_kind is not None and second_kind is not None
    try:
        handler._validate_new_cable_crossings()
    except ValueError:
        assert not should_allow, (first_kind, second_kind)
    else:
        assert should_allow, "Unrelated line guard was weakened"
    passed += 1

# High-capacity shared poles must keep all supported cable vertices in-icon,
# even when 0.7m lane separation is geometrically impossible.
handler = Renderer()
handler._road_avoidance_geometry = lambda: None
handler._roadside_poles = [{"source": (0, 0), "target": (0, 0), "group": ("LINE A",),
                           "icon": SimpleNamespace(dxf=SimpleNamespace(get=lambda key, default=1.0: 0.5))}]
doc = ezdxf.new()
handler._pole_follow_lines = []
for i in range(8):
    points = [(0, 0), (20, i)]
    handler._pole_follow_lines.append({"cable_kind": "distribution" if i % 2 else "sling",
                                      "source": points, "group": ("LINE A",),
                                      "entity": doc.modelspace().add_polyline2d(points)})
handler._render_pole_follow_lines()
for record in handler._pole_follow_lines:
    first = record["entity"].vertices[0].dxf.location
    assert math.hypot(first.x, first.y) <= 1.0 + 1e-7
    last = record["entity"].vertices[-1].dxf.location
    assert (last.x, last.y) == tuple(record["source"][-1])
passed += 1
assert advisory_crossings > 0, "Fixture did not retain any advisory collision"
print(json.dumps({"passed": True, "cases": passed, "converter_sha256": hashlib.sha256(source.encode()).hexdigest(),
                  "saved_dxf_audited": True, "advisory_crossings": advisory_crossings, "attachment_failures": 0}))
