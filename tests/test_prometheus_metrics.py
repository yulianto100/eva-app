from prometheus_client.parser import text_string_to_metric_families

import app as web_app
import prometheus_metrics
from metrics import record_job_completed, record_job_created, record_job_failed


def test_prometheus_endpoint_returns_text_format():
    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as client:
        response = client.get("/metrics/prometheus")

    assert response.status_code == 200
    assert "text/plain" in response.headers["Content-Type"]
    assert b"eva_jobs_created_total" in response.data


def test_prometheus_text_can_be_parsed():
    text = prometheus_metrics.generate_latest().decode("utf-8")
    families = list(text_string_to_metric_families(text))

    assert any(family.name == "eva_jobs_created" for family in families)


def test_prometheus_counters_increment_with_labels():
    labels = ("pytest_mode_created", "pytest_net_created")
    before = prometheus_metrics.jobs_created_total.labels(*labels)._value.get()

    record_job_created(123, labels[0], labels[1])

    after = prometheus_metrics.jobs_created_total.labels(*labels)._value.get()
    assert after == before + 1


def test_prometheus_histogram_observes_duration():
    labels = ("pytest_mode_duration", "pytest_net_duration")
    before = prometheus_metrics.job_duration_seconds.labels(*labels)._sum.get()

    record_job_completed("prom-job-done", labels[0], 2.5, labels[1])

    after = prometheus_metrics.job_duration_seconds.labels(*labels)._sum.get()
    assert after >= before + 2.5


def test_prometheus_failed_counter_labels():
    labels = ("pytest_mode_failed", "pytest_net_failed")
    before = prometheus_metrics.jobs_failed_total.labels(*labels)._value.get()

    record_job_failed("prom-job-failed", labels[0], labels[1])

    after = prometheus_metrics.jobs_failed_total.labels(*labels)._value.get()
    assert after == before + 1


def test_existing_metrics_json_endpoint_still_works():
    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as client:
        response = client.get("/metrics")

    payload = response.get_json()
    assert response.status_code == 200
    assert "jobs_created" in payload
