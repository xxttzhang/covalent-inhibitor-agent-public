from pathlib import Path

from covscreen.library_intake import resolve_library_first_intake


def _ready_root(tmp_path: Path) -> Path:
    target_dir = tmp_path / "configs" / "targets"
    campaign_dir = tmp_path / "configs" / "library_first"
    prescreen_dir = tmp_path / "configs" / "covindb"
    target_dir.mkdir(parents=True)
    campaign_dir.mkdir(parents=True)
    prescreen_dir.mkdir(parents=True)
    (target_dir / "EGFR.yaml").write_text(
        "\n".join(
            [
                "target_id: EGFR",
                "site_id: EGFR_C797",
                'target_regex: \"(?i)EGFR\"',
                "protein_chain: A",
                "residue_name: CYS",
                "residue_number: 797",
                "nucleophilic_atom: SG",
                "reaction_type: Michael Addition",
                "warhead_class: acrylamide",
            ]
        )
    )
    (target_dir / "ERBB2.yaml").write_text(
        "\n".join(
            [
                "target_id: ERBB2",
                "site_id: ERBB2_C805",
                'target_regex: \"(?i)ERBB2|HER2\"',
                "protein_chain: A",
                "residue_name: CYS",
                "residue_number: 805",
                "nucleophilic_atom: SG",
                "reaction_type: Michael Addition",
                "warhead_class: acrylamide",
            ]
        )
    )
    (prescreen_dir / "EGFR.yaml").write_text("target_id: EGFR\nsite_id: EGFR_C797\n")
    for name in ("protein.pdb", "reference.sdf", "seed.json", "receptor.mae"):
        (tmp_path / name).write_text("test")
    (campaign_dir / "EGFR_pilot.yaml").write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "campaign_id: egfr-c797-covindb-pilot-v1",
                "project_id: egfr-c797",
                "target_config: configs/covindb/EGFR.yaml",
                "pdb: protein.pdb",
                "reference_ligand: reference.sdf",
                "af3_source_data_json: seed.json",
                "covdock_receptor: receptor.mae",
                'attachment_residue: "A:797"',
                'covres: "A:CYS:797"',
            ]
        )
    )
    library = tmp_path / "covindb.csv"
    library.write_text("ID,SMILES\n1,CC\n")
    (tmp_path / "configs" / "local.inspire.yaml").write_text(
        f"covindb:\n  virtual_screening_library: {library}\n"
    )
    return tmp_path


def test_intake_resolves_natural_language_to_ready_campaign(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(
        root,
        "针对 EGFR C797，从 CovalentInDB 中筛选共价抑制剂",
    )
    assert result.state == "READY"
    assert result.target is not None
    assert result.target["site_id"] == "EGFR_C797"
    assert result.target_inferred is False
    assert result.campaign_config_path == "configs/library_first/EGFR_pilot.yaml"
    assert result.approval_required is True


def test_intake_uses_registered_site_when_target_only_is_supplied(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(root, "针对 EGFR 做共价抑制剂库筛选")
    assert result.state == "READY"
    assert result.target_inferred is True
    assert result.target is not None
    assert result.target["residue_number"] == 797


def test_intake_stops_on_site_mismatch(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(root, "针对 EGFR C481 做共价抑制剂库筛选")
    assert result.state == "SITE_MISMATCH"
    assert result.approval_required is False


def test_intake_reports_registered_target_without_campaign(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(root, "针对 HER2 C805 做共价抑制剂库筛选")
    assert result.state == "CAMPAIGN_NOT_CONFIGURED"
    assert result.target is not None
    assert result.target["target_id"] == "ERBB2"


def test_intake_does_not_silently_expand_pilot_to_full_library(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(root, "针对 EGFR C797 做全库筛选")
    assert result.state == "SCOPE_NOT_CONFIGURED"
    assert result.requested_scope == "full"


def test_intake_unknown_target_requires_dossier(tmp_path: Path):
    root = _ready_root(tmp_path)
    result = resolve_library_first_intake(root, "针对 CDK2 做共价抑制剂库筛选")
    assert result.state == "TARGET_NOT_REGISTERED"
    assert result.campaign_config_path is None
