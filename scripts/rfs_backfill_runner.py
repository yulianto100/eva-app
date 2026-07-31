import json
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, "/app")
import rfs_sheets

SOURCE = Path("/tmp/rfs_backfill_54.json")


def original_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(rfs_sheets._timezone()).isoformat(timespec="seconds")


def main() -> None:
    candidates = json.loads(SOURCE.read_text(encoding="utf-8"))
    spreadsheet = rfs_sheets.open_spreadsheet()
    worksheet = spreadsheet.worksheet(rfs_sheets.DATA_SHEET_NAME)
    current = rfs_sheets._data_records(rfs_sheets._all_values(worksheet))
    existing_ids = {r["message_id"] for r in current if r.get("message_id")}
    existing_fingerprints = {r["fingerprint"] for r in current if r.get("fingerprint")}

    add_rows, skipped = [], []
    for item in candidates:
        fingerprint = rfs_sheets.fingerprint_for(item["month"], item["region"], item["site"], item["hp"])
        if item["id"] in existing_ids or fingerprint in existing_fingerprints:
            skipped.append(item["id"])
            continue
        add_rows.append([
            item["month"],
            rfs_sheets.region_label(item["region"]),
            rfs_sheets.site_label(item["site"]),
            item["hp"],
            original_date(item["date"]),
            "manual-gmail-backfill",
            "planning@morarepublic.co.id",
            item["id"],
            item["subject"],
            fingerprint,
        ])

    if add_rows:
        worksheet.append_rows(add_rows, value_input_option="USER_ENTERED")
        rfs_sheets.rebuild_summary_sheet(spreadsheet)

    after = rfs_sheets._data_records(rfs_sheets._all_values(worksheet))
    written_ids = {r["message_id"] for r in after if r.get("message_id")}
    missing_after = [item["id"] for item in candidates if item["id"] not in written_ids]
    print(json.dumps({
        "candidates": len(candidates),
        "inserted": len(add_rows),
        "skipped_existing": len(skipped),
        "verified_present": len(candidates) - len(missing_after),
        "missing_after": missing_after,
        "total_sheet_records": len(after),
        "inserted_hp": sum(int(row[3]) for row in add_rows),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
