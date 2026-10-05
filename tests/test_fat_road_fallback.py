"""Retained source-pole ROAD obstruction: keep FAT beside pole, warn, no relocation.
Run: python test_fat_road_fallback.py /path/to/converter.py
Pure AST slice; no app import, network, or production writes.
"""
import ast
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

from shapely.geometry import Point as ShapelyPoint, Polygon, box

text = Path(sys.argv[1]).read_text()
tree = ast.parse(text)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OSMToDXFHandler")
cls.bases = []
cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_plan_ae_fat_icons"]
log = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pipeline_log")
ns = {"__name__": "fat_fallback_test", "math": math, "ShapelyPoint": ShapelyPoint, "Polygon": Polygon,
      "_distance": math.dist, "_clamp": lambda x, low, high: min(max(x, low), high),
      "KML_POLE_SNAP_RADIUS_METERS": 5.0, "KML_ROAD_POLE_MAX_SHIFT_METERS": 8.0,
      "KML_POLE_ICON_SIZE_METERS": 4.0, "KML_FAT_TO_POLE_GAP_METERS": 0.6,
      "KML_FAT_ICON_WIDTH_METERS": 3.2, "KML_FAT_ICON_HEIGHT_METERS": 2.2,
      "suggest_dxf_asset_layout": lambda **kwargs: {}}
exec(compile(ast.Module(body=[log, cls], type_ignores=[]), "<production-fat-planner>", "exec"), ns)
Renderer = ns["OSMToDXFHandler"]

def make(target=(0, 0), road=None):
    handler = Renderer()
    pending = {"code": "B03", "projected": (0.1, 0.1), "group": ("LINE B",), "style": {"icon_scale": 0.8}}
    icon = SimpleNamespace(dxf=SimpleNamespace(insert=SimpleNamespace(x=target[0], y=target[1]), get=lambda key, default=1: 0.8))
    pole = {"source": (0, 0), "target": target, "icon": icon}
    handler._roadside_poles = [pole]
    handler.pending_fat_icons = [pending]
    handler._source_owned_asset_pole = lambda _: pole
    handler._serving_pole_cable_rotation = lambda *args: 0.0
    handler._road_avoidance_geometry = lambda: road
    return handler, pole

# Source-centred 20m ROAD cannot be escaped within the 8m pole limit.
handler, pole = make(road=box(-10, -10, 10, 10))
messages = []
if "pipeline_logger" in next(n for n in cls.body if n.name == "_plan_ae_fat_icons").args.args[-1].arg:
    plans = handler._plan_ae_fat_icons(SimpleNamespace(warn=messages.append))
else:
    plans = handler._plan_ae_fat_icons()
assert len(plans) == 1
assert pole["target"] == pole["source"]
assert 1.6 < math.dist(plans[0]["center"], pole["target"]) < 4.0
assert any("ROAD" in message for message in messages), messages

# A moved pole cannot opt into the source-retained exception.
handler, _ = make(target=(1, 0), road=box(-10, -10, 10, 10))
try:
    handler._plan_ae_fat_icons()
except ValueError as exc:
    assert "cannot attach" in str(exc)
else:
    raise AssertionError("Moved-pole ROAD guard was weakened")

# Missing source owner and displacement remain fatal.
handler, _ = make(road=None)
handler._source_owned_asset_pole = lambda _: None
try:
    handler._plan_ae_fat_icons()
except ValueError as exc:
    assert "no serving pole" in str(exc)
else:
    raise AssertionError("Missing FAT source owner was waived")
handler, _ = make(target=(8.001, 0), road=None)
try:
    handler._plan_ae_fat_icons()
except ValueError as exc:
    assert "relocation limit" in str(exc)
else:
    raise AssertionError("Pole displacement limit waived")
print(json.dumps({"passed": True, "cases": 4, "source_pole_unchanged": True, "road_advisory_only_for_retained_pole": True}))
