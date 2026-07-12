from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import coreman_sheets
import fdt_coding_db
from config import Config

COREMAN_GRID_COLUMNS = {
    "hub_tube": 14,
    "hub_core": 16,
    "assignment_status": 18,
    "b_cluster": 23,
    "fdt_id": 24,
}


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def _upper(value: object) -> str:
    return _clean(value).upper()


def _expand_numbers(text: object) -> list[int]:
    result: list[int] = []
    for piece in re.split(r"[,.]", str(text or "")):
        piece = piece.strip()
        if not piece:
            continue
        m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", piece)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            step = 1 if start <= end else -1
            result.extend(range(start, end + step, step))
        else:
            try:
                result.append(int(piece))
            except ValueError:
                pass
    seen: set[int] = set()
    out: list[int] = []
    for n in result:
        if 1 <= n <= 12 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _status_from_word(word: object) -> str:
    upper = _upper(word)
    if upper in {"AKTIF", "TAKEN", "ACTIVE"}:
        return "TAKEN"
    if upper == "IDLE":
        return "IDLE"
    return upper or "TAKEN"


def _parse_tube_line(line: str) -> list[dict[str, Any]]:
    m = re.search(r"TUBE\s*(\d+)\s*CORE\s*(.+)$", line, flags=re.I)
    if not m:
        return []
    tube = int(m.group(1))
    if tube < 1 or tube > 12:
        return []
    body = m.group(2).strip()
    fat_match = re.search(r"\((\d+)\s*FAT\)", body, flags=re.I)
    fat_count = int(fat_match.group(1)) if fat_match else None
    body = re.sub(r"\([^)]*FAT\)", "", body, flags=re.I).strip()
    entries: list[dict[str, Any]] = []

    def add(numbers_text: str, status: str) -> None:
        for core in _expand_numbers(numbers_text.strip(" ,")):
            entries.append({"tube": tube, "core": core, "status": status, "fat_count": fat_count})

    status = "TAKEN"
    cursor = 0
    for sm in re.finditer(r"\b(IDLE|AKTIF|TAKEN|ACTIVE)\b", body, flags=re.I):
        add(body[cursor:sm.start()], status)
        status = _status_from_word(sm.group(1))
        cursor = sm.end()
    add(body[cursor:], status)
    return entries


def _parse_block(lines: list[str], b_olt: str, segment_name: str) -> dict[str, Any] | None:
    cluster = ""
    entries: list[dict[str, Any]] = []
    for line in lines:
        cleaned = _clean(line)
        if not cleaned:
            continue
        if re.search(r"TUBE\s*\d+\s*CORE", cleaned, flags=re.I):
            entries.extend(_parse_tube_line(cleaned))
            continue
        if re.match(r"^OLT\b", cleaned, flags=re.I):
            continue
        cluster = _upper(cleaned)
    if not cluster or not entries:
        return None
    return {
        "b_cluster": cluster,
        "b_olt": b_olt,
        "segment_name": segment_name,
        "core_entries": entries,
    }


def parse_mancore(text: str) -> dict[str, Any]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Data mancore wajib diisi")
    upper = normalized.upper()
    olt_match = re.search(r"\bOLT\s+(.+?)(?=\s+SEG(?:MENT)?\b|\n|,|$)", upper, flags=re.I)
    segment_match = re.search(r"\b(?:SEG|SEGMENT|MF\s+SEG)\s*0*(\d+)\b", upper, flags=re.I)
    b_olt = f"OLT {_clean(olt_match.group(1)).upper()}" if olt_match else ""
    segment_name = str(int(segment_match.group(1))) if segment_match else "1"

    lines = normalized.split("\n")
    blocks: list[dict[str, Any]] = []
    current: list[str] = []

    for line in lines:
        cleaned = _clean(line)
        if not cleaned:
            continue
        is_fdt_header = bool(re.search(r"FDT\s+\d+", cleaned, flags=re.I))
        if is_fdt_header:
            if current:
                block = _parse_block(current, b_olt, segment_name)
                if block:
                    blocks.append(block)
            current = [line]
            continue
        if re.match(r"^OLT\b", cleaned, flags=re.I) and current:
            current.append(line)
            continue
        if not current:
            current = [line]
        else:
            current.append(line)

    if current:
        block = _parse_block(current, b_olt, segment_name)
        if block:
            blocks.append(block)

    if not blocks:
        raise ValueError("Tube/core tidak terbaca. Format: Tube 12 core 5.6.7.8.9")

    return {
        "blocks": blocks,
        "b_olt": b_olt,
        "segment_name": segment_name,
        "raw_text": normalized,
    }

def _match_b_record(b_cluster: str) -> dict[str, Any]:
    query = _upper(b_cluster)
    rows = fdt_coding_db.list_records(Config.FDT_CODING_DB_PATH, query=query, limit=20)
    if not rows:
        raise ValueError(f"Cluster tidak ditemukan di coding FDT: {b_cluster}")
    # Prefer exact base site match, fallback first row.
    for row in rows:
        site = _upper(row.get("site_name"))
        base = re.sub(r"\s+-\s+FDT\s+\d+\s*$", "", site, flags=re.I)
        if base == query or site == query:
            return row
    return rows[0]


def _b_sheet_for(b_olt: str, segment_name: str) -> str:
    olt = _clean(b_olt).upper() or "UNKNOWN OLT"
    seg = re.sub(r"^0+", "", _clean(segment_name)) or "1"
    return f"{olt} - SEG {seg}"[:99]


def _pad(row: list[Any], width: int = 29) -> list[str]:
    out = [str(v or "") for v in row[:width]]
    while len(out) < width:
        out.append("")
    return out


def _is_coreman_grid(b_values: list[list[Any]]) -> bool:
    header = " ".join(str(cell or "").upper() for row in b_values[:4] for cell in row)
    return "FDT INPUT SPLICING" in header or "HUB FEEDER" in header or "CLUSTER NAME" in header


def _num(value: object) -> int | None:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


def _status_for(entry: dict[str, Any]) -> str:
    return "IDLE" if _upper(entry.get("status")) == "IDLE" else "ASSIGNED"


def submit_mancore(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("raw_text") or payload.get("text") or ""
    parsed = parse_mancore(text)

    olt_override = ""
    segment_override = ""
    if payload.get("b_olt"):
        olt_override = _upper(payload.get("b_olt"))
        if not olt_override.startswith("OLT "):
            olt_override = f"OLT {olt_override}"
    if payload.get("segment_name"):
        segment_override = _clean(payload.get("segment_name"))
    if payload.get("b_cluster") and parsed["blocks"] and len(parsed["blocks"]) == 1:
        parsed["blocks"][0]["b_cluster"] = _upper(payload.get("b_cluster"))

    results: list[dict[str, Any]] = []
    sheet_info: dict[str, Any] | None = None

    for block in parsed["blocks"]:
        b_cluster = block["b_cluster"]
        b_olt = olt_override or parsed["b_olt"] or block.get("b_olt", "")
        b_seg = segment_override or parsed["segment_name"] or block.get("segment_name", "1")
        import sys
        print(f"[DEBUG-MULTI] block={b_cluster}", file=sys.stderr)
        b_record = _match_b_record(b_cluster)
        print(f"[DEBUG-MULTI] fdt_code={b_record.get('fdt_code')}", file=sys.stderr)
        b_sheet = _b_sheet_for(b_olt or b_record.get("olt_initial"), b_seg)
        print(f"[DEBUG-MULTI] sheet={b_sheet}", file=sys.stderr)
        sheet_info = coreman_sheets.ensure_sheet(b_sheet)
        worksheet = coreman_sheets.worksheet_by_title(b_sheet)
        # REFRESH b_values inside loop per block
        b_values = worksheet.get(f"A:{coreman_sheets.column_label(max(29, worksheet.col_count or 0))}") or []
        updated_rows: list[int] = []

        if _is_coreman_grid(b_values):
            body_start = 3
            updates = []
            for entry in block["core_entries"]:
                key = (int(entry["tube"]), int(entry["core"]))
                for idx, row in enumerate(b_values[body_start:], start=body_start + 1):
                    padded = _pad(row)
                    if (_num(padded[COREMAN_GRID_COLUMNS["hub_tube"]]), _num(padded[COREMAN_GRID_COLUMNS["hub_core"]])) == key:
                        padded[COREMAN_GRID_COLUMNS["assignment_status"]] = _status_for(entry)
                        padded[COREMAN_GRID_COLUMNS["b_cluster"]] = "IDLE" if _status_for(entry) == "IDLE" else b_cluster
                        padded[COREMAN_GRID_COLUMNS["fdt_id"]] = "" if _status_for(entry) == "IDLE" else str(b_record.get("fdt_code") or "")
                        updates.append({"range": f"A{idx}:{coreman_sheets.column_label(len(padded))}{idx}", "values": [padded]})
                        updated_rows.append(idx)
                        break
            if updates:
                worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        else:
            rows = []
            for entry in block["core_entries"]:
                rows.append([
                    "FEEDER",
                    b_record.get("hub_id") or b_record.get("fdt_code") or "",
                    b_sheet,
                    entry["tube"],
                    entry["core"],
                    b_record.get("fdt_code") or "",
                    entry["tube"],
                    entry["core"],
                    entry.get("status") or "TAKEN",
                    f"{entry.get('fat_count')} FAT" if entry.get("fat_count") else "",
                    "",
                    "CLUSTER",
                    b_record.get("fdt_code") or "",
                    entry["tube"],
                    entry["core"],
                    b_record.get("site_name") or b_cluster,
                ])
            if rows:
                worksheet.append_rows(rows, value_input_option="USER_ENTERED")

        results.append({
            "b_sheet": b_sheet,
            "b_cluster": b_cluster,
            "fdt_code": b_record.get("fdt_code") or "",
            "entries": len(block["core_entries"]),
            "updated_rows": updated_rows,
        })

    return {
        "ok": True,
        "sheet": sheet_info,
        "blocks_count": len(results),
        "results": results,
        "b_sheet": results[-1]["b_sheet"] if results else "",
        "b_cluster": results[-1]["b_cluster"] if results else "",
        "fdt_code": results[-1]["fdt_code"] if results else "",
        "entries": sum(r["entries"] for r in results),
        "updated_rows": [row for r in results for row in r.get("updated_rows", [])],
        "b_olt": parsed["b_olt"],
        "segment_name": parsed["segment_name"],
        "raw_text": parsed["raw_text"],
    }
