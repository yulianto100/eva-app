import logging
import threading

from config import Config
from jobs import update_job

LOGGER = logging.getLogger(__name__)


def _queue_enabled() -> bool:
    mode = Config.CONVERSION_QUEUE_ENABLED
    if mode in {"1", "true", "yes", "on"}:
        return True
    if mode in {"0", "false", "no", "off"}:
        return False
    return bool(Config.REDIS_URL and Config.CELERY_BROKER_URL)


def enqueue_conversion_job(job_id: str, runner) -> str:
    if _queue_enabled():
        try:
            from tasks import run_conversion_task

            task = run_conversion_task.delay(job_id)
            update_job(
                job_id,
                task_id=task.id,
                queue_backend="celery",
                message="Job masuk antrean worker.",
            )
            return "celery"
        except Exception as exc:
            if not Config.ALLOW_THREAD_QUEUE_FALLBACK:
                LOGGER.error("Celery enqueue gagal dan fallback thread dimatikan. Detail: %s", exc)
                raise RuntimeError("Queue worker belum tersedia dan fallback thread dimatikan.") from exc
            LOGGER.warning("Celery enqueue gagal; fallback ke background thread. Detail: %s", exc)
            update_job(
                job_id,
                queue_backend="thread-fallback",
                message="Queue worker belum tersedia, memakai background thread.",
            )

    worker = threading.Thread(target=runner, args=(job_id,), daemon=True)
    worker.start()
    update_job(job_id, queue_backend="thread")
    return "thread"

