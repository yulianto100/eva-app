import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdt_sync_service


class FakeWorksheet:
    def __init__(self):
        self.formula = [
            ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE", "FAT"],
            ["=RIGHT(C2,3)", "", "", "BASE.001", "", '=\"KNG.100.0101.\"&C2', "=C2&\".A01\""],
        ]
        self.formatted = [
            ["NO", "SITE", "FDT ID", "x", "SEGMENT", "OLT CODE", "FAT"],
            ["", "", "", "BASE.001", "", "KNG.100.0101.", ".A01"],
        ]
        self.copies = []
        self.updates = []

    def get_values(self, value_render_option=None, **_kwargs):
        return self.formula if value_render_option == "FORMULA" else self.formatted

    def copy_range(self, *args, **kwargs):
        self.copies.append((args, kwargs))

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)


def test_legacy_create_reuses_empty_formula_row_for_same_hub():
    worksheet = FakeWorksheet()
    columns = {
        "legacy_template": 1,
        "legacy_last_column": 7,
        "site": 2,
        "fdt_code": 3,
        "segment": 5,
        "olt_code": 6,
    }
    record = {
        "hub_id": "KNG.100.0101",
        "site_name": "QA REUSE",
        "fdt_code": "QAKNG.003",
        "index_code": "S02",
    }

    row = fdt_sync_service._append_legacy_formula_row(worksheet, 1, columns, record)

    assert row == 2
    assert worksheet.copies == []
    assert [(item["range"], item["values"]) for item in worksheet.updates] == [
        ("B2", [["QA REUSE"]]),
        ("C2", [["QAKNG.003"]]),
        ("E2", [["S02"]]),
    ]


if __name__ == "__main__":
    test_legacy_create_reuses_empty_formula_row_for_same_hub()
    print("FDT_LEGACY_REUSE_ROW_TESTS_PASS")
