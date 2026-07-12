from pathlib import Path

import app as web_app
from config import Config


def test_download_names_include_network_type_suffix():
    names = web_app.download_names_for_mode(["BKS004470 - SERANG.kmz"], "dxf_boq_hpdb", "UG")

    assert names["dxf"] == "BKS004470 - SERANG_UG.dxf"
    assert names["boq"] == "BKS004470 - SERANG_BOQ_UG.xlsx"
    assert names["hpdb"] == "BKS004470 - SERANG_HPDB_UG.xlsx"
    assert names["final"] == "BKS004470 - SERANG_OUTPUT_UG.zip"

def test_kmz_download_name_matches_uploaded_kmz_name():
    names = web_app.download_names_for_mode(["BKS004470 - SERANG.kmz"], "kmz", "UG")

    assert web_app.standard_kmz_download_name("BKS004470 - SERANG.kmz") == "BKS004470 - SERANG.kmz"
    assert names["kmz"] == "BKS004470 - SERANG.kmz"
    assert names["final"] == "BKS004470 - SERANG.kmz"

def test_kml_standard_output_downloads_as_same_stem_kmz():
    names = web_app.download_names_for_mode(["Cluster Design.kml"], "kmz", "UG")

    assert names["kmz"] == "Cluster Design.kmz"
    assert names["final"] == "Cluster Design.kmz"

def test_standardized_kmz_entry_uses_original_upload_name(monkeypatch, tmp_path):
    job_id = "job-original-name"
    saved_name = f"{job_id}_1_BKS004470_-_SERANG.kmz"
    saved_path = tmp_path / saved_name
    saved_path.write_bytes(b"placeholder")
    original_name = "BKS004470 - SERANG.kmz"

    class Logger:
        def info(self, _message):
            pass

        def success(self, _message):
            pass

        def warn(self, _message):
            pass

    monkeypatch.setattr(web_app, "fdt_coding_source_ready", lambda: True)
    monkeypatch.setattr(web_app, "resolve_fdt_coding_workbook", lambda _job_id, _logger: tmp_path / "coding.xlsx")

    def fake_standardize_kmz_file(_source_path, _coding_path, output_path, network_type="AE", scope_type="Cluster", **_kwargs):
        return {"output_path": str(output_path), "warnings": []}

    monkeypatch.setattr(web_app, "standardize_kmz_file", fake_standardize_kmz_file)

    web_app.create_job(
        job_id,
        [saved_path],
        tmp_path / "out.zip",
        [],
        download_name="out.zip",
        output_mode="dxf_kmz",
    )
    web_app.update_job(job_id, original_filenames=[original_name])

    _paths, standard_kmz_outputs, summary = web_app.standardize_kml_inputs_for_job(
        job_id,
        [saved_path],
        "dxf_kmz",
        "AE",
        Logger(),
    )

    assert standard_kmz_outputs[0]["download_name"] == original_name
    assert standard_kmz_outputs[0]["source_name"] == original_name
    assert summary["source_name"] == original_name


def test_boq_nro_metadata_uses_original_kmz_name_and_matched_sheet():
    metadata = web_app.boq_nro_metadata_from_standardizer(
        {
            "source_name": "BKS004203 - PERUMAHAN JATIMULYA BLOK I BEKASI.kmz",
            "matched_sheet": "OLT Taman Narogong",
        }
    )

    assert metadata == {
        "cluster_name": "PERUMAHAN JATIMULYA BLOK I BEKASI",
        "olt_name": "OLT Taman Narogong",
    }


def test_fdt_coding_count_uses_real_spreadsheet_matches_only():
    assert web_app.fdt_coding_count_from_standardizer(
        {
            "default_fdt_coding": False,
            "available_fdt_codes": [{"fdt_code": "A.001"}, {"fdt_code": "A.002"}, {"fdt_code": "A.003"}],
        }
    ) == 3
    assert web_app.fdt_coding_count_from_standardizer(
        {
            "default_fdt_coding": True,
            "available_fdt_codes": [{"fdt_code": "XXX"}],
        }
    ) == 0

def test_variant_template_paths_use_copied_ae_ug_files():
    assert web_app.boq_template_path_for_network("AE") == Config.BOQ_TEMPLATE_PATH_AE
    assert web_app.boq_template_path_for_network("UG") == Config.BOQ_TEMPLATE_PATH_UG
    assert web_app.hpdb_template_path_for_network("AE") == Config.HPDB_TEMPLATE_PATH_AE
    assert web_app.hpdb_template_path_for_network("UG") == Config.HPDB_TEMPLATE_PATH_UG
    assert web_app.dxf_template_path_for_network("AE") == Config.DXF_TEMPLATE_PATH_AE
    assert web_app.dxf_template_path_for_network("UG") == Config.DXF_TEMPLATE_PATH_UG
    assert Config.BOQ_TEMPLATE_PATH_AE.name.endswith("_AE.xlsx")
    assert Config.BOQ_TEMPLATE_PATH_UG.name.endswith("_UG.xlsx")
    assert Config.DXF_TEMPLATE_PATH_AE.name.endswith("_AE.dxf")
    assert Config.DXF_TEMPLATE_PATH_UG.name.endswith("_UG.dxf")


def test_job_status_payload_exposes_selected_network_template_names():
    job = {
        "job_id": "job-1",
        "status": "queued",
        "progress": 0,
        "message": "queued",
        "stats": {},
        "input_paths": [str(Path("input.kmz"))],
        "output_mode": "boq",
        "network_type": "UG",
        "download_name": "PROJECT_BOQ_UG.xlsx",
    }

    with web_app.app.test_request_context():
        payload = web_app.job_status_payload(job)

    assert payload["network_type"] == "UG"
    assert payload["network_type_label"] == "UG / Underground"
    assert payload["boq_template_name"].endswith("_UG.xlsx")
    assert payload["astri_template_name"].endswith("_UG.xlsx")
