"""Prometheus metrics untuk EVA Converter."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest

__all__ = [
    "CONTENT_TYPE_LATEST",
    "app_info",
    "generate_latest",
    "http_request_duration_seconds",
    "http_requests_total",
    "job_duration_seconds",
    "jobs_active",
    "jobs_completed_total",
    "jobs_created_total",
    "jobs_failed_total",
    "jobs_retried_total",
    "redis_connected",
    "set_app_info",
    "update_system_gauges",
    "upload_bytes_total",
    "upload_files_total",
    "worker_available",
]

app_info = Info("eva_converter", "EVA Converter application info")

jobs_created_total = Counter(
    "eva_jobs_created_total",
    "Total number of conversion jobs created",
    ["output_mode", "network_type"],
)
jobs_completed_total = Counter(
    "eva_jobs_completed_total",
    "Total number of conversion jobs completed successfully",
    ["output_mode", "network_type"],
)
jobs_failed_total = Counter(
    "eva_jobs_failed_total",
    "Total number of conversion jobs that failed",
    ["output_mode", "network_type"],
)
jobs_retried_total = Counter(
    "eva_jobs_retried_total",
    "Total number of job retry attempts",
    ["output_mode"],
)
jobs_active = Gauge(
    "eva_jobs_active",
    "Number of currently active/processing jobs",
)

job_duration_seconds = Histogram(
    "eva_job_duration_seconds",
    "Time spent processing conversion jobs",
    ["output_mode", "network_type"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 900, 1800],
)

upload_bytes_total = Counter(
    "eva_upload_bytes_total",
    "Total bytes uploaded",
)
upload_files_total = Counter(
    "eva_upload_files_total",
    "Total number of files uploaded",
    ["file_type"],
)

http_requests_total = Counter(
    "eva_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)
http_request_duration_seconds = Histogram(
    "eva_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

redis_connected = Gauge(
    "eva_redis_connected",
    "Whether Redis connection is healthy (1=connected, 0=disconnected)",
)
worker_available = Gauge(
    "eva_worker_available",
    "Whether Celery worker is available (1=yes, 0=no)",
)


def normalize_label(value: str | None, default: str = "unknown") -> str:
    label = str(value or "").strip().lower()
    return label or default


def set_app_info(version: str) -> None:
    app_info.info({"version": version})


def update_system_gauges(redis_ok: bool, worker_ok: bool) -> None:
    redis_connected.set(1 if redis_ok else 0)
    worker_available.set(1 if worker_ok else 0)
