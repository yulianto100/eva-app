import json
import logging
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime

sys.path.insert(0, "/app")
import gmail_rfs_ingest as g
import rfs_sheets

QUERIES = [
    'from:planning@morarepublic.co.id "New RFS BEKASI"',
    'from:planning@morarepublic.co.id "New RFS JAKARTA SELATAN"',
    'from:planning@morarepublic.co.id "New RFS SUKABUMI"',
    'from:planning@morarepublic.co.id "New RFS KARAWANG"',
]


def iso_date(value):
    try:
        return parsedate_to_datetime(value).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(rfs_sheets._timezone()).isoformat(timespec="seconds")


def pending(service, query, processed):
    result, token = [], None
    while True:
        args = {"userId": "me", "q": query, "maxResults": 100}
        if token:
            args["pageToken"] = token
        page = service.users().messages().list(**args).execute()
        batch = page.get("messages", []) or []
        unseen = [item for item in batch if item.get("id") not in processed]
        result.extend(unseen)
        token = page.get("nextPageToken")
        if not token or not unseen:
            return result


def main():
    logging.basicConfig(level=logging.INFO)
    service = g._gmail_service()
    state = g._load_state()
    processed = set(state.get("processed_ids", []))
    items, seen = [], set()
    for query in QUERIES:
        for item in pending(service, query, processed):
            if item["id"] not in seen:
                items.append(item)
                seen.add(item["id"])

    stats = {"pending": len(items), "inserted": 0, "duplicates": 0, "skipped": 0, "errors": []}
    candidates = []
    for item in reversed(items):
        gmail_id = item["id"]
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
            if "planning@morarepublic.co.id" not in sender.lower() or "no-reply@myrepublic.net.id" not in reply_to.lower() or not parsed:
                stats["skipped"] += 1
                processed.add(gmail_id)
                continue
            candidates.append((gmail_id, sender, subject, date, parsed))
        except Exception as exc:
            stats["errors"].append({"id": gmail_id, "error": str(exc)})

    if candidates:
        spreadsheet = rfs_sheets.open_spreadsheet()
        worksheet = spreadsheet.worksheet(rfs_sheets.DATA_SHEET_NAME)
        existing = rfs_sheets._data_records(rfs_sheets._all_values(worksheet))
        existing_ids = {row["message_id"] for row in existing if row.get("message_id")}
        rows = []
        for gmail_id, sender, subject, date, parsed in candidates:
            month = g._month_from_date(date)
            fingerprint = rfs_sheets.fingerprint_for(month, parsed["region"], parsed["site"], parsed["hp"])
            if gmail_id in existing_ids:
                stats["duplicates"] += 1
            else:
                rows.append([month, rfs_sheets.region_label(parsed["region"]), rfs_sheets.site_label(parsed["site"]), parsed["hp"], iso_date(date), "gmail", sender, gmail_id, subject, fingerprint])
                existing_ids.add(gmail_id)
            processed.add(gmail_id)
        if rows:
            worksheet.append_rows(rows, value_input_option="USER_ENTERED")
            rfs_sheets.rebuild_summary_sheet(spreadsheet)
            stats["inserted"] = len(rows)

    state["processed_ids"] = list(processed)
    g._save_state(state)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()

# v4: batch Sheets writes: one read, one append, one summary rebuild per tick.
