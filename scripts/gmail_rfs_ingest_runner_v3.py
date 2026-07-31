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


def _iso_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(rfs_sheets._timezone()).isoformat(timespec="seconds")


def _pending_messages(service, query: str, processed: set[str]) -> list[dict]:
    """Read newest pages until reaching one already fully covered by state."""
    pending, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        batch = response.get("messages", []) or []
        unseen = [item for item in batch if item.get("id") not in processed]
        pending.extend(unseen)
        page_token = response.get("nextPageToken")
        if not page_token or not unseen:
            return pending


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = g._gmail_service()
    state = g._load_state()
    processed = set(state.get("processed_ids", []))
    items, seen_ids = [], set()
    for query in QUERIES:
        for item in _pending_messages(service, query, processed):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                items.append(item)

    stats = {"pending": len(items), "inserted": 0, "duplicates": 0, "skipped": 0, "errors": []}
    for item in reversed(items):
        gmail_id = item["id"]
        try:
            msg = service.users().messages().get(
                userId="me", id=gmail_id, format="metadata",
                metadataHeaders=["From", "Reply-To", "Subject", "Date"],
            ).execute()
            headers = (msg.get("payload") or {}).get("headers", [])
            sender = g._header(headers, "From")
            reply_to = g._header(headers, "Reply-To")
            subject = g._header(headers, "Subject")
            date_header = g._header(headers, "Date")
            parsed = g._parse_subject(subject)
            if (
                "planning@morarepublic.co.id" not in sender.lower()
                or "no-reply@myrepublic.net.id" not in reply_to.lower()
                or not parsed
            ):
                stats["skipped"] += 1
                processed.add(gmail_id)
                continue
            saved = rfs_sheets.append_record({
                "month": g._month_from_date(date_header),
                "region": parsed["region"],
                "site": parsed["site"],
                "hp": parsed["hp"],
                "recorded_at": _iso_date(date_header),
                "source_chat": "gmail",
                "sender": sender,
                "message_id": gmail_id,
                "raw_text": subject,
            })
            stats["duplicates" if saved.get("duplicate") else "inserted"] += 1
            processed.add(gmail_id)
        except Exception as exc:
            logging.exception("Gmail RFS ingest gagal untuk %s", gmail_id)
            stats["errors"].append({"id": gmail_id, "error": str(exc)})

    state["processed_ids"] = list(processed)
    g._save_state(state)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()

# v3: every-2-minute incremental polling; exact-area queries and paging only until state boundary.
