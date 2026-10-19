"""EVA subscription billing via the self-hosted GoPay QRIS gateway (pg.dyena.biz.id).

Flow: user picks a plan -> checkout creates a local invoice -> gateway returns a
dynamic QRIS string -> the background poller (plus the status endpoint while the
page is open) verifies settlement against the GoPay merchant feed -> the plan
activates exactly once -> confirmation email.

Invariants:
  - Settlement compares amount + the gateway's per-QRIS trx_id against the local
    invoice; mismatch never activates.
  - Activation is idempotent: _activate() runs under BEGIN IMMEDIATE and checks
    the terminal status, so repeated polls never extend a plan twice.
  - The gateway API key lives only in .secrets (mode 0600), never logged.
  - The background poller holds a process lock so only one poller runs per
    container; extra pollers would be harmless (idempotent) but wasteful.
"""
from __future__ import annotations

import fcntl
import json
import math
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("eva.billing")


def _resolve(env_key: str, candidates: list[str]) -> Path:
    override = os.environ.get(env_key)
    if override:
        return Path(override)
    for candidate in candidates:
        if Path(candidate).exists():
            return Path(candidate)
    return Path(candidates[0])


USERS_DB = _resolve("EVA_BILLING_DB", ["/app/data/users.db", "/opt/eva/data/users.db"])
SECRETS = _resolve("EVA_BILLING_SECRETS", ["/app/.secrets/pg.json", "/opt/eva/.secrets/pg.json"])
POLLER_LOCK = Path("/tmp/eva_billing_poller.lock")
INVOICE_PDF_DIR = _resolve("EVA_INVOICE_PDFS", ["/app/data/billing_receipts", "/opt/eva/data/billing_receipts"])

PLANS = {
    "standar": {"code": "standar", "label": "Standar", "price": 29000, "days": 30},
    "premium": {"code": "premium", "label": "Premium", "price": 69000, "days": 30},
}

# Tier order for plan-change rules: never let a payment move a user DOWN a rank.
PLAN_RANK = {"standar": 1, "premium": 2}
UPGRADE_MIN = 5000      # ponytail: flat floor so a 1-day upgrade is still worth a QR scan
UPGRADE_ROUND = 1000


def upgrade_quote(user: dict) -> dict | None:
    """Prorated Standar->Premium price: per-day price gap x remaining days (ceil),
    rounded up to Rp 1.000, floor Rp 5.000. Premium ends on the SAME date."""
    cur = user_plan(user)
    if not (cur.get("paid") and cur.get("plan") == "standar"):
        return None
    days = max(1, math.ceil((float(cur["until"]) - time.time()) / 86400))
    gap = PLANS["premium"]["price"] - PLANS["standar"]["price"]
    raw = gap * days / PLANS["standar"]["days"]
    price = max(UPGRADE_MIN, math.ceil(raw / UPGRADE_ROUND) * UPGRADE_ROUND)
    return {"days": days, "price": int(price), "until": float(cur["until"])}


# --- expiry reminders -------------------------------------------------------

REMINDER_DAYS = 3            # send the "expiring" mail this close to expiry
REMINDER_AFTER_DAYS = 7      # send the "expired" mail only within this window
REMINDER_INTERVAL = 1800     # sweep at most this often (poller calls it every tick)
_last_reminder_sweep = 0.0


def _claim_reminder(user_id: str, kind: str, key_until: float) -> bool:
    """True exactly once per (user, kind, expiry date): the insert IS the claim."""
    with _conn() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO billing_reminders(user_id, kind, plan_until, sent_at) VALUES(?,?,?,?)",
            (user_id, kind, float(key_until), time.time()),
        )
        return cur.rowcount == 1


def _send_reminder_email(kind: str, email: str, name: str, plan_label: str,
                         until_txt: str, when: str) -> None:
    """Best-effort handoff to the Resend helper (monkeypatched in tests)."""
    try:
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from eva_email import send_plan_reminder_async  # type: ignore
        send_plan_reminder_async(email, name, kind, plan_label, until_txt, when)
    except Exception as exc:
        LOGGER.warning("billing reminder email skipped: %s", type(exc).__name__)


def reminder_sweep(force: bool = False) -> int:
    """Send H-3 / expired / trial-ending mails, at most once per (user, kind, expiry)."""
    global _last_reminder_sweep
    now = time.time()
    if not force and now - _last_reminder_sweep < REMINDER_INTERVAL:
        return 0
    _last_reminder_sweep = now
    sent = 0
    ensure_schema()
    with _conn() as db:
        expiring = db.execute(
            "SELECT id, email, name, plan, plan_until FROM users "
            "WHERE plan IN ('standar','premium') AND plan_until>? AND plan_until<=?",
            (now, now + REMINDER_DAYS * 86400),
        ).fetchall()
        expired = db.execute(
            "SELECT id, email, name, plan, plan_until FROM users "
            "WHERE plan IN ('standar','premium') AND plan_until<=? AND plan_until>?",
            (now, now - REMINDER_AFTER_DAYS * 86400),
        ).fetchall()
        trial = db.execute(
            "SELECT u.id,u.email,u.name,t.until FROM billing_trials t JOIN users u ON u.id=t.user_id "
            "WHERE u.active=1 AND u.role!='owner' AND t.until>? AND t.until<=? "
            "AND (u.plan NOT IN ('standar','premium') OR u.plan_until<=?)",
            (now, now + 86400, now),
        ).fetchall()
    for u in expiring:
        days = max(1, math.ceil((float(u["plan_until"]) - now) / 86400))
        when = "besok" if days == 1 else "%d hari lagi" % days
        if _claim_reminder(u["id"], "expiring", u["plan_until"]):
            _send_reminder_email("expiring", str(u["email"]), str(u["name"] or ""),
                                 PLANS.get(str(u["plan"]), {}).get("label", "Standar"),
                                 _fmt_date_wib(float(u["plan_until"])), when)
            sent += 1
    for u in expired:
        if _claim_reminder(u["id"], "expired", u["plan_until"]):
            _send_reminder_email("expired", str(u["email"]), str(u["name"] or ""),
                                 PLANS.get(str(u["plan"]), {}).get("label", "Standar"),
                                 _fmt_date_wib(float(u["plan_until"])), "")
            sent += 1
    for u in trial:
        trial_until = float(u["until"])
        days = max(1, math.ceil((trial_until - now) / 86400))
        when = "besok" if days == 1 else "%d hari lagi" % days
        if _claim_reminder(u["id"], "trial", trial_until):
            _send_reminder_email("trial", str(u["email"]), str(u["name"] or ""),
                                 "Premium", _fmt_date_wib(trial_until), when)
            sent += 1
    if sent:
        LOGGER.info("billing reminders sent: %d", sent)
    return sent
TRIAL_DAYS = 3
POLL_INTERVAL = 20.0          # background poller cadence (seconds)
PENDING_TTL = 30 * 60         # invoice lifetime; matches the gateway QRIS window
EXPIRE_GRACE = 10 * 60        # keep polling this long past expiry (feed lag)
DEFAULT_PLAN = "standar"      # plan shown when nothing is active
_LOCK = threading.Lock()
_POLLER_STARTED = False
_POLLER_HANDLE = None
_MONTHS_ID = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
              "Agustus", "September", "Oktober", "November", "Desember")


def _conn() -> sqlite3.Connection:
    db = sqlite3.connect(USERS_DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def ensure_schema() -> None:
    """Create billing tables + user plan columns if missing. Idempotent."""
    with _conn() as db:
        cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in (("plan", "TEXT NOT NULL DEFAULT ''"),
                         ("plan_until", "REAL NOT NULL DEFAULT 0")):
            if col not in cols:
                try:
                    db.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, ddl))
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS billing_trials (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                started_at REAL NOT NULL,
                until REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_invoices (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                qris_id TEXT NOT NULL DEFAULT '',
                qris_url TEXT NOT NULL DEFAULT '',
                qris_string TEXT NOT NULL DEFAULT '',
                trx_id TEXT NOT NULL DEFAULT '',
                reference TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                paid_at REAL,
                activated_at REAL,
                until_after REAL NOT NULL DEFAULT 0,
                raw_status TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_billing_user ON billing_invoices(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_billing_status ON billing_invoices(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS billing_reminders (
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                plan_until REAL NOT NULL,
                sent_at REAL NOT NULL,
                PRIMARY KEY (user_id, kind, plan_until)
            );
            CREATE TABLE IF NOT EXISTS billing_invoice_emails (
                invoice_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                first_attempt_at REAL,
                sent_at REAL,
                provider_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        inv_cols = {row["name"] for row in db.execute("PRAGMA table_info(billing_invoices)").fetchall()}
        # Several gunicorn workers boot at once after a HUP and race these ALTERs;
        # a concurrent "duplicate column" means another worker already added it.
        for col, ddl in (("until_after", "REAL NOT NULL DEFAULT 0"),
                         ("kind", "TEXT NOT NULL DEFAULT 'new'"),
                         ("base_price", "INTEGER NOT NULL DEFAULT 0"),
                         ("upgrade_days", "INTEGER NOT NULL DEFAULT 0")):
            if col not in inv_cols:
                try:
                    db.execute("ALTER TABLE billing_invoices ADD COLUMN %s %s" % (col, ddl))
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise


def _gateway_conf() -> tuple[str, str]:
    try:
        conf = json.loads(SECRETS.read_text())
        return str(conf.get("base_url") or "https://pg.dyena.biz.id").rstrip("/"), str(conf.get("api_key") or "")
    except Exception:
        return "https://pg.dyena.biz.id", ""


def _gateway_request(method: str, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
    base, key = _gateway_conf()
    if not key:
        raise RuntimeError("Kunci PG belum dikonfigurasi.")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "x-api-key": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("PG mengembalikan data tidak valid.")
    return body


def _fmt_date_wib(ts: float) -> str:
    try:
        parts = time.gmtime(float(ts) + 7 * 3600)  # WIB
        return "%d %s %d" % (parts.tm_mday, _MONTHS_ID[parts.tm_mon - 1], parts.tm_year)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Plan state
# ---------------------------------------------------------------------------

def user_plan(user: dict) -> dict:
    """Paid assignments win; trials start only after an explicit, one-use claim."""
    state = {"plan": "free", "label": "Free", "until": 0, "trial": False,
             "active": False, "paid": False, "owner": False, "trial_eligible": False}
    if not user or not user.get("active", True):
        return state
    if user.get("role") == "owner":
        return {**state, "plan": "premium", "label": "Owner", "active": True, "owner": True}
    now = time.time()
    plan = str(user.get("plan") or "").strip().lower()
    try:
        until = float(user.get("plan_until") or 0)
    except (TypeError, ValueError):
        until = 0
    if plan in PLANS and math.isfinite(until) and until > now:
        return {**state, "plan": plan, "label": PLANS[plan]["label"], "until": until,
                "active": True, "paid": True}
    trial = None
    if user.get("id"):
        with _conn() as db:
            trial = db.execute("SELECT until FROM billing_trials WHERE user_id=?", (user["id"],)).fetchone()
    trial_until = float(trial["until"]) if trial else 0
    if math.isfinite(trial_until) and trial_until > now:
        return {**state, "plan": "premium", "label": "Premium", "until": trial_until,
                "trial": True, "active": True}
    return {**state, "trial_eligible": bool(user.get("id") and not trial)}


def start_trial(user_id: str) -> dict:
    """Claim 72 hours exactly once; never rewrite an assigned paid plan."""
    with _conn() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["active"]:
            raise PermissionError("Akun tidak tersedia.")
        if user["role"] == "owner" or (user["plan"] in PLANS and float(user["plan_until"]) > now):
            raise ValueError("Paket sudah aktif; uji coba tidak diperlukan.")
        trial = db.execute("SELECT until FROM billing_trials WHERE user_id=?", (user_id,)).fetchone()
        if trial and float(trial["until"]) <= now:
            raise ValueError("Uji coba Premium sudah pernah dipakai. Pilih paket untuk melanjutkan.")
        if not trial:
            db.execute("INSERT INTO billing_trials(user_id,started_at,until) VALUES(?,?,?)",
                       (user_id, now, now + TRIAL_DAYS * 86400))
    return user_plan(dict(user))


def plan_allows(plan_code: str, feature: str) -> bool:
    """Server and UI share package restrictions, independently of topology rules."""
    return plan_code == "premium" or plan_code == "standar" and feature not in {"dxf", "hpdb"}


def conversion_access(user: dict, targets) -> dict | None:
    plan = user_plan(user)
    if not plan["active"]:
        return {"error": "Pilih paket atau aktifkan uji coba Premium untuk melanjutkan.",
                "code": "EVA_PLAN_REQUIRED", "billing_url": "/langganan"}
    locked = sorted(target for target in targets if not plan_allows(plan["plan"], target))
    if locked:
        return {"error": "DXF dan HPDB tersedia di paket Premium. Upgrade untuk melanjutkan.",
                "code": "EVA_PREMIUM_REQUIRED", "features": locked, "billing_url": "/langganan"}
    return None


def public_invoice(inv: dict | None) -> dict | None:
    if not inv:
        return None
    return {
        "id": inv.get("id"),
        "plan": inv.get("plan"),
        "amount": inv.get("amount"),
        "base_price": int(inv.get("base_price") or 0)
        or PLANS.get(str(inv.get("plan") or ""), {}).get("price", inv.get("amount")),
        "kind": inv.get("kind") or "new",
        "upgrade_days": int(inv.get("upgrade_days") or 0),
        "status": inv.get("status"),
        "qris_string": inv.get("qris_string") or "",
        "qris_url": inv.get("qris_url") or "",
        "created_at": inv.get("created_at"),
        "expires_at": inv.get("expires_at"),
        "paid_at": inv.get("paid_at"),
        "activated_at": inv.get("activated_at"),
        "until_after": float(inv.get("until_after") or 0),
    }


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def create_invoice(user: dict, plan_code: str) -> dict:
    """Create a local invoice + a dynamic QRIS at the gateway."""
    plan = PLANS.get(plan_code)
    if not plan:
        raise ValueError("Paket tidak dikenal.")
    current = user_plan(user)
    if current.get("paid") and PLAN_RANK.get(plan_code, 0) < PLAN_RANK.get(str(current.get("plan") or ""), 0):
        # No downgrade while a higher paid plan is active (operator rule):
        # extend or wait for expiry, but never buy downward.
        raise ValueError(
            "Paket %s masih aktif sampai %s. Paket %s tidak bisa dibeli selama paket %s aktif."
            % (current["label"], _fmt_date_wib(current["until"]), plan["label"], current["label"])
        )
    ensure_schema()
    quote = upgrade_quote(user) if plan_code == "premium" else None
    kind = "upgrade" if quote else "new"
    price = quote["price"] if quote else plan["price"]
    up_days = quote["days"] if quote else 0
    now = time.time()
    inv_id = "EVA-" + secrets.token_hex(8).upper()
    # Unique amount ("kode unik"): the gateway matches payments by amount + time
    # and the first poller to see a match claims it, so two live invoices with
    # identical amounts could cross-claim each other's payment. A per-invoice
    # suffix keeps every live invoice amount distinct; the dynamic QRIS encodes
    # the exact total, so the buyer just scans and pays it.
    window = now - 2 * 3600
    with _conn() as db:
        db.execute("BEGIN IMMEDIATE")
        taken = {int(r["amount"]) for r in db.execute(
            "SELECT amount FROM billing_invoices WHERE created_at>?", (window,)).fetchall()}
        suffix = 0
        for _ in range(40):
            candidate = int.from_bytes(secrets.token_bytes(2), "big") % 999 + 1
            if price + candidate not in taken:
                suffix = candidate
                break
        if not suffix:
            for candidate in range(1, 1000):
                if price + candidate not in taken:
                    suffix = candidate
                    break
        if not suffix:
            suffix = int.from_bytes(secrets.token_bytes(2), "big") % 9000 + 1000
        amount = price + suffix
        db.execute(
            "INSERT INTO billing_invoices(id,user_id,plan,amount,status,created_at,expires_at,kind,base_price,upgrade_days) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (inv_id, user["id"], plan["code"], amount, "creating", now, now + PENDING_TTL, kind, price, up_days),
        )
        db.execute("COMMIT")
    try:
        resp = _gateway_request("POST", "/create-qris", {"amount": amount, "order_id": inv_id})
    except Exception as exc:
        # A create may commit server-side then time out client-side; check first.
        look = None
        try:
            look = _gateway_request(
                "GET", "/api/qr-status-by-order/" + urllib.parse.quote(inv_id, safe=""), timeout=10
            )
        except Exception:
            pass
        if look and str(look.get("status") or "").upper() in {"PENDING", "PAID"}:
            with _conn() as db:
                db.execute(
                    "UPDATE billing_invoices SET status='pending', qris_id=?, trx_id=?, raw_status='create-retry' WHERE id=?",
                    (str(look.get("qris_id") or ""), str(look.get("trx_id") or ""), inv_id),
                )
            return get_invoice(inv_id)
        with _conn() as db:
            db.execute("UPDATE billing_invoices SET status='failed', raw_status=? WHERE id=?",
                       (type(exc).__name__, inv_id))
        raise
    data = resp.get("data") or {}
    if not resp.get("success") or not data.get("qris_code"):
        with _conn() as db:
            db.execute("UPDATE billing_invoices SET status='failed', raw_status=? WHERE id=?",
                       (str(resp.get("message") or "create gagal")[:200], inv_id))
        raise RuntimeError("PG tidak mengembalikan QRIS.")
    with _conn() as db:
        db.execute(
            "UPDATE billing_invoices SET status='pending', qris_id=?, qris_url=?, qris_string=?, "
            "trx_id=?, raw_status='' WHERE id=?",
            (str(data.get("qris_id") or ""), str(data.get("qris_url") or ""),
             str(data.get("qris_code") or ""), str(data.get("trx_id") or ""), inv_id),
        )
    return get_invoice(inv_id)


def get_invoice(inv_id: str) -> dict | None:
    ensure_schema()
    with _conn() as db:
        row = db.execute("SELECT * FROM billing_invoices WHERE id=?", (inv_id,)).fetchone()
    return dict(row) if row else None


def _load_user(user_id: str) -> dict | None:
    """Fresh user row (plan fields) so display endpoints see just-activated plans."""
    try:
        with _conn() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def user_invoices(user_id: str, limit: int = 20) -> list[dict]:
    ensure_schema()
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM billing_invoices WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark(inv_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as db:
        db.execute(f"UPDATE billing_invoices SET {cols} WHERE id=?", (*fields.values(), inv_id))


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def check_invoice(inv_id: str, force: bool = False) -> dict:
    """Poll the gateway once for this invoice; activate on verified settlement.

    Returns the fresh invoice row. Safe to call from many places; the terminal
    transition happens under a transaction so double activation is impossible.
    """
    inv = get_invoice(inv_id)
    if not inv:
        raise ValueError("Invoice tidak ditemukan.")
    if inv["status"] in {"activated", "failed", "expired", "mismatch"}:
        return inv
    now = time.time()
    expires = float(inv["expires_at"] or 0)
    if now > expires + EXPIRE_GRACE:
        _mark(inv_id, status="expired", raw_status="expired")
        return get_invoice(inv_id)
    try:
        resp = _gateway_request(
            "GET", "/api/qr-status-by-order/" + urllib.parse.quote(str(inv["id"]), safe="")
        )
    except Exception as exc:
        LOGGER.warning("billing poll failed for %s: %s", inv_id, type(exc).__name__)
        return inv
    status = str(resp.get("status") or "").upper()
    raw = status or "?"
    if resp.get("paid") and status == "PAID":
        tx = resp.get("transaction") or {}
        try:
            remote_amount = int(tx.get("amount") or resp.get("amount") or 0)
        except (TypeError, ValueError):
            remote_amount = 0
        remote_trx = str(resp.get("trx_id") or tx.get("transaction_id") or "")
        if remote_amount and remote_amount != int(inv["amount"]):
            _mark(inv_id, status="mismatch", raw_status=raw, reference=remote_trx)
            LOGGER.error("billing amount mismatch %s: local=%s remote=%s",
                         inv_id, inv["amount"], remote_amount)
            return get_invoice(inv_id)
        if inv["trx_id"] and remote_trx and remote_trx != inv["trx_id"]:
            _mark(inv_id, status="mismatch", raw_status=raw, reference=remote_trx)
            LOGGER.error("billing trx mismatch %s", inv_id)
            return get_invoice(inv_id)
        return _activate(inv, remote_trx)
    if status in {"EXPIRED", "NOT_FOUND"}:
        _mark(inv_id, status="expired", raw_status=raw)
        return get_invoice(inv_id)
    if status:
        _mark(inv_id, raw_status=raw)
    return get_invoice(inv_id)


def _activate(inv: dict, reference: str) -> dict:
    """Terminal paid -> activated transition + plan extension, exactly once."""
    now = time.time()
    with _conn() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM billing_invoices WHERE id=?", (inv["id"],)).fetchone()
        if not row or row["status"] in {"activated", "paid"}:
            db.execute("COMMIT")
            return get_invoice(inv["id"])
        if row["status"] not in {"pending", "creating", "cancelled"}:
            db.execute("COMMIT")
            return get_invoice(inv["id"])
        plan = PLANS.get(str(row["plan"]))
        if not plan:
            db.execute("UPDATE billing_invoices SET status='mismatch', raw_status='unknown plan' WHERE id=?",
                       (inv["id"],))
            db.execute("COMMIT")
            return get_invoice(inv["id"])
        user = db.execute("SELECT plan, plan_until FROM users WHERE id=?", (row["user_id"],)).fetchone()
        current_until = float((user or {})["plan_until"] or 0) if user else 0.0
        requested_code = str(row["plan"])
        current_code = str((user or {})["plan"] or "").strip().lower() if user else ""
        if (current_code in PLANS and current_until > now
                and PLAN_RANK.get(current_code, 0) > PLAN_RANK.get(requested_code, 0)):
            # No-downgrade guard: a late lower-tier payment must never drop an
            # active higher paid plan; keep the higher plan and extend it.
            LOGGER.warning("billing no-downgrade guard: invoice=%s requested=%s kept=%s",
                           inv["id"], requested_code, current_code)
            plan = PLANS[current_code]
        if (row["kind"] if "kind" in row.keys() else "new") == "upgrade":
            # Prorated upgrade: Premium keeps the Standar end date (no extra days).
            # If Standar lapsed before payment, grant the paid days from now.
            plan = PLANS["premium"]
            new_until = current_until if current_until > now else now + int(row["upgrade_days"] or 0) * 86400
        else:
            base = max(current_until, now)  # never shorten a running paid period
            new_until = base + plan["days"] * 86400
        db.execute("UPDATE users SET plan=?, plan_until=? WHERE id=?", (plan["code"], new_until, row["user_id"]))
        db.execute(
            "UPDATE billing_invoices SET status='activated', paid_at=?, activated_at=?, reference=?, until_after=? WHERE id=?",
            (now, now, reference, new_until, inv["id"]),
        )
        # Commit the immutable receipt and mail intent with activation. A crash
        # cannot leave a paid user without a retryable email, or extend twice.
        receipt = dict(db.execute("SELECT * FROM billing_invoices WHERE id=?", (inv["id"],)).fetchone())
        customer = db.execute("SELECT email,name FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not customer:
            raise ValueError("Pemilik tagihan tidak ditemukan.")
        receipt = {key: receipt[key] for key in (
            "id", "user_id", "status", "plan", "amount", "base_price", "kind", "upgrade_days",
            "created_at", "paid_at", "activated_at", "until_after", "reference", "trx_id", "qris_id")}
        snapshot = {"invoice": receipt, "user": dict(customer), "service_start":
                    now if receipt.get("kind") == "upgrade" else max(current_until, now)}
        db.execute(
            "INSERT INTO billing_invoice_emails(invoice_id,user_id,payload_json) VALUES(?,?,?)",
            (inv["id"], row["user_id"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
        )
        db.execute("COMMIT")
    result = get_invoice(inv["id"])
    # The host invoice worker renders the PDF and sends via Resend; never block
    # settlement on rendering or delivery. The old fire-and-forget mail is replaced.
    LOGGER.info("billing activated invoice=%s user=%s plan=%s until=%.0f",
                inv["id"], inv["user_id"], plan["code"], new_until)
    return result


def _send_confirmation_async(inv: dict) -> None:
    try:
        threading.Thread(target=_send_confirmation, args=(dict(inv),), daemon=True).start()
    except Exception:
        pass


def _send_confirmation(inv: dict) -> None:
    """Confirmation email via the existing Resend helper (best effort)."""
    try:
        with _conn() as db:
            user = db.execute(
                "SELECT email, name, plan, plan_until FROM users WHERE id=?", (inv["user_id"],)
            ).fetchone()
        if not user:
            return
        plan = PLANS.get(str(user["plan"])) or PLANS.get(str(inv["plan"])) or {"label": "Premium"}
        until_txt = _fmt_date_wib(float(user["plan_until"] or 0))
        amount_txt = "Rp " + format(int(inv["amount"]), ",d").replace(",", ".")
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from eva_email import send_plan_activated_async  # type: ignore
        send_plan_activated_async(str(user["email"]), str(user["name"] or ""),
                                  plan["label"], until_txt, amount_txt, str(inv["id"]))
    except Exception as exc:
        LOGGER.warning("billing confirmation email skipped: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

def _poller_loop() -> None:
    """Poll pending invoices. A per-tick flock keeps exactly one process polling;
    taking it per tick (not once) means a HUP'd web keeps polling even when the
    previous worker still holds the lock during graceful reload."""
    try:
        handle = open(POLLER_LOCK, "w")
    except OSError:
        handle = None  # cannot lock: poll anyway (idempotent), correctness first
    while True:
        owned = handle is None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
            except (BlockingIOError, OSError):
                owned = False
        if owned:
            try:
                ensure_schema()
                cutoff = time.time() - (PENDING_TTL + EXPIRE_GRACE)
                with _conn() as db:
                    rows = db.execute(
                        "SELECT id FROM billing_invoices WHERE status IN ('pending','creating','cancelled') "
                        "AND created_at>? ORDER BY created_at ASC LIMIT 50",
                        (cutoff,),
                    ).fetchall()
                for row in rows:
                    try:
                        check_invoice(row["id"])
                    except Exception:
                        pass
                    time.sleep(0.4)
            except Exception:
                LOGGER.exception("billing poller tick failed")
            try:
                reminder_sweep()
            except Exception:
                LOGGER.exception("billing reminder sweep failed")
        time.sleep(POLL_INTERVAL)


def start_poller() -> None:
    """Start the background poller thread once per process."""
    global _POLLER_STARTED
    with _LOCK:
        if _POLLER_STARTED:
            return
        _POLLER_STARTED = True
    ensure_schema()
    t = threading.Thread(target=_poller_loop, name="eva-billing-poller", daemon=True)
    t.start()
    LOGGER.info("billing poller started every %.0fs", POLL_INTERVAL)


def mark_paid_for_test(inv_id: str) -> dict:
    """Test helper: force the paid transition without the gateway (unit tests only)."""
    inv = get_invoice(inv_id)
    if not inv:
        raise ValueError("Invoice tidak ditemukan.")
    return _activate(inv, inv["id"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_billing(app) -> None:
    """Register the subscription page + JSON endpoints. Called from app.py."""
    from flask import g, jsonify, render_template, request

    @app.route("/langganan")
    def billing_page():
        return render_template("langganan.html")

    @app.route("/api/billing/status")
    def api_billing_status():
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        plan = user_plan(user)
        invoices = user_invoices(user["id"], 12)
        pending = next((i for i in invoices if i["status"] in ("pending", "creating")), None)
        return jsonify({
            "plan": plan,
            "plans": [{"code": c, "label": p["label"], "price": p["price"]} for c, p in PLANS.items()],
            "upgrade": upgrade_quote(user),
            "invoice": public_invoice(pending),
            "invoices": [public_invoice(i) for i in invoices],
        })

    @app.route("/api/billing/invoice/<invoice_id>.pdf")
    def api_billing_invoice_pdf(invoice_id):
        from flask import send_file
        import re
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        if not re.fullmatch(r"EVA-[A-F0-9]{16}", invoice_id):
            return jsonify({"error": "Invoice tidak ditemukan."}), 404
        inv = get_invoice(invoice_id)
        if not inv or inv["user_id"] != user["id"] or inv["status"] != "activated":
            return jsonify({"error": "Invoice tidak ditemukan."}), 404
        path = INVOICE_PDF_DIR / (invoice_id + ".pdf")
        if not path.is_file() or path.is_symlink():
            response = jsonify({"error": "Invoice sedang disiapkan. Coba lagi sebentar."})
            response.headers["Retry-After"] = "15"
            return response, 503
        response = send_file(path, mimetype="application/pdf", as_attachment=True,
                             download_name=invoice_id + ".pdf", conditional=False, etag=False)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/billing/trial", methods=["POST"])
    def api_billing_trial():
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        payload = request.get_json(silent=True)
        if payload is not None and not isinstance(payload, dict):
            return jsonify({"error": "Data permintaan tidak valid."}), 400
        try:
            plan = start_trial(user["id"])
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"plan": plan})

    @app.route("/api/billing/checkout", methods=["POST"])
    def api_billing_checkout():
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Data permintaan tidak valid."}), 400
        code = str(payload.get("plan") or "").strip().lower()
        if code not in PLANS:
            return jsonify({"error": "Paket tidak dikenal."}), 400
        # Reuse a very recent pending invoice for the same plan (double-click safe).
        try:
            recent = next((i for i in user_invoices(user["id"], 5)
                           if i["status"] in ("pending", "creating")
                           and i["plan"] == code
                           and time.time() - float(i["created_at"] or 0) < 120), None)
        except Exception:
            recent = None
        if recent:
            return jsonify({"invoice": public_invoice(recent)}), 200
        try:
            inv = create_invoice(user, code)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            LOGGER.exception("billing checkout failed")
            return jsonify({"error": "Tagihan belum bisa dibuat. Coba lagi sebentar lagi."}), 503
        return jsonify({"invoice": public_invoice(inv)}), 201

    @app.route("/api/billing/payment/<invoice_id>")
    def api_billing_payment(invoice_id):
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        inv = get_invoice(str(invoice_id)[:64])
        if not inv or inv["user_id"] != user["id"]:
            return jsonify({"error": "Tagihan tidak ditemukan."}), 404
        if inv["status"] in ("pending", "creating"):
            try:
                inv = check_invoice(inv["id"])
            except Exception:
                pass
        # Re-read the user row: a poll that just activated the plan must report
        # the new masa aktif, not the copy loaded at request start.
        fresh = _load_user(user["id"]) or user
        return jsonify({"invoice": public_invoice(inv), "plan": user_plan(dict(fresh))})

    @app.route("/api/billing/cancel/<invoice_id>", methods=["POST"])
    def api_billing_cancel(invoice_id):
        user = getattr(g, "eva_user", None)
        if not user:
            return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
        inv = get_invoice(str(invoice_id)[:64])
        if not inv or inv["user_id"] != user["id"]:
            return jsonify({"error": "Tagihan tidak ditemukan."}), 404
        if inv["status"] in ("pending", "creating"):
            # Final check first: never cancel an invoice that just got paid.
            try:
                inv = check_invoice(inv["id"])
            except Exception:
                pass
            if inv["status"] in ("pending", "creating"):
                # The poller keeps watching cancelled invoices; a payment that
                # still arrives is honoured and activates the plan.
                _mark(inv["id"], status="cancelled")
                inv = get_invoice(inv["id"])
        return jsonify({"invoice": public_invoice(inv)})

    # Only the web process runs the poller; celery workers import this module
    # through app.py and must not poll (or log) on their own.
    if "celery" not in " ".join(sys.argv).lower():
        try:
            start_poller()
        except Exception:
            LOGGER.exception("billing poller start failed")
