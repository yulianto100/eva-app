#!/usr/bin/env python3
"""Restore valid Gmail RFS rows missing from the RFS sheet.
A Gmail message ID is the only duplicate key for Gmail-sourced records.
"""
import argparse
import json
import sys
from datetime import datetime

sys.path.insert(0, "/app")
import gmail_rfs_ingest as g
import rfs_sheets

QUERIES = [
    'from:planning@morarepublic.co.id "New RFS BEKASI"',
    'from:planning@morarepublic.co.id "New RFS JAKARTA SELATAN"',
    'from:planning@morarepublic.co.id "New RFS SUKABUMI"',
    'from:planning@morarepublic.co.id "New RFS KARAWANG"',
]


def list_all(service, query):
    items, token = [], None
    while True:
        args = {"userId": "me", "q": query, "maxResults": 100}
        if token:
            args["pageToken"] = token
        page = service.users().messages().list(**args).execute()
        items.extend(page.get("messages", []) or [])
        token = page.get("nextPageToken")
        if not token:
            return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Append missing valid Gmail IDs to Sheets")
    parser.add_argument("--report", default="/tmp/rfs_restore_by_gmail_id_report.json")
    args = parser.parse_args()

    service = g._gmail_service()
    spreadsheet = rfs_sheets.open_spreadsheet()
    worksheet = spreadsheet.worksheet(rfs_sheets.DATA_SHEET_NAME)
    existing = rfs_sheets._data_records(rfs_sheets._all_values(worksheet))
    existing_ids = {row["message_id"] for row in existing if row.get("message_id")}

    ids, seen = [], set()
    for query in QUERIES:
        for item in list_all(service, query):
            gmail_id = item.get("id")
            if gmail_id and gmail_id not in seen:
                seen.add(gmail_id)
                ids.append(gmail_id)

    missing, skipped, errors = [], 0, []
    for gmail_id in ids:
        try:
            message = service.users().messages().get(
                userId="me", id=gmail_id, format="metadata",
                metadataHeaders=["From", "Reply-To", "Subject", "Date"],
            ).execute()
            headers = (message.get("payload") or {}).get("headers", [])
            sender = g._header(headers, "From")
            reply_to = g._header(headers, "Reply-To")
            subject = g._header(headers, "Subject")
            date = g._header(headers, "Date")
            parsed = g._parse_subject(subject)
            if (
                not parsed
                or parsed["region"] not in g.TARGET_REGIONS
                or "planning@morarepublic.co.id" not in sender.lower()
                or "no-reply@myrepublic.net.id" not in reply_to.lower()
            ):
                skipped += 1
                continue
            if gmail_id in existing_ids:
                continue
            month = g._month_from_date(date)
            missing.append({
                "id": gmail_id,
                "month": month,
                "region": parsed["region"],
                "site": parsed["site"],
                "hp": parsed["hp"],
                "date": date,
                "sender": sender,
                "subject": subject,
                "fingerprint": rfs_sheets.fingerprint_for(month, parsed["region"], parsed["site"], parsed["hp"]),
            })
        except Exception as exc:
            errors.append({"id": gmail_id, "error": str(exc)})

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "gmail_messages_considered": len(ids),
        "existing_sheet_rows": len(existing),
        "missing_valid_gmail_ids": len(missing),
        "skipped_invalid": skipped,
        "errors": errors,
        "missing": missing,
    }

    if args.apply and missing:
        rows = []
        for item in missing:
            rows.append([
                item["month"],
                rfs_sheets.region_label(item["region"]),
                rfs_sheets.site_label(item["site"]),
                item["hp"],
                item["date"],
                "gmail",
                item["sender"],
                item["id"],
                item["subject"],
                item["fingerprint"],
            ])
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        rfs_sheets.rebuild_summary_sheet(spreadsheet)
        report["inserted"] = len(rows)
    else:
        report["inserted"] = 0

    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "missing"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
