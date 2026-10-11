import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hpdb
import kmz_standardizer as standardizer

NS = "http://www.opengis.net/kml/2.2"


def hp_name(source: str) -> str:
    folder = ET.Element(f"{{{NS}}}Folder")
    ET.SubElement(folder, f"{{{NS}}}name").text = "HP COVER"
    placemark = ET.SubElement(folder, f"{{{NS}}}Placemark")
    ET.SubElement(placemark, f"{{{NS}}}name").text = source
    standardizer.normalize_hp_cover_child_folders(folder, NS, [])
    return standardizer.direct_child_text_by_name(placemark, "name")


def test_block_code_separator_is_not_nn_and_parses_to_block_homenumber():
    for source in ("B2.19", "B2-19", "B2/19"):
        assert hp_name(source) == source
        assert hpdb.parse_homepass_block_and_number(source) == ("B2", "19")


def test_legacy_nn_block_code_repairs_then_parses_to_block_homenumber():
    for source, expected in (("NN-B2.19", "B2.19"), ("NN-B2-19", "B2-19"), ("NN-B2/19", "B2/19")):
        fixed = hp_name(source)
        assert fixed == expected
        assert hpdb.parse_homepass_block_and_number(fixed) == ("B2", "19")


def test_customer_name_keeps_nn_prefix_and_no_block_split():
    assert hp_name("Kontrakan-13") == "NN-KONTRAKAN-13"
    assert hpdb.parse_homepass_block_and_number("NN-KONTRAKAN-13") == ("", "NN-KONTRAKAN-13")


if __name__ == "__main__":
    test_block_code_separator_is_not_nn_and_parses_to_block_homenumber()
    test_legacy_nn_block_code_repairs_then_parses_to_block_homenumber()
    test_customer_name_keeps_nn_prefix_and_no_block_split()
    print("HP_BLOCK_HOMENUMBER_SEPARATOR_TESTS_PASS")
