import logging
import random
import sys
import time
from pathlib import Path

from celery import Celery
from celery.exceptions import Retry

from config import Config

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _celery_url() -> str:
    return Config.CELERY_BROKER_URL or Config.REDIS_URL or "memory://"


def _result_backend_url() -> str:
    return Config.CELERY_RESULT_BACKEND or Config.REDIS_URL or "cache+memory://"


celery_app = Celery(
    "eva_converter",
    broker=_celery_url(),
    backend=_result_backend_url(),
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Jakarta",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    result_expires=Config.JOB_STORE_TTL_SECONDS,
)


def _retry_delay_seconds(retries_done: int) -> int:
    base_delay = max(1, int(Config.JOB_RETRY_DELAY_SECONDS))
    delay = min(base_delay * (2 ** max(0, retries_done)), 300)
    jitter = random.uniform(0, max(1, delay * 0.2))
    return int(round(delay + jitter))


@celery_app.task(
    name="eva.run_conversion_job",
    bind=True,
    max_retries=Config.JOB_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_conversion_task(self, job_id: str) -> str:
    started_at = time.perf_counter()
    try:
        from app import run_conversion_job

        run_conversion_job(job_id, raise_on_error=True)
    except Exception as exc:
        duration = time.perf_counter() - started_at
        max_retries = int(Config.JOB_MAX_RETRIES)
        retry_count = int(getattr(self.request, "retries", 0)) + 1
        try:
            from alerts import alert_job_failed
            from converter import FatAreaGeometryError
            from jobs import get_job, mark_job_dead_letter, record_job_retry
            from metrics import record_job_failed, record_job_retried

            if not isinstance(exc, FatAreaGeometryError) and retry_count <= max_retries:
                delay = _retry_delay_seconds(int(getattr(self.request, "retries", 0)))
                record_job_retry(job_id, str(exc), retry_count, max_retries, delay)
                job = get_job(job_id) or {}
                record_job_retried(str(job.get("output_mode") or "unknown"))
                LOGGER.warning(
                    "Job %s gagal (attempt %s/%s), akan retry dalam %ss",
                    job_id,
                    retry_count,
                    max_retries,
                    delay,
                )
                raise self.retry(exc=exc, countdown=delay)

            LOGGER.exception("Celery worker gagal permanen untuk job %s", job_id)
            job = get_job(job_id) or {}
            mark_job_dead_letter(job_id, str(exc), duration)
            record_job_failed(
                job_id,
                str(job.get("output_mode") or "unknown"),
                str(job.get("network_type") or "unknown"),
            )
            alert_job_failed(job_id, str(exc), duration)
        except Retry:
            raise
        except Exception:
            LOGGER.exception("Gagal menyimpan status retry/error untuk job %s", job_id)
        raise
    return job_id


@celery_app.task(
    name="eva.run_astri_boq_submission",
    bind=True,
    max_retries=0,
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_astri_boq_submission_task(self, submission_id: str) -> str:
    """Run exactly once; ambiguous ASTRI mutations are never auto-retried."""
    from app import run_astri_boq_submission

    run_astri_boq_submission(submission_id)
    return submission_id
