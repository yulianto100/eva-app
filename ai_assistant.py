from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import requests

from config import Config

import fdt_coding_db
import coreman_db

try:
    import rfs_sheets
except Exception:
    rfs_sheets = None


SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".docx", ".txt", ".md", ".xlsx", ".kmz"}
DEFAULT_MODEL = "mimo-v2.5-pro"
MAX_HISTORY_MESSAGES = 10
MAX_MESSAGE_CHARS = 2200
MAX_CONTEXT_CHARS = 6500


def _resolve_path(env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else Config.BASE_DIR / path


KNOWLEDGE_DIR = _resolve_path("KNOWLEDGE_DIR", "knowledge")
KNOWLEDGE_DB_PATH = _resolve_path("KNOWLEDGE_DB_PATH", "data/knowledge_index.db")
BOT_WA_ENV_PATH = Config.BASE_DIR / "eva-wa-bot" / ".env"

MAX_MEMORY_RESULTS = 8
MAX_MEMORY_AGE_DAYS = 30


class AssistantConfigError(RuntimeError):
    """Raised when the AI provider is not configured."""


class AssistantProviderError(RuntimeError):
    """Raised when the AI provider rejects or cannot answer a request."""


class MinuteRateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        window_start = now - self.window_seconds
        with self._lock:
            hits = [hit for hit in self._hits.get(key, []) if hit >= window_start]
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])))
                self._hits[key] = hits
                return False, retry_after
            hits.append(now)
            self._hits[key] = hits
        return True, 0


chat_rate_limiter = MinuteRateLimiter(limit=10, window_seconds=60)
_index_lock = threading.Lock()


SYSTEM_PROMPT = """
Kamu adalah EVA - asisten virtual yang ramah, hangat, dan helpful.
Kamu seperti teman ngobrol yang pintar dan suka bantu.

Kamu bisa bantu soal web app EVA (upload file, convert DXF/BOQ/HPDB, dll) dan juga bisa ngobrol ringan tentang topik lain.

Kamu punya akses ke database internal:
- **FDT Coding**: data FDT records per site/OLT (fdt_id, fdt_code, site_name, dll)
- **ManCore**: batch OPM per cluster (status, jumlah FDT, alias)
- **RFS**: data RFS bulanan per region/site (HP, target, progress)

Kalau user nanya soal data di atas, jawab berdasarkan data yang dikasih. Kalau datanya gak cukup, bilang aja.

**Contoh cara nanya:**
- "cek data FDT di site [nama site]" atau "cari FDT code [kode]"
- "cek coreman cluster [nama cluster]" atau "data coreman [status]"
- "cek RFS [region]" atau "data RFS [bulan]"
- "cek semua database" atau "tampilkan data semua"

Kamu tinggal jawab sesuai data yang dikirim. Jangan menambahkan atau mengarang data.

Kamu juga punya akses **pencarian web real-time**. Kalau user nanya soal:
- Kurs / nilai tukar / harga terkini
- Berita terbaru
- Cuaca / informasi real-time lainnya
Kamu akan menerima hasil pencarian web dengan format: "judul\nisi snippet\nurl".

**ATURAN WAJIB untuk data real-time:**
1. **SALIN ANGKA PERSIS** dari snippet pencarian. Contoh: kalau snippet bilang "17.833,2", tulis "Rp 17.833" atau "Rp 17.833,2".
2. **JANGAN PERNAH mengarang angka** dari ingatanmu sendiri. Angka lama di training data sudah tidak berlaku.
3. Kalau ada beberapa angka dari sumber berbeda, ambil yang paling baru atau rata-rata.
4. Kalau datanya gak cukup di snippet, bilang aja "data terbaru belum tersedia".

Aturan:
- Jawab dalam bahasa Indonesia yang santai, natural, dan dari hati.
- Kalau ditanya soal EVA/convert/file/knowledge, jawab berdasarkan info yang ada.
- Kalau ditanya hal lain (rekomendasi, tips, info umum, curhat, atau sekadar say hi), jawab aja secara natural dan helpful.
- Cukup jawab langsung dengan teks biasa. Jangan pakai JSON format.
- Kalau perlu mengarahkan user ke fitur EVA, sebutkan langsung di jawaban (contoh: "Coba klik menu X di atas").
- Jangan pernah menyebut API key, env secret, atau detail internal server.
- Jangan menyebut atau menampilkan nama dokumen/sumber referensi.

Kamu punya memori dari percakapan sebelumnya dengan semua user. Gunakan info ini untuk:
- Mengingat topik yang sering ditanyakan orang
- Memberikan jawaban yang lebih personal dan kontekstual
- Merasa seperti sudah kenal dengan user yang sedang ngobrol
- Jangan sebutkan secara eksplit bahwa kamu "mengingat" sesuatu, cukup natural aja.
""".strip()


def _connect() -> sqlite3.Connection:
    KNOWLEDGE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(KNOWLEDGE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL,
            title TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eva_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            keywords TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_path ON kb_chunks(relative_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session ON eva_memories(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON eva_memories(created_at)")
    conn.commit()


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kb_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kb_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def _safe_relative_path(path: Path) -> str:
    return str(path.relative_to(KNOWLEDGE_DIR).as_posix())


def _extract_keywords(text: str) -> str:
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    stopwords = {"yang", "dan", "untuk", "dengan", "ini", "itu", "ada", "tidak",
                 "bisa", "dari", "pada", "juga", "akan", "sudah", "adalah",
                 "atau", "karena", "tetapi", "maka", "seperti", "hanya",
                 "kamu", "eva", "apa", "bagaimana", "gimana", "kenapa",
                 "mengapa", "siapa", "dimana", "kapan", "halo", "hai",
                 "help", "cara", "bantu", "tolong", "please", "thanks"}
    keywords = [w for w in words if w not in stopwords]
    return " ".join(keywords[:20])


def save_memory(session_id: str, role: str, content: str) -> None:
    try:
        conn = _connect()
        _init_schema(conn)
        keywords = _extract_keywords(content) if role == "user" else ""
        conn.execute(
            "INSERT INTO eva_memories(session_id, role, content, keywords, created_at) VALUES(?, ?, ?, ?, ?)",
            (session_id, role, content, keywords, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def search_memories(query: str, limit: int = MAX_MEMORY_RESULTS) -> list[dict[str, str]]:
    try:
        conn = _connect()
        _init_schema(conn)
        cutoff = time.time() - (MAX_MEMORY_AGE_DAYS * 86400)
        keywords = _extract_keywords(query)
        if not keywords.strip():
            return []
        keyword_list = keywords.split()[:10]
        conditions = " OR ".join(["keywords LIKE ?"] * len(keyword_list))
        params = [f"%{kw}%" for kw in keyword_list]
        params.append(cutoff)
        rows = conn.execute(
            f"""SELECT session_id, content, created_at
                FROM eva_memories
                WHERE role = 'user' AND ({conditions}) AND created_at > ?
                ORDER BY created_at DESC
                LIMIT ?""",
            params + [limit * 3],
        ).fetchall()
        conn.close()
        seen = set()
        results = []
        for row in rows:
            content = row["content"]
            if content not in seen:
                seen.add(content)
                results.append({"content": content, "session_id": row["session_id"]})
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def get_recent_memories(limit: int = 5) -> list[str]:
    try:
        conn = _connect()
        _init_schema(conn)
        rows = conn.execute(
            """SELECT content FROM eva_memories
               WHERE role = 'user'
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [row["content"] for row in rows]
    except Exception:
        return []


def cleanup_old_memories() -> int:
    try:
        conn = _connect()
        _init_schema(conn)
        cutoff = time.time() - (MAX_MEMORY_AGE_DAYS * 86400)
        cursor = conn.execute("DELETE FROM eva_memories WHERE created_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return 0


def list_knowledge_files() -> list[Path]:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return files


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Dependency pypdf belum terpasang untuk membaca PDF.") from exc

    reader = PdfReader(str(path))
    parts = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"Page {index}\n{text}")
    return "\n\n".join(parts)


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError("Dependency python-pptx belum terpasang untuk membaca PPTX.") from exc

    presentation = Presentation(str(path))
    parts = []
    for slide_index, slide in enumerate(presentation.slides, start=1):
        slide_parts = []
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and text.strip():
                slide_parts.append(text.strip())
        if slide_parts:
            parts.append(f"Slide {slide_index}\n" + "\n".join(slide_parts))
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Dependency python-docx belum terpasang untuk membaca DOCX.") from exc

    document = Document(str(path))
    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Dependency openpyxl belum terpasang untuk membaca XLSX.") from exc

    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        parts.append(f"[Sheet: {sheet_name}]")
        for row in ws.iter_rows(max_row=200, values_only=True):
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            if cells:
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _extract_kmz(path: Path) -> str:
    import zipfile
    import xml.etree.ElementTree as ET

    parts = []
    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith(".kml"):
                    with zf.open(name) as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        ns = {"kml": "http://www.opengis.net/kml/2.2"}
                        for pm in root.findall(".//kml:Placemark", ns):
                            name_el = pm.find("kml:name", ns)
                            desc_el = pm.find("kml:description", ns)
                            pm_name = name_el.text.strip() if name_el is not None and name_el.text else ""
                            pm_desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                            if pm_name or pm_desc:
                                parts.append(f"{pm_name}: {pm_desc}" if pm_desc else pm_name)
    except Exception:
        pass
    return "\n".join(parts) if parts else f"[KMZ file: {path.name} - no readable KML content]"


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix == ".kmz":
        return _extract_kmz(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk_text(text: str, size: int = 1200, overlap: int = 160) -> list[str]:
    text = _normalize_text(text)
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + 400:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]


def reindex_knowledge() -> dict[str, Any]:
    with _index_lock:
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        indexed_files = 0
        chunk_count = 0
        errors: list[dict[str, str]] = []

        with _connect() as conn:
            _init_schema(conn)
            conn.execute("DELETE FROM kb_chunks")
            for path in list_knowledge_files():
                relative_path = _safe_relative_path(path)
                try:
                    chunks = _chunk_text(_extract_text(path))
                    for chunk_index, chunk in enumerate(chunks):
                        conn.execute(
                            """
                            INSERT INTO kb_chunks(relative_path, title, chunk_index, chunk_text, updated_at)
                            VALUES(?, ?, ?, ?, ?)
                            """,
                            (relative_path, path.stem, chunk_index, chunk, now),
                        )
                    if chunks:
                        indexed_files += 1
                        chunk_count += len(chunks)
                except Exception as exc:
                    errors.append({"file": relative_path, "error": str(exc)})

            _set_meta(conn, "last_indexed_at", now)
            _set_meta(conn, "indexed_files", indexed_files)
            _set_meta(conn, "chunk_count", chunk_count)
            _set_meta(conn, "errors", errors[:20])
            conn.commit()

        return {
            "ok": True,
            "indexed_files": indexed_files,
            "chunks": chunk_count,
            "errors": errors[:20],
            "knowledge_dir": str(KNOWLEDGE_DIR),
            "last_indexed_at": now,
        }


def knowledge_status() -> dict[str, Any]:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        _init_schema(conn)
        indexed_files = int(conn.execute("SELECT COUNT(DISTINCT relative_path) AS count FROM kb_chunks").fetchone()["count"])
        chunk_count = int(conn.execute("SELECT COUNT(*) AS count FROM kb_chunks").fetchone()["count"])
        return {
            "ok": True,
            "knowledge_dir": str(KNOWLEDGE_DIR),
            "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            "source_files": len(list_knowledge_files()),
            "indexed_files": indexed_files,
            "chunks": chunk_count,
            "last_indexed_at": _get_meta(conn, "last_indexed_at"),
            "errors": _get_meta(conn, "errors", []),
        }


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9_\-/]{1,}", text.lower())
    return [token for token in tokens if len(token) > 1][:18]


def search_knowledge(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    tokens = _tokenize(query)
    if not tokens or not KNOWLEDGE_DB_PATH.exists():
        return []

    phrase = query.lower().strip()
    results: list[dict[str, Any]] = []
    with _connect() as conn:
        _init_schema(conn)
        rows = conn.execute(
            "SELECT relative_path, title, chunk_index, chunk_text FROM kb_chunks"
        ).fetchall()

    for row in rows:
        title = row["title"]
        text = row["chunk_text"]
        haystack = f"{title}\n{text}".lower()
        score = 0.0
        for token in tokens:
            score += haystack.count(token)
            if token in title.lower():
                score += 2
        if len(phrase) >= 5 and phrase in haystack:
            score += 6
        if score <= 0:
            continue
        score = score / (1 + len(text) / 1800)
        results.append(
            {
                "title": title,
                "source": row["relative_path"],
                "chunk_index": row["chunk_index"],
                "chunk": text[:900],
                "score": round(score, 3),
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:top_k]


def _compact_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    compacted: list[dict[str, str]] = []
    for item in history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        compacted.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    return compacted


def _compact_page_context(page_context: Any) -> str:
    if not isinstance(page_context, dict):
        return "{}"
    allowed = {
        "current_page",
        "available_elements",
        "app_state",
        "selected_outputs",
        "selected_files",
    }
    compacted = {key: page_context.get(key) for key in allowed if key in page_context}
    return json.dumps(compacted, ensure_ascii=False)[:2400]


def web_search(query: str, max_results: int = 5) -> str:
    searxng_url = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    if not searxng_url:
        return ""

    try:
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
            "language": "id",
            "safesearch": 0,
        }
        q_lower = query.lower()
        if any(k in q_lower for k in ["kurs", "harga", "rate", "hari ini", "sekarang", "terkini", "terbaru", "cuaca", "weather"]):
            params["time_range"] = "week"

        resp = requests.get(
            f"{searxng_url}/search",
            params=params,
            headers={"User-Agent": "EVA-Converter/1.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        results = []
        for item in data.get("results", [])[:max_results]:
            title = item.get("title", "")
            snippet = item.get("content", "")
            url = item.get("url", "")
            if title or snippet:
                results.append(f"{title}\n{snippet}\n{url}")
        return "\n\n".join(results) if results else ""
    except Exception:
        return ""


def _needs_realtime(query: str) -> bool:
    keywords = [
        "kurs", "harga", "rate", "nilai tukar", "usd", "dollar", "rupiah",
        "update", "real-time", "realtime", "terkini", "sekarang", "hari ini",
        "cuaca", "weather", "berita", "news", "terbaru", "price", "exchange",
        "saham", "stock", "crypto", "bitcoin", "harga emas", "gold price",
        "inflasi", "inflation", "gdp", "suku bunga", "interest rate",
    ]
    q = query.lower()
    for kw in keywords:
        if kw in q:
            return True
    return len(q.split()) <= 3 and any(w in q for w in ["kurs", "harga", "rate", "weather", "berita"])


def search_all_databases(query: str) -> str:
    parts = []
    q = query.lower().strip()
    if not q:
        return ""

    # FDT Coding
    try:
        fdt_total = fdt_coding_db.count_records(Config.FDT_CODING_DB_PATH)
        fdt_olts = fdt_coding_db.list_olts(Config.FDT_CODING_DB_PATH)
        if fdt_total > 0:
            parts.append(f"FDT Coding: {fdt_total} records, {len(fdt_olts)} OLTs")
        if len(q) > 2:
            fdt_results = fdt_coding_db.list_records(Config.FDT_CODING_DB_PATH, query=q, limit=10)
            if fdt_results:
                rows = []
                for r in fdt_results:
                    rows.append(f"- {r['site_name']} | FDT {r['fdt_id']} | {r['fdt_code']} | OLT {r['olt_initial']}")
                parts.append("FDT Coding matches:\n" + "\n".join(rows))
    except Exception:
        pass

    # Coreman OPM
    try:
        core_summary = coreman_db.dashboard_summary(Config.COREMAN_DB_PATH)
        if core_summary.get("total_batches", 0) > 0:
            parts.append(
                f"ManCore: {core_summary['total_batches']} batches "
                f"({core_summary['pending_batches']} pending), "
                f"{core_summary['total_aliases']} aliases, "
                f"{core_summary['total_sheets']} sheets"
            )
        if len(q) > 2:
            core_batches = coreman_db.list_opm_batches(Config.COREMAN_DB_PATH, limit=20)
            matched = []
            for b in core_batches:
                if q in (b.get("cluster_name") or "").lower() or q in (b.get("status") or "").lower():
                    matched.append(f"- {b['cluster_name']} | {b['status']} | {b.get('detected_fdt_count', 0)} FDT")
            if matched:
                parts.append("ManCore matches:\n" + "\n".join(matched[:10]))
    except Exception:
        pass

    # RFS
    if rfs_sheets:
        try:
            rfs_month = rfs_sheets.month_key()
            rfs_summary = rfs_sheets.summarize()
            if rfs_summary:
                parts.append(f"RFS ({rfs_month}):\n{rfs_sheets.format_month_summary(rfs_summary)}")
            if len(q) > 2:
                rfs_search = rfs_sheets.search_rfs(q)
                if rfs_search:
                    parts.append(rfs_search)
        except Exception:
            pass

    return "\n\n".join(parts) if parts else ""


def _build_messages(message: str, history: Any, page_context: Any, chunks: list[dict[str, Any]]) -> list[dict[str, str]]:
    context_parts = []
    for index, chunk in enumerate(chunks, start=1):
        context_parts.append(
            f"[{index}] {chunk['title']} ({chunk['source']})\n{chunk['chunk']}"
        )
    knowledge_context = "\n\n".join(context_parts)[:MAX_CONTEXT_CHARS]
    page_context_json = _compact_page_context(page_context)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    memories = search_memories(message, limit=5)
    recent = get_recent_memories(limit=3)
    all_memories = list(dict.fromkeys(
        [m["content"] for m in memories] + recent
    ))[:8]

    if all_memories:
        mem_text = "\n".join(f"- {m}" for m in all_memories)
        messages.append({"role": "system", "content": f"Percakapan sebelumnya dengan user lain:\n{mem_text}"})

    db_context = search_all_databases(message)
    if db_context:
        messages.append({"role": "system", "content": f"Data dari database:\n{db_context}"})

    if _needs_realtime(message):
        web_results = web_search(message)
        if web_results:
            messages.append({"role": "system", "content": f"Hasil pencarian web:\n{web_results}"})

    if knowledge_context:
        messages.append({"role": "system", "content": f"Knowledge context:\n{knowledge_context}"})
    messages.append({"role": "system", "content": f"Page context:\n{page_context_json}"})
    messages.extend(_compact_history(history))
    messages.append({"role": "user", "content": message[:MAX_MESSAGE_CHARS]})
    return messages


def _provider_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _try_extract_content(text: str) -> str:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ("content", "reply", "answer", "response"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _provider_config() -> tuple[str, str, str]:
    bot_env = _read_env_file(BOT_WA_ENV_PATH)
    api_key = (
        os.getenv("XIAOMI_MIMO_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or bot_env.get("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("XIAOMI_MIMO_BASE_URL", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or bot_env.get("OPENAI_BASE_URL", "").strip()
    )
    model = (
        os.getenv("XIAOMI_MIMO_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or bot_env.get("OPENAI_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    return api_key, base_url, model


def _post_mimo_chat(messages: list[dict[str, str]]) -> str:
    api_key, base_url, model = _provider_config()
    if not api_key or not base_url:
        raise AssistantConfigError("Xiaomi MiMo API belum dikonfigurasi.")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 700,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = _provider_url(base_url)
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code in {400, 415, 422}:
            payload.pop("response_format", None)
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code != 200:
            response.raise_for_status()
        data = response.json()
        msg = data.get("choices", [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        if not content and reasoning:
            content = reasoning
        if not content:
            raise AssistantProviderError("Provider tidak mengirim jawaban.")
        parsed_inner = _try_extract_content(content)
        if parsed_inner:
            content = parsed_inner
        content = re.sub(r"\{[^{}]*\"thoughts?\"\s*:\s*\"[^\"]*\"[^{}]*\}", "", content).strip()
        content = content.strip("\"'")
        return content
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {401, 403}:
            message = "AI provider menolak API key. Cek OPENAI_API_KEY/XIAOMI_MIMO_API_KEY."
        elif status_code == 402:
            message = "AI provider balas 402 Payment Required. Cek billing atau quota API key."
        elif status_code == 429:
            message = "AI provider sedang rate limit. Coba lagi sebentar."
        else:
            message = "Gagal menghubungi AI provider."
        raise AssistantProviderError(message) from exc
    except AssistantProviderError:
        raise
    except requests.RequestException as exc:
        raise AssistantProviderError("Gagal menghubungi AI provider.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise AssistantProviderError("Format jawaban Xiaomi MiMo tidak valid.") from exc


def _parse_json_reply(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {"reply": cleaned, "action": {"type": "none"}}


def _sanitize_action(action: Any) -> dict[str, str]:
    if not isinstance(action, dict):
        return {"type": "none"}
    action_type = str(action.get("type") or "none").strip()
    if action_type not in {"none", "highlight", "scroll_to", "open_panel", "start_tour"}:
        action_type = "none"
    target = str(action.get("target") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", target):
        target = ""
    return {"type": action_type, "target": target}


def ai_chat(payload: dict[str, Any]) -> dict[str, Any]:
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("Message wajib diisi.")
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS]

    session_id = str(payload.get("session_id") or f"web_{int(time.time())}")

    chunks = search_knowledge(message, top_k=5)
    messages = _build_messages(message, payload.get("history"), payload.get("page_context"), chunks)
    provider_content = _post_mimo_chat(messages)
    parsed = _parse_json_reply(provider_content)
    reply = str(parsed.get("reply") or provider_content).strip()
    action = _sanitize_action(parsed.get("action"))

    save_memory(session_id, "user", message)
    save_memory(session_id, "assistant", reply)

    return {
        "reply": reply,
        "action": action,
        "model": _provider_config()[2],
    }

