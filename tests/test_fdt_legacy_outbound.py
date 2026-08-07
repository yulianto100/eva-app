import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdt_sync_service


class FakeWorksheet:
    def __init__(self):
        self.updates = []
        self.formats = []

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)

    def format(self, target, payload):
        self.formats.append((target, payload))


def test_legacy_update_writes_only_site_fdt_code_and_segment():
    ws = FakeWorksheet()
    fdt_sync_service._outbound_update_row(
        ws,
        1,
        {"legacy_template": 1, "site": 2, "fdt_code": 3, "segment": 5, "fdt_no": 1},
        25,
        {
            "site_name": "SITE EDIT",
            "fdt_code": "PSR.155",
            "index_code": "S02",
            "fdt_id": "155",
            "hub_id": "IDM.100.0101",
        },
    )
    assert [(item["range"], item["values"]) for item in ws.updates] == [
        ("B25", [["SITE EDIT"]]),
        ("C25", [["PSR.155"]]),
        ("E25", [["S02"]]),
    ]
    assert ws.formats == []


def test_legacy_record_without_source_routes_to_declared_source_tab():
    source_id, sheet_name = fdt_sync_service._default_source_for_record(
        Path("/tmp/unused-fdt.db"),
        {"hub_id": "KNG.100.0201", "olt_initial": "OLT CIAWIGEBANG"},
    )
    assert source_id == fdt_sync_service.LEGACY_FDT_SPREADSHEET_ID
    assert sheet_name == "KUNINGAN"


def test_legacy_olt_management_marks_shared_sheet_as_protected():
    assert fdt_sync_service._legacy_shared_sheet_target("IDM.100.0102") == (
        fdt_sync_service.LEGACY_FDT_SPREADSHEET_ID,
        "OLT INDRAMAYU",
    )
    assert fdt_sync_service._legacy_shared_sheet_target("KNG.100.0201") == (
        fdt_sync_service.LEGACY_FDT_SPREADSHEET_ID,
        "KUNINGAN",
    )
    assert fdt_sync_service._legacy_shared_sheet_target("CWG.100.0201") is None


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("FDT_LEGACY_OUTBOUND_TESTS_PASS")
