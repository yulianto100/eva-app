import sys
from pathlib import Path

from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler


def test_compact_parcel_ladder_fits_smallest_intact_module(tmp_path):
    handler = OSMToDXFHandler(tmp_path / 'out.dxf')
    allowed = Polygon([(0, 0), (4, 0), (4, 10), (0, 10)])

    parcel = handler._fit_compact_parcel_ladder((2, 5), allowed, 0.0)

    assert parcel is not None
    assert parcel.area == 40.0
    assert allowed.covers(parcel)


def test_compact_parcel_ladder_returns_none_when_no_legal_module(tmp_path):
    handler = OSMToDXFHandler(tmp_path / 'out.dxf')
    allowed = Polygon([(0, 0), (1.9, 0), (1.9, 20), (0, 20)])

    assert handler._fit_compact_parcel_ladder((1, 10), allowed, 0.0) is None


if __name__ == '__main__':
    test_compact_parcel_ladder_fits_smallest_intact_module(Path('/tmp'))
    test_compact_parcel_ladder_returns_none_when_no_legal_module(Path('/tmp'))
    print('COMPACT_PARCEL_LADDER_TEST_PASS')
