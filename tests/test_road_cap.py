"""ROAD full-width cap10m via actual estimator/cache/raw-buffer functions.
Run: python test_road_cap.py /path/to/converter.py
AST-only; no app import, network or shared writes.
"""
import ast
import json
import math
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString

source = Path(sys.argv[1]).read_text()
tree = ast.parse(source)
namespace = {'json': json, 'math': math, 're': re, 'sqlite3': sqlite3, 'Path': Path, 'LineString': LineString,
             'Point': tuple, 'Sequence': list, '_distance': math.dist, 'tags_to_dict': dict, 'project_coordinates': lambda points, transform: [transform.transform(*p) for p in points]}
constant_names = {'DEFAULT_ROAD_WIDTHS_METERS', 'LANE_WIDTH_METERS', 'ROAD_EXCLUDED_HIGHWAYS', 'ROAD_EXCLUDED_SERVICES',
                  'ROAD_MIN_LENGTH_METERS', 'OSM_ROAD_CACHE_POLICY_SIGNATURE', 'MAX_ROAD_WIDTH_METERS'}
function_names = {'tag_value', '_clamp', 'parse_osm_measure_meters', 'estimate_road_half_width_meters', '_clean_points', 'road_cache_is_fresh', 'should_export_road', 'classify_tags'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in function_names or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constant_names for t in n.targets)]
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OSMToDXFHandler')
cls.bases = []
cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'_collect_road_buffer', 'load_roads_from_cache'}]
nodes.append(cls)
exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-road-cap-code>', 'exec'), namespace)
estimate = namespace['estimate_road_half_width_meters']
count = 0
for tags, expected in [({'highway': 'trunk'}, 5), ({'highway': 'primary'}, 5), ({'highway': 'secondary'}, 5),
                       ({'highway': 'tertiary'}, 4.5), ({'highway': 'residential'}, 2), ({'highway': 'track'}, 1.75),
                       ({'highway': 'trunk', 'width': '40'}, 5), ({'width': '9'}, 4.5), ({'width': '10'}, 5),
                       ({'width': '10.01'}, 5), ({'est_width': '25'}, 5), ({'width': '40 ft'}, 5),
                       ({'width': '20 ft'}, 3.048), ({'lanes': '4'}, 5), ({'lanes': '2'}, 3.7)]:
    assert abs(estimate(tags) - expected) <= 1e-9, (tags, estimate(tags), expected)
    count += 1
Renderer = namespace['OSMToDXFHandler']
for half, expected in ((7.0, 5.0), (20.0, 5.0), (5.0, 5.0), (2.0, 2.0)):
    handler = Renderer()
    handler.road_centerlines = []
    handler.road_buffers = []
    handler._road_buffer_centerlines = []
    handler._collect_road_buffer([(0, 0), (50, 0)], half)
    solid = handler.road_buffers[0]
    assert abs(solid.bounds[3] - solid.bounds[1] - expected * 2) < 1e-9, solid.bounds
    assert list(handler.road_centerlines[0].coords) == [(0, 0), (50, 0)]
    count += 1
# Cached full width14 and manual offset7 both obey10m even before rebuild.
with tempfile.TemporaryDirectory(prefix='road-cap-test-') as directory:
    db = Path(directory) / 'roads.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE roads (half_width REAL,name TEXT,coordinates TEXT,min_lon REAL,max_lon REAL,min_lat REAL,max_lat REAL)')
        connection.execute('INSERT INTO roads VALUES(7,"",?,0,1,0,1)', (json.dumps([[0, 0], [0.001, 0]]),))
    for manual in (None, 7.0, 2.0):
        handler = Renderer()
        handler.selected_layers = {'ROAD'}
        handler.transformer = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
        handler.road_offset_meters = manual
        handler.road_centerlines = []
        handler.road_buffers = []
        handler._road_buffer_centerlines = []
        handler.stats = {'ROAD': 0}
        handler.processed_ways = 0
        handler._queue_road_name = lambda *args: None
        assert handler.load_roads_from_cache(db) == 1
        expected = 4.0 if manual == 2.0 else 10.0
        solid = handler.road_buffers[0]
        assert abs(solid.bounds[3] - solid.bounds[1] - expected) < 1e-9
        count += 1
# Old cache policy must fail freshness after cap introduction.
with tempfile.TemporaryDirectory(prefix='road-cap-cache-') as directory:
    pbf = Path(directory) / 'roads.pbf'
    pbf.write_bytes(b'fixture only')
    cache = Path(directory) / 'roads.sqlite'
    stat = pbf.stat()
    policy = json.loads(namespace['OSM_ROAD_CACHE_POLICY_SIGNATURE'])
    assert policy.get('max_road_width_m') == 10.0, policy
    old_policy = dict(policy)
    old_policy.pop('max_road_width_m')
    old_policy['schema'] = 2
    with sqlite3.connect(cache) as connection:
        connection.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        connection.executemany('INSERT INTO meta VALUES(?,?)', [('source_size', str(stat.st_size)), ('source_mtime_ns', str(stat.st_mtime_ns)), ('policy_signature', json.dumps(old_policy, sort_keys=True, separators=(',', ':')))])
    assert not namespace['road_cache_is_fresh'](pbf, cache), 'Stale width cache accepted'
    with sqlite3.connect(cache) as connection:
        connection.execute('UPDATE meta SET value=? WHERE key="policy_signature"', (namespace['OSM_ROAD_CACHE_POLICY_SIGNATURE'],))
    assert namespace['road_cache_is_fresh'](pbf, cache)
    count += 1
print(json.dumps({'passed': True, 'cases': count, 'road_full_width_cap_m': 10, 'narrow_roads_unchanged': True, 'old_cache_invalidated': True}))
