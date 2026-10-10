import csv
import difflib
import fcntl
import html
from decimal import Decimal, InvalidOperation
import io
import json
import logging
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import kmz_standardizer
from kmz_standardizer import RULES_CONFIG_PATH, load_rules_config

import region_db

from flask import Flask, Response, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, stream_with_context, url_for, make_response
from flask.sessions import SecureCookieSessionInterface
import requests
from werkzeug.exceptions import RequestEntityTooLarge

from astri_rfs_documents import (
    DOCUMENT_ABD_EXTENSION,
    DOCUMENT_ABD_MAX_BYTES,
    DOCUMENT_ABD_MIME,
    DOCUMENT_ABD_SCOPES,
    DOCUMENT_ABD_TYPE_NAME,
    apply_document_abd_editor_values,
    build_document_abd_editor,
    document_abd_editor_matches,
    document_abd_scope,
    parse_document_abd_edit_form,
    parse_document_page,
    parse_rfs_search_rows,
    public_document_abd_editor,
    validate_kmz_upload,
)
from werkzeug.utils import secure_filename

from config import Config, validate_production_secrets
from logging_config import clear_request_id, set_request_id, setup_logging
from alerts import alert_job_failed, alert_server_unhealthy
from astri_boq_submissions import RedisBoqSubmissionStore, SubmissionConflict
from astri_boq_reconcile import (
    build_boq_overwrite_actions,
    fetch_boq_reconciliation,
    parse_boq_records,
)
from cleanup import start_cleanup_scheduler
from job_queue import enqueue_conversion_job
from boq import (
    BOQ_NRO_SHEET_NAME,
    BOQ_SHEET_NAME,
    calculate_dxf_boq_design_summary_values,
    extract_cluster_name_from_kmz_filename,
    generate_boq_excel,
)
from converter import canonical_standard_folder, convert_osm_to_dxf, road_cache_is_fresh, road_cache_path_for_pbf, validate_standard_kml_folders
import coreman_db
import coreman_sheets
import rfs_sheets
import gmail_rfs_ingest
import mancore_service
import ai_assistant
import fdt_coding_db
from hpdb import (
    HPDB_CLUSTER_SHEET_NAME,
    HPDB_FAT_SHEET_NAME,
    HPDB_HOMEPASS_SHEET_NAME,
    HPDB_MAX_FDT,
    generate_astri_excel,
    write_astri_output_zip,
)
from hpdb_mancore import generate_hpdb_excel
from jobs import (
    acquire_job_slot,
    cleanup_jobs,
    create_job as _system_create_job,
    create_job_logger,
    delete_job,
    get_job as _system_get_job,
    list_jobs as _system_list_jobs,
    refresh_job_slot,
    release_job_slot,
    recover_stale_jobs,
    update_job as _system_update_job,
)
from eva_r2_storage import publish_job_output, job_download_url
from kml_parser import iter_placemarks, load_kml_root
from kmz_standardizer import download_coding_workbook, standardize_kmz_file
import fdt_sync_service
import stella_connector
import stella_export_jobs
from metrics import (
    metrics_snapshot,
    record_job_completed,
    record_job_created,
    record_job_failed,
    record_job_started,
    record_upload_file,
)

try:
    import prometheus_metrics
except Exception:
    prometheus_metrics = None

setup_logging(
    Config.LOG_LEVEL,
    json_output=True if Config.LOG_FORMAT == "json" else False if Config.LOG_FORMAT in {"text", "human"} else None,
)

DEFAULT_CONVERT_LAYERS = ["ROAD", "KML"]
APP_VERSION = getattr(Config, "APP_VERSION", "1.0.0")
if prometheus_metrics:
    prometheus_metrics.set_app_info(APP_VERSION)
OUTPUT_MODE_DXF = "dxf"
OUTPUT_MODE_BOQ = "boq"
OUTPUT_MODE_ASTRI = "astri"
OUTPUT_MODE_HPDB = "hpdb"
OUTPUT_MODE_KMZ = "kmz"
OUTPUT_MODE_DXF_BOQ = "dxf_boq"
OUTPUT_MODE_DXF_HPDB = "dxf_hpdb"
OUTPUT_MODE_BOQ_HPDB = "boq_hpdb"
OUTPUT_MODE_DXF_BOQ_HPDB = "dxf_boq_hpdb"
DEFAULT_OUTPUT_MODE = OUTPUT_MODE_DXF if Config.DXF_CONVERSION_ENABLED else OUTPUT_MODE_BOQ
OUTPUT_TARGET_ORDER = [OUTPUT_MODE_DXF, OUTPUT_MODE_BOQ, OUTPUT_MODE_ASTRI, OUTPUT_MODE_HPDB, OUTPUT_MODE_KMZ]
OUTPUT_MODES = {
    "_".join(target for index, target in enumerate(OUTPUT_TARGET_ORDER) if mask & (1 << index))
    for mask in range(1, 1 << len(OUTPUT_TARGET_ORDER))
}
NETWORK_TYPE_AE = "AE"
NETWORK_TYPE_UG = "UG"
NETWORK_TYPES = set(Config.NETWORK_TYPES)
NETWORK_TYPE_LABELS = {
    NETWORK_TYPE_AE: "AE / Aerial",
    NETWORK_TYPE_UG: "UG / Underground",
}
SCOPE_TYPE_CLUSTER = "Cluster"
SCOPE_TYPE_FEEDER = "Feeder"
SCOPE_TYPE_SUBFEEDER = "Subfeeder"
SCOPE_TYPES = set(Config.SCOPE_TYPES)
SCOPE_TYPE_LABELS = {
    SCOPE_TYPE_CLUSTER: "Cluster",
    SCOPE_TYPE_FEEDER: "Feeder",
    SCOPE_TYPE_SUBFEEDER: "Subfeeder",
}
FEEDER_LIKE_SCOPE_TYPES = {SCOPE_TYPE_FEEDER, SCOPE_TYPE_SUBFEEDER}
FEEDER_ALLOWED_TARGETS = {"dxf", "boq", "kmz"}
BOQ_STATS_KEYS = {
    "BOQ_ITEMS": "boq_items",
    "BOQ_WARNINGS": "boq_warnings",
    "FDT": "fdt",
    "FAT": "fat",
    "POLE": "pole",
    "CABLE_LENGTH": "cable_length",
    "SLING_LENGTH": "sling_length",
}
HPDB_STATS_KEYS = {
    "HPDB_ROWS": "hpdb_rows",
    "HPDB_WARNINGS": "hpdb_warnings",
    "HPDB_FDT": "hpdb_fdt",
    "HPDB_FAT": "hpdb_fat",
    "HPDB_HOMEPASS": "hpdb_homepass",
    "HP_COVER": "hp_cover",
    "HP_UNCOVER": "hp_uncover",
}


TERMINAL_JOB_STATUSES = {"done", "error", "failed", "cancelled"}


class JobCancelled(Exception):
    """Raised internally when a queued/running job has been cancelled."""


LOGGER = logging.getLogger(__name__)

DASHBOARD_HOSTS = {
    "dashboard.dyena.id",
    "dashboard.eva-mrdesign.my.id",
}


def request_hostname() -> str:
    return request.host.partition(":")[0].strip().lower()


def session_cookie_domain_for_host(host: str) -> str | None:
    # Host-only cookies prevent sibling subdomains from inheriting EVA sessions.
    return None


class EvaSessionInterface(SecureCookieSessionInterface):
    def get_cookie_domain(self, app):
        return session_cookie_domain_for_host(request.host)


def admin_api_token_required() -> bool:
    return bool(Config.REQUIRE_ADMIN_API_TOKEN and Config.ADMIN_API_TOKEN)


def fdt_session_signing_key_secure() -> bool:
    """Validate the actual Flask signing key at the authorization boundary."""
    key = str(Config.SECRET_KEY or "").strip()
    lowered = key.casefold()
    weak_markers = (
        "change-me",
        "changeme",
        "placeholder",
        "your-secret",
        "default-secret",
        "development-secret",
        "example-secret",
        "supersecret",
    )
    if len(key) < 32 or any(marker in lowered for marker in weak_markers):
        return False
    if len(set(key)) < 8:
        return False
    # Reject repeated motifs such as "s" * 32 or "abcd" * 8.
    for width in range(1, min(16, len(key) // 2) + 1):
        if len(key) % width == 0 and key == key[:width] * (len(key) // width):
            return False
    return True


def require_fdt_write_access():
    if getattr(g, "eva_user", None):
        return None
    return jsonify({"error": "Login Google diperlukan."}), 401


def require_admin_api_access():
    user = getattr(g, "eva_user", None)
    if not user:
        return jsonify({"error": "Login Google diperlukan."}), 401
    if request.path.startswith(("/api/dashboard/", "/admin")) and user["role"] != "owner":
        return jsonify({"error": "Dashboard hanya untuk owner EVA."}), 403
    return None



def dashboard_admin_password() -> str:
    data_path = Config.BASE_DIR / "data" / "dashboard_admin_password.txt"
    if data_path.exists():
        return data_path.read_text(encoding="utf-8").strip()
    path = Config.BASE_DIR / "config" / "dashboard_admin_password.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    pw = os.environ.get("DASHBOARD_ADMIN_PASSWORD", "")
    if not pw:
        raise RuntimeError("Dashboard admin password not configured")
    return pw

def set_dashboard_admin_password(password: str) -> None:
    path = Config.BASE_DIR / "data" / "dashboard_admin_password.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)

def dashboard_admin_required():
    user = getattr(g, "eva_user", None)
    if user and user.get("role") == "owner":
        return None
    return jsonify({"error": "Dashboard hanya untuk owner EVA."}), 403

def is_dashboard_host() -> bool:
    return request_hostname() in DASHBOARD_HOSTS

def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())

def is_safe_output_path(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    allowed_roots = {
        Config.OUTPUT_DIR.resolve(),
        Config.BOQ_OUTPUT_DIR.resolve(),
        Config.HPDB_OUTPUT_DIR.resolve(),
    }
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False

def init_sentry() -> bool:
    if not Config.SENTRY_DSN:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        LOGGER.warning("SENTRY_DSN diset, tetapi sentry-sdk belum terpasang. Sentry dilewati.")
        return False

    sentry_sdk.init(
        dsn=Config.SENTRY_DSN,
        integrations=[FlaskIntegration()],
        release=Config.RELEASE_VERSION,
        environment=Config.ENVIRONMENT,
        traces_sample_rate=0.1,
    )
    return True


init_sentry()

app = Flask(__name__)
app.config.from_object(Config)
app.session_interface = EvaSessionInterface()
app.config.update(
    SESSION_COOKIE_NAME="__Host-eva-session",
    SESSION_COOKIE_DOMAIN=None,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    EVA_OWNER_EMAILS=["dwi.yulianto@morarepublic.co.id", "dwiyulianto1395@gmail.com"],
)
from eva_auth import register_auth
EVA_AUTH = register_auth(app)


def create_job(job_id, *args, **kwargs):
    return EVA_AUTH.create_job(_system_create_job, job_id, *args, **kwargs)


def update_job(job_id, **changes):
    if changes.get("status") == "done":
        job = _system_get_job(job_id)
        if job:
            changes.update(publish_job_output({**job, **changes}))
            current = _system_get_job(job_id)
            if not current or current.get("status") == "cancelled":
                return current
    return _system_update_job(job_id, **changes)


def get_job(job_id):
    return EVA_AUTH.get_job(_system_get_job, job_id)


def list_jobs(limit=50):
    return EVA_AUTH.list_jobs(_system_list_jobs, limit)


for secret_warning in validate_production_secrets():
    LOGGER.warning(secret_warning)
if Config.ENFORCE_PRODUCTION_SECRETS:
    secret_errors = [
        warning for warning in validate_production_secrets()
        if "SECRET_KEY" in warning or warning.startswith("CRITICAL:")
    ]
    if secret_errors:
        raise RuntimeError("Production secret validation failed: " + " | ".join(secret_errors))


@app.before_request
def enforce_fdt_route_family_write_access():
    if request.path.startswith("/api/fdt-coding/") and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        return require_fdt_write_access()
    return None


@app.before_request
def attach_request_context() -> None:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.request_id = request_id
    g.request_started_at = time.perf_counter()
    set_request_id(request_id)


@app.after_request
def attach_request_id_header(response):
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if request.path in EVA_AUTH.public_assets and response.status_code < 400:
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "private, no-store"
        response.vary.add("Cookie")
    if prometheus_metrics:
        endpoint = request.endpoint or "unknown"
        duration = time.perf_counter() - float(getattr(g, "request_started_at", time.perf_counter()))
        prometheus_metrics.http_requests_total.labels(request.method, endpoint, str(response.status_code)).inc()
        prometheus_metrics.http_request_duration_seconds.labels(request.method, endpoint).observe(duration)
    return response


@app.teardown_request
def clear_logging_context(_error) -> None:
    clear_request_id()


def create_app() -> Flask:
    """Return Flask app instance.

    App factory ini menjaga compatibility untuk Gunicorn/tests sambil memberi entrypoint
    yang siap dipakai saat route legacy dipindah ke Blueprint bertahap.
    """
    return app


ASTRI_BASE_URL = "https://astri.morarepublic.co.id"
ASTRI_SESSION_KEY = "astri_session_id"
ASTRI_SESSIONS: dict[str, dict] = {}
ASTRI_SESSION_STORE_PATH = Path("/app/data/astri_sessions.json")
ASTRI_LOGIN_TIMEOUT = 60
ASTRI_SESSION_TIMEOUT = 30
ASTRI_LOGIN_ATTEMPTS = 2
ASTRI_LOGIN_RETRY_DELAY = 1.0
_ASTRI_BOQ_SUBMISSION_STORE: RedisBoqSubmissionStore | None = None


def get_astri_boq_submission_store() -> RedisBoqSubmissionStore:
    global _ASTRI_BOQ_SUBMISSION_STORE
    if _ASTRI_BOQ_SUBMISSION_STORE is None:
        _ASTRI_BOQ_SUBMISSION_STORE = RedisBoqSubmissionStore(
            Config.REDIS_URL,
            prefix=Config.JOB_STORE_KEY_PREFIX,
            ttl_seconds=Config.JOB_STORE_TTL_SECONDS,
        )
    return _ASTRI_BOQ_SUBMISSION_STORE


def _restore_astri_cookies(sess: requests.Session, cookies: object) -> None:
    host = urllib.parse.urlsplit(ASTRI_BASE_URL).hostname or ""
    if isinstance(cookies, dict):
        cookies = [{"name": k, "value": v, "domain": host, "path": "/", "secure": True}
                   for k, v in cookies.items()]
    if not isinstance(cookies, list):
        return
    for item in cookies:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        domain = str(item.get("domain") or host)
        if domain.lstrip(".") != host:
            continue
        kwargs = {
            "domain": domain, "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)), "rest": item.get("rest") or {},
        }
        if isinstance(item.get("expires"), (int, float)):
            kwargs["expires"] = int(item["expires"])
        sess.cookies.set(item["name"], str(item.get("value") or ""), **kwargs)


def astri_session_record_by_id(sid: str, user_id: str = "", expected_username: str = "") -> dict:
    """Verify durable EVA creator binding before touching upstream credentials."""
    if (not user_id or EVA_AUTH.store.resource_owner("astri", sid) != user_id
            or not EVA_AUTH.store.user_active(user_id)):
        raise LookupError("Sesi ASTRI tidak dimiliki pengirim.")
    stored = load_astri_session_store().get(str(sid or ""))
    if (not isinstance(stored, dict) or not stored.get("cookies")
            or stored.get("owner_user_id") != user_id):
        raise LookupError("Sesi ASTRI pengirim tidak tersedia. Login ulang lalu submit kembali.")
    if expected_username and str(stored.get("username") or "").strip() != expected_username:
        raise LookupError("Akun ASTRI berbeda dari akun yang ditangkap pekerjaan.")
    sess = requests.Session()
    sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
    _restore_astri_cookies(sess, stored.get("cookies"))
    record = {
        "session": sess,
        "username": stored.get("username", ""),
        "created_at": stored.get("created_at", time.time()),
        "_stored": stored,
        "_session_id": str(sid or ""),
    }
    if not _astri_session_alive(record):
        refreshed = _try_auto_relogin(record, sid=str(sid or ""))
        if refreshed:
            return refreshed
        raise LookupError("Sesi ASTRI pengirim sudah berakhir. Login ulang lalu submit kembali.")
    return record

# Crypto helpers for storing ASTRI password (encrypted with SECRET_KEY)
def _get_astri_cipher():
    try:
        from cryptography.fernet import Fernet
        import base64
        secret = os.environ.get("SECRET_KEY") or Config.SECRET_KEY
        if not isinstance(secret, str) or len(secret) < 32 or secret == "change-me-in-production":
            return None
        # Preserve the existing key derivation so valid stored sessions survive.
        key = base64.urlsafe_b64encode(secret.encode()[:32])
        return Fernet(key)
    except Exception:
        return None

def _encrypt_astri_password(password: str) -> str | None:
    cipher = _get_astri_cipher()
    if not cipher:
        return None
    try:
        return cipher.encrypt(password.encode()).decode()
    except Exception:
        return None

def _decrypt_astri_password(encrypted: str) -> str | None:
    cipher = _get_astri_cipher()
    if not cipher or not encrypted:
        return None
    try:
        return cipher.decrypt(encrypted.encode()).decode()
    except Exception:
        return None
ASTRI_UPLOAD_TARGETS = {
    "kmz": {
        "type_name": "as_plan_drawing_kmz",
        "allowed_file_type": "kmz",
        "job_path_key": "standard_kmz_outputs",
    },
    "boq": {
        "type_name": "build_of_quantity_as_plan_drawing",
        "allowed_file_type": "xls, xlsx",
        "job_path_key": "boq_output_path",
        "job_name_key": "boq_download_name",
    },
    "hpdb": {
        "type_name": "homepass_database",
        "allowed_file_type": "xls, xlsx",
        "job_path_key": "hpdb_output_path",
        "job_name_key": "hpdb_download_name",
    },
}

FEEDER_UPLOAD_TARGETS = {
    "kmz": {
        "type_name": "as_plan_drawing_feeder_kmz",
        "allowed_file_type": "kmz",
        "job_path_key": "standard_kmz_outputs",
    },
    "boq": {
        "type_name": "build_of_quantity_feeder_as_plan_drawing",
        "allowed_file_type": "xls, xlsx",
        "job_path_key": "boq_output_path",
        "job_name_key": "boq_download_name",
    },
}

SUBFEEDER_UPLOAD_TARGETS = {
    "kmz": {
        "type_name": "as_plan_drawing_subfeeder_kmz",
        "allowed_file_type": "kmz",
        "job_path_key": "standard_kmz_outputs",
    },
    "boq": {
        "type_name": "build_of_quantity_subfeeder_as_plan_drawing",
        "allowed_file_type": "xls, xlsx",
        "job_path_key": "boq_output_path",
        "job_name_key": "boq_download_name",
    },
}

def normalize_match_text(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()

@contextmanager
def astri_user_lock(user_id: str):
    import hashlib
    lock_dir = ASTRI_SESSION_STORE_PATH.parent / "astri_user_locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = lock_dir / (hashlib.sha256(str(user_id).encode()).hexdigest() + ".lock")
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def astri_session_id() -> str:
    sid = session.get(ASTRI_SESSION_KEY)
    if not sid:
        sid = secrets.token_urlsafe(24)
        session[ASTRI_SESSION_KEY] = sid
    return sid

def load_astri_session_store() -> dict:
    try:
        if ASTRI_SESSION_STORE_PATH.exists():
            return json.loads(ASTRI_SESSION_STORE_PATH.read_text())
    except Exception:
        LOGGER.warning("Gagal membaca ASTRI session store", exc_info=True)
    return {}

def save_astri_session_store(store: dict) -> None:
    try:
        ASTRI_SESSION_STORE_PATH.write_text(json.dumps(store))
        ASTRI_SESSION_STORE_PATH.chmod(0o600)
    except Exception:
        LOGGER.warning("Gagal menyimpan ASTRI session store", exc_info=True)

def update_astri_session_store(sid: str, entry: dict | None) -> None:
    """Serialize record updates across processes without replacing stale snapshots."""
    path = ASTRI_SESSION_STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        store = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(store, dict):
            raise ValueError("ASTRI session store rusak; tidak ditimpa.")
        if entry is None:
            store.pop(sid, None)
        else:
            existing = store.get(sid)
            if existing is not None and (not isinstance(existing, dict)
                    or any(existing.get(key) != entry.get(key) for key in ("username", "owner_user_id"))):
                raise LookupError("Identitas sesi ASTRI tidak boleh berubah; gunakan login baru.")
            store[sid] = entry
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x") as stream:
                os.chmod(temp, 0o600)
                json.dump(store, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def persist_astri_session(sid: str, username: str, sess: requests.Session, password: str = "") -> dict:
    owner_user_id = EVA_AUTH.store.resource_owner("astri", sid)
    if not owner_user_id or not EVA_AUTH.store.user_active(owner_user_id):
        raise LookupError("Sesi ASTRI tidak dimiliki pengguna aktif.")
    entry = {
        "username": username,
        "created_at": time.time(),
        "owner_user_id": owner_user_id,
        "cookies": [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                     "secure": c.secure, "expires": c.expires, "rest": c._rest}
                    for c in sess.cookies],
    }
    if password:
        encrypted = _encrypt_astri_password(password)
        if not encrypted:
            raise ValueError("Penyimpanan login ASTRI terenkripsi belum siap.")
        entry["encrypted_password"] = encrypted
    update_astri_session_store(sid, entry)
    return entry

def current_astri_session() -> dict | None:
    user = getattr(g, "eva_user", None)
    if not user:
        session.pop(ASTRI_SESSION_KEY, None)
        return None
    linked = EVA_AUTH.store.astri_link(user["id"])
    sid = linked["session_id"] if linked else session.get(ASTRI_SESSION_KEY, "")
    if not sid or not EVA_AUTH.store.can_access("astri", sid, user):
        session.pop(ASTRI_SESSION_KEY, None)
        return None
    stored = load_astri_session_store().get(sid)
    if (not isinstance(stored, dict) or stored.get("owner_user_id") != user["id"]
            or linked and stored.get("username") != linked["username"]
            or not stored.get("cookies")):
        ASTRI_SESSIONS.pop(sid, None)
        session.pop(ASTRI_SESSION_KEY, None)
        return None
    session[ASTRI_SESSION_KEY] = sid
    record = ASTRI_SESSIONS.get(sid)
    if record:
        if str(record.get("username") or "") != str(stored.get("username") or ""):
            ASTRI_SESSIONS.pop(sid, None)
            return None
        if record.get("_stored") == stored:
            return record
    sess = requests.Session()
    sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
    _restore_astri_cookies(sess, stored.get("cookies"))
    record = {
        "session": sess,
        "username": stored.get("username", ""),
        "created_at": stored.get("created_at", time.time()),
        "_stored": stored,
        "_session_id": str(sid),
    }
    ASTRI_SESSIONS[sid] = record
    return record

def _astri_session_alive(record: dict) -> bool:
    username = str(record.get("username") or "").strip()
    if not username:
        return False
    base = urllib.parse.urlsplit(ASTRI_BASE_URL)
    target = f"{ASTRI_BASE_URL}/user"
    try:
        for _ in range(3):
            response = record["session"].get(target, timeout=ASTRI_SESSION_TIMEOUT, allow_redirects=False)
            destination = urllib.parse.urlsplit(str(getattr(response, "url", "") or target))
            if (destination.scheme != base.scheme or destination.netloc != base.netloc
                    or destination.path.rstrip("/") not in {"/user", "/setting/user/update"}):
                return False
            if response.status_code in {301, 302, 303}:
                location = str(response.headers.get("Location") or "")
                if not location:
                    return False
                target = urllib.parse.urljoin(target, location)
                redirect = urllib.parse.urlsplit(target)
                if (redirect.scheme != base.scheme or redirect.netloc != base.netloc
                        or redirect.path.rstrip("/") != "/setting/user/update"):
                    return False
                continue
            if response.status_code != 200:
                return False
            text = str(getattr(response, "text", "") or "")
            if re.search(r'<input\b[^>]*(?:type\s*=\s*["\']password|name\s*=\s*["\']user-identity)', text, re.I):
                return False
            return bool(re.search(r"(?<![\w.@-])" + re.escape(username) + r"(?![\w.@-])", text, re.I))
        return False
    except requests.RequestException:
        return False


def _astri_login_http_session(username: str, password: str) -> requests.Session:
    last_error: Exception | None = None
    for attempt in range(ASTRI_LOGIN_ATTEMPTS):
        sess = requests.Session()
        sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
        try:
            sess.get(f"{ASTRI_BASE_URL}/", timeout=ASTRI_LOGIN_TIMEOUT)
            response = sess.post(
                f"{ASTRI_BASE_URL}/login-process",
                data={
                    "user-identity": username,
                    "password": password,
                    "csrf_name": "",
                    "csrf_value": "",
                },
                allow_redirects=True,
                timeout=ASTRI_LOGIN_TIMEOUT,
            )
            if response.status_code != 200:
                raise ValueError("Login ASTRI gagal. Cek username/password atau akses akun.")
            if not _astri_session_alive({"session": sess, "username": username}):
                raise ValueError("Login ASTRI gagal diverifikasi.")
            return sess
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < ASTRI_LOGIN_ATTEMPTS:
                time.sleep(ASTRI_LOGIN_RETRY_DELAY)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Login ASTRI gagal tanpa hasil.")


def _astri_mutation_success_redirect(response: requests.Response | None) -> bool:
    if response is None or response.status_code not in {302, 303}:
        return False
    location = str(response.headers.get("Location") or "").strip()
    if not location:
        return False
    destination = urllib.parse.urlparse(urllib.parse.urljoin(f"{ASTRI_BASE_URL}/", location))
    base = urllib.parse.urlparse(ASTRI_BASE_URL)
    return (
        destination.scheme == base.scheme
        and destination.netloc == base.netloc
        and destination.path.rstrip("/") == "/success-process"
    )


def _try_auto_relogin(record: dict, sid: str = "") -> dict | None:
    stored = record.get("_stored") or {}
    resolved_sid = str(sid or record.get("_session_id") or "")
    owner_id = str(stored.get("owner_user_id") or "")
    username = str(record.get("username") or stored.get("username") or "")
    if not resolved_sid or not owner_id or not username:
        return None
    with astri_user_lock(owner_id):
        # Re-read after acquiring the per-user lock: unlink may have won first.
        if (EVA_AUTH.store.resource_owner("astri", resolved_sid) != owner_id
                or not EVA_AUTH.store.user_active(owner_id)):
            return None
        authoritative = load_astri_session_store().get(resolved_sid)
        if (not isinstance(authoritative, dict) or authoritative.get("owner_user_id") != owner_id
                or authoritative.get("username") != username):
            return None
        if authoritative != stored:
            # A sibling worker may have refreshed this account while we waited.
            refreshed_session = requests.Session()
            refreshed_session.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
            _restore_astri_cookies(refreshed_session, authoritative.get("cookies"))
            refreshed = {
                "session": refreshed_session, "username": username,
                "created_at": authoritative.get("created_at", time.time()),
                "_stored": authoritative, "_session_id": resolved_sid,
            }
            if _astri_session_alive(refreshed):
                ASTRI_SESSIONS[resolved_sid] = refreshed
                return refreshed
        encrypted = authoritative.get("encrypted_password")
        password = _decrypt_astri_password(encrypted) if encrypted else None
        if not password:
            return None
        try:
            sess = _astri_login_http_session(username, password)
            new_record = {
                "session": sess, "username": username, "created_at": time.time(),
                "_session_id": resolved_sid,
            }
            new_record["_stored"] = persist_astri_session(resolved_sid, username, sess, password=password)
            ASTRI_SESSIONS[resolved_sid] = new_record
            return new_record
        except Exception:
            # Never log exception bodies that may contain upstream secrets.
            LOGGER.warning("ASTRI auto re-login gagal untuk pengguna EVA %s", owner_id)
            return None

def require_astri_session() -> dict:
    record = current_astri_session()
    if not record:
        raise ValueError("ASTRI session belum login.")
    if not _astri_session_alive(record):
        refreshed = _try_auto_relogin(record)
        if not refreshed:
            raise ValueError("ASTRI session expired dan auto re-login gagal. Login ulang manual.")
        return refreshed
    return record

def astri_login_session(username: str, password: str) -> dict:
    if (not isinstance(username, str) or not isinstance(password, str)
            or not username.strip() or len(username) > 255 or not password or len(password) > 4096):
        raise ValueError("Username dan password ASTRI wajib diisi dengan benar.")
    username = username.strip()
    user = getattr(g, "eva_user", None)
    if not user or not EVA_AUTH.store.user_active(user["id"]):
        raise PermissionError("Pengguna EVA tidak aktif.")
    with astri_user_lock(user["id"]):
        linked = EVA_AUTH.store.astri_link(user["id"])
        if linked and linked["username"] != username:
            raise PermissionError("Satu akun EVA hanya dapat menautkan satu akun ASTRI. Lepas tautan lama dahulu.")
        if not _encrypt_astri_password(password):
            raise ValueError("Penyimpanan login ASTRI terenkripsi belum siap.")
        sess = _astri_login_http_session(username, password)
        sid = secrets.token_urlsafe(24)
        EVA_AUTH.store.claim("astri", sid, user["id"])
        record = {"session": sess, "username": username, "created_at": time.time(), "_session_id": sid}
        try:
            record["_stored"] = persist_astri_session(sid, username, sess, password=password)
            EVA_AUTH.store.link_astri(user["id"], username, sid)
        except Exception:
            # A failed new binding must leave the prior account/browser intact.
            with EVA_AUTH.store.connect() as db:
                db.execute("DELETE FROM resources WHERE kind='astri' AND reference=? AND user_id=?", (sid, user["id"]))
            update_astri_session_store(sid, None)
            raise
        session[ASTRI_SESSION_KEY] = sid
        session.modified = True
        ASTRI_SESSIONS[sid] = record
        return {"username": username, "created_at": record["created_at"]}

def astri_csv_rows(record: dict) -> list[dict]:
    response = record["session"].get(f"{ASTRI_BASE_URL}/download/drm/cluster", timeout=60)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text)))


def _astri_status_filter_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _astri_status_filter_matches(item: dict, status: str = "") -> bool:
    if not status:
        return True
    wanted = _astri_status_filter_token(status)
    return wanted in {
        _astri_status_filter_token(item.get("latest_status_label")),
        _astri_status_filter_token(item.get("latest_status_name")),
    }


def _astri_query_matches(item: dict, query_raw: str = "") -> bool:
    query = normalize_match_text(query_raw)
    if not query:
        return True
    haystack = normalize_match_text(" ".join(
        str(item.get(key) or "")
        for key in (
            "scope_code", "cluster_code", "subfeeder_code", "feeder_code",
            "target_osp_route_code", "cluster_name", "subfeeder_name",
            "feeder_name", "work_order_number",
        )
    ))
    return query in haystack


def _astri_csv_search_items(record: dict, scope: str, query_raw: str = "", status: str = "") -> list[dict]:
    endpoint = {
        "cluster": "cluster",
        "subfeeder": "subfeeder",
        "feeder": "feeder",
    }.get(str(scope or "").strip().lower())
    payload_fn = {
        "cluster": astri_cluster_match_payload_v3,
        "subfeeder": astri_subfeeder_match_payload,
        "feeder": astri_feeder_match_payload,
    }.get(str(scope or "").strip().lower())
    if not endpoint or not payload_fn:
        raise ValueError("Scope ASTRI tidak valid.")
    response = record["session"].get(f"{ASTRI_BASE_URL}/download/drm/{endpoint}", timeout=60)
    response.raise_for_status()
    items = []
    seen_keys = set()
    for raw_row in csv.DictReader(io.StringIO(response.text)):
        normalized = {_normalize_astri_field_key(key): value for key, value in raw_row.items()}
        item = payload_fn(normalized, query_raw)
        if not _astri_query_matches(item, query_raw) or not _astri_status_filter_matches(item, status):
            continue
        key = (item.get("target_type", ""), item.get("scope_code", ""), item.get("work_order_number", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(item)
    return items[:100]

def cluster_name_for_job(job: dict) -> str:
    summary = job.get("hpdb_summary") or {}
    for key in ("cluster_name", "project_name", "name"):
        if summary.get(key):
            return str(summary[key])
    originals = job.get("original_filenames") or []
    return default_project_stem(originals).replace("_", " ") if originals else ""

def find_astri_cluster(record: dict, cluster_name: str) -> dict | None:
    target = normalize_match_text(cluster_name)
    if not target:
        return None
    rows = astri_csv_rows(record)
    exact = [row for row in rows if normalize_match_text(row.get("Target Cluster Name")) == target]
    if exact:
        return exact[0]
    code_candidate = target.split()[0]
    if code_candidate:
        code_match = next((row for row in rows if normalize_match_text(row.get("Target Cluster Code")) == code_candidate), None)
        if code_match:
            return code_match
    code_match = next((row for row in rows if normalize_match_text(row.get("Target Cluster Code")) == target), None)
    if code_match:
        return code_match
    choices = [(normalize_match_text(row.get("Target Cluster Name")), row) for row in rows]
    best = None
    best_score = 0.0
    for name_key, row in choices:
        score = difflib.SequenceMatcher(None, target, name_key).ratio()
        if score > best_score:
            best = row
            best_score = score
    if best and best_score >= 0.82:
        best = dict(best)
        best["_match_score"] = round(best_score, 3)
        return best
    return None

def astri_match_payload(row: dict | None, cluster_name: str) -> dict:
    if not row:
        return {"matched": False, "query": cluster_name}
    area = row.get("Target Cluster Area", "")
    return {
        "area_plant_code": row.get("Target Cluster Area Plant Code", ""),
        "matched": True,
        "scope": "cluster",
        "target_type": "cluster",
        "scope_code": row.get("Target Cluster Code", ""),
        "query": cluster_name,
        "match_score": row.get("_match_score", 1),
        "cluster_code": row.get("Target Cluster Code", ""),
        "cluster_name": row.get("Target Cluster Name", ""),
        "work_order_number": row.get("Number", ""),
        "pr_number": row.get("PR Number", row.get("Purchase Requisition Number", "")),
        "vendor_label": row.get("Assigned Vendor Label", ""),
        "assigned_vendor_name": row.get("Assigned Vendor Name", ""),
        "vendor_name": row.get("Assigned Vendor Name", ""),
        "area": area,
        "area_name": area,
        "province": province_for_city(area),
        "homepass": row.get("Target Cluster Drm Homepass", ""),
        "net_type": row.get("Target Cluster Drm Net Type", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/cluster/drm/requirement-document?cluster_code={row.get('Target Cluster Code', '')}&work_order_number={row.get('Number', '')}&vendor_label={row.get('Assigned Vendor Label', '')}",
    }

def astri_cluster_match_payload_v3(row: dict, query: str = "") -> dict:
    code = row.get("target_cluster_code", "")
    name = row.get("target_cluster_name", "") or row.get("name", "")
    wo = row.get("number", "")
    vendor = row.get("assigned_vendor_label", "")
    assigned_vendor = row.get("assigned_vendor_name", "")
    area = row.get("cluster_area", "") or row.get("target_cluster_area", "")
    return {
        "matched": True,
        "target_type": "cluster",
        "scope": "cluster",
        "scope_code": code,
        "query": query,
        "match_score": 1,
        "cluster_code": code,
        "cluster_name": name,
        "work_order_number": wo,
        "pr_number": row.get("purchase_requisition_number", ""),
        "vendor_label": vendor,
        "assigned_vendor_name": assigned_vendor,
        "vendor_name": assigned_vendor or vendor,
        "area": area,
        "area_name": area,
        "province": province_for_city(area),
        "area_plant_code": row.get("cluster_area_plant_code", "") or row.get("target_cluster_area_plant_code", ""),
        "homepass": row.get("target_cluster_drm_homepass", ""),
        "net_type": row.get("target_cluster_drm_net_type", ""),
        "latest_status_label": row.get("latest_status_label", ""),
        "latest_status_name": row.get("latest_status_name", ""),
        "boq_insert_url": row.get("_astri_boq_insert_url", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/cluster/drm/requirement-document?cluster_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }


def _merge_astri_boq_action_params(row_html: str, row: dict) -> None:
    """Carry the exact live BOQ action query into the selected ASTRI row."""
    hrefs = re.findall(r'<a\b[^>]+href=("[^"]*"|\'[^\']*\')', row_html or "", flags=re.I | re.S)
    for raw_href in hrefs:
        href = html.unescape(raw_href[1:-1])
        if "/boq-drm/insert?" not in href:
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query, keep_blank_values=True)
        for key in (
            "assigned_vendor_name", "vendor_label", "cluster_area", "area", "osp_route_area",
            "cluster_area_plant_code", "area_plant_code", "osp_route_area_plant_code",
            "work_order_number", "drm_homepass", "drm_net_type", "assigned_subcont_vendor_name",
        ):
            if query.get(key):
                row[key] = query[key][0]
        row["_astri_boq_insert_url"] = urllib.parse.urljoin(ASTRI_BASE_URL, href)
        break


def _normalize_astri_field_key(raw_key: str) -> str:
    key = str(raw_key or "").strip().lower()
    key = re.sub(r"^work-order-", "", key)
    key = re.sub(r"(?:-|_)column$", "", key)
    return key.replace("-", "_").replace(" ", "_")


def parse_astri_cluster_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = _normalize_astri_field_key(class_name)
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        _merge_astri_boq_action_params(row_html, row)
        if row.get("target_cluster_code") and row.get("number"):
            rows.append(row)
    return rows


def search_astri_clusters_v3(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()

    # ASTRI v3 may return an empty tbody for status-filtered or exact queries.
    # CSV is the read-only authoritative fallback for matching/status extraction.
    if status:
        try:
            return _astri_csv_search_items(record, "cluster", query_raw, status)
        except Exception:
            if not query_raw:
                return []

    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/cluster/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_label]": status} if status else {}), "page": page, "limit": -1},
                timeout=60,
            )
            response.raise_for_status()
            rows = parse_astri_cluster_rows(response.text)
        except Exception:
            break
        if not rows:
            break
        for row in rows:
            haystack = normalize_match_text(" ".join([
                row.get("target_cluster_code", ""),
                row.get("target_cluster_name", ""),
                row.get("name", ""),
                row.get("number", ""),
            ]))
            if query and query not in haystack:
                continue
            item = astri_cluster_match_payload_v3(row, query_raw)
            if status and (item.get("latest_status_label", "") or "").strip().lower() != status.lower() and status.lower() not in (item.get("latest_status_label", "") or "").strip().lower():
                continue
            key = (item.get("target_type", ""), item.get("cluster_code", ""), item.get("work_order_number", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append(item)
        if len(items) >= 100 or len(rows) < 100:
            break
    items.sort(key=lambda item: item.get("cluster_code", ""))
    return items[:100]



def astri_subfeeder_match_payload(row: dict, query: str = "") -> dict:
    code = row.get("target_subfeeder_code", "")
    name = row.get("target_subfeeder_name", "")
    wo = row.get("number", "")
    vendor = row.get("assigned_vendor_label", "") or row.get("vendor_label", "")
    assigned_vendor = (
        row.get("assigned_vendor_name", "")
        or row.get("target_subfeeder_assigned_vendor_name", "")
        or row.get("target_subfeeder_vendor_name", "")
        or row.get("vendor_name", "")
    )
    return {
        "matched": True,
        "target_type": "subfeeder",
        "scope": "subfeeder",
        "scope_code": code,
        "query": query,
        "cluster_code": code,
        "cluster_name": name,
        "subfeeder_code": code,
        "subfeeder_name": name,
        "work_order_number": wo,
        "pr_number": row.get("target_subfeeder_purchase_requisition_number", "") or row.get("purchase_requisition_number", ""),
        "vendor_label": vendor,
        "assigned_vendor_name": assigned_vendor,
        "vendor_name": assigned_vendor or vendor,
        "area": row.get("cluster_area", "") or row.get("target_subfeeder_area", ""),
        "area_plant_code": row.get("cluster_area_plant_code", "") or row.get("target_subfeeder_area_plant_code", ""),
        # Canonical names required by astri_create_boq_link() validation.
        "cluster_area": row.get("cluster_area", "") or row.get("target_subfeeder_area", ""),
        "cluster_area_plant_code": row.get("cluster_area_plant_code", "") or row.get("target_subfeeder_area_plant_code", ""),
        "homepass": row.get("target_subfeeder_drm_homepass", ""),
        "net_type": row.get("target_subfeeder_drm_net_type", ""),
        "latest_status_label": row.get("latest_status_label", ""),
        "latest_status_name": row.get("latest_status_name", ""),
        "boq_insert_url": row.get("_astri_boq_insert_url", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/subfeeder/drm/requirement-document?subfeeder_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }

def parse_astri_subfeeder_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = _normalize_astri_field_key(class_name)
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        _merge_astri_boq_action_params(row_html, row)
        if row.get("target_subfeeder_code") and row.get("number"):
            rows.append(row)
    return rows

def search_astri_subfeeders(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()

    if status:
        try:
            return _astri_csv_search_items(record, "subfeeder", query_raw, status)
        except Exception:
            if not query_raw:
                return []

    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/subfeeder/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_label]": status} if status else {}), "page": page, "limit": -1},
                timeout=60,
            )
            response.raise_for_status()
            rows = parse_astri_subfeeder_rows(response.text)
        except Exception:
            break
        if not rows:
            break
        for row in rows:
            haystack = normalize_match_text(" ".join([row.get("target_subfeeder_code", ""), row.get("target_subfeeder_name", ""), row.get("number", "")]))
            if query and query not in haystack:
                continue
            item = astri_subfeeder_match_payload(row, query_raw)
            if status and (item.get("latest_status_label", "") or "").strip().lower() != status.lower() and status.lower() not in (item.get("latest_status_label", "") or "").strip().lower():
                continue
            key = (item.get("target_type", ""), item.get("cluster_code", ""), item.get("work_order_number", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append(item)
        if len(items) >= 100 or len(rows) < 100:
            break
    return items[:100]


def _feeder_row_value(row: dict, *keys: str) -> str:
    """Return first non-empty value across the listed CSV/HTML aliases. Also checks alternate key forms with spaces/hyphens converted to underscores."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    # Also check normalized versions from row raw keys
    for raw_key, value in row.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        norm = raw_key.strip().lower().replace(" ", "_").replace("-", "_")
        if norm in keys:
            return text
    return ""


def astri_feeder_match_payload(row: dict, query: str = "") -> dict:
    code = _feeder_row_value(
        row,
        "target_osp_route_code",
        "feeder_code",
        "osp_route_code",
        "code",
        "target_feeder_code",
        "target_wo",
        "code",
    )
    name = _feeder_row_value(
        row,
        "target_osp_route_label",
        "feeder_name",
        "osp_route_label",
        "feeder_label",
        "osp_route_name",
        "name",
        "target_feeder_label",   # preferred for display (uppercase)
        "target_feeder_name",
        "label",
    )
    wo = _feeder_row_value(row, "number", "work_order_number", "wo", "target_wo")
    vendor = _feeder_row_value(
        row,
        "assigned_vendor_label",
        "vendor_label",
        "target_osp_route_vendor_label",
        "vendor_name",
    )
    assigned_vendor = _feeder_row_value(
        row,
        "assigned_vendor_name",
        "target_osp_route_assigned_vendor_name",
        "target_osp_route_vendor_name",
        "vendor_name",
    )
    area = _feeder_row_value(
        row,
        "target_osp_route_area",
        "osp_route_area",
        "area",
        "cluster_area",
        "target_feeder_area",
        "area",
    )
    return {
        "matched": True,
        "target_type": "feeder",
        "scope": "feeder",
        "scope_code": code,
        "query": query,
        "cluster_code": code,
        "cluster_name": name,
        "feeder_code": code,
        "target_osp_route_code": code,
        "feeder_name": name,
        "work_order_number": wo,
        "pr_number": _feeder_row_value(
            row,
            "target_osp_route_purchase_requisition_number",
            "target_feeder_purchase_requisition_number",
            "purchase_requisition_number",
            "pr_number",
        ),
        "vendor_label": vendor,
        "assigned_vendor_name": assigned_vendor,
        "vendor_name": assigned_vendor or vendor,
        "area": area,
        "area_plant_code": _feeder_row_value(row, "osp_route_area_plant_code", "target_osp_route_area_plant_code", "area_plant_code"),
        # Canonical names required by astri_create_boq_link() validation.
        "osp_route_area": area,
        "osp_route_area_plant_code": _feeder_row_value(row, "osp_route_area_plant_code", "target_osp_route_area_plant_code", "area_plant_code"),
        "homepass": "",
        "route_length": _feeder_row_value(
            row,
            "target_osp_route_length",
            "target_osp_route_homepass",
            "target_feeder_length",
            "homepass",
            "length",
        ),
        "net_type": _feeder_row_value(
            row,
            "target_osp_route_drm_net_type",
            "target_feeder_drm_net_type",
            "drm_net_type",
            "net_type",
        ),
        "latest_status_label": _feeder_row_value(row, "latest_status_label"),
        "latest_status_name": _feeder_row_value(row, "latest_status_name"),
        "boq_insert_url": row.get("_astri_boq_insert_url", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/feeder/drm/requirement-document?feeder_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }


def parse_astri_feeder_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = _normalize_astri_field_key(class_name)
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        _merge_astri_boq_action_params(row_html, row)
        if row.get("target_osp_route_code") and row.get("number"):
            rows.append(row)
    return rows


def search_astri_feeders(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()

    if status:
        try:
            return _astri_csv_search_items(record, "feeder", query_raw, status)
        except Exception:
            if not query_raw:
                return []

    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/feeder/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_label]": status} if status else {}), "page": page, "limit": -1},
                timeout=60,
            )
            response.raise_for_status()
            rows = parse_astri_feeder_rows(response.text)
        except Exception:
            break
        if not rows:
            break
        for row in rows:
            haystack = normalize_match_text(" ".join([row.get("target_osp_route_code", ""), row.get("target_osp_route_label", ""), row.get("number", "")]))
            if query and query not in haystack:
                continue
            item = astri_feeder_match_payload(row, query_raw)
            if status and (item.get("latest_status_label", "") or "").strip().lower() != status.lower() and status.lower() not in (item.get("latest_status_label", "") or "").strip().lower():
                continue
            key = (item.get("target_type", ""), item.get("cluster_code", ""), item.get("work_order_number", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append(item)
        if len(items) >= 100 or len(rows) < 100:
            break
    return items[:100]


ASTRI_SUCCESSFUL_STATUS_LABEL = "Successful"
ASTRI_SUCCESSFUL_STATUS_NAME = "successful"
ASTRI_CLOSED_STATUS_LABEL = "Closed"
ASTRI_CLOSED_STATUS_NAME = "closed"
ASTRI_BOQ_TERMINAL_STATUSES = {("successful", "successful"), ("closed", "closed")}
ASTRI_WORK_ORDER_SCOPES = {"cluster", "subfeeder", "feeder"}


def astri_status_is_successful(latest_status_label: str = "", latest_status_name: str = "") -> bool:
    """Match only ASTRI's canonical terminal status; never match Unsuccessful."""
    return (
        str(latest_status_label or "").strip().casefold() == ASTRI_SUCCESSFUL_STATUS_LABEL.casefold()
        and str(latest_status_name or "").strip().casefold() == ASTRI_SUCCESSFUL_STATUS_NAME.casefold()
    )


def astri_status_is_closed(latest_status_label: str = "", latest_status_name: str = "") -> bool:
    """Match only ASTRI's canonical Closed pair."""
    return (
        str(latest_status_label or "").strip().casefold() == ASTRI_CLOSED_STATUS_LABEL.casefold()
        and str(latest_status_name or "").strip().casefold() == ASTRI_CLOSED_STATUS_NAME.casefold()
    )


def astri_boq_status_is_terminal(latest_status_label: str = "", latest_status_name: str = "") -> bool:
    """Closed/Successful allow async BOQ reconciliation after a close attempt."""
    return (
        str(latest_status_label or "").strip().casefold(),
        str(latest_status_name or "").strip().casefold(),
    ) in ASTRI_BOQ_TERMINAL_STATUSES


def _astri_scope_code_aliases(scope: str) -> tuple[str, ...]:
    normalized_scope = str(scope or "").strip().lower()
    try:
        return {
            "cluster": ("cluster_code",),
            "subfeeder": ("subfeeder_code",),
            "feeder": ("feeder_code", "target_osp_route_code"),
        }[normalized_scope]
    except KeyError as exc:
        raise ValueError("Scope ASTRI tidak valid.") from exc


def _astri_canonical_identity(match: dict, scope: str) -> dict:
    """Resolve exact scope code + WO; EVA-only metadata never supplies identity."""
    normalized_scope = str(scope or "").strip().lower()
    aliases = _astri_scope_code_aliases(normalized_scope)
    raw = dict(match or {})
    # __eva_* fields are transport hints only. Ignore them when real ASTRI
    # fields exist; reject naturally when the real fields are absent.
    data = {key: value for key, value in raw.items() if not str(key).startswith("__eva_")}
    real_values = {
        key: str(data.get(key) or "").strip()
        for key in aliases
        if str(data.get(key) or "").strip()
    }
    if len({value.casefold() for value in real_values.values()}) > 1:
        raise ValueError("Identitas kode ASTRI konflik.")
    cross_scope_fields = {
        "cluster": ("subfeeder_code", "feeder_code", "target_osp_route_code"),
        "subfeeder": ("feeder_code", "target_osp_route_code"),
        "feeder": ("subfeeder_code",),
    }[normalized_scope]
    if any(str(data.get(key) or "").strip() for key in cross_scope_fields):
        raise ValueError("Identitas ASTRI lintas-scope tidak boleh dikirim.")
    legacy_cluster_code = str(data.get("cluster_code") or "").strip()
    if normalized_scope in {"subfeeder", "feeder"} and legacy_cluster_code and real_values:
        native_code = next(iter(real_values.values()))
        if legacy_cluster_code.casefold() != native_code.casefold():
            raise ValueError("Identitas kode ASTRI legacy konflik dengan kode scope.")
    scope_hint = str(data.get("scope") or data.get("target_type") or data.get("boq_scope") or "").strip().lower()
    if scope_hint and scope_hint != normalized_scope:
        raise ValueError("Scope ASTRI tidak sesuai dengan route.")
    code = next(iter(real_values.values()), "")
    work_order_number = str(data.get("work_order_number") or data.get("work_order[number]") or "").strip()
    if not code or not work_order_number:
        raise ValueError("Kode scope dan work_order_number diperlukan untuk match ASTRI.")
    # Hidden fields cannot provide identity. They are transport-only, so a stale
    # value must not override valid outbound ASTRI fields.
    canonical = dict(data)
    canonical.update({
        "matched": True,
        "scope": normalized_scope,
        "target_type": normalized_scope,
        "scope_code": code,
        "work_order_number": work_order_number,
    })
    if normalized_scope == "cluster":
        canonical["cluster_code"] = code
    elif normalized_scope == "subfeeder":
        canonical["subfeeder_code"] = code
    else:
        canonical["feeder_code"] = code
        canonical["target_osp_route_code"] = code
    return canonical


def _astri_work_order_identity(match: dict, scope: str) -> tuple[str, str]:
    data = match or {}
    aliases = _astri_scope_code_aliases(scope)
    values = {
        key: str(data.get(key) or "").strip()
        for key in aliases
        if str(data.get(key) or "").strip()
    }
    if len({value.casefold() for value in values.values()}) > 1:
        raise ValueError("Identitas kode ASTRI live konflik.")
    return next(iter(values.values()), ""), str(data.get("work_order_number") or "").strip()


def select_unique_astri_match(record: dict, match: dict, scope: str) -> dict:
    """Resolve exactly one fresh ASTRI row for a selected scope/code/WO tuple."""
    normalized_scope = str(scope or "").strip().lower()
    canonical = _astri_canonical_identity(match or {}, normalized_scope)
    code, work_order_number = _astri_work_order_identity(canonical, normalized_scope)
    search_fn = {
        "cluster": search_astri_clusters_v3,
        "subfeeder": search_astri_subfeeders,
        "feeder": search_astri_feeders,
    }[normalized_scope]
    def exact_items(items: list[dict]) -> list[dict]:
        exact_rows = []
        for item in items:
            item_code, item_wo = _astri_work_order_identity(item, normalized_scope)
            if item_code.casefold() == code.casefold() and item_wo.casefold() == work_order_number.casefold():
                exact_rows.append(item)
        return exact_rows

    exact = exact_items(search_fn(record, code))
    # ASTRI's HTML list can contain the exact row while omitting the latest
    # status cells. Re-read the same exact identity from the CSV export before
    # declaring status unavailable; this is GET-only and preserves fail-closed
    # behavior when neither source resolves one exact row.
    if len(exact) != 1 or any(not str(exact[0].get(key) or "").strip() for key in ("latest_status_label", "latest_status_name")):
        try:
            csv_exact = exact_items(_astri_csv_search_items(record, normalized_scope, code))
        except Exception:
            csv_exact = []
        if len(csv_exact) == 1:
            exact = csv_exact
    if len(exact) != 1:
        raise LookupError(f"Target ASTRI tidak ditemukan/ambigu untuk {code} / {work_order_number}.")
    live = dict(exact[0])
    live.update({
        "matched": True,
        "scope": normalized_scope,
        "target_type": normalized_scope,
        "scope_code": code,
        "work_order_number": work_order_number,
    })
    if normalized_scope == "cluster":
        live["cluster_code"] = code
    elif normalized_scope == "subfeeder":
        live["subfeeder_code"] = code
    else:
        live["feeder_code"] = code
        live["target_osp_route_code"] = code
    return live


def fetch_astri_work_order_status(record: dict, match: dict, scope: str = "cluster") -> dict:
    """Read current ASTRI status; exact scope code + exact WO; malformed status fails closed."""
    normalized_scope = str(scope or "cluster").strip().lower()
    live = select_unique_astri_match(record, match, normalized_scope)
    code, work_order_number = _astri_work_order_identity(live, normalized_scope)
    latest_status_label = str(live.get("latest_status_label") or "").strip()
    latest_status_name = str(live.get("latest_status_name") or "").strip()
    if not latest_status_label or not latest_status_name:
        raise LookupError(f"Status WO ASTRI malformed untuk {code} / {work_order_number}.")
    return {
        "scope": normalized_scope,
        "scope_code": code,
        "cluster_code": code if normalized_scope == "cluster" else str(live.get("cluster_code") or "").strip(),
        "subfeeder_code": code if normalized_scope == "subfeeder" else str(live.get("subfeeder_code") or "").strip(),
        "feeder_code": code if normalized_scope == "feeder" else str(live.get("feeder_code") or "").strip(),
        "target_osp_route_code": code if normalized_scope == "feeder" else str(live.get("target_osp_route_code") or "").strip(),
        "work_order_number": work_order_number,
        "latest_status_label": latest_status_label,
        "latest_status_name": latest_status_name,
        "successful": astri_status_is_successful(latest_status_label, latest_status_name),
        "closed": astri_status_is_closed(latest_status_label, latest_status_name),
        "boq_terminal": astri_boq_status_is_terminal(latest_status_label, latest_status_name),
    }


def _astri_status_block(status_code: int, code: str, error: str, *, status: dict | None = None) -> dict:
    result = {"_http_status": status_code, "code": code, "error": error, "inserted": [], "ok": False, "close_ok": False}
    if status is not None:
        result["wo_status"] = status
    return result


def astri_live_mutation_gate(record: dict, match: dict, scope: str) -> dict | None:
    """Fresh gate immediately before each ASTRI mutation boundary."""
    try:
        status = fetch_astri_work_order_status(record, match, scope)
    except ValueError as exc:
        return _astri_status_block(400, "ASTRI_WO_STATUS_INPUT_INVALID", str(exc))
    except Exception as exc:
        LOGGER.warning("ASTRI WO status unavailable at mutation boundary: scope=%s error=%s", scope, exc)
        return _astri_status_block(
            503,
            "ASTRI_WO_STATUS_UNAVAILABLE",
            "Status WO ASTRI tidak dapat diverifikasi. Action dibatalkan; coba refresh atau login ulang.",
            status={"scope": scope, "unavailable": True},
        )
    if status["successful"]:
        return _astri_status_block(
            409,
            "ASTRI_WO_SUCCESSFUL",
            "Status WO di ASTRI sudah Successful. Approve Schedule dan Add BOQ dikunci.",
            status=status,
        )
    if status["closed"]:
        return _astri_status_block(
            409,
            "ASTRI_WO_CLOSED",
            "Status WO di ASTRI sudah Closed. Approve Schedule dikunci; Add BOQ tetap tersedia.",
            status=status,
        )
    return None


def astri_work_order_guard(record: dict, match: dict, scope: str):
    blocked = astri_live_mutation_gate(record, match, scope)
    if blocked:
        status_code = blocked.pop("_http_status")
        return jsonify(blocked), status_code
    return None


def astri_boq_live_mutation_gate(record: dict, match: dict, scope: str) -> dict | None:
    """Block Add BOQ only after exact Successful; retry transient ASTRI reads."""
    status = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            status = fetch_astri_work_order_status(record, match, scope)
            break
        except ValueError as exc:
            return _astri_status_block(400, "ASTRI_WO_STATUS_INPUT_INVALID", str(exc))
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if status is None:
        LOGGER.warning("ASTRI BOQ status unavailable at mutation boundary after 3 attempts: scope=%s error=%s", scope, last_exc)
        return _astri_status_block(
            503,
            "ASTRI_WO_STATUS_UNAVAILABLE",
            "Status WO ASTRI tidak dapat diverifikasi. Add BOQ dibatalkan; coba refresh atau login ulang.",
            status={"scope": scope, "unavailable": True},
        )
    if status["successful"]:
        status["boq_terminal"] = True
        return _astri_status_block(
            409,
            "ASTRI_BOQ_WO_TERMINAL",
            "Status WO di ASTRI sudah Successful. Add BOQ dikunci; jangan submit ulang.",
            status=status,
        )
    return None


def astri_boq_work_order_guard(record: dict, match: dict, scope: str):
    blocked = astri_boq_live_mutation_gate(record, match, scope)
    if blocked:
        status_code = blocked.pop("_http_status")
        return jsonify(blocked), status_code
    return None


def require_astri_boq_live_mutation_gate(record: dict, match: dict, scope: str) -> None:
    blocked = astri_boq_live_mutation_gate(record, match, scope)
    if blocked:
        raise AstriMutationBlocked(blocked)


def astri_payload_match(payload: dict, scope: str) -> dict:
    supplied = (payload or {}).get("match")
    if not isinstance(supplied, dict):
        raise ValueError("Match ASTRI modal wajib dikirim.")
    return _astri_canonical_identity(supplied, scope)


def astri_query_match(args, scope: str) -> dict:
    return _astri_canonical_identity(args.to_dict(flat=True), scope)


def astri_selected_payload_match(record: dict, payload: dict, scope: str) -> dict:
    """Use the exact modal identity; never rediscover from a job/name/first row."""
    return select_unique_astri_match(record, astri_payload_match(payload, scope), scope)


def astri_selected_query_match(record: dict, args, scope: str) -> dict:
    """Use one exact query identity for preview, supplier-map, and schedule form."""
    return select_unique_astri_match(record, astri_query_match(args, scope), scope)


def astri_schedule_match_from_form(form_data: dict, scope: str) -> dict:
    normalized_scope = str(scope or "").strip().lower()
    aliases = {
        "cluster": ("cluster[cluster_code]",),
        "subfeeder": ("work_order[subfeeder_code]", "subfeeder[subfeeder_code]"),
        "feeder": (
            "osp_route[feeder_code]",
            "work_order[feeder_code]",
            "osp_route[target_osp_route_code]",
            "work_order[target_osp_route_code]",
        ),
    }.get(normalized_scope)
    if not aliases:
        raise ValueError("Scope ASTRI tidak valid.")
    values = {key: str(form_data.get(key) or "").strip() for key in aliases if str(form_data.get(key) or "").strip()}
    if len({value.casefold() for value in values.values()}) > 1:
        raise ValueError("Identitas kode ASTRI form konflik.")
    code = next(iter(values.values()), "")
    work_order_number = str(form_data.get("work_order[number]") or "").strip()
    if not code or not work_order_number:
        raise ValueError("Field ASTRI scope code dan work_order[number] diperlukan.")
    canonical = {
        "scope": normalized_scope,
        "work_order_number": work_order_number,
        **{key: value for key, value in form_data.items() if str(key).startswith("__eva_")},
    }
    canonical[_astri_scope_code_aliases(normalized_scope)[0]] = code
    canonical = _astri_canonical_identity(canonical, normalized_scope)
    # Keep the outbound ASTRI identity only; __eva_* transport fields never
    # enter the mutation payload or authorization decision.
    code_key = _astri_scope_code_aliases(normalized_scope)[0]
    return {code_key: canonical[code_key], "work_order_number": canonical["work_order_number"]}


def astri_public_form_data(form_data: dict) -> dict:
    """Do not forward EVA-only status identifiers to ASTRI."""
    return {key: value for key, value in form_data.items() if not key.startswith("__eva_")}


class AstriMutationBlocked(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.status_code = int(self.payload.pop("_http_status", 503))
        super().__init__(self.payload.get("error", "ASTRI mutation dibatalkan."))


@app.errorhandler(AstriMutationBlocked)
def handle_astri_mutation_blocked(exc: AstriMutationBlocked):
    return jsonify(exc.payload), exc.status_code


def astri_exception_response(exc: Exception):
    if isinstance(exc, AstriMutationBlocked):
        return jsonify(exc.payload), exc.status_code
    if isinstance(exc, ValueError):
        return jsonify({"error": str(exc), "code": "ASTRI_MATCH_INPUT_INVALID"}), 400
    if isinstance(exc, LookupError):
        LOGGER.warning("ASTRI lookup response blocked: %s", exc, exc_info=True)
        return jsonify({
            "error": "Target/status ASTRI tidak dapat diverifikasi. Action dibatalkan.",
            "code": "ASTRI_WO_STATUS_UNAVAILABLE",
        }), 503
    LOGGER.warning("ASTRI exception response: %s", exc, exc_info=True)
    return jsonify({"error": "Request ASTRI tidak dapat diproses."}), 400


def require_astri_live_mutation_gate(record: dict, match: dict, scope: str) -> None:
    blocked = astri_live_mutation_gate(record, match, scope)
    if blocked:
        raise AstriMutationBlocked(blocked)


def astri_validate_selected_match(record: dict, match: dict, scope: str) -> tuple[dict, dict]:
    canonical = _astri_canonical_identity(match, scope)
    status = fetch_astri_work_order_status(record, canonical, scope)
    return canonical, status


def astri_job_upload_files(job: dict) -> dict[str, tuple[Path, str]]:
    files: dict[str, tuple[Path, str]] = {}
    kmz_entries = job.get("standard_kmz_outputs") or []
    if kmz_entries:
        entry = kmz_entries[0]
        path = Path(entry.get("path", ""))
        if path.exists():
            files["kmz"] = (path, entry.get("download_name") or path.name)
    for key in ("boq", "hpdb"):
        spec = ASTRI_UPLOAD_TARGETS[key]
        path = Path(job.get(spec["job_path_key"], ""))
        if path.exists():
            files[key] = (path, job.get(spec.get("job_name_key", "")) or path.name)
    return files

def upload_file_to_astri(record: dict, match: dict, target_key: str, path: Path, filename: str) -> dict:
    spec = ASTRI_UPLOAD_TARGETS[target_key]
    data = {
        "csrf_name": "",
        "csrf_value": "",
        "rfs_document[cluster_code]": match["cluster_code"],
        "rfs_document[work_order_number]": match["work_order_number"],
        "rfs_document[phase]": "",
        "rfs_document[vendor_label]": match["vendor_label"],
        "rfs_document[cluster_name]": match["cluster_name"],
        "rfs_document[type_name]": spec["type_name"],
        "rfs_document[allowed_file_type]": spec["allowed_file_type"],
    }
    with path.open("rb") as fh:
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/apd/upload/v2",
            data=data,
            files={"rfs_document[file]": (filename, fh)},
            allow_redirects=False,
            timeout=120,
        )
    LOGGER.info(
        "ASTRI cluster upload: target=%s file=%s status=%s location=%s body=%s",
        target_key, filename, response.status_code,
        response.headers.get("Location", "(none)"),
        response.text[:500],
    )
    verified = False
    try:
        req_docs = fetch_astri_requirement_documents(record, match)
        doc = req_docs.get(spec["type_name"])
        verified = bool(doc)
        LOGGER.info(
            "ASTRI cluster upload verify: type=%s found=%s docs=%s",
            spec["type_name"], verified, list(req_docs.keys()),
        )
    except Exception:
        LOGGER.exception("ASTRI cluster upload verify failed")
    ok = response.status_code in {200, 201, 302, 303} and verified
    return {"target": target_key, "filename": filename, "status_code": response.status_code, "ok": ok, "verified": verified}


def extract_tag_attrs(tag: str) -> dict:
    attrs = {}
    for key, quote, value in re.findall(r'([a-zA-Z0-9_-]+)=("|\')([^"\']*)\2', tag):
        attrs[key] = html.unescape(value)
    for m in re.finditer(r'(?:^|\s)([a-zA-Z0-9_-]+)(?=(?:\s|>|$))', tag):
        key = m.group(1)
        if key not in attrs:
            attrs[key] = ""
    return attrs

def fetch_astri_requirement_documents(record: dict, match: dict) -> dict[str, dict]:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/cluster/drm/requirement-document",
        params={
            "cluster_code": match["cluster_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    by_type: dict[str, list[dict]] = {}
    for tag in re.findall(r"<button[^>]+change-verification-status-document-button[^>]*>", response.text, flags=re.I):
        attrs = extract_tag_attrs(tag)
        type_name = attrs.get("data-document-type-name", "")
        uuid_value = attrs.get("data-document-uuid", "")
        if not (type_name and uuid_value):
            continue
        by_type.setdefault(type_name, []).append({
            "uuid": uuid_value,
            "type_name": type_name,
            "status": attrs.get("data-verification-status") == "1",
            "disabled": 'disabled' in tag,
        })
    docs: dict[str, dict] = {}
    for type_name, btns in by_type.items():
        uuid_value = btns[0]["uuid"]
        verify_btn = next((b for b in btns if b["status"] is True), None)
        verified = verify_btn["disabled"] if verify_btn else False
        docs[type_name] = {"uuid": uuid_value, "type_name": type_name, "verified": verified}
    return docs

def fetch_unverified_astri_documents(record: dict, match: dict) -> dict[str, dict]:
    all_docs = fetch_astri_requirement_documents(record, match)
    return {k: v for k, v in all_docs.items() if not v.get("verified")}

def fetch_verified_astri_documents(record: dict, match: dict) -> dict[str, dict]:
    all_docs = fetch_astri_requirement_documents(record, match)
    return {k: v for k, v in all_docs.items() if v.get("verified")}

def set_astri_document_verification(record: dict, match: dict, type_name: str, uuid_value: str, is_verified: bool) -> dict:
    response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/verification-status/change",
        data={
            "csrf_name": "",
            "csrf_value": "",
            "rfs_document[cluster_code]": "",
            "rfs_document[work_order_number]": match["work_order_number"],
            "rfs_document[vendor_label]": "",
            "ref_page": f"material/cluster/drm/requirement-document?cluster_code={match['cluster_code']}&work_order_number={match['work_order_number']}&vendor_label={match.get('vendor_label', '')}",
            "rfs_document[uuid]": uuid_value,
            "rfs_document[type_name]": type_name,
            "rfs_document[is_verified]": "1" if is_verified else "0",
        },
        allow_redirects=False,
        timeout=60,
    )
    ok = response.status_code in {200, 201, 302, 303}
    return {"type_name": type_name, "uuid": uuid_value, "status_code": response.status_code, "ok": ok}

def verify_astri_document(record: dict, match: dict, type_name: str, uuid_value: str) -> dict:
    return set_astri_document_verification(record, match, type_name, uuid_value, True)

def unverify_astri_document(record: dict, match: dict, type_name: str, uuid_value: str) -> dict:
    return set_astri_document_verification(record, match, type_name, uuid_value, False)

def is_astri_boundary_document(type_name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", type_name.lower()).strip()
    return "boundary" in normalized

def fetch_astri_feeder_requirement_documents(record: dict, match: dict) -> dict[str, dict]:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/feeder/drm/requirement-document",
        params={
            "feeder_code": match["cluster_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    by_type: dict[str, list[dict]] = {}
    for tag in re.findall(r"<button[^>]+change-verification-status-document-button[^>]*>", response.text, flags=re.I):
        attrs = extract_tag_attrs(tag)
        type_name = attrs.get("data-document-type-name", "")
        uuid_value = attrs.get("data-document-uuid", "")
        if not (type_name and uuid_value):
            continue
        by_type.setdefault(type_name, []).append({
            "uuid": uuid_value,
            "type_name": type_name,
            "status": attrs.get("data-verification-status") == "1",
            "disabled": 'disabled' in tag,
        })
    docs: dict[str, dict] = {}
    for type_name, btns in by_type.items():
        uuid_value = btns[0]["uuid"]
        verify_btn = next((b for b in btns if b["status"] is True), None)
        verified = verify_btn["disabled"] if verify_btn else False
        docs[type_name] = {"uuid": uuid_value, "type_name": type_name, "verified": verified}
    return docs


def fetch_astri_subfeeder_requirement_documents(record: dict, match: dict) -> dict[str, dict]:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/subfeeder/drm/requirement-document",
        params={
            "subfeeder_code": match["cluster_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    by_type: dict[str, list[dict]] = {}
    for tag in re.findall(r"<button[^>]+change-verification-status-document-button[^>]*>", response.text, flags=re.I):
        attrs = extract_tag_attrs(tag)
        type_name = attrs.get("data-document-type-name", "")
        uuid_value = attrs.get("data-document-uuid", "")
        if not (type_name and uuid_value):
            continue
        by_type.setdefault(type_name, []).append({
            "uuid": uuid_value,
            "type_name": type_name,
            "status": attrs.get("data-verification-status") == "1",
            "disabled": 'disabled' in tag,
        })
    docs: dict[str, dict] = {}
    for type_name, btns in by_type.items():
        uuid_value = btns[0]["uuid"]
        # Determine verified status: the actionable button tells us.
        # If status=1 button is disabled, doc IS verified (Cancel Verification is available).
        # If status=1 button is enabled, doc is NOT verified.
        verify_btn = next((b for b in btns if b["status"] is True), None)
        verified = verify_btn["disabled"] if verify_btn else False
        docs[type_name] = {"uuid": uuid_value, "type_name": type_name, "verified": verified}
    return docs


def set_astri_feeder_document_verification(record: dict, match: dict, type_name: str, uuid_value: str, is_verified: bool) -> dict:
    response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/verification-status/change",
        data={
            "csrf_name": "",
            "csrf_value": "",
            "rfs_document[cluster_code]": "",
            "rfs_document[work_order_number]": match["work_order_number"],
            "rfs_document[vendor_label]": "",
            "ref_page": f"material/feeder/drm/requirement-document?feeder_code={match['cluster_code']}&work_order_number={match['work_order_number']}&vendor_label={match.get('vendor_label', '')}",
            "rfs_document[uuid]": uuid_value,
            "rfs_document[type_name]": type_name,
            "rfs_document[is_verified]": "1" if is_verified else "0",
        },
        allow_redirects=False,
        timeout=60,
    )
    ok = response.status_code in {200, 201, 302, 303}
    result = {"type_name": type_name, "uuid": uuid_value, "status_code": response.status_code, "ok": ok}
    return result


def set_astri_subfeeder_document_verification(record: dict, match: dict, type_name: str, uuid_value: str, is_verified: bool) -> dict:
    response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/verification-status/change",
        data={
            "csrf_name": "",
            "csrf_value": "",
            "rfs_document[cluster_code]": "",
            "rfs_document[work_order_number]": match["work_order_number"],
            "rfs_document[vendor_label]": "",
            "ref_page": f"material/subfeeder/drm/requirement-document?subfeeder_code={match['cluster_code']}&work_order_number={match['work_order_number']}&vendor_label={match.get('vendor_label', '')}",
            "rfs_document[uuid]": uuid_value,
            "rfs_document[type_name]": type_name,
            "rfs_document[is_verified]": "1" if is_verified else "0",
        },
        allow_redirects=False,
        timeout=60,
    )
    ok = response.status_code in {200, 201, 302, 303}
    result = {"type_name": type_name, "uuid": uuid_value, "status_code": response.status_code, "ok": ok}
    return result


@app.route("/api/astri/subfeeder/verify", methods=["POST"])
def api_astri_subfeeder_verify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        docs = fetch_astri_subfeeder_requirement_documents(record, match)
        results = []
        for type_name, doc in docs.items():
            if is_astri_boundary_document(type_name):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-verify."})
                continue
            if doc.get("verified"):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "note": "Sudah terverifikasi sebelumnya."})
                continue
            result = set_astri_subfeeder_document_verification(record, match, type_name, doc["uuid"], True)
            result["target"] = type_name
            results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder verify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/subfeeder/unverify", methods=["POST"])
def api_astri_subfeeder_unverify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        docs = fetch_astri_subfeeder_requirement_documents(record, match)
        results = []
        for type_name, doc in docs.items():
            if is_astri_boundary_document(type_name):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-unverify."})
                continue
            if not doc.get("verified"):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "note": "Sudah unverified sebelumnya."})
                continue
            result = set_astri_subfeeder_document_verification(record, match, type_name, doc["uuid"], False)
            result["target"] = type_name
            results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder unverify failed")
        return jsonify({"error": str(exc)}), 400

def fetch_astri_requirement_page(record: dict, match: dict) -> str:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/cluster/drm/requirement-document",
        params={
            "cluster_code": match["cluster_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def fetch_astri_latest_documents(record: dict, match: dict, is_subfeeder: bool = False, is_feeder: bool = False) -> list[dict]:
    if is_feeder:
        page_html = fetch_astri_feeder_requirement_page(record, match)
    elif is_subfeeder:
        page_html = fetch_astri_subfeeder_requirement_page(record, match)
    else:
        page_html = fetch_astri_requirement_page(record, match)
    raw_match = re.search(r"documentListJsonRaw\s*=\s*`(.*?)`\s*;", page_html, flags=re.S)
    if not raw_match:
        return []
    document_lists = json.loads(raw_match.group(1))
    latest: dict[str, dict] = {}
    for type_name, info in document_lists.items():
        data = info.get("data") if isinstance(info, dict) else None
        if not isinstance(data, list) or not data:
            continue
        docs = [item for item in data if item.get("uuid") and item.get("filename")]
        if not docs:
            continue
        docs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        latest[type_name] = docs[0]
    return list(latest.values())


def _clean_apd_filename_part(value: str, fallback: str = "UNKNOWN") -> str:
    text = html.unescape(str(value or "")).strip().upper()
    text = re.sub(r'[\\/:*?"<>|]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text or fallback


def astri_apd_standard_filename(match: dict, is_subfeeder: bool = False, is_feeder: bool = False) -> str:
    if is_feeder:
        code = match.get("feeder_code") or match.get("target_osp_route_code") or match.get("cluster_code") or "FEEDER"
        name = match.get("feeder_name") or match.get("target_osp_route_label") or match.get("cluster_name") or match.get("name") or "UNKNOWN"
        stem = f"{_clean_apd_filename_part(code)} - {_clean_apd_filename_part(name)}"
    elif is_subfeeder:
        code = match.get("subfeeder_code") or match.get("cluster_code") or "SUBFEEDER"
        name = match.get("subfeeder_name") or match.get("cluster_name") or match.get("name") or "UNKNOWN"
        name = re.sub(r"^SF\s+", "", _clean_apd_filename_part(name)).strip()
        stem = f"{_clean_apd_filename_part(code)} - SF {name}"
    else:
        code = match.get("cluster_code") or "CLUSTER"
        name = match.get("cluster_name") or match.get("name") or "UNKNOWN"
        stem = f"{_clean_apd_filename_part(code)} - {_clean_apd_filename_part(name)}"
    return f"{stem}.kmz"


def select_astri_apd_kmz_document(docs: list[dict], is_subfeeder: bool = False, is_feeder: bool = False) -> dict | None:
    target = FEEDER_UPLOAD_TARGETS["kmz"]["type_name"] if is_feeder else SUBFEEDER_UPLOAD_TARGETS["kmz"]["type_name"] if is_subfeeder else ASTRI_UPLOAD_TARGETS["kmz"]["type_name"]
    def score(doc: dict) -> tuple[int, str]:
        type_name = str(doc.get("type_name") or "").lower()
        filename = str(doc.get("filename") or "").lower()
        hay = f"{type_name} {filename}"
        if type_name == target:
            pri = 0
        elif "kmz" in hay and "as_plan" in hay:
            pri = 1
        elif "kmz" in hay and "as built" in hay.replace("_", " "):
            pri = 2
        elif "kmz" in hay:
            pri = 3
        else:
            pri = 9
        return (pri, str(doc.get("created_at") or ""))
    kmz_docs = [d for d in docs if "kmz" in (str(d.get("type_name") or "") + " " + str(d.get("filename") or "")).lower()]
    if not kmz_docs:
        return None
    kmz_docs.sort(key=score)
    return kmz_docs[0]


def create_conversion_job_from_apd_kmz(match: dict, content: bytes, filename: str, scope_type: str, network_type: str = NETWORK_TYPE_AE) -> dict:
    output_mode = "boq_kmz"
    selected_layers = ["KML"]
    selected_kml_folders = []
    road_clip_boundary = True
    job_id = uuid.uuid4().hex
    safe_name = secure_filename(filename) or f"{job_id}.kmz"
    if not safe_name.lower().endswith(".kmz"):
        safe_name += ".kmz"
    input_path = Config.UPLOAD_DIR / f"{job_id}_1_{safe_name}"
    input_path.write_bytes(content)
    if input_path.stat().st_size > Config.MAX_FILE_BYTES:
        input_path.unlink(missing_ok=True)
        raise ValueError(f"Ukuran file APD melebihi {Config.MAX_UPLOAD_MB}MB.")
    dxf_output_path = Config.OUTPUT_DIR / f"{job_id}_{network_type}.dxf"
    boq_output_path = Config.BOQ_OUTPUT_DIR / f"{job_id}_BOQ_{network_type}.xlsx"
    astri_output_path = Config.HPDB_OUTPUT_DIR / f"{job_id}_ASTRI_{network_type}.xlsx"
    hpdb_output_path = Config.HPDB_OUTPUT_DIR / f"{job_id}_HPDB_{network_type}.xlsx"
    standard_kmz_output_path = Config.OUTPUT_DIR / f"{job_id}_STANDARDIZED.kmz"
    zip_output_path = Config.OUTPUT_DIR / f"{job_id}_OUTPUT_{network_type}.zip"
    names = download_names_for_mode([filename], output_mode, network_type)
    create_job(job_id, [input_path], zip_output_path, selected_layers, selected_kml_folders, road_clip_boundary, names["final"], output_mode=output_mode, dxf_output_path=dxf_output_path, boq_output_path=boq_output_path, astri_output_path=astri_output_path, hpdb_output_path=hpdb_output_path, dxf_download_name=names["dxf"], boq_download_name=names["boq"], astri_download_name=names["astri"], hpdb_download_name=names["hpdb"], download_mimetype=download_mimetype_for_mode(output_mode), network_type=network_type, scope_type=scope_type)
    update_job(job_id, original_filenames=[filename], standard_kmz_output_path=str(standard_kmz_output_path), standard_kmz_download_name=names["kmz"], apd_astri_match=match, apd_import_filename=filename)
    logger = create_job_logger(job_id)
    logger.info(f"APD ASTRI KMZ imported: {filename} ({format_bytes(len(content))}).")
    logger.info("Output mode selected: BOQ + KMZ Standard.")
    logger.info("KMZ auto-standardizer will run before export.")
    queue_backend = enqueue_conversion_job(job_id, run_conversion_job)
    logger.success(f"APD KMZ staged. Conversion worker queued via {queue_backend}.")
    record_job_created(len(content), output_mode, network_type)
    record_upload_file("kmz")
    return {"job_id": job_id, "filename": filename, "queue_backend": queue_backend}


def astri_bundle_download_name(match: dict, is_subfeeder: bool = False) -> str:
    code = match.get("cluster_code") or match.get("subfeeder_code") or "ASTRI"
    name = match.get("cluster_name") or match.get("subfeeder_name") or match.get("name") or "DOCUMENTS"
    prefix = f"{code} - {name}"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", prefix).strip(" ._") or "ASTRI DOCUMENTS"
    return f"{safe}.zip"

def _has_bool_attr(tag: str, attr: str) -> bool:
    return bool(re.search(rf'\b{re.escape(attr)}\b(?:\s|>|$|=)', tag))

def extract_astri_approve_schedule_form(page_html: str) -> dict:
    import re, html
    button_match = re.search(r'<button[^>]+approve-drm-schedule-button[^>]*>', page_html, flags=re.I)
    form_match = re.search(r'<form[^>]+id="verify-drm-schedule-form"[^>]*>.*?</form>', page_html, flags=re.S | re.I)
    if not button_match or not form_match:
        raise ValueError("Form Approve Schedule tidak tersedia di ASTRI.")
    button_attrs = extract_tag_attrs(button_match.group(0))
    form_html = form_match.group(0)
    form_html = re.sub(r'action=("|\')[^"\']*\1', 'action="#"', form_html, count=1, flags=re.I)
    form_html = re.sub(r'method=("|\')[^"\']*\1', 'method="POST"', form_html, count=1, flags=re.I)
    form_html = re.sub(r'id=("|\')verify-drm-schedule-form\1', 'id="eva-astri-approve-schedule-form"', form_html, count=1, flags=re.I)
    replacements = {
        "approve-drm-schedule-work-order-number-input": button_attrs.get("data-work-order-number", ""),
        "approve-drm-schedule-cluster-name-input": button_attrs.get("data-cluster-name", ""),
        "approve-drm-schedule-cluster-code-input": button_attrs.get("data-cluster-code", ""),
        "approve-drm-schedule-subfeeder-name-input": button_attrs.get("data-subfeeder-name", ""),
        "approve-drm-schedule-subfeeder-code-input": button_attrs.get("data-subfeeder-code", ""),
        "approve-drm-schedule-assigned-vendor-name-input": button_attrs.get("data-assigned-vendor-name", ""),
        "approve-drm-schedule-appointment-date-input": button_attrs.get("data-appointment-date", ""),
        "approve-drm-schedule-appointment-slot-name-input": button_attrs.get("data-appointment-slot-name", ""),
        "approve-drm-schedule-target-rfs-date-input": button_attrs.get("data-target-rfs-date", ""),
        # Current ASTRI Feeder form exposes route identity on data-osp-route-*.
        "approve-drm-schedule-osp-route-code-input": button_attrs.get("data-osp-route-code", ""),
        "approve-drm-schedule-osp-route-uuid-input": button_attrs.get("data-osp-route-uuid", ""),
        "approve-drm-schedule-osp-route-name-input": button_attrs.get("data-osp-route-name", ""),
        "approve-drm-schedule-osp-route-label-input": button_attrs.get("data-osp-route-label", ""),
    }
    for element_id, value in replacements.items():
        if not value:
            continue
        escaped = html.escape(value, quote=True)
        pattern = rf'(<input[^>]+id=("|\'){re.escape(element_id)}\2[^>]*\bvalue=)("|\')[^"\']*(\3)'
        form_html = re.sub(pattern, rf'\1"{escaped}"', form_html, count=1, flags=re.I)
        if not re.search(pattern, form_html):
            insert_pattern = rf'(<input[^>]+id=("|\'){re.escape(element_id)}\2[^>]*?)>'
            form_html = re.sub(insert_pattern, rf'\1 value="{escaped}">', form_html, count=1, flags=re.I)
    # Extract structured fields for frontend. Feeder uses custom radio
    # dropdowns for OLT/Metro; Cluster/Subfeeder retain the legacy parser.
    fields = []
    seen_ids = set()
    radio_groups = {}
    radio_group_order = []
    is_feeder_schedule_form = bool(
        button_attrs.get("data-osp-route-code")
        or re.search(r'name=("|\')osp_route\[feeder_code\]\1', form_html, re.I)
    )
    for inp in re.finditer(r'<input[^>]+>', form_html, re.I):
        tag = inp.group(0)
        attrs = extract_tag_attrs(tag)
        iname = attrs.get("name", "")
        ftype = attrs.get("type", "text").lower()
        if ftype == "radio" and is_feeder_schedule_form:
            if not iname:
                continue
            if iname not in radio_groups:
                radio_groups[iname] = {"options": [], "value": ""}
                radio_group_order.append(iname)
            value = attrs.get("value", "")
            label_text = value
            label_start = form_html.rfind("<label", 0, inp.start())
            label_end = form_html.find("</label>", inp.end())
            if label_start >= 0 and label_end >= inp.end():
                label_html = form_html[label_start:label_end + len("</label>")]
                text_match = re.search(r'<(?:span|div)[^>]*data-filter-text[^>]*>(.*?)</(?:span|div)>', label_html, re.I | re.S)
                label_text = html.unescape(re.sub(r"<[^>]+>", "", text_match.group(1) if text_match else label_html)).strip()
            radio_groups[iname]["options"].append({
                "value": value,
                "text": label_text or value,
                "disabled": _has_bool_attr(tag, "disabled"),
            })
            if _has_bool_attr(tag, "checked"):
                radio_groups[iname]["value"] = value
            continue
        iid = attrs.get("id", "") or (iname.replace("[", "_").replace("]", "").replace(".", "_"))
        if not iname or not iid or iid in seen_ids:
            continue
        seen_ids.add(iid)
        if ftype == "hidden":
            fields.append({
                "id": iid,
                "name": iname,
                "type": "hidden",
                "value": attrs.get("value", ""),
                "readonly": True,
                "disabled": False,
                "placeholder": "",
                "hidden": True,
            })
            continue
        fields.append({
            "id": iid,
            "name": iname,
            "type": ftype,
            "value": attrs.get("value", ""),
            "readonly": _has_bool_attr(tag, "readonly"),
            "disabled": _has_bool_attr(tag, "disabled"),
            "placeholder": attrs.get("placeholder", ""),
        })
    for sel in re.finditer(r'<select[^>]+>.*?</select>', form_html, re.I | re.S):
        stxt = sel.group(0)
        sattrs = extract_tag_attrs(stxt[:stxt.find(">")+1])
        sid = sattrs.get("id", "")
        sname = sattrs.get("name", "") or sid
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        opts = re.findall(r'(<option[^>]*?)value=("|\')([^"\']*)\2([^>]*)>(.*?)</option>', stxt, re.I | re.S)
        selected_values = [
            html.unescape(ov) for tag_start, _, ov, tag_end, ot in opts
            if "selected" in tag_start or "selected" in tag_end
        ]
        # ASTRI can mark placeholder and effective option selected; last wins.
        sel_val = selected_values[-1] if selected_values else (html.unescape(opts[0][2]) if opts else "")
        option_items = []
        for tag_start, quote, ov, tag_end, ot in opts:
            option_tag = f"{tag_start}value={quote}{ov}{quote}{tag_end}"
            option_items.append({
                "value": html.unescape(ov),
                "text": html.unescape(re.sub(r'<[^>]+>', '', ot)).strip(),
                "class": extract_tag_attrs(option_tag).get("class", ""),
                "disabled": _has_bool_attr(option_tag, "disabled"),
            })
        fields.append({
            "id": sid,
            "name": sname,
            "type": "select",
            "value": sel_val,
            "options": option_items,
            "readonly": False,
            "disabled": _has_bool_attr(stxt[:stxt.find(">")+1], "disabled"),
            "placeholder": "",
        })
    for group_name in radio_group_order:
        group = radio_groups[group_name]
        if group["options"]:
            fields.append({
                "id": group_name.replace("[", "_").replace("]", "").replace(".", "_"),
                "name": group_name,
                "type": "radio_group",
                "value": group["value"],
                "options": group["options"],
                "label": {"approve_drm_schedule_olt_name": "Select OLT", "approve_drm_schedule_metro_name": "Select Metro"}.get(group_name, group_name.replace("_", " ").title()),
                "readonly": False,
                "disabled": False,
                "placeholder": "",
            })
    for lm in re.finditer(r'<label[^>]+for=("|\')([^"\']*)\1[^>]*>(.*?)</label>', form_html, re.I | re.S):
        fid = lm.group(2)
        ltxt = html.unescape(re.sub(r'<[^>]+>', '', lm.group(3))).strip()
        for f in fields:
            if f["id"] == fid:
                f["label"] = ltxt
                break
    return {"form_html": form_html, "fields": fields, "button": button_attrs}



def get_astri_plant_code(area: str, supplied_code: str = "") -> str:
    """Return ASTRI area plant code; supplied live ASTRI value wins."""
    if str(supplied_code or "").strip():
        return str(supplied_code).strip()
    normalized_area = re.sub(r"\s+", " ", str(area or "").strip().upper())
    if not normalized_area:
        return ""
    db_path = Path(__file__).resolve().parent / "data" / "astri_plant_codes.db"
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT plant_code FROM area_plant_codes WHERE area_normalized = ?", (normalized_area,)
            ).fetchone()
        return str(row[0]).strip() if row and row[0] else ""
    except Exception as exc:
        LOGGER.warning("ASTRI plant-code lookup failed area=%s: %s", normalized_area, exc)
        return ""


def astri_create_boq_link(record: dict, match: dict) -> dict:
    """Build ASTRI insert URL directly from selected ASTRI match.

    Do not scrape DRM list buttons: list visibility is unrelated to whether ASTRI's
    BOQ insert form is accessible, and scopes use different endpoints/parameters.
    """
    from urllib.parse import urlencode

    scope = str(match.get("scope") or match.get("boq_scope") or "cluster").lower()
    code_key = {
        "cluster": "cluster_code",
        "subfeeder": "subfeeder_code",
        "feeder": "feeder_code",
    }.get(scope)
    code = str(match.get(code_key) or "").strip() if code_key else ""
    if not code:
        return {"enabled": False, "error": "Kode ASTRI kosong."}

    # The selected DRM row may carry ASTRI's exact action URL. Validate its
    # identity before trusting it; a stale URL must never authorize a mutation.
    exact_url = str(match.get("boq_insert_url") or "").strip()
    if exact_url:
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(exact_url)
        base_url = urlparse(ASTRI_BASE_URL)
        expected_path = f"/material/{scope}/boq-drm/insert"
        if (
            parsed_url.scheme.casefold() != base_url.scheme.casefold()
            or parsed_url.netloc.casefold() != base_url.netloc.casefold()
            or parsed_url.path.rstrip("/") != expected_path
        ):
            return {"enabled": False, "scope": scope, "error": "Origin/path URL BOQ ASTRI tidak valid."}
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        exact_code_key = "cluster_code" if scope in {"cluster", "subfeeder"} else "feeder_code"
        exact_code = str(query.get(exact_code_key, [""])[0] or "").strip()
        exact_wo = str(query.get("work_order_number", [""])[0] or "").strip()
        expected_wo = str(match.get("work_order_number") or "").strip()
        if exact_code.casefold() != code.casefold() or exact_wo.casefold() != expected_wo.casefold():
            return {"enabled": False, "scope": scope, "error": "URL BOQ ASTRI tidak sesuai dengan identitas match terpilih."}
        if scope == "cluster":
            contracts = (
                ("vendor", "assigned_vendor_name", "assigned_vendor_name", True),
                ("area", "cluster_area", "cluster_area", True),
                ("plant", "cluster_area_plant_code", "cluster_area_plant_code", False),
            )
        elif scope == "subfeeder":
            contracts = (
                ("vendor", "assigned_vendor_name", "assigned_vendor_name", True),
                ("area", "cluster_area", "cluster_area", True),
                ("plant", "cluster_area_plant_code", "cluster_area_plant_code", False),
            )
        else:
            contracts = (
                ("vendor", "assigned_vendor_name", "assigned_vendor_name", True),
                ("area", "osp_route_area", "osp_route_area", True),
                ("plant", "osp_route_area_plant_code", "osp_route_area_plant_code", False),
            )
        for label, query_key, match_key, required in contracts:
            expected = str(match.get(match_key) or "").strip()
            actual = str(query.get(query_key, [""])[0] or "").strip()
            if (required and (not expected or not actual)) or (expected and actual != expected):
                return {"enabled": False, "scope": scope, "error": f"URL BOQ ASTRI memiliki {label} yang berbeda dari match terpilih."}
        return {"enabled": True, "url": exact_url, "scope": scope}

    wo = str(match.get("work_order_number") or match.get("wo") or "").strip()
    area = str(match.get("area") or match.get("cluster_area") or match.get("osp_route_area") or "").strip()
    plant = get_astri_plant_code(
        area,
        match.get("area_plant_code") or match.get("cluster_area_plant_code") or match.get("osp_route_area_plant_code") or "",
    )
    subcont = str(match.get("assigned_subcont_vendor_name") or "").strip()

    if scope == "feeder":
        assigned_vendor = str(match.get("assigned_vendor_name") or "").strip()
        if not assigned_vendor:
            return {"enabled": False, "scope": scope, "error": "assigned_vendor_name ASTRI tidak ditemukan pada match terpilih."}
        # Exact ASTRI key. Preserve significant trailing underscores.
        path = "/material/feeder/boq-drm/insert"
        params = {
            "feeder_code": code,
            "assigned_vendor_name": assigned_vendor,
            "osp_route_area": area,
            "work_order_number": wo,
            "assigned_subcont_vendor_name": subcont,
            "osp_route_area_plant_code": plant,
        }
    elif scope == "subfeeder":
        assigned_vendor = str(match.get("assigned_vendor_name") or "").strip()
        if not assigned_vendor:
            return {"enabled": False, "scope": scope, "error": "assigned_vendor_name ASTRI tidak ditemukan pada match terpilih."}
        path = "/material/subfeeder/boq-drm/insert"
        params = {
            "cluster_code": code,
            "assigned_vendor_name": assigned_vendor,
            "cluster_area": area,
            "work_order_number": wo,
            "assigned_subcont_vendor_name": subcont,
            "cluster_area_plant_code": plant,
        }
    else:
        assigned_vendor = str(match.get("assigned_vendor_name") or "").strip()
        if not assigned_vendor:
            return {"enabled": False, "scope": scope, "error": "assigned_vendor_name ASTRI tidak ditemukan pada match terpilih."}
        path = "/material/cluster/boq-drm/insert"
        assigned_vendor = str(match.get("assigned_vendor_name") or "").strip()
        cluster_area = str(match.get("area") or match.get("cluster_area") or "").strip()
        if not assigned_vendor or not cluster_area:
            return {"enabled": False, "scope": scope, "error": "assigned_vendor_name/cluster_area ASTRI tidak ditemukan pada match terpilih."}
        params = {
            "cluster_code": code,
            "assigned_vendor_name": assigned_vendor,
            "cluster_area": cluster_area,
            "work_order_number": wo,
            "drm_homepass": str(match.get("homepass") or match.get("drm_homepass") or ""),
            "drm_net_type": str(match.get("net_type") or match.get("drm_net_type") or ""),
            "assigned_subcont_vendor_name": subcont,
            "cluster_area_plant_code": plant,
        }
    url = f"{ASTRI_BASE_URL}{path}?{urlencode(params)}"
    return {"enabled": True, "url": url, "scope": scope}



def _astri_boq_supplier_key(value) -> str:
    text = str(value or "").strip()
    decimal_match = re.fullmatch(r"(\d+)\.0+", text)
    if decimal_match:
        return decimal_match.group(1)
    digits = re.sub(r"\D", "", text)
    return digits or text


def _astri_supplier_kind(value: str) -> str:
    supplier = str(value or "").strip()
    if not supplier:
        return "-"
    return "Internal" if "internal" in supplier.casefold() else "Vendor"


def _parse_astri_boq_supplier_fields(page_html: str) -> dict[str, dict]:
    """Parse exact live ASTRI boq[CODE][vendor_name] values.

    Attribute order, quote style, HTML escaping, and select/input rendering vary
    between ASTRI scopes. Never infer supplier from project labels or row order.
    """
    mapping: dict[str, dict] = {}

    def add(code: str, supplier: str) -> None:
        key = _astri_boq_supplier_key(code)
        if not key:
            return
        value = html.unescape(str(supplier or "")).strip()
        mapping[key] = {
            "supplier": value,
            "vendor_name": value,
            "supplier_kind": _astri_supplier_kind(value),
            "supplier_found": bool(value),
        }

    for tag in re.findall(r"<input\b[^>]*>", page_html or "", flags=re.I):
        attrs = extract_tag_attrs(tag)
        name = str(attrs.get("name") or "")
        found = re.fullmatch(r"boq\[([^]]+)\]\[vendor_name\]", name, flags=re.I)
        if found:
            add(found.group(1), attrs.get("value", ""))

    for block in re.findall(r"<select\b[^>]*>.*?</select>", page_html or "", flags=re.I | re.S):
        start = block[: block.find(">") + 1]
        attrs = extract_tag_attrs(start)
        name = str(attrs.get("name") or "")
        found = re.fullmatch(r"boq\[([^]]+)\]\[vendor_name\]", name, flags=re.I)
        if not found:
            continue
        selected = re.search(
            r"<option\b[^>]*\bselected(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^ >]+))?[^>]*>(.*?)</option>",
            block,
            flags=re.I | re.S,
        )
        option = selected
        if not option:
            add(found.group(1), "")
            continue
        opening = re.match(r"<option\b[^>]*>", option.group(0), flags=re.I)
        option_attrs = extract_tag_attrs(opening.group(0)) if opening else {}
        supplier = option_attrs.get("value", "")
        if not supplier:
            supplier = re.sub(r"<[^>]+>", "", option.group(1)).strip()
        add(found.group(1), supplier)
    return mapping


def _is_astri_permit_item(item: dict) -> bool:
    description = str(item.get("description") or item.get("short_text") or "")
    return any(_astri_boq_supplier_key(item.get(key)) == "500002155"
               for key in ("material_number", "code", "material_code", "equipment_name")) or bool(
        re.search(r"\bRT\s*[/\-]\s*RW\b|\bCOMMUNITY\s+PERMIT\b", description, re.I)
    )


def _astri_boq_without_permit_fields(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """ASTRI owns permit: exclude its header and entire row from every POST."""
    permit_codes = {"500002155"}
    for name, value in fields:
        matched = re.fullmatch(r"boq\[([^]]+)\]\[(description|equipment_name|material_number)\]", str(name), re.I)
        if matched and _is_astri_permit_item({matched.group(2).lower(): value}):
            permit_codes.add(_astri_boq_supplier_key(matched.group(1)))
    safe = []
    for name, value in fields:
        if re.fullmatch(r"(?:work_order\[)?permit(?:_rt_rw)?\]?", str(name), re.I):
            continue
        matched = re.match(r"boq\[([^]]+)\]", str(name), re.I)
        if matched and _astri_boq_supplier_key(matched.group(1)) in permit_codes:
            continue
        safe.append((name, value))
    return safe


def _parse_astri_boq_permit(fields: list[tuple[str, str]]) -> dict:
    """Read the server-rendered permit price. Blank/invalid is not a zero."""
    from decimal import Decimal

    result = {"source": "ASTRI", "available": False, "value": None, "quantity_service": None}
    values = {}
    for name, value in fields:
        if name in {"boq[500002155][override_price_service]", "boq[500002155][quantity_service]"}:
            values.setdefault(name, []).append(str(value).strip())
    entries = values.get("boq[500002155][override_price_service]", [])
    if not entries or len(set(entries)) != 1 or not re.fullmatch(r"\d{1,15}(?:\.\d{1,6})?", entries[0]):
        return result
    number = Decimal(entries[0])
    if number > Decimal("1000000000000"):
        return result
    result.update(available=True, value=format(number, "f"))
    quantities = values.get("boq[500002155][quantity_service]", [])
    if quantities and len(set(quantities)) == 1 and re.fullmatch(r"\d{1,15}(?:\.\d{1,6})?", quantities[0]):
        result["quantity_service"] = format(Decimal(quantities[0]), "f")
    return result


def fetch_astri_boq_form_snapshot(record: dict, match: dict) -> dict:
    scope = str(match.get("scope") or match.get("target_type") or "cluster").strip().lower()
    if scope not in {"cluster", "subfeeder", "feeder"}:
        raise ValueError("Scope BOQ ASTRI tidak valid.")
    code = _astri_boq_scope_code(match, scope)
    wo = str(match.get("work_order_number") or "").strip()
    if not code or not wo:
        raise ValueError("Identitas kode/WO permit ASTRI belum lengkap.")
    create_boq = astri_create_boq_link(record, {**match, "scope": scope})
    url = create_boq.get("url") or ""
    if not create_boq.get("enabled") or not url:
        raise ValueError("Form BOQ ASTRI tidak tersedia untuk match ini.")
    page = record["session"].get(url, timeout=60)
    page.raise_for_status()
    from urllib.parse import parse_qs, urlsplit
    expected_url, final_url = urlsplit(url), urlsplit(page.url)
    if (final_url.scheme, final_url.netloc, final_url.path.rstrip("/")) != (expected_url.scheme, expected_url.netloc, expected_url.path.rstrip("/")):
        raise ValueError("Form BOQ ASTRI redirect ke halaman lain.")
    expected_query = parse_qs(expected_url.query, keep_blank_values=True)
    final_query = parse_qs(final_url.query, keep_blank_values=True)
    if expected_query != final_query or any(len(values) != 1 for query in (expected_query, final_query) for values in query.values()):
        raise ValueError("Kontrak URL form BOQ ASTRI berbeda dari target terpilih.")
    forms = re.findall(r'<form\b[^>]*\bid=["\']insert-boq-drm-form["\'][^>]*>.*?</form>', page.text, flags=re.I | re.S)
    if len(forms) != 1:
        raise ValueError("Form BOQ ASTRI tidak dapat diverifikasi.")
    fields = extract_astri_form_fields(forms[0])
    work_orders = [str(value).strip() for name, value in fields if name == "work_order_number"]
    codes = [str(value).strip() for name, value in fields if re.fullmatch(r"boq\[[^]]+\]\[(?:cluster_code|subfeeder_code|feeder_code)\]", name)]
    if not work_orders or set(work_orders) != {wo} or not codes or set(codes) != {code}:
        raise ValueError("Identitas form BOQ ASTRI berbeda dari kode/WO terpilih.")
    return {"suppliers": _parse_astri_boq_supplier_fields(forms[0]), "permit": _parse_astri_boq_permit(fields)}


def fetch_astri_boq_supplier_map(record: dict, match: dict) -> dict[str, dict]:
    return fetch_astri_boq_form_snapshot(record, match)["suppliers"]


def fetch_astri_feeder_requirement_page(record: dict, match: dict) -> str:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/feeder/drm/requirement-document",
        params={
            "feeder_code": match["feeder_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def fetch_astri_subfeeder_requirement_page(record: dict, match: dict) -> str:
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/subfeeder/drm/requirement-document",
        params={
            "subfeeder_code": match["subfeeder_code"],
            "work_order_number": match["work_order_number"],
            "vendor_label": match.get("vendor_label") or "",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.text

def astri_create_feeder_boq_link(record: dict, match: dict) -> dict:
    return astri_create_boq_link(record, {**match, "scope": "feeder"})


def astri_create_subfeeder_boq_link(record: dict, match: dict) -> dict:
    return astri_create_boq_link(record, {**match, "scope": "subfeeder"})


def _astri_scope_match_from_payload(payload: dict, scope: str) -> dict:
    match = astri_payload_match(payload, scope)
    # Keep display fields, but make the scope-specific identity authoritative.
    match["scope"] = scope
    match["target_type"] = scope
    if scope == "subfeeder":
        match["cluster_code"] = match["subfeeder_code"]
    elif scope == "feeder":
        match["cluster_code"] = match["feeder_code"]
        match["target_osp_route_code"] = match["feeder_code"]
    return match


def _astri_scope_query_match(args, scope: str) -> dict:
    match = astri_query_match(args, scope)
    match["scope"] = scope
    match["target_type"] = scope
    if scope == "subfeeder":
        match["cluster_code"] = match["subfeeder_code"]
    elif scope == "feeder":
        match["cluster_code"] = match["feeder_code"]
        match["target_osp_route_code"] = match["feeder_code"]
    return match


def _astri_selected_scope_query_match(record: dict, args, scope: str) -> dict:
    requested = _astri_scope_query_match(args, scope)
    live = select_unique_astri_match(record, requested, scope)
    live["scope"] = scope
    live["target_type"] = scope
    if scope == "subfeeder":
        live["cluster_code"] = live["subfeeder_code"]
    elif scope == "feeder":
        live["cluster_code"] = live["feeder_code"]
        live["target_osp_route_code"] = live["feeder_code"]
    return live


def _astri_submit_scope(
    record: dict,
    match: dict,
    scope: str,
    create_boq: dict,
    items: list[dict],
    pr_description: str,
    permit_rt_rw: str,
    submission_id: str = "",
) -> dict:
    return submit_astri_boq_items(
        record, create_boq, items, pr_description,
        is_subfeeder=scope == "subfeeder", permit_rt_rw=permit_rt_rw,
        status_match=match, status_scope=scope, submission_id=submission_id,
    )

def preview_eva_boq_file(path: Path) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    sheet = "BoQ NRO Cluster" if "BoQ NRO Cluster" in wb.sheetnames else (
        "BoQ NRO All Feeder" if "BoQ NRO All Feeder" in wb.sheetnames else wb.sheetnames[0]
    )
    ws = wb[sheet]
    rows = []
    for row in range(7, ws.max_row + 1):
        code_value = ws.cell(row, 14).value
        if code_value is None or str(code_value).strip() == "":
            continue
        material_qty = ws.cell(row, 4).value or 0
        service_qty = ws.cell(row, 5).value or 0
        if not material_qty and not service_qty:
            continue
        rows.append({
            "row": row,
            "code": str(code_value).strip(),
            "description": str(ws.cell(row, 2).value or "").strip(),
            "unit": str(ws.cell(row, 3).value or "").strip(),
            "material_qty": material_qty,
            "service_qty": service_qty,
            "vendor": "",
            "supplier": "",
            "supplier_kind": "",
        })
    return rows


def _astri_boq_scope_code(match: dict, scope: str) -> str:
    key = {"cluster": "cluster_code", "subfeeder": "subfeeder_code", "feeder": "feeder_code"}[scope]
    return str(match.get(key) or match.get("scope_code") or "").strip()


def _validate_astri_boq_items(items: object) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValueError("Tidak ada item BOQ.")
    if len(items) > 1000:
        raise ValueError("Jumlah item BOQ melebihi batas 1000.")
    clean: list[dict] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item BOQ ke-{index} tidak valid.")
        code = str(item.get("code") or item.get("material_number") or "").strip()
        if not code or len(code) > 100:
            raise ValueError(f"Kode item BOQ ke-{index} tidak valid.")
        clean.append(dict(item))
    return clean


def ensure_astri_boq_owner_id() -> str:
    user = getattr(g, "eva_user", None)
    if not user:
        raise ValueError("Login Google diperlukan.")
    session["astri_boq_owner_id"] = user["id"]
    return user["id"]


def _find_astri_boq_submission_by_attempt_id(
    attempt_id: str,
    *,
    owner_id: str = "",
    session_id: str = "",
) -> dict | None:
    """Recover a lost async response without creating another mutation."""
    try:
        normalized_attempt = str(uuid.UUID(str(attempt_id or "").strip()))
    except (ValueError, AttributeError, TypeError):
        return None
    if not owner_id and not session_id:
        return None
    store = get_astri_boq_submission_store()
    for key in store._redis.scan_iter(match=f"{store._prefix}:submission:*", count=200):
        raw = store._redis.hgetall(key)
        if not raw:
            continue
        try:
            snapshot = json.loads(raw.get("snapshot_json") or "{}")
        except (TypeError, ValueError):
            continue
        if str(snapshot.get("submission_attempt_id") or "") != normalized_attempt:
            continue
        same_owner = bool(owner_id and snapshot.get("owner_id") == owner_id
                          and snapshot.get("eva_user_id") == owner_id)
        if not same_owner:
            continue
        submission_id = str(raw.get("submission_id") or key.rsplit(":", 1)[-1])
        return store.get(submission_id)
    return None


def astri_session_record_for_submission(snapshot: dict) -> dict:
    """Never borrow another browser's account, including the same upstream username."""
    session_id = str(snapshot.get("session_id") or "")
    user_id = str(snapshot.get("eva_user_id") or "")
    if not session_id or not user_id:
        raise LookupError("Sesi ASTRI pengirim tidak tersedia. Login ulang lalu submit kembali.")
    username = str(snapshot.get("session_username") or "").strip()
    if not username:
        raise LookupError("Identitas akun ASTRI pengirim tidak tersedia.")
    return astri_session_record_by_id(session_id, user_id, username)


def enqueue_astri_boq_submission(submission_id: str) -> str:
    from tasks import run_astri_boq_submission_task

    task = run_astri_boq_submission_task.delay(submission_id)
    get_astri_boq_submission_store().update(submission_id, task_id=task.id)
    return str(task.id)


def start_astri_boq_submission(scope: str, job: dict | None = None):
    normalized_scope = str(scope or "").strip().lower()
    payload = request.get_json(silent=True) or {}
    match = _astri_scope_match_from_payload(payload, normalized_scope)
    items = payload.get("items") or []
    if not items and job:
        files = astri_job_upload_files(job)
        if "boq" not in files:
            return jsonify({"error": "Output BOQ EVA tidak tersedia."}), 400
        items = preview_eva_boq_file(files["boq"][0])
    items = [item for item in _validate_astri_boq_items(items) if not _is_astri_permit_item(item)]
    if not items:
        raise ValueError("Tidak ada item BOQ selain permit; permit dikelola ASTRI.")

    require_astri_session()
    sid = str(session.get(ASTRI_SESSION_KEY) or "")
    stored = load_astri_session_store().get(sid)
    if not sid or not isinstance(stored, dict) or not stored.get("cookies"):
        return jsonify({"error": "Sesi ASTRI belum login. Login ulang sebelum Add BOQ."}), 401
    owner_id = ensure_astri_boq_owner_id()

    submission_attempt_id = str(payload.get("submission_attempt_id") or "").strip()
    if submission_attempt_id:
        try:
            submission_attempt_id = str(uuid.UUID(submission_attempt_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("submission_attempt_id Add BOQ tidak valid.") from exc
    mutation_payload = {
        "scope": normalized_scope,
        "match": match,
        "items": items,
        "pr_description": str(payload.get("pr_description") or ""),
        "permit_rt_rw": "",  # Legacy snapshot schema only; never an ASTRI input.
        "attachment_link": str(payload.get("attachment_link") or ""),
        "source_job_id": str((job or {}).get("job_id") or ""),
    }
    if submission_attempt_id:
        mutation_payload["submission_attempt_id"] = submission_attempt_id
    snapshot = {
        **mutation_payload,
        "session_id": sid,
        "session_username": str(stored.get("username") or "").strip(),
        "owner_id": owner_id,
        "eva_user_id": g.eva_user["id"],
        "eva_user_name": str(g.eva_user.get("name") or ""),
        "eva_user_email": str(g.eva_user.get("email") or ""),
    }
    store = get_astri_boq_submission_store()
    identity = store.identity(
        normalized_scope,
        _astri_boq_scope_code(match, normalized_scope),
        str(match.get("work_order_number") or ""),
    )
    supersede_submission_id = ""
    if submission_attempt_id:
        existing = store.get_by_identity(identity)
        if existing and (existing.get("snapshot") or {}).get("eva_user_id") != owner_id:
            return jsonify({"error": "Add BOQ WO ini milik pengguna lain.", "code": "ASTRI_BOQ_SUBMISSION_CONFLICT"}), 409
        if existing and str(existing.get("state") or "") in {"done", "unknown", "partial"}:
            existing_attempt = str((existing.get("snapshot") or {}).get("submission_attempt_id") or "")
            if existing_attempt != submission_attempt_id:
                supersede_submission_id = str(existing.get("submission_id") or "")
    try:
        record, created = store.claim(
            identity,
            store.fingerprint(mutation_payload),
            snapshot,
            supersede_submission_id=supersede_submission_id,
        )
    except SubmissionConflict as exc:
        existing = store.get(exc.submission_id)
        public = store.public(existing) if existing and existing.get("snapshot", {}).get("owner_id") == owner_id else {}
        return jsonify({
            "error": "Add BOQ untuk WO ini sudah berjalan dengan payload berbeda. Jangan submit ulang.",
            "code": "ASTRI_BOQ_SUBMISSION_CONFLICT",
            **public,
        }), 409
    if created:
        try:
            enqueue_astri_boq_submission(record["submission_id"])
        except Exception:
            LOGGER.exception("ASTRI BOQ enqueue outcome unknown submission_id=%s", record["submission_id"])
            latest = store.update_if_current(
                record["submission_id"],
                expected_state=str(record.get("state") or "queued"),
                expected_updated_at=float(record.get("updated_at") or 0),
                state="unknown",
                error_code="ASTRI_BOQ_QUEUE_OUTCOME_UNKNOWN",
                message="Status antrean Add BOQ belum dapat dipastikan. Jangan submit ulang; periksa status proses.",
                finished_at=time.time(),
            ) or store.get(record["submission_id"]) or record
            return jsonify({
                **store.public(latest),
                "error": "Status antrean Add BOQ belum dapat dipastikan. Jangan submit ulang.",
                "code": "ASTRI_BOQ_QUEUE_OUTCOME_UNKNOWN",
                "status_url": url_for("api_astri_boq_submission_status", submission_id=record["submission_id"]),
            }), 202
    latest = store.get(record["submission_id"]) or record
    if latest.get("snapshot", {}).get("owner_id") != owner_id:
        return jsonify({
            "error": "Add BOQ untuk WO ini sedang diproses user lain. Jangan submit ulang.",
            "code": "ASTRI_BOQ_SUBMISSION_IN_PROGRESS",
        }), 409
    state_name = str(latest.get("state") or "queued")
    return jsonify({
        **store.public(latest),
        "code": "ASTRI_BOQ_SUBMISSION_IN_PROGRESS" if state_name in {"queued", "running"} else "ASTRI_BOQ_SUBMISSION_FINISHED",
        "reused": not created,
        "status_url": url_for("api_astri_boq_submission_status", submission_id=latest["submission_id"]),
    }), (202 if state_name in {"queued", "running"} else 200)


def start_astri_boq_submission_response(scope: str, job: dict | None = None):
    try:
        return start_astri_boq_submission(scope, job)
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "ASTRI_BOQ_INPUT_INVALID"}), 400
    except Exception:
        LOGGER.exception("ASTRI BOQ submission start failed scope=%s", scope)
        return jsonify({
            "error": "Add BOQ belum dapat dimulai. Belum ada data yang dikirim ke ASTRI.",
            "code": "ASTRI_BOQ_START_UNAVAILABLE",
        }), 503


@app.route("/api/astri/status")
def api_astri_status():
    user = getattr(g, "eva_user", None)
    linked = EVA_AUTH.store.astri_link(user["id"]) if user else None
    record = current_astri_session()
    if record and not _astri_session_alive(record):
        record = _try_auto_relogin(record, sid=str(record.get("_session_id") or ""))
    return jsonify({
        "connected": bool(record),
        "linked": bool(linked),
        "needs_login": not bool(record),
        "username": (linked or record or {}).get("username", ""),
    })


def _stella_session_token() -> str:
    token = str(session.get("stella_csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["stella_csrf_token"] = token
        session.modified = True
    return token


@app.before_request
def enforce_stella_server_access():
    if not request.path.startswith("/api/stella/"):
        return None
    if not fdt_session_signing_key_secure():
        return jsonify({"error": "Konfigurasi sesi EVA belum aman."}), 503
    if request.path == "/api/stella/session/start":
        return jsonify({"error": "Stella memakai sesi server. Login browser tidak diperlukan dari EVA."}), 410
    return None


@app.route("/api/stella/status")
def api_stella_status():
    """Return non-secret Stella browser status and the session download nonce."""
    csrf_token = _stella_session_token()
    try:
        payload = stella_connector.get_status()
        payload["csrf_token"] = csrf_token
        return jsonify(payload)
    except stella_connector.StellaLoginRequired as exc:
        return jsonify({
            "connected": False,
            "needs_login": not exc.needs_2fa,
            "needs_2fa": exc.needs_2fa,
            "csrf_token": csrf_token,
            "error": str(exc),
        })
    except stella_connector.StellaError as exc:
        return jsonify({"connected": False, "csrf_token": csrf_token, "error": str(exc)}), 503


@app.route("/api/stella/session/start", methods=["POST"])
def api_stella_session_start():
    """Open Stella login in the dedicated VPN browser; credentials stay there."""
    expected = str(session.get("stella_csrf_token") or "")
    provided = str(request.headers.get("X-Stella-CSRF") or "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        return jsonify({"error": "Token sesi Stella tidak valid. Muat ulang EVA."}), 403
    try:
        payload = stella_connector.start_login()
        payload["csrf_token"] = expected
        return jsonify(payload)
    except stella_connector.StellaUnavailable as exc:
        return jsonify({"connected": False, "needs_login": True, "error": str(exc)}), 503
    except stella_connector.StellaError as exc:
        return jsonify({"connected": False, "needs_login": True, "error": str(exc)}), 502


@app.route("/api/stella/options")
def api_stella_options():
    """Load runtime Area/Cluster options from the authenticated Stella browser."""
    area = str(request.args.get("area") or "").strip()
    query = str(request.args.get("query") or "").strip()
    try:
        return jsonify(stella_connector.list_options({"area": area, "query": query}))
    except stella_connector.StellaLoginRequired as exc:
        return jsonify({"error": str(exc), "needs_login": not exc.needs_2fa, "needs_2fa": exc.needs_2fa}), 428
    except stella_connector.StellaFilterError as exc:
        return jsonify({"error": str(exc)}), 400
    except stella_connector.StellaUnavailable as exc:
        return jsonify({"error": str(exc), "connected": False}), 503
    except stella_connector.StellaError as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/stella/homepass/jobs", methods=["POST"])
def api_stella_job_submit():
    expected = str(session.get("stella_csrf_token") or "")
    provided = str(request.headers.get("X-Stella-CSRF") or "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        return jsonify({"error": "Token sesi Stella tidak valid. Cek koneksi ulang."}), 403
    try:
        job = stella_export_jobs.submit(request.get_json(silent=True) or {}, g.eva_user["id"])
        return jsonify(job), 202
    except stella_export_jobs.JobBusy as exc:
        return jsonify({"error": str(exc)}), 409
    except (stella_connector.StellaFilterError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/stella/homepass/jobs/<job_id>")
def api_stella_job_status(job_id):
    owner = g.eva_user["id"]
    if not owner:
        return jsonify({"error": "Sesi ekspor tidak ditemukan. Cek koneksi ulang."}), 403
    try:
        response = jsonify(stella_export_jobs.get(job_id, owner))
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except FileNotFoundError:
        return jsonify({"error": "Ekspor tidak ditemukan."}), 404


@app.route("/api/stella/homepass/jobs/<job_id>/file")
def api_stella_job_file(job_id):
    owner = g.eva_user["id"]
    if not owner:
        return jsonify({"error": "Sesi ekspor tidak ditemukan. Cek koneksi ulang."}), 403
    try:
        job = stella_export_jobs.get(job_id, owner)
        path = stella_export_jobs.file(job_id, owner)
        response = send_file(str(path), as_attachment=True, download_name=job["filename"],
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Stella-Data-Rows"] = str(job["data_rows"])
        response.headers["X-Stella-Cluster-Count"] = str(job["cluster_count"])
        response.headers["X-Stella-Duplicate-Rows"] = str(job["duplicate_homepass_removed"])
        return response
    except FileNotFoundError:
        return jsonify({"error": "Ekspor tidak ditemukan."}), 404
    except stella_export_jobs.JobBusy as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/stella/homepass/download", methods=["POST"])
def api_stella_homepass_download():
    """Download one XLSX containing all Homepass rows for selected clusters."""
    expected = str(session.get("stella_csrf_token") or "")
    provided = str(request.headers.get("X-Stella-CSRF") or "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        return jsonify({"error": "Token sesi Stella tidak valid. Muat ulang EVA."}), 403
    payload = request.get_json(silent=True) or {}
    try:
        raw, meta = stella_connector.download_homepass(payload)
        response = make_response(raw)
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = f'attachment; filename="{secure_filename(meta["filename"])}"'
        response.headers["X-Stella-Data-Rows"] = str(meta["audit"]["data_rows"])
        response.headers["X-Stella-Cluster-Count"] = str(meta["audit"]["cluster_count"])
        response.headers["X-Stella-Duplicate-Rows"] = str(meta["audit"]["duplicate_homepass_removed"])
        return response
    except stella_connector.StellaLoginRequired as exc:
        return jsonify({"error": str(exc), "needs_login": not exc.needs_2fa, "needs_2fa": exc.needs_2fa}), 428
    except stella_connector.StellaFilterError as exc:
        return jsonify({"error": str(exc)}), 400
    except stella_connector.StellaUnavailable as exc:
        return jsonify({"error": str(exc), "connected": False}), 503
    except stella_connector.StellaExportError as exc:
        LOGGER.warning("Stella Homepass multi-cluster export failed: %s", exc)
        return jsonify({"error": str(exc)}), 502

def reconcile_astri_boq_submission(
    record: dict,
    *,
    astri_record: dict | None = None,
    terminal_status: dict | None = None,
    force: bool = False,
) -> dict:
    """GET-only reconciliation; never infer success from Closed alone."""
    store = get_astri_boq_submission_store()
    submission_id = str(record.get("submission_id") or "")
    if not submission_id:
        return record
    current = store.get(submission_id) or record
    allowed_states = {"queued", "reconciling", "partial", "unknown"}
    if force:
        allowed_states.add("running")
    if str(current.get("state") or "") not in allowed_states:
        return current
    record = current
    expected_state = str(record.get("state") or "")
    expected_updated_at = float(record.get("updated_at") or 0)
    if not force and not store.acquire_reconcile_lease(submission_id):
        return record
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    try:
        scope = str(snapshot.get("scope") or "").strip().lower()
        match = _astri_scope_match_from_payload({"match": snapshot.get("match")}, scope)
        session_record = astri_record or astri_session_record_for_submission(snapshot)
        status = terminal_status or fetch_astri_work_order_status(session_record, match, scope)
        if not status.get("boq_terminal"):
            return store.get(submission_id) or record
        existing_result = record.get("result") if isinstance(record.get("result"), dict) else {}
        mutation_plan = record.get("mutation_plan") if isinstance(record.get("mutation_plan"), list) else None
        if record.get("mutation_started") and not mutation_plan:
            return store.update_if_current(
                submission_id,
                expected_state=expected_state,
                expected_updated_at=expected_updated_at,
                state="unknown",
                close_ok=False,
                error_code="ASTRI_BOQ_PLAN_UNAVAILABLE",
                message="Mutation Add BOQ sudah dimulai tetapi mutation plan lengkap tidak tersedia. Jangan submit ulang; periksa ASTRI.",
                finished_at=time.time(),
            ) or record
        # Before mutation, the immutable browser snapshot is the only possible
        # reconciliation target. After mutation begins, only the persisted exact
        # post-parse/autofix outbound plan may certify completion.
        expected_items = (
            _validate_astri_boq_items(mutation_plan)
            if mutation_plan
            else _validate_astri_boq_items(snapshot.get("items"))
        )
        work_order_number = str(match.get("work_order_number") or "").strip()

        reconciliation = fetch_boq_reconciliation(
            session_record["session"],
            ASTRI_BASE_URL,
            scope,
            _astri_boq_scope_code(match, scope),
            expected_items,
            work_order_number,
            "" if mutation_plan else str(snapshot.get("permit_rt_rw") or ""),
            "" if mutation_plan else str(match.get("vendor_name") or match.get("assigned_vendor_name") or ""),
            allow_unexpected=bool(mutation_plan),
        )
        result = {
            **existing_result,
            "processed_count": int(reconciliation.get("live_count") or 0),
            "expected_count": int(reconciliation.get("expected_count") or 0),
            "duplicate_count": int(reconciliation.get("duplicate_count") or 0),
            "missing_count": int(reconciliation.get("missing_count") or 0),
            "unexpected_count": int(reconciliation.get("unexpected_count") or 0),
            "reconciled": bool(reconciliation.get("matched")),
        }
        label = str(status.get("latest_status_label") or "terminal")
        if reconciliation.get("matched"):
            from astri_boq_history import build_success_event
            finished_at = time.time()
            history_event = build_success_event(
                {**record, "finished_at": finished_at, "result": result}, status, reconciliation
            )
            changes = {"history_event": history_event} if history_event is not None else {}
            return store.update_if_current(
                submission_id,
                expected_state=expected_state,
                expected_updated_at=expected_updated_at,
                state="done",
                close_ok=True,
                error_code="ASTRI_BOQ_RECONCILED",
                message=f"WO sudah {label}; item BOQ terverifikasi lengkap tanpa duplikat.",
                result=result,
                finished_at=finished_at,
                **changes,
            ) or record
        mismatch_message = f"WO sudah {label}, tetapi item BOQ belum cocok. Jangan submit ulang; periksa ASTRI."
        mismatch_skipped = _astri_boq_skipped_codes(record)
        if mismatch_skipped:
            mismatch_message = ("Pricelist not available untuk material: %s. %s"
                                % (", ".join(mismatch_skipped), mismatch_message))
        return store.update_if_current(
            submission_id,
            expected_state=expected_state,
            expected_updated_at=expected_updated_at,
            state="unknown",
            close_ok=False,
            error_code="ASTRI_BOQ_RECONCILIATION_MISMATCH",
            message=mismatch_message,
            result=result,
            finished_at=time.time(),
        ) or record
    except Exception:
        LOGGER.warning("ASTRI BOQ GET-only reconciliation unavailable submission_id=%s", submission_id, exc_info=True)
        unavailable_message = "Status akhir Add BOQ belum dapat diverifikasi. Jangan submit ulang; periksa ASTRI."
        unavailable_skipped = _astri_boq_skipped_codes(record)
        if unavailable_skipped:
            unavailable_message = ("Pricelist not available untuk material: %s. %s"
                                   % (", ".join(unavailable_skipped), unavailable_message))
        return store.update_if_current(
            submission_id,
            expected_state=expected_state,
            expected_updated_at=expected_updated_at,
            state="unknown" if record.get("mutation_started") else str(record.get("state") or "unknown"),
            close_ok=False,
            error_code="ASTRI_BOQ_RECONCILIATION_UNAVAILABLE",
            message=unavailable_message,
        ) or record


@app.route("/api/astri/boq/submissions/recover")
def api_astri_boq_submission_recover():
    """Recover an async submission ID after a lost POST response; never enqueue."""
    attempt_id = str(request.args.get("attempt_id") or "").strip()
    owner_id = g.eva_user["id"]
    session_id = str(session.get(ASTRI_SESSION_KEY) or "")
    record = _find_astri_boq_submission_by_attempt_id(
        attempt_id,
        owner_id=owner_id,
        session_id=session_id,
    )
    if not record:
        return jsonify({
            "found": False,
            "code": "ASTRI_BOQ_SUBMISSION_NOT_FOUND",
        }), 404
    return jsonify({
        **get_astri_boq_submission_store().public(record),
        "found": True,
        "recovered": True,
        "code": "ASTRI_BOQ_SUBMISSION_RECOVERED",
        "status_url": url_for("api_astri_boq_submission_status", submission_id=record["submission_id"]),
    })


@app.route("/api/astri/boq/submissions/<submission_id>")
def api_astri_boq_submission_status(submission_id: str):
    store = get_astri_boq_submission_store()
    record = store.get(submission_id)
    if not record:
        return jsonify({"error": "Status Add BOQ tidak ditemukan."}), 404
    if record.get("snapshot", {}).get("eva_user_id") != g.eva_user["id"]:
        return jsonify({"error": "Status Add BOQ tidak ditemukan."}), 404
    state_name = str(record.get("state") or "")
    if state_name in {"reconciling", "partial", "unknown"}:
        record = reconcile_astri_boq_submission(record)
    elif state_name == "running" and time.time() - float(record.get("updated_at") or 0) > 900:
        record = reconcile_astri_boq_submission(record, force=True)
    return jsonify(store.public(record))


def _astri_boq_skipped_codes(record: dict) -> list[str]:
    """Material codes ASTRI rejected for a missing vendor pricelist (last checkpoint)."""
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    items = result.get("items") if isinstance(result.get("items"), list) else []
    codes: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("skipped_no_price"):
            code = str(item.get("code") or "").strip()
            if code and code not in codes:
                codes.append(code)
    return codes


def run_astri_boq_submission(submission_id: str) -> None:
    store = get_astri_boq_submission_store()
    if not store.mark_running(submission_id):
        return
    record = store.get(submission_id) or {}
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    identity_hash = str(record.get("identity_hash") or "")
    try:
        scope = str(snapshot.get("scope") or "").strip().lower()
        match = _astri_scope_match_from_payload({"match": snapshot.get("match")}, scope)
        astri_record = astri_session_record_for_submission(snapshot)
        blocked = astri_boq_live_mutation_gate(astri_record, match, scope)
        if blocked:
            code = str(blocked.get("code") or "ASTRI_BOQ_BLOCKED")
            status = blocked.get("wo_status") or {}
            if code == "ASTRI_BOQ_WO_TERMINAL":
                pending = _astri_boq_worker_update(
                    submission_id,
                    state="reconciling",
                    close_ok=False,
                    error_code=code,
                    message=f"WO sudah {status.get('latest_status_label') or 'terminal'}; memverifikasi item BOQ tanpa submit ulang.",
                ) or store.get(submission_id) or record
                reconcile_astri_boq_submission(
                    pending,
                    astri_record=astri_record,
                    terminal_status=status,
                    force=True,
                )
            else:
                latest = _astri_boq_worker_update(
                    submission_id,
                    state="blocked-before-mutation",
                    close_ok=False,
                    error_code=code,
                    message=str(blocked.get("error") or "Add BOQ dibatalkan sebelum mutation."),
                    finished_at=time.time(),
                ) or store.get(submission_id)
                if latest and latest.get("state") == "blocked-before-mutation" and not latest.get("mutation_started"):
                    store.release_identity(identity_hash, submission_id)
            return
        # History uses a server-observed pre-mutation status, never browser state.
        initial_status = fetch_astri_work_order_status(astri_record, match, scope)
        context = _astri_boq_worker_update(
            submission_id, history_context={"initial_status": initial_status},
        )
        if not context or str(context.get("state") or "") != "running":
            raise AstriMutationBlocked({"_http_status": 409, "code": "ASTRI_BOQ_WORKER_STALE",
                                        "error": "Proses Add BOQ bukan lagi pemilik state aktif."})
        create_boq = astri_create_boq_link(astri_record, match)
        if not create_boq.get("enabled") or not create_boq.get("url"):
            raise ValueError(str(create_boq.get("error") or "Link BOQ ASTRI tidak tersedia."))
        result = _astri_submit_scope(
            astri_record,
            match,
            scope,
            create_boq,
            _validate_astri_boq_items(snapshot.get("items")),
            str(snapshot.get("pr_description") or ""),
            str(snapshot.get("permit_rt_rw") or ""),
            submission_id=submission_id,
        )
        close_ok = bool(result.get("ok") and result.get("close_ok"))
        checkpoint = store.get(submission_id) or record
        checkpoint_result = checkpoint.get("result") if isinstance(checkpoint.get("result"), dict) else {}
        skipped_codes = [str(code) for code in (result.get("skipped_codes") or []) if str(code).strip()]
        if close_ok:
            partial_message = "ASTRI menerima Close WO; memverifikasi status dan item BOQ."
        elif skipped_codes:
            partial_message = ("Pricelist not available untuk material: %s. Work order belum di-close."
                               % ", ".join(skipped_codes))
        else:
            partial_message = "Add BOQ belum lengkap. Jangan submit ulang; periksa hasil ASTRI."
        pending = _astri_boq_worker_update(
            submission_id,
            state="reconciling" if close_ok else "partial",
            close_ok=False,
            error_code="" if close_ok else "ASTRI_BOQ_PARTIAL",
            message=partial_message,
            result={
                **checkpoint_result,
                "processed_count": len(result.get("inserted") or []) + len(result.get("updated") or []),
                "ok_count": int((result.get("summary") or {}).get("ok") or 0),
                "failed_count": int((result.get("summary") or {}).get("failed") or 0),
                "skipped_count": int((result.get("summary") or {}).get("skipped_no_price") or 0),
                "inserted_count": len(result.get("inserted") or []),
                "updated_count": len(result.get("updated") or []),
                "already_present_count": len(result.get("already_present") or []),
                "vendor_mismatch_count": len(result.get("vendor_mismatches") or []),
                "skipped_codes": skipped_codes,
            },
            finished_at=time.time() if not close_ok else None,
        ) or checkpoint
        if close_ok:
            reconcile_astri_boq_submission(pending, astri_record=astri_record, force=True)
    except AstriMutationBlocked as exc:
        latest = store.get(submission_id) or {}
        mutation_started = bool(latest.get("mutation_started"))
        state_name = "unknown" if mutation_started else "blocked-before-mutation"
        skipped_codes = _astri_boq_skipped_codes(latest)
        blocked_message = "Hasil Add BOQ belum dapat dipastikan. Jangan submit ulang; periksa ASTRI."
        if skipped_codes:
            blocked_message = ("Pricelist not available untuk material: %s. %s"
                               % (", ".join(skipped_codes), blocked_message))
        latest = _astri_boq_worker_update(
            submission_id,
            state=state_name,
            close_ok=False,
            error_code=str(exc.payload.get("code") or "ASTRI_BOQ_BLOCKED"),
            message=(blocked_message
                     if mutation_started else str(exc.payload.get("error") or "Add BOQ dibatalkan sebelum mutation.")),
            finished_at=time.time(),
        ) or store.get(submission_id)
        if latest and latest.get("state") == "blocked-before-mutation" and not latest.get("mutation_started"):
            store.release_identity(identity_hash, submission_id)
    except Exception:
        LOGGER.exception("ASTRI BOQ worker failed submission_id=%s", submission_id)
        latest = store.get(submission_id) or {}
        mutation_started = bool(latest.get("mutation_started"))
        skipped_codes = _astri_boq_skipped_codes(latest)
        failed_message = "Hasil Add BOQ belum dapat dipastikan. Jangan submit ulang; periksa ASTRI."
        if skipped_codes:
            failed_message = ("Pricelist not available untuk material: %s. %s"
                              % (", ".join(skipped_codes), failed_message))
        latest = _astri_boq_worker_update(
            submission_id,
            state="unknown" if mutation_started else "blocked-before-mutation",
            close_ok=False,
            error_code="ASTRI_BOQ_RESULT_UNKNOWN" if mutation_started else "ASTRI_BOQ_PRECHECK_FAILED",
            message=(failed_message
                     if mutation_started else "Add BOQ dibatalkan sebelum data dikirim ke ASTRI."),
            finished_at=time.time(),
        ) or store.get(submission_id)
        if latest and latest.get("state") == "blocked-before-mutation" and not latest.get("mutation_started"):
            store.release_identity(identity_hash, submission_id)

@app.route("/api/astri/login", methods=["POST"])
def api_astri_login():
    payload = request.get_json(silent=True) or {}
    try:
        info = astri_login_session(payload.get("username"), payload.get("password"))
        return jsonify({"connected": True, "linked": True, "username": info["username"]})
    except PermissionError as exc:
        return jsonify({"connected": False, "error": str(exc), "code": "ASTRI_ACCOUNT_ALREADY_LINKED"}), 409
    except ValueError as exc:
        return jsonify({"connected": False, "error": str(exc)}), 400
    except Exception as exc:
        LOGGER.warning("ASTRI login gagal (%s)", type(exc).__name__)
        return jsonify({"connected": False, "error": "Login ASTRI belum berhasil. Coba lagi."}), 503

@app.route("/api/astri/logout", methods=["POST"])
def api_astri_logout():
    user = getattr(g, "eva_user", None)
    if not user:
        return jsonify({"error": "Masuk EVA terlebih dahulu."}), 401
    with astri_user_lock(user["id"]):
        # Revocation first: no worker or stale browser can reuse removed SIDs.
        sids = EVA_AUTH.store.unlink_astri(user["id"])
        for sid in sids:
            ASTRI_SESSIONS.pop(sid, None)
            update_astri_session_store(sid, None)
    session.pop(ASTRI_SESSION_KEY, None)
    session.modified = True
    return jsonify({"connected": False, "linked": False, "username": ""})


def _clean_pr_scope_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^#?[A-Z]{2,}\d+\s*[:\-]?\s*", "", text, flags=re.I)
    text = re.sub(r"\b(SUKTEEDER|SUBFEEDER|CLUSTER)\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -:")
    return text


def _first_match_value(match: dict, *keys: str) -> str:
    for key in keys:
        value = (match or {}).get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _boq_item_qty(item: dict) -> float:
    for key in ("service_qty", "material_qty", "qty", "quantity", "volume"):
        try:
            value = item.get(key)
            if value not in (None, ""):
                return float(str(value).replace(".", "").replace(",", "."))
        except (TypeError, ValueError):
            continue
    return 0.0


def _boq_length_m(items: list[dict], match: dict) -> str:
    candidates = []
    for item in items or []:
        hay = " ".join(str(item.get(k, "")) for k in ("code", "description", "equipment_name", "name")).lower()
        unit = str(item.get("unit", item.get("uom", ""))).lower()
        qty = _boq_item_qty(item)
        if qty <= 0:
            continue
        if "project management" in hay or "project mgmt" in hay or (unit in ("m", "meter", "meters") and "management" in hay):
            return str(int(qty) if qty.is_integer() else qty)
        if unit in ("m", "meter", "meters") or "meter" in unit:
            candidates.append(qty)
    if candidates:
        qty = max(candidates)
        return str(int(qty) if qty.is_integer() else qty)
    raw = _first_match_value(match, "length", "target_osp_route_length", "osp_route_length", "target_feeder_length", "target_subfeeder_length")
    return raw


def _is_internal_team_vendor(value) -> bool:
    return str(value or "").strip().lower().replace("_", " ") in {"internal team", "internalteam"}


def _internal_boq_items(items: list[dict]) -> list[str]:
    out = []
    for item in items or []:
        # Supplier comes directly from ASTRI boq[CODE][vendor_name].
        supplier = str(
            item.get("supplier")
            or item.get("vendor_name")
            or item.get("vendor")
            or ""
        ).strip().lower()
        if supplier not in ("internal", "internal team"):
            continue
        name = str(item.get("description") or item.get("equipment_name") or item.get("name") or item.get("code") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _idr(value: str) -> str:
    try:
        n = int(float(str(value).replace(".", "").replace(",", ".")))
    except (TypeError, ValueError):
        return str(value or "").strip()
    return f"{n:,}".replace(",", ".")



def _numlike(value) -> float:
    try:
        txt = str(value or "").replace(".", "").replace(",", ".").strip()
        return float(txt) if txt else 0.0
    except Exception:
        return 0.0


def _boq_item_qty(item: dict) -> float:
    return _numlike(item.get("quantity_material") or item.get("qty_material") or item.get("material_qty") or item.get("material")) + _numlike(item.get("quantity_service") or item.get("qty_service") or item.get("service_qty") or item.get("service"))


def _boq_item_code(item: dict) -> str:
    return _astri_boq_supplier_key(
        item.get("material_number")
        or item.get("code")
        or item.get("material_code")
        or item.get("equipment_name")
        or ""
    )


def _is_internal_supplier_value(value) -> bool:
    return "internal" in str(value or "").strip().lower().replace("_", " ")



def _astri_form_supplier_map_from_parse_data(parse_data: list[tuple[str, str]]) -> dict[str, str]:
    suppliers: dict[str, str] = {}
    for name, value in parse_data or []:
        m = re.match(r"boq\[([^\]]+)\]\[vendor_name\]", str(name or ""))
        if m:
            suppliers[m.group(1)] = str(value or "").strip()
    return suppliers


def eva_ai_autofix_boq_payload(parsed_items: list[dict], parse_data: list[tuple[str, str]], permit_rt_rw: str = "") -> dict:
    """Fast EVA-AI/autofix layer for common Add BOQ regressions.
    It uses live ASTRI form data first, deterministic fixes second; no ASTRI submit here.
    """
    supplier_map = _astri_form_supplier_map_from_parse_data(parse_data)
    fixes=[]
    fixed=[]
    for item in parsed_items or []:
        if _is_astri_permit_item(item):
            continue
        data=dict(item)
        code=_boq_item_code(data)
        # ASTRI's per-item form value is authoritative, including Internal.
        # Never preserve a conflicting parser/project-vendor value.
        if code and code in supplier_map:
            exact_supplier = str(supplier_map[code] or "").strip()
            if data.get('vendor_name') != exact_supplier:
                data['vendor_name'] = exact_supplier
                fixes.append(f"supplier_fix:{code}->{exact_supplier or '<empty>'}")

        fixed.append(data)
    return {'items': fixed, 'fixes': fixes, 'supplier_map_count': len(supplier_map)}

def astri_boq_qa_gate(*, input_items: list[dict], parsed_items: list[dict], pr_description: str, permit_rt_rw: str) -> dict:
    """Deterministic Add BOQ safety gate. EVA-AI may explain anomalies later,
    but this gate blocks known regressions before ASTRI submit."""
    issues=[]; warnings=[]
    src=[i for i in (input_items or []) if _boq_item_qty(i)>0]
    parsed=[i for i in (parsed_items or []) if _boq_item_qty(i)>0 or "500002155" in _boq_item_code(i) or "RT/RW" in str(i.get("description","")).upper()]
    if src and len(parsed) < max(1, int(len(src)*0.85)):
        warnings.append(f"Parsed BOQ lebih sedikit: source {len(src)} row, ASTRI parsed {len(parsed)} row. Periksa merge material/service sebelum submit.")
    if len(parsed)==0:
        issues.append("ASTRI parse menghasilkan 0 item; submit diblokir supaya material BOQ tidak hilang.")
    missing_vendor=[]
    internal_desc=[]
    permit_seen=False
    for it in parsed:
        code=_boq_item_code(it)
        desc=str(it.get("description") or it.get("short_text") or "")
        vendor=str(it.get("vendor_name") or "").strip()
        if not vendor:
            missing_vendor.append(code or desc[:40] or "UNKNOWN")
        if _is_internal_supplier_value(vendor):
            internal_desc.append(desc.strip() or code or "Internal item")
        if code=="500002155" or "RT/RW" in desc.upper() or "COMMUNITY PERMIT" in desc.upper():
            permit_seen=True
            if permit_rt_rw and permit_rt_rw.strip() not in {"", "0", "0,00", "0.00"}:
                if _numlike(it.get("quantity_service")) != 1:
                    issues.append("Permit RT/RW ada nominal tapi quantity_service bukan 1.")
                if _numlike(it.get("override_price_service")) <= 0:
                    issues.append("Permit RT/RW ada nominal tapi override_price_service kosong/0.")
    if missing_vendor:
        issues.append("Supplier kosong untuk item: " + ", ".join(missing_vendor[:8]))
    if permit_rt_rw and permit_rt_rw.strip() not in {"", "0", "0,00", "0.00"} and not permit_seen:
        warnings.append("permit_rt_rw dikirim tapi item permit RT/RW tidak ditemukan di parsed BOQ.")
    desc=(pr_description or "").strip()
    if internal_desc and "Exclude" not in desc:
        issues.append("PR Description salah: ada supplier internal tapi tidak memakai Exclude list.")
    if not internal_desc and "All Material Provide By Vendor" not in desc:
        warnings.append("PR Description tidak berisi 'All Material Provide By Vendor' padahal tidak ada supplier internal.")
    if re.search(r"\b2(?:60|65|\.65)\s*%", desc) and "2.65%" not in desc:
        issues.append("PPh/permit description terdeteksi janggal; pastikan display 2.65%, bukan 265%.")
    return {"ok": not issues, "issues": issues, "warnings": warnings, "source_rows": len(src), "parsed_rows": len(parsed)}

def build_auto_pr_description(scope: str, match: dict, items: list[dict], permit_rt_rw: str = "", pph_pct: float = 0.0, donation_nominal: str = "", donation_vendor: str = "") -> str:
    scope = (scope or "cluster").lower()
    match = match or {}
    items = items or []
    internal = _internal_boq_items(items)
    donation_inhouse = _is_internal_team_vendor(donation_vendor or match.get("donation_vendor"))
    material_note = "\n".join(["Exclude", *internal]) if internal else "All Material Provide By Vendor"

    if scope == "feeder":
        code = _first_match_value(match, "feeder_code", "target_feeder_code", "cluster_code", "code")
        name = _clean_pr_scope_name(_first_match_value(match, "feeder_name", "target_feeder_name", "cluster_name", "name", "query"))
        length = _boq_length_m(items, match)
        lines = [f"{code} : Roll Out - {name}".strip(" :")]
        if length:
            lines += ["", f"Length : {length}m"]
        lines += ["", material_note]
        return "\n".join(lines).strip()

    if scope == "subfeeder":
        code = _first_match_value(match, "subfeeder_code", "target_subfeeder_code", "cluster_code", "code")
        name = _clean_pr_scope_name(_first_match_value(match, "subfeeder_name", "target_subfeeder_name", "cluster_name", "name", "query"))
        hp = _first_match_value(match, "homepass", "target_subfeeder_drm_homepass", "target_cluster_drm_homepass")
        length = _boq_length_m(items, match)
        area = _first_match_value(match, "area", "area_name") or "BKS"
        lines = [f"{code} : Roll Out - SUBFEEDER {name}".strip()]
        if hp:
            lines += ["", f"Total HP : {hp} HP"]
        if length:
            if not hp:
                lines.append("")
            lines.append(f"Length : {length}m")
        lines += ["", material_note]
        return "\n".join(lines).strip()

    code = _first_match_value(match, "cluster_code", "target_cluster_code", "code")
    name = _clean_pr_scope_name(_first_match_value(match, "cluster_name", "target_cluster_name", "name", "query"))
    hp = _first_match_value(match, "homepass", "target_cluster_drm_homepass")
    lines = [f"{code} : Roll Out - Cluster {name}".strip()]
    if hp:
        lines += ["", f"Total HP : {hp} HP"]
    if donation_nominal or permit_rt_rw:
        if not hp:
            lines.append("")
        # For vendor (non-inhouse) with PPh, the permit_rt_rw already includes PPh
        donation_amount = permit_rt_rw if (pph_pct > 0 and not donation_inhouse and donation_nominal) else (donation_nominal or permit_rt_rw)
        pph_text = f"{pph_pct * 100:g}%"
        donasi_label = "(Inhouse)" if donation_inhouse else (f"(include PPh {pph_text})" if pph_pct > 0 else "(Inhouse)")
        lines.append(f"Donation Approve : IDR {_idr(donation_amount)} {donasi_label}")
    lines += ["", material_note]
    return "\n".join(lines).strip()


@app.route("/api/astri/boq/auto-description", methods=["POST"])
def api_astri_boq_auto_description():
    payload = request.get_json(silent=True) or {}
    try:
        pph_pct = float(payload.get("match", {}).get("pph_pct", 0) or 0)
        # Donation API returns percent points (e.g. 2.65); the PR formatter uses a decimal fraction.
        if pph_pct > 1:
            pph_pct /= 100.0
        # Fallback: if pph_pct==0 but vendor known, look up PPH DB directly
        if pph_pct == 0:
            match = payload.get("match") or {}
            vendor_name = match.get("vendor_label") or match.get("vendor_name") or ""
            if vendor_name:
                import sqlite3 as _sqlite3, re as _re
                db_path = os.path.join(os.path.dirname(__file__), "data", "pph_vendor.db")
                if os.path.exists(db_path):
                    conn = _sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT vendor, pph_vendor FROM pph_vendor")
                    all_v = cur.fetchall()
                    conn.close()
                    def _norm(x):
                        return _re.sub(r"[^a-z0-9]", "", (x or "").lower())
                    vn = _norm(vendor_name)
                    for v, pph in all_v:
                        if _norm(v) == vn:
                            try:
                                pph_pct = float(str(pph).replace(",",".").replace("%","").strip()) / 100.0
                            except ValueError:
                                pass
                            break
                    if pph_pct == 0:
                        keywords_norm = [k for k in _re.findall(r"[a-z0-9]+", vendor_name.lower()) if len(k) >= 3]
                        best = None
                        for v, pph in all_v:
                            vnorm = _norm(v)
                            if any(k in vnorm for k in keywords_norm):
                                if best is None or len(v) > len(best[0]):
                                    best = (v, pph)
                        if best and best[1]:
                            try:
                                pph_pct = float(str(best[1]).replace(",",".").replace("%","").strip()) / 100.0
                            except ValueError:
                                pass
        description = build_auto_pr_description(
            payload.get("scope") or "cluster",
            payload.get("match") or {},
            payload.get("items") or [],
            payload.get("permit_rt_rw") or "",
            pph_pct=pph_pct,
            donation_nominal=payload.get("donation_nominal") or "",
            donation_vendor=payload.get("donation_vendor") or "",
        )
        return jsonify({"description": description})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ auto description failed")
        return jsonify({"error": str(exc), "description": ""}), 400






# --- ASTRI Magic Login ---
try:
    from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
except Exception:
    URLSafeTimedSerializer = None
    SignatureExpired = Exception
    BadTimeSignature = Exception

ASTRI_MAGIC_LOGIN_HTML = '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8"><title>Login ASTRI...</title><meta name="referrer" content="no-referrer"><style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f0fdf4;color:#065f46}.box{text-align:center;padding:40px}.spinner{width:48px;height:48px;border:4px solid #d1fae5;border-top-color:#10b981;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}@keyframes spin{to{transform:rotate(360deg)}}h2{margin:0 0 8px}p{color:#6b7280;font-size:14px}</style></head><body><div class="box"><div class="spinner"></div><h2>Login ASTRI otomatis...</h2><p>Mengalihkan ke halaman tujuan...</p></div><iframe name="login_frame" style="display:none"></iframe><script>var c=__CREDS_JSON__;var red=__REDIRECT_URL__;var f=document.createElement("form");f.method="POST";f.target="login_frame";f.action="__LOGIN_URL__";f.style.display="none";[{n:"user-identity",v:c.user},{n:"password",v:c.pass},{n:"csrf_name",v:""},{n:"csrf_value",v:""}].forEach(function(i){var x=document.createElement("input");x.type="hidden";x.name=i.n;x.value=i.v;f.appendChild(x);});document.body.appendChild(f);setTimeout(function(){f.submit();setTimeout(function(){if(red)window.location.href=red;},2000);},350);</script></body></html>'

def _astri_magic_serializer():
    if URLSafeTimedSerializer is None:
        raise RuntimeError("itsdangerous unavailable")
    salt = os.getenv("ASTRI_MAGIC_LOGIN_SALT", "eva-astri-magic-login-v1")
    return URLSafeTimedSerializer(salt + Config.SECRET_KEY)

@app.route("/api/astri/magic-token", methods=["POST"])
def api_astri_magic_token():
    return jsonify({"error": "Login ASTRI memakai akun pribadi, bukan tautan kredensial."}), 410

@app.route("/astri/magic-login/<token>")
def astri_magic_login_page(token):
    return jsonify({"error": "Login ASTRI memakai akun pribadi, bukan tautan kredensial."}), 410
# --- End ASTRI Magic Login ---

@app.route("/api/astri/feeder/match/<job_id>")
def api_astri_feeder_match(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        if not code:
            return jsonify({"matched": False, "query": cluster_name})
        items = search_astri_feeders(record, code)
        if items:
            return jsonify(items[0])
        return jsonify({"matched": False, "query": cluster_name})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder match failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/match/<job_id>")
def api_astri_match(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
        return jsonify(astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/subfeeder/match/<job_id>")
def api_astri_subfeeder_match(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
        # Extract code from cluster_name (e.g. "BKS002900 - SF VILLA INDAH PERMAI RW 33" -> "BKS002900")
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        if not code:
            return jsonify({"matched": False, "query": cluster_name})
        items = search_astri_subfeeders(record, code)
        if items:
            return jsonify(items[0])
        return jsonify({"matched": False, "query": cluster_name})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder match failed")
        return jsonify({"error": str(exc)}), 400

@app.route("/api/astri/search")
def api_astri_search():
    try:
        record = require_astri_session()
        query_raw = request.args.get("q", "").strip()
        target_type = (request.args.get("type") or "auto").strip().lower()
        status = request.args.get("status", "").strip()
        query = normalize_match_text(query_raw)
        if not query and not status:
            return jsonify({"items": []})

        if target_type == "subfeeder":
            return jsonify({"items": search_astri_subfeeders(record, query_raw, status=status)[:100]})
        if target_type == "feeder":
            return jsonify({"items": search_astri_feeders(record, query_raw, status=status)[:100]})
        if target_type == "cluster":
            return jsonify({"items": search_astri_clusters_v3(record, query_raw, status=status)[:100]})

        # auto: search all types and merge
        merged = []
        seen = set()
        for search_fn in (lambda: search_astri_clusters_v3(record, query_raw, status=status), lambda: search_astri_subfeeders(record, query_raw, status=status), lambda: search_astri_feeders(record, query_raw, status=status)):
            for item in search_fn():
                code = item.get("cluster_code", "") or item.get("subfeeder_code", "") or item.get("feeder_code", "")
                key = (item.get("target_type", ""), code, item.get("work_order_number", ""))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return jsonify({"items": merged[:200]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def _astri_rfs_document_identity(values: dict) -> dict[str, str]:
    scope, spec = document_abd_scope(values.get("scope"))
    identity_key = str(spec["identity_key"])
    identity = {
        "scope": scope,
        identity_key: str(values.get(identity_key) or "").strip(),
        "work_order_number": str(values.get("work_order_number") or "").strip(),
        "vendor_label": str(values.get("vendor_label") or "").strip(),
    }
    if not identity[identity_key] or not identity["work_order_number"] or not identity["vendor_label"]:
        label = "route feeder" if scope == "feeder" else scope
        raise ValueError(f"{label}, WO, dan vendor Document RFS wajib dipilih dari hasil pencarian.")
    limits = {identity_key: 240, "work_order_number": 180, "vendor_label": 180}
    for key, limit in limits.items():
        value = identity[key]
        if len(value) > limit or any(ord(char) < 32 for char in value):
            raise ValueError("Identitas Document RFS tidak valid.")
    return identity


def _astri_rfs_document_spec(identity: dict[str, str]) -> dict:
    _scope, spec = document_abd_scope(identity.get("scope"))
    return spec


def _astri_rfs_document_page_params(identity: dict[str, str]) -> dict[str, str]:
    spec = _astri_rfs_document_spec(identity)
    identity_key = str(spec["identity_key"])
    return {
        identity_key: str(identity[identity_key]),
        "work_order_number": str(identity["work_order_number"]),
        "vendor_label": str(identity["vendor_label"]),
    }


def _fetch_astri_rfs_document_page(record: dict, identity: dict[str, str]) -> tuple[str, dict]:
    scope = str(identity.get("scope") or "cluster")
    spec = _astri_rfs_document_spec(identity)
    response = record["session"].get(
        f"{ASTRI_BASE_URL}{spec['detail_path']}",
        params=_astri_rfs_document_page_params(identity),
        timeout=120,
    )
    response.raise_for_status()
    parsed = parse_document_page(response.text, scope=scope)
    return response.text, parsed


def _astri_rfs_upload_form_is_usable(defaults: dict, identity: dict[str, str]) -> bool:
    spec = _astri_rfs_document_spec(identity)
    identity_key = str(spec["identity_key"])
    allowed_raw = str(defaults.get("rfs_document[allowed_file_type]") or "")
    allowed_tokens = set(re.findall(r"[a-z0-9]+", allowed_raw.lower()))

    checks = {
        "identity": (
            str(defaults.get(str(spec["form_identity_key"])) or "").strip(),
            identity[identity_key],
        ),
        "work_order": (
            str(defaults.get("rfs_document[work_order_number]") or "").strip(),
            identity["work_order_number"],
        ),
        "vendor": (
            str(defaults.get("rfs_document[vendor_label]") or "").strip(),
            identity["vendor_label"],
        ),
        "phase": (
            str(defaults.get("rfs_document[phase]") or "").strip().lower(),
            "rfs",
        ),
        "type_name": (
            str(defaults.get("rfs_document[type_name]") or "").strip(),
            spec["type_name"],
        ),
        "uuid": (
            str(defaults.get(str(spec["form_uuid_key"])) or "").strip() if spec.get("form_uuid_key") else "n/a",
            "(non-empty)" if spec.get("form_uuid_key") else "n/a",
        ),
        "allowed_file_type": (
            allowed_raw or "(empty)",
            "must contain %r" % DOCUMENT_ABD_EXTENSION,
        ),
    }
    failures = []
    for name, (actual, expected) in checks.items():
        if name == "allowed_file_type":
            ok = DOCUMENT_ABD_EXTENSION in allowed_tokens
        elif name == "uuid":
            ok = True if not spec.get("form_uuid_key") else bool(actual)
        else:
            ok = actual == expected
        if not ok:
            failures.append("%s: got %r, want %r" % (name, actual, expected))

    if failures:
        LOGGER.warning(
            "KMZ upload DISABLED for %s — %d/%d checks failed: %s | defaults keys=%s",
            identity.get(identity_key, "?"), len(failures), len(checks),
            "; ".join(failures), sorted(defaults.keys()),
        )
    else:
        LOGGER.info("KMZ upload enabled for %s — all %d checks passed",
                    identity.get(identity_key, "?"), len(checks))
    return not failures


def _astri_rfs_upload_defaults(parsed: dict, identity: dict[str, str]) -> dict[str, str]:
    defaults = parsed.get("upload_defaults") or {}
    if not _astri_rfs_upload_form_is_usable(defaults, identity):
        raise ValueError("Form upload Document RFS tidak tersedia atau identitasnya tidak cocok.")
    return defaults


def _astri_rfs_original_filename(uploaded_filename: str) -> str:
    raw = str(uploaded_filename or "")
    filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename or not filename.strip():
        raise ValueError("Nama file KMZ tidak tersedia.")
    return filename


def _astri_rfs_filename_signature(value: str) -> str:
    """Collapse separators/whitespace so ASTRI's own renaming still compares equal.

    ASTRI stores uploads as `<type>_<code>_<timestamp>_<name>.kmz` and rewrites the
    operator filename on the way in (e.g. `BKS002466 - GRIYA ASRI RW 32.kmz` becomes
    `..._BKS002466  GRIYA ASRI RW 32.kmz`). Comparing raw strings therefore reports a
    false negative on a genuinely successful upload.
    """
    text = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if text.endswith(".kmz"):
        text = text[:-4]
    return re.sub(r"[^a-z0-9]+", "", text)


def _astri_rfs_verified_filename_matches(item: dict, identity: dict[str, str], expected_filename: str) -> bool:
    actual = str(item.get("filename") or "").strip()
    if actual == expected_filename:
        return True
    if re.search(r"(?:^|_)" + re.escape(expected_filename) + r"$", actual, flags=re.I):
        return True
    expected_signature = _astri_rfs_filename_signature(expected_filename)
    if not expected_signature:
        return False
    return _astri_rfs_filename_signature(actual).endswith(expected_signature)


def _astri_rfs_slot(parsed: dict, identity: dict[str, str]) -> dict:
    spec = _astri_rfs_document_spec(identity)
    slot = next((item for item in parsed.get("slots") or [] if item.get("type_name") == spec["type_name"]), None)
    if not slot:
        raise ValueError(f"{spec['label']} tidak tersedia di Document RFS.")
    return slot


ASTRI_ABD_TOPOLOGY_NETWORK: dict[str, str] = {
    "AERIAL": "AE",
    "AE": "AE",
    "UNDERGROUND": "UG",
    "UG": "UG",
}
ASTRI_ABD_SCOPE_TYPES: dict[str, str] = {
    "cluster": "Cluster",
    "subfeeder": "Subfeeder",
    "feeder": "Feeder",
}


def _astri_abd_metadata_values(record: dict, identity: dict[str, str]) -> dict[str, str]:
    """Read one fresh ABD metadata snapshot for topology and canonical naming."""
    try:
        state = _fetch_astri_document_abd_metadata(record, identity)
    except Exception as exc:
        LOGGER.warning(
            "ASTRI ABD metadata lookup failed: scope=%s target=%s err=%s",
            identity.get("scope"), identity.get(str(_astri_rfs_document_spec(identity)["identity_key"]), "?"), exc,
        )
        return {}
    return {
        str(field.get("key") or ""): str(field.get("value") or "").strip()
        for field in (state.get("editor") or {}).get("fields", [])
        if isinstance(field, dict)
    }


def _astri_abd_resolve_network_type(
    topology_raw: str,
    requested: Any,
) -> tuple[str | None, str, bool]:
    """Map ASTRI topology to the EVA network type. Returns (network_type, topology_label, user_confirmed)."""
    topology_label = re.sub(r"\s+", " ", str(topology_raw or "").strip()).upper()
    mapped = ASTRI_ABD_TOPOLOGY_NETWORK.get(topology_label)
    if mapped:
        return mapped, topology_label, False
    requested_value = str(requested or "").strip().upper()
    requested_mapped = ASTRI_ABD_TOPOLOGY_NETWORK.get(requested_value)
    if requested_mapped:
        return requested_mapped, topology_label, True
    return None, topology_label, True


def _astri_abd_converted_job_payload(job_id: str, identity: dict[str, str]) -> tuple[str, bytes]:
    """Return (filename, bytes) of a finished ABD convert job bound to this exact ASTRI target."""
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
        raise ValueError("Job hasil convert ABD tidak valid.")
    job = get_job(job_id)
    if not job:
        raise ValueError("Job hasil convert ABD tidak ditemukan. Jalankan convert ulang.")
    context = job.get("astri_abd_convert")
    if not isinstance(context, dict) or not context:
        raise ValueError("Job ini bukan hasil convert ABD ASTRI.")
    if str(job.get("status") or "") != "done":
        raise ValueError("Hasil convert ABD belum siap diupload.")
    spec = _astri_rfs_document_spec(identity)
    identity_key = str(spec["identity_key"])
    if (
        str(context.get("scope") or "") != str(identity["scope"])
        or str(context.get(identity_key) or "") != str(identity[identity_key])
        or str(context.get("work_order_number") or "") != str(identity["work_order_number"])
        or str(context.get("vendor_label") or "").casefold() != str(identity["vendor_label"]).casefold()
    ):
        raise ValueError("Hasil convert ABD tidak cocok dengan target ASTRI yang dipilih.")
    output_path = Path(str(job.get("output_path") or ""))
    if not is_safe_output_path(output_path) or not output_path.exists():
        raise ValueError("File hasil convert ABD sudah tidak tersedia. Jalankan convert ulang.")
    filename = str(job.get("download_name") or output_path.name)
    return filename, output_path.read_bytes()


ASTRI_DOCUMENT_ABD_UPDATE_LOCK_PATH = Path(
    os.environ.get("ASTRI_DOCUMENT_ABD_UPDATE_LOCK_PATH", "/app/data/document_abd_metadata.lock")
)


@contextmanager
def _astri_document_abd_update_lock(timeout_seconds: float = 15.0):
    ASTRI_DOCUMENT_ABD_UPDATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ASTRI_DOCUMENT_ABD_UPDATE_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Update metadata Document ABD lain sedang berjalan. Coba lagi.") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fetch_astri_rfs_selected_match(
    record: dict,
    identity: dict[str, str],
) -> tuple[dict[str, str], str, str]:
    scope = str(identity["scope"])
    spec = _astri_rfs_document_spec(identity)
    identity_key = str(spec["identity_key"])
    query = str(identity[identity_key])
    response = record["session"].get(
        f"{ASTRI_BASE_URL}{spec['search_path']}",
        params={"filter[global]": query, "limit": "-1"},
        timeout=180,
    )
    response.raise_for_status()
    rows = parse_rfs_search_rows(response.text, query, scope=scope)
    matches = [
        row for row in rows
        if str(row.get(identity_key) or "") == query
        and str(row.get("work_order_number") or "") == identity["work_order_number"]
        and str(row.get("vendor_label") or "").strip().casefold() == identity["vendor_label"].casefold()
    ]
    if len(matches) != 1:
        raise ValueError("Identitas Document ABD tidak ditemukan atau ambigu pada RFS ASTRI terbaru.")
    return matches[0], response.text, response.url


def _fetch_astri_feeder_route(
    record: dict,
    identity: dict[str, str],
    selected: dict[str, str],
    route_uuid: str,
) -> dict[str, Any]:
    area = str(selected.get("area") or "").strip()
    params = {"length": -1}
    if area:
        params["filter[area]"] = area
    else:
        params["filter[global]"] = identity["osp_route"]
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/route/list",
        params=params,
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Daftar route Feeder ASTRI tidak valid.")
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("name") or "") == identity["osp_route"]
        and str(row.get("uuid") or "") == route_uuid
    ]
    if len(matches) != 1:
        raise ValueError("Route Feeder ASTRI tidak ditemukan atau ambigu.")
    return matches[0]


def _label_cluster_document_abd_code_options(record: dict, form: dict[str, Any]) -> None:
    controls = {str(name): str(value) for name, value in form.get("controls", [])}
    area = controls.get("cluster[area]", "").strip()
    if not area:
        raise ValueError("Area Cluster ASTRI tidak tersedia untuk resolver OLT/Feeder Code.")
    specs = (
        ("/olt/list", "cluster[olt_name]", ("device_code", "code"), "OLT"),
        ("/route/list", "cluster[osp_route_name]", ("feeder_code", "code"), "Feeder"),
    )
    for path, source_name, code_keys, label_name in specs:
        response = record["session"].get(
            f"{ASTRI_BASE_URL}{path}",
            params={"filter[area]": area, "length": -1},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"Daftar {label_name} ASTRI tidak valid.")
        labels: dict[str, tuple[str, str]] = {}
        ambiguous: set[str] = set()
        display_owners: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            native = str(row.get("name") or "").strip()
            code = next((str(row.get(key) or "").strip() for key in code_keys if str(row.get(key) or "").strip()), "")
            if not native or not code:
                continue
            display = f"{code} — {str(row.get('label') or native).strip()}"
            previous = labels.get(native)
            if previous and previous != (code, display):
                ambiguous.add(native)
                continue
            owner = display_owners.get(display)
            if owner and owner != native:
                ambiguous.update((owner, native))
                continue
            labels[native] = (code, display)
            display_owners[display] = native
        current = controls.get(source_name, "").strip()
        # Robust resolution: accept the stored value as native name, label, or
        # code, case-insensitively. Older clusters may store a display label
        # while the OLT/route list exposes native names, and renamed or retired
        # assets leave the stored value unresolvable — neither case should
        # block the editor from loading.
        resolved: str | None = None
        if current and current not in ambiguous:
            if current in labels:
                resolved = current
            else:
                folded = current.casefold()
                for candidate in labels:
                    if candidate.casefold() == folded:
                        resolved = candidate
                        break
                if resolved is None:
                    for candidate, (code, display) in labels.items():
                        label_text = display.split("\u2014", 1)[-1].strip().casefold()
                        if code.casefold() == folded or label_text == folded:
                            resolved = candidate
                            break
        if resolved is None:
            LOGGER.warning(
                "ASTRI %s Code tidak bisa di-resolve (cluster=%s area=%r current=%r, %d label, %d ambigu) — editor tetap dibuka.",
                label_name, controls.get("cluster[code]", ""), area, current,
                len(labels), len(ambiguous),
            )
            continue
        form.setdefault("options", {})[source_name] = [
            {**option, "label": labels[value][1]}
            for option in form.get("options", {}).get(source_name, [])
            if (value := str(option.get("value") or "")) in labels and value not in ambiguous
        ]


def _fetch_astri_document_abd_metadata(record: dict, identity: dict[str, str]) -> dict[str, Any]:
    scope = str(identity["scope"])
    selected, listing_html, listing_url = _fetch_astri_rfs_selected_match(record, identity)
    if scope == "cluster":
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/distribution/cluster/detail",
            params={
                "cluster_code": identity["cluster_code"],
                "work_order_number": identity["work_order_number"],
            },
            timeout=180,
        )
        response.raise_for_status()
        page_html = response.text
        referer = response.url
        seed_values = None
        display_values = None
    elif scope == "subfeeder":
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/subfeeder/detail",
            params={"subfeeder_code": identity["cluster_code"]},
            timeout=180,
        )
        response.raise_for_status()
        page_html = response.text
        referer = response.url
        seed_values = None
        display_values = None
    else:
        _document_html, parsed = _fetch_astri_rfs_document_page(record, identity)
        defaults = parsed.get("upload_defaults") or {}
        route_uuid = str(defaults.get("rfs_document[osp_route_uuid]") or "").strip()
        if (
            str(defaults.get("rfs_document[osp_route_name]") or "") != identity["osp_route"]
            or str(defaults.get("rfs_document[work_order_number]") or "") != identity["work_order_number"]
            or str(defaults.get("rfs_document[vendor_label]") or "").casefold() != identity["vendor_label"].casefold()
            or not route_uuid
        ):
            raise ValueError("Identitas form Feeder ASTRI tidak cocok dengan hasil RFS terpilih.")
        route = _fetch_astri_feeder_route(record, identity, selected, route_uuid)
        route_status = str(route.get("document_completeness_phase3_status") or selected.get("document_status") or "").strip()
        seed_values = {
            "work_order[number]": identity["work_order_number"],
            "work_order[latest_status_name]": str(selected.get("status_name") or "").strip(),
            "work_order[assigned_vendor_label]": identity["vendor_label"],
            "osp_route[uuid]": route_uuid,
            "osp_route[feeder_code]": str(route.get("feeder_code") or "").strip(),
            "osp_route[name]": str(route.get("name") or "").strip(),
            "osp_route[label]": str(route.get("label") or "").strip(),
            "osp_route[document_completeness_phase3_status]": route_status,
        }
        display_values = {
            "route[area]": str(route.get("area") or selected.get("area") or "").strip(),
            "route[olt_name]": str(route.get("olt_label") or route.get("olt_name") or selected.get("olt") or "").strip(),
            "route[topology]": str(route.get("topology") or "").strip(),
            "route[drm_net_type]": str(route.get("drm_net_type") or "").strip(),
            "route[rfs_date]": str(route.get("rfs_date") or selected.get("rfs_date") or "").strip(),
            "route[purchase_order_number]": str(route.get("purchase_order_number") or selected.get("purchase_order_number") or "").strip(),
        }
        page_html = listing_html
        referer = listing_url
    form = parse_document_abd_edit_form(
        page_html,
        scope=scope,
        identity=identity,
        seed_values=seed_values,
    )
    if scope == "cluster":
        _label_cluster_document_abd_code_options(record, form)
    if display_values:
        form["display_values"] = display_values
    editor = build_document_abd_editor(form, scope=scope, identity=identity)
    return {"form": form, "editor": editor, "selected": selected, "referer": referer}


def _document_abd_requested_values(
    form: dict[str, Any],
    editor: dict[str, Any],
    requested: dict[str, Any],
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    payload = apply_document_abd_editor_values(form, scope=editor["scope"], values=requested)
    payload_map: dict[str, str] = {}
    for name, value in payload:
        payload_map[str(name)] = str(value)
    fields = {str(field["key"]): field for field in editor.get("fields") or []}
    normalized = {
        str(key): payload_map[str(fields[str(key)]["source_name"])]
        for key in requested
    }
    return payload, normalized


def _read_back_astri_document_abd_metadata(
    record: dict,
    identity: dict[str, str],
    requested: dict[str, str],
    attempts: int = 3,
) -> tuple[dict[str, Any] | None, bool]:
    latest = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(1)
        latest = _fetch_astri_document_abd_metadata(record, identity)
        if document_abd_editor_matches(latest["editor"], requested):
            return latest, True
    return latest, False


def _require_document_abd_json_mutation() -> dict[str, Any]:
    if (
        not request.is_json
        or request.headers.get("X-Requested-With") != "XMLHttpRequest"
        or (request.content_length or 0) > 16384
    ):
        raise ValueError("Request update metadata Document ABD tidak valid.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Payload update metadata Document ABD tidak valid.")
    allowed = {
        "scope",
        "cluster_code",
        "osp_route",
        "work_order_number",
        "vendor_label",
        "version",
        "values",
        "confirm_metadata_update",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"Parameter update metadata tidak diizinkan: {unknown[0]}.")
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
        raise ValueError("Request lintas situs ditolak.")
    if payload.get("confirm_metadata_update") is not True:
        raise ValueError("Konfirmasi update metadata ASTRI wajib diberikan.")
    return payload


@app.route("/api/astri/rfs-documents/search")
def api_astri_rfs_documents_search():
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"items": []})
        if len(query) > 120:
            raise ValueError("Kata kunci maksimal 120 karakter.")
        scope, spec = document_abd_scope(request.args.get("scope"))
        record = require_astri_session()
        response = record["session"].get(
            f"{ASTRI_BASE_URL}{spec['search_path']}",
            params={"filter[global]": query, "limit": "-1"},
            timeout=120,
        )
        response.raise_for_status()
        return jsonify({"scope": scope, "items": parse_rfs_search_rows(response.text, query, scope=scope)[:100]})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        LOGGER.exception("ASTRI Document RFS search failed")
        return jsonify({"error": "Pencarian Document RFS ASTRI gagal."}), 502


@app.route("/api/astri/rfs-documents/detail")
def api_astri_rfs_documents_detail():
    try:
        identity = _astri_rfs_document_identity(request.args)
        record = require_astri_session()
        _page_html, parsed = _fetch_astri_rfs_document_page(record, identity)
        defaults = parsed.get("upload_defaults") or {}
        operator = str(record.get("username") or "").strip()
        spec = _astri_rfs_document_spec(identity)
        slots = parsed.get("slots") or []
        form_usable = bool(operator) and _astri_rfs_upload_form_is_usable(defaults, identity)
        slots = [
            slot if form_usable and bool(slot.get("upload_enabled")) and str(slot.get("type_uuid") or "").strip()
            else {**slot, "upload_enabled": False}
            for slot in slots
        ]
        display_name = defaults.get(str(spec["form_name_key"]), "")
        return jsonify({
            "match": {
                **identity,
                "operator": operator,
                "cluster_name": display_name,
                "display_name": display_name,
                "display_code": identity.get(str(spec["identity_key"]), ""),
                "vendor_name": defaults.get("rfs_document[vendor_name]", ""),
                "area": defaults.get("rfs_document[cluster_area]", ""),
                "project_name": defaults.get("rfs_document[project_name]", ""),
            },
            "slots": slots,
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        LOGGER.exception("ASTRI Document RFS detail failed")
        return jsonify({"error": "Detail Document RFS ASTRI gagal dimuat."}), 502


@app.route("/api/astri/rfs-documents/metadata", methods=["GET", "POST"])
def api_astri_rfs_documents_metadata():
    if request.method in {"GET", "HEAD"}:
        try:
            identity = _astri_rfs_document_identity(request.args)
            record = require_astri_session()
            state = _fetch_astri_document_abd_metadata(record, identity)
            return jsonify({
                "ok": True,
                "metadata": public_document_abd_editor(state["editor"]),
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except requests.RequestException:
            LOGGER.exception("ASTRI Document ABD metadata GET failed")
            return jsonify({"error": "Metadata Document ABD ASTRI gagal dimuat."}), 502
        except (TypeError, KeyError, json.JSONDecodeError):
            LOGGER.exception("ASTRI Document ABD metadata response invalid")
            return jsonify({"error": "Format metadata Document ABD ASTRI tidak valid."}), 502

    if request.method != "POST":
        return jsonify({"error": "Metode perubahan metadata tidak diizinkan."}), 405
    try:
        request_payload = _require_document_abd_json_mutation()
        identity = _astri_rfs_document_identity(request_payload)
        requested = request_payload.get("values")
        version = str(request_payload.get("version") or "").strip().lower()
        if not isinstance(requested, dict) or not re.fullmatch(r"[a-f0-9]{64}", version):
            raise ValueError("Versi atau nilai metadata Document ABD tidak valid.")
        record = require_astri_session()
        operator = str(record.get("username") or "").strip()
        if not operator:
            raise ValueError("Identitas operator ASTRI tidak tersedia.")
        with _astri_document_abd_update_lock():
            fresh = _fetch_astri_document_abd_metadata(record, identity)
            if version != fresh["editor"]["version"]:
                return jsonify({
                    "error": "Data ASTRI berubah sejak form dibuka. Data terbaru sudah dimuat; periksa lalu simpan ulang.",
                    "code": "ASTRI_DOCUMENT_ABD_METADATA_STALE",
                    "metadata": public_document_abd_editor(fresh["editor"]),
                }), 409
            post_data, normalized = _document_abd_requested_values(
                fresh["form"], fresh["editor"], requested
            )
            if document_abd_editor_matches(fresh["editor"], normalized):
                raise ValueError("Tidak ada perubahan metadata untuk disimpan.")

            post_response = None
            post_error = None
            try:
                post_response = record["session"].post(
                    f"{ASTRI_BASE_URL}{fresh['form']['action']}",
                    data=post_data,
                    allow_redirects=False,
                    timeout=180,
                    headers={"Referer": fresh["referer"]},
                )
            except requests.RequestException as exc:
                # POST outcome may be ambiguous. Never repost: reconcile with GET below.
                post_error = exc

            readback = None
            matched = False
            try:
                readback, matched = _read_back_astri_document_abd_metadata(
                    record, identity, normalized, attempts=3
                )
            except (requests.RequestException, ValueError, TypeError, KeyError):
                LOGGER.exception("ASTRI Document ABD metadata read-back failed")

            status_code = post_response.status_code if post_response is not None else None
            LOGGER.info(
                "ASTRI Document ABD metadata update: operator=%s scope=%s target=%s wo=%s keys=%s post_status=%s post_error=%s readback=%s",
                operator,
                identity["scope"],
                identity.get("cluster_code") or identity.get("osp_route"),
                identity["work_order_number"],
                sorted(normalized),
                status_code,
                type(post_error).__name__ if post_error else "",
                matched,
            )
            if matched and readback:
                return jsonify({
                    "ok": True,
                    "reconciled": bool(post_error),
                    "changed": normalized,
                    "metadata": public_document_abd_editor(readback["editor"]),
                })
            if _astri_mutation_success_redirect(post_response):
                return jsonify({
                    "ok": True,
                    "accepted": True,
                    "verified": False,
                    "post_status": status_code,
                    "changed": normalized,
                }), 202
            if post_error:
                return jsonify({
                    "error": "Hasil POST ASTRI ambigu dan GET read-back belum cocok. POST tidak diulang otomatis; refresh metadata sebelum mencoba lagi.",
                    "code": "ASTRI_DOCUMENT_ABD_METADATA_AMBIGUOUS",
                    "metadata": public_document_abd_editor(readback["editor"]) if readback else None,
                }), 502
            return jsonify({
                "error": "ASTRI belum menampilkan nilai tersimpan pada GET read-back. Update tidak dinyatakan sukses.",
                "code": "ASTRI_DOCUMENT_ABD_METADATA_READBACK_MISMATCH",
                "post_status": status_code,
                "metadata": public_document_abd_editor(readback["editor"]) if readback else None,
            }), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (TypeError, KeyError, json.JSONDecodeError):
        LOGGER.exception("ASTRI Document ABD metadata payload invalid")
        return jsonify({"error": "Payload metadata Document ABD tidak valid."}), 400
    except requests.RequestException:
        LOGGER.exception("ASTRI Document ABD metadata update preparation failed")
        return jsonify({"error": "Metadata Document ABD ASTRI gagal disiapkan."}), 502


@app.route("/api/astri/rfs-documents/download")
def api_astri_rfs_documents_download():
    upstream = None
    try:
        identity = _astri_rfs_document_identity(request.args)
        spec = _astri_rfs_document_spec(identity)
        type_name = request.args.get("type_name", "").strip()
        uuid_value = request.args.get("uuid", "").strip()
        if type_name != spec["type_name"] or not uuid_value:
            raise ValueError(f"Hanya {spec['label']} dan UUID dokumen yang wajib dipilih.")
        record = require_astri_session()
        _page_html, parsed = _fetch_astri_rfs_document_page(record, identity)
        slot = _astri_rfs_slot(parsed, identity)
        document = next(
            (item for item in slot.get("versions") or [] if item.get("uuid") == uuid_value),
            None,
        )
        if not document:
            raise ValueError("Versi dokumen tidak ditemukan di Document RFS yang dipilih.")
        upstream = record["session"].get(
            f"{ASTRI_BASE_URL}{spec['download_path']}",
            params={"uuid": uuid_value},
            stream=True,
            timeout=180,
        )
        upstream.raise_for_status()
        filename = secure_filename(document.get("filename") or "") or f"document.{document['extension']}"

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if upstream.headers.get("Content-Length"):
            headers["Content-Length"] = upstream.headers["Content-Length"]
        return Response(
            stream_with_context(generate()),
            content_type=upstream.headers.get("Content-Type") or document.get("mime") or "application/octet-stream",
            headers=headers,
        )
    except ValueError as exc:
        if upstream is not None:
            upstream.close()
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        if upstream is not None:
            upstream.close()
        LOGGER.exception("ASTRI Document RFS download failed")
        return jsonify({"error": "Download Document RFS ASTRI gagal."}), 502


@app.route("/api/astri/rfs-documents/upload", methods=["POST"])
def api_astri_rfs_documents_upload():
    try:
        identity = _astri_rfs_document_identity(request.form)
        spec = _astri_rfs_document_spec(identity)
        type_name = request.form.get("type_name", "").strip()
        uploaded = request.files.get("file")
        convert_job_id = str(request.form.get("convert_job_id") or "").strip()
        if type_name != spec["type_name"]:
            raise ValueError(f"Hanya {spec['label']} dan file .kmz yang dapat diupload.")
        if convert_job_id and uploaded and uploaded.filename:
            raise ValueError("Pilih salah satu: file manual atau hasil convert ABD.")
        if not convert_job_id and (not uploaded or not uploaded.filename):
            raise ValueError(f"Hanya {spec['label']} dan file .kmz yang dapat diupload.")
        if request.form.get("confirm_upload") != "1":
            raise ValueError(f"Konfirmasi upload {spec['label']} wajib diberikan.")

        record = require_astri_session()
        operator = str(record.get("username") or "").strip()
        if not operator:
            raise ValueError("Identitas operator ASTRI tidak tersedia.")
        _page_html, parsed = _fetch_astri_rfs_document_page(record, identity)
        slot = _astri_rfs_slot(parsed, identity)
        if not slot.get("upload_enabled"):
            raise ValueError("Upload jenis dokumen ini dinonaktifkan oleh ASTRI.")
        if not str(slot.get("type_uuid") or "").strip():
            raise ValueError(f"UUID slot {spec['label']} tidak tersedia.")

        defaults = _astri_rfs_upload_defaults(parsed, identity)
        if convert_job_id:
            converted_name, content = _astri_abd_converted_job_payload(convert_job_id, identity)
            extension = validate_kmz_upload(converted_name, content)
            filename = _astri_rfs_original_filename(converted_name)
        else:
            content = uploaded.stream.read(DOCUMENT_ABD_MAX_BYTES + 1)
            extension = validate_kmz_upload(uploaded.filename, content)
            filename = _astri_rfs_original_filename(uploaded.filename)
        before_versions = {item.get("uuid") for item in slot.get("versions") or []}
        allowed_fields = (
            "csrf_name",
            "csrf_value",
            "rfs_document[cluster_code]",
            "rfs_document[osp_route_name]",
            "rfs_document[osp_route_label]",
            "rfs_document[osp_route_uuid]",
            "rfs_document[work_order_number]",
            "rfs_document[phase]",
            "rfs_document[vendor_label]",
            "rfs_document[vendor_name]",
            "rfs_document[cluster_area]",
            "rfs_document[project_name]",
            "ref_page",
            "rfs_document[cluster_name]",
        )
        data = {name: str(defaults.get(name) or "") for name in allowed_fields}
        data["rfs_document[uuid]"] = str(slot.get("type_uuid") or "")
        if not data["rfs_document[uuid]"]:
            raise ValueError(f"UUID slot {spec['label']} tidak tersedia.")
        data["rfs_document[type_name]"] = type_name
        data["rfs_document[allowed_file_type]"] = slot.get("allowed_file_type") or extension
        response = record["session"].post(
            f"{ASTRI_BASE_URL}{spec['upload_path']}",
            data=data,
            files={"rfs_document[file]": (filename, content, DOCUMENT_ABD_MIME)},
            allow_redirects=False,
            timeout=180,
        )
        if response.status_code >= 400:
            LOGGER.warning(
                "ASTRI Document RFS upload rejected: operator=%s scope=%s target=%s wo=%s status=%s",
                operator, identity["scope"], identity.get(str(spec["identity_key"]), ""), identity["work_order_number"], response.status_code,
            )
            return jsonify({"error": f"Upload Document RFS ASTRI ditolak (HTTP {response.status_code})."}), 502

        new_document = None
        after_count = len(before_versions)
        for attempt in range(5):
            if attempt:
                time.sleep(1)
            _verify_html, verified = _fetch_astri_rfs_document_page(record, identity)
            verified_slot = _astri_rfs_slot(verified, identity)
            versions = verified_slot.get("versions") or []
            after_count = len(versions)
            new_document = next(
                (
                    item for item in versions
                    if item.get("uuid") not in before_versions
                    and _astri_rfs_verified_filename_matches(item, identity, filename)
                ),
                None,
            )
            if new_document:
                break
        LOGGER.info(
            "ASTRI Document RFS upload: operator=%s scope=%s target=%s wo=%s type=%s file=%s source=%s status=%s location=%s version_added=%s",
            operator, identity["scope"], identity.get(str(spec["identity_key"]), ""), identity["work_order_number"], type_name, filename,
            f"convert_job:{convert_job_id}" if convert_job_id else "manual_file",
            response.status_code, response.headers.get("Location", ""), bool(new_document),
        )
        if not new_document:
            return jsonify({
                "error": "ASTRI belum menampilkan versi dokumen baru. Upload tidak dinyatakan sukses.",
                "status_code": response.status_code,
                "before_count": len(before_versions),
                "after_count": after_count,
            }), 502
        return jsonify({
            "ok": True,
            "status_code": response.status_code,
            "document": new_document,
            "before_count": len(before_versions),
            "after_count": after_count,
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        LOGGER.exception("ASTRI Document RFS upload failed")
        return jsonify({"error": "Upload Document RFS ASTRI gagal."}), 502


@app.route("/api/astri/rfs-documents/convert", methods=["POST"])
def api_astri_rfs_documents_convert():
    """Download the newest ASTRI ABD KMZ, standardize it to EVA, and park the result as a job."""
    saved_paths: list[Path] = []
    job_id = uuid.uuid4().hex
    started_at = time.perf_counter()
    job_created = False
    try:
        payload = request.get_json(silent=True) or {}
        identity = _astri_rfs_document_identity(payload)
        spec = _astri_rfs_document_spec(identity)
        identity_key = str(spec["identity_key"])
        record = require_astri_session()
        operator = str(record.get("username") or "").strip()
        if not operator:
            raise ValueError("Identitas operator ASTRI tidak tersedia.")

        _page_html, parsed = _fetch_astri_rfs_document_page(record, identity)
        slot = _astri_rfs_slot(parsed, identity)
        versions = [item for item in slot.get("versions") or [] if str(item.get("uuid") or "").strip()]
        if not versions:
            raise ValueError(f"{spec['label']} belum punya versi KMZ untuk dikonversi.")
        source_document = versions[0]

        metadata = _astri_abd_metadata_values(record, identity)
        network_type, topology_label, needs_confirm = _astri_abd_resolve_network_type(
            metadata.get("topology", ""), payload.get("network_type")
        )
        if not network_type:
            return jsonify({
                "error": "Topologi ASTRI tidak tersedia untuk target ini. Pilih AE atau UG untuk melanjutkan convert.",
                "code": "ASTRI_ABD_TOPOLOGY_CONFIRM_REQUIRED",
                "topology": topology_label,
                "options": [
                    {"value": "AE", "label": "AE (Aerial)"},
                    {"value": "UG", "label": "UG (Underground)"},
                ],
                "source_document": source_document,
            }), 409

        upstream = record["session"].get(
            f"{ASTRI_BASE_URL}{spec['download_path']}",
            params={"uuid": source_document["uuid"]},
            timeout=180,
        )
        upstream.raise_for_status()
        source_bytes = upstream.content
        source_name = str(source_document.get("filename") or f"{identity[identity_key]}.kmz")
        validate_kmz_upload(source_name if source_name.lower().endswith(".kmz") else f"{source_name}.kmz", source_bytes)

        scope_type = ASTRI_ABD_SCOPE_TYPES.get(str(identity["scope"]), "Cluster")
        target_code = str(identity[identity_key])
        defaults = parsed.get("upload_defaults") or {}
        target_name = str(
            metadata.get("route_label" if identity["scope"] == "feeder" else "name")
            or defaults.get(str(spec["form_name_key"]))
            or payload.get("target_name") or ""
        ).strip()
        filename_code = metadata.get("feeder_code", "") if identity["scope"] == "feeder" else target_code
        if not filename_code:
            raise ValueError("Kode Feeder ASTRI belum tersedia untuk nama file standar EVA.")
        download_name = astri_apd_standard_filename(
            {"cluster_code": filename_code, "cluster_name": target_name},
            is_subfeeder=identity["scope"] == "subfeeder",
            is_feeder=identity["scope"] == "feeder",
        )
        input_path = Config.UPLOAD_DIR / f"{job_id}_astri_abd_{secure_filename(source_name) or 'source.kmz'}"
        output_path = Config.OUTPUT_DIR / f"{job_id}_ASTRI_ABD_STANDARDIZED.kmz"
        input_path.write_bytes(source_bytes)
        saved_paths.append(input_path)

        create_job(
            job_id,
            [input_path],
            output_path,
            [],
            [],
            True,
            download_name,
            output_mode="kmz",
            download_mimetype=DOCUMENT_ABD_MIME,
            network_type=network_type,
            scope_type=scope_type,
        )
        job_created = True
        convert_logger = create_job_logger(job_id)
        convert_logger.info(
            f"Convert ABD ASTRI: scope={identity['scope']} target={target_code} "
            f"wo={identity['work_order_number']} source={source_name} topology={topology_label or '(kosong)'} "
            f"network={network_type}."
        )
        update_job(
            job_id,
            status="processing",
            progress=25,
            message=f"Standardisasi {spec['label']} {target_code} ({network_type}).",
            started_at=time.time(),
            astri_abd_convert={
                "scope": str(identity["scope"]),
                identity_key: target_code,
                "work_order_number": str(identity["work_order_number"]),
                "vendor_label": str(identity["vendor_label"]),
                "type_name": str(spec["type_name"]),
                "network_type": network_type,
                "scope_type": scope_type,
                "topology": topology_label,
                "topology_confirmed_by_user": bool(needs_confirm),
                "source_uuid": str(source_document["uuid"]),
                "source_filename": source_name,
                "operator": operator,
            },
        )

        coding_records = None
        coding_path = None
        if fdt_coding_source_ready():
            coding_records = resolve_fdt_coding_records(convert_logger)
            if not coding_records:
                try:
                    coding_path = resolve_fdt_coding_workbook(job_id, convert_logger)
                    if coding_path and coding_path.parent == Config.UPLOAD_DIR:
                        saved_paths.append(coding_path)
                except Exception as exc:
                    convert_logger.warn(fdt_coding_runtime_warning(str(exc)))
        else:
            convert_logger.warn(fdt_coding_runtime_warning())

        result = standardize_kmz_file(
            input_path,
            coding_path,
            output_path,
            network_type=network_type,
            scope_type=scope_type,
            coding_records=coding_records,
            source_name=source_name,
        )
        result["source_name"] = source_name
        for warning in result.get("warnings", []):
            convert_logger.warn(warning)
        duration = time.perf_counter() - started_at
        message = (
            f"ABD standar siap: {int(result.get('fdt_renamed', 0))} FDT code, "
            f"{int(result.get('name_standardized', 0))} nama objek, "
            f"{int(result.get('placemark_organized', 0))} objek dipindah, "
            f"{int(result.get('style_standardized', 0))} style."
        )
        convert_stats = {
            "KML": int(result.get("fdt_placemarks", 0)),
            "FDT": int(result.get("fdt_renamed", 0)),
            "NAMES": int(result.get("name_standardized", 0)),
            "ORGANIZED": int(result.get("placemark_organized", 0)),
            "STYLES": int(result.get("style_standardized", 0)),
            "BOQ_WARNINGS": len(result.get("warnings", [])),
        }
        update_job(
            job_id,
            status="done",
            progress=100,
            message=message,
            finished_at=time.time(),
            duration_seconds=duration,
            stats=convert_stats,
            standardizer_summary=result,
        )
        LOGGER.info(
            "ASTRI ABD convert: operator=%s scope=%s target=%s wo=%s source=%s network=%s topology=%s job=%s size=%s",
            operator, identity["scope"], target_code, identity["work_order_number"],
            source_name, network_type, topology_label or "(empty)", job_id, output_path.stat().st_size,
        )
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "download_name": download_name,
            "download_url": f"/download/{job_id}",
            "download_mimetype": DOCUMENT_ABD_MIME,
            "stats": convert_stats,
            "network_type": network_type,
            "topology": topology_label,
            "topology_confirmed_by_user": bool(needs_confirm),
            "scope_type": scope_type,
            "source_document": source_document,
            "output_bytes": output_path.stat().st_size,
            "message": message,
            "standardizer_summary": result,
        })
    except ValueError as exc:
        cleanup_saved_uploads(saved_paths)
        if job_created:
            update_job(job_id, status="error", progress=100, message="Convert ABD gagal.", error=str(exc), finished_at=time.time())
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException:
        cleanup_saved_uploads(saved_paths)
        if job_created:
            update_job(job_id, status="error", progress=100, message="Convert ABD gagal.", error="ASTRI request gagal.", finished_at=time.time())
        LOGGER.exception("ASTRI ABD convert download failed")
        return jsonify({"error": "Download ABD dari ASTRI gagal."}), 502
    except Exception as exc:
        cleanup_saved_uploads(saved_paths)
        if job_created:
            update_job(job_id, status="error", progress=100, message="Convert ABD gagal.", error=str(exc), finished_at=time.time())
        LOGGER.exception("ASTRI ABD convert failed")
        return jsonify({"error": f"Convert ABD gagal: {exc}"}), 500


@app.route("/api/astri/work-order/status")
def api_astri_work_order_status():
    try:
        ensure_astri_boq_owner_id()
        record = require_astri_session()
        scope = (request.args.get("scope") or "cluster").strip().lower()
        match = {
            "cluster_code": request.args.get("cluster_code", "").strip(),
            "subfeeder_code": request.args.get("subfeeder_code", "").strip(),
            "feeder_code": request.args.get("feeder_code", "").strip(),
            "target_osp_route_code": request.args.get("target_osp_route_code", "").strip(),
            "work_order_number": request.args.get("work_order_number", "").strip(),
        }
        return jsonify(fetch_astri_work_order_status(record, match, scope))
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "ASTRI_WO_STATUS_INPUT_INVALID"}), 400
    except Exception as exc:
        LOGGER.warning("ASTRI WO status endpoint unavailable: %s", exc)
        return jsonify({
            "error": "Status WO ASTRI tidak dapat diverifikasi. Coba refresh atau login ulang.",
            "code": "ASTRI_WO_STATUS_UNAVAILABLE",
        }), 503


@app.route("/api/astri/upload/<job_id>", methods=["POST"])
def api_astri_upload(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job belum selesai."}), 400
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
        docs = fetch_astri_requirement_documents(record, match)
        selected = ["kmz", "boq"]
        for key in selected:
            type_name = ASTRI_UPLOAD_TARGETS[key]["type_name"]
            doc = docs.get(type_name)
            if doc and doc.get("verified"):
                return jsonify({"error": f"Dokumen {type_name} sudah terverifikasi. Tidak bisa upload ulang.", "match": match}), 400
        files = astri_job_upload_files(job)
        results = []
        for key in selected:
            if key not in files:
                results.append({"target": key, "ok": False, "error": "File output tidak tersedia."})
                continue
            path, filename = files[key]
            results.append(upload_file_to_astri(record, match, key, path, filename))
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI upload failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/apd/convert", methods=["POST"])
def api_astri_apd_convert():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match") or {}
        is_subfeeder = bool(payload.get("subfeeder"))
        is_feeder = bool(payload.get("feeder"))
        network_type = normalize_network_type(match.get("net_type") or payload.get("network_type") or NETWORK_TYPE_AE)
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "Match ASTRI belum lengkap."}), 400
        docs = fetch_astri_latest_documents(record, match, is_subfeeder=is_subfeeder, is_feeder=is_feeder)
        doc = select_astri_apd_kmz_document(docs, is_subfeeder=is_subfeeder, is_feeder=is_feeder)
        if not doc:
            return jsonify({"error": "Dokumen KMZ APD ASTRI tidak ditemukan.", "documents": docs}), 404
        response = record["session"].get(f"{ASTRI_BASE_URL}/download/rfs-document", params={"uuid": doc["uuid"]}, timeout=180)
        response.raise_for_status()
        content = response.content or b""
        if not content:
            return jsonify({"error": "Download KMZ APD kosong."}), 400
        standard_name = astri_apd_standard_filename(match, is_subfeeder=is_subfeeder, is_feeder=is_feeder)
        scope_type = "Feeder" if is_feeder else "Subfeeder" if is_subfeeder else "Cluster"
        created = create_conversion_job_from_apd_kmz(match, content, standard_name, scope_type, network_type)
        return jsonify({"ok": True, "match": match, "document": doc, **created})
    except Exception as exc:
        LOGGER.exception("ASTRI APD convert failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/latest-documents/download", methods=["POST"])
def api_astri_latest_documents_download():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match") or {}
        is_subfeeder = bool(payload.get("subfeeder"))
        is_feeder = bool(payload.get("feeder"))
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "Match ASTRI belum lengkap."}), 400
        docs = fetch_astri_latest_documents(record, match, is_subfeeder=is_subfeeder, is_feeder=is_feeder)
        if not docs:
            return jsonify({"error": "Tidak ada file ASTRI yang bisa didownload."}), 404
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="astri_dl_")
        zip_path = os.path.join(tmpdir, "bundle.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for doc in docs:
                    response = record["session"].get(
                        f"{ASTRI_BASE_URL}/download/rfs-document",
                        params={"uuid": doc["uuid"]},
                        timeout=120,
                        stream=True,
                    )
                    response.raise_for_status()
                    filename = secure_filename(doc.get("filename") or f"{doc.get('type_name', 'document')}.bin") or f"{doc.get('type_name', 'document')}.bin"
                    dl_path = os.path.join(tmpdir, filename)
                    with open(dl_path, "wb") as dlf:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                dlf.write(chunk)
                    archive.write(dl_path, filename)
                    os.remove(dl_path)
            return send_file(
                zip_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=astri_bundle_download_name(match, is_subfeeder=is_subfeeder),
                max_age=0,
            )
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as exc:
        LOGGER.exception("ASTRI latest documents download failed")
        return jsonify({"error": str(exc)}), 400


def upload_file_to_astri_feeder(record: dict, match: dict, target_key: str, path: Path, filename: str) -> dict:
    spec = FEEDER_UPLOAD_TARGETS[target_key]
    data = {
        "csrf_name": "",
        "csrf_value": "",
        "rfs_document[cluster_code]": match["cluster_code"],
        "rfs_document[work_order_number]": match["work_order_number"],
        "rfs_document[phase]": "feeder",
        "rfs_document[vendor_label]": match["vendor_label"],
        "rfs_document[cluster_name]": match["cluster_name"],
        "rfs_document[type_name]": spec["type_name"],
        "rfs_document[allowed_file_type]": spec["allowed_file_type"],
    }
    with path.open("rb") as fh:
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/feeder/apd/upload/v2",
            data=data,
            files={"rfs_document[file]": (filename, fh)},
            allow_redirects=False,
            timeout=120,
        )
    LOGGER.info(
        "ASTRI feeder upload: target=%s file=%s status=%s location=%s body=%s",
        target_key, filename, response.status_code,
        response.headers.get("Location", "(none)"),
        response.text[:500],
    )
    verified = False
    try:
        req_docs = fetch_astri_feeder_requirement_documents(record, match)
        doc = req_docs.get(spec["type_name"])
        verified = bool(doc)
        LOGGER.info(
            "ASTRI feeder upload verify: type=%s found=%s docs=%s",
            spec["type_name"], verified, list(req_docs.keys()),
        )
    except Exception:
        LOGGER.exception("ASTRI feeder upload verify failed")
    ok = response.status_code in {200, 201, 302, 303} and verified
    return {"target": target_key, "filename": filename, "status_code": response.status_code, "ok": ok, "verified": verified}


def upload_file_to_astri_subfeeder(record: dict, match: dict, target_key: str, path: Path, filename: str) -> dict:
    spec = SUBFEEDER_UPLOAD_TARGETS[target_key]
    data = {
        "csrf_name": "",
        "csrf_value": "",
        "rfs_document[cluster_code]": match["cluster_code"],
        "rfs_document[work_order_number]": match["work_order_number"],
        "rfs_document[phase]": "subfeeder",
        "rfs_document[vendor_label]": match["vendor_label"],
        "rfs_document[cluster_name]": match["cluster_name"],
        "rfs_document[type_name]": spec["type_name"],
        "rfs_document[allowed_file_type]": spec["allowed_file_type"],
    }
    with path.open("rb") as fh:
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/apd/upload/v2",
            data=data,
            files={"rfs_document[file]": (filename, fh)},
            allow_redirects=False,
            timeout=120,
        )
    LOGGER.info(
        "ASTRI subfeeder upload: target=%s file=%s status=%s location=%s body=%s",
        target_key, filename, response.status_code,
        response.headers.get("Location", "(none)"),
        response.text[:500],
    )
    verified = False
    try:
        req_docs = fetch_astri_subfeeder_requirement_documents(record, match)
        doc = req_docs.get(spec["type_name"])
        verified = bool(doc)
        LOGGER.info(
            "ASTRI subfeeder upload verify: type=%s found=%s docs=%s",
            spec["type_name"], verified, list(req_docs.keys()),
        )
    except Exception:
        LOGGER.exception("ASTRI subfeeder upload verify failed")
    ok = response.status_code in {200, 201, 302, 303} and verified
    return {"target": target_key, "filename": filename, "status_code": response.status_code, "ok": ok, "verified": verified}


@app.route("/api/astri/feeder/upload/<job_id>", methods=["POST"])
def api_astri_feeder_upload(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job belum selesai."}), 400
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        query = payload.get("cluster_name") or cluster_name_for_job(job)
        code = query.split("-")[0].strip() if query else ""
        if not code:
            return jsonify({"error": "Kode feeder tidak bisa diekstrak.", "match": {"matched": False, "query": query}}), 400
        items = search_astri_feeders(record, code)
        match = items[0] if items else {"matched": False, "query": query}
        if not match.get("matched"):
            return jsonify({"error": "Feeder ASTRI tidak ditemukan.", "match": match}), 404
        docs = fetch_astri_feeder_requirement_documents(record, match)
        selected = ["kmz", "boq"]
        for key in selected:
            type_name = FEEDER_UPLOAD_TARGETS[key]["type_name"]
            doc = docs.get(type_name)
            if doc and doc.get("verified"):
                return jsonify({"error": f"Dokumen {type_name} sudah terverifikasi. Tidak bisa upload ulang.", "match": match}), 400
        files = astri_job_upload_files(job)
        results = []
        for key in selected:
            if key not in files:
                results.append({"target": key, "ok": False, "error": "File output tidak tersedia."})
                continue
            path, filename = files[key]
            results.append(upload_file_to_astri_feeder(record, match, key, path, filename))
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder upload failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/subfeeder/upload/<job_id>", methods=["POST"])
def api_astri_subfeeder_upload(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job belum selesai."}), 400
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        if not code:
            return jsonify({"error": "Kode subfeeder tidak bisa diekstrak.", "match": {"matched": False, "query": cluster_name}}), 400
        items = search_astri_subfeeders(record, code)
        match = items[0] if items else {"matched": False, "query": cluster_name}
        if not match.get("matched"):
            return jsonify({"error": "Subfeeder ASTRI tidak ditemukan.", "match": match}), 404
        docs = fetch_astri_subfeeder_requirement_documents(record, match)
        selected = ["kmz", "boq"]
        for key in selected:
            type_name = SUBFEEDER_UPLOAD_TARGETS[key]["type_name"]
            doc = docs.get(type_name)
            if doc and doc.get("verified"):
                return jsonify({"error": f"Dokumen {type_name} sudah terverifikasi. Tidak bisa upload ulang.", "match": match}), 400
        files = astri_job_upload_files(job)
        results = []
        for key in selected:
            if key not in files:
                results.append({"target": key, "ok": False, "error": "File output tidak tersedia."})
                continue
            path, filename = files[key]
            results.append(upload_file_to_astri_subfeeder(record, match, key, path, filename))
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder upload failed")
        return jsonify({"error": str(exc)}), 400


def _fetch_astri_scope_verification_documents(record: dict, match: dict, scope: str) -> dict[str, dict]:
    """Read every verification control for one exact ASTRI scope/WO."""
    normalized_scope = str(scope or "cluster").strip().lower()
    if normalized_scope == "subfeeder":
        return fetch_astri_subfeeder_requirement_documents(record, match)
    if normalized_scope == "feeder":
        return fetch_astri_feeder_requirement_documents(record, match)
    return fetch_astri_requirement_documents(record, match)


def _summarize_astri_document_verification(docs: dict[str, dict]) -> dict:
    documents = []
    for type_name, doc in (docs or {}).items():
        documents.append({
            **doc,
            "type_name": type_name,
            "actionable": not is_astri_boundary_document(type_name),
        })
    actionable = [doc for doc in documents if doc["actionable"]]
    unverified_count = sum(1 for doc in actionable if not doc.get("verified"))
    return {
        "documents": documents,
        "document_count": len(documents),
        "actionable_document_count": len(actionable),
        "verified_count": len(actionable) - unverified_count,
        "unverified_count": unverified_count,
        "has_unverified": unverified_count > 0,
        "all_verified": bool(actionable) and unverified_count == 0,
        "available": bool(actionable),
    }


@app.route("/api/astri/document-verification-status")
def api_astri_document_verification_status():
    try:
        record = require_astri_session()
        scope = (request.args.get("scope") or "cluster").strip().lower()
        if scope not in {"cluster", "subfeeder", "feeder"}:
            return jsonify({"error": "Scope ASTRI tidak valid."}), 400
        code_key = {
            "cluster": "cluster_code",
            "subfeeder": "subfeeder_code",
            "feeder": "feeder_code",
        }[scope]
        code = (request.args.get(code_key) or request.args.get("cluster_code") or "").strip()
        work_order_number = (request.args.get("work_order_number") or "").strip()
        if not code or not work_order_number:
            return jsonify({"error": "Kode scope dan work_order_number diperlukan."}), 400
        match = {
            "matched": True,
            "scope": scope,
            "target_type": scope,
            "cluster_code": code,
            code_key: code,
            "work_order_number": work_order_number,
            "vendor_label": (request.args.get("vendor_label") or "").strip(),
        }
        docs = _fetch_astri_scope_verification_documents(record, match, scope)
        return jsonify({"match": match, **_summarize_astri_document_verification(docs)})
    except Exception as exc:
        LOGGER.exception("ASTRI document verification status failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/verify", methods=["POST"])
def api_astri_cluster_verify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        targets = payload.get("targets")
        if targets == "all" or (isinstance(targets, list) and "all" in targets):
            docs = fetch_unverified_astri_documents(record, match)
            results = []
            for type_name, doc in docs.items():
                if is_astri_boundary_document(type_name):
                    results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-verify."})
                    continue
                result = verify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = type_name
                results.append(result)
        else:
            selected = [k for k in (targets or ["kmz", "boq", "hpdb"]) if k in ASTRI_UPLOAD_TARGETS]
            all_docs = fetch_astri_requirement_documents(record, match)
            results = []
            for key in selected:
                type_name = ASTRI_UPLOAD_TARGETS[key]["type_name"]
                doc = all_docs.get(type_name)
                if not doc:
                    results.append({"target": key, "type_name": type_name, "ok": False, "error": "UUID dokumen tidak ditemukan di ASTRI."})
                    continue
                if doc.get("verified"):
                    results.append({"target": key, "type_name": type_name, "ok": True, "note": "Sudah terverifikasi sebelumnya."})
                    continue
                result = verify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = key
                results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI cluster verify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/verify", methods=["POST"])
def api_astri_feeder_verify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        docs = fetch_astri_feeder_requirement_documents(record, match)
        results = []
        for type_name, doc in docs.items():
            if is_astri_boundary_document(type_name):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-verify."})
                continue
            if doc.get("verified"):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "note": "Sudah terverifikasi sebelumnya."})
                continue
            result = set_astri_feeder_document_verification(record, match, type_name, doc["uuid"], True)
            result["target"] = type_name
            results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder verify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/unverify", methods=["POST"])
def api_astri_feeder_unverify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        docs = fetch_astri_feeder_requirement_documents(record, match)
        results = []
        for type_name, doc in docs.items():
            if is_astri_boundary_document(type_name):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-unverify."})
                continue
            if not doc.get("verified"):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "note": "Sudah unverified sebelumnya."})
                continue
            result = set_astri_feeder_document_verification(record, match, type_name, doc["uuid"], False)
            result["target"] = type_name
            results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder unverify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/unverify", methods=["POST"])
def api_astri_cluster_unverify():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match", {})
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "match.cluster_code dan match.work_order_number diperlukan."}), 400
        targets = payload.get("targets")
        if targets == "all" or (isinstance(targets, list) and "all" in targets):
            docs = fetch_verified_astri_documents(record, match)
            results = []
            for type_name, doc in docs.items():
                if is_astri_boundary_document(type_name):
                    results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-unverify."})
                    continue
                result = unverify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = type_name
                results.append(result)
        else:
            selected = [k for k in (targets or ["kmz", "boq", "hpdb"]) if k in ASTRI_UPLOAD_TARGETS]
            all_docs = fetch_astri_requirement_documents(record, match)
            results = []
            for key in selected:
                type_name = ASTRI_UPLOAD_TARGETS[key]["type_name"]
                doc = all_docs.get(type_name)
                if not doc:
                    results.append({"target": key, "type_name": type_name, "ok": False, "error": "UUID dokumen tidak ditemukan di ASTRI."})
                    continue
                if not doc.get("verified"):
                    results.append({"target": key, "type_name": type_name, "ok": True, "note": "Belum terverifikasi."})
                    continue
                result = unverify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = key
                results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI cluster unverify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/verify/<job_id>", methods=["POST"])
def api_astri_verify(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job belum selesai."}), 400
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
        targets = payload.get("targets")
        if targets == "all" or (isinstance(targets, list) and "all" in targets):
            docs = fetch_unverified_astri_documents(record, match)
            results = []
            for type_name, doc in docs.items():
                if is_astri_boundary_document(type_name):
                    results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-verify."})
                    continue
                result = verify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = type_name
                results.append(result)
        else:
            selected = [k for k in (targets or ["kmz", "boq", "hpdb"]) if k in ASTRI_UPLOAD_TARGETS]
            all_docs = fetch_astri_requirement_documents(record, match)
            results = []
            for key in selected:
                type_name = ASTRI_UPLOAD_TARGETS[key]["type_name"]
                doc = all_docs.get(type_name)
                if not doc:
                    results.append({"target": key, "type_name": type_name, "ok": False, "error": "UUID dokumen tidak ditemukan di ASTRI."})
                    continue
                if doc.get("verified"):
                    results.append({"target": key, "type_name": type_name, "ok": True, "note": "Sudah terverifikasi sebelumnya."})
                    continue
                result = verify_astri_document(record, match, type_name, doc["uuid"])
                result["target"] = key
                results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI verify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/unverify/<job_id>", methods=["POST"])
def api_astri_unverify(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job belum selesai."}), 400
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
        docs = fetch_astri_requirement_documents(record, match)
        results = []
        for type_name, doc in docs.items():
            if is_astri_boundary_document(type_name):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "skipped": True, "note": "Boundary tidak di-unverify."})
                continue
            if not doc.get("verified"):
                results.append({"target": type_name, "type_name": type_name, "ok": True, "note": "Sudah unverified sebelumnya."})
                continue
            result = unverify_astri_document(record, match, type_name, doc["uuid"])
            result["target"] = type_name
            results.append(result)
        return jsonify({"match": match, "results": results, "ok": all(item.get("ok") for item in results)})
    except Exception as exc:
        LOGGER.exception("ASTRI unverify failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/boq/preview/<job_id>")
def api_astri_boq_preview(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "cluster")
        files = astri_job_upload_files(job)
        if "boq" not in files:
            return jsonify({"error": "Output BOQ EVA tidak tersedia. Convert dengan output BOQ dulu.", "match": match}), 400
        create_boq = astri_create_boq_link(record, match)
        if not create_boq.get("enabled"):
            return jsonify({"error": "Create BOQ belum aktif di ASTRI. Approve Schedule dulu.", "match": match, "create_boq": create_boq}), 400
        path, filename = files["boq"]
        items = preview_eva_boq_file(path)
        return jsonify({"match": match, "create_boq": create_boq, "filename": filename, "items": items, "total_items": len(items)})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ preview failed")
        return astri_exception_response(exc)


@app.route("/api/astri/boq/preview/nojob")
def api_astri_boq_preview_nojob():
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "cluster")
        create_boq = astri_create_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ preview no-job failed")
        return astri_exception_response(exc)


@app.route("/api/astri/boq/supplier-map/<job_id>")
def api_astri_boq_supplier_map(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        scope = (request.args.get("scope") or request.args.get("target_type") or "cluster").strip().lower()
        match = _astri_selected_scope_query_match(record, request.args, scope)
        return jsonify({"match": match, **fetch_astri_boq_form_snapshot(record, match)})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ supplier map failed")
        return astri_exception_response(exc)


@app.route("/api/astri/boq/submit/<job_id>", methods=["POST"])
def api_astri_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return start_astri_boq_submission_response("cluster", job)


@app.route("/api/astri/boq/submit", methods=["POST"])
def api_astri_boq_submit_nojob():
    return start_astri_boq_submission_response("cluster")

def extract_astri_form_fields(form_html: str) -> list[tuple[str, str]]:
    """Read submitted values, including selected vendor options, exactly as ASTRI renders them."""
    fields: list[tuple[str, str]] = []

    for tag in re.findall(r"<input\b[^>]*>", form_html or "", flags=re.I):
        attrs = extract_tag_attrs(tag)
        name = attrs.get("name")
        if name:
            fields.append((name, attrs.get("value", "")))

    for tag in re.findall(r"<textarea\b[^>]*>.*?</textarea>", form_html or "", flags=re.I | re.S):
        attrs = extract_tag_attrs(tag[: tag.find(">") + 1])
        name = attrs.get("name")
        if name:
            body = re.search(r">(.*?)</textarea>\s*$", tag, flags=re.I | re.S)
            fields.append((name, html.unescape(body.group(1)) if body else ""))

    for block in re.findall(r"<select\b[^>]*>.*?</select>", form_html or "", flags=re.I | re.S):
        attrs = extract_tag_attrs(block[: block.find(">") + 1])
        name = attrs.get("name")
        if not name:
            continue
        options = re.findall(r"<option\b[^>]*>(.*?)</option>", block, flags=re.I | re.S)
        selected = re.search(r"<option\b([^>]*)\bselected(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^ >]+))?[^>]*>(.*?)</option>", block, flags=re.I | re.S)
        if not selected and re.fullmatch(r"boq\[[^\]]+\]\[vendor_name\]", str(name), flags=re.I):
            fields.append((name, ""))
            continue
        option = selected or (re.search(r"<option\b([^>]*)>(.*?)</option>", block, flags=re.I | re.S) if options else None)
        if not option:
            fields.append((name, ""))
            continue
        option_attrs = extract_tag_attrs("<option" + option.group(1) + ">")
        value = option_attrs.get("value", "")
        if not value:
            value = re.sub(r"<[^>]+>", "", option.group(2)).strip()
        fields.append((name, html.unescape(value)))
    return fields

def submit_astri_boq_items(
    record: dict,
    create_boq: dict,
    items: list[dict],
    pr_description: str,
    is_subfeeder: bool = False,
    permit_rt_rw: str = "",
    status_match: dict | None = None,
    status_scope: str | None = None,
    submission_id: str = "",
) -> dict:
    if not status_match or not status_scope:
        raise AstriMutationBlocked({
            "_http_status": 503,
            "code": "ASTRI_WO_STATUS_UNAVAILABLE",
            "error": "Identitas/status WO ASTRI wajib diverifikasi sebelum BOQ parse.",
        })
    require_astri_boq_live_mutation_gate(record, status_match, status_scope)
    parse_data: list[tuple[str, str]] = []
    plant_from_url = ""
    from urllib.parse import parse_qs, urlparse
    boq_url = str(create_boq.get("url", ""))
    boq_query = parse_qs(urlparse(boq_url).query, keep_blank_values=True)
    plant_from_url = next(
        (
            str(boq_query.get(key, [""])[0] or "").strip()
            for key in ("cluster_area_plant_code", "osp_route_area_plant_code", "area_plant_code")
            if str(boq_query.get(key, [""])[0] or "").strip()
        ),
        "",
    )
    # The live ASTRI form is the only supplier source of truth. If it cannot be
    # read, fail closed; never synthesize supplier fields from the project URL,
    # Excel, row order, or a default vendor.
    try:
        page = record["session"].get(create_boq["url"], timeout=60)
        LOGGER.info(
            "ASTRI BOQ form status=%s form_found=%s bytes=%s",
            page.status_code,
            "insert-boq-drm-form" in page.text,
            len(page.text.encode("utf-8", errors="replace")),
        )
        page.raise_for_status()
        form_match = re.search(r'<form[^>]+id=("|\')insert-boq-drm-form\1[^>]*>.*?</form>', page.text, flags=re.I | re.S)
        if not form_match:
            form_match = next((m for m in re.finditer(r'<form\b[^>]*>.*?</form>', page.text, flags=re.I | re.S) if "boq[" in m.group(0)), None)
        if not form_match:
            raise AstriMutationBlocked({
                "_http_status": 503,
                "code": "ASTRI_SUPPLIER_FORM_UNAVAILABLE",
                "error": "Form BOQ ASTRI tidak tersedia; supplier per-item tidak dapat diverifikasi.",
            })
        fields = extract_astri_form_fields(form_match.group(0))
        # The exact boq[CODE][vendor_name] fields remain in parse_data.
        # They are joined back to parsed items before any insert/v2 call.
        item_by_code = {str(item.get("code", "")).strip(): item for item in items}
        for name, value in _astri_boq_without_permit_fields(fields):
            match = re.match(r'boq\[([^\]]+)\]\[(quantity_material|quantity_service|override_price_service)\]', name)
            if match:
                item = item_by_code.get(match.group(1))
                if item:
                    key = "material_qty" if match.group(2) == "quantity_material" else "service_qty"
                    if match.group(2) == "override_price_service":
                        value = str(item.get("override_price_service") or value or "")
                    else:
                        value = str(item.get(key) or "")
            parse_data.append((name, value))
        if pr_description:
            parse_data.append(("work_order_progress[detail]", "[#PR_DESCRIPTION] " + pr_description))
        parse_data.append(("__create_boq_url", create_boq.get("url", "")))
        return _submit_astri_parsed(
            record, parse_data, items, pr_description, is_subfeeder,
            "", plant_from_url, status_match, status_scope,
            submission_id=submission_id,
        )
    except AstriMutationBlocked:
        raise
    except Exception as exc:
        LOGGER.warning("Failed to fetch exact BOQ supplier form; mutation blocked: %s", exc)
        raise AstriMutationBlocked({
            "_http_status": 503,
            "code": "ASTRI_SUPPLIER_FORM_UNAVAILABLE",
            "error": "Form BOQ ASTRI tidak dapat dibaca; supplier per-item tidak terverifikasi.",
        }) from exc


def _astri_boq_public_updates(updated: list[dict] | None) -> list[dict]:
    return [
        {
            "code": str(item.get("code") or "")[:100],
            "uuid": str(item.get("uuid") or "")[:80],
            "description": str(item.get("description") or "")[:200],
            "vendor_before": str(item.get("vendor_before") or "")[:100],
            "vendor_target": str(item.get("vendor_target") or "")[:100],
            "vendor_updated": bool(item.get("vendor_updated")),
            "changed_fields": [str(field) for field in (item.get("changed_fields") or [])][:10],
            "before": {
                "quantity_material": str((item.get("before") or {}).get("quantity_material") or "0")[:40],
                "quantity_service": str((item.get("before") or {}).get("quantity_service") or "0")[:40],
            },
            "after": {
                "quantity_material": str((item.get("after") or {}).get("quantity_material") or "0")[:40],
                "quantity_service": str((item.get("after") or {}).get("quantity_service") or "0")[:40],
            },
            "ok": bool(item.get("ok")),
            "status_code": int(item.get("status_code") or 0),
            "error": str(item.get("error") or "")[:240],
        }
        for item in (updated or [])
    ]


def _astri_boq_submission_checkpoint(
    submission_id: str,
    *,
    stage: str,
    inserted: list[dict] | None = None,
    updated: list[dict] | None = None,
    already_present: list[str] | None = None,
    active_code: str = "",
    vendor_mismatches: list[dict] | None = None,
    mutation_plan: list[dict] | None = None,
    mutation_started: bool | None = None,
    message: str = "",
) -> dict | None:
    if not submission_id:
        return None
    public_items = [
        {
            "code": str(item.get("material_number") or item.get("equipment_name") or "")[:100],
            "equipment_name": str(item.get("equipment_name") or "")[:100],
            "description": str(item.get("description") or "")[:200],
            "vendor_name": str(item.get("vendor_name") or "")[:100],
            "quantity_material": str(item.get("quantity_material") or "")[:40],
            "quantity_service": str(item.get("quantity_service") or "")[:40],
            "ok": bool(item.get("ok")),
            "skipped_no_price": bool(item.get("skipped_no_price")),
            "status_code": int(item.get("status_code") or 0),
        }
        for item in (inserted or [])
    ]
    public_updates = _astri_boq_public_updates(updated)
    skipped_items = [item for item in public_items if item["skipped_no_price"]]
    successful_inserts = sum(1 for item in public_items if item["ok"])
    failed_inserts = sum(1 for item in public_items if not item["ok"] and not item["skipped_no_price"])
    successful_updates = sum(1 for item in public_updates if item["ok"])
    failed_updates = sum(1 for item in public_updates if not item["ok"])
    changes: dict[str, object] = {
        "last_stage": stage,
        "message": message or "Add BOQ sedang diproses oleh ASTRI.",
        "result": {
            "processed_count": len(public_items) + len(public_updates),
            "ok_count": successful_inserts + successful_updates,
            "failed_count": failed_inserts + failed_updates,
            "skipped_count": len(skipped_items),
            "inserted_count": len(public_items),
            "updated_count": len(public_updates),
            "already_present_count": len(already_present or []),
            "already_present_codes": [str(code)[:100] for code in (already_present or [])],
            "active_code": str(active_code or "")[:100],
            "vendor_mismatch_count": len(vendor_mismatches or []),
            "skipped_items": [{"code": item["code"], "description": item["description"]} for item in skipped_items],
            "items": public_items,
            "updated_items": public_updates,
            "vendor_mismatches": [
                {
                    "code": str(item.get("code") or "")[:100],
                    "vendor_before": str(item.get("vendor_before") or "")[:100],
                    "vendor_target": str(item.get("vendor_target") or "")[:100],
                    "vendor_updated": False,
                }
                for item in (vendor_mismatches or [])
            ],
        },
    }
    if mutation_plan is not None:
        changes["mutation_plan"] = mutation_plan
    if mutation_started is not None:
        changes["mutation_started"] = mutation_started
    checkpoint = _astri_boq_worker_update(submission_id, **changes)
    if not checkpoint or str(checkpoint.get("state") or "") != "running":
        raise AstriMutationBlocked({"_http_status": 409, "code": "ASTRI_BOQ_WORKER_STALE", "error": "Proses Add BOQ worker sudah bukan pemilik state aktif; mutation dihentikan."})
    return checkpoint

def _astri_boq_worker_update(submission_id: str, **changes: object) -> dict | None:
    """Worker may checkpoint only its current running revision; terminal reconciliation wins."""
    store = get_astri_boq_submission_store()
    current = store.get(submission_id)
    if not current or str(current.get("state") or "") != "running":
        return current
    return store.update_if_current(
        submission_id,
        expected_state="running",
        expected_updated_at=float(current.get("updated_at") or 0),
        **changes,
    )


def _astri_boq_outbound_plan(
    parsed_items: list[dict],
    *,
    is_subfeeder: bool,
    is_feeder: bool,
    cluster_code: str,
    work_order_number: str,
    plant_from_url: str,
    permit_rt_rw: str,
    scope_code: str = "",
) -> list[dict]:
    """Build the exact deterministic insert/v2 plan before the first item POST."""
    expected_code = str(scope_code or cluster_code or "").strip()
    expected_wo = str(work_order_number or "").strip()
    plan: list[dict] = []
    for parsed in parsed_items:
        if _is_astri_permit_item(parsed):
            continue
        data = dict(_astri_boq_without_permit_fields(list(parsed.items())))
        if not (data.get("quantity_material") or data.get("quantity_service")):
            continue
        parsed_codes = [
            str(data.get(key) or "").strip()
            for key in ("cluster_code", "subfeeder_code", "feeder_code", "target_osp_route_code")
            if str(data.get(key) or "").strip()
        ]
        if any(value.casefold() != expected_code.casefold() for value in parsed_codes if expected_code):
            raise AstriMutationBlocked({
                "_http_status": 409,
                "code": "ASTRI_BOQ_IDENTITY_CONFLICT",
                "error": "Identitas scope BOQ hasil parse berbeda dari match terpilih.",
            })
        parsed_wo = str(data.get("work_order_number") or "").strip()
        if parsed_wo and expected_wo and parsed_wo.casefold() != expected_wo.casefold():
            raise AstriMutationBlocked({
                "_http_status": 409,
                "code": "ASTRI_BOQ_IDENTITY_CONFLICT",
                "error": "Work order BOQ hasil parse berbeda dari match terpilih.",
            })
        if is_feeder:
            data["feeder_code"] = expected_code
            if "target_osp_route_code" in data:
                data["target_osp_route_code"] = expected_code
        else:
            # ASTRI Cluster and Subfeeder insert/v2 both use cluster_code at
            # the form boundary; selected scope code remains authoritative.
            data["cluster_code"] = expected_code
            if is_subfeeder and "subfeeder_code" in data:
                data["subfeeder_code"] = expected_code
        data["work_order_number"] = expected_wo
        if plant_from_url:
            data["area_plant_code"] = plant_from_url
        if is_subfeeder:
            data.setdefault("subcont_vendor_name", "")
            data.setdefault("area_plant_code", "")
        plan.append(data)
    return plan


def _astri_boq_reconciliation_plan(outbound_plan: list[dict]) -> list[dict]:
    """Persist only the fields needed for exact GET-only multiset reconciliation."""
    return [
        {
            "code": str(item.get("material_number") or item.get("equipment_name") or item.get("code") or "")[:100],
            "description": str(item.get("description") or item.get("short_text") or "")[:200],
            "equipment_name": str(item.get("equipment_name") or "")[:100],
            "material_qty": str(item.get("quantity_material") or ""),
            "service_qty": str(item.get("quantity_service") or ""),
            "supplier": str(item.get("vendor_name") or "")[:100],
            "phase": "drm",
        }
        for item in outbound_plan
    ]


def _astri_boq_resume_plan(record: dict, scope: str, scope_code: str, outbound_plan: list[dict]) -> tuple[list[dict], list[str], dict]:
    """Read live BOQ and plan UUID updates instead of inserting duplicate rows."""
    normalized_scope = str(scope or "").strip().lower()
    code = str(scope_code or "").strip()
    filter_field = {"cluster": "cluster_code", "subfeeder": "subfeeder_code", "feeder": "feeder_code"}.get(normalized_scope)
    if not filter_field or not code:
        raise AstriMutationBlocked({"_http_status": 409, "code": "ASTRI_BOQ_RESUME_IDENTITY_INVALID", "error": "Identitas Add BOQ retry tidak valid."})
    response = record["session"].get(
        f"{ASTRI_BASE_URL}/material/{normalized_scope}/boq-drm/list/",
        params={f"filter[{filter_field}]": code, "limit": -1},
        timeout=60,
    )
    response.raise_for_status()
    live_records = parse_boq_records(normalized_scope, code, response.text, "")
    actions = build_boq_overwrite_actions(outbound_plan, live_records)
    if actions["unupdatable"]:
        raise AstriMutationBlocked({
            "_http_status": 409,
            "code": "ASTRI_BOQ_UUID_UNAVAILABLE",
            "error": "UUID BOQ ASTRI tidak tersedia untuk: %s." % ", ".join(item["code"] for item in actions["unupdatable"]),
        })
    return actions["pending"], actions["already_present"], actions


def _astri_boq_update_quantity(record: dict, action: dict, scope_code: str, scope: str = "subfeeder") -> dict:
    """Official ASTRI overwrite endpoint, keyed by existing BOQ UUID."""
    if _is_astri_permit_item(action):
        raise ValueError("Permit RT/RW dikelola ASTRI; EVA tidak boleh mengubahnya.")
    after = action.get("after") or {}
    normalized_scope = str(scope or "subfeeder").strip().lower()
    filter_field = {
        "cluster": "cluster_code",
        "subfeeder": "subfeeder_code",
        "feeder": "feeder_code",
    }.get(normalized_scope, "subfeeder_code")
    ref_page = f"/material/{normalized_scope}/boq-drm/list/?filter[{filter_field}]={scope_code}"
    data = {
        "csrf_name": "",
        "csrf_value": "",
        "ref_page": ref_page,
        "boq[uuid]": str(action.get("uuid") or ""),
        # ASTRI's generic update form names this field cluster_code even on
        # the subfeeder list; keep the live form contract unchanged.
        "boq[cluster_code]": str(scope_code or ""),
        "boq[cluster_name]": str(action.get("scope_name") or ""),
        "boq[description]": str(action.get("description") or ""),
        "boq[quantity_material]": str(after.get("quantity_material") or "0"),
        "boq[quantity_service]": str(after.get("quantity_service") or "0"),
        "boq[override_price_material]": str(after.get("override_price_material") or "0"),
        "boq[override_price_service]": str(after.get("override_price_service") or "0"),
    }
    response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/boq/update/quantity/by-uuid",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=120,
        allow_redirects=False,
    )
    ok = response.status_code in {200, 201, 302, 303}
    try:
        body = response.json()
        ok = ok and body.get("success") is not False
    except Exception:
        body = {"text": response.text[:500]}
    return {**action, "ok": ok, "status_code": response.status_code, "error": "" if ok else str(body)[:240]}

def _submit_astri_parsed(
    record: dict,
    parse_data: list,
    items: list[dict],
    pr_description: str,
    is_subfeeder: bool,
    permit_rt_rw: str = "",
    plant_from_url: str = "",
    status_match: dict | None = None,
    status_scope: str | None = None,
    submission_id: str = "",
) -> dict:

    # Ignore legacy/client permit inputs at the final upstream mutation boundary.
    permit_rt_rw = ""
    parse_data = _astri_boq_without_permit_fields(parse_data)
    items = [item for item in items if not _is_astri_permit_item(item)]
    if status_match and status_scope:
        require_astri_boq_live_mutation_gate(record, status_match, status_scope)
    create_boq_url = str(dict(parse_data).get("__create_boq_url", ""))
    is_feeder = "/material/feeder/boq-drm/" in create_boq_url

    _astri_boq_submission_checkpoint(
        submission_id,
        stage="parse",
        mutation_started=True,
        message="Form BOQ sedang diproses oleh ASTRI.",
    )
    parse_response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/boq-drm/parse",
        data=parse_data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=120,
    )
    parse_response.raise_for_status()
    parsed_items = parse_response.json()
    LOGGER.info("ASTRI BOQ parsed item_count=%d", len(parsed_items))
    autofix = eva_ai_autofix_boq_payload(parsed_items, parse_data, permit_rt_rw=permit_rt_rw)
    parsed_items = autofix.get("items") or parsed_items
    if autofix.get("fixes"):
        LOGGER.info("EVA-AI BOQ autofix count=%d", len(autofix.get("fixes") or []))
    qa = astri_boq_qa_gate(input_items=items, parsed_items=parsed_items, pr_description=pr_description, permit_rt_rw=permit_rt_rw)
    if not qa.get("ok"):
        LOGGER.error("ASTRI BOQ QA blocked submit: %s", qa)
        return {"success": False, "blocked_by_eva_ai_qa": True, "error": "EVA-AI QA: BOQ submit diblokir karena payload berisiko.", "qa": qa, "inserted": []}
    if qa.get("warnings"):
        LOGGER.warning("ASTRI BOQ QA warnings: %s", qa)
    url_prefix = "subfeeder/" if is_subfeeder else ("feeder/" if is_feeder else "")
    insert_url = f"{ASTRI_BASE_URL}/process/material/{url_prefix}boq-drm/insert/v2"
    pd = dict(parse_data)
    selected_scope_code = _astri_boq_scope_code(status_match, str(status_scope)) if status_match and status_scope else ""
    cc = pd.get("cluster_code", "")
    won = pd.get("work_order_number", "")
    if status_match and status_scope and (
        not won or str(won).strip().casefold() != str(status_match.get("work_order_number") or "").strip().casefold()
    ):
        raise AstriMutationBlocked({
            "_http_status": 409,
            "code": "ASTRI_BOQ_IDENTITY_CONFLICT",
            "error": "Work order form BOQ berbeda dari match terpilih.",
        })
    outbound_plan = _astri_boq_outbound_plan(
        parsed_items,
        is_subfeeder=is_subfeeder,
        is_feeder=is_feeder,
        cluster_code=str(cc or ""),
        work_order_number=str(won or ""),
        plant_from_url=plant_from_url,
        permit_rt_rw=permit_rt_rw,
        scope_code=selected_scope_code,
    )
    if not outbound_plan:
        raise AstriMutationBlocked({
            "_http_status": 409,
            "code": "ASTRI_BOQ_PLAN_EMPTY",
            "error": "ASTRI parse tidak menghasilkan mutation plan BOQ yang dapat diverifikasi.",
        })
    reconciliation_plan = _astri_boq_reconciliation_plan(outbound_plan)
    _astri_boq_submission_checkpoint(
        submission_id,
        stage="plan-ready",
        mutation_plan=reconciliation_plan,
        mutation_started=True,
        message=f"Mutation plan BOQ {len(reconciliation_plan)} item tersimpan; siap mengirim ke ASTRI.",
    )
    outbound_plan, already_present, overwrite_actions = _astri_boq_resume_plan(
        record, status_scope or "", selected_scope_code or str(cc or ""), outbound_plan
    )
    if already_present:
        LOGGER.info("ASTRI BOQ retry skips %d exact existing items: %s",
                    len(already_present), ",".join(already_present[:20]))
    inserted = []
    updated = []
    for action in overwrite_actions.get("updates", []):
        require_astri_boq_live_mutation_gate(record, status_match, status_scope)
        _astri_boq_submission_checkpoint(
            submission_id,
            stage="overwrite",
            updated=updated,
            already_present=already_present,
            active_code=str(action.get("code") or ""),
            vendor_mismatches=overwrite_actions.get("vendor_mismatches"),
            mutation_started=True,
            message=f"Menimpa qty BOQ {len(updated) + 1} ke ASTRI...",
        )
        result = _astri_boq_update_quantity(record, action, selected_scope_code or str(cc or ""), status_scope or "subfeeder")
        updated.append(result)
        _astri_boq_submission_checkpoint(
            submission_id,
            stage="overwrite",
            updated=updated,
            already_present=already_present,
            vendor_mismatches=overwrite_actions.get("vendor_mismatches"),
            mutation_started=True,
            message=f"{len(updated)} qty BOQ sudah di-update ASTRI.",
        )
    for planned in outbound_plan:
        data = dict(planned)
        # Re-read the exact WO status immediately before every insert/v2 POST.
        require_astri_boq_live_mutation_gate(record, status_match, status_scope)
        _astri_boq_submission_checkpoint(
            submission_id,
            stage="insert",
            inserted=inserted,
            updated=updated,
            already_present=already_present,
            active_code=str(data.get("material_number") or data.get("equipment_name") or ""),
            vendor_mismatches=overwrite_actions.get("vendor_mismatches"),
            mutation_started=True,
            message=f"Mengirim item BOQ {len(inserted) + 1} ke ASTRI.",
        )
        LOGGER.info(
            "ASTRI BOQ insert item=%d/%d code=%s",
            len(inserted) + 1,
            len(outbound_plan),
            str(data.get("material_number") or data.get("equipment_name") or "")[:100],
        )
        response = record["session"].post(insert_url, data=data, timeout=120)
        ok = response.status_code in {200, 201, 302, 303}
        try:
            body = response.json()
            ok = ok and body.get("success") is not False
        except Exception:
            body = {"text": response.text[:500]}
        # One insert per item, exact selected supplier. No alternate vendor,
        # price-id, override, or synthetic retry is permitted. Never log payload/body.
        LOGGER.info("ASTRI BOQ insert response status=%s ok=%s", response.status_code, ok)
        # Categorize "no price even with override" as skipped (very rare — ASTRI rejects entirely)
        skipped_no_price = False
        if not ok and isinstance(body, dict):
            err_str = str(body)
            if "error_vendor_price_not_found" in err_str or "vendor_price" in err_str or "error_third_party_exception" in err_str or "third_party" in err_str:
                skipped_no_price = True
        inserted.append({
            "material_number": data.get("material_number") or data.get("equipment_name"),
            "equipment_name": data.get("equipment_name"),
            "description": data.get("description", ""),
            "vendor_name": data.get("vendor_name", ""),
            "quantity_material": data.get("quantity_material", ""),
            "quantity_service": data.get("quantity_service", ""),
            "ok": ok,
            "skipped_no_price": skipped_no_price,
            "override_used": data.get("override_price_service") == "0" or data.get("use_drm_price") == "true",
            "status_code": response.status_code,
            "response": body,
        })
        _astri_boq_submission_checkpoint(
            submission_id,
            stage="insert",
            inserted=inserted,
            updated=updated,
            already_present=already_present,
            vendor_mismatches=overwrite_actions.get("vendor_mismatches"),
            mutation_started=True,
            message=f"{len(inserted)} item BOQ sudah diproses ASTRI.",
        )

    won_val = dict(parse_data).get("work_order_number") or ""
    any_ok = any(item.get("ok") for item in inserted) or any(item.get("ok") for item in updated) or bool(already_present)
    skipped_count = sum(1 for i in inserted if i.get("skipped_no_price"))
    fail_count = sum(1 for i in inserted if not i.get("ok") and not i.get("skipped_no_price"))
    ok_count = sum(1 for i in inserted if i.get("ok")) + sum(1 for i in updated if i.get("ok"))
    overwrite_fail_count = sum(1 for i in updated if not i.get("ok"))
    if not any_ok:
        LOGGER.warning("Skipping Close WO: all %d inserts failed", len(inserted))
        return {"parsed_count": len(parsed_items), "inserted": inserted, "close_ok": False, "ok": False,
                "summary": {"ok": ok_count, "failed": fail_count, "skipped_no_price": skipped_count}}
    if fail_count or skipped_count or overwrite_fail_count:
        skipped_codes = [
            str(item.get("material_number") or item.get("equipment_name") or "").strip()
            for item in inserted if item.get("skipped_no_price")
        ]
        LOGGER.warning("Skipping Close WO: BOQ insert incomplete ok=%d failed=%d skipped_no_price=%d codes=%s",
                       ok_count, fail_count, skipped_count, ",".join(skipped_codes))
        return {"parsed_count": len(parsed_items), "inserted": inserted, "close_ok": False, "ok": False,
                "summary": {"ok": ok_count, "failed": fail_count + overwrite_fail_count, "skipped_no_price": skipped_count},
                "skipped_codes": skipped_codes,
                "error": ("Qty BOQ overwrite gagal; Work order tidak di-close." if overwrite_fail_count else "Pricelist not available untuk material: %s. Work order tidak di-close."
                         % ", ".join(skipped_codes))}
    # Re-read again immediately before Close WO. A status change after the last
    # insert must still block the close mutation.
    require_astri_boq_live_mutation_gate(record, status_match, status_scope)
    _astri_boq_submission_checkpoint(
        submission_id,
        stage="close",
        inserted=inserted,
        updated=updated,
        already_present=already_present,
        vendor_mismatches=overwrite_actions.get("vendor_mismatches"),
        mutation_started=True,
        message="Semua item masuk. Menutup WO di ASTRI.",
    )
    close_data = list(parse_data)
    close_data.extend([
        ("work_order[status]", "4"),
        ("work_order[number]", won_val),
        ("work_order[detail]", "[#PR_DESCRIPTION] " + pr_description),
        ("returnType", "json"),
    ])
    LOGGER.info("ASTRI BOQ Close WO request field_count=%d", len(close_data))
    try:
        close_response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/work-order/update",
            data=close_data,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=60,
        )
        close_ok = close_response.status_code in {200, 201, 302, 303}
        try:
            close_body = close_response.json()
            close_ok = close_ok and close_body.get("success") is not False
        except Exception:
            close_body = {"text": close_response.text[:500]}
        LOGGER.info("ASTRI BOQ Close WO response status=%s ok=%s", close_response.status_code, close_ok)
    except Exception as exc:
        LOGGER.warning("Close WO failed: %s", exc)
        close_ok = False
    return {"parsed_count": len(parsed_items), "inserted": inserted, "updated": updated, "already_present": already_present, "vendor_mismatches": overwrite_actions.get("vendor_mismatches"), "close_ok": close_ok,
            "ok": all(item.get("ok") or item.get("skipped_no_price") for item in inserted),
            "summary": {"ok": ok_count, "failed": fail_count, "skipped_no_price": skipped_count}}


@app.route("/api/astri/donation")
def api_astri_donation():
    code = request.args.get("cluster_code", "")
    try:
        record = require_astri_session()
        if not code:
            return jsonify({"found": False, "donation": None})
        params = {"filter[global]": code}
        page = record["session"].get(f"{ASTRI_BASE_URL}/propose-donation/list", params=params, timeout=60)
        page.raise_for_status()
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page.text, flags=re.S | re.I)
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
            if len(cells) < 14:
                continue
            clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if clean[4] == code:
                nominal_str = clean[14] if len(clean) > 14 else ""
                vendor_name = clean[3] if len(clean) > 3 else ""
                pph_pct = 0.0
                permit_rt_rw = ""
                try:
                    nominal_num = float(nominal_str.replace(".", "").replace(",", "."))
                except (ValueError, AttributeError):
                    nominal_num = 0.0
                if vendor_name:
                    import sqlite3 as _sqlite3
                    import re as _re
                    db_path = os.path.join(os.path.dirname(__file__), "data", "pph_vendor.db")
                    if os.path.exists(db_path):
                        conn = _sqlite3.connect(db_path)
                        cur = conn.cursor()
                        # Normalize: strip spaces/punct so 'INDO TUNAS MAKMUR' matches 'Indotunas Makmur'
                        def _norm(x):
                            return _re.sub(r"[^a-z0-9]", "", (x or "").lower())
                        vendor_norm = _norm(vendor_name)
                        cur.execute("SELECT vendor, pph_vendor FROM pph_vendor")
                        rows_db = cur.fetchall()
                        # 1. exact normalize
                        row_pph = None
                        for v, pph in rows_db:
                            if _norm(v) == vendor_norm:
                                row_pph = (pph,)
                                break
                        # 2. keyword match on normalized
                        if not row_pph:
                            keywords_norm = [k for k in _re.findall(r"[a-z0-9]+", vendor_name.lower()) if len(k) >= 3]
                            best = None
                            for v, pph in rows_db:
                                vnorm = _norm(v)
                                if any(k in vnorm for k in keywords_norm):
                                    if best is None or len(v) > len(best[0]):
                                        best = (v, pph)
                            row_pph = (best[1],) if best else None
                        if row_pph and row_pph[0]:
                            pph_str = str(row_pph[0]).replace(",", ".").replace("%", "").strip()
                            try:
                                pph_pct = float(pph_str) / 100.0
                            except ValueError:
                                pph_pct = 0.0
                        conn.close()
                # INTERNAL TEAM is inhouse donation. Do not inject donation
                # nominal into ASTRI work_order[permit] or permit BOQ line.
                if _is_internal_team_vendor(vendor_name):
                    permit_rt_rw = "0"
                elif nominal_num > 0 and pph_pct > 0:
                    permit_rt_rw = str(int(nominal_num / (1 - pph_pct)))
                elif nominal_num > 0:
                    permit_rt_rw = str(int(nominal_num))
                item = {
                    "wo": clean[1] if len(clean) > 1 else "",
                    "status_wo": clean[2] if len(clean) > 2 else "",
                    "vendor": vendor_name,
                    "cluster_code": clean[4] if len(clean) > 4 else "",
                    "cluster_name": clean[6] if len(clean) > 6 else "",
                    "area": clean[7] if len(clean) > 7 else "",
                    "donation_status": clean[11] if len(clean) > 11 else "",
                    "clean_list": clean[12] if len(clean) > 12 else "",
                    "homepass": clean[13] if len(clean) > 13 else "",
                    "nominal": nominal_str,
                    "batch": clean[15] if len(clean) > 15 else "",
                    "pic": clean[18] if len(clean) > 18 else "",
                    "pph_pct": pph_pct * 100,
                    "permit_rt_rw": permit_rt_rw,
                }
                return jsonify({"found": True, "donation": item})
        return jsonify({"found": False, "donation": None})
    except Exception as exc:
        LOGGER.exception("ASTRI donation fetch failed")
        return jsonify({"found": False, "donation": None})


@app.route("/api/astri/subfeeder/schedule/form")
def api_astri_subfeeder_schedule_form():
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "subfeeder")
        form = extract_astri_approve_schedule_form(fetch_astri_subfeeder_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder schedule form failed")
        return astri_exception_response(exc)

@app.route("/api/astri/subfeeder/schedule/approve", methods=["POST"])
def api_astri_subfeeder_schedule_approve():
    try:
        record = require_astri_session()
        form_data = request.form.to_dict(flat=True)
        guard = astri_work_order_guard(record, astri_schedule_match_from_form(form_data, "subfeeder"), "subfeeder")
        if guard:
            return guard
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/subfeeder/drm/schedule/approve",
            data=astri_public_form_data(form_data),
            allow_redirects=False,
            timeout=60,
        )
        location = response.headers.get("Location", "")
        if response.status_code in {302, 303} and re.search(r"/(login|setting|dashboard)(\?|$|/)", location, re.I):
            ok = False
            reason = "Redirect to login/setting page - session may be expired"
        else:
            ok = response.status_code in {200, 201, 302, 303}
            reason = ""
        LOGGER.warning(f"Schedule approve: status={response.status_code} location={location} ok={ok}")
        return jsonify({"ok": ok, "status_code": response.status_code, "location": location, "reason": reason})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder schedule approve failed")
        return jsonify({"error": str(exc)}), 400

@app.route("/api/astri/subfeeder/boq/preview/nojob")
def api_astri_subfeeder_boq_preview_nojob():
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "subfeeder")
        create_boq = astri_create_subfeeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder BOQ preview nojob failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/astri/subfeeder/boq/preview/<job_id>")
def api_astri_subfeeder_boq_preview(job_id: str):
    job = get_job(job_id)
    try:
        record = require_astri_session()
        if job:
            match = _astri_selected_scope_query_match(record, request.args, "subfeeder")
            files = astri_job_upload_files(job)
            create_boq = astri_create_subfeeder_boq_link(record, match)
            if "boq" in files:
                path, filename = files["boq"]
                items = preview_eva_boq_file(path)
                return jsonify({"match": match, "create_boq": create_boq, "filename": filename, "items": items, "total_items": len(items), "permit_rt_rw": ""})
            return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
        match = _astri_selected_scope_query_match(record, request.args, "subfeeder")
        create_boq = astri_create_subfeeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder BOQ preview failed")
        return astri_exception_response(exc)



@app.route("/api/astri/subfeeder/boq/submit/<job_id>", methods=["POST"])
def api_astri_subfeeder_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return start_astri_boq_submission_response("subfeeder", job)


@app.route("/api/astri/subfeeder/boq/submit", methods=["POST"])
def api_astri_subfeeder_boq_submit_nojob():
    return start_astri_boq_submission_response("subfeeder")


@app.route("/api/astri/boq/supplier-map")
def api_astri_boq_supplier_map_nojob():
    try:
        record = require_astri_session()
        scope = (request.args.get("scope", "").strip() or "cluster").lower()
        match = _astri_selected_scope_query_match(record, request.args, scope)
        return jsonify({"match": match, **fetch_astri_boq_form_snapshot(record, match)})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ supplier map no-job failed")
        return astri_exception_response(exc)


@app.route("/api/astri/boq/parse-file", methods=["POST"])
def api_astri_boq_parse_file():
    if "file" not in request.files:
        return jsonify({"error": "File tidak ditemukan."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Nama file tidak valid."}), 400
    import tempfile
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp)
        tmp_path = Path(tmp.name)
    try:
        items = preview_eva_boq_file(tmp_path)
        if not items:
            return jsonify({"error": "Tidak bisa parse BOQ file. Format tidak sesuai.", "items": []}), 400

        # Supplier is source-of-truth from ASTRI form field boq[CODE][vendor_name],
        # never from the manually uploaded Excel workbook.
        supplier_map = {}
        supplier_error = ""
        raw_match = request.form.get("astri_match", "")
        try:
            supplied_match = json.loads(raw_match) if raw_match else {}
            supplied_scope = str(
                supplied_match.get("scope")
                or supplied_match.get("target_type")
                or "cluster"
            ).strip().lower()
            supplied_code_key = {
                "cluster": "cluster_code",
                "subfeeder": "subfeeder_code",
                "feeder": "feeder_code",
            }.get(supplied_scope)
            if supplied_code_key and supplied_match.get(supplied_code_key):
                record = require_astri_session()
                supplier_map = fetch_astri_boq_supplier_map(
                    record, {**supplied_match, "scope": supplied_scope}
                )
                LOGGER.warning(
                    "Manual BOQ supplier map: scope=%s code=%s feeder=%s wo=%s vendor=%s mapped=%s",
                    supplied_match.get("scope"), supplied_match.get("cluster_code"), supplied_match.get("feeder_code"),
                    supplied_match.get("work_order_number"), supplied_match.get("vendor_label") or supplied_match.get("assigned_vendor_name"), len(supplier_map)
                )
            else:
                supplier_error = "Match ASTRI kosong."
                LOGGER.warning("Manual BOQ supplier map skipped: empty match raw=%s", raw_match[:500])
        except Exception as exc:
            supplier_error = str(exc)
            LOGGER.warning("Manual BOQ supplier map failed: %s", exc)

        # Excel may deserialize material code as 500001012.0, 500001012.00,
        # "500001012 - description", or formatted numeric text. ASTRI keys are digits only.
        def _boq_supplier_key(value):
            text = str(value or "").strip()
            decimal_match = re.match(r"^(\d+)\.0+$", text)
            if decimal_match:
                return decimal_match.group(1)
            return re.sub(r"\D", "", text)

        normalized_supplier_map = {_boq_supplier_key(code): value for code, value in supplier_map.items()}
        parsed_keys = []
        mapped_count = 0
        for item in items:
            code = _boq_supplier_key(item.get("code"))
            parsed_keys.append(code)
            supplier = normalized_supplier_map.get(code) or {}
            item["supplier"] = supplier.get("supplier") or ""
            item["supplier_kind"] = supplier.get("supplier") or ""
            item["supplier_found"] = bool(supplier.get("supplier"))
            mapped_count += int(item["supplier_found"])

        LOGGER.warning(
            "Manual BOQ supplier join: parsed=%s mapped=%s parsed_sample=%s astri_sample=%s supplier_values=%s",
            len(items), mapped_count, parsed_keys[:10], list(normalized_supplier_map)[:10],
            {key: (normalized_supplier_map.get(key) or {}).get("supplier") for key in ["500004774", "500003890", "200000100", "500002050", "200001039", "500002053"]}
        )
        return jsonify({
            "items": items,
            "total_items": len(items),
            "filename": file.filename,
            "supplier_source": "ASTRI",
            "supplier_mapped": mapped_count,
            "supplier_error": supplier_error,
        })
    except Exception as exc:
        return jsonify({"error": f"Gagal parse BOQ file: {exc}", "items": []}), 400
    finally:
        tmp_path.unlink(missing_ok=True)


@app.route("/api/astri/subfeeder/boq/upload-manual/<job_id>", methods=["POST"])
def api_astri_subfeeder_boq_upload_manual(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        cluster_name = request.form.get("cluster_name") or cluster_name_for_job(job)
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        items_list = search_astri_subfeeders(record, code) if code else []
        match = items_list[0] if items_list else {"matched": False, "query": cluster_name}
        if not match.get("matched"):
            return jsonify({"error": "Subfeeder ASTRI tidak ditemukan.", "match": match}), 404
        if "file" not in request.files:
            return jsonify({"error": "File tidak ditemukan."}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "Nama file tidak valid."}), 400
        import tempfile
        suffix = Path(file.filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            file.save(tmp)
            tmp_path = Path(tmp.name)
        try:
            result = upload_file_to_astri_subfeeder(record, match, "boq", tmp_path, file.filename)
            return jsonify({"match": match, **result})
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder manual BOQ upload failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/route-list")
def api_astri_route_list():
    try:
        record = require_astri_session()
        raw_area = request.args.get("area", "").strip()
        area = raw_area.replace("_", " ").upper().strip()
        for prefix in ["FEEDER AREA ", "AREA ", "WILAYAH "]:
            if area.startswith(prefix):
                area = area[len(prefix):].strip()
                break
        LOGGER.info("Route list: normalized area=%r (was %r)", area, raw_area)
        if not area:
            return jsonify({"data": []})
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/route/list",
            params={"filter[area]": area, "length": -1},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=60,
        )
        if response.status_code != 200:
            return jsonify({"data": [], "error": f"ASTRI {response.status_code}"})
        data = response.json()
        items = []
        for r in data.get("data", []):
            items.append({"value": r.get("name", ""), "text": f"{r.get('feeder_code','')} - {r.get('label','')}"})
        return jsonify({"data": items, "count": len(items)})
    except Exception as exc:
        LOGGER.exception("ASTRI route list failed")
        return jsonify({"error": str(exc), "data": []}), 400


@app.route("/api/astri/olt-list")
def api_astri_olt_list():
    try:
        record = require_astri_session()
        raw_area = request.args.get("area", "").strip()
        LOGGER.info("OLT list request: raw area=%r", raw_area)
        # Normalize: underscores to spaces, uppercase, strip common prefixes
        area = raw_area.replace("_", " ").upper().strip()
        for prefix in ["OLT ARE ", "AREA ", "OLT AREA ", "WILAYAH "]:
            if area.startswith(prefix):
                area = area[len(prefix):].strip()
                break
        if area != raw_area:
            LOGGER.info("OLT list: normalized area=%r (was %r)", area, raw_area)
        if not area:
            # Perhaps the select value is empty but text has area name — just try raw
            if raw_area:
                area = raw_area
            else:
                return jsonify({"data": []})
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/olt/list",
            params={
                "filter[area]": area,
                "filter[label]": "--not_null",
                "filter[order_by]": "label",
                "filter[order_rule]": "ASC",
                "limit": -1,
                "returnType": "json",
            },
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=60,
        )
        if response.status_code != 200:
            return jsonify({"data": [], "error": f"ASTRI {response.status_code}"})
        data = response.json()
        items = []
        for r in data.get("data", []):
            code = r.get("device_code", "") or r.get("code", "")
            name = r.get("name", "")
            label = r.get("label", "") or name
            value = f"{code} - {name}" if code else name
            text = f"{code} - {label}" if code else label
            relation = {k: r.get(k, "") for k in (
                "uuid", "site_id", "site_is_odc", "odc_uuid", "odc_code", "odc_name", "odc_label",
                "site_uuid", "site_code", "site_name", "site_label",
            )}
            relation["device_label"] = text
            relation["device_uuid"] = relation.pop("uuid", "")
            relation["assigned_site_id"] = relation.pop("site_id", "")
            items.append({"value": value, "text": text, "relation": relation})
        return jsonify({"data": items, "count": len(items)})
    except Exception as exc:
        LOGGER.exception("ASTRI olt list failed")
        return jsonify({"error": str(exc), "data": []}), 400


@app.route("/api/astri/metro-list")
def api_astri_metro_list():
    try:
        record = require_astri_session()
        raw_area = request.args.get("area", "").strip()
        area = raw_area.replace("_", " ").upper().strip()
        for prefix in ["METRO AREA ", "AREA ", "WILAYAH "]:
            if area.startswith(prefix):
                area = area[len(prefix):].strip()
                break
        if not area:
            return jsonify({"data": []})
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/metro/list",
            params={
                "filter[area]": area,
                "filter[label]": "--not_null",
                "filter[order_by]": "label",
                "filter[order_rule]": "ASC",
                "limit": -1,
                "returnType": "json",
            },
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            timeout=60,
        )
        if response.status_code != 200:
            return jsonify({"data": [], "error": f"ASTRI {response.status_code}"})
        items = []
        for r in response.json().get("data", []):
            code = r.get("device_code", "") or r.get("code", "")
            name = r.get("name", "")
            label = r.get("label", "") or name
            value = f"{code} - {name}" if code else name
            text = f"{code} - {label}" if code else label
            relation = {k: r.get(k, "") for k in (
                "uuid", "site_id", "site_is_odc", "odc_uuid", "odc_code", "odc_name", "odc_label",
                "site_uuid", "site_code", "site_name", "site_label",
            )}
            relation["device_label"] = text
            relation["device_uuid"] = relation.pop("uuid", "")
            relation["assigned_site_id"] = relation.pop("site_id", "")
            items.append({"value": value, "text": text, "relation": relation})
        return jsonify({"data": items, "count": len(items)})
    except Exception as exc:
        LOGGER.exception("ASTRI metro list failed")
        return jsonify({"error": str(exc), "data": []}), 400


@app.route("/api/astri/clean-list-area")
def api_astri_clean_list_area():
    """Fetch province + area_name from ASTRI clean-list for a given cluster_code."""
    try:
        record = require_astri_session()
        cluster_code = request.args.get("cluster_code", "").strip()
        if not cluster_code:
            return jsonify({"error": "cluster_code required"})
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/distribution/cluster/clean-list",
            params={"filter[category_name]": "survey_permit_acquisition"},
            timeout=60,
        )
        if response.status_code != 200:
            return jsonify({"error": f"ASTRI {response.status_code}"})
        html = response.text
        import re as _re
        # Find the row with matching cluster_code
        pattern = rf'<tr[^>]*>.*?{ _re.escape(cluster_code) }.*?</tr>'
        row_match = _re.search(pattern, html, _re.S | _re.I)
        if not row_match:
            return jsonify({"area_name": "", "province": "", "found": False})
        tds = _re.findall(r'<td[^>]*>(.*?)</td>', row_match.group(0), _re.S)
        cols = [_re.sub(r'<[^>]+>', '', td).strip() for td in tds]
        # Columns: 0=Action,1=WO,2=Status,3=Vendor,4=Subcont,5=PIC,6=ClusterCode,
        #          7=ClusterProjectName,8=Name,9=Area,10=Coordinate,11=OLT,12=StatusPermit,
        #          13=StatusCluster,14=NTPRequestNumber
        area_name = cols[9] if len(cols) > 9 else ""
        # Province lookup from city/area
        province = ""
        if area_name:
            province = province_for_city(area_name)
        return jsonify({"area_name": area_name, "province": province, "found": True, "columns": cols[:15]})
    except Exception as exc:
        LOGGER.exception("ASTRI clean-list area failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/schedule/form")
def api_astri_feeder_schedule_form():
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "feeder")
        form = extract_astri_approve_schedule_form(fetch_astri_feeder_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder schedule form failed")
        return astri_exception_response(exc)


@app.route("/api/astri/feeder/schedule/approve", methods=["POST"])
def api_astri_feeder_schedule_approve():
    try:
        record = require_astri_session()
        form_data = request.form.to_dict(flat=True)
        guard = astri_work_order_guard(record, astri_schedule_match_from_form(form_data, "feeder"), "feeder")
        if guard:
            return guard
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/feeder/drm/schedule/approve",
            data=astri_public_form_data(form_data),
            allow_redirects=False,
            timeout=60,
        )
        location = response.headers.get("Location", "")
        if response.status_code in {302, 303} and re.search(r"/(login|setting|dashboard)(\?|$|/)", location, re.I):
            ok = False
            reason = "Redirect to login/setting page - session may be expired"
        else:
            ok = response.status_code in {200, 201, 302, 303}
            reason = ""
        LOGGER.warning(f"Schedule approve: status={response.status_code} location={location} ok={ok}")
        return jsonify({"ok": ok, "status_code": response.status_code, "location": location, "reason": reason})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder schedule approve failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/boq/preview/nojob")
def api_astri_feeder_boq_preview_nojob():
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "feeder")
        create_boq = astri_create_feeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder BOQ preview nojob failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/astri/feeder/boq/preview/<job_id>")
def api_astri_feeder_boq_preview(job_id: str):
    job = get_job(job_id)
    try:
        record = require_astri_session()
        if job:
            match = _astri_selected_scope_query_match(record, request.args, "feeder")
            files = astri_job_upload_files(job)
            create_boq = astri_create_feeder_boq_link(record, match)
            if "boq" in files:
                path, filename = files["boq"]
                items = preview_eva_boq_file(path)
                return jsonify({"match": match, "create_boq": create_boq, "filename": filename, "items": items, "total_items": len(items), "permit_rt_rw": ""})
            return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
        match = _astri_selected_scope_query_match(record, request.args, "feeder")
        create_boq = astri_create_feeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder BOQ preview failed")
        return astri_exception_response(exc)


@app.route("/api/astri/feeder/boq/submit/<job_id>", methods=["POST"])
def api_astri_feeder_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return start_astri_boq_submission_response("feeder", job)


@app.route("/api/astri/feeder/boq/submit", methods=["POST"])
def api_astri_feeder_boq_submit_nojob():
    return start_astri_boq_submission_response("feeder")


@app.route("/api/astri/schedule/form")
def api_astri_schedule_form_direct():
    try:
        record = require_astri_session()
        scope = (request.args.get("scope") or request.args.get("target_type") or "cluster").strip().lower()
        if scope != "cluster":
            return jsonify({"error": "Gunakan endpoint schedule/form sesuai scope ASTRI."}), 400
        match = _astri_selected_scope_query_match(record, request.args, "cluster")
        form = extract_astri_approve_schedule_form(fetch_astri_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule form failed")
        return astri_exception_response(exc)


@app.route("/api/astri/schedule/form/<job_id>")
def api_astri_schedule_form(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        match = _astri_selected_scope_query_match(record, request.args, "cluster")
        form = extract_astri_approve_schedule_form(fetch_astri_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule form failed")
        return astri_exception_response(exc)

@app.route("/api/astri/schedule/approve", methods=["POST"])
def api_astri_schedule_approve_direct():
    try:
        record = require_astri_session()
        form_data = request.form.to_dict(flat=True)
        guard = astri_work_order_guard(record, astri_schedule_match_from_form(form_data, "cluster"), "cluster")
        if guard:
            return guard
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/drm/schedule/approve",
            data=astri_public_form_data(form_data),
            allow_redirects=False,
            timeout=60,
        )
        location = response.headers.get("Location", "")
        if response.status_code in {302, 303} and re.search(r"/(login|setting|dashboard)(\?|$|/)", location, re.I):
            ok = False
            reason = "Redirect to login/setting page - session may be expired"
        else:
            ok = response.status_code in {200, 201, 302, 303}
            reason = ""
        LOGGER.warning(f"Schedule approve: status={response.status_code} location={location} ok={ok}")
        return jsonify({"ok": ok, "status_code": response.status_code, "location": location, "reason": reason})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule approve failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/schedule/approve/<job_id>", methods=["POST"])
def api_astri_schedule_approve(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        form_data = request.form.to_dict(flat=True)
        guard = astri_work_order_guard(record, astri_schedule_match_from_form(form_data, "cluster"), "cluster")
        if guard:
            return guard
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/drm/schedule/approve",
            data=astri_public_form_data(form_data),
            allow_redirects=False,
            timeout=60,
        )
        location = response.headers.get("Location", "")
        if response.status_code in {302, 303} and re.search(r"/(login|setting|dashboard)(\?|$|/)", location, re.I):
            ok = False
            reason = "Redirect to login/setting page - session may be expired"
        else:
            ok = response.status_code in {200, 201, 302, 303}
            reason = ""
        LOGGER.warning(f"Schedule approve: status={response.status_code} location={location} ok={ok}")
        return jsonify({"ok": ok, "status_code": response.status_code, "location": location, "reason": reason})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule approve failed")
        return jsonify({"error": str(exc)}), 400


Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
Config.BOQ_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
Config.HPDB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
Config.GEOFABRIK_DIR.mkdir(parents=True, exist_ok=True)
Config.GEOFABRIK_KECAMATAN_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_memories_on_startup():
    try:
        from ai_assistant import cleanup_old_memories
        deleted = cleanup_old_memories()
        if deleted:
            print(f"[memory] Cleaned up {deleted} old memories")
    except Exception:
        pass

_cleanup_memories_on_startup()


def list_geofabrik_regions() -> list[dict]:
    """List available Geofabrik .osm.pbf files in the configured directory."""
    regions = []
    if not Config.GEOFABRIK_DIR.is_dir():
        return regions
    for path in sorted(Config.GEOFABRIK_DIR.iterdir()):
        if path.suffix == ".pbf" and path.is_file():
            size_bytes = path.stat().st_size
            cache_path = road_cache_path_for_pbf(path)
            cache_ready = road_cache_is_fresh(path, cache_path)
            regions.append({
                "name": path.stem,
                "label": path.stem.replace("-", " ").replace("_", " ").title(),
                "filename": path.name,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "road_cache_ready": cache_ready,
                "road_cache_mb": round(cache_path.stat().st_size / (1024 * 1024), 1) if cache_path.exists() else 0,
            })
    return regions



KECAMATAN_CITY_OVERRIDES = {
    # Jakarta Barat
    "Cengkareng": "Jakarta Barat", "Grogol Petamburan": "Jakarta Barat", "Kalideres": "Jakarta Barat",
    "Kebon Jeruk": "Jakarta Barat", "Kembangan": "Jakarta Barat", "Palmerah": "Jakarta Barat",
    "Taman Sari": "Jakarta Barat", "Tambora": "Jakarta Barat",
    # Jakarta Timur
    "Cakung": "Jakarta Timur", "Cipayung": "Jakarta Timur", "Ciracas": "Jakarta Timur",
    "Duren Sawit": "Jakarta Timur", "Jatinegara": "Jakarta Timur", "Kramat Jati": "Jakarta Timur",
    "Makasar": "Jakarta Timur", "Matraman": "Jakarta Timur", "Pasar Rebo": "Jakarta Timur",
    "Pulo Gadung": "Jakarta Timur", "Pulogadung": "Jakarta Timur",
    # Jakarta Utara
    "Cilincing": "Jakarta Utara", "Kelapa Gading": "Jakarta Utara", "Koja": "Jakarta Utara",
    "Pademangan": "Jakarta Utara", "Penjaringan": "Jakarta Utara", "Tanjung Priok": "Jakarta Utara",
    # Jakarta Pusat
    "Cempaka Putih": "Jakarta Pusat", "Gambir": "Jakarta Pusat", "Kemayoran": "Jakarta Pusat",
    "Menteng": "Jakarta Pusat", "Sawah Besar": "Jakarta Pusat", "Senen": "Jakarta Pusat",
    "Tanah Abang": "Jakarta Pusat",
    # Jakarta Selatan
    "Cilandak": "Jakarta Selatan", "Jagakarsa": "Jakarta Selatan", "Kebayoran Baru": "Jakarta Selatan",
    "Kebayoran Lama": "Jakarta Selatan", "Mampang Prapatan": "Jakarta Selatan", "Pancoran": "Jakarta Selatan",
    "Pasar Minggu": "Jakarta Selatan", "Pesanggrahan": "Jakarta Selatan", "Setiabudi": "Jakarta Selatan", "Tebet": "Jakarta Selatan",
    # Kota Bekasi
    "Bantargebang": "Kota Bekasi", "Bekasi Barat": "Kota Bekasi", "Bekasi Selatan": "Kota Bekasi",
    "Bekasi Timur": "Kota Bekasi", "Bekasi Utara": "Kota Bekasi", "Jatiasih": "Kota Bekasi",
    "Jatisampurna": "Kota Bekasi", "Medansatria": "Kota Bekasi", "Mustikajaya": "Kota Bekasi",
    "Pondokgede": "Kota Bekasi", "Pondokmelati": "Kota Bekasi", "Rawalumbu": "Kota Bekasi",
    # Kabupaten Bekasi
    "Babelan": "Kabupaten Bekasi", "Cibitung": "Kabupaten Bekasi", "Cikarang Barat": "Kabupaten Bekasi",
    "Cikarang Selatan": "Kabupaten Bekasi", "Cikarang Utara": "Kabupaten Bekasi", "Serang Baru": "Kabupaten Bekasi",
    "Setu": "Kabupaten Bekasi", "Sukawangi": "Kabupaten Bekasi", "Tambelang": "Kabupaten Bekasi",
    "Tambun Selatan": "Kabupaten Bekasi", "Tambun Utara": "Kabupaten Bekasi", "Tarumajaya": "Kabupaten Bekasi",
    # Depok
    "Beji": "Kota Depok", "Cimanggis": "Kota Depok", "Cinere": "Kota Depok", "Limo": "Kota Depok",
    "Pancoran Mas": "Kota Depok", "Sawangan": "Kota Depok", "Tapos": "Kota Depok",
    # Tangerang Raya
    "Batuceper": "Kota Tangerang", "Benda": "Kota Tangerang", "Cipondoh": "Kota Tangerang",
    "Pinang": "Kota Tangerang", "Tangerang": "Kota Tangerang",
    "Ciputat Timur": "Kota Tangerang Selatan", "Pamulang": "Kota Tangerang Selatan", "Pondok Aren": "Kota Tangerang Selatan",
    "Ciputat": "Kota Tangerang Selatan", "Serpong": "Kota Tangerang Selatan", "Serpong Utara": "Kota Tangerang Selatan",
    "Balaraja": "Kabupaten Tangerang", "Cikupa": "Kabupaten Tangerang", "Cisauk": "Kabupaten Tangerang",
    "Cisoka": "Kabupaten Tangerang", "Curug": "Kabupaten Tangerang", "Kelapa Dua": "Kabupaten Tangerang",
    "Kosambi": "Kabupaten Tangerang", "Legok": "Kabupaten Tangerang", "Pagedangan": "Kabupaten Tangerang",
    "Pakuhaji": "Kabupaten Tangerang", "Panongan": "Kabupaten Tangerang", "Pasar Kemis": "Kabupaten Tangerang",
    "Rajeg": "Kabupaten Tangerang", "Sepatan": "Kabupaten Tangerang", "Sepatan Timur": "Kabupaten Tangerang",
    "Sindangjaya": "Kabupaten Tangerang", "Solear": "Kabupaten Tangerang", "Teluknaga": "Kabupaten Tangerang",
    "Tigaraksa": "Kabupaten Tangerang",
    # Bogor
    "Bojong Gede": "Kabupaten Bogor", "Cibinong": "Kabupaten Bogor", "Cileungsi": "Kabupaten Bogor",
    "Citeureup": "Kabupaten Bogor", "Gunung Putri": "Kabupaten Bogor", "Gunung Sindur": "Kabupaten Bogor",
    "Jasinga": "Kabupaten Bogor", "Jonggol": "Kabupaten Bogor", "Klapanunggal": "Kabupaten Bogor",
    "Parung Panjang": "Kabupaten Bogor", "Tajurhalang": "Kabupaten Bogor",
    # Karawang
    "Karawang Barat": "Kabupaten Karawang", "Karawang Timur": "Kabupaten Karawang", "Klari": "Kabupaten Karawang",
    # Kota Bogor
    "Bogor Barat": "Kota Bogor", "Bogor Selatan": "Kota Bogor", "Bogor Tengah": "Kota Bogor",
    "Bogor Timur": "Kota Bogor", "Bogor Utara": "Kota Bogor", "Tanah Sareal": "Kota Bogor",
    # Kota Depok
    "Bojongsari": "Kota Depok", "Cilodong": "Kota Depok", "Cipayung Depok": "Kota Depok",
    "Sukmajaya": "Kota Depok",
    # Kota Tangerang
    "Cibodas": "Kota Tangerang", "Ciledug": "Kota Tangerang", "Jatiuwung": "Kota Tangerang", "Karang Tengah": "Kota Tangerang",
    "Karawaci": "Kota Tangerang", "Larangan": "Kota Tangerang", "Neglasari": "Kota Tangerang",
    "Periuk": "Kota Tangerang",
    # Kabupaten Bekasi
    "Cabangbungin": "Kabupaten Bekasi", "Cibarusah": "Kabupaten Bekasi", "Cikarang Pusat": "Kabupaten Bekasi",
    "Cikarang Timur": "Kabupaten Bekasi", "Karangbahagia": "Kabupaten Bekasi",
    "Muaragembong": "Kabupaten Bekasi", "Pebayuran": "Kabupaten Bekasi",
    "Sukakarya": "Kabupaten Bekasi", "Sukatani": "Kabupaten Bekasi",
    # Kabupaten Tangerang
    "Gunung Kaler": "Kabupaten Tangerang", "Jambe": "Kabupaten Tangerang",
    "Jayanti": "Kabupaten Tangerang", "Kemiri": "Kabupaten Tangerang",
    "Kresek": "Kabupaten Tangerang", "Kronjo": "Kabupaten Tangerang",
    "Mauk": "Kabupaten Tangerang", "Mekar Baru": "Kabupaten Tangerang",
    "Sukadiri": "Kabupaten Tangerang", "Sukajaya": "Kabupaten Tangerang",
    "Sukamulya": "Kabupaten Tangerang", "Sukaraja": "Kabupaten Tangerang",
    "Tanjungsari": "Kabupaten Tangerang", "Tenjo": "Kabupaten Tangerang",
    # Kabupaten Bogor
    "Babakan Madang": "Kabupaten Bogor", "Caringin": "Kabupaten Bogor", "Cariu": "Kabupaten Bogor",
    "Ciampea": "Kabupaten Bogor", "Cibungbulang": "Kabupaten Bogor", "Cigombong": "Kabupaten Bogor",
    "Cigudeg": "Kabupaten Bogor", "Cijeruk": "Kabupaten Bogor", "Cisarua": "Kabupaten Bogor",
    "Ciseeng": "Kabupaten Bogor", "Dramaga": "Kabupaten Bogor", "Kemang": "Kabupaten Bogor",
    "Leuwiliang": "Kabupaten Bogor", "Leuwisadeng": "Kabupaten Bogor", "Megamendung": "Kabupaten Bogor",
    "Nanggung": "Kabupaten Bogor", "Pamijahan": "Kabupaten Bogor", "Parung": "Kabupaten Bogor",
    "Rumpin": "Kabupaten Bogor", "Sukamakmur": "Kabupaten Bogor", "Tenjolaya": "Kabupaten Bogor",
    # Jakarta
    "Cipinang Besar Selatan": "Jakarta Timur", "Johar Baru": "Jakarta Pusat",
    # Kabupaten Karawang
    "Cikampek": "Kabupaten Karawang", "Cilamaya": "Kabupaten Karawang", "Cilamaya Kulon": "Kabupaten Karawang",
    "Cilamaya Wetan": "Kabupaten Karawang", "Cilebar": "Kabupaten Karawang", "Jatisari": "Kabupaten Karawang",
    "Jayakerta": "Kabupaten Karawang", "Kotabaru": "Kabupaten Karawang", "Lemahabang": "Kabupaten Karawang",
    "Majalaya": "Kabupaten Karawang", "Pakisjaya": "Kabupaten Karawang", "Pangkalan": "Kabupaten Karawang",
    "Pedes": "Kabupaten Karawang", "Rawamerta": "Kabupaten Karawang", "Rengasdengklok": "Kabupaten Karawang",
    "Talagasari": "Kabupaten Karawang", "Tegalwaru": "Kabupaten Karawang", "Telukjambe Barat": "Kabupaten Karawang",
    "Telukjambe Timur": "Kabupaten Karawang", "Tempuran": "Kabupaten Karawang", "Tirtajaya": "Kabupaten Karawang",
    "Tirtamulya": "Kabupaten Karawang",
}

PROVINCE_FOR_CITY = {
    "Jakarta Barat": "DKI Jakarta",
    "Jakarta Pusat": "DKI Jakarta",
    "Jakarta Selatan": "DKI Jakarta",
    "Jakarta Timur": "DKI Jakarta",
    "Jakarta Utara": "DKI Jakarta",
    "Kabupaten Bekasi": "Jawa Barat",
    "Kabupaten Bogor": "Jawa Barat",
    "Kabupaten Karawang": "Jawa Barat",
    "Kabupaten Tangerang": "Banten",
    "Kota Bekasi": "Jawa Barat",
    "Kota Bogor": "Jawa Barat",
    "Kota Depok": "Jawa Barat",
    "Kota Tangerang": "Banten",
    "Kota Tangerang Selatan": "Banten",
}

KECAMATAN_MANIFEST_PATH = Config.GEOFABRIK_KECAMATAN_DIR / "manifest.json"
_KECAMATAN_MANIFEST_CACHE: tuple[float, dict[str, dict]] | None = None

_PROVINCE_FOR_CITY_LOWER = {k.lower(): v for k, v in PROVINCE_FOR_CITY.items()}

def province_for_city(city: str) -> str:
    db_result = region_db.province_for_city(city)
    if db_result and db_result != "Lainnya":
        return db_result
    return PROVINCE_FOR_CITY.get(city, _PROVINCE_FOR_CITY_LOWER.get(city.lower(), "Lainnya"))

def load_kecamatan_manifest() -> dict[str, dict]:
    global _KECAMATAN_MANIFEST_CACHE
    if not KECAMATAN_MANIFEST_PATH.is_file():
        _KECAMATAN_MANIFEST_CACHE = None
        return {}
    mtime = KECAMATAN_MANIFEST_PATH.stat().st_mtime
    if _KECAMATAN_MANIFEST_CACHE and _KECAMATAN_MANIFEST_CACHE[0] == mtime:
        return _KECAMATAN_MANIFEST_CACHE[1]
    try:
        data = json.loads(KECAMATAN_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    entries = data.get("files", data) if isinstance(data, dict) else {}
    manifest = entries if isinstance(entries, dict) else {}
    _KECAMATAN_MANIFEST_CACHE = (mtime, manifest)
    return manifest

def kecamatan_manifest_entry(path: Path) -> dict:
    entry = load_kecamatan_manifest().get(path.name, {})
    return entry if isinstance(entry, dict) else {}

def kecamatan_label_from_path(path: Path) -> str:
    entry = kecamatan_manifest_entry(path)
    if entry.get("kecamatan"):
        return str(entry["kecamatan"])
    name = path.name
    if name.lower().endswith(".osm.pbf"):
        name = name[:-8]
    else:
        name = path.stem
    return name.replace("_", " ").replace("-", " ").strip().title()

def city_for_kecamatan(label: str) -> str:
    result = KECAMATAN_CITY_OVERRIDES.get(label)
    if result:
        return result
    return region_db.city_for_kecamatan(label)

def city_for_kecamatan_path(path: Path, label: str) -> str:
    entry = kecamatan_manifest_entry(path)
    if entry.get("city"):
        return str(entry["city"])
    return city_for_kecamatan(label)

def province_for_kecamatan_path(path: Path, city: str) -> str:
    return province_for_city(city)

def area_name_for_city(city: str) -> str:
    return re.sub(r"^\s*(?:kota|kabupaten|kab\.?)\s+", "", str(city or "").strip(), flags=re.IGNORECASE).strip()

def admin_defaults_for_osm_selection(kecamatan_path: Path | None, selected_city: str = "") -> dict:
    if not kecamatan_path:
        return {}
    district = kecamatan_label_from_path(kecamatan_path)
    city = (selected_city or "").strip() or city_for_kecamatan(district)
    defaults = {
        "district": district,
        "_locked_admin_fields": ["district"],
    }
    if city and city != "Lainnya":
        area_name = area_name_for_city(city)
        prov = province_for_city(city)
        defaults.update({
            "city": city,
            "regency_city": city,
            "area_name": area_name,
            "Area": area_name,
            "_locked_admin_fields": ["district", "city", "regency_city"],
        })
        if prov and prov != "Lainnya":
            defaults["province"] = prov
    return defaults

def resolve_admin_defaults_from_region_db(defaults: dict) -> dict:
    """Make region.db the final source for province/city/district/subdistrict."""
    if not defaults:
        return defaults
    city = str(defaults.get("city") or defaults.get("regency_city") or "").strip()
    district = str(defaults.get("district") or "").strip()
    subdistrict = str(defaults.get("sub_district") or defaults.get("subdistrict") or "").strip()
    resolved = region_db.resolve_admin_path(district=district, subdistrict=subdistrict, city=city)
    if not resolved:
        return defaults
    if resolved.get("province"):
        defaults["province"] = resolved["province"]
    if resolved.get("city"):
        defaults["city"] = resolved["city"]
        defaults["regency_city"] = resolved["city"]
        area_name = area_name_for_city(resolved["city"])
        defaults["area_name"] = area_name
        defaults["Area"] = area_name
    if resolved.get("district"):
        defaults["district"] = resolved["district"]
    if resolved.get("subdistrict"):
        defaults["sub_district"] = resolved["subdistrict"]
        defaults["subdistrict"] = resolved["subdistrict"]
    defaults["_locked_admin_fields"] = ["province", "city", "regency_city", "district", "sub_district", "subdistrict"]
    return defaults

def require_admin_defaults_from_region_db(defaults: dict | None) -> dict | None:
    """Resolve lookup values while preserving popup-locked administrative output values."""
    if not defaults:
        return defaults
    original_defaults = dict(defaults)
    original_locked = set(original_defaults.get("_locked_admin_fields") or [])
    resolved_defaults = resolve_admin_defaults_from_region_db(dict(defaults))
    district = str(defaults.get("district") or "").strip()
    city = str(defaults.get("city") or defaults.get("regency_city") or "").strip()
    if district and city:
        resolved = region_db.resolve_admin_path(district=district, city=city)
        if resolved:
            resolved_defaults["province"] = resolved.get("province", "")
            resolved_defaults["city"] = resolved.get("city", "")
            resolved_defaults["regency_city"] = resolved.get("city", "")
            resolved_defaults["district"] = resolved.get("district", "")
            if resolved.get("subdistrict"):
                resolved_defaults["sub_district"] = resolved["subdistrict"]
                resolved_defaults["subdistrict"] = resolved["subdistrict"]
            area_name = area_name_for_city(resolved.get("city", ""))
            resolved_defaults["area_name"] = area_name
            resolved_defaults["Area"] = area_name
            resolved_defaults["_locked_admin_fields"] = ["province", "city", "regency_city", "district", "sub_district", "subdistrict"]
        else:
            for key in ("province", "city", "regency_city", "district", "sub_district", "subdistrict"):
                resolved_defaults.pop(key, None)
    if original_locked:
        for key in original_locked:
            if key in original_defaults:
                resolved_defaults[key] = original_defaults[key]
        resolved_defaults["_locked_admin_fields"] = sorted(original_locked)
    return resolved_defaults

def iter_osm_kecamatan_files() -> list[Path]:
    files: list[Path] = []
    if Config.GEOFABRIK_DIR.is_dir():
        files.extend(path for path in Config.GEOFABRIK_DIR.iterdir() if path.is_file() and path.suffix.lower() == ".osm")
    if Config.GEOFABRIK_KECAMATAN_DIR.is_dir():
        files.extend(path for path in Config.GEOFABRIK_KECAMATAN_DIR.iterdir() if path.is_file() and path.name.lower().endswith((".osm", ".pbf", ".osm.pbf")))
    return sorted(files, key=lambda path: kecamatan_label_from_path(path))

def resolve_osm_kecamatan_path(filename: str) -> Path | None:
    clean = Path(filename).name
    for base in (Config.GEOFABRIK_KECAMATAN_DIR, Config.GEOFABRIK_DIR):
        candidate = base / clean
        if candidate.is_file() and candidate.name.lower().endswith((".osm", ".pbf", ".osm.pbf")):
            return candidate
    return None

def list_osm_kecamatan_regions() -> list[dict]:
    """List per-kecamatan OSM/PBF extracts available on the server."""
    regions = []
    for path in iter_osm_kecamatan_files():
        size_bytes = path.stat().st_size
        label = kecamatan_label_from_path(path)
        city = city_for_kecamatan_path(path, label)
        is_pbf = path.name.lower().endswith(".pbf")
        cache_path = road_cache_path_for_pbf(path) if is_pbf else None
        cache_ready = bool(cache_path and road_cache_is_fresh(path, cache_path))
        regions.append({
            "name": path.stem,
            "label": label,
            "city": city,
            "province": province_for_kecamatan_path(path, city),
            "filename": path.name,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 1),
            "road_cache_ready": cache_ready,
            "road_cache_mb": round(cache_path.stat().st_size / (1024 * 1024), 1) if cache_path and cache_path.exists() else 0,
        })
    cities = sorted({region["city"] for region in regions})
    return regions

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "assets/favicon.ico", mimetype="image/vnd.microsoft.icon")


def allowed_file(filename: str) -> bool:
    lower_name = filename.lower()
    return any(lower_name.endswith(extension) for extension in Config.ALLOWED_EXTENSIONS)

import re as _filename_re


def filename_without_extension(name: str) -> str:
    return _filename_re.sub(r"\.(kml|kmz)$", "", str(name or ""), flags=_filename_re.IGNORECASE).strip()


FILENAME_VALIDATION_RULES = {
    "Cluster": {
        "pattern": _filename_re.compile(r"^[A-Z]{2,}\d{4,}\s*-\s*(?!SF\b).+$", _filename_re.IGNORECASE),
        "sample": "BKS005380 - PERUMAHAN KIRANA CIKARANG BEKASI.kmz",
        "checklist": ["Kode area/project di depan.", "Tanda strip (-) sebagai pemisah.", "Nama project/lokasi lengkap.", "Jangan pakai prefix SF untuk cluster."],
    },
    "Subfeeder": {
        "pattern": _filename_re.compile(r"^[A-Z]{2,}\d{4,}\s*-\s*SF\s+.+$", _filename_re.IGNORECASE),
        "sample": "BKS005380 - SF PERUMAHAN KIRANA CIKARANG BEKASI.kmz",
        "checklist": ["Kode area/project di depan.", "Tanda strip (-) sebagai pemisah.", "Prefix SF setelah strip.", "Nama project/lokasi lengkap."],
    },
    "Feeder": {
        "pattern": _filename_re.compile(r"^[A-Z]{1,}\d{4,}\s*-\s*.+\b(?:MAIN\s+FEEDER|FEEDER)\b.+\b(?:OLT|SEGMENT|FO|\d+C)\b.+$", _filename_re.IGNORECASE),
        "sample": "FBKS0152 - BEKASI - MAIN FEEDER OLT_CIKARANG_UTARA_SEGMENT_3_TO_PASIRGOMBONG_FO_144C.kmz",
        "checklist": ["Kode feeder di depan.", "Mengandung FEEDER / MAIN FEEDER.", "Mengandung OLT/Segment/destination.", "Mengandung kapasitas kabel, contoh: 144C."],
    },
}


def filename_rule_for_scope(scope_type: str) -> dict:
    config_rules = (load_rules_config().get("filename_rules") or {})
    raw = config_rules.get(scope_type) or config_rules.get("Cluster") or {}
    fallback = FILENAME_VALIDATION_RULES.get(scope_type) or FILENAME_VALIDATION_RULES["Cluster"]
    pattern_text = raw.get("pattern") or fallback["pattern"].pattern
    try:
        pattern = _filename_re.compile(pattern_text, _filename_re.IGNORECASE)
    except _filename_re.error:
        pattern = fallback["pattern"]
    return {
        "pattern": pattern,
        "sample": raw.get("sample") or fallback["sample"],
        "checklist": raw.get("checklist") or fallback["checklist"],
    }


def validate_upload_filenames(scope_type: str, files: list) -> dict:
    rule = filename_rule_for_scope(scope_type)
    bad = []
    for file in files:
        name = getattr(file, "filename", "")
        if not name or not str(name).lower().endswith((".kml", ".kmz")):
            continue
        base = filename_without_extension(name)
        if not rule["pattern"].match(base):
            bad.append(name)
    return {"valid": not bad, "bad_files": bad, "rule": rule, "scope": scope_type}


def is_manual_osm_upload(filename: str) -> bool:
    lower_name = str(filename or "").lower()
    return lower_name.endswith((".osm", ".pbf", ".osm.pbf"))


def file_suffix(filename: str) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".osm.pbf"):
        return ".osm.pbf"
    return Path(lower_name).suffix

def safe_download_name(filename: str) -> str:
    source = Path(filename or "").name
    lower_source = source.lower()
    if lower_source.endswith(".osm.pbf"):
        stem = source[:-8]
    else:
        stem = Path(source).stem
    clean_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" ._")
    return f"{clean_stem or 'osm-to-dxf-convert'}.dxf"

def safe_download_stem(filename: str) -> str:
    source = Path(filename or "").name
    lower_source = source.lower()
    if lower_source.endswith(".osm.pbf"):
        stem = source[:-8]
    else:
        stem = Path(source).stem
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" ._") or "osm-to-dxf-convert"

def default_project_stem(original_filenames: list[str]) -> str:
    for filename in original_filenames:
        if filename.lower().endswith((".kmz", ".kml")):
            return safe_download_stem(filename)
    for filename in original_filenames:
        if filename:
            return safe_download_stem(filename)
    return "osm-to-dxf-convert"

def default_download_name(original_filenames: list[str]) -> str:
    for filename in original_filenames:
        if filename.lower().endswith((".kmz", ".kml")):
            return safe_download_name(filename)
    for filename in original_filenames:
        if filename:
            return safe_download_name(filename)
    return "osm-to-dxf-convert.dxf"

def standard_kmz_download_name(filename: str) -> str:
    return f"{safe_download_stem(filename)}.kmz"

def fdt_coding_database_ready() -> bool:
    try:
        return fdt_coding_db.count_records(Config.FDT_CODING_DB_PATH) > 0
    except Exception:
        return False

def fdt_coding_source_label() -> str:
    if fdt_coding_database_ready():
        return f"Database ({Config.FDT_CODING_DB_PATH.name})"
    if Config.FDT_CODING_SPREADSHEET_URL:
        return "Spreadsheet link"
    return Config.FDT_CODING_SPREADSHEET_PATH.name

def fdt_coding_runtime_warning(detail: str | None = None) -> str:
    message = (
        "Source coding FDT belum siap; output standardization akan memakai default XXX placeholders. "
        "Lengkapi database FDT coding atau set FDT_CODING_SPREADSHEET_URL/FDT_CODING_SPREADSHEET_PATH."
    )
    if detail:
        return f"{message} Detail: {detail}"
    return message

def latest_cached_fdt_coding_workbook() -> Path | None:
    candidates: list[Path] = []
    if Config.FDT_CODING_SPREADSHEET_PATH.exists():
        candidates.append(Config.FDT_CODING_SPREADSHEET_PATH)
    candidates.extend(
        sorted(
            Config.UPLOAD_DIR.glob("*_fdt_coding_source.xlsx"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    for path in candidates:
        try:
            if path.exists() and path.stat().st_size > 512:
                return path
        except OSError:
            continue
    return None

def fdt_coding_source_ready() -> bool:
    return bool(fdt_coding_database_ready() or Config.FDT_CODING_SPREADSHEET_URL or latest_cached_fdt_coding_workbook())

def resolve_fdt_coding_records(logger=None):
    try:
        records = fdt_coding_db.all_records(Config.FDT_CODING_DB_PATH)
    except Exception as exc:
        if logger:
            logger.warn(f"Database coding FDT gagal dibaca; fallback spreadsheet. Detail: {exc}")
        return None
    if records:
        if logger:
            logger.info(f"Using FDT coding database: {len(records):,} record(s).")
        return records
    return None

def resolve_fdt_coding_workbook(job_id: str, logger=None) -> Path:
    if Config.FDT_CODING_SPREADSHEET_URL:
        cache_path = Config.UPLOAD_DIR / f"{job_id}_fdt_coding_source.xlsx"
        if logger:
            logger.info("Downloading FDT coding spreadsheet from configured link.")
        try:
            return download_coding_workbook(Config.FDT_CODING_SPREADSHEET_URL, cache_path)
        except Exception as exc:
            fallback_path = latest_cached_fdt_coding_workbook()
            if fallback_path:
                if logger:
                    logger.warn(
                        "Download spreadsheet coding FDT gagal; memakai cache lokal "
                        f"{fallback_path.name}. Detail: {exc}"
                    )
                return fallback_path
            raise

    fallback_path = latest_cached_fdt_coding_workbook()
    if fallback_path:
        if logger:
            logger.info(f"Using FDT coding spreadsheet: {fallback_path.name}.")
        return fallback_path

    raise FileNotFoundError(
        "FDT coding spreadsheet belum dikonfigurasi. Set FDT_CODING_SPREADSHEET_URL "
        "atau FDT_CODING_SPREADSHEET_PATH di server."
    )

def output_targets_for_mode(output_mode: str) -> set[str]:
    return set(output_mode.split("_")) if output_mode in OUTPUT_MODES else {DEFAULT_OUTPUT_MODE}

def normalize_network_type(network_type: str | None) -> str:
    normalized = str(network_type or Config.DEFAULT_NETWORK_TYPE).strip().upper()
    if normalized == "UNDERGROUND":
        return "UG"
    if normalized == "AERIAL":
        return "AE"
    return normalized if normalized in NETWORK_TYPES else Config.DEFAULT_NETWORK_TYPE

def network_type_label(network_type: str | None) -> str:
    return NETWORK_TYPE_LABELS.get(normalize_network_type(network_type), NETWORK_TYPE_LABELS[Config.DEFAULT_NETWORK_TYPE])

def variant_path(paths_by_network: dict[str, Path], network_type: str | None) -> Path:
    normalized = normalize_network_type(network_type)
    return paths_by_network.get(normalized) or paths_by_network[Config.DEFAULT_NETWORK_TYPE]

def boq_template_path_for_network(network_type: str | None) -> Path:
    return variant_path(Config.BOQ_TEMPLATE_PATHS, network_type)

def hpdb_template_path_for_network(network_type: str | None) -> Path:
    return variant_path(Config.HPDB_TEMPLATE_PATHS, network_type)

def hpdb_defaults_path_for_network(network_type: str | None) -> Path:
    return variant_path(Config.HPDB_DEFAULTS_PATHS, network_type)

def dxf_template_path_for_network(network_type: str | None) -> Path:
    return variant_path(Config.DXF_TEMPLATE_PATHS, network_type)

def normalize_scope_type(scope_type: str | None) -> str:
    raw = str(scope_type or Config.DEFAULT_SCOPE_TYPE).strip().lower()
    aliases = {
        "cluster": SCOPE_TYPE_CLUSTER,
        "feeder": SCOPE_TYPE_FEEDER,
        "subfeeder": SCOPE_TYPE_SUBFEEDER,
        "sub feeder": SCOPE_TYPE_SUBFEEDER,
        "sub-feeder": SCOPE_TYPE_SUBFEEDER,
    }
    return aliases.get(raw, Config.DEFAULT_SCOPE_TYPE)


def is_feeder_like_scope(scope_type: str | None) -> bool:
    return normalize_scope_type(scope_type) in FEEDER_LIKE_SCOPE_TYPES

def boq_template_path_for_scope_network(scope_type: str | None, network_type: str | None) -> Path:
    scope = normalize_scope_type(scope_type)
    paths = Config.BOQ_TEMPLATE_PATHS_BY_SCOPE.get(scope, Config.BOQ_TEMPLATE_PATHS)
    return variant_path(paths, network_type)

def dxf_template_path_for_scope_network(scope_type: str | None, network_type: str | None) -> Path:
    scope = normalize_scope_type(scope_type)
    paths = Config.DXF_TEMPLATE_PATHS_BY_SCOPE.get(scope, Config.DXF_TEMPLATE_PATHS)
    return variant_path(paths, network_type)

def template_ready_by_network(paths_by_network: dict[str, Path]) -> dict[str, bool]:
    return {network_type: path.exists() for network_type, path in paths_by_network.items()}

def template_name_by_network(paths_by_network: dict[str, Path]) -> dict[str, str]:
    return {network_type: path.name for network_type, path in paths_by_network.items()}

def mode_has_dxf(output_mode: str) -> bool:
    return OUTPUT_MODE_DXF in output_targets_for_mode(output_mode)

def mode_has_boq(output_mode: str) -> bool:
    return OUTPUT_MODE_BOQ in output_targets_for_mode(output_mode)

def mode_has_astri(output_mode: str) -> bool:
    return OUTPUT_MODE_ASTRI in output_targets_for_mode(output_mode)

def mode_has_hpdb(output_mode: str) -> bool:
    return OUTPUT_MODE_HPDB in output_targets_for_mode(output_mode)

def mode_has_kmz(output_mode: str) -> bool:
    return OUTPUT_MODE_KMZ in output_targets_for_mode(output_mode)

def is_multi_output_mode(output_mode: str) -> bool:
    return len(output_targets_for_mode(output_mode)) > 1

def human_join_labels(labels: list[str]) -> str:
    clean_labels = [label for label in labels if label]
    if len(clean_labels) <= 1:
        return clean_labels[0] if clean_labels else "Output"
    if len(clean_labels) == 2:
        return " dan ".join(clean_labels)
    return f"{', '.join(clean_labels[:-1])}, dan {clean_labels[-1]}"

def download_names_for_mode(original_filenames: list[str], output_mode: str, network_type: str | None = None) -> dict[str, str]:
    stem = default_project_stem(original_filenames)
    targets = output_targets_for_mode(output_mode)
    variant = normalize_network_type(network_type)
    return {
        "dxf": f"{stem}_{variant}.dxf",
        "boq": f"BOQ - {stem}_{variant}.xlsx",
        "astri": f"ASTRI - {stem}_{variant}.xlsx",
        "hpdb": f"HPDB - {stem}_{variant}.xlsx",
        "kmz": standard_kmz_download_name(stem),
        "zip": f"{stem}_OUTPUT_{variant}.zip",
        "final": (
            f"{stem}_{variant}.dxf"
            if targets == {OUTPUT_MODE_DXF}
            else f"BOQ - {stem}_{variant}.xlsx"
            if targets == {OUTPUT_MODE_BOQ}
            else f"ASTRI - {stem}_{variant}.xlsx"
            if targets == {OUTPUT_MODE_ASTRI}
            else f"HPDB - {stem}_{variant}.xlsx"
            if targets == {OUTPUT_MODE_HPDB}
            else standard_kmz_download_name(stem)
            if targets == {OUTPUT_MODE_KMZ}
            else f"{stem}_OUTPUT_{variant}.zip"
        ),
    }

def download_mimetype_for_mode(output_mode: str) -> str:
    targets = output_targets_for_mode(output_mode)
    if len(targets) > 1:
        return "application/zip"
    if targets in ({OUTPUT_MODE_BOQ}, {OUTPUT_MODE_ASTRI}, {OUTPUT_MODE_HPDB}):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if targets == {OUTPUT_MODE_KMZ}:
        return "application/vnd.google-earth.kmz"
    return "application/dxf"


def astri_output_files(job: dict, result: dict | None) -> list[dict]:
    files = list((result or {}).get("output_files") or [])
    if not files and job.get("astri_output_path") and job.get("astri_download_name"):
        files = [{"path": job["astri_output_path"], "download_name": job["astri_download_name"], "fdt_number": None}]
    names = [item.get("download_name") for item in files]
    if not files or len(names) != len(set(names)):
        raise ValueError("Output ASTRI kosong atau duplikat.")
    for item in files:
        path = Path(str(item.get("path") or ""))
        name = str(item.get("download_name") or "")
        if not is_safe_output_path(path) or not path.is_file():
            raise ValueError("File ASTRI tidak valid atau tidak tersedia.")
        if not name or Path(name).name != name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."}:
            raise ValueError("Nama file ASTRI tidak aman.")
    return files


def prepare_astri_job_artifact(job: dict, output_mode: str, result: dict) -> dict:
    files = astri_output_files(job, result)
    base_path = Path(job.get("astri_xlsx_base_path") or job["astri_output_path"]).with_suffix(".xlsx")
    base_name = Path(job.get("astri_xlsx_download_name") or job["astri_download_name"]).with_suffix(".xlsx").name
    if not is_safe_output_path(base_path):
        raise ValueError("Path output ASTRI tidak aman.")
    updates = {"astri_output_files": files, "astri_xlsx_base_path": str(base_path),
               "astri_xlsx_download_name": base_name}
    if len(files) == 1:
        updates.update(astri_output_path=files[0]["path"], astri_download_name=files[0]["download_name"])
        if output_targets_for_mode(output_mode) == {OUTPUT_MODE_ASTRI}:
            updates.update(output_path=files[0]["path"], download_name=files[0]["download_name"],
                           download_mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    elif output_targets_for_mode(output_mode) == {OUTPUT_MODE_ASTRI}:
        path = base_path.with_suffix(".zip")
        write_astri_output_zip(files, path,
                               (Config.OUTPUT_DIR, Config.BOQ_OUTPUT_DIR, Config.HPDB_OUTPUT_DIR))
        name = Path(base_name).with_suffix(".zip").name
        updates.update(output_path=str(path), download_name=name, download_mimetype="application/zip",
                       astri_output_path=str(path), astri_download_name=name)
    return updates


def cleanup_saved_uploads(paths) -> None:
    for saved_path in paths:
        try:
            Path(saved_path).unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("Gagal menghapus file upload sementara %s: %s", saved_path, exc)

def format_bytes(byte_count: int | float) -> str:
    size = float(byte_count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} GB"

def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, second = divmod(total_seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"

def build_completion_summary(result: dict, duration_seconds: float, counts: dict) -> str:
    warnings = int(counts.get("WARNING", 0))
    errors = int(counts.get("ERROR", 0))
    return "\n".join(
        [
            "=================================",
            "CONVERSION COMPLETE",
            f"Roads: {int(result.get('roads', 0)):,}",
            f"OSM Nodes: {int(result.get('processed_nodes', 0)):,}",
            f"KML Layers: {int(result.get('kml_layers', 0)):,}",
            f"DXF Layers: {int(result.get('dxf_layers', 0)):,}",
            f"Warnings: {warnings:,}",
            f"Errors: {errors:,}",
            f"Duration: {format_duration(duration_seconds)}",
            "=================================",
        ]
    )


def build_boq_completion_summary(result: dict, duration_seconds: float, counts: dict) -> str:
    summary = result.get("summary", {})
    warnings = int(counts.get("WARNING", 0))
    errors = int(counts.get("ERROR", 0))
    return "\n".join(
        [
            "=================================",
            "BOQ EXPORT COMPLETE",
            f"Mapped BOQ Items: {int(summary.get('total_mapped_boq_item', 0)):,}",
            f"Warning/Unmapped: {int(summary.get('total_warning_unmapped', 0)):,}",
            f"Distribution Cable: {int(summary.get('total_distribution_cable_length', 0)):,} m",
            f"Sling Wire: {int(summary.get('total_sling_wire_length', 0)):,} m",
            f"FDT Detected: {int(summary.get('total_fdt_detected', 0)):,}",
            f"FAT Detected: {int(summary.get('total_fat_detected', 0)):,}",
            f"Pole Detected: {int(summary.get('total_pole_detected', 0)):,}",
            f"Warnings: {warnings:,}",
            f"Errors: {errors:,}",
            f"Duration: {format_duration(duration_seconds)}",
            "=================================",
        ]
    )

def build_hpdb_completion_summary(result: dict, duration_seconds: float, counts: dict, title: str = "HPDB EXPORT COMPLETE") -> str:
    summary = result.get("summary", {})
    warnings = int(counts.get("WARNING", 0))
    errors = int(counts.get("ERROR", 0))
    return "\n".join(
        [
            "=================================",
            title,
            f"FDT Detected: {int(summary.get('total_fdt_detected', 0)):,}",
            f"CLUSTER Rows: {int(summary.get('total_cluster_rows', 0)):,}",
            f"FAT Rows: {int(summary.get('total_fat_rows', 0)):,}",
            f"HOMEPASS Rows: {int(summary.get('total_homepass_rows', 0)):,}",
            f"HP COVER Exported: {int(summary.get('total_hp_cover_exported', 0)):,}",
            f"HP UNCOVER Ignored: {int(summary.get('total_hp_uncover_ignored', 0)):,}",
            f"HPDB Warnings: {int(summary.get('total_hpdb_warnings', 0)):,}",
            f"Warnings: {warnings:,}",
            f"Errors: {errors:,}",
            f"Duration: {format_duration(duration_seconds)}",
            "=================================",
        ]
    )

def merge_boq_stats(stats: dict | None, boq_result: dict | None = None) -> dict:
    merged = dict(stats or {})
    summary = (boq_result or {}).get("summary", {})
    for stats_key, summary_key in BOQ_STATS_KEYS.items():
        merged[stats_key] = int(summary.get(summary_key, 0) or 0)
    return merged

def merge_hpdb_stats(stats: dict | None, hpdb_result: dict | None = None) -> dict:
    merged = dict(stats or {})
    summary = (hpdb_result or {}).get("summary", {})
    for stats_key, summary_key in HPDB_STATS_KEYS.items():
        merged[stats_key] = int(summary.get(summary_key, 0) or 0)
    return merged

def merge_standardizer_stats(stats: dict | None, summary: dict | None = None) -> dict:
    merged = dict(stats or {})
    summary = summary or {}
    merged["FDT"] = max(int(merged.get("FDT", 0) or 0), int(summary.get("fdt_placemarks", 0) or 0))
    merged["NAMES"] = int(summary.get("name_standardized", 0) or 0)
    merged["ORGANIZED"] = int(summary.get("placemark_organized", 0) or 0)
    merged["STYLES"] = int(summary.get("style_standardized", 0) or 0)
    return merged

def is_kml_kmz_path(path: str | Path) -> bool:
    return str(path).lower().endswith((".kml", ".kmz"))

def aggregate_validation_reports(reports: list[dict]) -> dict:
    if not reports:
        return {}
    errors = [issue for report in reports for issue in report.get("errors", [])]
    warnings = [issue for report in reports for issue in report.get("warnings", [])]
    checks: list[dict] = []
    for report in reports:
        checks.extend(report.get("checks", []))
    issue_by_code: Counter = Counter()
    issue_by_folder: Counter = Counter()
    for report in reports:
        issue_by_code.update(report.get("issue_by_code", {}))
        issue_by_folder.update(report.get("issue_by_folder", {}))
    summaries = [report.get("summary", {}) for report in reports]
    summary = {
        key: sum(int(item.get(key, 0) or 0) for item in summaries)
        for key in ("placemarks", "styled_placemarks", "required_folders", "missing_folders", "present_required_folders", "nonempty_required_folders", "empty_required_folders", "empty_required_folder_instances", "duplicate_points", "duplicate_lines", "folder_count", "cable_count", "cable_bad_format")
    }
    first = reports[0]
    files = []
    for report in reports:
        file_errors = list(report.get("errors", []))
        file_warnings = list(report.get("warnings", []))
        files.append({
            "source_name": report.get("source_name", ""),
            "scope_type": report.get("scope_type", "Cluster"),
            "network_type": report.get("network_type", "AE"),
            "status": "error" if file_errors else "warning" if file_warnings else "valid",
            "valid": not file_errors,
            "error_count": len(file_errors),
            "warning_count": len(file_warnings),
            "errors": file_errors[:80],
            "warnings": file_warnings[:80],
            "checks": list(report.get("checks", []))[:80],
            "summary": dict(report.get("summary", {})),
        })
    return {
        "scope_type": first.get("scope_type", "Cluster"),
        "network_type": first.get("network_type", "AE"),
        "status": "error" if errors else "warning" if warnings else "valid",
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:80],
        "warnings": warnings[:80],
        "checks": checks[:80],
        "issue_by_code": dict(issue_by_code),
        "issue_by_folder": dict(issue_by_folder),
        "summary": summary,
        "files": files,
    }


def aggregate_standardizer_results(results: list[dict]) -> dict:
    if not results:
        return {}
    if len(results) == 1:
        return dict(results[0])

    warnings: list[str] = []
    fdt_changes: list[dict] = []
    folder_changes: list[dict] = []
    name_changes: list[dict] = []
    organizer_changes: list[dict] = []
    style_changes: list[dict] = []
    files: list[dict] = []
    validation_reports = [result.get("validation_report") for result in results if isinstance(result.get("validation_report"), dict)]
    for result in results:
        source_name = result.get("source_name", "")
        files.append(
            {
                "source_name": source_name,
                "output_path": result.get("output_path", ""),
                "matched_site": result.get("matched_site", ""),
                "matched_sheet": result.get("matched_sheet", ""),
                "matched_row": result.get("matched_row"),
                "default_fdt_coding": bool(result.get("default_fdt_coding")),
                "available_fdt_code_count": int(result.get("available_fdt_code_count", 0) or 0),
                "fdt_renamed": int(result.get("fdt_renamed", 0) or 0),
                "name_standardized": int(result.get("name_standardized", 0) or 0),
                "folder_renamed": int(result.get("folder_renamed", 0) or 0),
                "folder_unwrapped": int(result.get("folder_unwrapped", 0) or 0),
                "placemark_organized": int(result.get("placemark_organized", 0) or 0),
                "distribution_cable_standardized": int(result.get("distribution_cable_standardized", 0) or 0),
                "style_standardized": int(result.get("style_standardized", 0) or 0),
            }
        )
        warnings.extend(
            f"{source_name}: {warning}" if source_name else str(warning)
            for warning in result.get("warnings", [])
        )
        fdt_changes.extend(result.get("fdt_changes", []))
        folder_changes.extend(result.get("folder_changes", []))
        name_changes.extend(result.get("name_changes", []))
        organizer_changes.extend(result.get("organizer_changes", []))
        style_changes.extend(result.get("style_changes", []))

    first = results[0]
    return {
        "output_path": "",
        "project_candidates": [],
        "source_name": first.get("source_name", ""),
        "matched_project": first.get("matched_project", ""),
        "match_score": first.get("match_score", 0),
        "matched_site": first.get("matched_site", ""),
        "matched_sheet": first.get("matched_sheet", ""),
        "matched_row": first.get("matched_row"),
        "available_fdt_codes": first.get("available_fdt_codes", []),
        "available_fdt_code_count": sum(int(result.get("available_fdt_code_count", 0) or 0) for result in results),
        "fdt_placemarks": sum(int(result.get("fdt_placemarks", 0) or 0) for result in results),
        "fdt_renamed": sum(int(result.get("fdt_renamed", 0) or 0) for result in results),
        "folder_renamed": sum(int(result.get("folder_renamed", 0) or 0) for result in results),
        "folder_unwrapped": sum(int(result.get("folder_unwrapped", 0) or 0) for result in results),
        "name_standardized": sum(int(result.get("name_standardized", 0) or 0) for result in results),
        "placemark_organized": sum(int(result.get("placemark_organized", 0) or 0) for result in results),
        "distribution_cable_standardized": sum(int(result.get("distribution_cable_standardized", 0) or 0) for result in results),
        "style_standardized": sum(int(result.get("style_standardized", 0) or 0) for result in results),
        "fdt_changes": fdt_changes,
        "folder_changes": folder_changes,
        "name_changes": name_changes,
        "organizer_changes": organizer_changes,
        "style_changes": style_changes,
        "warnings": warnings,
        "files": files,
        "validation_report": aggregate_validation_reports(validation_reports),
    }

def boq_nro_metadata_from_standardizer(summary: dict | None) -> dict[str, str]:
    summary = summary or {}
    files = summary.get("files") or []
    first_file = files[0] if files and isinstance(files[0], dict) else {}
    source_name = str(summary.get("source_name") or first_file.get("source_name") or "").strip()
    matched_sheet = str(summary.get("matched_sheet") or first_file.get("matched_sheet") or "").strip()
    metadata = {"cluster_name": "", "olt_name": matched_sheet}
    if source_name:
        metadata["cluster_name"] = extract_cluster_name_from_kmz_filename(source_name)
    return metadata

def fdt_coding_count_from_standardizer(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    files = summary.get("files")
    if isinstance(files, list) and files:
        return sum(
            int(file.get("available_fdt_code_count", 0) or 0)
            for file in files
            if isinstance(file, dict) and not file.get("default_fdt_coding")
        )
    if summary.get("default_fdt_coding"):
        return 0
    count = summary.get("available_fdt_code_count")
    if count is not None:
        try:
            return max(int(count or 0), 0)
        except (TypeError, ValueError):
            return 0
    codes = summary.get("available_fdt_codes")
    return len(codes) if isinstance(codes, list) else 0


def hpdb_olt_defaults_from_standardizer(summary: dict | None) -> dict[str, str]:
    if not isinstance(summary, dict):
        return {}
    records = summary.get("available_fdt_codes")
    if not isinstance(records, list):
        return {}

    # Pick first real Coding FDT value, not blindly records[0]. Some jobs may
    # prepend DEFAULT/fallback rows with empty OLT fields; HPDB/ASTRI must still
    # get olthostname from later matched Coding FDT rows.
    olt_code = ""
    olt_hostname = ""
    for record in records:
        if not isinstance(record, dict):
            continue
        if not olt_code:
            olt_code = fdt_coding_db.normalize_text(record.get("hub_id")).upper()
        if not olt_hostname:
            olt_hostname = fdt_coding_db.normalize_text(record.get("olt_hostname")).upper()
        if olt_code and olt_hostname:
            break

    default_record = summary.get("default_fdt_coding")
    if isinstance(default_record, dict):
        if not olt_code:
            olt_code = fdt_coding_db.normalize_text(default_record.get("hub_id")).upper()
        if not olt_hostname:
            olt_hostname = fdt_coding_db.normalize_text(default_record.get("olt_hostname")).upper()

    if olt_code and not olt_hostname:
        # Backfill OLT hostname from Coding FDT DB by olt_code/hub_id. Some
        # standardizer summaries carry hub_id but not olt_hostname, while
        # fdt_coding_olts / fdt_coding_olt_catalog already have hostname.
        try:
            import sqlite3 as _sqlite3
            if Config.FDT_CODING_DB_PATH.exists():
                with _sqlite3.connect(Config.FDT_CODING_DB_PATH) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT olt_hostname FROM fdt_coding_olts
                        WHERE hub_id = ? AND IFNULL(olt_hostname, '') != ''
                        LIMIT 1
                        """,
                        (olt_code,),
                    )
                    row = cur.fetchone()
                    if not row:
                        cur.execute(
                            """
                            SELECT olt_hostname FROM fdt_coding_olt_catalog
                            WHERE olt_code = ? AND IFNULL(olt_hostname, '') != ''
                            LIMIT 1
                            """,
                            (olt_code,),
                        )
                        row = cur.fetchone()
                    if row and row[0]:
                        olt_hostname = fdt_coding_db.normalize_text(row[0]).upper()
        except Exception:
            LOGGER.exception("Failed to backfill olthostname from Coding FDT DB for olt_code=%s", olt_code)

    defaults: dict[str, str] = {}
    if olt_code:
        defaults["olt_code"] = olt_code
    if olt_hostname:
        defaults["olthostname"] = olt_hostname
    return defaults

def standardize_kml_inputs_for_job(
    job_id: str,
    input_paths: list[str | Path],
    output_mode: str,
    network_type: str,
    pipeline_logger,
    scope_type: str = "Cluster",
) -> tuple[list[Path], list[dict], dict]:
    paths = [Path(path) for path in input_paths]
    kml_indices = [index for index, path in enumerate(paths) if is_kml_kmz_path(path)]
    if not kml_indices:
        return paths, [], {}

    # Standardization is mandatory regardless of output_mode. The user's
    # output selection only controls whether the standardized KMZ is saved
    # as a downloadable artifact, not whether standardization happens.
    if not fdt_coding_source_ready():
        pipeline_logger.warn(fdt_coding_runtime_warning())
        coding_path = None
        coding_records = None
    else:
        pipeline_logger.info(f"KMZ auto-standardizer source: {fdt_coding_source_label()}.")
        coding_records = resolve_fdt_coding_records(pipeline_logger)
        try:
            coding_path = None if coding_records else resolve_fdt_coding_workbook(job_id, pipeline_logger)
        except Exception as exc:
            pipeline_logger.warn(fdt_coding_runtime_warning(str(exc)))
            coding_path = None

    update_job(job_id, progress=12, message="Menstandarkan KML/KMZ dari coding FDT.")

    targets = output_targets_for_mode(output_mode)
    single_kmz_output = targets == {OUTPUT_MODE_KMZ} and len(kml_indices) == 1
    job = get_job(job_id) or {}
    original_filenames = job.get("original_filenames") or []
    standardized_paths = list(paths)
    standard_kmz_outputs: list[dict] = []
    results: list[dict] = []

    for output_index, input_index in enumerate(kml_indices, start=1):
        source_path = paths[input_index]
        source_name = original_filenames[input_index] if input_index < len(original_filenames) else source_path.name
        output_path = (
            Path(job["output_path"])
            if single_kmz_output
            else (
                Config.OUTPUT_DIR / f"{job_id}_STANDARDIZED_{output_index}.kmz"
                if mode_has_kmz(output_mode)
                else Config.UPLOAD_DIR / f"{job_id}_standardized_{output_index}.kmz"
            )
        )
        # Mandatory: any standardization failure aborts the whole job.
        try:
            result = standardize_kmz_file(source_path, coding_path, output_path, network_type=network_type, scope_type=scope_type, coding_records=coding_records, source_name=source_name)
        except Exception as exc:
            raise RuntimeError(f"KMZ standardization gagal untuk {source_path.name}: {exc}") from exc

        result["source_name"] = source_name
        if isinstance(result.get("validation_report"), dict):
            result["validation_report"]["source_name"] = source_name
        results.append(result)
        standardized_paths[input_index] = output_path
        if mode_has_kmz(output_mode):
            standard_kmz_outputs.append(
                {
                    "path": str(output_path),
                    "download_name": standard_kmz_download_name(source_name),
                    "source_name": source_name,
                }
            )
        pipeline_logger.success(
            f"KMZ standardized: {source_name} "
            f"({int(result.get('fdt_renamed', 0))} FDT, "
            f"{int(result.get('name_standardized', 0))} object names, "
            f"{int(result.get('placemark_organized', 0))} objects moved, "
            f"{int(result.get('distribution_cable_standardized', 0))} distribution cables, "
            f"{int(result.get('folder_renamed', 0))} folders, "
            f"{int(result.get('folder_unwrapped', 0))} wrapper folders, "
            f"{int(result.get('style_standardized', 0))} styles)."
        )
        for warning in result.get("warnings", []):
            pipeline_logger.warn(f"{source_name}: {warning}")

    summary = aggregate_standardizer_results(results)
    update_job(
        job_id,
        input_paths=[str(path) for path in standardized_paths],
        input_path=str(standardized_paths[0]) if standardized_paths else None,
        standard_kmz_outputs=standard_kmz_outputs,
        standardizer_summary=summary,
        stats=merge_standardizer_stats((get_job(job_id) or {}).get("stats", {}), summary),
    )
    return standardized_paths, standard_kmz_outputs, summary

def parse_selected_layers() -> list[str]:
    return DEFAULT_CONVERT_LAYERS.copy()


def parse_road_clip_boundary() -> bool:
    return request.form.get("road_clip_boundary_enabled", "1").strip() != "0"


def parse_output_mode() -> str:
    output_mode = request.form.get("output_mode", DEFAULT_OUTPUT_MODE).strip()
    if output_mode not in OUTPUT_MODES:
        raise ValueError("Output mode tidak valid.")
    if not Config.DXF_CONVERSION_ENABLED and mode_has_dxf(output_mode):
        raise ValueError("Fitur DXF sementara dinonaktifkan di live server.")
    return output_mode

def parse_network_type() -> str:
    network_type = str(request.form.get("network_type", Config.DEFAULT_NETWORK_TYPE)).strip().upper()
    if network_type not in NETWORK_TYPES:
        raise ValueError("Network type tidak valid.")
    return network_type

def parse_scope_type() -> str:
    return normalize_scope_type(request.form.get("scope_type", Config.DEFAULT_SCOPE_TYPE))

def parse_selected_kml_folders(scope_type: str | None = None) -> list[str] | None:
    # Folder selection is intentionally hidden and automatic. More
    # importantly, raw source folder names are normalized before DXF export;
    # carrying a raw filter (for example only FAT/CABLE) into the standardized
    # KMZ silently drops BOUNDARY FAT, poles, HP and Sling Wire. None means
    # parse every eligible standardized folder.
    return None


def job_status_payload(job: dict) -> dict:
    download_url = None
    if job["status"] == "done":
        download_url = url_for("download_file", job_id=job["job_id"])
    network_type = normalize_network_type(job.get("network_type"))
    scope_type = normalize_scope_type(job.get("scope_type"))
    boq_template_path = boq_template_path_for_scope_network(scope_type, network_type)
    hpdb_template_path = hpdb_template_path_for_network(network_type)

    return {
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "stats": job["stats"],
        "selected_layers": job.get("selected_layers", []),
        "selected_kml_folders": job.get("selected_kml_folders", []),
        "road_clip_boundary": job.get("road_clip_boundary", True),
        "output_mode": job.get("output_mode", OUTPUT_MODE_DXF),
        "network_type": network_type,
        "network_type_label": network_type_label(network_type),
        "scope_type": scope_type,
        "logs": job.get("logs", []),
        "log_seq": job.get("log_seq", 0),
        "log_counts": job.get("log_counts", {"WARNING": 0, "ERROR": 0}),
        "retry_count": int(job.get("retry_count", 0) or 0),
        "max_retries": int(job.get("max_retries", Config.JOB_MAX_RETRIES) or Config.JOB_MAX_RETRIES),
        "last_retry_at": job.get("last_retry_at"),
        "retry_errors": job.get("retry_errors", []),
        "dead_letter": bool(job.get("dead_letter", False)),
        "file_count": len(job.get("input_paths", [])),
        "download_url": download_url,
        "download_name": job.get("download_name", f"osm-to-dxf-merged-{job['job_id']}.dxf"),
        "download_mimetype": job.get("download_mimetype", "application/dxf"),
        "boq_template_ready": boq_template_path.exists(),
        "boq_template_name": boq_template_path.name,
        "boq_sheet_name": BOQ_SHEET_NAME,
        "boq_output_name": job.get("boq_download_name"),
        "boq_summary": job.get("boq_summary", {}),
        "boq_preview": job.get("boq_preview", []),
        "boq_nro_sheet_name": BOQ_NRO_SHEET_NAME,
        "boq_nro_preview": job.get("boq_nro_preview", {"columns": [], "rows": []}),
        "boq_warnings": job.get("boq_warnings", []),
        "astri_template_ready": hpdb_template_path.exists(),
        "astri_template_name": hpdb_template_path.name,
        "astri_output_name": job.get("astri_download_name"),
        "astri_output_files": [
            {"fdt_number": item.get("fdt_number"), "download_name": item.get("download_name")}
            for item in job.get("astri_output_files", []) if isinstance(item, dict) and item.get("download_name")
        ],
        "hpdb_template_ready": hpdb_template_path.exists(),
        "hpdb_template_name": hpdb_template_path.name,
        "hpdb_sheet_names": [HPDB_CLUSTER_SHEET_NAME, HPDB_FAT_SHEET_NAME, HPDB_HOMEPASS_SHEET_NAME],
        "hpdb_max_fdt": HPDB_MAX_FDT,
        "hpdb_output_name": job.get("hpdb_download_name"),
        "hpdb_summary": job.get("hpdb_summary", {}),
        "hpdb_preview": job.get("hpdb_preview", {"cluster": [], "fat": [], "homepass": [], "limit": None, "totals": {}}),
        "hpdb_warnings": job.get("hpdb_warnings", []),
        "hpdb_issues": job.get("hpdb_issues", []),
        "standardizer_summary": job.get("standardizer_summary", {}),
        "error": job.get("error"),
    }


ACTIVITY_OUTPUT_LABELS = {
    OUTPUT_MODE_DXF: "DXF CAD",
    OUTPUT_MODE_BOQ: "BOQ Excel",
    OUTPUT_MODE_ASTRI: "ASTRI Excel",
    OUTPUT_MODE_HPDB: "HPDB Excel",
    OUTPUT_MODE_KMZ: "KMZ Standard",
}


def activity_output_label(output_mode: str | None) -> str:
    targets = output_targets_for_mode(output_mode or DEFAULT_OUTPUT_MODE)
    labels = [ACTIVITY_OUTPUT_LABELS.get(target, target.upper()) for target in OUTPUT_TARGET_ORDER if target in targets]
    return human_join_labels(labels)


def activity_float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compact_standardizer_summary(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return {}
    scalar_keys = [
        "matched_site",
        "matched_sheet",
        "matched_row",
        "matched_project",
        "match_score",
        "default_fdt_coding",
        "fdt_placemarks",
        "fdt_renamed",
        "name_standardized",
        "placemark_organized",
        "distribution_cable_standardized",
        "folder_renamed",
        "folder_unwrapped",
        "style_standardized",
        "network_type",
        "source_name",
        "available_fdt_code_count",
        "scope_type",
        "validation_report",
    ]
    compact = {key: summary.get(key) for key in scalar_keys if key in summary}
    report = summary.get("validation_report")
    if isinstance(report, dict):
        compact["validation_report"] = {
            key: report.get(key)
            for key in ("scope_type", "network_type", "status", "valid", "error_count", "warning_count", "errors", "warnings", "checks", "issue_by_code", "issue_by_folder", "summary", "files")
            if key in report
        }
    for key in ("warnings", "fdt_changes", "folder_changes", "name_changes", "distribution_cable_changes", "organizer_changes"):
        values = summary.get(key)
        if isinstance(values, list):
            compact[key] = values[:12]
            compact[f"{key}_total"] = len(values)
    return compact


def job_duration_seconds(job: dict) -> float | None:
    explicit = activity_float(job.get("duration_seconds"))
    if explicit is not None:
        return explicit
    started = activity_float(job.get("started_at"))
    finished = activity_float(job.get("finished_at"))
    if started is not None and finished is not None:
        return max(0.0, finished - started)
    if job.get("status") in TERMINAL_JOB_STATUSES:
        created = activity_float(job.get("created_at"))
        updated = activity_float(job.get("updated_at"))
        if created is not None and updated is not None:
            return max(0.0, updated - created)
    return None


def job_active_seconds(job: dict) -> float | None:
    if job.get("status") in TERMINAL_JOB_STATUSES:
        return job_duration_seconds(job)
    started = activity_float(job.get("started_at") or job.get("created_at"))
    if started is None:
        return None
    finished = activity_float(job.get("finished_at"))
    end = finished if finished is not None else time.time()
    return max(0.0, end - started)


def job_output_names(job: dict) -> list[str]:
    names = []
    output_mode = job.get("output_mode", DEFAULT_OUTPUT_MODE)
    if mode_has_dxf(output_mode) and job.get("dxf_download_name"):
        names.append(job["dxf_download_name"])
    if mode_has_boq(output_mode) and job.get("boq_download_name"):
        names.append(job["boq_download_name"])
    if mode_has_astri(output_mode):
        names.extend(item["download_name"] for item in job.get("astri_output_files", [])
                     if isinstance(item, dict) and item.get("download_name"))
        if not job.get("astri_output_files") and job.get("astri_download_name"):
            names.append(job["astri_download_name"])
    if mode_has_hpdb(output_mode) and job.get("hpdb_download_name"):
        names.append(job["hpdb_download_name"])
    if mode_has_kmz(output_mode) and job.get("standard_kmz_download_name"):
        names.append(job["standard_kmz_download_name"])
    if job.get("download_name") and job["download_name"] not in names:
        names.append(job["download_name"])
    return names


def job_activity_payload(job: dict, include_logs: bool = False) -> dict:
    status = job.get("status", "unknown")
    job_id = job.get("job_id", "")
    output_mode = job.get("output_mode", DEFAULT_OUTPUT_MODE)
    log_counts = job.get("log_counts") or {}
    original_filenames = job.get("original_filenames") or [Path(path).name for path in job.get("input_paths", [])]
    download_url = None
    if status == "done":
        download_url = url_for("download_file", job_id=job_id)
    payload = {
        "job_id": job_id,
        "short_id": job_id[:8],
        "operator_name": str(job.get("owner_name") or "Sebelum login Google"),
        "operator_email": str(job.get("owner_email") or ""),
        "status": status,
        "progress": int(job.get("progress", 0) or 0),
        "message": job.get("message", ""),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "duration_seconds": job_duration_seconds(job),
        "active_seconds": job_active_seconds(job),
        "output_mode": output_mode,
        "output_label": activity_output_label(output_mode),
        "network_type": normalize_network_type(job.get("network_type")),
        "network_type_label": network_type_label(job.get("network_type")),
        "file_count": len(job.get("input_paths", [])),
        "input_files": original_filenames,
        "download_url": download_url,
        "download_name": job.get("download_name"),
        "download_mimetype": job.get("download_mimetype"),
        "output_names": job_output_names(job),
        "warning_count": int(log_counts.get("WARNING", 0) or 0),
        "error_count": int(log_counts.get("ERROR", 0) or 0),
        "retry_count": int(job.get("retry_count", 0) or 0),
        "max_retries": int(job.get("max_retries", Config.JOB_MAX_RETRIES) or Config.JOB_MAX_RETRIES),
        "dead_letter": bool(job.get("dead_letter", False)),
        "log_seq": int(job.get("log_seq", 0) or 0),
        "queue_backend": job.get("queue_backend"),
        "store_backend": job.get("store_backend"),
        "task_id": job.get("task_id"),
        "stats": job.get("stats", {}),
        "selected_kml_folders": job.get("selected_kml_folders") or [],
        "road_clip_boundary": job.get("road_clip_boundary", True),
        "standardizer_summary": compact_standardizer_summary(job.get("standardizer_summary")),
        "boq_summary": job.get("boq_summary", {}),
        "hpdb_summary": job.get("hpdb_summary", {}),
    }
    if include_logs:
        payload["logs"] = (job.get("logs") or [])[-160:]
        payload["boq_warnings"] = (job.get("boq_warnings") or [])[:80]
        payload["hpdb_warnings"] = (job.get("hpdb_warnings") or [])[:80]
        payload["hpdb_issues"] = (job.get("hpdb_issues") or [])[:80]
    return payload


def status_counts(jobs: list[dict]) -> dict[str, int]:
    counts = {"queued": 0, "processing": 0, "retrying": 0, "done": 0, "error": 0, "failed": 0, "cancelled": 0}
    for job in jobs:
        status = str(job.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts



def _get_astri_session_from_store(job: dict | None = None) -> "requests.Session | None":
    """Use only the integration explicitly captured by this conversion's creator."""
    if not job:
        return None
    user_id = str(job.get("owner_user_id") or "")
    sid = str(job.get("astri_session_id") or "")
    if not user_id or not sid:
        return None
    try:
        return astri_session_record_by_id(sid, user_id)["session"]
    except LookupError:
        return None


def _inject_boq_permit_from_astri(boq_output_path: str, cluster_code: str, scope_type: str, network_type: str, job: dict | None = None) -> None:
    """Inject RT/RW Permit values from ASTRI donation into BOQ Excel.
    Skips silently if no ASTRI session or no donation found.
    """
    try:
        sess = _get_astri_session_from_store(job)
        if not sess:
            LOGGER.info("BOQ permit inject: no ASTRI session, skipping.")
            return

        # Fetch donation
        params = {"filter[global]": cluster_code}
        page = sess.get(f"{ASTRI_BASE_URL}/propose-donation/list", params=params, timeout=60)
        page.raise_for_status()
        donation_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page.text, flags=re.S | re.I)

        donation_nominal = 0.0
        vendor_name = ""
        for row in donation_rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
            if len(cells) < 14:
                continue
            clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if clean[4] == cluster_code:
                nominal_str = clean[14] if len(clean) > 14 else ""
                vendor_name = clean[3] if len(clean) > 3 else ""
                try:
                    donation_nominal = float(nominal_str.replace(".", "").replace(",", "."))
                except (ValueError, AttributeError):
                    donation_nominal = 0.0
                break

        if donation_nominal <= 0:
            LOGGER.info("BOQ permit inject: no donation for %s, skipping.", cluster_code)
            return

        # Calculate PPH
        pph_pct = 0.0
        if vendor_name:
            db_path = os.path.join(os.path.dirname(__file__), "data", "pph_vendor.db")
            if os.path.exists(db_path):
                import sqlite3 as _sqlite3
                conn = _sqlite3.connect(db_path)
                cur = conn.cursor()
                def _norm(x):
                    return re.sub(r"[^a-z0-9]", "", (x or "").lower())
                vendor_norm = _norm(vendor_name)
                cur.execute("SELECT vendor, pph_vendor FROM pph_vendor")
                rows_db = cur.fetchall()
                row_pph = None
                for v, pph in rows_db:
                    if _norm(v) == vendor_norm:
                        row_pph = (pph,)
                        break
                if not row_pph:
                    keywords_norm = [k for k in re.findall(r"[a-z0-9]+", vendor_name.lower()) if len(k) >= 3]
                    best = None
                    for v, pph in rows_db:
                        vnorm = _norm(v)
                        if any(k in vnorm for k in keywords_norm):
                            if best is None or len(v) > len(best[0]):
                                best = (v, pph)
                    row_pph = (best[1],) if best else None
                if row_pph and row_pph[0]:
                    pph_str = str(row_pph[0]).replace(",", ".").replace("%", "").strip()
                    try:
                        pph_pct = float(pph_str) / 100.0
                    except ValueError:
                        pph_pct = 0.0
                conn.close()

        permit_rt_rw = int(donation_nominal / (1 - pph_pct)) if pph_pct > 0 else int(donation_nominal)

        # Open BOQ and inject
        from openpyxl import load_workbook
        boq_path = Path(boq_output_path)
        if not boq_path.exists():
            LOGGER.warning("BOQ permit inject: file not found %s", boq_path)
            return

        wb = load_workbook(boq_path)

        # Find the NRO sheet (cluster only)
        nro_sheet = None
        if "BoQ NRO Cluster" in wb.sheetnames:
            nro_sheet = wb["BoQ NRO Cluster"]
        if not nro_sheet:
            LOGGER.info("BOQ permit inject: no NRO Cluster sheet found, skipping.")
            return

        # Find RT/RW Permit row
        permit_row = None
        for row_num in range(1, nro_sheet.max_row + 1):
            desc = nro_sheet.cell(row_num, 2).value
            if desc and "RT/RW Permit" in str(desc):
                permit_row = row_num
                break
        if not permit_row:
            LOGGER.info("BOQ permit inject: RT/RW Permit row not found, skipping.")
            return

        # Set values: E=Service Qty=1, G=Service Price=permit_rt_rw
        nro_sheet.cell(permit_row, 5).value = 1  # Column E
        nro_sheet.cell(permit_row, 7).value = permit_rt_rw  # Column G

        # Also update Summary PO Permit sheet if exists
        if "Summary PO Permit" in wb.sheetnames:
            summary_ws = wb["Summary PO Permit"]
            summary_ws["B2"] = donation_nominal
            pph_display = round(pph_pct * 100, 2)
            summary_ws["A3"] = f"(Permit + PPh {pph_display}%)"
            summary_ws["B3"] = f"=B2+(B2*{pph_display}%)"

        wb.save(boq_path)
        LOGGER.warning(
            "BOQ permit inject OK: %s | donation=%s vendor=%s pph=%s%% permit_rt_rw=%s",
            cluster_code, donation_nominal, vendor_name, pph_pct * 100, permit_rt_rw
        )
    except Exception:
        LOGGER.exception("BOQ permit inject failed for %s", cluster_code)


def run_conversion_job(job_id: str, raise_on_error: bool = False) -> None:
    job = get_job(job_id)
    if not job:
        return

    slot_token: str | None = None
    slot_refresh_stop = threading.Event()
    last_progress_update = {"time": 0.0}
    last_progress_log = {"ways": 0}
    pipeline_logger = create_job_logger(job_id)
    started_at = time.perf_counter()
    output_mode = job.get("output_mode", OUTPUT_MODE_DXF)
    network_type = normalize_network_type(job.get("network_type"))
    scope_type = normalize_scope_type(job.get("scope_type"))
    boq_template_path = boq_template_path_for_scope_network(scope_type, network_type)
    hpdb_template_path = hpdb_template_path_for_network(network_type)
    hpdb_defaults_path = hpdb_defaults_path_for_network(network_type)
    dxf_template_path = dxf_template_path_for_scope_network(scope_type, network_type)
    output_targets = output_targets_for_mode(output_mode)
    # Feeder hanya support DXF, BOQ, KMZ
    if is_feeder_like_scope(scope_type):
        output_targets = output_targets & FEEDER_ALLOWED_TARGETS
    dxf_progress_cap = 88 if output_targets == {OUTPUT_MODE_DXF} else 58

    def ensure_not_cancelled() -> None:
        if (get_job(job_id) or {}).get("status") == "cancelled":
            raise JobCancelled()

    def wait_for_conversion_slot() -> str:
        attempts = 0
        while True:
            ensure_not_cancelled()
            token = acquire_job_slot(job_id)
            if token:
                if attempts:
                    pipeline_logger.info("Slot konversi tersedia. Job mulai diproses.")
                return token
            attempts += 1
            if attempts == 1 or attempts % 6 == 0:
                update_job(
                    job_id,
                    status="queued",
                    message="Menunggu slot konversi tersedia.",
                )
                pipeline_logger.info("Menunggu slot konversi tersedia.")
            time.sleep(Config.JOB_SLOT_RETRY_SECONDS)

    def keep_slot_alive() -> None:
        interval = max(5, Config.JOB_SLOT_TTL_SECONDS // 3)
        while not slot_refresh_stop.wait(interval):
            if slot_token and not refresh_job_slot(slot_token):
                LOGGER.warning("Slot konversi job %s tidak bisa direfresh.", job_id)
                return

    def on_progress(processed_ways: int) -> None:
        ensure_not_cancelled()
        now = time.time()
        if now - last_progress_update["time"] < 0.75:
            return
        last_progress_update["time"] = now
        progress = min(dxf_progress_cap, 15 + processed_ways // 2500)
        update_job(
            job_id,
            progress=progress,
            message=f"Memproses geometri OSM... {processed_ways:,} ways terbaca.",
        )
        if processed_ways - last_progress_log["ways"] >= 10000:
            last_progress_log["ways"] = processed_ways
            pipeline_logger.info(f"Parsed {processed_ways:,} OSM ways...")

    def on_boq_progress(start: int, end: int):
        def _callback(step_progress: int, message: str) -> None:
            ensure_not_cancelled()
            span = max(end - start, 1)
            progress = min(end, start + int(round(span * max(0, min(100, int(step_progress))) / 100)))
            update_job(job_id, progress=progress, message=message)
            pipeline_logger.info(message)

        return _callback

    try:
        ensure_not_cancelled()
        slot_token = wait_for_conversion_slot()
        ensure_not_cancelled()
        slot_refresh_thread = threading.Thread(
            target=keep_slot_alive,
            name=f"job-slot-refresh-{job_id}",
            daemon=True,
        )
        slot_refresh_thread.start()
        started_at = time.perf_counter()
        update_job(job_id, started_at=time.time())
        record_job_started(job_id)
        LOGGER.info("Job %s dimulai", job_id)
        input_paths = job.get("input_paths") or [job["input_path"]]
        has_osm_input = any(str(path).lower().endswith((".osm", ".pbf", ".osm.pbf")) for path in input_paths)
        hpdb_auto_geocode = Config.HPDB_AUTO_GEOCODE_ENABLED and (mode_has_astri(output_mode) or mode_has_hpdb(output_mode))
        hpdb_defaults_override = job.get("osm_admin_defaults") or None
        if hpdb_auto_geocode:
            pipeline_logger.info("HPDB/ASTRI administrative area will be overwritten from reverse geocode when coordinates are available.")
        dxf_result = None
        boq_result = None
        astri_result = None
        hpdb_result = None
        standardizer_summary = None
        excel_targets = [target for target in (OUTPUT_MODE_BOQ, OUTPUT_MODE_ASTRI, OUTPUT_MODE_HPDB) if target in output_targets]
        excel_windows: dict[str, tuple[int, int]] = {}
        if excel_targets:
            excel_start = 60 if mode_has_dxf(output_mode) else 10
            excel_end = 96
            excel_span = max(excel_end - excel_start, 1)
            for index, target in enumerate(excel_targets):
                start = excel_start + round(excel_span * index / len(excel_targets))
                end = excel_start + round(excel_span * (index + 1) / len(excel_targets))
                excel_windows[target] = (start, end)
        pipeline_logger.info(f"Reading {len(input_paths)} input file(s).")
        pipeline_logger.info(f"Output mode: {output_mode}.")
        pipeline_logger.info(f"Network type: {network_type_label(network_type)}.")
        ensure_not_cancelled()
        # Standardize KML/KMZ inputs FIRST. Mandatory for all output modes -
        # downstream converters (DXF/BOQ/HPDB/ASTRI) cannot tolerate raw, messy
        # KMZ inputs and would produce inconsistent or invalid outputs.
        input_paths, standard_kmz_outputs, standardizer_summary = standardize_kml_inputs_for_job(
            job_id,
            input_paths,
            output_mode,
            network_type,
            pipeline_logger,
            scope_type=scope_type,
        )
        hpdb_coding_defaults = hpdb_olt_defaults_from_standardizer(standardizer_summary)
        if not hpdb_coding_defaults:
            # If standardizer summary doesn't contain FDT coding info (e.g., fallback to default records),
            # fall back directly to Coding FDT DB for OLT code and hostname.
            try:
                from fdt_coding_db import connect as fdt_connect, all_records
                with fdt_connect(Config.FDT_CODING_DB_PATH) as conn:
                    records = all_records(conn)
                    if records:
                        # Get the first record with non-empty olt_initial/hub_id
                        best = next((r for r in records if r.hub_id), None)
                        if best:
                            olt_code = str(best.hub_id or "").strip().upper()
                            olt_hostname = (best.olt_hostname or getattr(best, "olt_hostname", "") or "").strip().upper()
                            if olt_code:
                                hpdb_coding_defaults.setdefault("olt_code", olt_code)
                            if olt_hostname:
                                hpdb_coding_defaults.setdefault("olthostname", olt_hostname)
                            if hpdb_coding_defaults:
                                pipeline_logger.info(
                                    "HPDB/ASTRI OLT fields use Coding FDT (fallback): "
                                    + ", ".join(f"{k}={v}" for k, v in hpdb_coding_defaults.items())
                                )
            except Exception as exc:
                pipeline_logger.warn(f"Fallback Coding FDT direct lookup failed: {exc}")
        if hpdb_coding_defaults:
            hpdb_defaults_override = {**(hpdb_defaults_override or {}), **hpdb_coding_defaults}
            pipeline_logger.info(
                "HPDB/ASTRI OLT fields use Coding FDT: "
                + ", ".join(f"{key}={value}" for key, value in hpdb_coding_defaults.items())
            )
        hpdb_defaults_override = require_admin_defaults_from_region_db(hpdb_defaults_override)
        pipeline_logger.info("Checking KML/KMZ readability...")
        ensure_not_cancelled()
        for input_path in input_paths:
            validate_standard_kml_folders(Path(input_path))
        pipeline_logger.success("Input readability check complete.")
        update_job(
            job_id,
            status="processing",
            progress=18,
            message=f"Membaca {len(input_paths)} file OSM/PBF/KML/KMZ.",
        )

        dxf_design_summary_overrides = {}
        if mode_has_dxf(output_mode):
            if boq_template_path.exists():
                try:
                    dxf_design_summary_overrides = calculate_dxf_boq_design_summary_values(
                        input_paths,
                        boq_template_path,
                        network_type=network_type,
                        scope_type=scope_type,
                    )
                    for label, value in dxf_design_summary_overrides.items():
                        pipeline_logger.info(
                            f"DXF summary {label} uses BOQ NRO source: {value}."
                        )
                except Exception as exc:
                    pipeline_logger.warning(
                        f"Could not calculate required BOQ NRO DXF summary overrides: {exc}"
                    )
                    raise RuntimeError("DXF design summary BOQ NRO calculation failed.") from exc
            else:
                raise FileNotFoundError(
                    f"BOQ {scope_type} {network_type} template not found; DXF design summary cannot be calculated."
                )

        if mode_has_dxf(output_mode):
            ensure_not_cancelled()
            pipeline_logger.info(
                "Boundary clipping: "
                + ("enabled via BOUNDARY CLUSTER." if job.get("road_clip_boundary", True) else "disabled; all OSM roads will be exported.")
            )
            dxf_result = convert_osm_to_dxf(
                input_path=input_paths,
                output_path=job.get("dxf_output_path") or job["output_path"],
                selected_layers=job.get("selected_layers"),
                selected_kml_folders=job.get("selected_kml_folders"),
                road_clip_boundary=job.get("road_clip_boundary", True),
                progress_callback=on_progress,
                preview_feature_limit=Config.PREVIEW_FEATURE_LIMIT,
                pipeline_logger=pipeline_logger,
                validate_inputs=False,
                dxf_template_path=dxf_template_path,
                network_type=network_type,
                design_summary_overrides=dxf_design_summary_overrides,
                scope_type=scope_type,
                original_filenames=job.get("original_filenames"),
            )
            update_job(job_id, progress=60 if is_multi_output_mode(output_mode) else 95, message="Menyimpan DXF dan preview.")

        boq_nro_metadata = boq_nro_metadata_from_standardizer(standardizer_summary)

        if mode_has_boq(output_mode):
            ensure_not_cancelled()
            boq_start, boq_end = excel_windows.get(OUTPUT_MODE_BOQ, (10, 96))
            fdt_coding_count = (
                fdt_coding_count_from_standardizer(standardizer_summary)
                if is_feeder_like_scope(scope_type)
                else None
            )
            if fdt_coding_count:
                pipeline_logger.info(
                    f"BOQ Feeder C63 uses {fdt_coding_count} FDT coding record(s): {fdt_coding_count * 5}."
                )
            boq_result = generate_boq_excel(
                input_paths=input_paths,
                output_path=job.get("boq_output_path"),
                template_path=boq_template_path,
                pipeline_logger=pipeline_logger,
                progress_callback=on_boq_progress(boq_start, boq_end),
                cluster_name_override=boq_nro_metadata["cluster_name"],
                olt_name=boq_nro_metadata["olt_name"],
                network_type=network_type,
                scope_type=scope_type,
                fdt_coding_count=fdt_coding_count,
            )
            update_job(
                job_id,
                boq_summary=boq_result.get("summary", {}),
                boq_preview=boq_result.get("preview_rows", []),
                boq_nro_preview=boq_result.get("nro_preview", {"columns": [], "rows": []}),
                boq_warnings=boq_result.get("warnings", []),
            )
        # Inject RT/RW Permit from ASTRI donation into BOQ
        try:
            _cluster_code = ""
            try:
                _cname = boq_nro_metadata.get("cluster_name", "")
                _parts = _cname.split(" - ", 1)
                _cluster_code = _parts[0].strip() if _parts else _cname.split()[0] if _cname else ""
            except Exception:
                pass
            if not _cluster_code:
                try:
                    _cluster_code = (job.get("apd_astri_match") or {}).get("cluster_code", "")
                except Exception:
                    pass
            if _cluster_code and not is_feeder_like_scope(scope_type):
                _inject_boq_permit_from_astri(
                    job.get("boq_output_path", ""),
                    _cluster_code,
                    scope_type,
                    network_type,
                    job=job,
                )
        except Exception:
            LOGGER.exception("BOQ permit injection step failed")

        if mode_has_astri(output_mode):
            ensure_not_cancelled()
            astri_start, astri_end = excel_windows.get(OUTPUT_MODE_ASTRI, (10, 96))
            astri_result = generate_astri_excel(
                input_paths=input_paths,
                output_path=job.get("astri_xlsx_base_path") or Path(job["astri_output_path"]).with_suffix(".xlsx"),
                template_path=hpdb_template_path,
                defaults_path=hpdb_defaults_path,
                network_type=network_type,
                auto_geocode=hpdb_auto_geocode,
                defaults_override=hpdb_defaults_override,
                pipeline_logger=pipeline_logger,
                progress_callback=on_boq_progress(astri_start, astri_end),
                cluster_name_override=boq_nro_metadata["cluster_name"],
                split_by_fdt=True,
                download_name=job.get("astri_xlsx_download_name") or Path(job["astri_download_name"]).with_suffix(".xlsx").name,
            )
            astri_updates = prepare_astri_job_artifact(job, output_mode, astri_result)
            job.update(astri_updates)
            update_job(
                job_id,
                **astri_updates,
                hpdb_summary=astri_result.get("summary", {}),
                hpdb_preview=astri_result.get("preview", {"cluster": [], "fat": [], "homepass": [], "limit": None, "totals": {}}),
                hpdb_warnings=astri_result.get("warnings", []),
                hpdb_issues=astri_result.get("issues", []),
            )

        if mode_has_hpdb(output_mode):
            ensure_not_cancelled()
            hpdb_start, hpdb_end = excel_windows.get(OUTPUT_MODE_HPDB, (10, 96))
            hpdb_result = generate_hpdb_excel(
                input_paths=input_paths,
                output_path=job.get("hpdb_output_path"),
                defaults_path=hpdb_defaults_path,
                network_type=network_type,
                auto_geocode=hpdb_auto_geocode,
                defaults_override=hpdb_defaults_override,
                pipeline_logger=pipeline_logger,
                progress_callback=on_boq_progress(hpdb_start, hpdb_end),
                cluster_name_override=boq_nro_metadata["cluster_name"],
            )
            update_job(
                job_id,
                hpdb_summary=hpdb_result.get("summary", {}),
                hpdb_preview=hpdb_result.get("preview", {"cluster": [], "fat": [], "homepass": [], "limit": None, "totals": {}}),
                hpdb_warnings=hpdb_result.get("warnings", []),
                hpdb_issues=hpdb_result.get("issues", []),
            )

        if is_multi_output_mode(output_mode):
            ensure_not_cancelled()
            zip_path = Path(job["output_path"])
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            pipeline_logger.info("Preparing ZIP download artifact...")
            zip_build_path = zip_path.with_name(f".{zip_path.name}.tmp") if mode_has_astri(output_mode) else zip_path
            try:
                with zipfile.ZipFile(zip_build_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    if mode_has_dxf(output_mode):
                        archive.write(job.get("dxf_output_path"), arcname=job.get("dxf_download_name") or f"{job_id}.dxf")
                    if mode_has_boq(output_mode):
                        archive.write(job.get("boq_output_path"), arcname=job.get("boq_download_name") or f"BOQ - {job_id}.xlsx")
                    if mode_has_astri(output_mode):
                        for item in astri_output_files(job, astri_result):
                            archive.write(item["path"], arcname=item["download_name"])
                    if mode_has_hpdb(output_mode):
                        archive.write(job.get("hpdb_output_path"), arcname=job.get("hpdb_download_name") or f"HPDB - {job_id}.xlsx")
                    if mode_has_kmz(output_mode):
                        for entry in (get_job(job_id) or {}).get("standard_kmz_outputs", standard_kmz_outputs):
                            archive.write(entry["path"], arcname=entry.get("download_name") or f"{job_id}_STANDARDIZED.kmz")
                if zip_build_path != zip_path:
                    zip_build_path.replace(zip_path)
            except Exception:
                if zip_build_path != zip_path:
                    zip_build_path.unlink(missing_ok=True)
                raise

        ensure_not_cancelled()
        update_job(job_id, progress=98, message="Preparing download")
        pipeline_logger.info("Preparing download artifact...")
        output_size = Path(job["output_path"]).stat().st_size if Path(job["output_path"]).exists() else 0
        pipeline_logger.success(f"Estimated output size: {format_bytes(output_size)}")
        duration = time.perf_counter() - started_at
        pipeline_logger.success(f"Export duration: {format_duration(duration)}")
        current_job = get_job(job_id) or {}
        counts = current_job.get("log_counts", {"WARNING": 0, "ERROR": 0})
        if dxf_result:
            pipeline_logger.success(build_completion_summary(dxf_result, duration, counts))
        if boq_result:
            pipeline_logger.success(build_boq_completion_summary(boq_result, duration, counts))
        if astri_result:
            pipeline_logger.success(build_hpdb_completion_summary(astri_result, duration, counts, title="ASTRI EXPORT COMPLETE"))
        if hpdb_result:
            pipeline_logger.success(build_hpdb_completion_summary(hpdb_result, duration, counts))

        hpdb_stats_result = hpdb_result or astri_result
        stats = merge_hpdb_stats(merge_boq_stats(dxf_result["stats"] if dxf_result else {}, boq_result), hpdb_stats_result)
        labels = []
        details = []
        if dxf_result:
            labels.append("DXF")
            details.append(f"{dxf_result['processed_ways']:,} ways")
        if boq_result:
            labels.append("BOQ")
            mapped = int((boq_result or {}).get("summary", {}).get("total_mapped_boq_item", 0))
            details.append(f"{mapped:,} BOQ item")
        if astri_result:
            labels.append("ASTRI")
            astri_rows = int((astri_result or {}).get("summary", {}).get("hpdb_rows", 0))
            details.append(f"{astri_rows:,} ASTRI row")
        if hpdb_result:
            labels.append("HPDB")
            hpdb_rows = int((hpdb_result or {}).get("summary", {}).get("hpdb_rows", 0))
            details.append(f"{hpdb_rows:,} HPDB row")
        if mode_has_kmz(output_mode):
            labels.append("KMZ")
            names_fixed = int((standardizer_summary or {}).get("name_standardized", 0) or 0)
            styles_fixed = int((standardizer_summary or {}).get("style_standardized", 0) or 0)
            details.append(f"{names_fixed:,} nama, {styles_fixed:,} style distandarkan")
        message = f"{human_join_labels(labels)} selesai."
        if details:
            message = f"{message} {', '.join(details)}."
        stats = merge_standardizer_stats(stats, standardizer_summary)

        update_job(
            job_id,
            status="done",
            progress=100,
            message=message,
            finished_at=time.time(),
            duration_seconds=duration,
            stats=stats,
            preview_geojson=dxf_result["preview"] if dxf_result else {"type": "FeatureCollection", "features": []},
            boq_summary=(boq_result or {}).get("summary", current_job.get("boq_summary", {})),
            boq_preview=(boq_result or {}).get("preview_rows", current_job.get("boq_preview", [])),
            boq_nro_preview=(boq_result or {}).get("nro_preview", current_job.get("boq_nro_preview", {"columns": [], "rows": []})),
            boq_warnings=(boq_result or {}).get("warnings", current_job.get("boq_warnings", [])),
            hpdb_summary=(hpdb_result or astri_result or {}).get("summary", current_job.get("hpdb_summary", {})),
            hpdb_preview=(hpdb_result or astri_result or {}).get("preview", current_job.get("hpdb_preview", {"cluster": [], "fat": [], "homepass": [], "limit": None, "totals": {}})),
            hpdb_warnings=(hpdb_result or astri_result or {}).get("warnings", current_job.get("hpdb_warnings", [])),
            hpdb_issues=(hpdb_result or astri_result or {}).get("issues", current_job.get("hpdb_issues", [])),
            standardizer_summary=standardizer_summary or current_job.get("standardizer_summary", {}),
            error=None,
        )
        record_job_completed(job_id, output_mode, duration, network_type)
        LOGGER.info("Job %s selesai", job_id)
    except JobCancelled:
        duration = time.perf_counter() - started_at
        update_job(
            job_id,
            status="cancelled",
            message="Cancelled by user.",
            finished_at=time.time(),
            duration_seconds=duration,
        )
        pipeline_logger.warn("Job cancelled by user.")
        LOGGER.info("Job %s dibatalkan oleh user", job_id)
    except Exception as exc:
        LOGGER.exception("Job %s gagal", job_id)
        duration = time.perf_counter() - started_at
        pipeline_logger.error(f"Processing failed: {exc}")
        if (get_job(job_id) or {}).get("status") == "cancelled":
            update_job(
                job_id,
                message="Cancelled by user.",
                finished_at=time.time(),
                duration_seconds=duration,
            )
            return
        if raise_on_error:
            raise
        update_job(
            job_id,
            status="error",
            progress=100,
            message="Proses gagal.",
            error=str(exc),
            finished_at=time.time(),
            duration_seconds=duration,
        )
        record_job_failed(job_id, output_mode, network_type)
        alert_job_failed(job_id, str(exc), duration)
    finally:
        slot_refresh_stop.set()
        if slot_token:
            release_job_slot(slot_token)


@app.route("/admin/login", methods=["GET", "POST"])
def dashboard_login():
    return redirect(url_for("dashboard_home"))

@app.route("/admin/logout", methods=["POST"])
def dashboard_logout():
    return EVA_AUTH.logout()

@app.route("/admin/password", methods=["GET", "POST"])
def dashboard_change_password():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    error = ""
    success = ""
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        expected = dashboard_admin_password()
        if not expected or not secrets.compare_digest(current_password, expected):
            error = "Password lama salah."
        elif len(new_password) < 12:
            error = "Password baru minimal 12 karakter."
        elif new_password != confirm_password:
            error = "Konfirmasi password tidak sama."
        else:
            set_dashboard_admin_password(new_password)
            success = "Password berhasil diganti."
    return render_template("admin_change_password.html", error=error, success=success)

@app.route("/admin")
def dashboard_home():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    template_dir = Config.BASE_DIR / "templates"
    templates = [p.name for p in template_dir.glob("*.xlsx")]
    rules_path = Config.BASE_DIR / "config" / "rules.json"
    return render_template("admin_dashboard.html", templates=templates, rules_path=str(rules_path))


def dashboard_boq_templates() -> dict[str, dict]:
    return {
        "cluster_ae": {"label": "Cluster AE", "path": Config.BOQ_TEMPLATE_PATH_AE},
        "cluster_ug": {"label": "Cluster UG", "path": Config.BOQ_TEMPLATE_PATH_UG},
        "subfeeder_ae": {"label": "Subfeeder AE", "path": Config.BOQ_SUBFEEDER_TEMPLATE_PATH_AE},
        "subfeeder_ug": {"label": "Subfeeder UG", "path": Config.BOQ_SUBFEEDER_TEMPLATE_PATH_UG},
        "feeder_ae": {"label": "Feeder AE", "path": Config.BOQ_FEEDER_TEMPLATE_PATH_AE},
        "feeder_ug": {"label": "Feeder UG", "path": Config.BOQ_FEEDER_TEMPLATE_PATH_UG},
    }


@app.route("/admin/boq-templates")
def dashboard_boq_template_page():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    rows = []
    for key, meta in dashboard_boq_templates().items():
        path = Path(meta["path"])
        rows.append({
            "key": key,
            "label": meta["label"],
            "name": path.name,
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if path.exists() else "-",
        })
    return render_template("admin_boq_templates.html", rows=rows)


@app.route("/admin/boq-templates/<template_key>/download")
def dashboard_boq_template_download(template_key):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    meta = dashboard_boq_templates().get(template_key)
    if not meta:
        return "Template tidak dikenal", 404
    path = Path(meta["path"])
    if not path.exists():
        return "Template tidak ditemukan", 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/admin/boq-templates/<template_key>/upload", methods=["POST"])
def dashboard_boq_template_upload(template_key):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    meta = dashboard_boq_templates().get(template_key)
    if not meta:
        return "Template tidak dikenal", 404
    uploaded = request.files.get("template")
    if not uploaded or not uploaded.filename.lower().endswith(".xlsx"):
        return "Upload file .xlsx", 400
    target = Path(meta["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_dir = Config.BASE_DIR / "backups" / "boq_templates"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{target.stem}.{int(time.time())}{target.suffix}"
        shutil.copy2(target, backup)
    uploaded.save(target)
    return redirect(url_for("dashboard_boq_template_page"))


def olt_catalog_path() -> Path:
    return Config.BASE_DIR / "data" / "OLT.db"


def olt_catalog_row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


@app.route("/admin/olt-catalog")
def dashboard_olt_catalog():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    q = normalize_text(request.args.get("q", ""))
    page = max(int(request.args.get("page", 1) or 1), 1)
    limit = 50
    offset = (page - 1) * limit
    conn = sqlite3.connect(str(olt_catalog_path()))
    conn.row_factory = sqlite3.Row
    try:
        where = ""
        params = []
        if q:
            where = "WHERE olt_code LIKE ? OR olt_initial LIKE ? OR olt_name LIKE ? OR olt_hostname LIKE ? OR olt_area LIKE ?"
            params = [f"%{q}%"] * 5
        total = conn.execute(f"SELECT COUNT(*) AS total FROM olt_catalog {where}", params).fetchone()["total"]
        rows = conn.execute(
            f"SELECT id, olt_code, olt_initial, olt_name, olt_hostname, olt_vendor, olt_area, updated_at FROM olt_catalog {where} ORDER BY olt_code LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()
    return render_template("admin_olt_catalog.html", rows=rows, q=q, page=page, total=total, limit=limit)


@app.route("/admin/olt-catalog/save", methods=["POST"])
def dashboard_olt_catalog_save():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    olt_id = request.form.get("id", "").strip()
    payload = {
        "olt_code": normalize_text(request.form.get("olt_code", "")).upper(),
        "olt_initial": normalize_text(request.form.get("olt_initial", "")).upper(),
        "olt_name": normalize_text(request.form.get("olt_name", "")),
        "olt_hostname": normalize_text(request.form.get("olt_hostname", "")).upper(),
        "olt_vendor": normalize_text(request.form.get("olt_vendor", "")).upper(),
        "olt_area": normalize_text(request.form.get("olt_area", "")),
    }
    if not payload["olt_code"]:
        return "OLT code wajib diisi", 400
    now = datetime.utcnow().isoformat(timespec="seconds")
    conn = sqlite3.connect(str(olt_catalog_path()))
    try:
        if olt_id:
            conn.execute(
                """
                UPDATE olt_catalog
                SET olt_code=?, olt_initial=?, olt_name=?, olt_hostname=?, olt_vendor=?, olt_area=?, updated_at=?
                WHERE id=?
                """,
                (*payload.values(), now, olt_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO olt_catalog (olt_code, olt_initial, olt_name, olt_hostname, olt_vendor, olt_area, source_row, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (*payload.values(), now, now),
            )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return f"Duplicate/invalid OLT: {exc}", 400
    finally:
        conn.close()
    return redirect(url_for("dashboard_olt_catalog", q=payload["olt_code"]))


@app.route("/admin/olt-catalog/<int:olt_id>/delete", methods=["POST"])
def dashboard_olt_catalog_delete(olt_id):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    conn = sqlite3.connect(str(olt_catalog_path()))
    try:
        conn.execute("DELETE FROM olt_catalog WHERE id=?", (olt_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("dashboard_olt_catalog"))


@app.route("/admin/jobs")
def dashboard_jobs():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    if request.args.get("tab") == "boq":
        if request.host.partition(":")[0].casefold() != "dashboard.dyena.id":
            return jsonify({"error": "Halaman tidak ditemukan."}), 404
        return render_template("admin_boq_history.html")
    from jobs import list_jobs
    page = max(int(request.args.get("page", 1) or 1), 1)
    limit = 20
    items = []
    try:
        raw = list_jobs(limit=limit * page)
        for job in raw:
            items.append({
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "progress": job.get("progress"),
                "message": job.get("message"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "error": job.get("error"),
                "output_mode": job.get("output_mode"),
                "scope_type": job.get("scope_type"),
                "network_type": job.get("network_type"),
                "log_counts": job.get("log_counts"),
            })
    except Exception as exc:
        items = []
        items.append({"job_id": "ERR", "status": "error", "error": str(exc)})
    return render_template("admin_jobs.html", jobs=items, page=page)


@app.route("/admin/jobs/<job_id>")
def dashboard_job_detail(job_id):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    from jobs import get_job
    job = get_job(job_id) or {}
    return render_template("admin_job_detail.html", job=job)


@app.route("/api/dashboard/dry-run", methods=["POST"])
def api_dashboard_dry_run():
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith((".kml", ".kmz")):
        return jsonify({"error": "Upload file .kml/.kmz"}), 400
    scope_type = normalize_scope_type(request.form.get("scope_type", "Cluster"))
    network_type = normalize_network_type(request.form.get("network_type", "AE"))
    job_id = f"dryrun-{uuid.uuid4().hex}"
    input_path = Config.UPLOAD_DIR / f"{job_id}_{secure_filename(uploaded.filename)}"
    output_path = Config.OUTPUT_DIR / f"{job_id}_preview.kmz"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    uploaded.save(input_path)
    try:
        summary = standardize_kmz_file(input_path, None, output_path, network_type=network_type, scope_type=scope_type, source_name=uploaded.filename)
        return jsonify({
            "ok": True,
            "summary": {
                "scope_type": summary.get("scope_type"),
                "network_type": summary.get("network_type"),
                "matched_project": summary.get("matched_project"),
                "matched_site": summary.get("matched_site"),
                "match_score": summary.get("match_score"),
                "warnings": summary.get("warnings", []),
                "folder_changes": summary.get("folder_changes", [])[:80],
                "name_changes": summary.get("name_changes", [])[:120],
                "organizer_changes": summary.get("organizer_changes", [])[:80],
                "distribution_cable_changes": summary.get("distribution_cable_changes", [])[:80],
                "style_changes": summary.get("style_changes", [])[:80],
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


@app.route("/api/filename-rules")
def api_filename_rules():
    rules = load_rules_config().get("filename_rules") or {}
    return jsonify({"rules": rules})


@app.route("/api/dashboard/boq-history", methods=["GET"])
def api_dashboard_boq_history():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    if request.host.partition(":")[0].casefold() != "dashboard.dyena.id":
        return jsonify({"error": "Halaman tidak ditemukan."}), 404
    try:
        page = int(request.args.get("page", "1"))
        result = get_astri_boq_submission_store().list_success_history(
            page=page, q=request.args.get("q", ""), scope=request.args.get("scope", ""),
        )
        return jsonify(result)
    except ValueError:
        return jsonify({"error": "Filter riwayat Add BOQ tidak valid."}), 400
    except Exception:
        return jsonify({"error": "Riwayat Add BOQ belum dapat dimuat. Coba muat ulang."}), 503


@app.route("/api/dashboard/jobs")
def api_dashboard_jobs():
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    limit = min(int(request.args.get("limit", 20) or 20), 50)
    jobs = list_jobs(limit=limit)
    summary = []
    for job in jobs:
        download_url = None
        if job.get("status") in ("done", "completed") and job.get("output_path"):
            download_url = f"/download/{job['job_id']}"
        summary.append({
            "job_id": job.get("job_id"),
            "operator_name": str(job.get("owner_name") or "Sebelum login Google"),
            "operator_email": str(job.get("owner_email") or ""),
            "status": job.get("status"),
            "progress": job.get("progress"),
            "message": job.get("message"),
            "error": job.get("error"),
            "scope_type": job.get("scope_type"),
            "network_type": job.get("network_type"),
            "output_mode": job.get("output_mode"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "download_url": download_url,
            "download_name": job.get("download_name"),
            "output_names": job_output_names(job),
            "original_filenames": job.get("original_filenames") or [],
        })
    return jsonify({"jobs": summary})


@app.route("/api/dashboard/jobs/<job_id>/cancel", methods=["POST"])
def api_dashboard_cancel_job(job_id):
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    update_job(job_id, status="cancelled", message="Cancelled by admin.", error="Cancelled via dashboard.")
    return jsonify({"ok": True})


@app.route("/api/dashboard/jobs/<job_id>/delete", methods=["POST"])
def api_dashboard_delete_job(job_id):
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    delete_job(job_id)
    return jsonify({"ok": True})


@app.route("/api/dashboard/jobs/<job_id>/restart", methods=["POST"])
def api_dashboard_restart_job(job_id):
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    new_job_id = uuid.uuid4().hex
    original_owner = str(job.get("owner_user_id") or EVA_AUTH.store.resource_owner("job", job_id) or "")
    # Legacy/system jobs remain explicitly owner-managed, never assigned to a public signup.
    owner_id = original_owner or g.eva_user["id"]
    if not EVA_AUTH.store.user_active(owner_id):
        return jsonify({"error": "Akun pembuat pekerjaan sudah dinonaktifkan."}), 403
    sid = str(job.get("astri_session_id") or "")
    if not sid or EVA_AUTH.store.resource_owner("astri", sid) != owner_id:
        sid = ""
    def fresh_path(key):
        value = job.get(key)
        if not value:
            return None
        path = Path(str(value))
        return str(path.with_name(new_job_id + "_" + path.name))
    EVA_AUTH.store.claim("job", new_job_id, owner_id)
    _system_create_job(
        job_id=new_job_id,
        input_paths=job.get("input_paths") or [job.get("input_path", "")],
        output_path=fresh_path("output_path"),
        selected_layers=job.get("selected_layers") or ["ROAD", "KML"],
        selected_kml_folders=job.get("selected_kml_folders"),
        road_clip_boundary=job.get("road_clip_boundary", True),
        output_mode=job.get("output_mode", "dxf"),
        network_type=job.get("network_type", "AE"),
        scope_type=job.get("scope_type", "Cluster"),
        dxf_output_path=fresh_path("dxf_output_path"),
        boq_output_path=fresh_path("boq_output_path"),
        astri_output_path=fresh_path("astri_output_path"),
        hpdb_output_path=fresh_path("hpdb_output_path"),
        dxf_download_name=job.get("dxf_download_name"),
        boq_download_name=job.get("boq_download_name"),
        astri_download_name=job.get("astri_download_name"),
        hpdb_download_name=job.get("hpdb_download_name"),
        download_name=job.get("download_name"),
        download_mimetype=job.get("download_mimetype"),
        owner_user_id=owner_id,
        astri_session_id=sid,
        owner_name=job.get("owner_name", ""),
        owner_email=job.get("owner_email", ""),
    )
    update_job(new_job_id, osm_admin_defaults=job.get("osm_admin_defaults") or {},
               original_filenames=job.get("original_filenames") or [],
               standard_kmz_output_path=fresh_path("standard_kmz_output_path"),
               standard_kmz_download_name=job.get("standard_kmz_download_name"),
               astri_xlsx_base_path=fresh_path("astri_xlsx_base_path"),
               hpdb_xlsx_base_path=fresh_path("hpdb_xlsx_base_path"))
    enqueue_conversion_job(new_job_id, run_conversion_job)
    update_job(job_id, status="restarted", message=f"Restarted to {new_job_id}.")
    return jsonify({"ok": True, "new_job_id": new_job_id})


@app.route("/admin/filename-rules", methods=["GET", "POST"])
def dashboard_filename_rules():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    config = load_rules_config()
    rules = config.setdefault("filename_rules", {})
    error = ""
    success = ""
    if request.method == "POST":
        scope = request.form.get("scope", "Cluster")
        try:
            pattern = request.form.get("pattern", "").strip()
            _filename_re.compile(pattern)
            rules[scope] = {
                "pattern": pattern,
                "sample": request.form.get("sample", "").strip(),
                "checklist": [line.strip() for line in request.form.get("checklist", "").splitlines() if line.strip()],
            }
            backup = RULES_CONFIG_PATH.with_suffix(RULES_CONFIG_PATH.suffix + f".{int(time.time())}.bak")
            if RULES_CONFIG_PATH.exists():
                shutil.copy2(RULES_CONFIG_PATH, backup)
            RULES_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            success = "Filename rule saved."
        except _filename_re.error as exc:
            error = f"Regex invalid: {exc}"
    return render_template("admin_filename_rules.html", rules=rules, error=error, success=success)


@app.route("/admin/dry-run")
def dashboard_dry_run():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    return render_template("admin_dry_run.html")


@app.route("/admin/astri")
def dashboard_astri_monitor():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    return render_template("admin_astri.html")


@app.route("/api/dashboard/astri")
def api_dashboard_astri():
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    status_payload = {"connected": False}
    try:
        with app.test_request_context('/api/astri/status'):
            response = api_astri_status()
        if isinstance(response, tuple):
            response = response[0]
        status_payload = response.get_json() if hasattr(response, 'get_json') else {"connected": False}
    except Exception:
        status_payload = {"connected": False}
    recent = [job_activity_payload(job) for job in list_jobs(limit=50)
              if 'astri' in str(job.get('output_mode') or '')][:10]
    return jsonify({"status": status_payload, "jobs": recent})


@app.route("/admin/kmz-styles")
def dashboard_kmz_styles():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    rows = []
    for key, spec in sorted(kmz_standardizer.STYLE_SPECS.items()):
        rows.append({
            "key": key,
            "style_id": spec.style_id,
            "color": spec.color,
            "icon_href": spec.icon_href or "",
            "icon_scale": spec.icon_scale or "",
            "line_width": spec.line_width or "",
            "line_opacity": spec.line_opacity or "",
            "poly_fill_opacity": spec.poly_fill_opacity or "",
            "poly_fill": spec.poly_fill or "",
            "poly_outline": spec.poly_outline or "",
            "list_icon_href": spec.list_icon_href or "",
        })
    return render_template("admin_kmz_styles.html", rows=rows)


@app.route("/admin/kmz-styles/<style_key>/save", methods=["POST"])
def dashboard_kmz_style_save(style_key):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    config = load_rules_config()
    styles = config.setdefault("kmz_styles", {})
    styles[style_key] = {
        "color": normalize_text(request.form.get("color")),
        "icon_href": normalize_text(request.form.get("icon_href")),
        "icon_scale": normalize_text(request.form.get("icon_scale")),
        "line_width": normalize_text(request.form.get("line_width")),
        "line_opacity": normalize_text(request.form.get("line_opacity")),
        "poly_fill_opacity": normalize_text(request.form.get("poly_fill_opacity")),
        "poly_fill": normalize_text(request.form.get("poly_fill")),
        "poly_outline": normalize_text(request.form.get("poly_outline")),
        "list_icon_href": normalize_text(request.form.get("list_icon_href")),
    }
    styles[style_key] = {k: v for k, v in styles[style_key].items() if v != ""}
    backup = RULES_CONFIG_PATH.with_suffix(RULES_CONFIG_PATH.suffix + f".{int(time.time())}.bak")
    if RULES_CONFIG_PATH.exists():
        shutil.copy2(RULES_CONFIG_PATH, backup)
    RULES_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return redirect(url_for("dashboard_kmz_styles"))


@app.route("/admin/rules", methods=["GET", "POST"])
def dashboard_rules():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    error = ""
    success = ""
    target = RULES_CONFIG_PATH
    if request.method == "POST":
        raw_rules = request.form.get("rules", "")
        try:
            payload = json.loads(raw_rules)
            if not isinstance(payload, dict):
                raise ValueError("JSON root harus object.")
            backup = target.with_suffix(target.suffix + f".{int(time.time())}.bak")
            if target.exists():
                shutil.copy2(target, backup)
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            success = f"Rules saved. Backup: {backup.name}"
        except (ValueError, json.JSONDecodeError) as exc:
            error = str(exc)
    rules_text = target.read_text(encoding="utf-8") if target.exists() else "{}\n"
    return render_template("admin_rules.html", rules_text=rules_text, error=error, success=success)


@app.route("/api/dashboard/rules", methods=["GET"])
def api_dashboard_rules():
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    config = {
        key: {str(k): str(v) for k, v in value.items()}
        for key, value in load_rules_config().items()
    }
    return jsonify(config)


@app.route("/api/dashboard/rules", methods=["POST"])
def api_dashboard_save_rules():
    if not _is_dashboard_auth():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload harus JSON object."}), 400
    target = RULES_CONFIG_PATH
    if not target.exists():
        target = Config.BASE_DIR / "config" / "rules.json"
    backup = target.with_suffix(target.suffix + ".bak")
    if target.exists():
        shutil.copy2(target, backup)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    kmz_standardizer._RULES_CONFIG = json.loads(target.read_text(encoding="utf-8"))
    kmz_standardizer.EXT_PARTNER_ALIASES = {
        **kmz_standardizer.DEFAULT_EXT_PARTNER_ALIASES,
        **{str(k).upper(): str(v).upper() for k, v in (kmz_standardizer._RULES_CONFIG.get("ext_partner_aliases") or {}).items()},
    }
    kmz_standardizer.EXT_PARTNER_TARGET_FOLDER = {
        **kmz_standardizer.DEFAULT_EXT_PARTNER_TARGET_FOLDER,
        **{str(k).upper(): str(v) for k, v in (kmz_standardizer._RULES_CONFIG.get("ext_partner_target_folder") or {}).items()},
    }
    kmz_standardizer.BARE_EXT_PARTNER_PATTERN = re.compile(
        rf"^\s*EXT[\.\s_-]*(?P<partner>{kmz_standardizer.partner_pattern_from_aliases(kmz_standardizer.EXT_PARTNER_ALIASES)})\s*\.?\s*$",
        re.IGNORECASE,
    )
    kmz_standardizer.EXT_PARTNER_POLE_PATTERN = re.compile(
        rf"\bEXT\.(?P<partner>{kmz_standardizer.partner_pattern_from_aliases(kmz_standardizer.EXT_PARTNER_ALIASES)})"
        r"(?:\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)?))?\.(?P<pole>P\d+)\b",
        re.IGNORECASE,
    )
    kmz_standardizer.EXT_PARTNER_BARE_PATTERN = re.compile(
        rf"\b(?:EXT[\.\s_-]*)?(?P<partner>{kmz_standardizer.partner_pattern_from_aliases(kmz_standardizer.EXT_PARTNER_ALIASES)})\b"
        r"(?:[\.\s_-]+(?P<cluster>[A-Z0-9-]+))?\b",
        re.IGNORECASE,
    )
    kmz_standardizer.PARTNER_DOT_POLE_PATTERN = re.compile(
        rf"(?<![A-Z0-9])(?P<partner>{kmz_standardizer.partner_pattern_from_aliases(kmz_standardizer.EXT_PARTNER_ALIASES)})"
        r"\s*\.\s*P?(?P<number>\d+)(?![A-Z0-9])",
        re.IGNORECASE,
    )
    kmz_standardizer.STANDARD_POLE_NUMBER_PATTERN = re.compile(
        rf"\b(?:MR|EXT\.(?:{kmz_standardizer.partner_pattern_from_aliases({k: k for k in kmz_standardizer.EXT_PARTNER_TARGET_FOLDER})}))\.(?P<cluster>[A-Z0-9-]+(?:\.[A-Z0-9-]+)?)\.P(?P<number>\d+)\b",
        re.IGNORECASE,
    )
    return jsonify({"ok": True, "backup": str(backup), "path": str(target)})


def _is_dashboard_auth():
    protected = dashboard_admin_required()
    if protected is not None:
        return False
    return True


@app.route("/")
def index():
    if is_dashboard_host():
        return redirect(url_for("dashboard_home"))
    default_network_type = normalize_network_type(Config.DEFAULT_NETWORK_TYPE)
    default_scope_type = normalize_scope_type(Config.DEFAULT_SCOPE_TYPE)
    default_boq_template_path = boq_template_path_for_network(default_network_type)
    default_hpdb_template_path = hpdb_template_path_for_network(default_network_type)
    return render_template(
        "index.html",
        max_upload_mb=Config.MAX_UPLOAD_MB,
        default_network_type=default_network_type,
        network_types=[
            {"value": network_type, "label": NETWORK_TYPE_LABELS[network_type]}
            for network_type in Config.NETWORK_TYPES
        ],
        default_scope_type=default_scope_type,
        scope_types=[
            {"value": scope, "label": SCOPE_TYPE_LABELS[scope]}
            for scope in Config.SCOPE_TYPES
        ],
        boq_template_ready=default_boq_template_path.exists(),
        boq_template_name=default_boq_template_path.name,
        boq_template_ready_by_network=template_ready_by_network(Config.BOQ_TEMPLATE_PATHS),
        boq_template_name_by_network=template_name_by_network(Config.BOQ_TEMPLATE_PATHS),
        boq_feeder_template_ready_by_network=template_ready_by_network(Config.BOQ_FEEDER_TEMPLATE_PATHS),
        boq_feeder_template_name_by_network=template_name_by_network(Config.BOQ_FEEDER_TEMPLATE_PATHS),
        boq_sheet_name=BOQ_SHEET_NAME,
        astri_template_ready=default_hpdb_template_path.exists(),
        astri_template_name=default_hpdb_template_path.name,
        astri_template_ready_by_network=template_ready_by_network(Config.HPDB_TEMPLATE_PATHS),
        astri_template_name_by_network=template_name_by_network(Config.HPDB_TEMPLATE_PATHS),
        hpdb_template_ready=default_hpdb_template_path.exists(),
        hpdb_template_name=default_hpdb_template_path.name,
        hpdb_template_ready_by_network=template_ready_by_network(Config.HPDB_TEMPLATE_PATHS),
        hpdb_template_name_by_network=template_name_by_network(Config.HPDB_TEMPLATE_PATHS),
        hpdb_sheet_names=[HPDB_CLUSTER_SHEET_NAME, HPDB_FAT_SHEET_NAME, HPDB_HOMEPASS_SHEET_NAME],
        hpdb_max_fdt=HPDB_MAX_FDT,
        fdt_coding_source_ready=fdt_coding_source_ready(),
        fdt_coding_source_label=fdt_coding_source_label(),
        dxf_conversion_enabled=Config.DXF_CONVERSION_ENABLED,
        default_output_mode=DEFAULT_OUTPUT_MODE,
        app_version=APP_VERSION,
    )


def redis_connection_ok() -> bool:
    if not Config.REDIS_URL:
        return False
    try:
        import redis

        redis.Redis.from_url(Config.REDIS_URL, socket_connect_timeout=2, decode_responses=True).ping()
        return True
    except Exception:
        return False


def worker_available_ok() -> bool:
    if Config.CONVERSION_QUEUE_ENABLED in {"0", "false", "no", "off"}:
        return True
    if not Config.CELERY_BROKER_URL:
        return False
    try:
        from tasks import celery_app

        inspector = celery_app.control.inspect(timeout=1.0)
        replies = inspector.ping() or {}
        return bool(replies)
    except Exception:
        return False


def template_directory_has_files(paths_by_variant: dict | None, fallback_path: Path, suffix: str) -> bool:
    directories = {Path(fallback_path).parent}
    if paths_by_variant:
        directories.update(Path(path).parent for path in paths_by_variant.values())
    for directory in directories:
        try:
            if directory.exists() and directory.is_dir() and any(directory.glob(f"*{suffix}")):
                return True
        except OSError:
            continue
    return False

def directory_is_writable(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_dir():
            return False
        probe = path / f".readyz-{uuid.uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False

def readiness_checks() -> dict[str, bool]:
    dxf_paths = {
        **getattr(Config, "DXF_TEMPLATE_PATHS", {}),
        **getattr(Config, "DXF_FEEDER_TEMPLATE_PATHS", {}),
        **getattr(Config, "DXF_SUBFEEDER_TEMPLATE_PATHS", {}),
    }
    boq_paths = {
        **getattr(Config, "BOQ_TEMPLATE_PATHS", {}),
        **getattr(Config, "BOQ_FEEDER_TEMPLATE_PATHS", {}),
        **getattr(Config, "BOQ_SUBFEEDER_TEMPLATE_PATHS", {}),
    }
    hpdb_template_paths = getattr(Config, "HPDB_TEMPLATE_PATHS", {})
    hpdb_defaults_paths = getattr(Config, "HPDB_DEFAULTS_PATHS", {})
    return {
        "secret_key_configured": Config.DEBUG or Config.SECRET_KEY != "change-me-in-production",
        "redis_connected": redis_connection_ok(),
        "dxf_templates_available": template_directory_has_files(dxf_paths, Config.DXF_TEMPLATE_PATH, ".dxf"),
        "boq_templates_available": template_directory_has_files(boq_paths, Config.BOQ_TEMPLATE_PATH, ".xlsx"),
        "hpdb_templates_available": template_directory_has_files(hpdb_template_paths, Config.HPDB_TEMPLATE_PATH, ".xlsx"),
        "hpdb_defaults_available": template_directory_has_files(hpdb_defaults_paths, Config.HPDB_DEFAULTS_PATH, ".json"),
        "fdt_coding_source_ready": fdt_coding_source_ready(),
        "region_db_available": region_db.database_ready(),
        "upload_dir_writable": directory_is_writable(Config.UPLOAD_DIR),
        "output_dir_writable": directory_is_writable(Config.OUTPUT_DIR),
        "boq_output_dir_writable": directory_is_writable(Config.BOQ_OUTPUT_DIR),
        "hpdb_output_dir_writable": directory_is_writable(Config.HPDB_OUTPUT_DIR),
    }

@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "service": "eva-web",
        "version": APP_VERSION,
        "timestamp": int(time.time()),
    })

@app.route("/readyz")
def readyz():
    checks = readiness_checks()
    if all(checks.values()):
        return jsonify({"status": "ready", "checks": checks})
    return jsonify({"status": "not ready", "checks": checks}), 503

@app.route("/health")
def health_check():
    """Health check endpoint untuk Docker, load balancer, dan monitoring."""
    readiness = readiness_checks()
    checks = {
        "status": "ok",
        "timestamp": time.time(),
        "version": APP_VERSION,
        "redis_connected": readiness["redis_connected"],
        "worker_available": False,
        "upload_dir_writable": readiness["upload_dir_writable"],
        "output_dir_writable": readiness["output_dir_writable"],
        "boq_output_dir_writable": readiness["boq_output_dir_writable"],
        "hpdb_output_dir_writable": readiness["hpdb_output_dir_writable"],
        "readyz_checks": readiness,
    }

    checks["worker_available"] = worker_available_ok()
    if prometheus_metrics:
        prometheus_metrics.update_system_gauges(checks["redis_connected"], checks["worker_available"])

    all_ok = all(readiness.values())
    checks["status"] = "ok" if all_ok else "degraded"
    if not all_ok:
        failing_checks = sorted(name for name, is_ok in readiness.items() if not is_ok)
        alert_server_unhealthy(
            "Health dependencies failed: " + ", ".join(failing_checks)
        )
    return jsonify(checks), 200 if all_ok else 503


@app.route("/metrics")
def metrics():
    return jsonify(metrics_snapshot())


@app.route("/metrics/prometheus")
def prometheus_metrics_endpoint():
    if not prometheus_metrics:
        return "prometheus-client belum tersedia\n", 503, {"Content-Type": "text/plain; charset=utf-8"}
    prometheus_metrics.update_system_gauges(redis_connection_ok(), worker_available_ok())
    return prometheus_metrics.generate_latest(), 200, {"Content-Type": prometheus_metrics.CONTENT_TYPE_LATEST}


@app.route("/api/activity")
def activity():
    try:
        limit = min(max(int(request.args.get("limit", "40")), 1), 100)
    except ValueError:
        limit = 40
    status_filter = request.args.get("status", "all").strip().lower()
    search_text = request.args.get("q", "").strip().lower()

    all_jobs = list_jobs(120)
    filtered_jobs = all_jobs
    if status_filter not in {"", "all"}:
        filtered_jobs = [job for job in filtered_jobs if str(job.get("status", "")).lower() == status_filter]
    if search_text:
        def _matches(job: dict) -> bool:
            haystack = " ".join(
                [
                    str(job.get("job_id", "")),
                    str(job.get("download_name", "")),
                    str(job.get("message", "")),
                    str(job.get("output_mode", "")),
                    str(job.get("network_type", "")),
                    " ".join(str(name) for name in job.get("original_filenames", [])),
                ]
            ).lower()
            return search_text in haystack

        filtered_jobs = [job for job in filtered_jobs if _matches(job)]

    return jsonify(
        {
            "server_time": time.time(),
            "metrics": metrics_snapshot() if g.eva_user["role"] == "owner" else {},
            "counts": status_counts(all_jobs),
            "filtered_count": len(filtered_jobs),
            "jobs": [job_activity_payload(job) for job in filtered_jobs[:limit]],
        }
    )


@app.route("/api/activity/<job_id>")
def activity_detail(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return jsonify(job_activity_payload(job, include_logs=True))


@app.route("/api/knowledge/status")
def api_knowledge_status():
    try:
        return jsonify(ai_assistant.knowledge_status())
    except Exception as exc:
        LOGGER.exception("Gagal membaca status knowledge base")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/knowledge/reindex", methods=["POST"])
def api_knowledge_reindex():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        return jsonify(ai_assistant.reindex_knowledge())
    except Exception as exc:
        LOGGER.exception("Gagal reindex knowledge base")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/ai-chat", methods=["POST"])
def api_ai_chat():
    return jsonify({"error": "AI chat dinonaktifkan."}), 503


@app.route("/validate-folders", methods=["POST"])
def validate_folders():
    files = request.files.getlist("files") or request.files.getlist("file")
    files = [file for file in files if file and file.filename]
    if not files:
        return jsonify({"error": "File KML/KMZ tidak ditemukan di request."}), 400

    results = []
    saved_paths: list[Path] = []
    try:
        for index, file in enumerate(files, start=1):
            if not file.filename.lower().endswith((".kml", ".kmz")):
                return jsonify({"error": f"Validasi folder hanya menerima .kml atau .kmz: {file.filename}"}), 400
            safe_name = secure_filename(file.filename) or f"validate_{index}{file_suffix(file.filename)}"
            temp_path = Config.UPLOAD_DIR / f"validate_{uuid.uuid4().hex}_{safe_name}"
            file.save(temp_path)
            saved_paths.append(temp_path)
            root = load_kml_root(temp_path)
            folder_counts: dict[str, int] = {}
            warning_count = 0
            for placemark in iter_placemarks(root):
                canonical_folders = [
                    canonical_standard_folder(folder_name)
                    for folder_name in placemark.folder_stack
                    if canonical_standard_folder(folder_name)
                ]
                if canonical_folders:
                    folder = canonical_folders[-1]
                    folder_counts[folder] = folder_counts.get(folder, 0) + 1
                else:
                    warning_count += 1
            results.append(
                {
                    "filename": file.filename,
                    "folders": [
                        {"name": folder_name, "items": count}
                        for folder_name, count in sorted(folder_counts.items())
                    ],
                    "item_count": sum(folder_counts.values()),
                    "warning_count": warning_count,
                }
            )
        return jsonify({"files": results})
    except Exception as exc:
        return jsonify({"error": f"Validasi folder gagal: {exc}"}), 400
    finally:
        cleanup_saved_uploads(saved_paths)


@app.route("/openapi.yaml")
def openapi_spec():
    return send_from_directory(app.root_path, "openapi.yaml", mimetype="text/yaml")


@app.route("/docs")
def api_docs():
    return render_template("docs.html")



@app.route("/api/osm/kecamatan")
def osm_kecamatan_regions():
    regions = list_osm_kecamatan_regions()
    cities_sorted = sorted({region["city"] for region in regions})
    provinces = sorted({region["province"] for region in regions if region["province"] != "Lainnya"})
    return jsonify({"regions": regions, "cities": cities_sorted, "provinces": provinces})

@app.route("/api/geofabrik/regions")
def geofabrik_regions():
    """Return list of pre-downloaded Geofabrik extracts available on the server."""
    return jsonify({"regions": list_geofabrik_regions()})


@app.route("/convert", methods=["POST"])
@app.route("/upload", methods=["POST"])
def upload_file():
    geofabrik_region = request.form.get("geofabrik_region", "").strip()
    osm_city = request.form.get("osm_city", "").strip()
    osm_kecamatan = request.form.get("osm_kecamatan", "auto").strip()
    astri_cluster_code = request.form.get("astri_cluster_code", "").strip()
    override_province = request.form.get("province", "").strip()
    override_area_name = request.form.get("area_name", "").strip()
    files = request.files.getlist("files") or request.files.getlist("file")
    files = [file for file in files if file and file.filename]

    geofabrik_path = None
    kecamatan_path = None
    admin_override_fields = _apply_admin_overrides({}, request.form)
    if osm_kecamatan and osm_kecamatan.lower() not in {"auto", "all", ""}:
        kecamatan_path = resolve_osm_kecamatan_path(osm_kecamatan)
        if not kecamatan_path:
            return jsonify({"error": f"OSM kecamatan tidak ditemukan: {osm_kecamatan}"}), 400
    if geofabrik_region and not kecamatan_path:
        geofabrik_path = Config.GEOFABRIK_DIR / geofabrik_region
        if not geofabrik_path.is_file():
            return jsonify({"error": f"Geofabrik region tidak ditemukan: {geofabrik_region}"}), 400

    if not files and not geofabrik_path and not kecamatan_path:
        return jsonify({"error": "File tidak ditemukan di request. Upload file atau pilih region Geofabrik."}), 400

    for file in files:
        if not allowed_file(file.filename):
            return jsonify({"error": f"Format file tidak didukung: {file.filename}. Upload user hanya menerima .kml atau .kmz."}), 400
        if is_manual_osm_upload(file.filename):
            return jsonify({"error": "Upload OSM/PBF manual dinonaktifkan. Pilih ROAD dari OSM server Provinsi/Kota/Kecamatan."}), 400

    try:
        selected_layers = parse_selected_layers()
        scope_type = parse_scope_type()
        selected_kml_folders = parse_selected_kml_folders(scope_type)
        road_clip_boundary = parse_road_clip_boundary()
        output_mode = parse_output_mode()
        network_type = parse_network_type()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Feeder hanya support DXF, BOQ, KMZ ???????? filter output targets
    if is_feeder_like_scope(scope_type):
        feeder_filtered = set(output_mode.split("_")) & FEEDER_ALLOWED_TARGETS
        if feeder_filtered:
            output_mode = "_".join(
                target for target in OUTPUT_TARGET_ORDER if target in feeder_filtered
            )
        else:
            output_mode = OUTPUT_MODE_BOQ

    boq_template_path = boq_template_path_for_scope_network(scope_type, network_type)
    hpdb_template_path = hpdb_template_path_for_network(network_type)

    if mode_has_boq(output_mode) or mode_has_astri(output_mode) or mode_has_hpdb(output_mode) or mode_has_kmz(output_mode):
        kml_files = [file for file in files if file.filename.lower().endswith((".kml", ".kmz"))]
        has_kml = bool(kml_files)
    else:
        kml_files = []
        has_kml = False

    filename_validation = validate_upload_filenames(scope_type, kml_files)
    filename_warning_accepted = request.form.get("filename_warning_accepted") == "1"
    if not filename_validation["valid"] and not filename_warning_accepted:
        rule = filename_validation["rule"]
        return jsonify({
            "error": "Nama file belum sesuai format.",
            "filename_validation": {
                "scope": filename_validation["scope"],
                "bad_files": filename_validation["bad_files"],
                "sample": rule["sample"],
                "checklist": rule["checklist"],
            },
        }), 400

    if mode_has_boq(output_mode):
        if not has_kml:
            return jsonify({"error": "KML/KMZ file is required for BOQ export."}), 400
        if not boq_template_path.exists():
            return jsonify({"error": f"BOQ {network_type} template not found on server."}), 500
    if mode_has_astri(output_mode):
        if not has_kml:
            return jsonify({"error": "KML/KMZ file is required for ASTRI export."}), 400
        if not hpdb_template_path.exists():
            return jsonify({"error": f"ASTRI {network_type} template not found on server."}), 500
    if mode_has_hpdb(output_mode):
        if not has_kml:
            return jsonify({"error": "KML/KMZ file is required for HPDB export."}), 400
    if mode_has_kmz(output_mode):
        if not has_kml:
            return jsonify({"error": "KML/KMZ file is required for KMZ Standard output."}), 400
        if output_targets_for_mode(output_mode) == {OUTPUT_MODE_KMZ} and (len(files) != 1 or len(kml_files) != 1):
            return jsonify({"error": "KMZ Standard only menerima satu file .kml atau .kmz di upload utama."}), 400

    job_id = uuid.uuid4().hex
    input_paths = []
    original_filenames = []
    osm_admin_defaults = admin_defaults_for_osm_selection(kecamatan_path, osm_city)
    osm_admin_defaults.update(admin_override_fields)
    # Legacy direct overrides remain as fallback; popup admin fields win.
    if override_province and not osm_admin_defaults.get("province"):
        osm_admin_defaults["province"] = override_province
    if override_area_name and not osm_admin_defaults.get("area_name"):
        osm_admin_defaults["area_name"] = override_area_name
        osm_admin_defaults["Area"] = override_area_name
    # Auto-lookup province from city if not overridden
    _city_val = osm_admin_defaults.get("city", "") or osm_admin_defaults.get("regency_city", "")
    if _city_val and not osm_admin_defaults.get("province"):
        _prov = province_for_city(_city_val)
        if _prov and _prov != "Lainnya":
            osm_admin_defaults["province"] = _prov
    osm_admin_defaults = require_admin_defaults_from_region_db(osm_admin_defaults) or {}
    # Re-apply popup overrides at the very end so user-selected values win.
    for _k, _v in admin_override_fields.items():
        if _v:
            osm_admin_defaults[_k] = _v
    if admin_override_fields.get("postal_code"):
        osm_admin_defaults["postalcode"] = admin_override_fields["postal_code"]
        osm_admin_defaults["postal_code"] = admin_override_fields["postal_code"]
    if admin_override_fields:
        osm_admin_defaults["_locked_admin_fields"] = [
            "province", "city", "regency_city", "district",
            "sub_district", "subdistrict", "postal_code", "postalcode",
            "area_name", "Area",
        ]
    dxf_output_path = Config.OUTPUT_DIR / f"{job_id}_{network_type}.dxf"
    boq_output_path = Config.BOQ_OUTPUT_DIR / f"{job_id}_BOQ_{network_type}.xlsx"
    astri_output_path = Config.HPDB_OUTPUT_DIR / f"{job_id}_ASTRI_{network_type}.xlsx"
    hpdb_output_path = Config.HPDB_OUTPUT_DIR / f"{job_id}_HPDB_{network_type}.xlsx"
    standard_kmz_output_path = Config.OUTPUT_DIR / f"{job_id}_STANDARDIZED.kmz"
    zip_output_path = Config.OUTPUT_DIR / f"{job_id}_OUTPUT_{network_type}.zip"
    targets = output_targets_for_mode(output_mode)
    output_path = dxf_output_path
    if is_multi_output_mode(output_mode):
        output_path = zip_output_path
    elif targets == {OUTPUT_MODE_BOQ}:
        output_path = boq_output_path
    elif targets == {OUTPUT_MODE_ASTRI}:
        output_path = astri_output_path
    elif targets == {OUTPUT_MODE_HPDB}:
        output_path = hpdb_output_path
    elif targets == {OUTPUT_MODE_KMZ}:
        output_path = standard_kmz_output_path

    try:
        if kecamatan_path:
            original_filenames.append(kecamatan_path.name)
            input_paths.append(kecamatan_path)
        elif geofabrik_path:
            original_filenames.append(geofabrik_path.name)
            input_paths.append(geofabrik_path)

        for index, file in enumerate(files, start=len(input_paths) + 1):
            original_filenames.append(file.filename)
            safe_name = secure_filename(file.filename) or f"input_{index}{file_suffix(file.filename)}"
            input_path = Config.UPLOAD_DIR / f"{job_id}_{index}_{safe_name}"
            if not input_path.suffix:
                input_path = input_path.with_suffix(file_suffix(file.filename))

            file.save(input_path)
            if input_path.stat().st_size > Config.MAX_FILE_BYTES:
                cleanup_saved_uploads([input_path, *input_paths])
                return jsonify({"error": f"Ukuran tiap file maksimal {Config.MAX_UPLOAD_MB}MB."}), 413
            input_paths.append(input_path)

        names = download_names_for_mode(original_filenames, output_mode, network_type)
        create_job(
            job_id,
            input_paths,
            output_path,
            selected_layers,
            selected_kml_folders,
            road_clip_boundary,
            names["final"],
            output_mode=output_mode,
            dxf_output_path=dxf_output_path,
            boq_output_path=boq_output_path,
            astri_output_path=astri_output_path,
            hpdb_output_path=hpdb_output_path,
            dxf_download_name=names["dxf"],
            boq_download_name=names["boq"],
            astri_download_name=names["astri"],
            hpdb_download_name=names["hpdb"],
            download_mimetype=download_mimetype_for_mode(output_mode),
            network_type=network_type,
            scope_type=scope_type,
        )
        update_job(
            job_id,
            original_filenames=original_filenames,
            osm_admin_defaults=osm_admin_defaults,
            standard_kmz_output_path=str(standard_kmz_output_path),
            standard_kmz_download_name=names["kmz"],
        )
        upload_logger = create_job_logger(job_id)
        total_upload_size = sum(path.stat().st_size for path in input_paths)
        upload_logger.info(f"Upload accepted: {len(input_paths)} file(s), {format_bytes(total_upload_size)} total.")
        if kecamatan_path:
            upload_logger.info(f"Using OSM kecamatan: {kecamatan_path.name}")
            if osm_admin_defaults:
                upload_logger.info(
                    "HPDB/ASTRI admin locked from dropdown: "
                    f"district={osm_admin_defaults.get('district')}, "
                    f"city={osm_admin_defaults.get('city') or osm_admin_defaults.get('regency_city') or '-'}"
                )
        elif geofabrik_path:
            upload_logger.info(f"Using Geofabrik region: {geofabrik_path.name}")
        upload_logger.info(f"Output mode selected: {output_mode}.")
        upload_logger.info(f"Network type selected: {network_type_label(network_type)}.")
        upload_logger.info(
            "ROAD clipping preference: "
            + ("BOUNDARY CLUSTER" if road_clip_boundary else "all roads from OSM")
            + "."
        )
        if mode_has_boq(output_mode):
            upload_logger.info(
                f"BOQ {network_type} template status: "
                + ("Ready." if boq_template_path.exists() else "Missing.")
            )
        if mode_has_astri(output_mode):
            upload_logger.info(
                f"ASTRI {network_type} template status: "
                + ("Ready." if hpdb_template_path.exists() else "Missing.")
            )
        if selected_kml_folders:
            upload_logger.info(f"KML folder filter enabled: {len(selected_kml_folders)} folder(s) selected.")
        if has_kml:
            upload_logger.info("KMZ auto-standardizer will run before export.")
        record_job_created(total_upload_size, output_mode, network_type)
        for original_filename in original_filenames:
            record_upload_file(file_suffix(original_filename).lstrip(".") or "unknown")
        queue_backend = enqueue_conversion_job(job_id, run_conversion_job)
        upload_logger.success(f"Files staged. Conversion worker queued via {queue_backend}.")
        LOGGER.info("Upload diterima untuk job %s: %s file", job_id, len(input_paths))
        return jsonify({"job_id": job_id, "file_count": len(input_paths)})
    except RuntimeError as exc:
        LOGGER.exception("Upload gagal")
        cleanup_saved_uploads(input_paths)
        return jsonify({"error": f"Upload gagal: {exc}"}), 503
    except Exception as exc:
        LOGGER.exception("Upload gagal")
        cleanup_saved_uploads(input_paths)
        return jsonify({"error": f"Upload gagal: {exc}"}), 500


@app.route("/standardize-kmz", methods=["POST"])
def standardize_kmz_upload():
    kmz_file = request.files.get("kmz_file")

    if not kmz_file or not kmz_file.filename:
        return jsonify({"error": "File KMZ/KML belum dipilih."}), 400

    kmz_name = kmz_file.filename
    if not kmz_name.lower().endswith((".kml", ".kmz")):
        return jsonify({"error": "Standardizer hanya menerima input .kml atau .kmz."}), 400

    job_id = uuid.uuid4().hex
    saved_paths: list[Path] = []
    input_path = Config.UPLOAD_DIR / f"{job_id}_standardize_{secure_filename(kmz_name) or f'input{file_suffix(kmz_name)}'}"
    output_path = Config.OUTPUT_DIR / f"{job_id}_STANDARDIZED.kmz"
    download_name = standard_kmz_download_name(kmz_name)
    standardize_started_at = time.perf_counter()

    try:
        kmz_file.save(input_path)
        saved_paths.append(input_path)
        if input_path.stat().st_size > Config.MAX_FILE_BYTES:
            cleanup_saved_uploads(saved_paths)
            return jsonify({"error": f"Ukuran tiap file maksimal {Config.MAX_UPLOAD_MB}MB."}), 413
        record_job_created(input_path.stat().st_size, "kmz", Config.DEFAULT_NETWORK_TYPE)
        record_upload_file(file_suffix(kmz_name).lstrip(".") or "unknown")

        create_job(
            job_id,
            [input_path],
            output_path,
            [],
            [],
            True,
            download_name,
            output_mode="kmz",
            download_mimetype="application/vnd.google-earth.kmz",
        )
        standardize_logger = create_job_logger(job_id)
        standardize_logger.info(f"KMZ standardizer upload accepted: {kmz_name}.")
        update_job(job_id, status="processing", progress=20, message="Membaca KMZ dan template coding FDT.", started_at=time.time())
        coding_records = None
        if fdt_coding_source_ready():
            standardize_logger.info(f"FDT coding source: {fdt_coding_source_label()}.")
            coding_records = resolve_fdt_coding_records(standardize_logger)
            try:
                coding_path = None if coding_records else resolve_fdt_coding_workbook(job_id, standardize_logger)
                if coding_path and coding_path.parent == Config.UPLOAD_DIR:
                    saved_paths.append(coding_path)
            except Exception as exc:
                standardize_logger.warn(fdt_coding_runtime_warning(str(exc)))
                coding_path = None
        else:
            standardize_logger.warn(fdt_coding_runtime_warning())
            coding_path = None

        result = standardize_kmz_file(input_path, coding_path, output_path, network_type=Config.DEFAULT_NETWORK_TYPE, coding_records=coding_records, source_name=kmz_name)
        result["source_name"] = kmz_name
        if isinstance(result.get("validation_report"), dict):
            result["validation_report"]["source_name"] = kmz_name
        for change in result.get("fdt_changes", []):
            standardize_logger.success(
                f"FDT {change.get('fdt_order')}: {change.get('from')} -> {change.get('to')} "
                f"({change.get('source_sheet')} row {change.get('source_row')})."
            )
        if result.get("folder_renamed"):
            standardize_logger.info(f"Folder standardized: {result.get('folder_renamed')} folder name(s).")
        if result.get("folder_unwrapped"):
            standardize_logger.info(f"Wrapper folder removed: {result.get('folder_unwrapped')} folder(s).")
        if result.get("name_standardized"):
            standardize_logger.success(f"Object names standardized: {result.get('name_standardized')} placemark name(s).")
        if result.get("placemark_organized"):
            standardize_logger.success(f"Objects organized into standard folders: {result.get('placemark_organized')} placemark(s).")
        if result.get("distribution_cable_standardized"):
            standardize_logger.success(f"Distribution cable descriptions standardized: {result.get('distribution_cable_standardized')} line(s).")
        if result.get("style_standardized"):
            standardize_logger.success(f"KMZ styles standardized: {result.get('style_standardized')} placemark style(s).")
        for warning in result.get("warnings", []):
            standardize_logger.warn(warning)

        message = (
            f"KMZ standar siap: {int(result.get('fdt_renamed', 0))} FDT code updated, "
            f"{int(result.get('name_standardized', 0))} object names fixed, "
            f"{int(result.get('placemark_organized', 0))} objects moved, "
            f"{int(result.get('distribution_cable_standardized', 0))} cable description fixed, "
            f"{int(result.get('folder_renamed', 0))} folder fixed, "
            f"{int(result.get('folder_unwrapped', 0))} wrapper removed, "
            f"{int(result.get('style_standardized', 0))} style fixed."
        )
        standardize_duration = time.perf_counter() - standardize_started_at
        update_job(
            job_id,
            status="done",
            progress=100,
            message=message,
            finished_at=time.time(),
            duration_seconds=standardize_duration,
            stats={
                "KML": int(result.get("fdt_placemarks", 0)),
                "FDT": int(result.get("fdt_renamed", 0)),
                "NAMES": int(result.get("name_standardized", 0)),
                "ORGANIZED": int(result.get("placemark_organized", 0)),
                "STYLES": int(result.get("style_standardized", 0)),
                "BOQ_WARNINGS": len(result.get("warnings", [])),
            },
            standardizer_summary=result,
        )
        record_job_completed(job_id, "kmz", standardize_duration, Config.DEFAULT_NETWORK_TYPE)
        payload = job_status_payload(get_job(job_id) or {})
        payload["standardizer_summary"] = result
        return jsonify(payload)
    except Exception as exc:
        LOGGER.exception("KMZ standardizer gagal")
        if get_job(job_id):
            update_job(
                job_id,
                status="error",
                progress=100,
                message="Standardizer gagal.",
                error=str(exc),
                finished_at=time.time(),
                duration_seconds=time.perf_counter() - standardize_started_at,
            )
            create_job_logger(job_id).error(str(exc))
            record_job_failed(job_id, "kmz", Config.DEFAULT_NETWORK_TYPE)
            alert_job_failed(job_id, str(exc), time.perf_counter() - standardize_started_at)
        else:
            cleanup_saved_uploads(saved_paths)
        return jsonify({"error": f"Standardizer gagal: {exc}"}), 500


def revoke_celery_task(task_id: str) -> bool:
    try:
        from tasks import celery_app

        celery_app.control.revoke(task_id, terminate=True)
        return True
    except Exception as exc:
        LOGGER.warning("Gagal revoke Celery task %s: %s", task_id, exc)
        return False

@app.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404

    status_value = str(job.get("status", ""))
    if status_value in {"done", "error", "failed"}:
        return jsonify({"error": "Job sudah selesai atau gagal.", "status": status_value}), 409

    if status_value != "cancelled":
        update_job(
            job_id,
            status="cancelled",
            message="Cancelled by user.",
            finished_at=time.time(),
            error=None,
        )
        task_id = job.get("task_id")
        if task_id:
            revoke_celery_task(str(task_id))

    return jsonify({"status": "cancelled"})

@app.route("/status/<job_id>")
def status(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return jsonify(job_status_payload(job))


@app.route("/jobs/<job_id>/download")
def download_file_compat(job_id: str):
    """Backward compat: redirect old /jobs/<id>/download to /download/<id>."""
    return redirect(f"/download/{job_id}")



def job_file_map(job: dict) -> dict[str, Path]:
    """Map output download names to their file paths."""
    mapping: dict[str, Path] = {}
    if job.get("dxf_download_name") and job.get("dxf_output_path"):
        mapping[job["dxf_download_name"]] = Path(job["dxf_output_path"])
    if job.get("boq_download_name") and job.get("boq_output_path"):
        mapping[job["boq_download_name"]] = Path(job["boq_output_path"])
    if not job.get("astri_output_files") and job.get("astri_download_name") and job.get("astri_output_path"):
        mapping[job["astri_download_name"]] = Path(job["astri_output_path"])
    for item in job.get("astri_output_files") or []:
        if isinstance(item, dict) and item.get("download_name") and item.get("path"):
            mapping[item["download_name"]] = Path(item["path"])
    if job.get("hpdb_download_name") and job.get("hpdb_output_path"):
        mapping[job["hpdb_download_name"]] = Path(job["hpdb_output_path"])
    if job.get("download_name") and job.get("output_path"):
        mapping[job["download_name"]] = Path(job["output_path"])
    for entry in job.get("standard_kmz_outputs") or []:
        name = entry.get("download_name")
        path = entry.get("path")
        if name and path:
            mapping[name] = Path(path)
    return mapping


@app.route("/download/<job_id>")
def download_file(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    if job["status"] != "done":
        return jsonify({"error": "File output belum siap diunduh."}), 409

    output_path = Path(job["output_path"])
    if not is_safe_output_path(output_path):
        return jsonify({"error": "File output berada di path yang tidak diizinkan."}), 403
    # Authentication and job ownership were checked before any object access.
    # Retry only the mirror on GET; never rerun conversion or upload on HEAD.
    mirror_metadata_failed = False
    if request.method == "GET" and job.get("r2_upload_status") == "failed" and output_path.is_file():
        mirror = publish_job_output(job)
        try:
            saved = _system_update_job(job_id, **mirror)
            if saved is None:
                raise RuntimeError("Job disappeared during mirror retry")
            job = {**job, **mirror}
        except Exception as exc:
            LOGGER.warning("r2_metadata_retry_failed job_id=%s error_type=%s", job_id, type(exc).__name__)
            mirror_metadata_failed = True
    remote_url = None if mirror_metadata_failed else job_download_url(job, method=request.method)
    if remote_url:
        response = redirect(remote_url, code=302)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    if not output_path.exists():
        return jsonify({"error": "File output sudah tidak tersedia. Silakan proses ulang."}), 410

    return send_file(
        output_path,
        as_attachment=True,
        download_name=job.get("download_name", f"osm-to-dxf-merged-{job_id}.dxf"),
        mimetype=job.get("download_mimetype", "application/dxf"),
    )




@app.route("/admin/jobs/<job_id>/download-file")
def download_single_file(job_id: str):
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    file_name = request.args.get("name", "").strip()
    if not file_name:
        return jsonify({"error": "Parameter 'name' diperlukan."}), 400
    file_map = job_file_map(job)
    if file_name not in file_map:
        available = list(file_map.keys())
        return jsonify({"error": f"File tidak ditemukan. Tersedia: {available}"}), 404
    file_path = file_map[file_name]
    if not is_safe_output_path(file_path):
        return jsonify({"error": "File berada di path yang tidak diizinkan."}), 403
    if not file_path.exists():
        return jsonify({"error": "File sudah tidak tersedia."}), 410
    return send_file(
        file_path,
        as_attachment=True,
        download_name=file_name,
    )


@app.route("/preview/<job_id>")
def preview(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    return jsonify(job.get("preview_geojson", {"type": "FeatureCollection", "features": []}))


def _wants_json() -> bool:
    """Check if the client prefers JSON response (API call)."""
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if request.path.startswith(("/api/", "/convert", "/job/", "/jobs", "/status/", "/metrics/")):
        return True
    return False


def _error_context(error_code: int, title: str, message: str, detail: str = "") -> dict:
    return {
        "error_code": error_code,
        "error_title": title,
        "error_message": message,
        "error_detail": detail,
        "request_id": getattr(g, "request_id", None),
    }


@app.errorhandler(400)
def handle_bad_request(error):
    if _wants_json():
        return jsonify({"error": str(error.description or "Request tidak valid.")}), 400
    ctx = _error_context(400, "Request Tidak Valid", str(error.description or "Permintaan yang dikirim tidak valid atau tidak lengkap."))
    return render_template("error.html", **ctx), 400


@app.errorhandler(403)
def handle_forbidden(error):
    if _wants_json():
        return jsonify({"error": str(error.description or "Akses ditolak.")}), 403
    ctx = _error_context(403, "Akses Ditolak", str(error.description or "Anda tidak memiliki izin untuk mengakses halaman ini."))
    return render_template("error.html", **ctx), 403


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(_error):
    message = f"Total upload maksimal {Config.MAX_BATCH_UPLOAD_MB}MB. Tiap file maksimal {Config.MAX_UPLOAD_MB}MB."
    if _wants_json():
        return jsonify({"error": message}), 413
    ctx = _error_context(413, "File Terlalu Besar", message, f"Batas upload: {Config.MAX_UPLOAD_MB}MB per file, {Config.MAX_BATCH_UPLOAD_MB}MB per batch.")
    return render_template("error.html", **ctx), 413


@app.errorhandler(404)
def handle_not_found(_error):
    if _wants_json():
        return jsonify({"error": "Endpoint tidak ditemukan."}), 404
    ctx = _error_context(404, "Halaman Tidak Ditemukan", f"URL <code>{request.path}</code> tidak ditemukan di server ini.")
    return render_template("error.html", **ctx), 404


@app.errorhandler(429)
def handle_rate_limit(error):
    if _wants_json():
        return jsonify({"error": "Terlalu banyak request. Coba lagi nanti."}), 429
    ctx = _error_context(429, "Terlalu Banyak Request", "Anda telah melebihi batas request. Silakan tunggu beberapa saat sebelum mencoba lagi.")
    return render_template("error.html", **ctx), 429


@app.errorhandler(500)
def handle_internal_error(error):
    LOGGER.exception("Internal server error: %s", error)
    if _wants_json():
        return jsonify({"error": "Terjadi kesalahan internal di server."}), 500
    ctx = _error_context(500, "Kesalahan Server", "Terjadi kesalahan tak terduga di server. Tim kami sudah diberitahu dan sedang menangani masalah ini.")
    return render_template("error.html", **ctx), 500


start_cleanup_scheduler(
    upload_dir=Config.UPLOAD_DIR,
    output_dir=Config.OUTPUT_DIR,
    max_age_seconds=Config.CLEANUP_MAX_AGE_SECONDS,
    interval_seconds=Config.CLEANUP_INTERVAL_SECONDS,
    cleanup_jobs_callback=lambda: cleanup_jobs(Config.JOB_MAX_AGE_SECONDS),
    stale_recovery_callback=lambda: recover_stale_jobs(Config.JOB_STALE_THRESHOLD_SECONDS),
    stale_check_interval_seconds=Config.JOB_STALE_CHECK_INTERVAL_SECONDS,
    extra_directories=[(Config.ASTRI_DOWNLOAD_DIR, Config.ASTRI_DOWNLOAD_MAX_AGE_SECONDS)],
)


OUTBOUND_SYNC_ENABLED = os.getenv("OUTBOUND_SYNC_ENABLED", "true").lower() in {"1", "true", "yes"}


def _bg_sync_create_records(record_ids: list[int]) -> None:
    if not OUTBOUND_SYNC_ENABLED or not record_ids:
        return
    try:
        threading.Thread(
            target=fdt_sync_service.sync_outbound_create_records,
            args=(record_ids,),
            daemon=True,
        ).start()
    except Exception:
        LOGGER.exception("Background outbound sync create records gagal dijadwalkan.")

class FdtSpreadsheetSyncError(RuntimeError):
    """Raised when a DB edit cannot be reconciled to its source spreadsheet."""


def _sync_source_after_update(record_id: int, before: dict, after: dict) -> bool:
    """Reconcile every source-backed edit before returning success to the UI."""
    if not before or not before.get("source_spreadsheet_id") or int(before.get("source_row", 0) or 0) < 1:
        return False
    if not OUTBOUND_SYNC_ENABLED:
        raise FdtSpreadsheetSyncError("Outbound sync FDT nonaktif; perubahan dibatalkan.")
    if not fdt_sync_service.sync_outbound_update_record(
        record_id,
        record_snapshot=before,
    ):
        raise FdtSpreadsheetSyncError(
            "Perubahan gagal disinkronkan ke spreadsheet; database dikembalikan."
        )
    return True


def _bg_sync_update_record(record_id: int, record_snapshot: dict | None = None) -> None:
    if not OUTBOUND_SYNC_ENABLED:
        return
    try:
        threading.Thread(
            target=fdt_sync_service.sync_outbound_update_record,
            args=(record_id,),
            kwargs={"record_snapshot": record_snapshot},
            daemon=True,
        ).start()
    except Exception:
        LOGGER.exception("Background outbound sync update record %d gagal dijadwalkan.", record_id)

def _bg_sync_delete_record(record_id: int, record_snapshot: dict | None = None) -> None:
    if not OUTBOUND_SYNC_ENABLED:
        return
    try:
        threading.Thread(
            target=fdt_sync_service.sync_outbound_delete_record,
            args=(record_id,),
            kwargs={"record_snapshot": record_snapshot},
            daemon=True,
        ).start()
    except Exception:
        LOGGER.exception("Background outbound sync delete record %d gagal dijadwalkan.", record_id)

def _bg_sync_delete_records(record_snapshots: dict) -> None:
    """Sequential batch delete with delay to avoid 429 quota."""
    if not OUTBOUND_SYNC_ENABLED or not record_snapshots:
        return
    try:
        threading.Thread(
            target=fdt_sync_service.sync_outbound_delete_records,
            args=(dict(record_snapshots),),
            daemon=True,
        ).start()
    except Exception:
        LOGGER.exception("Background outbound sync batch delete gagal dijadwalkan.")




INBOUND_SYNC_ENABLED = os.getenv("INBOUND_SYNC_ENABLED", "true").lower() in {"1", "true", "yes"}
if INBOUND_SYNC_ENABLED:
    try:
        fdt_sync_service.start_inbound_sync_scheduler()
    except Exception:
        LOGGER.exception("Gagal memulai inbound sync scheduler.")


@app.route("/fdt-coding")
def fdt_coding_admin_page():
    if is_dashboard_host():
        protected = dashboard_admin_required()
        if protected is not None:
            return protected
    return render_template("fdt_coding.html", db_path=str(Config.FDT_CODING_DB_PATH))

@app.route("/api/fdt-coding/summary")
def api_fdt_coding_summary():
    try:
        area = request.args.get("area", "").strip()
        total = fdt_coding_db.count_matching_records(Config.FDT_CODING_DB_PATH, area=area)
        olts = fdt_coding_db.list_olts(Config.FDT_CODING_DB_PATH, area=area or None)
        return jsonify({"total": total, "olts": olts, "source_label": fdt_coding_source_label()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/fdt-coding/shareable-fdt")
def api_fdt_coding_shareable_fdt():
    try:
        area = request.args.get("area", "")
        olt = request.args.get("olt", "")
        if not area or not olt:
            return jsonify({"records": []})
        records = fdt_coding_db.list_shareable_fdt(
            Config.FDT_CODING_DB_PATH, area, olt, request.args.get("q", "")
        )
        return jsonify({"records": records})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/available-fdt-ids")
def api_fdt_coding_available_fdt_ids():
    try:
        ids = fdt_coding_db.available_fdt_ids(
            Config.FDT_CODING_DB_PATH,
            olt_initial=request.args.get("olt", ""),
            count=int(request.args.get("count", 1)),
        )
        return jsonify({"ids": ids})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/fdt-coding/olts", methods=["POST"])
def api_fdt_coding_create_olt():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    sheet_result = None
    try:
        with fdt_sync_service.fdt_sync_operation_lock():
            payload = request.get_json(silent=True) or {}
            olt = fdt_coding_db.prepare_olt(Config.FDT_CODING_DB_PATH, payload)
            if not OUTBOUND_SYNC_ENABLED:
                return jsonify({"error": "Outbound sync FDT nonaktif; OLT tidak disimpan."}), 503

            try:
                sheet_result = fdt_sync_service.create_olt_sheet_from_template(olt)
            except fdt_sync_service.OltSheetRollbackError:
                raise
            except Exception as sheet_exc:
                raise FdtSpreadsheetSyncError(
                    f"Spreadsheet OLT gagal diverifikasi: {sheet_exc}"
                ) from sheet_exc
            if sheet_result.get("verified") is not True:
                raise FdtSpreadsheetSyncError("Spreadsheet OLT gagal diverifikasi.")

            try:
                olt = fdt_coding_db.create_olt(Config.FDT_CODING_DB_PATH, olt)
            except Exception as db_exc:
                if sheet_result.get("rollback"):
                    try:
                        fdt_sync_service.rollback_olt_sheet_creation(sheet_result)
                    except Exception as rollback_exc:
                        raise fdt_sync_service.OltSheetRollbackError(
                            f"Database OLT gagal dan rollback spreadsheet perlu diperiksa: {rollback_exc}"
                        ) from db_exc
                raise

        public_sheet = {key: value for key, value in sheet_result.items() if key != "rollback"}
        return jsonify({"olt": olt, "sheet": public_sheet}), 201
    except fdt_sync_service.OltSheetRollbackError as exc:
        LOGGER.exception("Rollback spreadsheet Tambah OLT gagal; rekonsiliasi manual diperlukan.")
        return jsonify({
            "error": str(exc),
            "code": "FDT_OLT_ROLLBACK_FAILED",
            "reconciliation_required": True,
        }), 500
    except FdtSpreadsheetSyncError as exc:
        return jsonify({"error": str(exc), "code": "FDT_OLT_SHEET_FAILED"}), 502
    except Exception as exc:
        LOGGER.exception("Gagal membuat OLT Coding FDT.")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/olts/<path:olt_initial>", methods=["DELETE"])
def api_fdt_coding_delete_olt(olt_initial: str):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        olt_snapshot = next((olt for olt in fdt_coding_db.list_olts(Config.FDT_CODING_DB_PATH) if olt.get("olt_initial") == fdt_coding_db.normalize_text(olt_initial).upper()), None)
        deleted = fdt_coding_db.delete_olt(
            Config.FDT_CODING_DB_PATH,
            olt_initial,
            force=request.args.get("force", "0") == "1",
        )
        if not deleted:
            return jsonify({"error": "OLT tidak ditemukan"}), 404
        sheet_result = {"deleted": False, "reason": "outbound_sync_disabled"}
        if OUTBOUND_SYNC_ENABLED:
            try:
                sheet_result = fdt_sync_service.delete_olt_sheet(olt_initial, (olt_snapshot or {}).get("hub_id", ""))
            except Exception as sheet_exc:
                LOGGER.exception("Gagal hapus sheet OLT %s.", olt_initial)
                sheet_result = {"deleted": False, "error": str(sheet_exc)}
        return jsonify({"deleted": True, "sheet": sheet_result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/records")
def api_fdt_coding_records():
    try:
        olt_initial = request.args.get("olt", "")
        query = request.args.get("q", "")
        area = request.args.get("area", "")
        limit = int(request.args.get("limit", 250))
        offset = int(request.args.get("offset", 0))
        records = fdt_coding_db.list_records(
            Config.FDT_CODING_DB_PATH,
            olt_initial=olt_initial,
            query=query,
            area=area,
            limit=limit,
            offset=offset,
        )
        total = fdt_coding_db.count_matching_records(
            Config.FDT_CODING_DB_PATH,
            olt_initial=olt_initial,
            query=query,
            area=area,
        )
        return jsonify({"records": records, "total": total, "limit": limit, "offset": offset})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/fdt-coding/records", methods=["POST"])
def api_fdt_coding_create_record():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        if "shared_fdt_id" in payload:
            if not OUTBOUND_SYNC_ENABLED:
                return jsonify({"error": "Outbound sync FDT nonaktif; sharing tidak disimpan."}), 503
            with fdt_sync_service.fdt_sync_operation_lock():
                records = fdt_coding_db.create_records(Config.FDT_CODING_DB_PATH, payload)
                record_id = records[0]["id"]
                try:
                    fdt_sync_service.sync_outbound_create_shared_record(
                        record_id, payload["shared_fdt_id"]
                    )
                except fdt_sync_service.SharedFdtSyncError as exc:
                    if not exc.reconciliation_required:
                        fdt_coding_db.delete_record(Config.FDT_CODING_DB_PATH, record_id)
                    return jsonify({
                        "error": (f"Baris {record_id} mungkin sudah tertulis ke spreadsheet; jangan ulangi simpan. "
                                  f"Periksa rekonsiliasi: {exc}") if exc.reconciliation_required else str(exc),
                        "reconciliation_required": exc.reconciliation_required,
                        "record_id": record_id if exc.reconciliation_required else None,
                    }), 502
                records = [fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id)]
        else:
            records = fdt_coding_db.create_records(Config.FDT_CODING_DB_PATH, payload)
            _bg_sync_create_records([rec["id"] for rec in records])
        return jsonify({"record": records[0] if records else None, "records": records}), 201
    except FdtSpreadsheetSyncError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/records/bulk", methods=["PUT"])
def api_fdt_coding_bulk_update_records():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    before_map = {}
    try:
        payload = request.get_json(silent=True) or {}
        ids = [int(record_id) for record_id in (payload.get("ids") or [])]
        before_map = {record_id: fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id) for record_id in ids}
        move_ids = [
            record_id
            for record_id, before in before_map.items()
            if before and fdt_sync_service.record_requires_sheet_move({**before, **payload}, before)
        ]
        if move_ids:
            return jsonify({"error": "Ganti OLT pada edit massal tidak diizinkan; edit satu record agar pemindahan sheet atomik.", "move_ids": move_ids}), 400
        records = fdt_coding_db.update_records(Config.FDT_CODING_DB_PATH, ids, payload)
        moved = []
        for rec in records:
            record_id = int(rec["id"])
            before = before_map.get(record_id) or {}
            if fdt_sync_service.record_requires_sheet_move(rec, before):
                if not OUTBOUND_SYNC_ENABLED or not fdt_sync_service.sync_outbound_update_record(record_id, record_snapshot=before):
                    raise FdtSpreadsheetSyncError(f"Record {record_id} gagal dipindahkan ke sheet target.")
                moved.append(record_id)
            else:
                _bg_sync_update_record(record_id, before)
        return jsonify({"records": [fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, int(r["id"])) or r for r in records], "updated": len(records), "moved": moved})
    except FdtSpreadsheetSyncError as exc:
        for snapshot in before_map.values():
            try:
                fdt_coding_db.restore_record_snapshot(Config.FDT_CODING_DB_PATH, snapshot)
            except Exception:
                LOGGER.exception("Rollback DB bulk FDT gagal setelah outbound move gagal.")
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/records/bulk", methods=["DELETE"])
def api_fdt_coding_bulk_delete_records():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        ids = payload.get("ids") or []
        snapshots = {int(record_id): fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, int(record_id)) for record_id in ids}
        deleted = fdt_coding_db.delete_records(Config.FDT_CODING_DB_PATH, ids)
        _bg_sync_delete_records(snapshots)
        return jsonify({"deleted": deleted})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@app.route("/api/fdt-coding/records/<int:record_id>", methods=["PUT"])
def api_fdt_coding_update_record(record_id: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    before = None
    updated = False
    try:
        with fdt_sync_service.fdt_sync_operation_lock():
            before = fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id)
            record = fdt_coding_db.update_record(Config.FDT_CODING_DB_PATH, record_id, request.get_json(silent=True) or {})
            updated = True
            _sync_source_after_update(record_id, before, record)
            response_record = fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id) or record
        return jsonify({"record": response_record})
    except FdtSpreadsheetSyncError as exc:
        if updated and before:
            try:
                fdt_coding_db.restore_record_snapshot(Config.FDT_CODING_DB_PATH, before)
            except Exception:
                LOGGER.exception("Rollback DB FDT gagal setelah outbound move gagal: %d", record_id)
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        if updated and before:
            try:
                fdt_coding_db.restore_record_snapshot(Config.FDT_CODING_DB_PATH, before)
            except Exception:
                LOGGER.exception("Rollback DB FDT gagal setelah update gagal: %d", record_id)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/records/<int:record_id>", methods=["DELETE"])
def api_fdt_coding_delete_record(record_id: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        snapshot = fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id)
        deleted = fdt_coding_db.delete_record(Config.FDT_CODING_DB_PATH, record_id)
        if not deleted:
            return jsonify({"error": "Record tidak ditemukan"}), 404
        _bg_sync_delete_record(record_id, snapshot)
        return jsonify({"deleted": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@app.route("/api/fdt-coding/import", methods=["POST"])
def api_fdt_coding_import():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    saved_path = None
    try:
        replace = request.form.get("replace", "1") != "0"
        upload = request.files.get("file")
        if upload and upload.filename:
            if not upload.filename.lower().endswith(".xlsx"):
                return jsonify({"error": "Import hanya menerima .xlsx"}), 400
            safe_name = secure_filename(upload.filename) or "fdt_coding.xlsx"
            saved_path = Config.UPLOAD_DIR / f"fdt_import_{uuid.uuid4().hex}_{safe_name}"
            upload.save(saved_path)
            workbook_path = saved_path
        else:
            workbook_path = resolve_fdt_coding_workbook(f"fdt-import-{uuid.uuid4().hex}")
        imported = fdt_coding_db.import_workbook(Config.FDT_CODING_DB_PATH, workbook_path, replace=replace)
        return jsonify({"imported": imported, "replace": replace, "db_path": str(Config.FDT_CODING_DB_PATH)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        if saved_path:
            cleanup_saved_uploads([saved_path])


@app.route("/api/fdt-coding/sync", methods=["POST"])
def api_fdt_coding_sync():
    """Force an inbound sync (spreadsheet -> DB) on demand.

    Normally the background scheduler keeps the DB fresh and only fetches
    cell values when the Drive modifiedTime changed. This endpoint lets the
    UI trigger an immediate refresh (force=True bypasses the modifiedTime
    gate) without waiting for the next poll. It still updates the DB only;
    the page keeps reading from the DB."""
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    if not INBOUND_SYNC_ENABLED:
        return jsonify({"error": "Inbound sync dinonaktifkan (INBOUND_SYNC_ENABLED=false)."}), 400
    force = (request.args.get("force", "0") == "1") or bool((request.get_json(silent=True) or {}).get("force"))
    try:
        results = fdt_sync_service.sync_all_incremental(force=force)
        changed = sum(int(v.get("changed", 0)) for v in results.values())
        return jsonify({
            "synced": True,
            "force": force,
            "changed": changed,
            "results": results,
            "total": fdt_coding_db.count_records(Config.FDT_CODING_DB_PATH),
        })
    except Exception as exc:
        # Surface quota errors clearly so the UI can show a friendly note
        # instead of a raw 500.
        if fdt_sync_service._is_google_quota_error(exc):
            return jsonify({"error": "Google Sheets quota tercapai, coba lagi sebentar lagi.", "quota": True}), 429
        return jsonify({"error": str(exc)}), 500


@app.route("/coreman")
def coreman_dashboard_page():
    return render_template("coreman.html", db_path=str(Config.COREMAN_DB_PATH), fdt_db_path=str(Config.FDT_CODING_DB_PATH))


@app.route("/api/coreman/summary")
def api_coreman_summary():
    try:
        return jsonify({
            "summary": coreman_db.dashboard_summary(Config.COREMAN_DB_PATH),
            "batches": coreman_db.list_opm_batches(Config.COREMAN_DB_PATH, limit=20),
            "aliases": coreman_db.list_aliases(Config.COREMAN_DB_PATH),
            "sheets": coreman_db.list_sheets(Config.COREMAN_DB_PATH),
            "logs": coreman_db.list_audit_logs(Config.COREMAN_DB_PATH, limit=25),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/batches")
def api_coreman_batches():
    try:
        return jsonify({"batches": coreman_db.list_opm_batches(Config.COREMAN_DB_PATH, status=request.args.get("status", ""), limit=int(request.args.get("limit", 100)))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def coreman_fdt_number_matches(record: dict, fdt_number: str) -> bool:
    """Match user-facing FDT number from OPM text to FDT coding records.

    OPM reports usually say `FDT.3`, while Coding FDT stores the generated
    code as something like `PPRMT.099`. The correct match is the FDT order in
    the cluster (`fdt_order=3`) or the `- FDT 3` suffix in site_name, not the
    generated FDT ID `099`.
    """
    raw = str(fdt_number or "").strip()
    if not raw:
        return True
    match = re.search(r"\d+", raw)
    if not match:
        return True
    wanted = int(match.group(0))
    try:
        if int(record.get("fdt_order") or 0) == wanted:
            return True
    except (TypeError, ValueError):
        pass
    site_name = str(record.get("site_name") or "").upper()
    return bool(re.search(rf"\bFDT\s*[.\-]?\s*0*{wanted}\b", site_name))


def coreman_cluster_search_terms(cluster_name: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(cluster_name or "").strip()).upper()
    if not raw:
        return []
    terms = [raw]
    cleaned = re.sub(r"^(?:CLUSTER|PERUMAHAN|PERUM)\s+", "", raw).strip()
    if cleaned and cleaned not in terms:
        terms.append(cleaned)
    return terms


@app.route("/api/coreman/batches", methods=["POST"])
def api_coreman_create_batch():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        cluster_name = str(payload.get("cluster_name") or "").strip()
        fdt_number = str(payload.get("fdt_number") or "").strip()
        fdt_numbers = [str(value).strip() for value in (payload.get("fdt_numbers") or []) if str(value).strip()]
        if fdt_number and fdt_number not in fdt_numbers:
            fdt_numbers.append(fdt_number)
        matched_records = []
        if cluster_name:
            seen_ids = set()
            for term in coreman_cluster_search_terms(cluster_name):
                for record in fdt_coding_db.list_records(Config.FDT_CODING_DB_PATH, query=term, limit=250, offset=0):
                    site_name = str(record.get("site_name") or "").upper()
                    if term in site_name and record.get("id") not in seen_ids:
                        matched_records.append(record)
                        seen_ids.add(record.get("id"))
        if fdt_numbers:
            filtered_records = [
                record for record in matched_records
                if any(coreman_fdt_number_matches(record, number) for number in fdt_numbers)
            ]
            matched_records = filtered_records or (matched_records if len(matched_records) == 1 else [])
        batch = coreman_db.create_opm_batch(Config.COREMAN_DB_PATH, {
            **payload,
            "matched_fdt_records": matched_records,
            "detected_fdt_count": len(matched_records),
            "detected_fdt_labels": [f"FDT {record.get('fdt_order') or record.get('fdt_id', '')}" for record in matched_records],
            "status": payload.get("status") or ("waiting_mancore" if matched_records else "needs_confirmation"),
            "waiting_mancore": bool(matched_records),
        })
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {
            "batch_id": batch.get("id"),
            "event_type": "batch_created",
            "actor_name": payload.get("source_sender") or "system",
            "source_chat_id": payload.get("source_chat_id") or "",
            "message_text": payload.get("raw_message") or "",
            "payload": {"matched_count": len(matched_records), "fdt_numbers": fdt_numbers},
        })
        return jsonify({"batch": batch, "matched_records": matched_records}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/batches/<int:batch_id>", methods=["PUT"])
def api_coreman_update_batch(batch_id: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        batch = coreman_db.update_opm_batch(Config.COREMAN_DB_PATH, batch_id, request.get_json(silent=True) or {})
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"batch_id": batch_id, "event_type": "batch_updated", "payload": request.get_json(silent=True) or {}})
        return jsonify({"batch": batch})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/aliases")
def api_coreman_aliases():
    try:
        return jsonify({"aliases": coreman_db.list_aliases(Config.COREMAN_DB_PATH, request.args.get("q", ""))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/aliases", methods=["POST"])
def api_coreman_create_alias():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        alias = coreman_db.create_alias(Config.COREMAN_DB_PATH, request.get_json(silent=True) or {})
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"event_type": "alias_upserted", "payload": alias})
        return jsonify({"alias": alias}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/aliases/<int:alias_id>", methods=["DELETE"])
def api_coreman_delete_alias(alias_id: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        deleted = coreman_db.delete_alias(Config.COREMAN_DB_PATH, alias_id)
        if not deleted:
            return jsonify({"error": "Alias tidak ditemukan"}), 404
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"event_type": "alias_deleted", "payload": {"id": alias_id}})
        return jsonify({"deleted": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/sheets")
def api_coreman_sheets():
    try:
        return jsonify({"sheets": coreman_db.list_sheets(Config.COREMAN_DB_PATH, request.args.get("olt", ""))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/sheets", methods=["POST"])
def api_coreman_upsert_sheet():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        olt_name = str(payload.get("olt_name") or "").strip().upper()
        segment_name = str(payload.get("segment_name") or "").strip().lstrip("0") or "1"
        sheet_name = str(payload.get("sheet_name") or f"{olt_name} - SEG {segment_name}").strip().upper()
        template_name = str(payload.get("template_sheet_name") or coreman_sheets.template_sheet_name()).strip()
        row_count = int(payload.get("row_count") or 144)
        google_sheet = coreman_sheets.ensure_sheet(sheet_name, template_name=template_name, row_count=row_count)
        sheet = coreman_db.upsert_sheet(Config.COREMAN_DB_PATH, {
            **payload,
            "olt_name": olt_name,
            "segment_name": segment_name,
            "sheet_name": google_sheet["title"],
            "spreadsheet_id": coreman_sheets.spreadsheet_id(),
            "template_sheet_name": template_name,
            "sheet_gid": str(google_sheet["gid"]),
            "row_count": google_sheet["row_count"],
            "status": payload.get("status") or "active",
            "last_synced_at": time.time(),
        })
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"event_type": "sheet_upserted", "payload": {"sheet": sheet, "google_sheet": google_sheet}})
        return jsonify({"sheet": sheet, "google_sheet": google_sheet}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/logs")
def api_coreman_logs():
    try:
        return jsonify({"logs": coreman_db.list_audit_logs(Config.COREMAN_DB_PATH, limit=int(request.args.get("limit", 200)))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/reset", methods=["DELETE"])
def api_coreman_reset():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        result = coreman_db.reset_operational_data(
            Config.COREMAN_DB_PATH,
            include_aliases=bool(payload.get("include_aliases", False)),
            include_sheets=bool(payload.get("include_sheets", False)),
        )
        return jsonify({"deleted": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/spreadsheet/sheets")
def api_coreman_spreadsheet_sheets():
    try:
        return jsonify({"spreadsheet_id": coreman_sheets.spreadsheet_id(), "sheets": coreman_sheets.list_sheets()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/spreadsheet/rows")
def api_coreman_spreadsheet_rows():
    try:
        sheet_name = request.args.get("sheet", "").strip()
        if not sheet_name:
            return jsonify({"error": "Parameter sheet wajib diisi"}), 400
        return jsonify(coreman_sheets.get_rows(
            sheet_name,
            limit=int(request.args.get("limit", 400)),
            offset=int(request.args.get("offset", 0)),
        ))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/coreman/spreadsheet/rows/<int:row_number>", methods=["PUT"])
def api_coreman_spreadsheet_update_row(row_number: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        sheet_name = str(payload.get("sheet") or "").strip()
        if not sheet_name:
            return jsonify({"error": "sheet wajib diisi"}), 400
        row = coreman_sheets.update_row(sheet_name, row_number, payload.get("values") or [])
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"event_type": "spreadsheet_row_updated", "payload": {"sheet": sheet_name, "row_number": row_number}})
        return jsonify({"row": row})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/coreman/spreadsheet/rows/<int:row_number>", methods=["DELETE"])
def api_coreman_spreadsheet_clear_row(row_number: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        sheet_name = request.args.get("sheet", "").strip()
        if not sheet_name:
            return jsonify({"error": "Parameter sheet wajib diisi"}), 400
        row = coreman_sheets.clear_assignment(sheet_name, row_number)
        coreman_db.add_audit_log(Config.COREMAN_DB_PATH, {"event_type": "spreadsheet_row_cleared", "payload": {"sheet": sheet_name, "row_number": row_number}})
        return jsonify({"row": row, "cleared": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/rfs")
def rfs_dashboard_page():
    return render_template("rfs.html")


@app.route("/gdr")
def gdr_dashboard_page():
    return render_template("gdr.html")


@app.route("/mancore")
@app.route("/admin/mancore")
def dashboard_mancore_input():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    return render_template("admin_mancore.html")


@app.route("/api/mancore/records", methods=["POST"])
def api_mancore_create_record():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        result = mancore_service.submit_mancore(request.get_json(silent=True) or {})
        return jsonify(result), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ---------- GDR Dashboard API ----------

def _gdr_admin_required():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    if request.host.partition(":")[0].casefold() != "dashboard.dyena.id":
        return jsonify({"error": "Halaman tidak ditemukan."}), 404
    return None


def _gdr_admin_users():
    with EVA_AUTH.store.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,name,email,role,active FROM users ORDER BY name,email")]


def _gdr_send_entry(entry):
    out_dir = (Config.OUTPUT_DIR / "gdr").resolve()
    filename = str(entry.get("file") or "")
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "File not found"}), 404
    target = out_dir / filename
    if target.is_symlink() or not target.resolve().is_relative_to(out_dir) or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=True, download_name=filename)


@app.route("/admin/gdr")
def admin_gdr_page():
    protected = _gdr_admin_required()
    if protected is not None:
        return protected
    return render_template("admin_gdr_vendors.html")


@app.route("/api/dashboard/gdr")
def api_dashboard_gdr():
    protected = _gdr_admin_required()
    if protected is not None:
        return protected
    import gdr_service
    store = gdr_service.private_store()
    return jsonify({"users": _gdr_admin_users(), "vendors": store.all_vendors(),
                    "history": store.all_history(), "operations": store.reservations_for_admin()})


@app.route("/api/dashboard/gdr/vendors/<user_id>/<code>", methods=["PUT", "DELETE"])
def api_dashboard_gdr_vendor(user_id, code):
    protected = _gdr_admin_required()
    if protected is not None:
        return protected
    import gdr_service
    from gdr_store import owner_id
    try:
        uid = owner_id(user_id)
        target_user = next((u for u in _gdr_admin_users() if u["id"] == uid), None)
        if target_user is None:
            return jsonify({"error": "Pengguna tidak ditemukan."}), 404
        store = gdr_service.private_store()
        if request.method == "PUT":
            if target_user["active"] != 1:
                return jsonify({"error": "Pengguna dinonaktifkan."}), 409
            store.put_vendor(target_user, code, request.get_json(silent=True))
        elif request.method == "DELETE":
            if not store.delete_vendor(uid, code):
                return jsonify({"error": "Vendor tidak ditemukan."}), 404
        else:
            return jsonify({"error": "Metode tidak diizinkan."}), 405
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/dashboard/gdr/history/<entry_id>/download")
def api_dashboard_gdr_download(entry_id):
    protected = _gdr_admin_required()
    if protected is not None:
        return protected
    import gdr_service
    entry = gdr_service.private_store().admin_entry(entry_id)
    if entry is None:
        return jsonify({"error": "File not found"}), 404
    return _gdr_send_entry(entry)





@app.route("/api/gdr/vendors")
def api_gdr_vendors():
    import gdr_service
    return jsonify({"vendors": gdr_service.load_vendors(g.eva_user["id"])})


@app.route("/api/gdr/vendors/<code>", methods=["PUT"])
def api_gdr_vendor_save(code):
    import gdr_service
    try:
        store = gdr_service.private_store()
        store.put_vendor(g.eva_user, code, request.get_json(silent=True))
        return jsonify({"ok": True, "count": len(store.vendors(g.eva_user["id"]))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/gdr/vendors/<code>", methods=["DELETE"])
def api_gdr_vendor_delete(code):
    import gdr_service
    try:
        store = gdr_service.private_store()
        if not store.delete_vendor(g.eva_user["id"], code):
            return jsonify({"error": "Vendor tidak ditemukan."}), 404
        return jsonify({"ok": True, "count": len(store.vendors(g.eva_user["id"]))})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400



@app.route("/api/gdr/next-number", methods=["GET"])
def api_gdr_next_number():
    import gdr_service
    try:
        info = gdr_service.get_next_gdr_number()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
def _gdr_payload(data):
    """Validate all workbook/provider inputs before issuing a number."""
    import math
    def text(value, limit):
        if not isinstance(value, str) or len(value) > limit or any(
            ord(c) < 32 and c not in "\n\t" for c in value
        ):
            raise ValueError("Field GDR tidak valid.")
        return value.strip()
    def quantity(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("Jumlah material tidak valid.")
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Jumlah material tidak valid.") from None
        if not math.isfinite(value) or not 0 < value <= 1000000000:
            raise ValueError("Jumlah material tidak valid.")
        return value
    result = {key: text(data.get(key, ""), 256) for key in (
        "area", "pmo", "request_name", "request_mobile", "approve_name", "approve_mobile"
    )}
    clusters = data.get("clusters")
    if clusters is None or clusters == []:
        clusters = [{"pr_number": data.get("pr_number", ""), "cluster": data.get("cluster", "")}]
    if not isinstance(clusters, list) or len(clusters) > 200 or any(not isinstance(c, dict) for c in clusters):
        raise ValueError("Target GDR tidak valid.")
    result["clusters"] = [{"pr_number": text(c.get("pr_number", ""), 128),
                           "cluster": text(c.get("cluster", ""), 512)} for c in clusters]
    result["clusters"] = [c for c in result["clusters"] if c["pr_number"] or c["cluster"]]
    materials = data.get("materials")
    if not isinstance(materials, list) or not 0 < len(materials) <= 500:
        raise ValueError("Isi 1 sampai 500 material.")
    result["materials"] = []
    for m in materials:
        if not isinstance(m, dict):
            raise ValueError("Material GDR tidak valid.")
        name = text(m.get("name", ""), 512)
        if not name:
            raise ValueError("Nama material wajib diisi.")
        parts = m.get("pr_breakdown", [])
        if not isinstance(parts, list) or len(parts) > 200 or any(not isinstance(p, dict) for p in parts):
            raise ValueError("Rincian PR tidak valid.")
        result["materials"].append({"name": name, "qty": quantity(m.get("qty")),
            "unit": text(m.get("unit", ""), 64), "pr_breakdown": [
                {"pr_number": text(p.get("pr_number", ""), 128), "qty": quantity(p.get("qty"))} for p in parts
            ]})
    return result


@app.route("/api/gdr/generate", methods=["POST"])
def api_gdr_generate():
    import gdr_service
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Input GDR tidak valid."}), 400
    try:
        from gdr_store import vendor_code
        vendor = vendor_code(data.get("vendor", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    store = gdr_service.private_store()
    vendor_data = store.vendor(g.eva_user["id"], vendor)
    if vendor_data is None:
        return jsonify({"error": "Vendor tidak ditemukan di akun ini."}), 404
    try:
        data = _gdr_payload(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    clusters = data["clusters"]
    materials = data["materials"]
    pr_number = clusters[0]["pr_number"] if clusters else ""
    cluster = clusters[0]["cluster"] if clusters else ""
    area, pmo = data["area"], data["pmo"]
    request_name, request_mobile = data["request_name"], data["request_mobile"]
    approve_name, approve_mobile = data["approve_name"], data["approve_mobile"]

    import hashlib
    request_hash = hashlib.sha256(json.dumps(
        {**data, "vendor": vendor, "vendor_details": vendor_data},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()
    gdr_info = None
    try:
        with store.sheet_write_guard():
            try:
                gdr_info = gdr_service.allocate_gdr_number(
                    vendor, [c["pr_number"] for c in clusters if c.get("pr_number")],
                    clusters=clusters, area=area, pmo=pmo,
                    owner_user=g.eva_user, request_hash=request_hash,
                )
                gdr_label = gdr_info["label"]
                stage = "EXCEL_FAILED"
                out_file = gdr_service.generate_gdr_excel(
                    vendor=vendor, materials=materials, gdr_label=gdr_label,
                    pr_number=pr_number, cluster=cluster, clusters=clusters, area=area, pmo=pmo,
                    request_name=request_name, request_mobile=request_mobile,
                    approve_name=approve_name, approve_mobile=approve_mobile,
                    vendor_data=vendor_data,
                )
                stage = "MATERIALS_WRITE_FAILED"
                gdr_service.write_materials_to_gdr_sheet(
                    vendor=vendor, materials=materials, pr_number=pr_number,
                    cluster=cluster, gdr_label=gdr_label,
                )
                store.update_reservation(gdr_label, "materials_written")
                stage = "HISTORY_FAILED"
                EVA_AUTH.store.claim("gdr", str(out_file.name), g.eva_user["id"])
                store.add_history(g.eva_user, {
                    "number": gdr_info["number"], "gdr_label": gdr_label,
                    "vendor": vendor, "vendor_details": vendor_data,
                    "pr_number": pr_number, "cluster": cluster, "clusters": clusters,
                    "area": area, "pmo": pmo, "request_name": request_name,
                    "request_mobile": request_mobile, "approve_name": approve_name,
                    "approve_mobile": approve_mobile, "date": datetime.now().strftime("%Y-%m-%d"),
                    "item_count": len(materials), "file": str(out_file.name),
                })
                store.update_reservation(gdr_label, "done")
                return jsonify({"gdr_label": gdr_label, "number": gdr_info["number"],
                                "download_url": f"/api/gdr/download/{out_file.name}"})
            except Exception:
                if gdr_info is not None:
                    store.update_reservation(gdr_info["label"], "failed", failure_code=stage)
                raise
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except TimeoutError:
        return jsonify({"error": "Penulisan GDR belum terkonfirmasi. Periksa dashboard admin sebelum mencoba lagi."}), 503
    except Exception:
        return jsonify({"error": "GDR belum selesai. Nomor tetap dicadangkan; periksa dashboard admin sebelum mencoba lagi."}), 502


@app.route("/api/gdr/download/<path:filename>")
def api_gdr_download(filename):
    import gdr_service
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "File not found"}), 404
    entry = gdr_service.private_store().file_entry(g.eva_user["id"], filename)
    if entry is None:
        return jsonify({"error": "File not found"}), 404
    return _gdr_send_entry(entry)


@app.route("/api/gdr/history")
def api_gdr_history():
    import gdr_service
    rows = gdr_service.load_history(g.eva_user["id"])
    return jsonify({"history": rows})


# ---------- KMZ Coordinate Extractor ----------

@app.route("/kmz-coords")
def kmz_coords_page():
    return render_template("kmz_coords.html")


@app.route("/api/kmz-coords/analyze", methods=["POST"])
def api_kmz_coords_analyze():
    """Standardize uploaded KMZ files, extract placemarks, cache the rows, and
    return per-file counts (FAT, FDT, HP Cover, and pole subtypes)."""
    import kmz_coordinate_extractor as kce

    files = request.files.getlist("kmz_files")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "File KMZ/KML belum dipilih."}), 400
    for f in files:
        if not f.filename.lower().endswith((".kml", ".kmz")):
            return jsonify({"error": f"File {f.filename} bukan .kml/.kmz."}), 400

    coding_records = None
    coding_path = None
    if fdt_coding_source_ready():
        try:
            coding_records = resolve_fdt_coding_records()
            if not coding_records:
                coding_path = resolve_fdt_coding_workbook(uuid.uuid4().hex)
        except Exception:
            coding_records = None
            coding_path = None

    token = uuid.uuid4().hex
    saved_paths: list[Path] = []
    per_file_rows: dict[str, list] = {}
    per_file_counts: list[dict] = []
    try:
        for f in files:
            name = f.filename
            in_path = Config.UPLOAD_DIR / f"{token}_{secure_filename(name) or 'input.kmz'}"
            out_path = Config.OUTPUT_DIR / f"{token}_{secure_filename(name)}_STD.kmz"
            f.save(in_path)
            saved_paths.append(in_path)
            if in_path.stat().st_size > Config.MAX_FILE_BYTES:
                return jsonify({"error": f"Ukuran tiap file maksimal {Config.MAX_UPLOAD_MB}MB."}), 413

            standardize_kmz_file(
                in_path,
                coding_path,
                out_path,
                network_type=Config.DEFAULT_NETWORK_TYPE,
                coding_records=coding_records,
                source_name=name,
            )
            saved_paths.append(out_path)

            root = kce.read_kmz_root(out_path)
            rows = kce.extract_coordinates(root)
            per_file_rows[name] = rows
            per_file_counts.append({"file": name, "summary": kce.count_summary(rows)})

        # Bind the cache before publishing its token or writing usable rows.
        EVA_AUTH.store.claim("coords", token, g.eva_user["id"])
        cache_path = Config.OUTPUT_DIR / f"{token}_coords.json"
        cache_path.write_text(json.dumps(per_file_rows, ensure_ascii=False), encoding="utf-8")
        return jsonify({"token": token, "files": per_file_counts})
    except Exception as exc:
        LOGGER.exception("KMZ coordinate analyze gagal")
        return jsonify({"error": f"Analisa gagal: {exc}"}), 500
    finally:
        cleanup_saved_uploads(saved_paths)


@app.route("/api/kmz-coords/generate", methods=["POST"])
def api_kmz_coords_generate():
    """Build the Excel from cached analyze rows, filtered by selected categories."""
    import kmz_coordinate_extractor as kce

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Input analisa tidak valid."}), 400
    token = data.get("token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        return jsonify({"error": "Token analisa tidak valid. Upload ulang."}), 400
    if not EVA_AUTH.store.can_access("coords", token, g.eva_user):
        return jsonify({"error": "Data analisa tidak ditemukan."}), 404
    categories = data.get("categories") or None
    cache_path = Config.OUTPUT_DIR / f"{token}_coords.json"
    if cache_path.is_symlink() or not cache_path.resolve().is_relative_to(Config.OUTPUT_DIR.resolve()):
        return jsonify({"error": "Data analisa tidak ditemukan."}), 404
    if not cache_path.exists():
        return jsonify({"error": "Data analisa kadaluarsa. Upload ulang."}), 400

    try:
        per_file_rows = json.loads(cache_path.read_text(encoding="utf-8"))
        xlsx_path = Config.OUTPUT_DIR / f"{token}_koordinat.xlsx"
        kce.build_excel(per_file_rows, xlsx_path, categories=categories)
        return send_file(
            xlsx_path,
            as_attachment=True,
            download_name="Koordinat KMZ.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        LOGGER.exception("KMZ coordinate generate gagal")
        return jsonify({"error": f"Generate gagal: {exc}"}), 500


@app.route("/api/gdr/history/<int:number>", methods=["DELETE"])
def api_gdr_history_delete(number):
    import gdr_service
    try:
        entry = gdr_service.private_store().history_entry(g.eva_user["id"], number)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if entry is None:
        return jsonify({"error": "Riwayat GDR tidak ditemukan"}), 404
    return _gdr_delete_entry(entry["id"])


@app.route("/api/gdr/history/entry/<entry_id>", methods=["DELETE"])
def api_gdr_history_delete_entry(entry_id):
    return _gdr_delete_entry(entry_id)


def _gdr_delete_entry(entry_id):
    import gdr_service
    store = gdr_service.private_store()
    try:
        with store.sheet_write_guard():
            entry = store.personal_entry(g.eva_user["id"], entry_id)
            if entry is None:
                return jsonify({"error": "Riwayat GDR tidak ditemukan"}), 404
            store.retire_number(g.eva_user, entry)
            store.update_reservation(entry["gdr_label"], "delete_failed", failure_code="DELETE_FAILED")
            gdr_service.delete_gdr_from_sheets(entry["gdr_label"])
            out_dir = (Config.OUTPUT_DIR / "gdr").resolve()
            filename = str(entry.get("file") or "")
            if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
                raise ValueError("Identitas file GDR tidak valid.")
            target = out_dir / filename
            if target.is_symlink() or not target.resolve().is_relative_to(out_dir):
                raise ValueError("Identitas file GDR tidak valid.")
            target.unlink(missing_ok=True)
            if not store.delete_history(g.eva_user["id"], entry["id"]):
                return jsonify({"error": "Riwayat GDR tidak ditemukan"}), 404
            store.update_reservation(entry["gdr_label"], "deleted")
            return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Penghapusan GDR belum selesai. Riwayat dipertahankan; periksa dashboard admin."}), 502




@app.route("/admin/gmail-rfs/auth")
def admin_gmail_rfs_auth():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    origin = EVA_AUTH.origin()
    tx = EVA_AUTH.store.begin_oauth(origin)
    EVA_AUTH.store.claim("gmail-state", tx["state"], g.eva_user["id"])
    session["gmail_oauth"] = {"state": tx["state"], "browser": tx["browser"]}
    try:
        callback = origin + "/oauth/gmail/callback"
        return redirect(gmail_rfs_ingest.auth_url(state=tx["state"], verifier=tx["verifier"], redirect_uri=callback))
    except Exception:
        session.pop("gmail_oauth", None)
        return jsonify({"error": "Konfigurasi Gmail OAuth belum siap."}), 503


@app.route("/oauth/gmail/callback")
def oauth_gmail_callback():
    pending = session.pop("gmail_oauth", None)
    state = request.args.get("state", "")
    user = getattr(g, "eva_user", None)
    if (not user or user["role"] != "owner" or not isinstance(pending, dict)
            or not state or pending.get("state") != state
            or not EVA_AUTH.store.can_access("gmail-state", state, user)):
        return jsonify({"error": "Sesi Gmail OAuth tidak valid."}), 400
    try:
        origin = EVA_AUTH.origin()
        tx = EVA_AUTH.store.consume_oauth(state, pending.get("browser", ""), origin)
        if request.args.get("error"):
            return jsonify({"error": "Otorisasi Gmail dibatalkan."}), 400
        callback_url = origin + request.full_path.rstrip("?")
        gmail_rfs_ingest.handle_callback(callback_url, state=state, verifier=tx["verifier"],
                                         redirect_uri=origin + "/oauth/gmail/callback")
        return "Gmail RFS OAuth sukses. Tutup tab ini.", 200
    except Exception:
        LOGGER.exception("Gmail OAuth callback gagal")
        return jsonify({"error": "Gmail OAuth gagal. Mulai otorisasi ulang."}), 400


@app.route("/admin/gmail-rfs/run", methods=["GET", "POST"])
def admin_gmail_rfs_run():
    if request.method != "POST":
        return jsonify({"error": "Gunakan POST untuk menjalankan ingest."}), 405
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    try:
        limit = int(request.args.get("limit", "25") or 25)
        return jsonify(gmail_rfs_ingest.ingest(max_results=max(1, min(limit, 100))))
    except Exception as exc:
        LOGGER.exception("Gmail RFS ingest manual gagal")
        return jsonify({"error": str(exc)}), 500


@app.route("/admin/gmail-rfs/status")
def admin_gmail_rfs_status():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    return jsonify({"authorized": gmail_rfs_ingest.is_authorized()})


@app.route("/api/rfs/ensure", methods=["POST"])
def api_rfs_ensure():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        return jsonify(rfs_sheets.ensure_rfs_sheets())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/rfs/months")
def api_rfs_months():
    try:
        return jsonify({"months": rfs_sheets.months(), "current_month": rfs_sheets.default_dashboard_month()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/rfs/summary")
def api_rfs_summary():
    try:
        month = request.args.get("month", "").strip() or None
        return jsonify(rfs_sheets.summarize(month))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/rfs/records", methods=["POST"])
def api_rfs_create_record():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        result = rfs_sheets.append_record(request.get_json(silent=True) or {})
        return jsonify(result), 200 if result.get("duplicate") else 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/rfs/targets", methods=["POST"])
def api_rfs_set_target():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        return jsonify(rfs_sheets.set_target(request.get_json(silent=True) or {}))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/regions/search")
def api_regions_search():
    """Search Indonesian administrative regions (province/city/district/subdistrict)."""
    q = request.args.get("q", "").strip()
    city = request.args.get("city", "").strip()
    district = request.args.get("district", "").strip()
    subdistrict = request.args.get("subdistrict", "").strip()
    postal_code = request.args.get("postal_code", "").strip()

    REGION_DB = Config.REGION_DB_PATH
    if not REGION_DB.is_file():
        return jsonify([])

    conn = sqlite3.connect(str(REGION_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        if postal_code:
            cur.execute("""SELECT province, city, district, subdistrict, postal_code FROM regions
                WHERE postal_code LIKE ?
                ORDER BY province, city, district, subdistrict""", (f"%{postal_code}%",))
        elif subdistrict:
            cur.execute("""SELECT province, city, district, subdistrict, postal_code FROM regions
                WHERE (subdistrict LIKE ? OR subdistrict_key = ?) AND subdistrict != ''
                ORDER BY province, city, district""", (f"%{subdistrict}%", region_db.region_key(subdistrict)))
        elif district:
            cur.execute("""SELECT province, city, district, subdistrict, postal_code FROM regions
                WHERE district LIKE ? OR district_key = ?
                ORDER BY province, city, district, subdistrict""", (f"%{district}%", region_db.region_key(district)))
        elif city:
            cur.execute("""SELECT province, city, district, subdistrict, postal_code FROM regions
                WHERE city LIKE ? OR city_key = ?
                ORDER BY province, city, district, subdistrict""", (f"%{city}%", region_db.region_key(city)))
        elif q:
            cur.execute("""SELECT province, city, district, subdistrict, postal_code FROM regions
                WHERE subdistrict LIKE ? OR district LIKE ?
                    OR subdistrict_key = ? OR district_key = ?
                ORDER BY province, city, district, subdistrict""",
                (f"%{q}%", f"%{q}%", region_db.region_key(q), region_db.region_key(q)))
        else:
            cur.execute("""SELECT province, COUNT(DISTINCT city) as cities,
                COUNT(DISTINCT district) as districts, COUNT(*) as entries
                FROM regions GROUP BY province ORDER BY province""")

        rows = [dict(r) for r in cur.fetchall()]
        return jsonify(rows)
    finally:
        conn.close()






def _region_db_path() -> Path | None:
    region_path = getattr(Config, "REGION_DB_PATH", None)
    if region_path and Path(region_path).is_file():
        return Path(region_path)
    fallback = Path(os.environ.get("REGION_DB_PATH", ""))
    if str(fallback) and fallback.is_file():
        return fallback
    fallback = Path("/opt/eva/data/region.db")
    return fallback if fallback.is_file() else None

def _region_db_rows(sql: str, params: tuple = ()) -> list[dict]:
    db_path = _region_db_path()
    if not db_path:
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

def _safe_limit() -> int:
    try:
        return min(max(int(request.args.get("limit", 20) or 20), 1), 5000)
    except ValueError:
        return 20

def _region_city_filter_values(value: str) -> list[str]:
    value = str(value or "").strip()
    if not value:
        return []
    values = [value]
    if value.lower().startswith("kab. "):
        values.append("Kabupaten " + value[5:].strip())
    elif not value.lower().startswith(("kabupaten ", "kota ")):
        values.extend([f"Kota {value}", f"Kabupaten {value}"])
    return list(dict.fromkeys(values))

def _region_suggest(field: str, q: str = "", **filters) -> list[dict]:
    clauses = [f"{field} != ''"]
    params: list[str] = []
    if q:
        if field in {"city", "district", "subdistrict"}:
            clauses.append(f"({field} LIKE ? OR {field}_key = ?)")
            params.extend([f"%{q}%", region_db.region_key(q)])
        else:
            clauses.append(f"{field} LIKE ?")
            params.append(f"%{q}%")
    for key in ("province", "city", "district", "subdistrict"):
        val = str(filters.get(key) or "").strip()
        if not val:
            continue
        if key == "city":
            city_values = _region_city_filter_values(val)
            clauses.append("(" + " OR ".join(["city = ?"] * len(city_values)) + ")")
            params.extend(city_values)
        else:
            clauses.append(f"{key} = ?")
            params.append(val)
    params.append(_safe_limit())
    return _region_db_rows(f"SELECT DISTINCT {field} FROM regions WHERE {' AND '.join(clauses)} ORDER BY {field} LIMIT ?", tuple(params))

def _apply_admin_overrides(base: dict, form: dict) -> dict:
    out = dict(base or {})
    aliases = {"subdistrict": "sub_district", "postalcode": "postal_code", "Area": "area_name"}
    for key in ("province", "city", "regency_city", "district", "subdistrict", "sub_district", "postal_code", "postalcode", "area_name", "Area"):
        values = form.getlist(key) if hasattr(form, "getlist") else [form.get(key, "")]
        value = next((str(item).strip() for item in reversed(values) if str(item).strip()), "")
        if not value:
            continue
        target = aliases.get(key, key)
        if target in {"city", "regency_city"} and value.lower().startswith("kab. "):
            value = "Kabupaten " + value[5:].strip()
        out[target] = value
        if target == "sub_district":
            out["subdistrict"] = value
        if target == "postal_code":
            out["postalcode"] = value
        if target == "area_name":
            out["Area"] = value
    return out

@app.route("/api/regions/provinces")
def api_regions_provinces():
    rows = _region_suggest("province", request.args.get("q", "").strip())
    return jsonify([{"province": row.get("province", "")} for row in rows])

@app.route("/api/regions/cities")
def api_regions_cities():
    rows = _region_suggest("city", request.args.get("q", "").strip(), province=request.args.get("province", ""))
    return jsonify(rows)

@app.route("/api/regions/districts")
def api_regions_districts():
    rows = _region_suggest("district", request.args.get("q", "").strip(), province=request.args.get("province", ""), city=request.args.get("city", ""))
    return jsonify(rows)

@app.route("/api/regions/subdistricts")
def api_regions_subdistricts():
    rows = _region_suggest("subdistrict", request.args.get("q", "").strip(), province=request.args.get("province", ""), city=request.args.get("city", ""), district=request.args.get("district", ""))
    return jsonify(rows)

@app.route("/api/regions/postal-codes")
def api_regions_postal_codes():
    rows = _region_suggest("postal_code", request.args.get("q", "").strip(), province=request.args.get("province", ""), city=request.args.get("city", ""), district=request.args.get("district", ""), subdistrict=request.args.get("subdistrict", ""))
    return jsonify(rows)

@app.route("/api/astri/areas")
def api_astri_areas():
    q = request.args.get("q", "").strip()
    clauses = ["area_name != ''"]
    params = []
    if q:
        clauses.append("area_name LIKE ?")
        params.append(f"%{q}%")
    for key in ("province", "city"):
        val = request.args.get(key, "").strip()
        if val:
            clauses.append(f"{key} = ?")
            params.append(val)
    params.append(_safe_limit())
    return jsonify(_region_db_rows(f"SELECT DISTINCT area_name, province, city, source FROM astri_areas WHERE {' AND '.join(clauses)} ORDER BY area_name LIMIT ?", tuple(params)))


# ── GDR: fetch internal materials from ASTRI ─────────────────────────────────

def _clean_astri_cell(value):
    import re, html as _html
    value = re.sub(r'<script\b[^>]*>.*?</script>', '', value or '', flags=re.I | re.S)
    value = re.sub(r'<style\b[^>]*>.*?</style>', '', value, flags=re.I | re.S)
    return _html.unescape(re.sub(r'<[^>]+>', ' ', value)).strip()


def _parse_number(value):
    import re
    text = (value or '').strip().replace('\xa0', ' ')
    text = re.sub(r'[^0-9,.-]+', '', text)
    if not text:
        return None
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        return float(text)
    except ValueError:
        return None


def _parse_astri_internal_materials(html_content):
    """Parse only MATERIAL INTERNAL table/section from ASTRI requirement page."""
    import re
    result = []
    # Cut from MATERIAL INTERNAL heading until next material/vendor section heading.
    m = re.search(r'MATERIAL\s+INTERNAL(?P<body>.*?)(?:MATERIAL\s+VENDOR|</body>|$)', html_content or '', re.I | re.S)
    if not m:
        return result
    body = m.group('body')
    table_m = re.search(r'<table[^>]*>(.*?)</table>', body, re.I | re.S)
    table_html = table_m.group(1) if table_m else body

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.I | re.S)
    for row_html in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.I | re.S)
        cleaned = [_clean_astri_cell(c) for c in cells]
        cleaned = [c for c in cleaned if c]
        if len(cleaned) < 3:
            continue
        lowered = ' '.join(cleaned).lower()
        if any(h in lowered for h in ['material name', 'nama material', 'qty', 'quantity']) and not any(ch.isdigit() for ch in lowered):
            continue

        # Prefer common layout: no, material/name/code, qty, unit.
        name = ''
        qty = None
        unit = ''
        for i, cell in enumerate(cleaned):
            n = _parse_number(cell)
            if n is not None and i > 0:
                qty = n
                # choose nearest previous non-number as name, nearest next as unit
                for j in range(i - 1, -1, -1):
                    if _parse_number(cleaned[j]) is None and not re.fullmatch(r'\d+', cleaned[j]):
                        name = cleaned[j]
                        break
                if i + 1 < len(cleaned):
                    unit = cleaned[i + 1]
                break
        if not name or qty is None or qty <= 0:
            continue
        # Skip vendor-looking rows defensively; section cut should already prevent this.
        if re.search(r'\bvendor\b', name, re.I):
            continue
        result.append({'name': name, 'qty': qty, 'unit': unit})
    return result


def fetch_astri_cluster_review_page(record: dict, match: dict) -> str:
    from urllib.parse import quote
    vendor_label = (match.get("vendor_label") or "").strip()
    url = (
        f"{ASTRI_BASE_URL}/material/cluster/drm/review/v3"
        f"?cluster_code={quote(match['cluster_code'], safe='')}"
        f"&work_order_number={quote(match['work_order_number'], safe='')}"
        f"&vendor_label={quote(vendor_label, safe='')}"
    )
    response = record["session"].get(url, timeout=60)
    response.raise_for_status()
    return response.text


def _parse_astri_boq_list_internal_materials(html_content, target_code, code_column=None):
    """Parse BOQ List (DRM) table and keep only Internal Team rows.
    Works for cluster, subfeeder, and feeder pages."""
    import re
    from html import unescape

    def strip_tags(text):
        text = re.sub(r'<[^>]+>', ' ', text or '')
        return re.sub(r'\s+', ' ', unescape(text)).strip()

    table_match = re.search(r'<table[^>]*>(.*?)</table>', html_content or '', re.I | re.S)
    if not table_match:
        return []
    table_html = table_match.group(0)
    row_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.I | re.S)
    if len(row_matches) < 2:
        return []

    headers = [strip_tags(x) for x in re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row_matches[0], re.I | re.S)]
    code_cols = [c for c in [code_column, 'Cluster Code', 'Subfeeder Code', 'Feeder Code'] if c]
    wanted = {
        'Vendor Name': None,
        'Material Number': None,
        'Description': None,
        'Qty Material': None,
        'Qty Service': None,
        'Boq Phase': None,
        'Remarks': None,
    }
    for i, h in enumerate(headers):
        if h in wanted:
            wanted[h] = i
        for cc in code_cols:
            if h == cc and cc not in wanted:
                wanted[cc] = i
    found_code_key = next((cc for cc in code_cols if wanted.get(cc) is not None), None)
    if found_code_key is None or wanted.get('Vendor Name') is None or wanted.get('Description') is None:
        return []

    materials = []
    for row_html in row_matches[1:]:
        cells = [strip_tags(x) for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.I | re.S)]
        if not cells:
            continue
        try:
            code_val = cells[wanted[found_code_key]]
            vendor_name = cells[wanted['Vendor Name']]
        except Exception:
            continue
        if code_val != target_code or vendor_name != 'Internal Team':
            continue
        desc = cells[wanted['Description']] if wanted['Description'] is not None and wanted['Description'] < len(cells) else ''
        qty_material = cells[wanted['Qty Material']] if wanted.get('Qty Material') is not None and wanted['Qty Material'] < len(cells) else '0'
        qty_service = cells[wanted['Qty Service']] if wanted.get('Qty Service') is not None and wanted['Qty Service'] < len(cells) else '0'
        phase = cells[wanted['Boq Phase']] if wanted.get('Boq Phase') is not None and wanted['Boq Phase'] < len(cells) else 'drm'
        remarks = cells[wanted['Remarks']] if wanted.get('Remarks') is not None and wanted['Remarks'] < len(cells) else 'Material'
        material_number = cells[wanted['Material Number']] if wanted.get('Material Number') is not None and wanted['Material Number'] < len(cells) else desc
        materials.append({
            'code': material_number or desc,
            'description': desc,
            'material_qty': qty_material or '0',
            'service_qty': qty_service or '0',
            'remarks': remarks or 'Material',
            'phase': phase or 'drm',
            'source': 'boq-drm-list',
        })
    return materials


def fetch_astri_subfeeder_boq_list_page(record: dict, match: dict) -> str:
    from urllib.parse import quote
    code = match.get("subfeeder_code") or match.get("cluster_code", "")
    url = (
        f"{ASTRI_BASE_URL}/material/subfeeder/boq-drm/list/"
        f"?filter%5Bsubfeeder_code%5D={quote(code, safe='')}"
    )
    response = record["session"].get(url, timeout=60)
    response.raise_for_status()
    return response.text


def fetch_astri_feeder_boq_list_page(record: dict, match: dict) -> str:
    from urllib.parse import quote
    code = match.get("feeder_code") or match.get("cluster_code", "")
    url = (
        f"{ASTRI_BASE_URL}/material/feeder/boq-drm/list/"
        f"?filter%5Bfeeder_code%5D={quote(code, safe='')}"
    )
    response = record["session"].get(url, timeout=60)
    response.raise_for_status()
    return response.text


def fetch_astri_cluster_boq_list_page(record: dict, match: dict) -> str:
    from urllib.parse import quote
    url = (
        f"{ASTRI_BASE_URL}/material/cluster/boq-drm/list/"
        f"?filter%5Bcluster_code%5D={quote(match['cluster_code'], safe='')}"
    )
    response = record['session'].get(url, timeout=60)
    response.raise_for_status()
    return response.text

def _parse_astri_review_internal_materials(html_content):
    """Parse BOQ Data table from DRM Review page and keep only Internal Team rows."""
    import re
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html_content or '', re.I | re.S)
    result = []
    header = None
    for row_html in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.I | re.S)
        cleaned = [_clean_astri_cell(c) for c in cells]
        cleaned = [c.strip() for c in cleaned]
        if not cleaned:
            continue
        lowered = [c.lower() for c in cleaned]
        if 'vendor name' in lowered and 'description' in lowered and 'qty material' in lowered:
            header = {name.lower(): idx for idx, name in enumerate(cleaned)}
            continue
        if not header or len(cleaned) <= max(header.get('vendor name', 0), header.get('description', 0), header.get('qty material', 0)):
            continue
        vendor = cleaned[header['vendor name']].strip()
        if vendor.lower() != 'internal team':
            continue
        name = cleaned[header['description']].strip()
        qty = _parse_number(cleaned[header['qty material']])
        if not name or qty is None or qty <= 0:
            continue
        result.append({'name': name, 'qty': qty, 'unit': 'UNIT'})
    return result


@app.route("/api/gdr/fetch-astri-materials")
def api_gdr_fetch_astri_materials():
    try:
        record = require_astri_session()
        pr_number = request.args.get("pr_number", "").strip()
        work_order_number = request.args.get("work_order_number", "").strip()
        cluster_code = request.args.get("cluster_code", "").strip()
        vendor_label = request.args.get("vendor_label", "").strip()
        target_type = (request.args.get("target_type") or "cluster").strip().lower()
        effective_work_order = work_order_number or pr_number
        if not effective_work_order or not cluster_code:
            return jsonify({"error": "work_order_number and cluster_code required"}), 400
        match = {
            "work_order_number": effective_work_order,
            "cluster_code": cluster_code,
            "subfeeder_code": cluster_code,
            "feeder_code": cluster_code,
            "vendor_label": vendor_label,
        }
        if target_type == "feeder":
            page_html = fetch_astri_feeder_boq_list_page(record, match)
            materials = _parse_astri_boq_list_internal_materials(page_html, cluster_code, code_column='Feeder Code')
        elif target_type == "subfeeder":
            page_html = fetch_astri_subfeeder_boq_list_page(record, match)
            materials = _parse_astri_boq_list_internal_materials(page_html, cluster_code, code_column='Subfeeder Code')
        else:
            page_html = fetch_astri_cluster_boq_list_page(record, match)
            materials = _parse_astri_boq_list_internal_materials(page_html, cluster_code, code_column='Cluster Code')
        return jsonify({"materials": materials, "target_type": target_type})
    except Exception as exc:
        import traceback as _tb
        LOGGER.error("ASTRI material fetch: %s\n%s", exc, _tb.format_exc())
        return jsonify({"error": str(exc)}), 500

from abd_cad_api import register_abd_cad
from combined_batch import generate_combined
register_abd_cad(app, generate_combined, Path(__file__).resolve().parent / "data" / "abd_cad_sample.dxf")

from eva_drm_notifications import register_drm_notifications
register_drm_notifications(app, globals())

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
