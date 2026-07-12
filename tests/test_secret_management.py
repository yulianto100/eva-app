from pathlib import Path

import pytest

from config import Config, generate_secret_key, read_secret_file, validate_production_secrets


def test_validate_production_secrets_detects_default_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "SECRET_KEY", "change-me-in-production")
    monkeypatch.setattr(Config, "DEBUG", False)
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)

    warnings = validate_production_secrets()

    assert any("SECRET_KEY masih default" in warning for warning in warnings)


def test_validate_production_secrets_detects_short_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "SECRET_KEY", "short-secret")
    monkeypatch.setattr(Config, "DEBUG", False)
    monkeypatch.setattr(Config, "BASE_DIR", tmp_path)

    warnings = validate_production_secrets()

    assert any("SECRET_KEY terlalu pendek" in warning for warning in warnings)


def test_read_secret_file_prefers_env_var(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("TEST_SECRET", "from-env")
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert read_secret_file("TEST_SECRET") == "from-env"


def test_read_secret_file_reads_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert read_secret_file("TEST_SECRET") == "from-file"


def test_read_secret_file_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.delenv("TEST_SECRET_FILE", raising=False)

    assert read_secret_file("TEST_SECRET", default="fallback") == "fallback"


def test_generate_secret_key_is_long_and_random():
    first = generate_secret_key()
    second = generate_secret_key()

    assert len(first) >= 64
    assert first != second
    assert "\n" not in first
