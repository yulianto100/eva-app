"""Guard: no credential material in frontend-served assets.

`static/` is the Flask static_folder; `templates/` is rendered server-side.
Runtime credentials live under `.secrets/`, `data/` and `config/` and must
never be embedded in, copied into, or referenced from these directories.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("static", "templates")
SKIP_DIR_NAMES = {"node_modules", "downloads", "test-results", ".git", "backups"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svgz",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".kmz", ".kml", ".dxf", ".xlsx", ".xls", ".docx", ".pdf",
    ".db", ".sqlite", ".sqlite3", ".pyc", ".map",
}
MAX_FILE_BYTES = 2_000_000

SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("resend key", re.compile(r"\bre_[A-Za-z0-9]{16,}\b")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bghp_[0-9A-Za-z]{36}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    (
        "literal secret assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|"
            r"refresh[_-]?token|access[_-]?token)\s*[:=]\s*[\"']"
            r"[A-Za-z0-9+/=_@#$%^&*.\-]{12,}[\"']"
        ),
    ),
]

FORBIDDEN_REFS = [
    ("astri session store ref", re.compile(r"astri_sessions\.json")),
    ("dashboard admin password ref", re.compile(r"dashboard_admin_password")),
]


def _iter_candidate_files():
    for dirname in SCAN_DIRS:
        base = ROOT / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if ".bak" in path.name:
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


@pytest.mark.parametrize(
    ("label", "pattern"),
    SECRET_PATTERNS + FORBIDDEN_REFS,
    ids=[label for label, _ in SECRET_PATTERNS + FORBIDDEN_REFS],
)
def test_no_secret_material_in_frontend_assets(label, pattern):
    hits = []
    for path in _iter_candidate_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"{label} found in frontend-served files: {hits}"
