import sys
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler


def make_handler(tmp_path):
    handler = OSMToDXFHandler(tmp_path / "out.dxf")
    handler.road_centerlines = [LineString([(0, 0), (80, 0)])]
    road = Polygon([(0, -2), (80, -2), (80, 2), (0, 2)])
    handler._home_parcel_road_solid_cache = road
    handler._home_parcel_road_solid_ready = True
    return handler, road


def test_road_front_rows_use_minimum_4x10_and_share_edges(tmp_path):
    handler, road = make_handler(tmp_path)
    sites = [(8, 7), (14, 7), (20, 7), (8, 17), (14, 17), (20, 17)]
    region = Polygon([(0, 2), (80, 2), (80, 35), (0, 35)])

    cells = handler._road_front_row_cells(list(range(len(sites))), sites, region)

    assert set(cells) == set(range(len(sites)))
    parcels = [cells[i] for i in range(len(sites))]
    assert all(parcel.covers(Point(sites[i])) for i, parcel in enumerate(parcels))
    assert all(parcel.area >= 40.0 for parcel in parcels)
    assert all(parcel.intersection(road).area <= 1e-7 for parcel in parcels)
    assert all(parcels[i].intersection(parcels[j]).area <= 1e-7 for i in range(len(parcels)) for j in range(i))
    # Front row touches ROAD boundary; follow row shares a clean parcel edge.
    assert min(y for x, y in parcels[0].exterior.coords) == 2.0
    assert min(y for x, y in parcels[3].exterior.coords) == max(y for x, y in parcels[0].exterior.coords)


def test_road_front_cell_grows_only_up_to_13x13(tmp_path):
    handler, _road = make_handler(tmp_path)
    site = [(30, 8)]
    region = Polygon([(0, 2), (80, 2), (80, 50), (0, 50)])

    parcel = handler._road_front_row_cells([0], site, region)[0]

    assert parcel.area == 169.0
    minx, miny, maxx, maxy = parcel.bounds
    assert maxx - minx == 13.0
    assert maxy - miny == 13.0


if __name__ == '__main__':
    test_road_front_rows_use_minimum_4x10_and_share_edges(Path('/tmp'))
    test_road_front_cell_grows_only_up_to_13x13(Path('/tmp'))
    print('ROAD_FRONT_MODULE_TEST_PASS')
