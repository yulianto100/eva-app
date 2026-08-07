import sys
from pathlib import Path
from shapely.geometry import Point
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler


def test_open_grid_packs_shared_edge_parcels(tmp_path):
    handler = OSMToDXFHandler(tmp_path / 'out.dxf')
    sites = [(10, 10), (15, 10), (20, 10), (10, 20), (15, 20), (20, 20)]
    source_before = list(sites)
    parcels = handler._open_ai_grid_cells(
        list(range(len(sites))), sites, 0.0,
        {'along_shift_m': 2.0, 'normal_shift_m': 1.0},
    )
    assert set(parcels) == set(range(len(sites)))
    assert sites == source_before  # immutable KMZ/projected source input
    for parcel in parcels.values():
        assert 40.0 <= parcel.area <= 169.0
    for left, right in ((0, 1), (1, 2), (3, 4), (4, 5), (0, 3), (1, 4), (2, 5)):
        assert parcels[left].distance(parcels[right]) < 1e-7
        assert parcels[left].intersection(parcels[right]).area < 1e-7
    for index, source in enumerate(sites):
        assert parcels[index].distance(Point(source)) <= 12.0 + 1e-7


if __name__ == '__main__':
    test_open_grid_packs_shared_edge_parcels(Path('/tmp'))
    print('OPEN_AI_GRID_TEST_PASS')
