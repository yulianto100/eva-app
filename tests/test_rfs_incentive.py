import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rfs_sheets


def test_incentive_uses_city_ytd_difficulty_and_achievement():
    result = rfs_sheets._incentive_result(
        region="Bekasi",
        area_progress=0.877,
        national_progress=0.8173,
        ytd_progress=0.8058,
        target_hp=5000,
    )

    assert result["status"] == "ELIGIBLE"
    assert result["difficulty"] == 1.25
    assert result["payout_factor"] == 1.0
    assert result["incentive_idr"] == round(2_000_000 * 1.25 * 0.877 * 0.8058)


def test_incentive_eligible_regardless_of_national_achievement():
    result = rfs_sheets._incentive_result(
        region="Sukabumi",
        area_progress=0.85,
        national_progress=0.60,
        ytd_progress=1.0,
        target_hp=5000,
    )

    assert result["status"] == "ELIGIBLE"
    assert result["payout_factor"] == 1.0
    assert result["incentive_idr"] == round(2_000_000 * 1.0 * 0.85 * 1.0 * 1.0)


def test_incentive_marks_missing_target():
    result = rfs_sheets._incentive_result(
        region="Bekasi",
        area_progress=None,
        national_progress=None,
        ytd_progress=None,
        target_hp=0,
    )

    assert result["status"] == "NO TARGET"
    assert result["incentive_idr"] == 0
