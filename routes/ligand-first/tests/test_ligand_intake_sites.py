from pathlib import Path

import pytest

from covscreen.ligand_intake import _site_numbers, resolve_ligand_first_intake


@pytest.mark.parametrize("query,expected", [
    ("针对C797做共价化", {797}),
    ("靶向Cys797位点", {797}),
    ("A:CYS:797", {797}),
    ("半胱氨酸-797", {797}),
    ("C797和C775", {797, 775}),
    ("C797S", set()),
    ("ABC797", set()),
    ("C7970", {7970}),
])
def test_residue_parsing(query, expected):
    assert _site_numbers(query) == expected


def test_missing_site_never_uses_registered_default():
    root = (Path(__file__).resolve().parents[3] / "science")
    result = resolve_ligand_first_intake(root, "针对EGFR将erlotinib共价化")
    assert result.state == "SITE_REQUIRED"
    assert result.campaign_config_path is None
    assert not result.approval_required


def test_wrong_site_next_to_chinese_is_not_discarded():
    root = (Path(__file__).resolve().parents[3] / "science")
    result = resolve_ligand_first_intake(root, "针对EGFR的C775位点将erlotinib共价化")
    assert result.state == "SITE_MISMATCH"


def test_correct_chinese_query_keeps_registered_site():
    root = (Path(__file__).resolve().parents[3] / "science")
    result = resolve_ligand_first_intake(root, "针对EGFR的C797位点将erlotinib共价化")
    assert result.state in {"READY", "INPUTS_MISSING"}
    assert result.target["residue"] == "CYS797"


def test_explicit_native_config_does_not_fall_back_to_legacy_aliases():
    root = (Path(__file__).resolve().parents[3] / "science")
    config = "configs/ligand_first/EGFR_C797_erlotinib_native_validation_v1.yaml"
    result = resolve_ligand_first_intake(root, f"针对EGFR C797将erlotinib共价化，使用 `{config}`。")
    assert result.state in {"READY", "INPUTS_MISSING"}
    assert result.campaign_config_path == config
    wrong = resolve_ligand_first_intake(root, f"针对EGFR C775将erlotinib共价化，使用 `{config}`。")
    assert wrong.state == "SITE_MISMATCH"


def test_unknown_explicit_config_does_not_silently_choose_registered_default():
    root = (Path(__file__).resolve().parents[3] / "science")
    result = resolve_ligand_first_intake(root,
        "EGFR C797 erlotinib configs/ligand_first/does_not_exist.yaml")
    assert result.state == "CAMPAIGN_NOT_REGISTERED"
    assert result.campaign_config_path is None
