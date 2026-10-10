import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from config import Config
from converter import DXF_LAYERS

LOGGER = logging.getLogger(__name__)
MAX_JOB_LOGS = 300
JOB_SLOT_KEY = "job-slot"


def _now() -> float:
    return time.time()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _copy_job(job: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(_json_safe(job))


class JobStore(Protocol):
    name: str

    def create_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        ...

    def update_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        ...

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        ...

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        ...

    def delete_job(self, job_id: str) -> bool:
        ...

    def append_log(self, job_id: str, severity: str, message: str) -> dict[str, Any] | None:
        ...

    def cleanup_jobs(self, max_age_seconds: int) -> int:
        ...

    def acquire_slot(self, job_id: str, ttl_seconds: int) -> str | None:
        ...

    def refresh_slot(self, token: str, ttl_seconds: int) -> bool:
        ...

    def release_slot(self, token: str) -> None:
        ...


class MemoryJobStore:
    name = "memory"

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._slots: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._jobs[job_id] = _copy_job(job)
            return _copy_job(self._jobs[job_id])

    def update_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.update(_json_safe(changes))
            job["updated_at"] = _now()
            return _copy_job(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return _copy_job(job) if job else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: float(job.get("updated_at") or job.get("created_at") or 0),
                reverse=True,
            )
            return [_copy_job(job) for job in jobs[: max(1, int(limit))]]

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def append_log(self, job_id: str, severity: str, message: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            entry = _append_log_to_job(job, severity, message)
            return _copy_job(entry)

    def cleanup_jobs(self, max_age_seconds: int) -> int:
        now = _now()
        removed = 0
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                is_finished = job.get("status") in {"done", "error", "failed", "cancelled"}
                if is_finished and now - float(job.get("created_at", now)) > max_age_seconds:
                    self._jobs.pop(job_id, None)
                    removed += 1
            self._cleanup_expired_slots(now)
        return removed

    def acquire_slot(self, job_id: str, ttl_seconds: int) -> str | None:
        now = _now()
        with self._lock:
            self._cleanup_expired_slots(now)
            max_slots = Config.CONVERSION_MAX_CONCURRENT_JOBS
            if len(self._slots) >= max_slots:
                return None
            token = f"{job_id}:{now:.6f}"
            self._slots[token] = {"job_id": job_id, "expires_at": now + ttl_seconds}
            return token

    def refresh_slot(self, token: str, ttl_seconds: int) -> bool:
        with self._lock:
            slot = self._slots.get(token)
            if not slot:
                return False
            slot["expires_at"] = _now() + ttl_seconds
            return True

    def release_slot(self, token: str) -> None:
        with self._lock:
            self._slots.pop(token, None)

    def _cleanup_expired_slots(self, now: float) -> None:
        for token, slot in list(self._slots.items()):
            if float(slot.get("expires_at", 0)) <= now:
                self._slots.pop(token, None)


class RedisJobStore:
    name = "redis"

    def __init__(self, redis_url: str, prefix: str, ttl_seconds: int) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._redis.ping()
        self._prefix = prefix.strip(":") or "eva"
        self._ttl_seconds = max(int(ttl_seconds), 60)

    def create_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        safe_job = _copy_job(job)
        self._write_job(job_id, safe_job)
        return _copy_job(safe_job)

    def update_job(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        job.update(_json_safe(changes))
        job["updated_at"] = _now()
        self._write_job(job_id, job)
        return _copy_job(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._job_key(job_id))
        if not raw:
            return None
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Redis job payload rusak untuk job %s", job_id)
            return None
        return _copy_job(job)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for key in self._redis.scan_iter(match=f"{self._prefix}:job:*", count=200):
            raw = self._redis.get(key)
            if not raw:
                continue
            try:
                jobs.append(json.loads(raw))
            except json.JSONDecodeError:
                LOGGER.warning("Redis job payload rusak untuk key %s", key)
        jobs.sort(
            key=lambda job: float(job.get("updated_at") or job.get("created_at") or 0),
            reverse=True,
        )
        return [_copy_job(job) for job in jobs[: max(1, int(limit))]]

    def delete_job(self, job_id: str) -> bool:
        return bool(self._redis.delete(self._job_key(job_id)))

    def append_log(self, job_id: str, severity: str, message: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        entry = _append_log_to_job(job, severity, message)
        self._write_job(job_id, job)
        return _copy_job(entry)

    def cleanup_jobs(self, max_age_seconds: int) -> int:
        now = _now()
        removed = 0
        for key in self._redis.scan_iter(match=f"{self._prefix}:job:*", count=200):
            raw = self._redis.get(key)
            if not raw:
                continue
            try:
                job = json.loads(raw)
            except json.JSONDecodeError:
                self._redis.delete(key)
                removed += 1
                continue
            is_finished = job.get("status") in {"done", "error", "failed", "cancelled"}
            if is_finished and now - float(job.get("created_at", now)) > max_age_seconds:
                self._redis.delete(key)
                removed += 1
        return removed

    def acquire_slot(self, job_id: str, ttl_seconds: int) -> str | None:
        # A cancelled/finished task can leave a Redis lease behind if Docker
        # terminates its process before the task's finally block runs.
        self.reclaim_terminal_slots()
        token = f"{job_id}:{_now():.6f}"
        for index in range(Config.CONVERSION_MAX_CONCURRENT_JOBS):
            key = self._slot_key(index)
            if self._redis.set(key, token, nx=True, ex=max(ttl_seconds, 60)):
                return f"{index}:{token}"
        return None

    def reclaim_terminal_slots(self) -> int:
        """Release only leases whose owning job is already terminal."""
        reclaimed = 0
        terminal_statuses = {"done", "error", "failed", "cancelled"}
        for index in range(Config.CONVERSION_MAX_CONCURRENT_JOBS):
            key = self._slot_key(index)
            raw_token = self._redis.get(key)
            if not raw_token:
                continue
            job_id = raw_token.split(":", 1)[0]
            raw_job = self._redis.get(self._job_key(job_id))
            if not raw_job:
                continue
            try:
                status = str(json.loads(raw_job).get("status") or "").lower()
            except json.JSONDecodeError:
                continue
            if status not in terminal_statuses:
                continue
            # Compare-and-delete prevents deleting a lease renewed by another
            # task while this worker was inspecting it.
            deleted = self._redis.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                    return redis.call('DEL', KEYS[1])
                end
                return 0
                """,
                1,
                key,
                raw_token,
            )
            reclaimed += int(deleted or 0)
        return reclaimed

    def refresh_slot(self, token: str, ttl_seconds: int) -> bool:
        index, raw_token = self._split_slot_token(token)
        key = self._slot_key(index)
        current = self._redis.get(key)
        if current != raw_token:
            return False
        self._redis.expire(key, max(ttl_seconds, 60))
        return True

    def release_slot(self, token: str) -> None:
        index, raw_token = self._split_slot_token(token)
        key = self._slot_key(index)
        current = self._redis.get(key)
        if current == raw_token:
            self._redis.delete(key)

    def _write_job(self, job_id: str, job: dict[str, Any]) -> None:
        payload = json.dumps(_json_safe(job), ensure_ascii=False, separators=(",", ":"))
        self._redis.set(self._job_key(job_id), payload, ex=self._ttl_seconds)

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}"

    def _slot_key(self, index: int) -> str:
        return f"{self._prefix}:slot:{JOB_SLOT_KEY}:{index}"

    def _split_slot_token(self, token: str) -> tuple[int, str]:
        index, raw_token = token.split(":", 1)
        return int(index), raw_token


def _append_log_to_job(job: dict[str, Any], severity: str, message: str) -> dict[str, Any]:
    normalized_severity = severity.upper()
    if normalized_severity == "WARN":
        normalized_severity = "WARNING"
    if normalized_severity not in {"INFO", "SUCCESS", "WARNING", "ERROR"}:
        normalized_severity = "INFO"

    job["log_seq"] = int(job.get("log_seq", 0)) + 1
    entry = {
        "id": job["log_seq"],
        "time": time.strftime("%H:%M:%S"),
        "severity": normalized_severity,
        "message": str(message),
    }
    logs = job.setdefault("logs", [])
    logs.append(entry)
    if len(logs) > MAX_JOB_LOGS:
        del logs[: len(logs) - MAX_JOB_LOGS]

    counts = job.setdefault("log_counts", {"WARNING": 0, "ERROR": 0})
    if normalized_severity in counts:
        counts[normalized_severity] = int(counts.get(normalized_severity, 0)) + 1
    job["updated_at"] = _now()
    return entry


def _build_store() -> JobStore:
    if Config.REDIS_URL:
        try:
            store = RedisJobStore(
                Config.REDIS_URL,
                Config.JOB_STORE_KEY_PREFIX,
                Config.JOB_STORE_TTL_SECONDS,
            )
            LOGGER.info("Job store aktif: Redis (%s)", Config.REDIS_URL)
            return store
        except Exception as exc:
            LOGGER.warning("Redis job store tidak tersedia; fallback ke memory. Detail: %s", exc)
    return MemoryJobStore()


_store: JobStore = _build_store()


class JobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def info(self, message: str) -> None:
        append_job_log(self.job_id, "INFO", message)

    def success(self, message: str) -> None:
        append_job_log(self.job_id, "SUCCESS", message)

    def warn(self, message: str) -> None:
        append_job_log(self.job_id, "WARNING", message)

    def warning(self, message: str) -> None:
        self.warn(message)

    def error(self, message: str) -> None:
        append_job_log(self.job_id, "ERROR", message)

    def progress(self, progress: int, message: str) -> None:
        current_job = get_job(self.job_id) or {}
        update_job(
            self.job_id,
            progress=max(int(current_job.get("progress", 0) or 0), int(progress)),
            message=str(message),
        )


def create_job_logger(job_id: str) -> JobLogger:
    return JobLogger(job_id)


def append_job_log(job_id: str, severity: str, message: str) -> dict[str, Any] | None:
    return _store.append_log(job_id, severity, message)


def create_job(
    job_id: str,
    input_paths: list[str | Path],
    output_path: str | Path,
    selected_layers: list[str],
    selected_kml_folders: list[str] | None = None,
    road_clip_boundary: bool = True,
    download_name: str | None = None,
    output_mode: str = "dxf",
    dxf_output_path: str | Path | None = None,
    boq_output_path: str | Path | None = None,
    astri_output_path: str | Path | None = None,
    hpdb_output_path: str | Path | None = None,
    dxf_download_name: str | None = None,
    boq_download_name: str | None = None,
    astri_download_name: str | None = None,
    hpdb_download_name: str | None = None,
    download_mimetype: str | None = None,
    network_type: str = "AE",
    scope_type: str = "Cluster",
    owner_user_id: str = "",
    astri_session_id: str = "",
    owner_name: str = "",
    owner_email: str = "",
) -> dict[str, Any]:
    normalized_input_paths = [str(path) for path in input_paths]
    job = {
        "job_id": job_id,
        "owner_user_id": str(owner_user_id),
        "owner_name": str(owner_name),
        "owner_email": str(owner_email),
        "astri_session_id": str(astri_session_id),
        "input_paths": normalized_input_paths,
        "input_path": normalized_input_paths[0],
        "output_path": str(output_path),
        "download_name": download_name or f"osm-to-dxf-merged-{job_id}.dxf",
        "download_mimetype": download_mimetype or "application/dxf",
        "output_mode": output_mode,
        "network_type": network_type,
        "scope_type": scope_type,
        "dxf_output_path": str(dxf_output_path or output_path),
        "boq_output_path": str(boq_output_path) if boq_output_path else None,
        "astri_output_path": str(astri_output_path) if astri_output_path else None,
        "hpdb_output_path": str(hpdb_output_path) if hpdb_output_path else None,
        "dxf_download_name": dxf_download_name,
        "boq_download_name": boq_download_name,
        "astri_download_name": astri_download_name,
        "hpdb_download_name": hpdb_download_name,
        "status": "queued",
        "progress": 0,
        "message": "Job masuk antrean.",
        "stats": {layer: 0 for layer in DXF_LAYERS},
        "selected_layers": selected_layers,
        "selected_kml_folders": selected_kml_folders,
        "road_clip_boundary": bool(road_clip_boundary),
        "logs": [],
        "log_seq": 0,
        "log_counts": {"WARNING": 0, "ERROR": 0},
        "error": None,
        "retry_count": 0,
        "max_retries": Config.JOB_MAX_RETRIES,
        "last_retry_at": None,
        "retry_errors": [],
        "dead_letter": False,
        "created_at": _now(),
        "updated_at": _now(),
        "preview_geojson": {"type": "FeatureCollection", "features": []},
        "store_backend": _store.name,
    }
    return _store.create_job(job_id, job)


def update_job(job_id: str, **changes) -> dict[str, Any] | None:
    return _store.update_job(job_id, changes)


def get_job(job_id: str) -> dict[str, Any] | None:
    return _store.get_job(job_id)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    return _store.list_jobs(limit)


def delete_job(job_id: str) -> bool:
    return _store.delete_job(job_id)


def cleanup_jobs(max_age_seconds: int) -> int:
    return _store.cleanup_jobs(max_age_seconds)


def record_job_retry(
    job_id: str,
    error_message: str,
    retry_count: int,
    max_retries: int,
    delay_seconds: int | float | None = None,
) -> dict[str, Any] | None:
    job = get_job(job_id) or {}
    retry_errors = list(job.get("retry_errors") or [])
    retry_errors.append(
        {
            "at": _now(),
            "message": str(error_message),
        }
    )
    delay_text = f" Retry dalam {int(delay_seconds)}s." if delay_seconds is not None else ""
    append_job_log(
        job_id,
        "WARNING",
        f"Job gagal (retry {retry_count}/{max_retries}).{delay_text} Detail: {error_message}",
    )
    return update_job(
        job_id,
        status="retrying",
        message=f"Job gagal sementara, menunggu retry {retry_count}/{max_retries}.",
        error=str(error_message),
        retry_count=retry_count,
        max_retries=max_retries,
        last_retry_at=_now(),
        retry_errors=retry_errors,
        dead_letter=False,
    )


def mark_job_dead_letter(
    job_id: str,
    error_message: str,
    duration_seconds: float | None = None,
) -> dict[str, Any] | None:
    job = get_job(job_id) or {}
    retry_errors = list(job.get("retry_errors") or [])
    if not retry_errors or retry_errors[-1].get("message") != str(error_message):
        retry_errors.append(
            {
                "at": _now(),
                "message": str(error_message),
            }
        )
    append_job_log(
        job_id,
        "ERROR",
        f"Job masuk dead letter setelah retry maksimum. Detail: {error_message}",
    )
    changes: dict[str, Any] = {
        "status": "failed",
        "progress": 100,
        "message": "Job gagal setelah retry maksimum.",
        "error": str(error_message),
        "finished_at": _now(),
        "retry_errors": retry_errors,
        "dead_letter": True,
    }
    if duration_seconds is not None:
        changes["duration_seconds"] = float(duration_seconds)
    return update_job(job_id, **changes)


def recover_stale_jobs(max_processing_seconds: int = 1800) -> list[str]:
    """
    Cari job yang stuck di 'processing' lebih dari threshold.

    Reset status ke 'queued' supaya bisa di-pickup ulang.
    Return list of recovered job_ids.
    """
    now = _now()
    threshold = max(1, int(max_processing_seconds))
    recovered: list[str] = []
    for job in list_jobs(1000):
        if job.get("status") != "processing":
            continue
        last_seen = float(job.get("updated_at") or job.get("started_at") or job.get("created_at") or now)
        if now - last_seen <= threshold:
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        update_job(
            job_id,
            status="queued",
            message="Job dipulihkan dari status processing yang stale.",
            stale_recovered_at=now,
            stale_recovery_count=int(job.get("stale_recovery_count", 0) or 0) + 1,
        )
        append_job_log(job_id, "WARNING", "Job dipulihkan dari status processing yang stale dan dikembalikan ke antrean.")
        recovered.append(job_id)
    return recovered


def acquire_job_slot(job_id: str) -> str | None:
    return _store.acquire_slot(job_id, Config.JOB_SLOT_TTL_SECONDS)


def refresh_job_slot(token: str) -> bool:
    return _store.refresh_slot(token, Config.JOB_SLOT_TTL_SECONDS)


def release_job_slot(token: str) -> None:
    _store.release_slot(token)


def job_store_backend() -> str:
    return _store.name
