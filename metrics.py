import json
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

from config import Config

LOGGER = logging.getLogger(__name__)
START_TIME = time.time()
MAX_DURATION_SAMPLES = 100


class MetricsStore:
    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, float] = defaultdict(float)
        self._active_jobs: set[str] = set()
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=MAX_DURATION_SAMPLES))

    def incr(self, key: str, amount: float = 1) -> None:
        with self._lock:
            self._counters[key] += amount

    def mark_active(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.add(job_id)

    def mark_inactive(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.discard(job_id)

    def record_duration(self, output_mode: str, duration_seconds: float) -> None:
        with self._lock:
            self._durations[output_mode].append(float(duration_seconds))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            avg_duration = {
                mode: round(sum(samples) / len(samples), 3)
                for mode, samples in self._durations.items()
                if samples
            }
            return {
                "backend": self.name,
                "uptime_seconds": round(time.time() - START_TIME, 3),
                "jobs_created": int(self._counters["jobs_created"]),
                "jobs_completed": int(self._counters["jobs_completed"]),
                "jobs_failed": int(self._counters["jobs_failed"]),
                "jobs_active": len(self._active_jobs),
                "avg_duration_seconds": avg_duration,
                "total_bytes_uploaded": int(self._counters["total_bytes_uploaded"]),
            }


class RedisMetricsStore(MetricsStore):
    name = "redis"

    def __init__(self, redis_url: str, prefix: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._redis.ping()
        self._prefix = prefix.strip(":") or "eva"

    def incr(self, key: str, amount: float = 1) -> None:
        if isinstance(amount, int) or float(amount).is_integer():
            self._redis.hincrby(self._counter_key(), key, int(amount))
        else:
            self._redis.hincrbyfloat(self._counter_key(), key, amount)

    def mark_active(self, job_id: str) -> None:
        self._redis.sadd(self._active_key(), job_id)

    def mark_inactive(self, job_id: str) -> None:
        self._redis.srem(self._active_key(), job_id)

    def record_duration(self, output_mode: str, duration_seconds: float) -> None:
        key = self._duration_key(output_mode)
        pipe = self._redis.pipeline()
        pipe.lpush(key, json.dumps(float(duration_seconds)))
        pipe.ltrim(key, 0, MAX_DURATION_SAMPLES - 1)
        pipe.execute()

    def snapshot(self) -> dict[str, Any]:
        counters = self._redis.hgetall(self._counter_key())
        modes = []
        for key in self._redis.scan_iter(match=f"{self._prefix}:metrics:duration:*", count=100):
            modes.append(key.rsplit(":", 1)[-1])

        avg_duration = {}
        for mode in sorted(modes):
            samples = []
            for raw_value in self._redis.lrange(self._duration_key(mode), 0, MAX_DURATION_SAMPLES - 1):
                try:
                    samples.append(float(json.loads(raw_value)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if samples:
                avg_duration[mode] = round(sum(samples) / len(samples), 3)

        return {
            "backend": self.name,
            "uptime_seconds": round(time.time() - START_TIME, 3),
            "jobs_created": int(float(counters.get("jobs_created", 0) or 0)),
            "jobs_completed": int(float(counters.get("jobs_completed", 0) or 0)),
            "jobs_failed": int(float(counters.get("jobs_failed", 0) or 0)),
            "jobs_active": int(self._redis.scard(self._active_key())),
            "avg_duration_seconds": avg_duration,
            "total_bytes_uploaded": int(float(counters.get("total_bytes_uploaded", 0) or 0)),
        }

    def _counter_key(self) -> str:
        return f"{self._prefix}:metrics:counters"

    def _active_key(self) -> str:
        return f"{self._prefix}:metrics:active_jobs"

    def _duration_key(self, output_mode: str) -> str:
        return f"{self._prefix}:metrics:duration:{output_mode}"


def _build_store() -> MetricsStore:
    if Config.REDIS_URL:
        try:
            store = RedisMetricsStore(Config.REDIS_URL, Config.JOB_STORE_KEY_PREFIX)
            LOGGER.info("Metrics store aktif: Redis (%s)", Config.REDIS_URL)
            return store
        except Exception as exc:
            LOGGER.warning("Redis metrics store tidak tersedia; fallback ke memory. Detail: %s", exc)
    return MetricsStore()


_store = _build_store()


def _prometheus():
    try:
        import prometheus_metrics

        return prometheus_metrics
    except Exception:
        return None


def _label(value: str | None, default: str = "unknown") -> str:
    return str(value or default).strip().lower() or default


def _sync_prometheus_active_jobs() -> None:
    prometheus = _prometheus()
    if not prometheus:
        return
    try:
        prometheus.jobs_active.set(_store.snapshot()["jobs_active"])
    except Exception:
        LOGGER.exception("Gagal sync Prometheus active jobs gauge")


def record_job_created(total_bytes: int = 0, output_mode: str = "unknown", network_type: str = "unknown") -> None:
    _store.incr("jobs_created")
    if total_bytes:
        _store.incr("total_bytes_uploaded", total_bytes)
    prometheus = _prometheus()
    if prometheus:
        prometheus.jobs_created_total.labels(_label(output_mode), _label(network_type)).inc()
        if total_bytes:
            prometheus.upload_bytes_total.inc(total_bytes)


def record_job_started(job_id: str) -> None:
    _store.mark_active(job_id)
    _sync_prometheus_active_jobs()


def record_job_completed(
    job_id: str,
    output_mode: str,
    duration_seconds: float,
    network_type: str = "unknown",
) -> None:
    _store.mark_inactive(job_id)
    _store.incr("jobs_completed")
    _store.record_duration(output_mode, duration_seconds)
    prometheus = _prometheus()
    if prometheus:
        prometheus.jobs_completed_total.labels(_label(output_mode), _label(network_type)).inc()
        prometheus.job_duration_seconds.labels(_label(output_mode), _label(network_type)).observe(duration_seconds)
    _sync_prometheus_active_jobs()


def record_job_failed(job_id: str, output_mode: str = "unknown", network_type: str = "unknown") -> None:
    _store.mark_inactive(job_id)
    _store.incr("jobs_failed")
    prometheus = _prometheus()
    if prometheus:
        prometheus.jobs_failed_total.labels(_label(output_mode), _label(network_type)).inc()
    _sync_prometheus_active_jobs()


def record_job_retried(output_mode: str = "unknown") -> None:
    prometheus = _prometheus()
    if prometheus:
        prometheus.jobs_retried_total.labels(_label(output_mode)).inc()


def record_upload_file(file_type: str) -> None:
    prometheus = _prometheus()
    if prometheus:
        prometheus.upload_files_total.labels(_label(file_type)).inc()


def metrics_snapshot() -> dict[str, Any]:
    return _store.snapshot()
