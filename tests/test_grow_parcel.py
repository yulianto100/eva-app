import sys
from pathlib import Path
from shapely.geometry import LineString, Point, Polygon
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler

def test_grow_fills_space_between_close_neighbors(tmp_path):
    h = OSMToDXFHandler(tmp_path / 'x.dxf')
    h.road_centerlines = [LineString([(0, 0), (80, 0)])]
    road = Polygon([(0, -2), (80, -2), (80, 2), (0, 2)])
    h._home_parcel_road_solid_cache = road
    h._home_parcel_road_solid_ready = True
    # Close sources: 5m apart, well within 13m cap
    sites = [(10, 7), (15, 7), (20, 7), (25, 7)]
    cells = h._road_strip_cells(list(range(4)), sites, None, 0.0)
    assert set(cells) == set(range(4))
    parcels = [cells[i] for i in range(4)]
    # Adjacent parcels share edges (gap < 0.01m)
    for i in range(len(parcels) - 1):
        gap = parcels[i].distance(parcels[i + 1])
        assert gap < 0.01, f'gap {gap} between {i} and {i+1}'
    # Each parcel covers its source
    for i, p in enumerate(parcels):
        assert p.covers(Point(sites[i]))
    # No ROAD overlap
    for p in parcels:
        assert p.intersection(road).area <= 1e-7
    # Area grew beyond minimum 40
    for p in parcels:
        assert p.area >= 40
    # Endpoint parcels grew wider than midpoint (they extend outward up to 13m cap)
    assert parcels[0].area >= parcels[1].area - 1.0
    assert parcels[3].area >= parcels[2].area - 1.0

if __name__ == '__main__':
    test_grow_fills_space_between_close_neighbors(Path('/tmp'))
    print('GROW_NEIGHBOR_TEST_PASS')
