import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kmz_standardizer as standardizer

NS = "http://www.opengis.net/kml/2.2"


def hp_name(source: str) -> str:
    folder = ET.Element(f"{{{NS}}}Folder")
    ET.SubElement(folder, f"{{{NS}}}name").text = "HP COVER"
    placemark = ET.SubElement(folder, f"{{{NS}}}Placemark")
    ET.SubElement(placemark, f"{{{NS}}}name").text = source
    standardizer.normalize_hp_cover_child_folders(folder, NS, [])
    return standardizer.direct_child_text_by_name(placemark, "name")


def test_dotted_block_codes_never_get_nn_prefix():
    assert hp_name("B2.19") == "B2.19"
    assert hp_name("b2.24") == "B2.24"
    assert hp_name("C12.7") == "C12.7"
    assert hp_name("NN-B2.19") == "B2.19"


def test_customer_name_still_gets_nn_prefix():
    assert hp_name("Kontrakan-13") == "NN-KONTRAKAN-13"


if __name__ == "__main__":
    test_dotted_block_codes_never_get_nn_prefix()
    test_customer_name_still_gets_nn_prefix()
    print("HP_COVER_DOTTED_BLOCK_TESTS_PASS")
