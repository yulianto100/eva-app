import json
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/opt/eva")
import gmail_rfs_ingest as g
import rfs_sheets

AREAS = {"BEKASI", "JAKARTA SELATAN", "SUKABUMI"}
QUERY = 'from:planning@morarepublic.co.id subject:"New RFS"'


def all_messages(service):
    messages, page = [], None
    while True:
        kwargs = {"userId": "me", "q": QUERY, "maxResults": 500}
        if page:
            kwargs["pageToken"] = page
        response = service.users().messages().list(**kwargs).execute()
        messages.extend(response.get("messages", []) or [])
        page = response.get("nextPageToken")
        if not page:
            return messages


def main():
    svc = g._gmail_service()
    emails = all_messages(svc)

    spreadsheet = rfs_sheets.open_spreadsheet()
    ws = spreadsheet.worksheet(rfs_sheets.DATA_SHEET_NAME)
    records = rfs_sheets._data_records(rfs_sheets._all_values(ws))
    existing_message_ids = {r["message_id"] for r in records if r.get("message_id")}
    existing_fingerprints = {r["fingerprint"] for r in records if r.get("fingerprint")}

    valid, missing, represented_by_fp, rejected, parse_fail = [], [], [], [], []
    source_counts = Counter()
    all_seen = []

    for item in emails:
        msg = svc.users().messages().get(
            userId="me", id=item["id"], format="metadata",
            metadataHeaders=["From", "Reply-To", "Subject", "Date"],
        ).execute()
        headers = (msg.get("payload") or {}).get("headers", [])
        from_h = g._header(headers, "From")
        reply_to = g._header(headers, "Reply-To")
        subject = g._header(headers, "Subject")
        date_h = g._header(headers, "Date")
        parsed = g._parse_subject(subject)

        base = {"id": item["id"], "date": date_h, "from": from_h, "reply_to": reply_to, "subject": subject}
        if not parsed:
            if any(area in subject.upper() for area in AREAS):
                parse_fail.append(base)
            continue
        if parsed["region"] not in AREAS:
            continue
        base.update(parsed)
        base["month"] = g._month_from_date(date_h)
        base["fingerprint"] = rfs_sheets.fingerprint_for(base["month"], base["region"], base["site"], base["hp"])
        all_seen.append(base)
        source_counts[base["region"]] += 1

        acceptable = "planning@morarepublic.co.id" in from_h.lower() and "no-reply@myrepublic.net.id" in reply_to.lower()
        if not acceptable:
            base["reason"] = "sender/reply-to tidak lolos filter"
            rejected.append(base)
            continue
        valid.append(base)
        if base["id"] in existing_message_ids:
            continue
        if base["fingerprint"] in existing_fingerprints:
            represented_by_fp.append(base)
        else:
            missing.append(base)

    rec_counts = Counter(r["region"].upper() for r in records if r["region"].upper() in AREAS)
    valid_counts = Counter(x["region"] for x in valid)
    out = {
        "query": QUERY,
        "gmail_result_total": len(emails),
        "areas": sorted(AREAS),
        "gmail_target_subjects_by_area": dict(sorted(source_counts.items())),
        "gmail_accepted_by_area": dict(sorted(valid_counts.items())),
        "rfs_sheet_records_by_area": dict(sorted(rec_counts.items())),
        "rfs_sheet_total_records": len(records),
        "missing_not_represented": missing,
        "represented_by_same_site_hp_month_but_message_id_absent": represented_by_fp,
        "rejected_by_current_filter": rejected,
        "target_like_parse_fail": parse_fail,
        "summary": {
            "accepted_target_emails": len(valid),
            "missing_not_represented_count": len(missing),
            "represented_by_fingerprint_count": len(represented_by_fp),
            "rejected_by_filter_count": len(rejected),
            "target_like_parse_fail_count": len(parse_fail),
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
