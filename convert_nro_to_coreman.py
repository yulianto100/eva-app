from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

NRO_PATH = Path(r"C:/Users/Employee/Downloads/MANAJEMEN CORE NRO BEKASI.xlsx")
OUTPUT_PATH = Path(r"C:/Users/Employee/Downloads/Coreman_Ops_Dashboard_Bekasi.xlsx")
FDT_DB_PATH = Path(r"D:\Master-app\converter\eva-converter\data\fdt_coding.db")

TUBE_COLOUR_MAP = {
    1: "Blue", 2: "Orange", 3: "Green", 4: "Brown", 5: "Slate",
    6: "White", 7: "Red", 8: "Black", 9: "Yellow", 10: "Violet",
    11: "Pink", 12: "Cyan",
}
HUB_TUBE_COLOUR = {
    1: "Biru", 2: "Orange", 3: "Hijau", 4: "Cokelat", 5: "Abu-abu",
    6: "Putih", 7: "Merah", 8: "Hitam", 9: "Kuning", 10: "Ungu",
    11: "Pink", 12: "Cyan",
}
COLOUR_TO_TUBE = {
    "BIRU": 1, "BLUE": 1, "ORANGE": 2, "OREN": 2, "ORANYE": 2,
    "HIJAU": 3, "GREEN": 3, "COKLAT": 4, "COKELAT": 4, "BROWN": 4,
    "ABU-ABU": 5, "ABU ABU": 5, "ABUABU": 5, "GREY": 5, "GRAY": 5, "SLATE": 5,
    "PUTIH": 6, "WHITE": 6, "MERAH": 7, "RED": 7,
    "HITAM": 8, "BLACK": 8, "KUNING": 9, "YELLOW": 9,
    "UNGU": 10, "VIOLET": 10, "PINK": 11, "ROSE": 11,
    "CYAN": 12, "TOSCA": 12, "TOSKA": 12, "AQUA": 12,
}
OLT_ALIASES = {
    "CKB": "CIKARANG BARAT",
    "OLT CKB": "CIKARANG BARAT",
    "CKU": "CIKARANG UTARA",
    "OLT CKU": "CIKARANG UTARA",
    "JATIASIH": "JATI ASIH",
    "OLT JATIASIH": "JATI ASIH",
    "NORT BEKASI": "NORTH BEKASI",
    "NORTH": "NORTH BEKASI",
    "OLT NORTH": "NORTH BEKASI",
    "SEGERA CITY": "SEGARA CITY",
    "OLT SEGERA CITY": "SEGARA CITY",
    "GRITA METROPOLITAN": "GRIYA METROPOLITAN",
    "LAMBA": "LAMBANG",
    "LAMBANG JAYA": "LAMBANG JAYA",
    "OLT LAMBANG": "LAMBANG",
    "LIPPO": "LIPPO CIKARANG",
    "OLT LIPPO": "LIPPO CIKARANG",
    "NAROGONG": "TAMAN NAROGONG",
    "OLT NAROGONG": "TAMAN NAROGONG",
    "PEKAYON": "PEKAYON JAYA",
    "SUKURKUN": "SUKARUKUN",
    "OLT SUKURKUN": "SUKARUKUN",
}
# Map normalized OLT name to db olt_initial
OLT_TO_DB = {
    "BABELAN": "OLT BABELAN",
    "BINTARA": "OLT BINTARA",
    "CAKUNG": "OLT CAKUNG",
    "CIBARUSAH": "OLT CIBARUSAH",
    "CIBARUSAH KOTA": "OLT CIBARUSAH KOTA",
    "CIKARANG BARAT": "OLT CIKARANG BARAT",
    "CIKARANG UTARA": "OLT CIKARANG UTARA 2",
    "DUREN JAYA": "OLT DUREN JAYA",
    "GRIYA METROPOLITAN": "OLT PEKAYON",
    "HARAPAN JAYA": "OLT HARAPAN JAYA",
    "JATI ASIH": "OLT JATIASIH",
    "JATIWARINGIN": "OLT JATIWARINGIN",
    "JAYASAMPURNA": "OLT JAYASAMPURNA",
    "KEDUNGWARINGIN": "OLT KEDUNGWARINGIN",
    "LAMBANG": "OLT LAMBANG JAYA",
    "LAMBANG JAYA": "OLT LAMBANG JAYA",
    "LIPPO CIKARANG": "OLT LIPPO CIKARANG",
    "MEDAN SATRIA": "OLT MEDAN SATRIA",
    "NORTH BEKASI": "OLT NORTH BEKASI",
    "PEKAYON JAYA": "OLT PEKAYON JAYA",
    "PPI": "OLT PPI",
    "SEGARA CITY": "OLT SEGARA CITY",
    "SETU": "OLT SETU",
    "SUKARUKUN": "OLT SUKARUKUN",
    "TAMAN NAROGONG": "OLT TAMAN NAROGONG",
    "TAMAN NAROGONG": "OLT TAMAN NAROGONG",
}
LINE_TO_SEGMENT = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5,
    "F": 6, "G": 7, "H": 8, "I": 9, "J": 10,
}
HEADER_FILL = PatternFill(start_color="002060", end_color="002060", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=9, name="Calibri")
SUB_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
SUB_FONT = Font(color="FFFFFF", bold=True, size=8, name="Calibri")
DATA_FONT = Font(size=8, name="Calibri")
BOLD_FONT = Font(bold=True, size=8, name="Calibri")
ACTIVE_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
IDLE_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
ASSIGNED_FILL = PatternFill(start_color="D9E2F3", end_color="D9E2F3", fill_type="solid")
THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def normalize_olt(name):
    raw = " ".join(str(name or "").strip().upper().split())
    if raw.startswith("OLT "):
        raw = raw[4:]
    return OLT_ALIASES.get(raw, raw)


def norm_text(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper().strip())


def tube_num_from_colour(colour):
    return COLOUR_TO_TUBE.get(str(colour or "").strip().upper())


def seg_num(seg):
    m = re.search(r"(\d+)", str(seg or ""))
    return int(m.group(1)) if m else 0


def core_global(tube, core):
    return (tube - 1) * 12 + core


def tube_core(g):
    return (g - 1) // 12 + 1, (g - 1) % 12 + 1


def parse_tube(text):
    m = re.match(r"Tube\s+(\d+)\s*-\s*(.+)", str(text or "").strip(), re.I)
    if m:
        return int(m.group(1)), m.group(2).strip()
    m = re.match(r"Tube\s+(\d+)", str(text or "").strip(), re.I)
    if m:
        return int(m.group(1)), ""
    return None, ""


def parse_cores(text):
    results = []
    for part in re.split(r",\s*", str(text or "").strip()):
        m = re.match(r"Core\s+(\d+)\s*-\s*(.+)", part.strip(), re.I)
        if m:
            results.append((int(m.group(1)), m.group(2).strip()))
    return results


def normalize_segment(seg):
    text = str(seg or "").strip()
    text = re.sub(r"(?i)^segmen\w*\b", "Segment", text)
    text = re.sub(r"(?i)^segment\s*", "Segment ", text)
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(\d+)", text)
    return f"Segment {m.group(1)}" if m else text


def classify_line_to_segment(line_text):
    text = str(line_text or "").strip()
    if not text:
        return 0
    m = re.match(r"(?i)seg[me]*n?t?\w*\s*(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.match(r"^([A-Z])$", text.strip().upper())
    if m:
        return LINE_TO_SEGMENT.get(m.group(1), 0)
    m = re.match(r"^(\d+)(?:\.0)?$", text.strip())
    if m:
        return int(m.group(1))
    return 0


def load_fdt_db(db_path):
    """Load FDT coding records grouped by olt_initial.
    Returns: {olt_initial: {norm_site_name: (site_name, fdt_code, fdt_id)}}
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT olt_initial, site_name, fdt_code, fdt_id FROM fdt_coding_records WHERE site_name IS NOT NULL AND site_name != ''"
    ).fetchall()
    conn.close()

    by_olt = defaultdict(list)
    for r in rows:
        olt = r["olt_initial"].strip()
        site = r["site_name"].strip()
        code = r["fdt_code"].strip()
        fdt_id = str(r["fdt_id"] or "").strip()
        by_olt[olt].append((site, code, fdt_id))

    # Build normalized lookup per OLT
    lookup = {}
    for olt, entries in by_olt.items():
        norm_map = {}
        for site, code, fdt_id in entries:
            nk = norm_text(site)
            if nk not in norm_map:
                norm_map[nk] = (site, code, fdt_id)
        lookup[olt] = norm_map
    return by_olt, lookup


def match_cluster(olt_lookup, olt_initial, cluster_name):
    """Try to match cluster name to FDT coding db. Returns (full_site_name, fdt_code) or None."""
    norm_map = olt_lookup.get(olt_initial, {})
    if not norm_map:
        return None
    nc = norm_text(cluster_name)
    # Exact match
    if nc in norm_map:
        site, code, fid = norm_map[nc]
        return (site, code)
    # Substring match
    for nk, (site, code, fid) in norm_map.items():
        if nc in nk or nk in nc:
            return (site, code)
    # Fuzzy match (similarity > 0.7)
    best_score = 0.0
    best_match = None
    for nk, (site, code, fid) in norm_map.items():
        score = SequenceMatcher(None, nc, nk).ratio()
        if score > best_score:
            best_score = score
            best_match = (site, code)
    if best_score >= 0.7:
        return best_match
    return None


def read_form(path):
    wb = load_workbook(path, data_only=True)
    ws = wb["Form Mancore Bekasi"]
    entries = []
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=False):
        olt = str(row[1].value or "").strip()
        if not olt:
            continue
        segment = normalize_segment(str(row[2].value or "").strip())
        cluster = str(row[3].value or "").strip()
        coords = str(row[4].value or "").strip()
        tube_text = str(row[5].value or "").strip()
        active_text = str(row[6].value or "").strip()
        idle_text = str(row[7].value or "").strip()
        olt2 = str(row[8].value or "").strip()
        slot_olt = row[9].value
        port_olt = row[10].value
        tube_num, tube_col = parse_tube(tube_text)
        entries.append(dict(
            olt=olt, segment=segment, cluster=cluster, coords=coords,
            tube_num=tube_num, tube_colour=tube_col,
            active_cores=parse_cores(active_text),
            idle_cores=parse_cores(idle_text),
            olt2=olt2, slot_olt=slot_olt, port_olt=port_olt,
        ))
    wb.close()
    return entries


def read_all_olt(path):
    wb = load_workbook(path, data_only=True)
    ws = wb["ALL OLT"]
    entries = []
    cur_no = cur_cluster = cur_olt = cur_otb = cur_rak = cur_line = None
    for row in ws.iter_rows(min_row=5, max_row=ws.max_row, values_only=False):
        a = row[0].value
        b = row[1].value
        c = row[2].value
        d = row[3].value
        e = row[4].value
        g = row[6].value
        h = row[7].value
        u = row[20].value
        v = row[21].value
        if a is not None: cur_no = a
        if b is not None: cur_cluster = str(b).strip()
        if c is not None: cur_olt = str(c).strip()
        if d is not None: cur_otb = d
        if e is not None: cur_rak = e
        if g is not None: cur_line = str(g).strip()
        tube_colour = str(h or "").strip()
        if tube_colour and cur_cluster and cur_olt:
            tube_num = tube_num_from_colour(tube_colour)
            entries.append(dict(
                no=cur_no, cluster=cur_cluster, olt=cur_olt,
                otb=cur_otb, rak=cur_rak, line=cur_line,
                tube_colour=tube_colour, tube_num=tube_num,
                remark=str(u or "").strip() if u else "",
                distance=v,
            ))
    wb.close()
    return entries


def create_sheet(wb, name):
    ws = wb.create_sheet(title=name)
    ws.merge_cells("A1:D1")
    ws["A1"] = "OLT SITE"
    ws.merge_cells("E1:G1")
    ws["E1"] = "ODF/OTB"
    ws.merge_cells("H1:W1")
    ws["H1"] = "Feeder and Sub-Feeder Cable Mapping"
    ws.merge_cells("X1:AC1")
    ws["X1"] = "FDT Input Splicing and Termination Module"
    headers = [
        "OLT SITE", "OLT", "Slot No.", "Port No.", "ODF No.", "Tray No.", "Port No.",
        "Closure", "Main Feeder", "Tube", "Tube", "Fiber",
        "Closure", "Hub feeder", "Tube", "Tube", "Fiber",
        "Closure", "2nd Sub feeder", "Tube", "Tube", "Fiber",
        "OLT-FDT", "Cluster Name", "FDT ID", "HP",
        "Tray No.", "Port No.", "Splitter Module No.",
    ]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=2, column=i, value=h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER
        c.border = THIN_BORDER
    subs = {
        8: "(Distance)", 9: "Cable Type", 10: "Number", 11: "Colour", 12: "Core No.",
        13: "(Distance)", 14: "Cable Type", 15: "Number", 16: "Colour", 17: "Core No.",
        18: "(Distance)", 19: "Cable Type", 20: "Number", 21: "Colour", 22: "Core No.",
        23: "Length",
    }
    for ci, h in subs.items():
        c = ws.cell(row=3, column=ci, value=h)
        c.fill = SUB_FILL
        c.font = SUB_FONT
        c.alignment = CENTER
        c.border = THIN_BORDER
    for ci in range(1, 30):
        c = ws.cell(row=1, column=ci)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER
        c.border = THIN_BORDER
    widths = {
        "A": 20, "B": 18, "C": 8, "D": 8, "E": 8, "F": 8, "G": 8,
        "H": 14, "I": 12, "J": 8, "K": 10, "L": 8, "M": 14,
        "N": 12, "O": 8, "P": 10, "Q": 8, "R": 14,
        "S": 14, "T": 8, "U": 10, "V": 8, "W": 10,
        "X": 30, "Y": 20, "Z": 8, "AA": 8, "AB": 8, "AC": 14,
    }
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 25
    ws.row_dimensions[3].height = 18
    return ws


def build_dashboard(form_entries, all_olt_entries, fdt_lookup):
    wb = Workbook()
    wb.remove(wb.active)

    all_olt_groups = defaultdict(list)
    for e in all_olt_entries:
        olt_norm = normalize_olt(e["olt"])
        seg = classify_line_to_segment(e["line"])
        if seg == 0:
            seg = 1
        all_olt_groups[(olt_norm, seg)].append(e)

    form_groups = defaultdict(list)
    for e in form_entries:
        olt_norm = normalize_olt(e["olt"])
        seg = seg_num(e["segment"])
        if seg == 0:
            seg = 1
        form_groups[(olt_norm, seg)].append(e)

    all_keys = set(all_olt_groups.keys()) | set(form_groups.keys())
    sorted_keys = sorted(all_keys)

    matched_count = 0
    unmatched_count = 0

    for key in sorted_keys:
        olt_norm, seg = key
        olt_all = all_olt_groups.get(key, [])
        olt_form = form_groups.get(key, [])
        if not olt_all and not olt_form:
            continue

        olt_name = olt_all[0]["olt"] if olt_all else olt_form[0]["olt"]
        sname = f"OLT {olt_norm} - SEG {seg}"
        if len(sname) > 31:
            sname = sname[:31]

        print(f"  {sname} ({len(olt_all)} ALL_OLT + {len(olt_form)} form)")
        ws = create_sheet(wb, sname)

        db_olt_initial = OLT_TO_DB.get(olt_norm, "")

        core_assignments = {}

        # Process ALL OLT entries with FDT lookup
        for e in olt_all:
            tn = e.get("tube_num")
            if tn is None:
                continue
            cluster_raw = e["cluster"]
            # Try FDT db lookup
            result = match_cluster(fdt_lookup, db_olt_initial, cluster_raw) if db_olt_initial else None
            if result:
                cluster_name, fdt_code = result
                matched_count += 1
            else:
                cluster_name = cluster_raw
                fdt_code = ""
                unmatched_count += 1
            for cn in range(1, 13):
                gc = core_global(tn, cn)
                core_assignments[gc] = dict(
                    cluster=cluster_name, status="ASSIGNED",
                    fdt_code=fdt_code, source="all_olt",
                )

        # Overlay Form entries
        for e in olt_form:
            tn = e["tube_num"]
            if tn is None:
                continue
            cluster_raw = e["cluster"]
            result = match_cluster(fdt_lookup, db_olt_initial, cluster_raw) if db_olt_initial else None
            if result:
                cluster_name, fdt_code = result
                matched_count += 1
            else:
                cluster_name = cluster_raw
                fdt_code = ""
                unmatched_count += 1
            for cn, _ in e["active_cores"]:
                gc = core_global(tn, cn)
                core_assignments[gc] = dict(
                    cluster=cluster_name, status="ACTIVE",
                    fdt_code=fdt_code, source="form",
                )
            for cn, _ in e["idle_cores"]:
                gc = core_global(tn, cn)
                core_assignments[gc] = dict(
                    cluster=cluster_name, status="IDLE",
                    fdt_code=fdt_code, source="form",
                )

        # Fill 144 rows
        cur_slot = 1
        cur_port = 1
        for gc in range(1, 145):
            tn, ct = tube_core(gc)
            ri = 3 + gc
            if ct == 1 and gc > 1:
                cur_port += 1
                if cur_port > 12:
                    cur_slot += 1
                    cur_port = 1
            if gc == 1:
                ws.cell(row=ri, column=1, value=olt_norm).font = BOLD_FONT
            if ct == 1:
                ws.cell(row=ri, column=2, value=cur_slot)
                ws.cell(row=ri, column=3, value=cur_slot)
                ws.cell(row=ri, column=4, value=cur_port)
            ws.cell(row=ri, column=5, value=1)
            ws.cell(row=ri, column=6, value=tn)
            ws.cell(row=ri, column=7, value=ct)
            ws.cell(row=ri, column=9, value="144/12T")
            ws.cell(row=ri, column=10, value=tn)
            ws.cell(row=ri, column=11, value=TUBE_COLOUR_MAP.get(tn, ""))
            ws.cell(row=ri, column=12, value=gc)
            ws.cell(row=ri, column=14, value="144/12T")
            ws.cell(row=ri, column=15, value=tn)
            ws.cell(row=ri, column=16, value=HUB_TUBE_COLOUR.get(tn, ""))
            ws.cell(row=ri, column=17, value=ct)

            a = core_assignments.get(gc)
            if a:
                status = a["status"]
                ws.cell(row=ri, column=19, value="AKTIF" if status == "ACTIVE" else "IDLE" if status == "IDLE" else "ASSIGNED")
                ws.cell(row=ri, column=24, value=a["cluster"])
                if a.get("fdt_code"):
                    ws.cell(row=ri, column=25, value=a["fdt_code"])
                ws.cell(row=ri, column=27, value=1)
                ws.cell(row=ri, column=28, value=ct)
                ws.cell(row=ri, column=29, value=1)
                fill = ACTIVE_FILL if status == "ACTIVE" else IDLE_FILL if status == "IDLE" else ASSIGNED_FILL
                for cc in range(24, 30):
                    ws.cell(row=ri, column=cc).fill = fill

            for cc in range(1, 30):
                cell = ws.cell(row=ri, column=cc)
                cell.font = DATA_FONT
                cell.alignment = CENTER
                cell.border = THIN_BORDER

    print(f"\nFDT matching: {matched_count} matched, {unmatched_count} unmatched")
    return wb


def main():
    print("Loading FDT coding database...")
    fdt_by_olt, fdt_lookup = load_fdt_db(FDT_DB_PATH)
    print(f"  {len(fdt_by_olt)} OLTs, {sum(len(v) for v in fdt_by_olt.values())} records")

    print("Reading Form Mancore Bekasi...")
    form_entries = read_form(NRO_PATH)
    print(f"  {len(form_entries)} form entries")

    print("Reading ALL OLT...")
    all_olt_entries = read_all_olt(NRO_PATH)
    print(f"  {len(all_olt_entries)} ALL OLT entries")

    print("\nBuilding dashboard...")
    wb = build_dashboard(form_entries, all_olt_entries, fdt_lookup)

    print(f"\nSaving to {OUTPUT_PATH}...")
    wb.save(OUTPUT_PATH)
    print(f"Done! {len(wb.sheetnames)} sheets")


if __name__ == "__main__":
    main()

