import csv
import difflib
import html
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
from pathlib import Path
from typing import Any

import kmz_standardizer
from kmz_standardizer import RULES_CONFIG_PATH, load_rules_config

import region_db

from flask import Flask, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for, make_response
import requests
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from config import Config, validate_production_secrets
from logging_config import clear_request_id, set_request_id, setup_logging
from alerts import alert_job_failed, alert_server_unhealthy
from cleanup import start_cleanup_scheduler
from job_queue import enqueue_conversion_job
from boq import (
    BOQ_NRO_CABLE_LOOP_HOLDER_CELL,
    BOQ_NRO_SHEET_NAME,
    BOQ_SHEET_NAME,
    calculate_boq_nro_cell_values,
    calculate_feeder_boq_design_summary_values,
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
)
from hpdb_mancore import generate_hpdb_excel
from jobs import (
    acquire_job_slot,
    cleanup_jobs,
    create_job,
    create_job_logger,
    delete_job,
    get_job,
    list_jobs,
    refresh_job_slot,
    release_job_slot,
    recover_stale_jobs,
    update_job,
)
from kml_parser import iter_placemarks, load_kml_root
from kmz_standardizer import download_coding_workbook, standardize_kmz_file
import fdt_sync_service
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

def admin_api_token_required() -> bool:
    return bool(Config.REQUIRE_ADMIN_API_TOKEN and Config.ADMIN_API_TOKEN)

def require_admin_api_access():
    if not admin_api_token_required():
        return None
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    if provided and secrets.compare_digest(provided, Config.ADMIN_API_TOKEN):
        return None
    return jsonify({"error": "Admin token required."}), 403



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
    if session.get("dashboard_admin") is True:
        return None
    return redirect(url_for("dashboard_login"))

def is_dashboard_host() -> bool:
    return request.host.split(":", 1)[0].lower() == "dashboard.eva-mrdesign.my.id"

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
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
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


ASTRI_BASE_URL = "https://astri-osp.myrepublic.net.id"
ASTRI_SESSION_KEY = "astri_session_id"
ASTRI_SESSIONS: dict[str, dict] = {}
ASTRI_SESSION_STORE_PATH = Path("/tmp/eva_astri_sessions.json")

# Crypto helpers for storing ASTRI password (encrypted with SECRET_KEY)
def _get_astri_cipher():
    try:
        from cryptography.fernet import Fernet
        import base64
        secret = os.environ.get("SECRET_KEY") or Config.SECRET_KEY or secrets.token_urlsafe(32)
        key = base64.urlsafe_b64encode(secret.encode()[:32].ljust(32, b"\0"))
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

def persist_astri_session(sid: str, username: str, sess: requests.Session, password: str = "") -> dict:
    store = load_astri_session_store()
    entry = {
        "username": username,
        "created_at": time.time(),
        "cookies": requests.utils.dict_from_cookiejar(sess.cookies),
    }
    if password:
        encrypted = _encrypt_astri_password(password)
        if encrypted:
            entry["encrypted_password"] = encrypted
    store[sid] = entry
    save_astri_session_store(store)
    return entry

def current_astri_session() -> dict | None:
    sid = session.get(ASTRI_SESSION_KEY, "")
    if not sid:
        return None
    record = ASTRI_SESSIONS.get(sid)
    if record:
        return record
    stored = load_astri_session_store().get(sid)
    if not stored:
        return None
    sess = requests.Session()
    sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
    sess.cookies.update(stored.get("cookies") or {})
    record = {"session": sess, "username": stored.get("username", ""), "created_at": stored.get("created_at", time.time()), "_stored": stored}
    ASTRI_SESSIONS[sid] = record
    return record

def _astri_session_alive(record: dict) -> bool:
    try:
        r = record["session"].get(f"{ASTRI_BASE_URL}/user", timeout=15, allow_redirects=False)
        return r.status_code in {200} or (r.status_code in {301, 302, 303} and "/user" in r.headers.get("Location", ""))
    except Exception:
        return False

def _try_auto_relogin(record: dict) -> dict | None:
    stored = record.get("_stored") or {}
    encrypted = stored.get("encrypted_password")
    username = record.get("username") or stored.get("username")
    if not encrypted or not username:
        return None
    password = _decrypt_astri_password(encrypted)
    if not password:
        return None
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
        sess.get(f"{ASTRI_BASE_URL}/", timeout=20)
        response = sess.post(
            f"{ASTRI_BASE_URL}/login-process",
            data={
                "user-identity": username,
                "password": password,
                "csrf_name": "",
                "csrf_value": "",
            },
            allow_redirects=False,
            timeout=20,
        )
        if response.status_code not in {301, 302, 303} or "/user" not in response.headers.get("Location", ""):
            return None
        new_record = {"session": sess, "username": username, "created_at": time.time()}
        sid = astri_session_id()
        new_record["_stored"] = persist_astri_session(sid, username, sess, password=password)
        ASTRI_SESSIONS[sid] = new_record
        return new_record
    except Exception:
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
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError("Username dan password ASTRI wajib diisi.")
    sess = requests.Session()
    sess.headers.update({"User-Agent": "EVA-ASTRI-Connector/1.0"})
    sess.get(f"{ASTRI_BASE_URL}/", timeout=20)
    response = sess.post(
        f"{ASTRI_BASE_URL}/login-process",
        data={
            "user-identity": username,
            "password": password,
            "csrf_name": "",
            "csrf_value": "",
        },
        allow_redirects=False,
        timeout=20,
    )
    if response.status_code not in {301, 302, 303} or "/user" not in response.headers.get("Location", ""):
        raise ValueError("Login ASTRI gagal. Cek username/password atau akses akun.")
    user_page = sess.get(f"{ASTRI_BASE_URL}/user", timeout=20)
    if username not in user_page.text and "ASTRI OSP" not in user_page.text:
        raise ValueError("Login ASTRI gagal diverifikasi.")
    sid = astri_session_id()
    session.modified = True
    record = {"session": sess, "username": username, "created_at": time.time()}
    record["_stored"] = persist_astri_session(sid, username, sess, password=password)
    ASTRI_SESSIONS[sid] = record
    return {"username": username, "created_at": record["created_at"]}

def astri_csv_rows(record: dict) -> list[dict]:
    response = record["session"].get(f"{ASTRI_BASE_URL}/download/drm/cluster", timeout=60)
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text)))

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
        "matched": True,
        "query": cluster_name,
        "match_score": row.get("_match_score", 1),
        "cluster_code": row.get("Target Cluster Code", ""),
        "cluster_name": row.get("Target Cluster Name", ""),
        "work_order_number": row.get("Number", ""),
        "pr_number": row.get("PR Number", row.get("Purchase Requisition Number", "")),
        "vendor_label": row.get("Assigned Vendor Label", ""),
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
    area = row.get("target_cluster_area", "")
    return {
        "matched": True,
        "target_type": "cluster",
        "query": query,
        "match_score": 1,
        "cluster_code": code,
        "cluster_name": name,
        "work_order_number": wo,
        "pr_number": row.get("purchase_requisition_number", ""),
        "vendor_label": vendor,
        "vendor_name": row.get("assigned_vendor_name", "") or vendor,
        "area": area,
        "area_name": area,
        "province": province_for_city(area),
        "area_plant_code": row.get("target_cluster_area_plant_code", ""),
        "homepass": row.get("target_cluster_drm_homepass", ""),
        "net_type": row.get("target_cluster_drm_net_type", ""),
        "latest_status_label": row.get("latest_status_label", ""),
        "latest_status_name": row.get("latest_status_name", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/cluster/drm/requirement-document?cluster_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }


def parse_astri_cluster_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = class_name.replace("work-order-", "").replace("-column", "").replace("-", "_")
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        if row.get("target_cluster_code") and row.get("number"):
            rows.append(row)
    return rows


def search_astri_clusters_v3(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()
    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/cluster/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_name]": status} if status else {}), "page": page, "limit": 100},
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
    vendor = row.get("assigned_vendor_label", "")
    return {
        "matched": True,
        "target_type": "subfeeder",
        "query": query,
        "cluster_code": code,
        "cluster_name": name,
        "subfeeder_code": code,
        "subfeeder_name": name,
        "work_order_number": wo,
        "pr_number": row.get("target_subfeeder_purchase_requisition_number", "") or row.get("purchase_requisition_number", ""),
        "vendor_label": vendor,
        "vendor_name": vendor,
        "area": row.get("target_subfeeder_area", ""),
        "area_plant_code": row.get("target_subfeeder_area_plant_code", ""),
        "homepass": row.get("target_subfeeder_drm_homepass", ""),
        "net_type": row.get("target_subfeeder_drm_net_type", ""),
        "latest_status_label": row.get("latest_status_label", ""),
        "latest_status_name": row.get("latest_status_name", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/subfeeder/drm/requirement-document?subfeeder_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }

def parse_astri_subfeeder_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = class_name.replace("work-order-", "").replace("-column", "").replace("-", "_")
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        if row.get("target_subfeeder_code") and row.get("number"):
            rows.append(row)
    return rows

def search_astri_subfeeders(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()
    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/subfeeder/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_name]": status} if status else {}), "page": page, "limit": 100},
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


def astri_feeder_match_payload(row: dict, query: str = "") -> dict:
    code = row.get("target_osp_route_code", "")
    name = row.get("target_osp_route_label", "")
    wo = row.get("number", "")
    vendor = row.get("assigned_vendor_label", "")
    return {
        "matched": True,
        "target_type": "feeder",
        "query": query,
        "cluster_code": code,
        "cluster_name": name,
        "feeder_code": code,
        "feeder_name": name,
        "work_order_number": wo,
        "pr_number": row.get("target_osp_route_purchase_requisition_number", "") or row.get("purchase_requisition_number", ""),
        "vendor_label": vendor,
        "vendor_name": vendor,
        "area": row.get("target_osp_route_area", ""),
        "homepass": row.get("target_osp_route_length", ""),
        "net_type": row.get("target_osp_route_drm_net_type", ""),
        "latest_status_label": row.get("latest_status_label", ""),
        "latest_status_name": row.get("latest_status_name", ""),
        "requirement_url": f"{ASTRI_BASE_URL}/material/feeder/drm/requirement-document?feeder_code={code}&work_order_number={wo}&vendor_label={vendor}",
    }


def parse_astri_feeder_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.I | re.S):
        cells = re.findall(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', row_html, flags=re.I | re.S)
        row = {}
        for class_name, value_html in cells:
            key = class_name.replace("work-order-", "").replace("-column", "").replace("-", "_")
            value = html.unescape(re.sub(r"<[^>]+>|\s+", " ", value_html)).strip()
            row[key] = value
        if row.get("target_osp_route_code") and row.get("number"):
            rows.append(row)
    return rows


def search_astri_feeders(record: dict, query_raw: str, status: str = "") -> list[dict]:
    query = normalize_match_text(query_raw)
    items = []
    seen_keys = set()
    for page in range(1, 21):
        try:
            response = record["session"].get(
                f"{ASTRI_BASE_URL}/material/feeder/drm/list/v3",
                params={**({"filter[global]": query_raw} if query_raw else {}), **({"filter[latest_status_name]": status} if status else {}), "page": page, "limit": 100},
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
    # Extract structured fields for frontend
    fields = []
    seen_ids = set()
    for inp in re.finditer(r'<input[^>]+>', form_html, re.I):
        tag = inp.group(0)
        attrs = extract_tag_attrs(tag)
        iid = attrs.get("id", "") or (attrs.get("name", "").replace("[", "_").replace("]", "").replace(".", "_"))
        iname = attrs.get("name", "")
        if not iname or not iid or iid in seen_ids:
            continue
        seen_ids.add(iid)
        ftype = attrs.get("type", "text")
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
        sel_val = ""
        for tag_start, _, ov, tag_end, ot in opts:
            if "selected" in tag_start or "selected" in tag_end:
                sel_val = ov
                break
        if not sel_val and opts:
            sel_val = opts[0][2]
        fields.append({
            "id": sid,
            "name": sname,
            "type": "select",
            "value": sel_val,
            "options": [{"value": v, "text": html.unescape(re.sub(r'<[^>]+>', '', t)).strip()} for _, _, v, _, t in opts],
            "readonly": False,
            "disabled": _has_bool_attr(stxt[:stxt.find(">")+1], "disabled"),
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



def astri_create_boq_link(record: dict, match: dict) -> dict:
    response = record["session"].get(f"{ASTRI_BASE_URL}/material/cluster/drm/list/v3", timeout=60)
    response.raise_for_status()
    pattern = r'<a[^>]+/material/cluster/boq-drm/insert[^>]*' + re.escape(match["cluster_code"]) + r'[^>]*>.*?</a>'
    found = re.search(pattern, response.text, flags=re.I | re.S)
    if not found:
        return {"enabled": False, "error": "Create BOQ link tidak ditemukan di ASTRI."}
    tag = found.group(0)
    href_match = re.search(r'href=("|\')([^"\']+)\1', tag, flags=re.I)
    href = html.unescape(href_match.group(2)) if href_match else ""
    enabled = "disabled" not in tag.lower()
    return {"enabled": enabled, "href": href, "url": f"{ASTRI_BASE_URL}{href}" if href.startswith("/") else href}



def fetch_astri_feeder_requirement_page(record: dict, match: dict) -> str:
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
    return response.text


def fetch_astri_subfeeder_requirement_page(record: dict, match: dict) -> str:
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
    return response.text

def astri_create_feeder_boq_link(record: dict, match: dict) -> dict:
    response = record["session"].get(f"{ASTRI_BASE_URL}/material/feeder/drm/list/v3", timeout=60)
    response.raise_for_status()
    pattern = r'<a[^>]+/material/feeder/boq-drm/insert[^>]*' + re.escape(match["cluster_code"]) + r'[^>]*>.*?</a>'
    found = re.search(pattern, response.text, flags=re.I | re.S)
    if not found:
        return {"enabled": False, "error": "Create BOQ feeder link tidak ditemukan di ASTRI."}
    tag = found.group(0)
    href_match = re.search(r'href=("|\')([^"\']+)\1', tag, flags=re.I)
    href = html.unescape(href_match.group(2)) if href_match else ""
    enabled = "disabled" not in tag.lower()
    return {"enabled": enabled, "href": href, "url": f"{ASTRI_BASE_URL}{href}" if href.startswith("/") else href}


def astri_create_subfeeder_boq_link(record: dict, match: dict) -> dict:
    code = match.get("cluster_code", "")
    vendor = match.get("vendor_label", match.get("assigned_vendor_name", "")).lower().replace(" ", "_")
    area = match.get("area", "")
    won = match.get("work_order_number", "")
    plant = match.get("area_plant_code", match.get("cluster_area_plant_code", ""))
    parts = [
        f"cluster_code={code}",
        f"assigned_vendor_name={vendor}",
        f"cluster_area={area}",
        f"work_order_number={won}",
        f"assigned_subcont_vendor_name=",
        f"cluster_area_plant_code={plant}",
    ]
    href = f"/material/subfeeder/boq-drm/insert?{'&'.join(parts)}"
    return {"enabled": True, "href": href, "url": f"{ASTRI_BASE_URL}{href}", "error": ""}

def preview_eva_boq_file(path: Path) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    ws = wb["BoQ NRO Cluster"] if "BoQ NRO Cluster" in wb.sheetnames else wb.active
    rows = []
    for row in range(7, ws.max_row + 1):
        code = ws.cell(row, 14).value
        if not code:
            continue
        material_qty = ws.cell(row, 4).value or 0
        service_qty = ws.cell(row, 5).value or 0
        if not material_qty and not service_qty:
            continue
        rows.append({
            "row": row,
            "code": str(code).strip(),
            "description": str(ws.cell(row, 2).value or "").strip(),
            "unit": str(ws.cell(row, 3).value or "").strip(),
            "material_qty": material_qty,
            "service_qty": service_qty,
        })
    return rows

@app.route("/api/astri/status")
def api_astri_status():
    record = current_astri_session()
    return jsonify({"connected": bool(record), "username": (record or {}).get("username", "")})

@app.route("/api/astri/login", methods=["POST"])
def api_astri_login():
    payload = request.get_json(silent=True) or {}
    try:
        info = astri_login_session(payload.get("username"), payload.get("password"))
        return jsonify({"connected": True, "username": info["username"]})
    except Exception as exc:
        LOGGER.warning("ASTRI login failed: %s", exc)
        return jsonify({"connected": False, "error": str(exc)}), 400

@app.route("/api/astri/logout", methods=["POST"])
def api_astri_logout():
    sid = session.get(ASTRI_SESSION_KEY, "")
    if sid:
        ASTRI_SESSIONS.pop(sid, None)
        store = load_astri_session_store()
        store.pop(sid, None)
        save_astri_session_store(store)
    session.pop(ASTRI_SESSION_KEY, None)
    session.modified = True
    return jsonify({"connected": False})


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


def _internal_boq_items(items: list[dict]) -> list[str]:
    out = []
    for item in items or []:
        vendor = str(item.get("vendor_name", item.get("vendor", ""))).strip().lower()
        if vendor != "internal":
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


def build_auto_pr_description(scope: str, match: dict, items: list[dict], permit_rt_rw: str = "") -> str:
    scope = (scope or "cluster").lower()
    match = match or {}
    items = items or []
    internal = _internal_boq_items(items)
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
        name = _clean_pr_scope_name(_first_match_value(match, "subfeeder_name", "target_subfeeder_name", "cluster_name", "name", "query"))
        hp = _first_match_value(match, "homepass", "target_subfeeder_drm_homepass", "target_cluster_drm_homepass")
        length = _boq_length_m(items, match)
        area = _first_match_value(match, "area", "area_name") or "BKS"
        lines = [f"{area} : Roll Out - SUBFEEDER {name}".strip()]
        if hp:
            lines += ["", f"Total HP : {hp} HP"]
        if length:
            if not hp:
                lines.append("")
            lines.append(f"Length : {length}m")
        lines += ["", material_note]
        return "\n".join(lines).strip()

    name = _clean_pr_scope_name(_first_match_value(match, "cluster_name", "target_cluster_name", "name", "query"))
    hp = _first_match_value(match, "homepass", "target_cluster_drm_homepass")
    lines = [f"Roll Out - Cluster {name}".strip()]
    if hp:
        lines += ["", f"Total HP : {hp} HP"]
    if permit_rt_rw:
        if not hp:
            lines.append("")
        lines.append(f"Donation Approve : IDR {_idr(permit_rt_rw)}")
    lines += ["", material_note]
    return "\n".join(lines).strip()


@app.route("/api/astri/boq/auto-description", methods=["POST"])
def api_astri_boq_auto_description():
    payload = request.get_json(silent=True) or {}
    try:
        description = build_auto_pr_description(
            payload.get("scope") or "cluster",
            payload.get("match") or {},
            payload.get("items") or [],
            payload.get("permit_rt_rw") or "",
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

ASTRI_MAGIC_LOGIN_HTML = '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8"><title>Login ASTRI...</title><meta name="referrer" content="no-referrer"><style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f0fdf4;color:#065f46}.box{text-align:center;padding:40px}.spinner{width:48px;height:48px;border:4px solid #d1fae5;border-top-color:#10b981;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}@keyframes spin{to{transform:rotate(360deg)}}h2{margin:0 0 8px}p{color:#6b7280;font-size:14px}</style></head><body><div class="box"><div class="spinner"></div><h2>Login ASTRI otomatis...</h2><p>Anda akan dialihkan ke ASTRI.</p></div><script>var c=__CREDS_JSON__;var f=document.createElement("form");f.method="POST";f.action="__LOGIN_URL__";f.style.display="none";[{n:"user-identity",v:c.user},{n:"password",v:c.pass},{n:"csrf_name",v:""},{n:"csrf_value",v:""}].forEach(function(i){var x=document.createElement("input");x.type="hidden";x.name=i.n;x.value=i.v;f.appendChild(x);});document.body.appendChild(f);setTimeout(function(){f.submit()},350);</script></body></html>'

def _astri_magic_serializer():
    if URLSafeTimedSerializer is None:
        raise RuntimeError("itsdangerous unavailable")
    salt = os.getenv("ASTRI_MAGIC_LOGIN_SALT", "eva-astri-magic-login-v1")
    return URLSafeTimedSerializer(salt + Config.SECRET_KEY)

@app.route("/api/astri/magic-token", methods=["POST"])
def api_astri_magic_token():
    try:
        record = require_astri_session()
        stored = record.get("_stored") or {}
        username = record.get("username") or stored.get("username") or ""
        encrypted_password = stored.get("encrypted_password") or ""
        if not encrypted_password:
            sid = astri_session_id()
            from_disk = load_astri_session_store().get(sid) or {}
            encrypted_password = from_disk.get("encrypted_password", "")
            if not username:
                username = from_disk.get("username", "")
        if not username or not encrypted_password:
            return jsonify({"error": "Session ASTRI tidak punya kredensial tersimpan. Login ulang dulu."}), 400
        token = _astri_magic_serializer().dumps({"u": username, "p": encrypted_password})
        return jsonify({"token": token})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception:
        LOGGER.exception("ASTRI magic token failed")
        return jsonify({"error": "Gagal generate token login ASTRI."}), 500

@app.route("/astri/magic-login/<token>")
def astri_magic_login_page(token):
    try:
        data = _astri_magic_serializer().loads(token, max_age=120)
        username = data.get("u", "")
        encrypted_password = data.get("p", "")
        password = _decrypt_astri_password(encrypted_password)
        if not username or not password:
            return "Kredensial ASTRI tidak valid. Login ulang dari EVA.", 400
        creds_json = json.dumps({"user": username, "pass": password})
        page = ASTRI_MAGIC_LOGIN_HTML.replace("__CREDS_JSON__", creds_json).replace("__LOGIN_URL__", f"{ASTRI_BASE_URL}/login-process")
        response = make_response(page)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    except SignatureExpired:
        return "Tautan login ASTRI sudah kadaluarsa. Klik lagi dari EVA.", 400
    except BadTimeSignature:
        return "Tautan login ASTRI tidak valid.", 400
    except Exception:
        LOGGER.exception("ASTRI magic login page failed")
        return "Gagal buka login otomatis ASTRI.", 500
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
        selected = [key for key in (payload.get("targets") or ["kmz", "boq", "hpdb"]) if key in ASTRI_UPLOAD_TARGETS]
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
        network_type = normalize_network_type(payload.get("network_type") or NETWORK_TYPE_AE)
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
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for doc in docs:
                response = record["session"].get(
                    f"{ASTRI_BASE_URL}/download/rfs-document",
                    params={"uuid": doc["uuid"]},
                    timeout=120,
                )
                response.raise_for_status()
                filename = secure_filename(doc.get("filename") or f"{doc.get('type_name', 'document')}.bin") or f"{doc.get('type_name', 'document')}.bin"
                archive.writestr(filename, response.content)
        bundle.seek(0)
        return send_file(
            bundle,
            mimetype="application/zip",
            as_attachment=True,
            download_name=astri_bundle_download_name(match, is_subfeeder=is_subfeeder),
            max_age=0,
        )
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
        selected = [key for key in (payload.get("targets") or ["kmz", "boq"]) if key in FEEDER_UPLOAD_TARGETS]
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
        selected = [key for key in (payload.get("targets") or ["kmz", "boq"]) if key in SUBFEEDER_UPLOAD_TARGETS]
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
        cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
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
        return jsonify({"error": str(exc)}), 400

@app.route("/api/astri/boq/submit/<job_id>", methods=["POST"])
def api_astri_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
        files = astri_job_upload_files(job)
        if "boq" not in files:
            return jsonify({"error": "Output BOQ EVA tidak tersedia.", "match": match}), 400
        create_boq = astri_create_boq_link(record, match)
        if not create_boq.get("enabled") or not create_boq.get("url"):
            return jsonify({"error": create_boq.get("error", "Link BOQ ASTRI tidak tersedia."), "match": match, "create_boq": create_boq}), 400
        items = payload.get("items") or preview_eva_boq_file(files["boq"][0])
        result = submit_astri_boq_items(record, create_boq, items, payload.get("pr_description", ""), is_subfeeder=False, permit_rt_rw=payload.get("permit_rt_rw", ""))
        return jsonify({"match": match, "create_boq": create_boq, **result})
    except Exception as exc:
        LOGGER.exception("ASTRI BOQ submit failed")
        return jsonify({"error": str(exc)}), 400







def extract_astri_form_fields(form_html: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for tag in re.findall(r'<(?:input|textarea|select)\b[^>]*>(?:.*?</textarea>)?', form_html, flags=re.I | re.S):
        attrs = extract_tag_attrs(tag)
        name = attrs.get("name")
        if not name:
            continue
        value = attrs.get("value", "")
        if tag.lower().startswith("<textarea"):
            body = re.search(r'<textarea[^>]*>(.*?)</textarea>', tag, flags=re.I | re.S)
            value = html.unescape(body.group(1)) if body else ""
        fields.append((name, value))
    return fields

def submit_astri_boq_items(record: dict, create_boq: dict, items: list[dict], pr_description: str, is_subfeeder: bool = False, permit_rt_rw: str = "") -> dict:
    parse_data: list[tuple[str, str]] = []
    vendor_from_url = ""
    plant_from_url = ""
    plant_match = re.search(r"cluster_area_plant_code=([^&]+)", create_boq.get("url", ""))
    if plant_match:
        plant_from_url = requests.utils.unquote(plant_match.group(1)).strip()
    # Try to fetch the actual BOQ form page; fall back to constructing data directly
    try:
        page = record["session"].get(create_boq["url"], timeout=60)
        LOGGER.warning("create_boq URL=%s status=%s form_found=%s len=%s", create_boq["url"], page.status_code, 'insert-boq-drm-form' in page.text, len(page.text))
        page.raise_for_status()
        form_match = re.search(r'<form[^>]+id=("|\')insert-boq-drm-form\1[^>]*>.*?</form>', page.text, flags=re.I | re.S)
        if not form_match:
            form_match = next((m for m in re.finditer(r'<form\b[^>]*>.*?</form>', page.text, flags=re.I | re.S) if "boq[" in m.group(0)), None)
        if form_match:
            fields = extract_astri_form_fields(form_match.group(0))
            url_match = re.search(r"assigned_vendor_name=([^&]+)", create_boq.get("url", ""))
            if url_match:
                vendor_from_url = requests.utils.unquote(url_match.group(1)).strip().replace(" ", "_")
            item_by_code = {str(item.get("code", "")).strip(): item for item in items}
            for name, value in fields:
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
            if permit_rt_rw:
                parse_data.append(("work_order[permit_rt_rw]", permit_rt_rw))
                parse_data.append(("permit_rt_rw", permit_rt_rw))
            parse_data.append(("__create_boq_url", create_boq.get("url", "")))
            return _submit_astri_parsed(record, parse_data, items, pr_description, vendor_from_url, is_subfeeder, permit_rt_rw, plant_from_url)
    except Exception:
        LOGGER.warning("Failed to fetch BOQ form page, constructing data directly from items")
    # Fallback: build parse data from items directly
    if is_subfeeder:
        # also try to get vendor from create_boq URL or from match data
        url_vendor = re.search(r"assigned_vendor_name=([^&]+)", create_boq.get("url", ""))
        if url_vendor:
            vendor_from_url = requests.utils.unquote(url_vendor.group(1)).strip().replace(" ", "_")
    area_val = ""
    area_m = re.search(r"cluster_area=([^&]+)", create_boq.get("url", ""))
    if area_m:
        area_val = requests.utils.unquote(area_m.group(1)).strip()
    cc = ""
    cc_m = re.search(r"cluster_code=([^&]+)", create_boq.get("url", ""))
    if cc_m:
        cc = requests.utils.unquote(cc_m.group(1)).strip()
    for item in items:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        is_service = code.startswith("5")
        parse_data.append((f"boq[{code}][quantity_material]", str(item.get("material_qty", "") or "")))
        parse_data.append((f"boq[{code}][quantity_service]", str(item.get("service_qty", "") or "")))
        parse_data.append((f"boq[{code}][equipment_name]", code))
        parse_data.append((f"boq[{code}][description]", item.get("description", code)))
        parse_data.append((f"boq[{code}][remarks]", "Service" if is_service else "Material"))
        parse_data.append((f"boq[{code}][phase]", "drm"))
        parse_data.append((f"boq[{code}][area]", area_val))
        parse_data.append((f"boq[{code}][area_plant_code]", plant_from_url))
        parse_data.append((f"boq[{code}][cluster_code]", cc))
        parse_data.append((f"boq[{code}][vendor_name]", vendor_from_url))
        parse_data.append((f"boq[{code}][override_price_service]", ""))
    # Extract cluster_code / work_order_number from the BOQ URL
    for key in ("cluster_code", "work_order_number"):
        val = re.search(rf"{key}=([^&]+)", create_boq.get("url", ""))
        if val:
            parse_data.append((key, requests.utils.unquote(val.group(1)).strip()))
    if pr_description:
        parse_data.append(("work_order_progress[detail]", "[#PR_DESCRIPTION] " + pr_description))
    if permit_rt_rw:
        parse_data.append(("work_order[permit_rt_rw]", permit_rt_rw))
        parse_data.append(("permit_rt_rw", permit_rt_rw))
    parse_data.append(("__create_boq_url", create_boq.get("url", "")))
    return _submit_astri_parsed(record, parse_data, items, pr_description, vendor_from_url, is_subfeeder, permit_rt_rw, plant_from_url)


def _submit_astri_parsed(record: dict, parse_data: list, items: list[dict], pr_description: str, vendor_from_url: str, is_subfeeder: bool, permit_rt_rw: str = "", plant_from_url: str = "") -> dict:

    create_boq_url = str(dict(parse_data).get("__create_boq_url", ""))
    is_feeder = "/material/feeder/boq-drm/" in create_boq_url

    parse_response = record["session"].post(
        f"{ASTRI_BASE_URL}/process/material/boq-drm/parse",
        data=parse_data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=120,
    )
    parse_response.raise_for_status()
    parsed_items = parse_response.json()
    LOGGER.warning("Parsed %d items: %s", len(parsed_items), parsed_items[0] if parsed_items else "none")
    url_prefix = "subfeeder/" if is_subfeeder else ("feeder/" if is_feeder else "")
    insert_url = f"{ASTRI_BASE_URL}/process/material/{url_prefix}boq-drm/insert/v2"
    pd = dict(parse_data)
    cc = pd.get("cluster_code", "")
    won = pd.get("work_order_number", "")
    inserted = []
    for parsed in parsed_items:
        if not (parsed.get("quantity_material") or parsed.get("quantity_service")):
            continue
        data = dict(parsed)
        # ASTRI parse endpoint sometimes returns subfeeder_code even for cluster BOQ.
        # When this is a cluster insert, replace subfeeder_code key with cluster_code.
        if not is_subfeeder and not is_feeder and "subfeeder_code" in data and "cluster_code" not in data:
            data["cluster_code"] = data.pop("subfeeder_code")
        elif not is_subfeeder and not is_feeder and data.get("subfeeder_code") and not data.get("cluster_code"):
            data["cluster_code"] = data.pop("subfeeder_code")
        if cc:
            data.setdefault("cluster_code", cc)
        if won:
            data.setdefault("work_order_number", won)
        if vendor_from_url and not data.get("vendor_name"):
            data["vendor_name"] = vendor_from_url
        if plant_from_url:
            data["area_plant_code"] = plant_from_url
        if is_subfeeder:
            data.setdefault("subcont_vendor_name", "")
            data.setdefault("area_plant_code", "")
        LOGGER.warning("Insert/v2 data=%s", data)
        response = record["session"].post(insert_url, data=data, timeout=120)
        ok = response.status_code in {200, 201, 302, 303}
        try:
            body = response.json()
            ok = ok and body.get("success") is not False
        except Exception:
            body = {"text": response.text[:500]}
        # Retry with alternate vendor (internal <-> assigned_vendor) if vendor_price error
        if not ok and isinstance(body, dict):
            err_msg = str(body)
            if "vendor_price_id" in err_msg or "error_pdo_exception" in err_msg or "error_third_party_exception" in err_msg:
                current_vendor = data.get("vendor_name", "")
                alt_vendor = vendor_from_url if current_vendor == "internal" else "internal"
                if alt_vendor and alt_vendor != current_vendor:
                    data["vendor_name"] = alt_vendor
                    LOGGER.warning("Retry Insert/v2 with vendor=%s (was %s)", alt_vendor, current_vendor)
                    response = record["session"].post(insert_url, data=data, timeout=120)
                    ok = response.status_code in {200, 201, 302, 303}
                    try:
                        body = response.json()
                        ok = ok and body.get("success") is not False
                    except Exception:
                        body = {"text": response.text[:500]}
        # Final fallback: if still failing due to vendor_price missing, force override_price=0
        # so ASTRI accepts the item without price-list lookup
        if not ok and isinstance(body, dict):
            err_msg = str(body)
            if "error_vendor_price_not_found" in err_msg or "vendor_price" in err_msg:
                data["vendor_name"] = ""  # Clear vendor so ASTRI skips price lookup
                data["vendor_price_id"] = ""  # MySQL converts '' → 0, bypasses NOT NULL
                data["override_price_material"] = "0"
                data["override_price_service"] = "0"
                data["use_drm_price"] = "true"
                LOGGER.warning("Retry Insert/v2 with override_price=0 + use_drm_price=true for %s", parsed.get("equipment_name"))
                response = record["session"].post(insert_url, data=data, timeout=120)
                ok = response.status_code in {200, 201, 302, 303}
                try:
                    body = response.json()
                    ok = ok and body.get("success") is not False
                except Exception:
                    body = {"text": response.text[:500]}
            # Second fallback: if override still fails (vendor_price_id NOT NULL constraint),
            # try with a hardcoded vendor_price_id=1 (ASTRI might have a dummy entry)
            if not ok and isinstance(body, dict):
                err_msg = str(body)
                if "vendor_price_id" in err_msg:
                    data["vendor_price_id"] = "1"
                    LOGGER.warning("Retry Insert/v2 with vendor_price_id=1 for %s", parsed.get("equipment_name"))
                    response = record["session"].post(insert_url, data=data, timeout=120)
                    ok = response.status_code in {200, 201, 302, 303}
                    try:
                        body = response.json()
                        ok = ok and body.get("success") is not False
                    except Exception:
                        body = {"text": response.text[:500]}
        LOGGER.warning("Insert/v2 response code=%s ok=%s body=%s", response.status_code, ok, body)
        # Categorize "no price even with override" as skipped (very rare — ASTRI rejects entirely)
        skipped_no_price = False
        if not ok and isinstance(body, dict):
            err_str = str(body)
            if "error_vendor_price_not_found" in err_str or "vendor_price" in err_str:
                skipped_no_price = True
        inserted.append({
            "equipment_name": parsed.get("equipment_name"),
            "description": parsed.get("description", ""),
            "ok": ok,
            "skipped_no_price": skipped_no_price,
            "override_used": data.get("override_price_service") == "0" or data.get("use_drm_price") == "true",
            "status_code": response.status_code,
            "response": body,
        })

    won_val = dict(parse_data).get("work_order_number") or ""
    any_ok = any(item.get("ok") for item in inserted)
    skipped_count = sum(1 for i in inserted if i.get("skipped_no_price"))
    fail_count = sum(1 for i in inserted if not i.get("ok") and not i.get("skipped_no_price"))
    ok_count = sum(1 for i in inserted if i.get("ok"))
    if not any_ok:
        LOGGER.warning("Skipping Close WO: all %d inserts failed", len(inserted))
        return {"parsed_count": len(parsed_items), "inserted": inserted, "close_ok": False, "ok": False,
                "summary": {"ok": ok_count, "failed": fail_count, "skipped_no_price": skipped_count}}
    if fail_count or skipped_count:
        LOGGER.warning("Skipping Close WO: BOQ insert incomplete ok=%d failed=%d skipped_no_price=%d", ok_count, fail_count, skipped_count)
        return {"parsed_count": len(parsed_items), "inserted": inserted, "close_ok": False, "ok": False,
                "summary": {"ok": ok_count, "failed": fail_count, "skipped_no_price": skipped_count},
                "error": "BOQ belum lengkap masuk ke ASTRI. Work order tidak di-close."}
    close_data = list(parse_data)
    close_data.extend([
        ("work_order[status]", "4"),
        ("work_order[number]", won_val),
        ("work_order[detail]", "[#PR_DESCRIPTION] " + pr_description),
        ("returnType", "json"),
    ])
    if permit_rt_rw:
        close_data.extend([
            ("work_order[permit_rt_rw]", permit_rt_rw),
            ("permit_rt_rw", permit_rt_rw),
        ])
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
        LOGGER.warning("Close WO response code=%s ok=%s body=%s", close_response.status_code, close_ok, close_body)
    except Exception as exc:
        LOGGER.warning("Close WO failed: %s", exc)
        close_ok = False
    return {"parsed_count": len(parsed_items), "inserted": inserted, "close_ok": close_ok,
            "ok": all(item.get("ok") or item.get("skipped_no_price") for item in inserted),
            "summary": {"ok": ok_count, "failed": fail_count, "skipped_no_price": skipped_count}}


@app.route("/api/astri/donation")
def api_astri_donation():
    try:
        record = require_astri_session()
        code = request.args.get("cluster_code", "")
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
                if nominal_num > 0 and pph_pct > 0:
                    permit_rt_rw = str(int(nominal_num / (1 - pph_pct)))
                elif nominal_num > 0:
                    # If vendor PPH is not registered, still pass RT/RW permit using
                    # ASTRI donation nominal. Previously this stayed empty, so cluster
                    # close/update did not send work_order[permit_rt_rw].
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
        return jsonify({"error": str(exc), "found": False}), 400


@app.route("/api/astri/subfeeder/schedule/form")
def api_astri_subfeeder_schedule_form():
    try:
        record = require_astri_session()
        match = request.args.to_dict(flat=True)
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "cluster_code dan work_order_number diperlukan."}), 400
        form = extract_astri_approve_schedule_form(fetch_astri_subfeeder_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder schedule form failed")
        return jsonify({"error": str(exc)}), 400

@app.route("/api/astri/subfeeder/schedule/approve", methods=["POST"])
def api_astri_subfeeder_schedule_approve():
    try:
        record = require_astri_session()
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/subfeeder/drm/schedule/approve",
            data=request.form.to_dict(flat=True),
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
        mcc = request.args.get("cluster_code") or ""
        mwo = request.args.get("work_order_number") or ""
        if not mcc or not mwo:
            return jsonify({"error": "Data match tidak lengkap."}), 404
        match = {
            "matched": True,
            "cluster_code": mcc,
            "work_order_number": mwo,
            "vendor_label": request.args.get("vendor_label", ""),
            "cluster_name": request.args.get("cluster_name", mcc),
            "area": request.args.get("area", ""),
            "area_plant_code": request.args.get("area_plant_code", ""),
        }
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
            cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
            code = cluster_name.split("-")[0].strip() if cluster_name else ""
            items_list = search_astri_subfeeders(record, code) if code else []
            match = items_list[0] if items_list else {"matched": False, "query": cluster_name}
            if not match.get("matched"):
                return jsonify({"error": "Subfeeder ASTRI tidak ditemukan.", "match": match}), 404
            files = astri_job_upload_files(job)
            create_boq = astri_create_subfeeder_boq_link(record, match)
            if "boq" in files:
                path, filename = files["boq"]
                items = preview_eva_boq_file(path)
                return jsonify({"match": match, "create_boq": create_boq, "filename": filename, "items": items, "total_items": len(items), "permit_rt_rw": ""})
            return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
        # No job — use match from query params
        mcc = request.args.get("cluster_code") or ""
        mwo = request.args.get("work_order_number") or ""
        if not mcc or not mwo:
            return jsonify({"error": "Job tidak ditemukan dan data match tidak lengkap."}), 404
        match = {
            "matched": True,
            "cluster_code": mcc,
            "work_order_number": mwo,
            "vendor_label": request.args.get("vendor_label", ""),
            "cluster_name": request.args.get("cluster_name", mcc),
            "area": request.args.get("area", ""),
            "area_plant_code": request.args.get("area_plant_code", ""),
        }
        create_boq = astri_create_subfeeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder BOQ preview failed")
        return jsonify({"error": str(exc)}), 400



@app.route("/api/astri/subfeeder/boq/submit/<job_id>", methods=["POST"])
def api_astri_subfeeder_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        items_list = search_astri_subfeeders(record, code) if code else []
        match = items_list[0] if items_list else {"matched": False, "query": cluster_name}
        if not match.get("matched"):
            return jsonify({"error": "Subfeeder ASTRI tidak ditemukan.", "match": match}), 404
        files = astri_job_upload_files(job)
        if "boq" not in files:
            return jsonify({"error": "Output BOQ EVA tidak tersedia.", "match": match}), 400
        create_boq = astri_create_subfeeder_boq_link(record, match)
        if not create_boq.get("url"):
            return jsonify({"error": create_boq.get("error", "Link BOQ ASTRI tidak tersedia."), "match": match, "create_boq": create_boq}), 400
        items = payload.get("items") or preview_eva_boq_file(files["boq"][0])
        result = submit_astri_boq_items(record, create_boq, items, payload.get("pr_description", ""), is_subfeeder=True, permit_rt_rw=payload.get("permit_rt_rw", ""))
        return jsonify({"match": match, "create_boq": create_boq, **result})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder BOQ submit failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/subfeeder/boq/submit", methods=["POST"])
def api_astri_subfeeder_boq_submit_nojob():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match") or {}
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "Data match ASTRI tidak lengkap."}), 400
        match["matched"] = True
        create_boq = astri_create_subfeeder_boq_link(record, match)
        items = payload.get("items") or []
        if not items:
            return jsonify({"error": "Tidak ada item BOQ."}), 400
        if not create_boq.get("url"):
            return jsonify({"error": create_boq.get("error", "Link BOQ ASTRI tidak tersedia."), "match": match, "create_boq": create_boq}), 400
        result = submit_astri_boq_items(record, create_boq, items, payload.get("pr_description", ""), is_subfeeder=True, permit_rt_rw=payload.get("permit_rt_rw", ""))
        return jsonify({"match": match, "create_boq": create_boq, **result})
    except Exception as exc:
        LOGGER.exception("ASTRI subfeeder BOQ submit no-job failed")
        return jsonify({"error": str(exc)}), 400


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
        return jsonify({"items": items, "total_items": len(items), "filename": file.filename})
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
        area = request.args.get("area", "").strip()
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
        area = request.args.get("area", "").strip()
        if not area:
            return jsonify({"data": []})
        response = record["session"].get(
            f"{ASTRI_BASE_URL}/olt/list",
            params={"filter[area]": area, "length": -1},
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
            items.append({"value": f"{code} - {name}" if code else name, "text": f"{code} - {label}" if code else label})
        return jsonify({"data": items, "count": len(items)})
    except Exception as exc:
        LOGGER.exception("ASTRI olt list failed")
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
        match = request.args.to_dict(flat=True)
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "cluster_code dan work_order_number diperlukan."}), 400
        form = extract_astri_approve_schedule_form(fetch_astri_feeder_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder schedule form failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/schedule/approve", methods=["POST"])
def api_astri_feeder_schedule_approve():
    try:
        record = require_astri_session()
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/feeder/drm/schedule/approve",
            data=request.form.to_dict(flat=True),
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
        mcc = request.args.get("cluster_code") or ""
        mwo = request.args.get("work_order_number") or ""
        if not mcc or not mwo:
            return jsonify({"error": "Data match tidak lengkap."}), 404
        match = {
            "matched": True,
            "cluster_code": mcc,
            "work_order_number": mwo,
            "vendor_label": request.args.get("vendor_label", ""),
            "cluster_name": request.args.get("cluster_name", mcc),
            "area": request.args.get("area", ""),
        }
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
            cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
            code = cluster_name.split("-")[0].strip() if cluster_name else ""
            items_list = search_astri_feeders(record, code) if code else []
            match = items_list[0] if items_list else {"matched": False, "query": cluster_name}
            if not match.get("matched"):
                return jsonify({"error": "Feeder ASTRI tidak ditemukan.", "match": match}), 404
            files = astri_job_upload_files(job)
            create_boq = astri_create_feeder_boq_link(record, match)
            if "boq" in files:
                path, filename = files["boq"]
                items = preview_eva_boq_file(path)
                return jsonify({"match": match, "create_boq": create_boq, "filename": filename, "items": items, "total_items": len(items), "permit_rt_rw": ""})
            return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
        mcc = request.args.get("cluster_code") or ""
        mwo = request.args.get("work_order_number") or ""
        if not mcc or not mwo:
            return jsonify({"error": "Job tidak ditemukan dan data match tidak lengkap."}), 404
        match = {
            "matched": True,
            "cluster_code": mcc,
            "work_order_number": mwo,
            "vendor_label": request.args.get("vendor_label", ""),
            "cluster_name": request.args.get("cluster_name", mcc),
            "area": request.args.get("area", ""),
        }
        create_boq = astri_create_feeder_boq_link(record, match)
        return jsonify({"match": match, "create_boq": create_boq, "filename": None, "items": [], "total_items": 0, "permit_rt_rw": ""})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder BOQ preview failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/boq/submit/<job_id>", methods=["POST"])
def api_astri_feeder_boq_submit(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        cluster_name = payload.get("cluster_name") or cluster_name_for_job(job)
        code = cluster_name.split("-")[0].strip() if cluster_name else ""
        items_list = search_astri_feeders(record, code) if code else []
        match = items_list[0] if items_list else {"matched": False, "query": cluster_name}
        if not match.get("matched"):
            return jsonify({"error": "Feeder ASTRI tidak ditemukan.", "match": match}), 404
        files = astri_job_upload_files(job)
        if "boq" not in files:
            return jsonify({"error": "Output BOQ EVA tidak tersedia.", "match": match}), 400
        create_boq = astri_create_feeder_boq_link(record, match)
        if not create_boq.get("url"):
            return jsonify({"error": create_boq.get("error", "Link BOQ ASTRI tidak tersedia."), "match": match, "create_boq": create_boq}), 400
        items = payload.get("items") or preview_eva_boq_file(files["boq"][0])
        result = submit_astri_boq_items(record, create_boq, items, payload.get("pr_description", ""), is_subfeeder=False, permit_rt_rw=payload.get("permit_rt_rw", ""))
        return jsonify({"match": match, "create_boq": create_boq, **result})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder BOQ submit failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/feeder/boq/submit", methods=["POST"])
def api_astri_feeder_boq_submit_nojob():
    try:
        record = require_astri_session()
        payload = request.get_json(silent=True) or {}
        match = payload.get("match") or {}
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "Data match ASTRI tidak lengkap."}), 400
        match["matched"] = True
        create_boq = astri_create_feeder_boq_link(record, match)
        items = payload.get("items") or []
        if not items:
            return jsonify({"error": "Tidak ada item BOQ."}), 400
        if not create_boq.get("url"):
            return jsonify({"error": create_boq.get("error", "Link BOQ ASTRI tidak tersedia."), "match": match, "create_boq": create_boq}), 400
        result = submit_astri_boq_items(record, create_boq, items, payload.get("pr_description", ""), is_subfeeder=False, permit_rt_rw=payload.get("permit_rt_rw", ""))
        return jsonify({"match": match, "create_boq": create_boq, **result})
    except Exception as exc:
        LOGGER.exception("ASTRI feeder BOQ submit no-job failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/schedule/form")
def api_astri_schedule_form_direct():
    try:
        record = require_astri_session()
        match = request.args.to_dict(flat=True)
        if not match.get("cluster_code") or not match.get("work_order_number"):
            return jsonify({"error": "cluster_code dan work_order_number diperlukan."}), 400
        form = extract_astri_approve_schedule_form(fetch_astri_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule form failed")
        return jsonify({"error": str(exc)}), 400


@app.route("/api/astri/schedule/form/<job_id>")
def api_astri_schedule_form(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan."}), 404
    try:
        record = require_astri_session()
        cluster_name = request.args.get("cluster_name") or cluster_name_for_job(job)
        match = astri_match_payload(find_astri_cluster(record, cluster_name), cluster_name)
        if not match.get("matched"):
            return jsonify({"error": "Cluster ASTRI tidak ditemukan.", "match": match}), 404
        form = extract_astri_approve_schedule_form(fetch_astri_requirement_page(record, match))
        return jsonify({"match": match, **form})
    except Exception as exc:
        LOGGER.exception("ASTRI schedule form failed")
        return jsonify({"error": str(exc)}), 400

@app.route("/api/astri/schedule/approve", methods=["POST"])
def api_astri_schedule_approve_direct():
    try:
        record = require_astri_session()
        form_data = request.form.to_dict(flat=True)
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/drm/schedule/approve",
            data=form_data,
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
        response = record["session"].post(
            f"{ASTRI_BASE_URL}/process/material/drm/schedule/approve",
            data=form_data,
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
    """For selected OSM district/city, overwrite admin values strictly from region.db."""
    if not defaults:
        return defaults
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
    ]
    compact = {key: summary.get(key) for key in scalar_keys if key in summary}
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
    if mode_has_astri(output_mode) and job.get("astri_download_name"):
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
            if is_feeder_like_scope(scope_type):
                if boq_template_path.exists():
                    try:
                        feeder_summary_values = calculate_feeder_boq_design_summary_values(
                            input_paths,
                            boq_template_path,
                            network_type=network_type,
                        )
                        for label, value in feeder_summary_values.items():
                            dxf_design_summary_overrides[label] = value
                            pipeline_logger.info(
                                f"DXF summary {label} uses Feeder BOQ/KMZ source: {value}."
                            )
                    except Exception as exc:
                        pipeline_logger.warning(
                            f"Could not calculate Feeder BOQ/KMZ DXF summary overrides: {exc}"
                        )
                elif network_type == NETWORK_TYPE_UG:
                    pipeline_logger.warning(
                        f"BOQ Feeder {network_type} template not found; DXF trenching/boring summary will use fallback warnings."
                    )
            elif boq_template_path.exists():
                try:
                    nro_values = calculate_boq_nro_cell_values(
                        input_paths,
                        boq_template_path,
                        [BOQ_NRO_CABLE_LOOP_HOLDER_CELL],
                    )
                    cable_loop_holder_value = nro_values.get(BOQ_NRO_CABLE_LOOP_HOLDER_CELL)
                    if cable_loop_holder_value is not None:
                        dxf_design_summary_overrides["TOTAL CABLE LOOP HOLDER/HANGER"] = cable_loop_holder_value
                        pipeline_logger.info(
                            "DXF summary TOTAL CABLE LOOP HOLDER/HANGER uses "
                            f"{BOQ_NRO_SHEET_NAME}!{BOQ_NRO_CABLE_LOOP_HOLDER_CELL}: {cable_loop_holder_value}."
                        )
                except Exception as exc:
                    pipeline_logger.warning(
                        "Could not calculate "
                        f"{BOQ_NRO_SHEET_NAME}!{BOQ_NRO_CABLE_LOOP_HOLDER_CELL} for DXF summary: {exc}"
                    )
            else:
                pipeline_logger.warning(
                    f"BOQ {network_type} template not found; DXF cable loop holder summary will use KML fallback."
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

        if mode_has_astri(output_mode):
            ensure_not_cancelled()
            astri_start, astri_end = excel_windows.get(OUTPUT_MODE_ASTRI, (10, 96))
            astri_result = generate_astri_excel(
                input_paths=input_paths,
                output_path=job.get("astri_output_path"),
                template_path=hpdb_template_path,
                defaults_path=hpdb_defaults_path,
                network_type=network_type,
                auto_geocode=hpdb_auto_geocode,
                defaults_override=hpdb_defaults_override,
                pipeline_logger=pipeline_logger,
                progress_callback=on_boq_progress(astri_start, astri_end),
                cluster_name_override=boq_nro_metadata["cluster_name"],
            )
            update_job(
                job_id,
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
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                if mode_has_dxf(output_mode):
                    archive.write(job.get("dxf_output_path"), arcname=job.get("dxf_download_name") or f"{job_id}.dxf")
                if mode_has_boq(output_mode):
                    archive.write(job.get("boq_output_path"), arcname=job.get("boq_download_name") or f"BOQ - {job_id}.xlsx")
                if mode_has_astri(output_mode):
                    archive.write(job.get("astri_output_path"), arcname=job.get("astri_download_name") or f"ASTRI - {job_id}.xlsx")
                if mode_has_hpdb(output_mode):
                    archive.write(job.get("hpdb_output_path"), arcname=job.get("hpdb_download_name") or f"HPDB - {job_id}.xlsx")
                if mode_has_kmz(output_mode):
                    for entry in (get_job(job_id) or {}).get("standard_kmz_outputs", standard_kmz_outputs):
                        archive.write(entry["path"], arcname=entry.get("download_name") or f"{job_id}_STANDARDIZED.kmz")

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
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        expected = dashboard_admin_password()
        if expected and secrets.compare_digest(password, expected):
            session["dashboard_admin"] = True
            return redirect(url_for("dashboard_home"))
        error = "Password salah."
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout", methods=["POST"])
def dashboard_logout():
    session.pop("dashboard_admin", None)
    return redirect(url_for("dashboard_login"))

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
    new_job_id = f"{job_id}-r{int(time.time())}"
    update_job(job_id, status="restarted", message=f"Restarted to {new_job_id}.")
    new_job = create_job(
        job_id=new_job_id,
        input_paths=job.get("input_paths") or [job.get("input_path", "")],
        output_path=job.get("output_path", ""),
        selected_layers=job.get("selected_layers") or ["ROAD", "KML"],
        selected_kml_folders=job.get("selected_kml_folders"),
        output_mode=job.get("output_mode", "dxf"),
        network_type=job.get("network_type", "AE"),
        scope_type=job.get("scope_type", "Cluster"),
        dxf_output_path=job.get("dxf_output_path"),
        boq_output_path=job.get("boq_output_path"),
        astri_output_path=job.get("astri_output_path"),
        hpdb_output_path=job.get("hpdb_output_path"),
        dxf_download_name=job.get("dxf_download_name"),
        boq_download_name=job.get("boq_download_name"),
        astri_download_name=job.get("astri_download_name"),
        hpdb_download_name=job.get("hpdb_download_name"),
        download_name=job.get("download_name"),
        download_mimetype=job.get("download_mimetype"),
    )
    enqueue_conversion_job(new_job_id, run_conversion_job)
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
    recent = [job for job in list_jobs(limit=50) if 'astri' in str(job.get('output_mode') or '')][:10]
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
            "metrics": metrics_snapshot(),
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
    # Override province / area_name from form or ASTRI match
    if override_province and not kecamatan_path:
        osm_admin_defaults["province"] = override_province
    if override_area_name:
        osm_admin_defaults["area_name"] = override_area_name
        osm_admin_defaults["Area"] = override_area_name
    # Auto-lookup province from city if not overridden
    _city_val = osm_admin_defaults.get("city", "")
    if _city_val and not osm_admin_defaults.get("province"):
        _prov = province_for_city(_city_val)
        if _prov and _prov != "Lainnya":
            osm_admin_defaults["province"] = _prov
    osm_admin_defaults = require_admin_defaults_from_region_db(osm_admin_defaults) or {}
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
    if job.get("astri_download_name") and job.get("astri_output_path"):
        mapping[job["astri_download_name"]] = Path(job["astri_output_path"])
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

def _bg_sync_update_record(record_id: int) -> None:
    if not OUTBOUND_SYNC_ENABLED:
        return
    try:
        threading.Thread(
            target=fdt_sync_service.sync_outbound_update_record,
            args=(record_id,),
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
        total = fdt_coding_db.count_records(Config.FDT_CODING_DB_PATH)
        olts = fdt_coding_db.list_olts(Config.FDT_CODING_DB_PATH)
        return jsonify({"total": total, "olts": olts, "source_label": fdt_coding_source_label()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    try:
        olt = fdt_coding_db.create_olt(Config.FDT_CODING_DB_PATH, request.get_json(silent=True) or {})
        sheet_result = {"created": False, "reason": "outbound_sync_disabled"}
        if OUTBOUND_SYNC_ENABLED:
            try:
                sheet_result = fdt_sync_service.create_olt_sheet_from_template(olt)
            except Exception as sheet_exc:
                LOGGER.exception("Gagal membuat sheet OLT %s.", olt.get("olt_initial"))
                sheet_result = {"created": False, "error": str(sheet_exc)}
        return jsonify({"olt": olt, "sheet": sheet_result}), 201
    except Exception as exc:
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
        limit = int(request.args.get("limit", 250))
        offset = int(request.args.get("offset", 0))
        records = fdt_coding_db.list_records(
            Config.FDT_CODING_DB_PATH,
            olt_initial=olt_initial,
            query=query,
            limit=limit,
            offset=offset,
        )
        total = fdt_coding_db.count_matching_records(
            Config.FDT_CODING_DB_PATH,
            olt_initial=olt_initial,
            query=query,
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
        records = fdt_coding_db.create_records(Config.FDT_CODING_DB_PATH, request.get_json(silent=True) or {})
        _bg_sync_create_records([rec["id"] for rec in records])
        return jsonify({"record": records[0] if records else None, "records": records}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/fdt-coding/records/bulk", methods=["PUT"])
def api_fdt_coding_bulk_update_records():
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        payload = request.get_json(silent=True) or {}
        ids = [int(record_id) for record_id in (payload.get("ids") or [])]
        before_map = {record_id: fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id) for record_id in ids}
        records = fdt_coding_db.update_records(Config.FDT_CODING_DB_PATH, ids, payload)
        for rec in records:
            record_id = int(rec["id"])
            before = before_map.get(record_id)
            moved_sheet = bool(before) and (
                fdt_coding_db.normalize_text(before.get("source_spreadsheet_id")) != fdt_coding_db.normalize_text(rec.get("source_spreadsheet_id"))
                or fdt_coding_db.normalize_text(before.get("sheet_name")).upper() != fdt_coding_db.normalize_text(rec.get("sheet_name")).upper()
            )
            if moved_sheet:
                _bg_sync_delete_record(record_id, before)
                fdt_coding_db.set_record_source_metadata(
                    Config.FDT_CODING_DB_PATH,
                    record_id,
                    "",
                    rec.get("sheet_name", ""),
                    0,
                )
                _bg_sync_create_records([record_id])
            else:
                _bg_sync_update_record(record_id)
        return jsonify({"records": records, "updated": len(records)})
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
        for record_id in ids:
            _bg_sync_delete_record(int(record_id), snapshots.get(int(record_id)))
        return jsonify({"deleted": deleted})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@app.route("/api/fdt-coding/records/<int:record_id>", methods=["PUT"])
def api_fdt_coding_update_record(record_id: int):
    protected = require_admin_api_access()
    if protected is not None:
        return protected
    try:
        before = fdt_coding_db.get_record(Config.FDT_CODING_DB_PATH, record_id)
        record = fdt_coding_db.update_record(Config.FDT_CODING_DB_PATH, record_id, request.get_json(silent=True) or {})
        moved_sheet = bool(before) and (
            fdt_coding_db.normalize_text(before.get("source_spreadsheet_id")) != fdt_coding_db.normalize_text(record.get("source_spreadsheet_id"))
            or fdt_coding_db.normalize_text(before.get("sheet_name")).upper() != fdt_coding_db.normalize_text(record.get("sheet_name")).upper()
        )
        if moved_sheet:
            if before:
                _bg_sync_delete_record(record_id, before)
            fdt_coding_db.set_record_source_metadata(
                Config.FDT_CODING_DB_PATH,
                record_id,
                "",
                record.get("sheet_name", ""),
                0,
            )
            _bg_sync_create_records([record_id])
        else:
            _bg_sync_update_record(record_id)
        return jsonify({"record": record})
    except Exception as exc:
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


@app.route("/api/gdr/vendors")
def api_gdr_vendors():
    import gdr_service
    return jsonify({"vendors": gdr_service.load_vendors()})


@app.route("/api/gdr/vendors/<code>", methods=["PUT"])
def api_gdr_vendor_save(code):
    import gdr_service
    data = request.get_json(force=True)
    vendors = gdr_service.load_vendors()
    vendors[code.upper()] = {
        "vendor_name": data.get("vendor_name", ""),
        "origin": data.get("origin", ": EMR"),
        "destination": data.get("destination", ""),
        "pic": data.get("pic", ""),
    }
    gdr_service.save_vendors(vendors)
    return jsonify({"ok": True, "count": len(vendors)})


@app.route("/api/gdr/vendors/<code>", methods=["DELETE"])
def api_gdr_vendor_delete(code):
    import gdr_service
    vendors = gdr_service.load_vendors()
    vendors.pop(code.upper(), None)
    gdr_service.save_vendors(vendors)
    return jsonify({"ok": True, "count": len(vendors)})



@app.route("/api/gdr/next-number", methods=["GET"])
def api_gdr_next_number():
    import gdr_service
    try:
        info = gdr_service.get_next_gdr_number()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/api/gdr/generate", methods=["POST"])
def api_gdr_generate():
    import gdr_service
    data = request.get_json(force=True)
    vendor = data.get("vendor", "").upper()
    clusters = data.get("clusters") or []
    if not clusters:
        clusters = [{"pr_number": data.get("pr_number", ""), "cluster": data.get("cluster", "")}]
    clusters = [
        {
            "pr_number": str(item.get("pr_number", "")).strip(),
            "cluster": str(item.get("cluster", "")).strip(),
        }
        for item in clusters
        if str(item.get("pr_number", "")).strip() or str(item.get("cluster", "")).strip()
    ]
    pr_number = clusters[0]["pr_number"] if clusters else ""
    cluster = clusters[0]["cluster"] if clusters else ""
    area = data.get("area", "")
    pmo = data.get("pmo", "")
    request_name = data.get("request_name", "")
    request_mobile = data.get("request_mobile", "")
    approve_name = data.get("approve_name", "")
    approve_mobile = data.get("approve_mobile", "")
    materials = data.get("materials", [])
    if not vendor:
        return jsonify({"error": "Vendor wajib dipilih"}), 400
    if not materials:
        return jsonify({"error": "Minimal 1 material"}), 400

    try:
        # Allocate and mark GDR number atomically
        gdr_info = gdr_service.allocate_gdr_number(vendor, [c["pr_number"] for c in clusters if c.get("pr_number")], clusters=clusters, area=area, pmo=pmo)
        gdr_label = gdr_info["label"]

        # Generate Excel file
        out_file = gdr_service.generate_gdr_excel(
            vendor=vendor,
            materials=materials,
            gdr_label=gdr_label,
            pr_number=pr_number,
            cluster=cluster,
            clusters=clusters,
            area=area,
            pmo=pmo,
            request_name=request_name,
            request_mobile=request_mobile,
            approve_name=approve_name,
            approve_mobile=approve_mobile,
        )


        # Write materials to GDR spreadsheet
        gdr_service.write_materials_to_gdr_sheet(
            vendor=vendor,
            materials=materials,
            pr_number=pr_number,
            cluster=cluster,
            gdr_label=gdr_label,
        )

        # Save to history
        history = gdr_service.load_history()
        history.insert(0, {
            "number": gdr_info["number"],
            "gdr_label": gdr_label,
            "vendor": vendor,
            "pr_number": pr_number,
            "cluster": cluster,
            "clusters": clusters,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "item_count": len(materials),
            "file": str(out_file.name),
        })
        gdr_service.save_history(history)

        return jsonify({
            "gdr_label": gdr_label,
            "number": gdr_info["number"],
            "download_url": f"/api/gdr/download/{out_file.name}",
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/gdr/download/<path:filename>")
def api_gdr_download(filename):
    from config import Config
    from pathlib import Path
    # Keep original generated filename (spaces/commas are valid) but block traversal.
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    gdr_dir = (Config.OUTPUT_DIR / "gdr").resolve()
    filepath = (gdr_dir / filename).resolve()
    if not filepath.exists() or not str(filepath).startswith(str(gdr_dir)):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)


@app.route("/api/gdr/history")
def api_gdr_history():
    import gdr_service
    return jsonify({"history": gdr_service.load_history()})


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

        # Cache extracted rows so Generate doesn't re-standardize.
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

    data = request.get_json(force=True)
    token = data.get("token", "")
    categories = data.get("categories") or None
    if not token:
        return jsonify({"error": "Token analisa tidak ada. Upload ulang."}), 400
    cache_path = Config.OUTPUT_DIR / f"{token}_coords.json"
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
    history = gdr_service.load_history()
    # Find the entry by number to get its label
    entry = next((h for h in history if h.get("number") == number), None)
    if entry is None:
        return jsonify({"error": "Riwayat GDR tidak ditemukan"}), 404
    gdr_label = entry.get("gdr_label", "")
    # Delete from spreadsheets
    try:
        gdr_service.delete_gdr_from_sheets(gdr_label)
    except Exception:
        pass
    # Delete from local history
    history = [h for h in history if h.get("number") != number]
    gdr_service.save_history(history)
    # Delete local file if exists
    out_dir = Config.BASE_DIR / "output" / "gdr"
    if gdr_label:
        for f in out_dir.glob("*"):
            if gdr_label.replace("/", "-") in f.name:
                f.unlink(missing_ok=True)
    return jsonify({"ok": True})




@app.route("/admin/gmail-rfs/auth")
def admin_gmail_rfs_auth():
    protected = dashboard_admin_required()
    if protected is not None:
        return protected
    try:
        return redirect(gmail_rfs_ingest.auth_url())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/oauth/gmail/callback")
def oauth_gmail_callback():
    try:
        callback_url = request.url
        if callback_url.startswith("http://"):
            callback_url = "https://" + callback_url[len("http://"):]
        gmail_rfs_ingest.handle_callback(callback_url)
        return "Gmail RFS OAuth sukses. Tutup tab ini, lalu jalankan /admin/gmail-rfs/run.", 200
    except Exception as exc:
        LOGGER.exception("Gmail OAuth callback gagal")
        return f"Gmail OAuth gagal: {exc}", 500


@app.route("/admin/gmail-rfs/run", methods=["GET", "POST"])
def admin_gmail_rfs_run():
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

    REGION_DB = Config.REGION_DB_PATH
    if not REGION_DB.is_file():
        return jsonify([])

    conn = sqlite3.connect(str(REGION_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        if subdistrict:
            cur.execute("""SELECT province, city, district, subdistrict FROM regions
                WHERE (subdistrict LIKE ? OR subdistrict_key = ?) AND subdistrict != ''
                ORDER BY province, city, district""", (f"%{subdistrict}%", region_db.region_key(subdistrict)))
        elif district:
            cur.execute("""SELECT province, city, district, subdistrict FROM regions
                WHERE district LIKE ? OR district_key = ?
                ORDER BY province, city, district, subdistrict""", (f"%{district}%", region_db.region_key(district)))
        elif city:
            cur.execute("""SELECT province, city, district, subdistrict FROM regions
                WHERE city LIKE ? OR city_key = ?
                ORDER BY province, city, district, subdistrict""", (f"%{city}%", region_db.region_key(city)))
        elif q:
            cur.execute("""SELECT province, city, district, subdistrict FROM regions
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

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
