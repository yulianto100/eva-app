"""ABD CAD from native source entities; cabinet optical schedule stays unverified."""
from pathlib import Path
from zipfile import ZipFile
from tempfile import TemporaryDirectory
import hashlib
import json
import math
import re

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44

if __package__:
    from .native_wire_adapter import native_upper, THETA
    from .native_trim import trim_bank_tails
    from .export_compatible import export_compatible
else:
    from native_wire_adapter import native_upper, THETA
    from native_trim import trim_bank_tails
    from export_compatible import export_compatible
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

SOURCE_SHA = '8efd44545bcda71ba9c77454e804cc24895299854f1bed6fe2181d644b8de675'


def _workbook(name, lines, book=None):
    book = book if book is not None else Workbook()
    fats = [f"{line['name']}{i:02}" for line in lines for i in range(1, line['fat_count'] + 1)]
    active_splitters = math.ceil(len(fats) / 8)
    sheet = book.active if len(book.sheetnames) == 1 and book.active.title == 'Sheet' else book.create_sheet(name)
    sheet.title = name
    sheet.sheet_view.showGridLines = False
    side = Side(style='thin', color='9A9A9A')
    border = Border(left=side, right=side, top=side, bottom=side)
    for plate in (1, 2):
        start = 1 if plate == 1 else 10
        sheet.merge_cells(start_row=1, start_column=start, end_row=1, end_column=start + 8)
        title = sheet.cell(1, start, f'PLATE-{plate}')
        title.font = Font(name='Arial', size=12, bold=True, color='171717')
        title.alignment = Alignment(horizontal='center', vertical='center')
        for col in range(start, start + 9):
            sheet.column_dimensions[get_column_letter(col)].width = (6, 18, 12)[(col-start) % 3]
            for row in (1, 2):
                cell = sheet.cell(row, col)
                cell.fill = PatternFill('solid', fgColor='FFE066')
                cell.border = border
            header = sheet.cell(2, col, ('Port', 'Front', 'Rear')[(col-start) % 3])
            header.font = Font(name='Arial', size=10, bold=True, color='171717')
            header.alignment = Alignment(horizontal='center', vertical='center')
        for offset in range(36):
            port = (plate - 1) * 36 + offset + 1
            if offset < 32:
                ordinal = (plate - 1) * 32 + offset
                active = ordinal < len(fats)
                splitter = 1 + ordinal // 8
                label = f'SP{splitter}{"*" if plate == 2 else ""}'
                front = f'{label} OUT{ordinal % 8 + 1}' if active else None
                rear = fats[ordinal] if active else None
            else:
                splitter = (plate - 1) * 4 + offset - 31
                active = splitter <= active_splitters
                front = f'SP{splitter}{"*" if plate == 2 else ""}' if active else None
                rear = f'Core-{splitter}{"*" if plate == 2 else ""}' if active else None
            values = (port, front, rear)
            for k, value in enumerate(values):
                cell = sheet.cell(3 + offset // 3, start + (offset % 3) * 3 + k, value)
                cell.fill = PatternFill('solid', fgColor='F8CBAD' if 33 <= port <= 36 else 'E2EFDA' if 69 <= port <= 72 else 'FFFFFF')
                cell.font = Font(name='Arial', size=9, color='171717')
                cell.alignment = Alignment(horizontal='center', vertical='center')
                cell.border = border
    sheet.row_dimensions[1].height = 23
    sheet.row_dimensions[2].height = 20
    for row in range(3, 15):
        sheet.row_dimensions[row].height = 20
    sheet.print_area = 'A1:R14'
    sheet.print_options.horizontalCentered = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 1
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    return book


CASES = {'PPI.010': (18, 15, 48, 48, 3000, 5000, 2580),
         'PPI.011': (16, 13, 48, 48, 5600, 7100, 2520),
         'PPI.012': (18, 10, 48, 24, 7800, 9300, 2428)}


def _validate(spec):
    if not isinstance(spec, dict) or set(spec) != {'fdts'} or not isinstance(spec['fdts'], list) or not 1 <= len(spec['fdts']) <= 10:
        raise ValueError('Expected 1-10 FDT per batch')
    seen = set()
    for item in spec['fdts']:
        if not isinstance(item, dict) or set(item) != {'name', 'lines'} or not isinstance(item['name'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,30}', item['name']):
            raise ValueError('FDT name must be 1-31 safe filename/worksheet characters')
        if item['name'].casefold() in seen:
            raise ValueError('Duplicate FDT name')
        seen.add(item['name'].casefold())
        lines = item['lines']
        if not isinstance(lines, list) or not 1 <= len(lines) <= 5 or any(not isinstance(line, dict) or set(line) != {'name', 'fat_count', 'cable_cores'} for line in lines) or [line['name'] for line in lines] != list('ABCDE'[:len(lines)]):
            raise ValueError('Expected ordered A..E lines with fat_count and cable_cores')
        for line in lines:
            fat, cores = line['fat_count'], line['cable_cores']
            # ponytail: native cable assembly has four 12-core tubes;
            # add an audited wider source assembly before accepting >48 cores.
            if type(fat) is not int or fat < 0 or type(cores) is not int or cores not in (12, 24, 36, 48):
                raise ValueError('FAT must be nonnegative; cable_cores must be 12, 24, 36 or 48')
            if fat > cores * 5 // 12:
                raise ValueError(f"Line {line['name']} exceeds source-observed five visual F labels per 12 cores")

        total = sum(line['fat_count'] for line in lines)
        if not 1 <= total <= 36:
            raise ValueError('FAT total must be 1-36 within the fixed cabinet')
        # Operator-approved upper routing: G/output continues across A..E;
        # each bank reuses A/B core/tube motifs. Cabinet Front/Rear stays unassigned.
    return spec['fdts']


def _lower_hatch(m, template):
    """Copy a source full-fill polygon per occupied service/splitter slot."""
    labels = {int(e.dxf.text[3:]): e for e in m.query('TEXT') if re.fullmatch(r'OUT\d{2}', e.dxf.text)}
    if set(labels) != set(range(1, 73)):
        raise ValueError('Missing fixed lower hardware ports')
    source_label = template.entitydb['169A'].dxf.insert
    original_fill = template.entitydb['1693']
    polygon = original_fill.paths[0].vertices
    # The same native HATCH includes 20 active fills AND 36 fixed thin strips.
    # Clone its audited strip paths; replacing all green fills drops this detail.
    fixed_strips = original_fill.copy()
    fixed_strips.paths.paths = fixed_strips.paths.paths[20:]
    fixed_strips.remove_association()
    m.add_entity(fixed_strips)
    return labels, source_label, polygon


def _fill(m, labels, source_label, polygon, total):
    g = math.ceil(total / 8)
    slots = list(range(1, min(total, 32) + 1)) + list(range(37, 37 + max(0, total-32)))
    slots += list(range(33, 33 + min(g, 4))) + list(range(69, 69 + max(0, g-4)))
    assert len(slots) == total + g and len(set(slots)) == len(slots)
    for slot in slots:
        at = labels[slot].dxf.insert
        delta_x, delta_y = at.x-source_label.x, at.y-source_label.y
        hatch = m.add_hatch(color=3, dxfattribs={'layer': 'Service Core'})
        hatch.set_solid_fill(color=3)
        hatch.paths.add_polyline_path([(x+delta_x, y+delta_y) for x,y,*_ in polygon], is_closed=True)
    return slots


def generate(spec, template_path, out_dir):
    """Generate one DXF/XLSX per FDT in one ZIP; custom mapping deliberately unassigned."""
    items = _validate(spec)  # fail before creating files
    source, dest = Path(template_path).resolve(), Path(out_dir).resolve()
    if source.parent == dest or source.parent in dest.parents:
        raise ValueError('Cannot write into read-only source directory')
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError('Source SHA-256 mismatch')
    files = []
    for item in items:
        name, lines = item['name'], item['lines']
        configuration = (lines[0]['fat_count'], lines[1]['fat_count'], lines[0]['cable_cores'], lines[1]['cable_cores']) if len(lines) == 2 else ()
        source_name = next((label for label, case in CASES.items() if case[:4] == configuration), None)
        lo, hi = 3000, 5000
        doc = ezdxf.readfile(source)
        if doc.dxfversion != 'AC1032' or doc.units != 4 or len(doc.modelspace()) != 7537:
            raise ValueError('Source CAD version/geometry mismatch')
        template = ezdxf.readfile(source)
        m = doc.modelspace()
        cache = bbox.Cache()
        for entity in list(m):
            box = bbox.extents([entity], cache=cache) if entity.dxftype() not in ('OLE2FRAME','VIEWPORT') else None
            keep = box is not None and box.has_data and lo <= box.extmin.x and box.extmax.x < hi
            keep = keep and box.extmax.y < 1300 and (entity.dxftype() != 'HATCH' or entity.dxf.layer not in ('Service Core','LABEL CABLE'))
            if not keep:
                m.delete_entity(entity)
        doc.blocks.delete_block('A$C48fed096')
        labels, source_label, polygon = _lower_hatch(m, template)
        _fill(m, labels, source_label, polygon, sum(line['fat_count'] for line in lines))
        # Match EVA's standardized Distribution Cable styles, not the source
        # ABD's 48C magenta layer inherited by every capacity.
        from ezdxf.colors import rgb2int
        from make_cluster_template import CLUSTER_SHA256
        cluster_path = Path(__file__).with_name('Template ABD Cluster.dxf')
        manifest = json.loads(cluster_path.with_suffix('.manifest.json').read_text())
        if manifest['source_sha256'] != CLUSTER_SHA256 or hashlib.sha256(cluster_path.read_bytes()).hexdigest() != manifest['template_sha256']:
            raise ValueError('Cluster FO cable style provenance mismatch')
        cluster = ezdxf.readfile(cluster_path)
        cable_rgb = {12: (0, 170, 255), 24: (0, 255, 0),
                     36: (255, 0, 255), 48: (170, 0, 255)}
        for core in {line['cable_cores'] for line in lines}:
            source_layer = cluster.layers.get(f'FO_{core}_CORE')
            layer = doc.layers.get(source_layer.dxf.name) if source_layer.dxf.name in doc.layers else doc.layers.new(source_layer.dxf.name)
            layer.dxf.color = source_layer.dxf.color
            layer.dxf.true_color = rgb2int(cable_rgb[core])
        route_report = native_upper(m, item, source)
        protected = {handle for route in route_report['fat_routes'] for handle in route['output_route_handles']}
        trim_bank_tails(m, protected_handles=protected)
        for route in route_report['fat_routes']:
            wire = doc.entitydb[route['output_route_handles'][0]]
            start, end = list(wire.get_points('xy'))[0], list(wire.get_points('xy'))[-1]
            if math.dist(start, route['g_endpoint'][:2]) > 1e-5 or math.dist(end, route['fat_endpoint'][:2]) > 1e-5:
                raise ValueError(f"{name}: trimmed service route {route['fat']} lost its terminal")
        # Orthomode must be orthogonal in DWG world coordinates, not only
        # the sample's slightly rotated (0.593-degree) local coordinate frame.
        for entity in m:
            entity.transform(Matrix44.z_rotate(-THETA))
        if doc.audit().errors:
            raise ValueError(f'{name}: DXF audit failed')
        dest.mkdir(parents=True, exist_ok=True)
        cad, xlsx = dest / f'{name}.dxf', dest / f'{name}_port_mapping_single_sheet.xlsx'
        with TemporaryDirectory(prefix="abd-cad-export-") as tmp:
            raw = Path(tmp) / "native.dxf"
            doc.saveas(raw)
            export_compatible(raw, cad)
        book = _workbook(name, lines)
        book.save(xlsx)
        files.extend((cad,xlsx))
    path = dest / 'abd_cad.zip'
    with ZipFile(path, 'w') as archive:
        for file in files:
            archive.write(file, file.name)
    return path


def generate_combined(spec, template_path, out_dir, oda_path=None):
    """One real DWG plus one workbook for all validated FDTs."""
    from combined_batch import generate_combined as build
    return build(spec, template_path, out_dir, oda_path=oda_path) if oda_path else build(spec, template_path, out_dir)
