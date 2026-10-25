"""Standalone ASTRI keepalive; execute native auth helpers, never import app."""
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import urllib.parse
import uuid

import requests

AUTH_HELPERS = {
    "AstriUnavailable", "AstriCredentialsRejected", "astri_user_lock",
    "_restore_astri_cookies", "_get_astri_cipher", "_encrypt_astri_password",
    "_decrypt_astri_password", "load_astri_session_store", "update_astri_session_store",
    "persist_astri_session", "_astri_login_page", "_astri_challenge_page",
    "_astri_timeout", "_astri_liveness_timeout", "_astri_session_state",
    "_astri_session_probe", "_astri_login_http_session", "_try_auto_relogin",
}
AUTH_CONSTANTS = {
    "ASTRI_BASE_URL", "ASTRI_SESSION_STORE_PATH", "ASTRI_LOGIN_TIMEOUT",
    "ASTRI_SESSION_TIMEOUT", "ASTRI_LOGIN_ATTEMPTS", "ASTRI_LOGIN_RETRY_DELAY",
}
STATES = ("connected", "unavailable", "reconnecting", "credentials_missing",
          "credentials_invalid", "expired")
BACKOFF_WINDOWS = (3600, 21600, 86400)


def _state_path():
    """Resolved per call so tests and systemd runs can point at private paths."""
    return Path(os.environ.get("KEEPALIVE_STATE_PATH") or "/app/data/astri_keepalive_state.json")


def _cipher_id(entry):
    """Opaque fingerprint of the stored secret; detects a user password update."""
    value = entry.get("encrypted_password") if isinstance(entry, dict) else None
    return hashlib.sha256(str(value or "").encode()).hexdigest() if value else ""


def _load_state():
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    """Persist only when there is something to remember; never create an empty file."""
    path = _state_path()
    if not state and not path.exists():
        return
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x") as stream:
            os.chmod(temp, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class ReadOnlyAuth:
    def __init__(self, path):
        self.path = Path(path)
        self.expected = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    def links(self):
        with self.connect() as db:
            rows = db.execute("""
                SELECT l.user_id, l.username, l.session_id FROM astri_links l
                JOIN users u ON u.id=l.user_id AND u.active=1
                JOIN resources r ON r.kind='astri' AND r.reference=l.session_id
                                AND r.user_id=l.user_id
                ORDER BY l.user_id
            """).fetchall()
        return [dict(row) for row in rows]

    def resource_owner(self, kind, reference):
        if kind != "astri":
            return None
        with self.connect() as db:
            row = db.execute("""
                SELECT l.user_id, l.username, l.session_id FROM astri_links l
                JOIN users u ON u.id=l.user_id AND u.active=1
                JOIN resources r ON r.kind='astri' AND r.reference=l.session_id
                                AND r.user_id=l.user_id
                WHERE l.session_id=?
            """, (reference,)).fetchone()
        if not row or self.expected is not None and dict(row) != self.expected:
            return None
        return row["user_id"]

    def user_active(self, user_id):
        with self.connect() as db:
            row = db.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(row and row["active"] == 1)


def load_native(app_path=Path("/app/app.py"), db_path=Path("/app/data/users.db"), store_path=None):
    tree = ast.parse(Path(app_path).read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in AUTH_HELPERS]
    if {node.name for node in nodes} != AUTH_HELPERS or len(nodes) != len(AUTH_HELPERS):
        raise RuntimeError("Native ASTRI auth helpers unavailable")
    constants = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in AUTH_CONSTANTS:
                value = node.value
                if target.id == "ASTRI_SESSION_STORE_PATH":
                    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "Path":
                        raise RuntimeError("Native ASTRI store path unavailable")
                    constants[target.id] = Path(ast.literal_eval(value.args[0]))
                else:
                    constants[target.id] = ast.literal_eval(value)
    if constants.keys() != AUTH_CONSTANTS:
        raise RuntimeError("Native ASTRI constants unavailable")
    base = urllib.parse.urlsplit(constants["ASTRI_BASE_URL"])
    if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment:
        raise RuntimeError("Native ASTRI origin invalid")
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        try:
            secret = Path(os.environ.get("SECRET_KEY_FILE") or "/run/secrets/eva_secret_key").read_text().strip()
        except OSError:
            raise RuntimeError("ASTRI encryption key unavailable") from None
    if len(secret) < 32 or secret == "change-me-in-production":
        raise RuntimeError("ASTRI encryption key invalid")
    logger = logging.Logger("astri-keepalive-native", level=logging.CRITICAL)
    logger.disabled = True  # Native warnings contain identifiers; aggregate output only.
    ns = dict(ast=ast, contextmanager=contextmanager, fcntl=fcntl, json=json,
              os=os, Path=Path, re=re, time=time, urllib=urllib, uuid=uuid,
              requests=requests, Config=SimpleNamespace(SECRET_KEY=secret), LOGGER=logger,
              EVA_AUTH=SimpleNamespace(store=ReadOnlyAuth(db_path)), ASTRI_SESSIONS={})
    ns.update(constants)
    if store_path is not None:
        ns["ASTRI_SESSION_STORE_PATH"] = Path(store_path)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(app_path), "exec"), ns)
    if ns["_get_astri_cipher"]() is None:
        raise RuntimeError("ASTRI encryption key invalid")
    return ns


def run_once(ns, now=None):
    now = time.time() if now is None else now
    report = dict.fromkeys(STATES, 0)
    report["timestamp"] = now
    report["total"] = 0
    report["renewed"] = 0
    report["backoff"] = 0
    backoff_state = _load_state()
    auth = ns["EVA_AUTH"].store
    try:
        for link in auth.links():
            auth.expected = link
            report["total"] += 1
            state = "unavailable"
            record = None
            stored = None
            with ns["astri_user_lock"](link["user_id"], wait=False) as acquired:
                if not acquired:
                    state = "reconnecting"
                elif auth.resource_owner("astri", link["session_id"]) == link["user_id"]:
                    stored = ns["load_astri_session_store"]().get(link["session_id"])
                    if (isinstance(stored, dict) and stored.get("owner_user_id") == link["user_id"]
                            and stored.get("username") == link["username"]):
                        with requests.Session() as sess:
                            sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
                            ns["_restore_astri_cookies"](sess, stored.get("cookies"))
                            record = {"session": sess, "username": link["username"],
                                      "_stored": stored, "_session_id": link["session_id"]}
                            result = ns["_astri_session_state"](record, deadline=time.monotonic() + 45)
                            if result in STATES:
                                state = result
            # Renewal runs after the probe lock is released: the native re-login
            # helper takes the same per-user lock itself, so holding it here would
            # make every renewal bounce as "reconnecting".
            if state == "expired" and record is not None:
                sid = link["session_id"]
                entry = backoff_state.get(sid) or {}
                cipher = _cipher_id(stored)
                suppressed = (entry.get("cipher") == cipher and isinstance(entry.get("next_attempt"), (int, float))
                              and now < entry["next_attempt"])
                if suppressed:
                    # A rejected password is not retried on every tick; the wait
                    # grows so a changed credential cannot lock the ASTRI account.
                    state = "credentials_invalid"
                    report["backoff"] += 1
                else:
                    refreshed = ns["_try_auto_relogin"](record, sid=sid,
                                                        deadline=time.monotonic() + 45)
                    if refreshed is not None:
                        state = "connected"
                        report["renewed"] += 1
                        backoff_state.pop(sid, None)
                        _save_state(backoff_state)
                    else:
                        reason = record.get("_auth_state")
                        state = reason if reason in STATES else "unavailable"
                        if state == "credentials_invalid":
                            failures = int(entry.get("failures") or 0) + 1
                            window = BACKOFF_WINDOWS[min(failures - 1, len(BACKOFF_WINDOWS) - 1)]
                            backoff_state[sid] = {"failures": failures, "next_attempt": now + window,
                                                  "cipher": cipher}
                            _save_state(backoff_state)
            report[state] += 1
    finally:
        auth.expected = None
    return report


def main():
    ns = load_native(
        app_path=Path(os.environ.get("KEEPALIVE_APP_PATH") or "/app/app.py"),
        db_path=Path(os.environ.get("KEEPALIVE_DB_PATH") or "/app/data/users.db"),
        store_path=os.environ.get("KEEPALIVE_STORE_PATH") or None,
    )
    print(json.dumps(run_once(ns), sort_keys=True))


if __name__ == "__main__":
    main()
