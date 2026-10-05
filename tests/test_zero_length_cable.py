"""Zero-length cables retain fatal source-direction validation.
Run: python test_zero_length_cable.py /path/to/converter.py
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

# Reuse the existing AST-only renderer/Ezdxf bootstrap, not its policy cases.
harness = Path(__file__).with_name("test_collision_advisory.py")
bootstrap = ast.parse(harness.read_text())
for index, node in enumerate(bootstrap.body):
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "fixture" for t in node.targets):
        bootstrap.body = bootstrap.body[:index]
        break
scope = {"__file__": str(harness)}
exec(compile(bootstrap, str(harness), "exec"), scope)
handler = scope["Renderer"]()
handler._road_avoidance_geometry = lambda: None
handler._roadside_poles = [{"source": (0, 0), "target": (0, 0), "group": ("LINE A",),
                           "icon": SimpleNamespace(dxf=SimpleNamespace(get=lambda key, default=1.0: 1.0))}]
doc = scope["ezdxf"].new()
points = [(0, 0), (0, 0)]
handler._pole_follow_lines = [{"cable_kind": "distribution", "source": points, "group": ("LINE A",),
                              "entity": doc.modelspace().add_polyline2d(points)}]
try:
    handler._render_pole_follow_lines()
except ValueError as error:
    assert str(error) == "Coincident cable visits have no source lane direction", str(error)
else:
    raise AssertionError("Zero-length cable must fail source-direction validation")
print(json.dumps({"passed": True, "cases": 1,
                  "converter_sha256": scope["hashlib"].sha256(scope["source"].encode()).hexdigest()}))
