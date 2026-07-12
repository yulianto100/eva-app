import logging
import threading
import time
from pathlib import Path
from typing import Callable, Sequence


LOGGER = logging.getLogger(__name__)
_started = False
_start_lock = threading.Lock()
REQUIRED_OUTPUT_DIRECTORIES = ("boq", "hpdb")


def cleanup_directory(
    directory: str | Path,
    max_age_seconds: int,
    preserve_directories: Sequence[str] | None = None,
) -> int:
    now = time.time()
    removed = 0
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    preserved = {root / name for name in (preserve_directories or ())}

    for path in root.rglob("*"):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
                LOGGER.info("Cleanup menghapus file lama: %s", path)
        except OSError as exc:
            LOGGER.warning("Gagal menghapus %s: %s", path, exc)

    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        if path in preserved:
            continue
        try:
            path.rmdir()
        except OSError:
            pass

    return removed


def cleanup_old_files(upload_dir: str | Path, output_dir: str | Path, max_age_seconds: int) -> int:
    return cleanup_directory(upload_dir, max_age_seconds) + cleanup_directory(
        output_dir,
        max_age_seconds,
        preserve_directories=REQUIRED_OUTPUT_DIRECTORIES,
    )


def start_cleanup_scheduler(
    upload_dir: str | Path,
    output_dir: str | Path,
    max_age_seconds: int,
    interval_seconds: int,
    cleanup_jobs_callback: Callable[[], int] | None = None,
    stale_recovery_callback: Callable[[], list[str]] | None = None,
    stale_check_interval_seconds: int = 300,
    extra_directories: Sequence[tuple[str | Path, int]] | None = None,
) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        last_cleanup = 0.0
        last_stale_check = 0.0
        while True:
            try:
                now = time.time()
                if now - last_cleanup >= max(1, interval_seconds):
                    last_cleanup = now
                    removed_files = cleanup_old_files(upload_dir, output_dir, max_age_seconds)
                    for directory, directory_max_age_seconds in extra_directories or ():
                        removed_files += cleanup_directory(directory, directory_max_age_seconds)
                    removed_jobs = cleanup_jobs_callback() if cleanup_jobs_callback else 0
                    if removed_files or removed_jobs:
                        LOGGER.info("Cleanup selesai: %s file, %s job", removed_files, removed_jobs)
                if stale_recovery_callback and now - last_stale_check >= max(1, stale_check_interval_seconds):
                    last_stale_check = now
                    recovered_jobs = stale_recovery_callback()
                    if recovered_jobs:
                        LOGGER.warning("Recovered stale jobs: %s", ", ".join(recovered_jobs))
                        try:
                            from alerts import alert_server_unhealthy

                            alert_server_unhealthy(f"Recovered stale jobs: {', '.join(recovered_jobs)}")
                        except Exception:
                            LOGGER.exception("Gagal mengirim alert stale job recovery")
            except Exception:
                LOGGER.exception("Cleanup gagal")
            sleep_seconds = min(
                max(1, interval_seconds),
                max(1, stale_check_interval_seconds) if stale_recovery_callback else max(1, interval_seconds),
            )
            time.sleep(sleep_seconds)

    thread = threading.Thread(target=loop, name="cleanup-scheduler", daemon=True)
    thread.start()

