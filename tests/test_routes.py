import io
import sys
import types
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

import app as web_app
import coreman_db
import fdt_coding_db
from config import Config
from jobs import create_job, get_job, update_job


MINIMAL_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Test Point</name>
      <Point><coordinates>106.8,-6.2,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""


def test_hidden_kml_folder_selector_does_not_filter_standardized_output():
    with web_app.app.test_request_context(
        "/upload",
        method="POST",
        data={"kml_folder_filter_enabled": "1", "kml_folders": ["FAT", "CABLE"]},
    ):
        assert web_app.parse_selected_kml_folders("Cluster") is None


def _make_minimal_fdt_coding_workbook(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "OLT Taman Narogong"
    worksheet.cell(12, 3, "Site Name")
    worksheet.cell(12, 7, "Inisial Name Cluster")
    worksheet.cell(12, 8, "No FDT ID")
    worksheet.cell(12, 9, "Coding FDT")
    worksheet.cell(12, 10, "Index FDT")
    worksheet.cell(13, 3, "MUTIARA GADING TIMUR BLOK DAMAR RW 24 RT. 7")
    worksheet.cell(13, 7, "MGTDMR")
    worksheet.cell(13, 8, 149)
    worksheet.cell(13, 9, "MGTDMR.149")
    worksheet.cell(13, 10, "BKS.100.0501.MGTDMR.149")
    workbook.save(path)


@pytest.fixture
def client(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "outputs"
    boq_dir = output_dir / "boq"
    hpdb_dir = output_dir / "hpdb"
    for directory in (upload_dir, output_dir, boq_dir, hpdb_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(Config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(Config, "BOQ_OUTPUT_DIR", boq_dir)
    monkeypatch.setattr(Config, "HPDB_OUTPUT_DIR", hpdb_dir)
    monkeypatch.setattr(Config, "MAX_FILE_BYTES", 1024 * 1024)
    monkeypatch.setattr(Config, "MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(Config, "REDIS_URL", "")
    monkeypatch.setattr(web_app, "enqueue_conversion_job", lambda job_id, _runner: "test")

    web_app.app.config["TESTING"] = True
    with web_app.app.test_client() as test_client:
        yield test_client


def test_index_returns_upload_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"Upload" in response.data


def test_health_returns_ok(client, monkeypatch, tmp_path):
    dxf_dir = tmp_path / "dxf_templates"
    template_dir = tmp_path / "templates"
    config_dir = tmp_path / "config"
    dxf_dir.mkdir()
    template_dir.mkdir()
    config_dir.mkdir()
    dxf_template = dxf_dir / "Template_AE.dxf"
    boq_template = template_dir / "BoQ APD Cluster_AE.xlsx"
    hpdb_template = template_dir / "TEMPLATE HPDB_AE.xlsx"
    hpdb_defaults = config_dir / "hpdb_defaults_AE.json"
    fdt_workbook = config_dir / "fdt_coding.xlsx"
    dxf_template.write_text("0\nSECTION\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    boq_template.write_bytes(b"xlsx")
    hpdb_template.write_bytes(b"xlsx")
    hpdb_defaults.write_text("{}", encoding="utf-8")
    fdt_workbook.write_bytes(b"fdt workbook")

    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32)
    monkeypatch.setattr(Config, "DEBUG", False)
    monkeypatch.setattr(Config, "DXF_TEMPLATE_PATH", dxf_template)
    monkeypatch.setattr(Config, "DXF_TEMPLATE_PATHS", {"AE": dxf_template})
    monkeypatch.setattr(Config, "DXF_FEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "DXF_SUBFEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "BOQ_TEMPLATE_PATH", boq_template)
    monkeypatch.setattr(Config, "BOQ_TEMPLATE_PATHS", {"AE": boq_template})
    monkeypatch.setattr(Config, "BOQ_FEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "BOQ_SUBFEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "HPDB_TEMPLATE_PATH", hpdb_template)
    monkeypatch.setattr(Config, "HPDB_TEMPLATE_PATHS", {"AE": hpdb_template})
    monkeypatch.setattr(Config, "HPDB_DEFAULTS_PATH", hpdb_defaults)
    monkeypatch.setattr(Config, "HPDB_DEFAULTS_PATHS", {"AE": hpdb_defaults})
    monkeypatch.setattr(Config, "FDT_CODING_SPREADSHEET_URL", "")
    monkeypatch.setattr(Config, "FDT_CODING_SPREADSHEET_PATH", fdt_workbook)
    monkeypatch.setattr(web_app, "redis_connection_ok", lambda: True)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["upload_dir_writable"] is True
    assert payload["output_dir_writable"] is True
    assert payload["readyz_checks"]["hpdb_templates_available"] is True
    assert payload["readyz_checks"]["fdt_coding_source_ready"] is True

def test_healthz_returns_ok_even_when_redis_is_down(client, monkeypatch):
    monkeypatch.setattr(web_app, "redis_connection_ok", lambda: False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}

def test_readyz_returns_not_ready_with_failed_checks(client, monkeypatch):
    monkeypatch.setattr(Config, "SECRET_KEY", "change-me-in-production")
    monkeypatch.setattr(Config, "DEBUG", False)
    monkeypatch.setattr(web_app, "redis_connection_ok", lambda: False)

    response = client.get("/readyz")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "not ready"
    assert payload["checks"]["secret_key_configured"] is False
    assert payload["checks"]["redis_connected"] is False

def test_readyz_returns_ready_when_dependencies_are_available(client, monkeypatch, tmp_path):
    dxf_dir = tmp_path / "dxf_templates"
    template_dir = tmp_path / "templates"
    config_dir = tmp_path / "config"
    dxf_dir.mkdir()
    template_dir.mkdir()
    config_dir.mkdir()
    dxf_template = dxf_dir / "Template_AE.dxf"
    boq_template = template_dir / "BoQ APD Cluster_AE.xlsx"
    hpdb_template = template_dir / "TEMPLATE HPDB_AE.xlsx"
    hpdb_defaults = config_dir / "hpdb_defaults_AE.json"
    fdt_workbook = config_dir / "fdt_coding.xlsx"
    dxf_template.write_text("0\nSECTION\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    boq_template.write_bytes(b"xlsx")
    hpdb_template.write_bytes(b"xlsx")
    hpdb_defaults.write_text("{}", encoding="utf-8")
    fdt_workbook.write_bytes(b"fdt workbook")

    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32)
    monkeypatch.setattr(Config, "DEBUG", False)
    monkeypatch.setattr(Config, "DXF_TEMPLATE_PATH", dxf_template)
    monkeypatch.setattr(Config, "DXF_TEMPLATE_PATHS", {"AE": dxf_template})
    monkeypatch.setattr(Config, "DXF_FEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "DXF_SUBFEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "BOQ_TEMPLATE_PATH", boq_template)
    monkeypatch.setattr(Config, "BOQ_TEMPLATE_PATHS", {"AE": boq_template})
    monkeypatch.setattr(Config, "BOQ_FEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "BOQ_SUBFEEDER_TEMPLATE_PATHS", {})
    monkeypatch.setattr(Config, "HPDB_TEMPLATE_PATH", hpdb_template)
    monkeypatch.setattr(Config, "HPDB_TEMPLATE_PATHS", {"AE": hpdb_template})
    monkeypatch.setattr(Config, "HPDB_DEFAULTS_PATH", hpdb_defaults)
    monkeypatch.setattr(Config, "HPDB_DEFAULTS_PATHS", {"AE": hpdb_defaults})
    monkeypatch.setattr(Config, "FDT_CODING_SPREADSHEET_URL", "")
    monkeypatch.setattr(Config, "FDT_CODING_SPREADSHEET_PATH", fdt_workbook)
    monkeypatch.setattr(web_app, "redis_connection_ok", lambda: True)

    response = client.get("/readyz")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert all(payload["checks"].values())


def test_convert_accepts_minimal_kml(client):
    response = client.post(
        "/convert",
        data={
            "files": (io.BytesIO(MINIMAL_KML), "sample.kml"),
            "output_mode": "kmz",
            "network_type": "AE",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["job_id"]


def test_convert_rejects_invalid_file_extension(client):
    response = client.post(
        "/convert",
        data={
            "files": (io.BytesIO(b"not kml"), "sample.txt"),
            "output_mode": "kmz",
            "network_type": "AE",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "Format file tidak didukung" in response.get_json()["error"]


def test_convert_rejects_file_above_limit(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_FILE_BYTES", 2)
    monkeypatch.setattr(Config, "MAX_UPLOAD_MB", 0)

    response = client.post(
        "/convert",
        data={
            "files": (io.BytesIO(MINIMAL_KML), "large.kml"),
            "output_mode": "kmz",
            "network_type": "AE",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert "maksimal" in response.get_json()["error"]


def test_status_returns_job_payload(client):
    response = client.post(
        "/convert",
        data={
            "files": (io.BytesIO(MINIMAL_KML), "status.kml"),
            "output_mode": "kmz",
            "network_type": "AE",
        },
        content_type="multipart/form-data",
    )
    job_id = response.get_json()["job_id"]

    status_response = client.get(f"/status/{job_id}")
    payload = status_response.get_json()

    assert status_response.status_code == 200
    assert {"status", "progress", "message"}.issubset(payload)


def test_status_missing_job_returns_404(client):
    response = client.get("/status/not-a-real-job")

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_download_returns_completed_output(client, tmp_path):
    job_id = "route-download-job"
    output_path = Config.OUTPUT_DIR / "result.kmz"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"kmz-content")

    create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        download_name="result.kmz",
        output_mode="kmz",
        download_mimetype="application/vnd.google-earth.kmz",
    )
    update_job(job_id, status="done", progress=100, message="Selesai.")

    response = client.get(f"/download/{job_id}")

    assert response.status_code == 200
    assert response.data == b"kmz-content"

def test_download_rejects_output_outside_managed_directories(client, tmp_path):
    job_id = "route-download-unsafe"
    output_path = tmp_path / "outside" / "unsafe.kmz"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"unsafe")

    create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        download_name="unsafe.kmz",
        output_mode="kmz",
        download_mimetype="application/vnd.google-earth.kmz",
    )
    update_job(job_id, status="done", progress=100, message="Selesai.")

    response = client.get(f"/download/{job_id}")

    assert response.status_code == 403
    assert "tidak diizinkan" in response.get_json()["error"]

def test_cancel_processing_job(client, tmp_path, monkeypatch):
    revoked_tasks = []
    monkeypatch.setattr(web_app, "revoke_celery_task", lambda task_id: revoked_tasks.append(task_id) or True)
    job_id = "route-cancel-processing"
    output_path = tmp_path / "outputs" / "cancel.kmz"
    create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        download_name="cancel.kmz",
        output_mode="kmz",
        download_mimetype="application/vnd.google-earth.kmz",
    )
    update_job(job_id, status="processing", progress=42, message="Running.", task_id="task-123")

    response = client.post(f"/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.get_json()["status"] == "cancelled"
    assert get_job(job_id)["status"] == "cancelled"
    assert get_job(job_id)["message"] == "Cancelled by user."
    assert revoked_tasks == ["task-123"]

def test_cancel_requires_admin_token_when_enabled(client, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "REQUIRE_ADMIN_API_TOKEN", True)
    monkeypatch.setattr(Config, "ADMIN_API_TOKEN", "secret-token")
    job_id = "route-cancel-protected"
    output_path = tmp_path / "outputs" / "cancel-protected.kmz"
    create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        download_name="cancel-protected.kmz",
        output_mode="kmz",
        download_mimetype="application/vnd.google-earth.kmz",
    )
    update_job(job_id, status="processing", progress=42, message="Running.")

    blocked = client.post(f"/jobs/{job_id}/cancel")
    allowed = client.post(f"/jobs/{job_id}/cancel", headers={"X-Admin-Token": "secret-token"})

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert get_job(job_id)["status"] == "cancelled"

def test_cancel_completed_job_returns_409(client, tmp_path):
    job_id = "route-cancel-done"
    output_path = tmp_path / "outputs" / "done.kmz"
    create_job(
        job_id,
        [Path("input.kml")],
        output_path,
        [],
        download_name="done.kmz",
        output_mode="kmz",
        download_mimetype="application/vnd.google-earth.kmz",
    )
    update_job(job_id, status="done", progress=100, message="Selesai.")

    response = client.post(f"/jobs/{job_id}/cancel")

    assert response.status_code == 409
    assert get_job(job_id)["status"] == "done"

def test_cancel_missing_job_returns_404(client):
    response = client.post("/jobs/not-a-real-job/cancel")

    assert response.status_code == 404
    assert "error" in response.get_json()

def test_fdt_coding_db_adds_source_spreadsheet_id_column(client, tmp_path):
    db_path = tmp_path / "fdt_coding.db"

    with fdt_coding_db.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fdt_coding_records)").fetchall()}

    assert "source_spreadsheet_id" in columns


def test_fdt_coding_import_workbook_persists_source_spreadsheet_id(client, tmp_path):
    workbook_path = tmp_path / "coding.xlsx"
    db_path = tmp_path / "fdt_coding.db"

    _make_minimal_fdt_coding_workbook(workbook_path)
    imported = fdt_coding_db.import_workbook(
        db_path,
        workbook_path,
        replace=True,
        source_spreadsheet_id="spreadsheet-123",
    )

    assert imported == 1
    records = fdt_coding_db.list_records(db_path)
    assert records[0]["source_spreadsheet_id"] == "spreadsheet-123"


def test_fdt_coding_update_record_preserves_source_spreadsheet_id(client, tmp_path):
    db_path = tmp_path / "fdt_coding.db"
    record = fdt_coding_db.create_record(
        db_path,
        {
            "olt_initial": "OLT TEST",
            "hub_id": "BKS.100.0001",
            "olt_name_initial": "OLT TEST",
            "site_name": "SITE TEST",
            "fdt_order": 1,
            "fdt_id": "001",
            "fdt_code": "SITE.001",
            "index_code": "IDX-001",
            "cluster_initial": "SITE",
            "source_spreadsheet_id": "spreadsheet-abc",
            "sheet_name": "OLT TEST",
            "source_row": 12,
        },
    )

    updated = fdt_coding_db.update_record(
        db_path,
        record["id"],
        {
            "olt_initial": record["olt_initial"],
            "hub_id": record["hub_id"],
            "olt_name_initial": record["olt_name_initial"],
            "site_name": record["site_name"],
            "fdt_order": record["fdt_order"],
            "fdt_id": record["fdt_id"],
            "fdt_code": record["cluster_initial"],
            "cluster_initial": record["cluster_initial"],
            "sheet_name": record["sheet_name"],
            "source_row": record["source_row"],
            "source_spreadsheet_id": record["source_spreadsheet_id"],
        },
    )

    assert updated["source_spreadsheet_id"] == "spreadsheet-abc"


def test_init_sentry_only_when_dsn_is_set(monkeypatch):
    calls = []

    def fake_init(**kwargs):
        calls.append(kwargs)

    class FakeFlaskIntegration:
        pass

    sentry_module = types.ModuleType("sentry_sdk")
    sentry_module.init = fake_init
    integrations_module = types.ModuleType("sentry_sdk.integrations")
    integrations_module.__path__ = []
    flask_module = types.ModuleType("sentry_sdk.integrations.flask")
    flask_module.FlaskIntegration = FakeFlaskIntegration
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", integrations_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.flask", flask_module)
    monkeypatch.setattr(Config, "SENTRY_DSN", "")

    assert web_app.init_sentry() is False
    assert calls == []

    monkeypatch.setattr(Config, "SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr(Config, "RELEASE_VERSION", "test-release")
    monkeypatch.setattr(Config, "ENVIRONMENT", "test")

    assert web_app.init_sentry() is True
    assert calls[0]["dsn"] == "https://public@example.invalid/1"
    assert calls[0]["release"] == "test-release"
    assert calls[0]["environment"] == "test"
    assert calls[0]["traces_sample_rate"] == 0.1
    assert isinstance(calls[0]["integrations"][0], FakeFlaskIntegration)

def _write_minimal_kmz(path: Path, name: str = "Test Point") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark><name>{name}</name><Point><coordinates>106.8,-6.2,0</coordinates></Point></Placemark>
  </Document>
</kml>
'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml)


def test_standardization_runs_for_dxf_only_output_mode(client, monkeypatch, tmp_path):
    job_id = "mandatory-standardize-dxf"
    raw_path = tmp_path / "uploads" / "raw.kmz"
    output_path = tmp_path / "outputs" / "result.dxf"
    _write_minimal_kmz(raw_path, "Raw")
    calls = []
    downstream_inputs = []

    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: False)
    monkeypatch.setattr(web_app, "record_job_completed", lambda *args, **kwargs: None)

    def fake_standardize(source_path, _coding_path, standard_path, network_type="AE", scope_type="Cluster", **_kwargs):
        calls.append(Path(source_path))
        _write_minimal_kmz(Path(standard_path), "Standardized")
        return {"output_path": str(standard_path), "warnings": []}

    def fake_convert_osm_to_dxf(input_path, output_path, **_kwargs):
        downstream_inputs.extend(Path(path) for path in input_path)
        Path(output_path).write_text("0\nSECTION\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
        return {
            "stats": {},
            "processed_ways": 0,
            "preview": {"type": "FeatureCollection", "features": []},
        }

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize)
    monkeypatch.setattr(web_app, "convert_osm_to_dxf", fake_convert_osm_to_dxf)

    create_job(job_id, [raw_path], output_path, [], output_mode="dxf", dxf_output_path=output_path)

    web_app.run_conversion_job(job_id)

    standardized_path = tmp_path / "uploads" / f"{job_id}_standardized_1.kmz"
    assert calls == [raw_path]
    assert downstream_inputs == [standardized_path]
    job = get_job(job_id)
    assert job["status"] == "done"
    assert job["input_paths"] == [str(standardized_path)]


def test_standardization_failure_aborts_job_when_kmz_not_in_output_mode(client, monkeypatch, tmp_path):
    job_id = "mandatory-standardize-failure"
    raw_path = tmp_path / "uploads" / "raw.kmz"
    output_path = tmp_path / "outputs" / "result.xlsx"
    _write_minimal_kmz(raw_path, "Raw")
    downstream_calls = []

    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: False)
    monkeypatch.setattr(web_app, "record_job_failed", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "alert_job_failed", lambda *args, **kwargs: None)

    def fake_standardize(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    def fake_generate_boq_excel(*_args, **_kwargs):
        downstream_calls.append("boq")
        return {"summary": {}, "preview_rows": [], "nro_preview": {"columns": [], "rows": []}, "warnings": []}

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize)
    monkeypatch.setattr(web_app, "generate_boq_excel", fake_generate_boq_excel)

    create_job(
        job_id,
        [raw_path],
        output_path,
        [],
        output_mode="boq",
        boq_output_path=output_path,
        download_mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    web_app.run_conversion_job(job_id)

    job = get_job(job_id)
    assert job["status"] == "error"
    assert "KMZ standardization gagal" in job["error"]
    assert "simulated failure" in job["error"]
    assert downstream_calls == []


def test_job_state_input_paths_updated_even_when_standardizer_made_zero_changes(client, monkeypatch, tmp_path):
    job_id = "mandatory-standardize-zero-change"
    raw_path = tmp_path / "uploads" / "already-standard.kmz"
    output_path = tmp_path / "outputs" / "result.dxf"
    _write_minimal_kmz(raw_path, "Already Standard")

    class Logger:
        def info(self, _message):
            pass

        def success(self, _message):
            pass

        def warn(self, _message):
            pass

    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: False)

    def fake_standardize(_source_path, _coding_path, standard_path, network_type="AE", scope_type="Cluster", **_kwargs):
        _write_minimal_kmz(Path(standard_path), "Already Standard")
        return {
            "output_path": str(standard_path),
            "warnings": [],
            "fdt_renamed": 0,
            "name_standardized": 0,
            "folder_renamed": 0,
            "folder_unwrapped": 0,
            "placemark_organized": 0,
            "distribution_cable_standardized": 0,
            "style_standardized": 0,
        }

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize)

    create_job(job_id, [raw_path], output_path, [], output_mode="dxf")

    paths, _outputs, summary = web_app.standardize_kml_inputs_for_job(
        job_id,
        [raw_path],
        "dxf",
        "AE",
        Logger(),
    )

    standardized_path = tmp_path / "uploads" / f"{job_id}_standardized_1.kmz"
    job = get_job(job_id)
    assert paths == [standardized_path]
    assert summary["name_standardized"] == 0
    assert job["input_paths"] == [str(standardized_path)]
    assert job["input_path"] == str(standardized_path)


def test_hpdb_auto_geocode_skipped_when_osm_not_uploaded(client, monkeypatch, tmp_path):
    job_id = "hpdb-no-osm-geocode"
    raw_path = tmp_path / "uploads" / "raw.kmz"
    output_path = tmp_path / "outputs" / "hpdb.xlsx"
    _write_minimal_kmz(raw_path, "Raw")
    auto_geocode_values = []

    monkeypatch.setattr(Config, "HPDB_AUTO_GEOCODE_ENABLED", True)
    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: False)
    monkeypatch.setattr(web_app, "record_job_completed", lambda *args, **kwargs: None)

    def fake_standardize(_source_path, _coding_path, standard_path, **_kwargs):
        _write_minimal_kmz(Path(standard_path), "Standardized")
        return {"output_path": str(standard_path), "warnings": []}

    def fake_generate_hpdb_excel(*_args, **kwargs):
        auto_geocode_values.append(kwargs.get("auto_geocode"))
        Path(kwargs["output_path"]).write_text("hpdb", encoding="utf-8")
        return {"summary": {}, "preview": {"cluster": [], "fat": [], "homepass": []}, "warnings": [], "issues": []}

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize)
    monkeypatch.setattr(web_app, "generate_hpdb_excel", fake_generate_hpdb_excel)

    create_job(job_id, [raw_path], output_path, [], output_mode="hpdb", hpdb_output_path=output_path)

    web_app.run_conversion_job(job_id)

    assert auto_geocode_values == [False]
    assert get_job(job_id)["status"] == "done"


def test_hpdb_auto_geocode_kept_when_osm_uploaded(client, monkeypatch, tmp_path):
    job_id = "hpdb-with-osm-geocode"
    kmz_path = tmp_path / "uploads" / "raw.kmz"
    osm_path = tmp_path / "uploads" / "roads.osm"
    output_path = tmp_path / "outputs" / "hpdb.xlsx"
    _write_minimal_kmz(kmz_path, "Raw")
    osm_path.write_text("<osm version='0.6'></osm>", encoding="utf-8")
    auto_geocode_values = []

    monkeypatch.setattr(Config, "HPDB_AUTO_GEOCODE_ENABLED", True)
    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: False)
    monkeypatch.setattr(web_app, "record_job_completed", lambda *args, **kwargs: None)

    def fake_standardize(_source_path, _coding_path, standard_path, **_kwargs):
        _write_minimal_kmz(Path(standard_path), "Standardized")
        return {"output_path": str(standard_path), "warnings": []}

    def fake_generate_hpdb_excel(*_args, **kwargs):
        auto_geocode_values.append(kwargs.get("auto_geocode"))
        Path(kwargs["output_path"]).write_text("hpdb", encoding="utf-8")
        return {"summary": {}, "preview": {"cluster": [], "fat": [], "homepass": []}, "warnings": [], "issues": []}

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize)
    monkeypatch.setattr(web_app, "generate_hpdb_excel", fake_generate_hpdb_excel)

    create_job(job_id, [kmz_path, osm_path], output_path, [], output_mode="hpdb", hpdb_output_path=output_path)

    web_app.run_conversion_job(job_id)

    assert auto_geocode_values == [True]
    assert get_job(job_id)["status"] == "done"


def test_coreman_page_and_summary_api(client, monkeypatch, tmp_path):
    coreman_db_path = tmp_path / "coreman.db"
    monkeypatch.setattr(Config, "COREMAN_DB_PATH", coreman_db_path)
    coreman_db.create_opm_batch(coreman_db_path, {"cluster_name": "PERMATA CIKARANG SELATAN", "status": "waiting_mancore", "detected_fdt_labels": ["FDT 1", "FDT 2"]})
    coreman_db.create_alias(coreman_db_path, {"alias_name": "BALTIC", "canonical_cluster_name": "PERUMAHAN SEGARA CITY CLUSTER BALTIC", "fdt_code": "SGCBT.009", "olt_name": "OLT SEGARA CITY"})
    coreman_db.upsert_sheet(coreman_db_path, {"olt_name": "OLT SEGARA CITY", "segment_name": "1", "sheet_name": "OLT SEGARA CITY - SEG 1"})
    coreman_db.add_audit_log(coreman_db_path, {"event_type": "batch_created", "actor_name": "tester"})

    page = client.get("/coreman")
    summary = client.get("/api/coreman/summary")

    assert page.status_code == 200
    assert b"ManCore Dashboard" in page.data
    assert summary.status_code == 200
    payload = summary.get_json()
    assert payload["summary"]["total_batches"] == 1
    assert payload["summary"]["total_aliases"] == 1
    assert payload["summary"]["total_sheets"] == 1
    assert payload["summary"]["total_logs"] == 1


def test_coreman_create_batch_matches_fdt_records(client, monkeypatch, tmp_path):
    coreman_db_path = tmp_path / "coreman.db"
    fdt_db_path = tmp_path / "fdt_coding.db"
    monkeypatch.setattr(Config, "COREMAN_DB_PATH", coreman_db_path)
    monkeypatch.setattr(Config, "FDT_CODING_DB_PATH", fdt_db_path)
    monkeypatch.setattr(Config, "REQUIRE_ADMIN_API_TOKEN", False)
    fdt_coding_db.create_record(
        fdt_db_path,
        {
            "olt_initial": "OLT CIBARUSAH",
            "hub_id": "BKS.100.2601",
            "olt_name_initial": "OCB",
            "site_name": "PERUM PERMATA CIKARANG SELATAN - FDT 3",
            "fdt_order": 3,
            "fdt_id": "099",
            "fdt_code": "PPRMT.099",
            "index_code": "",
            "cluster_initial": "PPRMT",
            "source_spreadsheet_id": "",
            "sheet_name": "OLT CIBARUSAH",
            "source_row": 99,
        },
    )

    response = client.post("/api/coreman/batches", json={"cluster_name": "PERMATA CIKARANG SELATAN", "fdt_number": "3", "raw_message": "HASIL OPM", "source_sender": "user-a"})

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["batch"]["status"] == "waiting_mancore"
    assert payload["matched_records"][0]["fdt_code"] == "PPRMT.099"


def test_coreman_alias_write_requires_admin_token(client, monkeypatch, tmp_path):
    coreman_db_path = tmp_path / "coreman.db"
    monkeypatch.setattr(Config, "COREMAN_DB_PATH", coreman_db_path)
    monkeypatch.setattr(Config, "REQUIRE_ADMIN_API_TOKEN", True)
    monkeypatch.setattr(Config, "ADMIN_API_TOKEN", "secret-token")

    blocked = client.post("/api/coreman/aliases", json={"alias_name": "BALTIC", "canonical_cluster_name": "PERUMAHAN SEGARA CITY CLUSTER BALTIC"})
    allowed = client.post("/api/coreman/aliases", headers={"X-Admin-Token": "secret-token"}, json={"alias_name": "BALTIC", "canonical_cluster_name": "PERUMAHAN SEGARA CITY CLUSTER BALTIC"})

    assert blocked.status_code == 403
    assert allowed.status_code == 201

