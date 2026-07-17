from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Behind nginx/Cloudflare proxy Flask may receive http internally; public callback stays HTTPS.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

sys.path.insert(0, "/opt/eva")
import rfs_sheets
from config import Config

LOGGER = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_SECRET_FILE = Path(os.getenv("GMAIL_OAUTH_CLIENT_FILE", Config.BASE_DIR / ".secrets" / "gmail_oauth_client.json"))
TOKEN_FILE = Path(os.getenv("GMAIL_OAUTH_TOKEN_FILE", Config.BASE_DIR / "data" / "gmail_token.json"))
STATE_FILE = Path(os.getenv("GMAIL_RFS_STATE_FILE", Config.BASE_DIR / "data" / "gmail_rfs_state.json"))
REDIRECT_URI = os.getenv("GMAIL_OAUTH_REDIRECT_URI", "https://eva-mrdesign.my.id/oauth/gmail/callback")
SEARCH_QUERY = os.getenv("GMAIL_RFS_QUERY", 'from:planning@morarepublic.co.id subject:"New RFS"')
TARGET_REGIONS = ["BEKASI", "SUKABUMI", "JAKARTA SELATAN", "KARAWANG"]
SUBJECT_RE = re.compile(r"(?:\[New\s+RFS[^\]]*\]\s*-\s*)?New\s+RFS\s+(BEKASI|SUKABUMI|JAKARTA\s+SELATAN|KARAWANG)\s*-\s*(.+?)\s*:\s*(\d+)\s*(?:HP|HOUSEHOLD)", re.I)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def _header(headers: list[dict], name: str) -> str:
    wanted = name.lower()
    for item in headers or []:
        if str(item.get("name", "")).lower() == wanted:
            return _clean(item.get("value", ""))
    return ""


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_ids": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ids = list(dict.fromkeys(state.get("processed_ids", [])))[-5000:]
    STATE_FILE.write_text(json.dumps({"processed_ids": ids}, indent=2), encoding="utf-8")


def credentials() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def is_authorized() -> bool:
    return credentials() is not None


def auth_url() -> str:
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI, autogenerate_code_verifier=False)
    url, _state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return url


def handle_callback(authorization_response: str) -> dict:
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI, autogenerate_code_verifier=False)
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return {"ok": True, "token_file": str(TOKEN_FILE)}


def _gmail_service():
    creds = credentials()
    if creds is None:
        raise RuntimeError("Gmail OAuth belum authorized. Buka /admin/gmail-rfs/auth dulu.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _month_from_date(date_header: str) -> str:
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m")
    except Exception:
        return rfs_sheets.month_key()


def _parse_subject(subject: str) -> dict | None:
    match = SUBJECT_RE.search(subject or "")
    if not match:
        return None
    region = _clean(match.group(1)).upper()
    site = _clean(match.group(2)).upper()
    hp = int(match.group(3))
    return {"region": region, "site": site, "hp": hp}


def ingest(max_results: int = 25) -> dict:
    svc = _gmail_service()
    state = _load_state()
    processed = set(state.get("processed_ids", []))
    result = svc.users().messages().list(userId="me", q=SEARCH_QUERY, maxResults=max_results).execute()
    messages = result.get("messages", []) or []
    stats = {"seen": len(messages), "inserted": 0, "duplicates": 0, "skipped": 0, "errors": []}

    for item in reversed(messages):
        gmail_id = item.get("id")
        if not gmail_id or gmail_id in processed:
            stats["skipped"] += 1
            continue
        try:
            msg = svc.users().messages().get(userId="me", id=gmail_id, format="metadata", metadataHeaders=["From", "Reply-To", "Subject", "Date", "Message-ID"]).execute()
            headers = (msg.get("payload") or {}).get("headers", [])
            from_h = _header(headers, "From")
            reply_to = _header(headers, "Reply-To")
            subject = _header(headers, "Subject")
            date_h = _header(headers, "Date")

            if "planning@morarepublic.co.id" not in from_h.lower():
                stats["skipped"] += 1; processed.add(gmail_id); continue
            if "no-reply@myrepublic.net.id" not in reply_to.lower():
                stats["skipped"] += 1; processed.add(gmail_id); continue
            parsed = _parse_subject(subject)
            if not parsed or parsed["region"] not in TARGET_REGIONS:
                stats["skipped"] += 1; processed.add(gmail_id); continue

            payload = {
                "month": _month_from_date(date_h),
                "region": parsed["region"],
                "site": parsed["site"],
                "hp": parsed["hp"],
                "recorded_at": datetime.now(rfs_sheets._timezone()).isoformat(timespec="seconds"),
                "source_chat": "gmail",
                "sender": from_h,
                "message_id": gmail_id,
                "raw_text": subject,
            }
            saved = rfs_sheets.append_record(payload)
            if saved.get("duplicate"):
                stats["duplicates"] += 1
            else:
                stats["inserted"] += 1
            processed.add(gmail_id)
        except Exception as exc:
            LOGGER.exception("Gmail RFS ingest gagal untuk %s", gmail_id)
            stats["errors"].append({"id": gmail_id, "error": str(exc)})

    state["processed_ids"] = list(processed)
    _save_state(state)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(ingest(), indent=2))
