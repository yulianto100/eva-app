"""One CAD sheet plus one multi-sheet workbook for a validated FDT batch."""
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import hashlib
import os
import subprocess

import ezdxf
from ezdxf import bbox, xref
from ezdxf.colors import rgb2int
from openpyxl import Workbook, load_workbook

from abd_cad import _validate, _workbook, CASES, SOURCE_SHA, generate
from inspect_dxf import inspect

# ponytail: native source, ten-FDT bound; recheck pitch/roundtrip before extending.
ODA = Path(os.environ.get('ABD_CAD_ODA', '/home/ubuntu/abd-cad-refine-investigation/compat/tools/squashfs-root/AppRun'))
PITCH = 3000.0
# Source OLE2FRAME 1738 declares this footprint; its payload is empty.
TABLE_WIDTH = 1100.670452710707
TABLE_HEIGHT = 322.8851292097242
TABLE_LAYER = 'ABD_PORT_TABLE'


def _draw_port_table(m, sheet, center_bottom):
    """Editable CAD schedule from the exact worksheet, source OLE footprint."""
    x = center_bottom[0] - TABLE_WIDTH / 2
    y = center_bottom[1]
    cell_w, cell_h = TABLE_WIDTH / 18, TABLE_HEIGHT / 14
    ox = x + TABLE_WIDTH
    oy = y + TABLE_HEIGHT
    colors = {'white': (255, 255, 255), 'yellow': (255, 224, 102),
              'orange': (248, 203, 173), 'green': (226, 239, 218)}
    def rectangle(x0, y0, x1, y1, rgb):
        hatch = m.add_hatch(dxfattribs={'layer': TABLE_LAYER})
        hatch.set_solid_fill(rgb=rgb)
        hatch.paths.add_polyline_path(((x0,y0),(x1,y0),(x1,y1),(x0,y1)), is_closed=True)
        return hatch
    rectangle(x, y, ox, oy, colors['white'])
    rectangle(x, oy-2*cell_h, ox, oy, colors['yellow'])
    for port in (*range(33, 37), *range(69, 73)):
        offset = port - 1
        col = (offset // 36)*9 + (offset % 3)*3
        row = 3 + (offset % 36)//3
        col_x = x + col*cell_w
        rectangle(col_x, oy-row*cell_h, col_x+3*cell_w,
                  oy-(row-1)*cell_h,
                  colors['orange' if port <= 36 else 'green'])
    grid_style = {'layer': TABLE_LAYER, 'color': 250, 'true_color': rgb2int((40, 40, 40))}
    for col in range(19):
        xx = x + col*cell_w
        first = oy-cell_h if col not in (0, 9, 18) else oy
        m.add_line((xx, y), (xx, first), dxfattribs=grid_style)
    for row in range(15):
        yy = oy-row*cell_h
        m.add_line((x,yy),(ox,yy),dxfattribs=grid_style)
    for row in sheet.iter_rows(min_row=1,max_row=14):
        for cell in row:
            if cell.value is None:
                continue
            col, line = cell.column-1, cell.row-1
            left, right = x + col*cell_w, x+(col+1)*cell_w
            if cell.row == 1:
                left, right = x+(col//9)*9*cell_w, x+(col//9+1)*9*cell_w
            cy = oy-(line+.5)*cell_h
            title = cell.row == 1
            value = str(cell.value)
            text = m.add_text(value, dxfattribs={
                'layer':TABLE_LAYER, 'height':9 if title else 5.5,
                'color':7, 'true_color':rgb2int((23,23,23)), 'style':'Standard',
                'insert':(left+2, cy)})
            # Plain left-baseline TEXT survives font substitutions better than
            # MIDDLE_CENTER: the latter shifted Front text over the Rear grid.
            box = bbox.extents([text])
            if box.size.x > right-left-4 or box.size.y > cell_h-4:
                raise ValueError(f'{sheet.title}: CAD table cell {cell.coordinate} overflows')
            text.translate((left+right-box.extmin.x-box.extmax.x)/2,
                           cy-(box.extmin.y+box.extmax.y)/2, 0)
            box = bbox.extents([text])
            if (box.extmin.x < left+2 or box.extmax.x > right-2 or
                    box.extmin.y < oy-(line+1)*cell_h+2 or
                    box.extmax.y > oy-line*cell_h-2):
                raise ValueError(f'{sheet.title}: CAD table cell {cell.coordinate} crosses grid')
    return (x,y,ox,oy)


def _oda(source, target, kind, executable, home):
    target.mkdir()
    environment = os.environ.copy()
    runtime = home / 'run'
    runtime.mkdir(exist_ok=True)
    runtime.chmod(0o700)
    environment.update(HOME=str(home), TMPDIR=str(home),
                       XDG_CACHE_HOME=str(home / 'cache'), XDG_CONFIG_HOME=str(home / 'config'),
                       XDG_DATA_HOME=str(home / 'data'), XDG_RUNTIME_DIR=str(runtime))
    run = subprocess.run(['xvfb-run', '-a', str(executable), str(source), str(target),
                          'ACAD2010', kind, '0', '0', '*.dxf' if kind == 'DWG' else '*.dwg'],
                         env=environment, capture_output=True, text=True, timeout=180)
    if run.returncode:
        raise ValueError(f'ODA {kind} conversion failed: {run.stderr[:1000]}')


def inspect_dwg(dwg_path, oda_path=ODA):
    """Independently reopen a delivered DWG; temporary DXF is never packaged."""
    dwg = Path(dwg_path).resolve()
    if not dwg.is_file() or not dwg.read_bytes().startswith(b'AC1024'):
        raise ValueError('Not a real R2010 DWG')
    with TemporaryDirectory(prefix='abd-dwg-reopen-') as folder:
        root = Path(folder)
        incoming = root / 'input'
        incoming.mkdir()
        (incoming / dwg.name).write_bytes(dwg.read_bytes())
        home = root / 'home'
        home.mkdir()
        output = root / 'reopened'
        _oda(incoming, output, 'DXF', Path(oda_path).resolve(), home)
        dxf = output / dwg.with_suffix('.dxf').name
        doc = ezdxf.readfile(dxf)
        audit = doc.audit()
        references = inspect(dxf)
        if audit.errors or audit.fixes or references['missing_references'] or references['duplicates']:
            raise ValueError('Delivered DWG failed independent CAD reopen')
        return {'types': Counter(e.dxftype() for e in doc.modelspace()),
                'entity_count': len(doc.modelspace()),
                'titles': [e.dxf.text for e in doc.modelspace().query('TEXT')],
                'roundtrip_bytes': dxf.read_bytes()}


def generate_combined(spec, template_path, out_dir, oda_path=ODA):
    """Validate the complete batch; return one real DWG and one multi-sheet XLSX."""
    items = _validate(spec)
    source = Path(template_path).resolve()
    output = Path(out_dir).resolve()
    if source.parent == output or source.parent in output.parents:
        raise ValueError('Cannot write into read-only source directory')
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError('Source SHA-256 mismatch')
    executable = Path(oda_path).resolve()
    if not executable.is_file():
        raise ValueError('ODA converter unavailable; cannot produce real DWG')
    if not output.parent.is_dir():
        raise ValueError('Output parent directory must exist')
    # Stage on the destination filesystem; publish only after a complete ZIP.
    with TemporaryDirectory(prefix='abd-combined-', dir=output.parent) as folder:
        temp = Path(folder)
        combined = None
        workbook = Workbook()
        title_locations = []
        right_edge = None
        expected_types = Counter()
        for index, item in enumerate(items):
            generated = temp / f'one-{index}'
            generate({'fdts': [item]}, source, generated)
            single = ezdxf.readfile(generated / f"{item['name']}.dxf")
            if single.audit().errors:
                raise ValueError('Individual FDT audit failed')
            original = list(single.modelspace())
            expected_types.update(e.dxftype() for e in original)
            bounds = bbox.extents(original, cache=bbox.Cache())
            if not bounds.has_data:
                raise ValueError('Empty FDT drawing')
            left, right = bounds.extmin.x + index * PITCH, bounds.extmax.x + index * PITCH
            if right_edge is not None and left <= right_edge + 50:
                raise ValueError('FDT layout overlap')
            right_edge = right
            if combined is None:
                combined = single
                sheet = combined.modelspace()
                title_locations.append((item['name'], next(e.dxf.insert for e in sheet.query('TEXT') if e.dxf.text == item['name'])))
            else:
                # Resource loader carries styles/layers/blocks; transform only copied entities.
                before = len(combined.modelspace())
                loader = xref.Loader(single, combined, conflict_policy=xref.ConflictPolicy.NUM_PREFIX)
                loader.load_modelspace()
                loader.execute()
                added = list(combined.modelspace())[before:]
                if len(added) != len(original):
                    raise ValueError('CAD transfer lost FDT entities')
                dx = index * PITCH
                for entity in added:
                    # xref NUM_PREFIX duplicates identically styled FO layers.
                    # Keep the measured source layer/color for every FDT.
                    layer = entity.dxf.layer
                    for native in ('FO_12_CORE', 'FO_24_CORE', 'FO_36_CORE', 'FO_48_CORE'):
                        if layer.endswith('$' + native):
                            imported, target = combined.layers.get(layer), combined.layers.get(native)
                            style = (imported.dxf.color, imported.dxf.get('true_color'))
                            current = (target.dxf.color, target.dxf.get('true_color'))
                            if style != current:
                                if any(e.dxf.layer == native for e in list(combined.modelspace())[:before]):
                                    raise ValueError('FO layer colors differ across FDTs')
                                target.dxf.color = imported.dxf.color
                                if imported.dxf.hasattr('true_color'):
                                    target.dxf.true_color = imported.dxf.true_color
                                else:
                                    target.dxf.discard('true_color')
                            entity.dxf.layer = native
                            break
                    entity.translate(dx, 0, 0)
                title_locations.append((item['name'], next(e.dxf.insert for e in added if e.dxftype() == 'TEXT' and e.dxf.text == item['name'])))
            name = item['name']
            _workbook(name, item['lines'], book=workbook)
            table = _draw_port_table(combined.modelspace(), workbook[name],
                                     (3887 + index * PITCH, 1158))
            if table[1] <= 1033.23 or table[3] >= 1567.15:
                raise ValueError(f'{name}: port table overlaps native drawing')
            expected_types.update(e.dxftype() for e in combined.modelspace()
                                  if e.dxf.layer == TABLE_LAYER
                                  and table[0] <= bbox.extents([e]).extmin.x <= table[2])
        assert combined is not None
        cad = temp / 'ABD_CAD.dxf'
        combined.saveas(cad)
        checked = ezdxf.readfile(cad)
        audit = checked.audit()
        refs = inspect(cad)
        if audit.errors or audit.fixes or refs['missing_references'] or refs['duplicates']:
            raise ValueError('Combined DXF invalid')
        actual_types = Counter(e.dxftype() for e in checked.modelspace())
        if actual_types != expected_types:
            raise ValueError('Combined DXF lost native entities')
        for name, at in title_locations:
            # FAT label can equal the FDT title (e.g. FDT "A10" has FAT A10).
            if sum(e.dxf.text == name and abs(e.dxf.insert.x - at.x) < 1e-4
                   and abs(e.dxf.insert.y - at.y) < 1e-4
                   for e in checked.modelspace().query('TEXT')) != 1:
                raise ValueError(f'Missing FDT title {name}')
        home = temp / 'oda-home'
        home.mkdir()
        src = temp / 'oda-src'
        src.mkdir()
        (src / cad.name).write_bytes(cad.read_bytes())
        dwgdir = temp / 'oda-dwg'
        _oda(src, dwgdir, 'DWG', executable, home)
        dwg = dwgdir / 'ABD_CAD.dwg'
        if not dwg.is_file() or not dwg.read_bytes().startswith(b'AC1024'):
            raise ValueError('ODA did not create a real DWG')
        recovered_dir = temp / 'oda-recovered'
        _oda(dwgdir, recovered_dir, 'DXF', executable, home)
        recovered = recovered_dir / 'ABD_CAD.dxf'
        if not recovered.is_file():
            raise ValueError('ODA did not reopen the DWG')
        reopened = ezdxf.readfile(recovered)
        verify = reopened.audit()
        check_refs = inspect(recovered)
        if verify.errors or verify.fixes or check_refs['missing_references'] or check_refs['duplicates'] or Counter(e.dxftype() for e in reopened.modelspace()) != actual_types:
            raise ValueError('Independent DWG roundtrip mismatch')
        xlsx = temp / 'ABD_CAD_port_mapping.xlsx'
        workbook.save(xlsx)
        book = load_workbook(xlsx)
        if book.sheetnames != [item['name'] for item in items]:
            raise ValueError('Multi-FDT workbook sheets mismatch')
        book.close()
        # Build the entire package before publishing the directory atomically.
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError('Output directory must be empty')
        package = temp / 'package'
        package.mkdir()
        for src_file, filename in ((dwg, 'ABD_CAD.dwg'), (xlsx, 'ABD_CAD_port_mapping.xlsx')):
            (package / filename).write_bytes(src_file.read_bytes())
        with ZipFile(package / 'ABD_CAD.zip', 'w') as bundle:
            bundle.write(package / 'ABD_CAD.dwg', 'ABD_CAD.dwg')
            bundle.write(package / 'ABD_CAD_port_mapping.xlsx', 'ABD_CAD_port_mapping.xlsx')
        if output.exists():
            output.rmdir()  # API provides an empty temporary directory.
        package.rename(output)
        return output / 'ABD_CAD.zip'
