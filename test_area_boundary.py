from xml.etree import ElementTree as ET
from kmz_standardizer import collect_organizer_placemarks, collect_organizer_fat_areas, organizer_target_path, collect_organizer_fdt_refs, collect_organizer_cable_refs, KML_NAMESPACE
ns=KML_NAMESPACE
q=lambda n:f"{{{ns}}}{n}"
root=ET.Element(q("kml")); doc=ET.SubElement(root,q("Document")); proj=ET.SubElement(doc,q("Folder")); ET.SubElement(proj,q("name")).text="TEST"
line=ET.SubElement(proj,q("Folder")); ET.SubElement(line,q("name")).text="LINE A - FDT 1"
fat=ET.SubElement(line,q("Folder")); ET.SubElement(fat,q("name")).text="FAT"
pm=ET.SubElement(fat,q("Placemark")); ET.SubElement(pm,q("name")).text="A01"; pt=ET.SubElement(pm,q("Point")); ET.SubElement(pt,q("coordinates")).text="0.5,0.5,0"
area=ET.SubElement(line,q("Folder")); ET.SubElement(area,q("name")).text="AREA"
pm2=ET.SubElement(area,q("Placemark")); ET.SubElement(pm2,q("name")).text="AREA A01"; poly=ET.SubElement(pm2,q("Polygon")); outer=ET.SubElement(poly,q("outerBoundaryIs")); ring=ET.SubElement(outer,q("LinearRing")); ET.SubElement(ring,q("coordinates")).text="0,0,0 1,0,0 1,1,0 0,1,0 0,0,0"
refs=collect_organizer_placemarks(root,[])
fdt_refs=collect_organizer_fdt_refs(refs,[])
fat_areas=collect_organizer_fat_areas(refs,[])
cables=collect_organizer_cable_refs(refs,fdt_refs,fat_areas)
fat_areas=collect_organizer_fat_areas(refs,cables)
print('fat_areas', [(a.fat_code,a.line_order,a.fdt_order) for a in fat_areas])
for r in refs:
    n=r.element.find(q('name'))
    if n is not None and 'AREA' in (n.text or ''):
        print('target', organizer_target_path(r,cables,fat_areas,fdt_refs,'AE'), 'name_after', n.text)
