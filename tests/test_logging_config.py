import io
import json
import logging

import logging_config


def test_json_formatter_outputs_valid_json():
    record = logging.LogRecord("eva.test", logging.INFO, __file__, 10, "Halo %s", ("EVA",), None)
    payload = json.loads(logging_config.JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "eva.test"
    assert payload["message"] == "Halo EVA"
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord("eva.test", logging.WARNING, __file__, 10, "Retry", (), None)
    record.job_id = "job-123"
    record.request_id = "req-456"

    payload = json.loads(logging_config.JsonFormatter().format(record))

    assert payload["job_id"] == "job-123"
    assert payload["request_id"] == "req-456"


def test_json_formatter_masks_sensitive_extra_fields():
    record = logging.LogRecord("eva.test", logging.INFO, __file__, 10, "Secret", (), None)
    record.api_key = "super-secret"

    payload = json.loads(logging_config.JsonFormatter().format(record))

    assert payload["api_key"] == "***"


def test_json_formatter_includes_exception_info():
    logger = logging.getLogger("test_json_formatter_includes_exception_info")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config.JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    logger.propagate = False

    try:
        raise ValueError("bad input")
    except ValueError:
        logger.exception("Gagal")

    payload = json.loads(stream.getvalue())
    assert payload["exception"]["type"] == "ValueError"
    assert "bad input" in payload["exception"]["message"]
    assert "Traceback" in payload["exception"]["traceback"]


def test_human_formatter_outputs_readable_text():
    record = logging.LogRecord("eva.test", logging.INFO, __file__, 10, "Halo EVA", (), None)
    output = logging_config.HumanFormatter().format(record)

    assert "[INFO" in output
    assert "eva.test: Halo EVA" in output


def test_setup_logging_respects_json_env(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    logging_config.setup_logging("INFO")

    logging.getLogger("eva.setup").info("Format JSON", extra={"job_id": "job-json"})

    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == "Format JSON"
    assert payload["job_id"] == "job-json"


def test_setup_logging_respects_text_env(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "text")
    logging_config.setup_logging("INFO")

    logging.getLogger("eva.setup").info("Format text")

    output = capsys.readouterr().out
    assert "eva.setup: Format text" in output
    assert not output.lstrip().startswith("{")


def test_setup_logging_injects_request_id(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    logging_config.setup_logging("INFO")
    logging_config.set_request_id("req-test")

    logging.getLogger("eva.request").info("Dengan request id")

    payload = json.loads(capsys.readouterr().out)
    assert payload["request_id"] == "req-test"
    logging_config.clear_request_id()
