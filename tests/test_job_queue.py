import pytest

import job_queue
from config import Config


def test_enqueue_conversion_job_raises_when_fallback_disabled(monkeypatch):
    updates = []

    monkeypatch.setattr(Config, "CONVERSION_QUEUE_ENABLED", "true")
    monkeypatch.setattr(Config, "REDIS_URL", "redis://example")
    monkeypatch.setattr(Config, "CELERY_BROKER_URL", "redis://example")
    monkeypatch.setattr(Config, "ALLOW_THREAD_QUEUE_FALLBACK", False)
    monkeypatch.setattr(job_queue, "update_job", lambda job_id, **changes: updates.append((job_id, changes)))

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tasks":
            raise RuntimeError("celery unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match="fallback thread dimatikan"):
        job_queue.enqueue_conversion_job("job-1", lambda _job_id: None)

    assert updates == []


def test_enqueue_conversion_job_falls_back_to_thread_when_allowed(monkeypatch):
    updates = []
    ran = []

    monkeypatch.setattr(Config, "CONVERSION_QUEUE_ENABLED", "true")
    monkeypatch.setattr(Config, "REDIS_URL", "redis://example")
    monkeypatch.setattr(Config, "CELERY_BROKER_URL", "redis://example")
    monkeypatch.setattr(Config, "ALLOW_THREAD_QUEUE_FALLBACK", True)
    monkeypatch.setattr(job_queue, "update_job", lambda job_id, **changes: updates.append((job_id, changes)))

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(job_queue.threading, "Thread", ImmediateThread)

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tasks":
            raise RuntimeError("celery unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    backend = job_queue.enqueue_conversion_job("job-2", lambda job_id: ran.append(job_id))

    assert backend == "thread"
    assert ran == ["job-2"]
    assert updates[0][1]["queue_backend"] == "thread-fallback"
    assert updates[-1][1]["queue_backend"] == "thread"
