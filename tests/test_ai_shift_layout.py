import sys
from pathlib import Path
from shapely.geometry import LineString, Point, Polygon
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler


def make_handler(tmp_path):
    h = OSMToDXFHandler(tmp_path / 'out.dxf')
    h.road_centerlines = [LineString([(0, 0), (80, 0)])]
    h._home_parcel_road_solid_cache = Polygon([(0, -2), (80, -2), (80, 2), (0, 2)])
    h._home_parcel_road_solid_ready = True
    return h


def test_ai_shift_is_bounded_and_shared_edges_survive(tmp_path):
    h = make_handler(tmp_path)
    sites = [(10, 7), (15, 7), (20, 7), (25, 7)]
    region = Polygon([(-20, 2), (100, 2), (100, 40), (-20, 40)])
    base = h._road_strip_cells(list(range(4)), sites, region, 0.0, {})
    shifted = h._road_strip_cells(
        list(range(4)), sites, region, 0.0,
        {'normal_shift_m': 4.0, 'along_shift_m': 3.0},
    )
    assert set(base) == set(shifted) == set(range(4))
    for idx in range(4):
        assert shifted[idx].centroid.x - base[idx].centroid.x == 3.0
        assert shifted[idx].centroid.y - base[idx].centroid.y == 4.0
        assert shifted[idx].area <= 169.0 + 1e-6
        assert shifted[idx].intersection(h._home_parcel_road_solid()).area <= 1e-7
    for idx in range(3):
        assert shifted[idx].distance(shifted[idx + 1]) < 1e-6
        assert shifted[idx].intersection(shifted[idx + 1]).area <= 1e-7


def test_ai_shift_clamped_to_twelve_meters(tmp_path):
    h = make_handler(tmp_path)
    sites = [(20, 7), (25, 7)]
    region = Polygon([(-30, 2), (120, 2), (120, 50), (-30, 50)])
    base = h._road_strip_cells([0, 1], sites, region, 0.0, {})
    shifted = h._road_strip_cells(
        [0, 1], sites, region, 0.0,
        {'normal_shift_m': 50.0, 'along_shift_m': -50.0},
    )
    assert shifted[0].centroid.x - base[0].centroid.x == -12.0
    assert shifted[0].centroid.y - base[0].centroid.y == 12.0


if __name__ == '__main__':
    test_ai_shift_is_bounded_and_shared_edges_survive(Path('/tmp'))
    test_ai_shift_clamped_to_twelve_meters(Path('/tmp'))
    print('AI_SHIFT_LAYOUT_TEST_PASS')
