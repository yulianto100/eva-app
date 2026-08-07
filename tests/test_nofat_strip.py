import sys
from pathlib import Path
from shapely.geometry import LineString, Point, Polygon
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import OSMToDXFHandler

def test_no_fat_strip_uses_local_road_region(tmp_path):
    h=OSMToDXFHandler(tmp_path/'out.dxf')
    h.road_centerlines=[LineString([(0,0),(80,0)])]
    road=Polygon([(0,-2),(80,-2),(80,2),(0,2)])
    h._home_parcel_road_solid_cache=road; h._home_parcel_road_solid_ready=True
    sites=[(8,7),(13,7),(18,7),(8,17),(13,17),(18,17)]
    cells=h._road_strip_cells(list(range(6)),sites,None,0.0)
    assert set(cells)==set(range(6))
    parcels=[cells[i] for i in range(6)]
    assert all(p.covers(Point(sites[i])) for i,p in enumerate(parcels))
    assert all(p.area>=40 for p in parcels)
    assert all(p.intersection(road).area<=1e-7 for p in parcels)
    assert all(parcels[i].intersection(parcels[j]).area<=1e-7 for i in range(6) for j in range(i))

if __name__=='__main__':
    test_no_fat_strip_uses_local_road_region(Path('/tmp'))
    print('NO_FAT_STRIP_TEST_PASS')
