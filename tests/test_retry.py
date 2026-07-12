from pathlib import Path

import jobs as jobs_module
from jobs import create_job, get_job, mark_job_dead_letter, record_job_retry, recover_stale_jobs, update_job


def _create_retry_job(job_id: str, tmp_path):
    output_path = tmp_path / f"{job_id}.zip"
    return create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        output_mode="boq",
        network_type="AE",
    )


def test_retry_count_increment(tmp_path):
    job_id = "retry-count-job"
    _create_retry_job(job_id, tmp_path)

    record_job_retry(job_id, "temporary failure", retry_count=1, max_retries=2, delay_seconds=30)
    job = get_job(job_id)

    assert job["status"] == "retrying"
    assert job["retry_count"] == 1
    assert job["max_retries"] == 2
    assert job["retry_errors"][0]["message"] == "temporary failure"


def test_stale_job_recovery(monkeypatch, tmp_path):
    job_id = "stale-recovery-job"
    monkeypatch.setattr(jobs_module, "_now", lambda: 1.0)
    _create_retry_job(job_id, tmp_path)
    update_job(job_id, status="processing", started_at=1, message="stuck")

    monkeypatch.setattr(jobs_module, "_now", lambda: 10.0)
    recovered = recover_stale_jobs(max_processing_seconds=1)
    job = get_job(job_id)

    assert job_id in recovered
    assert job["status"] == "queued"
    assert job["stale_recovery_count"] == 1


def test_dead_letter_marking_after_max_retries(tmp_path):
    job_id = "dead-letter-job"
    _create_retry_job(job_id, tmp_path)

    record_job_retry(job_id, "first failure", retry_count=1, max_retries=2)
    record_job_retry(job_id, "second failure", retry_count=2, max_retries=2)
    mark_job_dead_letter(job_id, "final failure", duration_seconds=12.5)
    job = get_job(job_id)

    assert job["status"] == "failed"
    assert job["dead_letter"] is True
    assert job["duration_seconds"] == 12.5
    assert job["retry_errors"][-1]["message"] == "final failure"


def test_status_transition_retry_then_done(tmp_path):
    job_id = "retry-done-job"
    _create_retry_job(job_id, tmp_path)

    update_job(job_id, status="processing", progress=10)
    record_job_retry(job_id, "worker restart", retry_count=1, max_retries=2)
    update_job(job_id, status="processing", progress=20)
    update_job(job_id, status="done", progress=100, message="Selesai.")
    job = get_job(job_id)

    assert job["status"] == "done"
    assert job["retry_count"] == 1
    assert job["dead_letter"] is False


def test_status_transition_retry_then_failed(tmp_path):
    job_id = "retry-failed-job"
    _create_retry_job(job_id, tmp_path)

    update_job(job_id, status="processing", progress=10)
    record_job_retry(job_id, "failure 1", retry_count=1, max_retries=2)
    record_job_retry(job_id, "failure 2", retry_count=2, max_retries=2)
    mark_job_dead_letter(job_id, "failure 3")
    job = get_job(job_id)

    assert job["status"] == "failed"
    assert job["dead_letter"] is True
    assert len(job["retry_errors"]) == 3
