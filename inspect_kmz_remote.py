import sys, zipfile, json
from lxml import etree
path=sys.argv[1]
with zipfile.ZipFile(path) as z:
    kml=[n for n in z.namelist() if n.lower().endswith('.kml')][0]
    tree=etree.parse(z.open(kml))
ns={'k':'http://www.opengis.net/kml/2.2'}
root=tree.getroot()

def lname(e): return etree.QName(e.tag).localname

def text(e):
    n=e.find('k:name',ns)
    return (n.text or '').strip() if n is not None else ''

def direct_pm_count(e): return sum(1 for c in e if lname(c)=='Placemark')

def walk(e, stack=[]):
    for c in e:
        if lname(c)=='Folder':
            n=text(c); st=stack+[n]
            if n in {'BOUNDARY FAT','FAT','SLACK HANGER','HP COVER','HP UNCOVER'} or n.startswith('LINE') or n.startswith('FDT') or 'BOUNDARY' in n or 'COVER' in n:
                print('/'.join(st), 'pm=', direct_pm_count(c))
            walk(c, st)
walk(root, [])

# Summary line folders with boundary/slack counts
print('--- SUMMARY ---')
def find_child_folder(folder,name):
    for c in folder:
        if lname(c)=='Folder' and text(c).upper()==name.upper(): return c
for f in root.xpath('.//*[local-name()="Folder"]'):
    n=text(f)
    if n.startswith('LINE'):
        bf=find_child_folder(f,'BOUNDARY FAT')
        fat=find_child_folder(f,'FAT')
        slack=find_child_folder(f,'SLACK HANGER')
        print(n, 'boundary_fat', direct_pm_count(bf) if bf is not None else None, 'fat', direct_pm_count(fat) if fat is not None else None, 'slack', direct_pm_count(slack) if slack is not None else None)
