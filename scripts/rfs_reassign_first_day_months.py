#!/usr/bin/env python3
"""Normalize existing Gmail RFS rows: any email dated calendar day 1 (WIB) belongs to prior month."""
import argparse
import json
import re
import sys

sys.path.insert(0, "/app")
import gmail_rfs_ingest as g
import rfs_sheets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default="/tmp/rfs_reassign_first_day_report.json")
    args = parser.parse_args()

    service = g._gmail_service()
    spreadsheet = rfs_sheets.open_spreadsheet()
    worksheet = spreadsheet.worksheet(rfs_sheets.DATA_SHEET_NAME)
    records = rfs_sheets._data_records(rfs_sheets._all_values(worksheet))
    changes, errors = [], []

    for record in records:
        gmail_id = record.get("message_id", "")
        if not gmail_id or not re.match(r"^[0-9A-Za-z_-]{16,}$", gmail_id):
            continue
        try:
            message = service.users().messages().get(
                userId="me", id=gmail_id, format="metadata", metadataHeaders=["Date"]
            ).execute()
            headers = (message.get("payload") or {}).get("headers", [])
            date = g._header(headers, "Date")
            expected_month = g._month_from_date(date)
            if record["month"] != expected_month:
                changes.append({
                    "row_number": record["row_number"],
                    "gmail_id": gmail_id,
                    "old_month": record["month"],
                    "new_month": expected_month,
                    "date": date,
                    "site": record["site"],
                    "hp": record["hp"],
                })
        except Exception as exc:
            errors.append({"gmail_id": gmail_id, "error": str(exc)})

    if args.apply and changes:
        updates = [
            {"range": f"A{item['row_number']}", "values": [[item["new_month"]]]}
            for item in changes
        ]
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        rfs_sheets.rebuild_summary_sheet(spreadsheet)

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "gmail_rows_checked": sum(1 for row in records if row.get("message_id")),
        "rows_reassigned": len(changes) if args.apply else 0,
        "changes_found": len(changes),
        "errors": errors,
        "changes": changes,
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "changes"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
