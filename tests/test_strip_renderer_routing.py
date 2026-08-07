import sys
from pathlib import Path
from shapely.geometry import LineString, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler


def test_strip_parcel_is_not_replaced_by_compact_fallback(tmp_path):
    h = OSMToDXFHandler(tmp_path / 'out.dxf')
    h.road_centerlines = [LineString([(0, 0), (60, 0)])]
    h._home_parcel_road_solid_cache = Polygon([(0, -2), (60, -2), (60, 2), (0, 2)])
    h._home_parcel_road_solid_ready = True
    sites = [(10, 7), (15, 7)]
    region = Polygon([(0, 2), (60, 2), (60, 25), (0, 25)])
    strips = h._road_strip_cells([0, 1], sites, region, 0)
    assert len(strips) == 2
    assert all(p.area >= 40 for p in strips.values())
    # Route invariant: accepted strip gets drawn as-is, not compact re-fit.
    assert h._parcel_for_strip_or_fallback(strips[0], sites[0], region, 0).equals(strips[0])

if __name__ == '__main__':
    test_strip_parcel_is_not_replaced_by_compact_fallback(Path('/tmp'))
    print('STRIP_RENDERER_ROUTING_TEST_PASS')
