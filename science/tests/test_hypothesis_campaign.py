import json
import shutil
from pathlib import Path

import pytest
import yaml

from covscreen.hypothesis_campaign import isolate
from covscreen.hypothesis_campaign import isolate_library
from covscreen.ligand_chain import LigandFirstChainConfig, STAGE_ORDER, inspect_ligand_chain


@pytest.fixture
def template(tmp_path):
    source = Path(__file__).resolve().parents[1]
    relative = "configs/ligand_first/EGFR_C797_erlotinib_native_validation_v1.yaml"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    shutil.copyfile(source / relative, path)
    config = yaml.safe_load(path.read_text())
    campaign = tmp_path / config["campaign_path"]
    campaign.parent.mkdir(parents=True)
    shutil.copyfile(source / config["campaign_path"], campaign)
    return tmp_path, relative, campaign


def test_new_identity_is_idempotent_and_has_no_historical_runs(template):
    root, relative, campaign = template
    originals = ((root / relative).read_bytes(), campaign.read_bytes())
    receipt = isolate(root, "hyp-" + "a" * 24, {"rationale": "test"}, relative)
    assert receipt == isolate(root, "hyp-" + "a" * 24, {"rationale": "test"}, relative)
    config = LigandFirstChainConfig.model_validate(yaml.safe_load((root / receipt["config_path"]).read_text()))
    original = yaml.safe_load(originals[0])
    assert config.project_id != original["project_id"]
    assert config.campaign_id != original["campaign_id"]
    assert config.stage_order == list(STAGE_ORDER)
    assert not config.seed_run_ids
    assert config.dispatch_policy is None
    binding = receipt["input_binding"]
    assert binding["paths"]["pdb"] == config.pdb
    assert binding["paths"]["reference_ligand"] == config.reference_ligand
    assert binding["project_id"] == config.project_id
    assert binding["parent_pose"]["target_author_residue_number"] == 773
    assert binding["parent_pose"]["target_uniprot_residue_number"] == 797
    assert binding["parent_pose"]["protein_chain_id"] == "A"
    assert binding["seed_run_ids"] == {}
    chain = inspect_ligand_chain(root, config)
    assert chain.next_stage == "structure_intake"
    assert all(stage.run_id is None for stage in chain.stages)
    assert json.loads((root / config.campaign_path).read_text())["campaign_id"] == config.campaign_id
    assert originals == ((root / relative).read_bytes(), campaign.read_bytes())


def test_changed_hypothesis_gets_different_campaign(template):
    root, relative, _ = template
    first = isolate(root, "hyp-" + "a" * 24, {"rationale": "first"}, relative)
    second = isolate(root, "hyp-" + "a" * 24, {"rationale": "second"}, relative)
    assert first["campaign_id"] != second["campaign_id"]


def test_registration_cannot_silently_overwrite_changed_config(template):
    root, relative, _ = template
    receipt = isolate(root, "hyp-" + "a" * 24, {}, relative)
    (root / receipt["config_path"]).write_text("tampered")
    with pytest.raises(ValueError, match="Immutable hypothesis campaign"):
        isolate(root, "hyp-" + "a" * 24, {}, relative)


def test_library_new_study_clears_seeds_and_compiles_real_first_stage(tmp_path):
    from test_library_intake import _ready_root
    from covscreen.tool_api import library_first_chain_tool
    _ready_root(tmp_path)
    relative = "configs/library_first/EGFR_pilot.yaml"
    source = tmp_path / relative
    original = yaml.safe_load(source.read_text())
    original["seed_run_ids"] = {"prescreen": "old-historical-run"}
    source.write_text(yaml.safe_dump(original))
    first = isolate_library(tmp_path, "hyp-" + "a" * 24, {"study_id": "study-" + "1" * 36}, relative, "EGFR", "C797")
    second = isolate_library(tmp_path, "hyp-" + "a" * 24, {"study_id": "study-" + "2" * 36}, relative, "EGFR", "C797")
    assert first["campaign_id"] != second["campaign_id"]
    config = yaml.safe_load((tmp_path / first["config_path"]).read_text())
    assert config["seed_run_ids"] == {}
    assert config["project_id"] != original["project_id"]
    assert config["pdb"] == original["pdb"]
    planned = library_first_chain_tool(tmp_path, first["config_path"], execution_mode="plan", submit=False)
    assert planned["chain"]["next_stage"] == "prescreen"
    assert all(stage["run_id"] is None for stage in planned["chain"]["stages"])
    assert planned["next_request"]["project_id"] == config["project_id"]
    assert planned["next_request"]["human_approved"] is False
    assert planned["submitted"] is False
    assert yaml.safe_load(source.read_text()) == original


def test_library_other_site_never_falls_back_to_egfr(tmp_path):
    from test_library_intake import _ready_root
    _ready_root(tmp_path)
    with pytest.raises(ValueError, match="target/site"):
        isolate_library(tmp_path, "hyp-" + "a" * 24, {}, "configs/library_first/EGFR_pilot.yaml", "JAK3", "C909")


def test_ligand_study_identity_separates_same_hypothesis(template):
    root, relative, _ = template
    first = isolate(root, "hyp-" + "a" * 24, {"study_id": "study-" + "1" * 36}, relative)
    second = isolate(root, "hyp-" + "a" * 24, {"study_id": "study-" + "2" * 36}, relative)
    assert first["campaign_id"] != second["campaign_id"]


def test_ligand_campaign_identity_is_checked_before_any_new_config(template):
    root, relative, _ = template
    evidence = {"target": "EGFR", "site": "C797", "taxon": 9606, "identity": {"uniprot_id": "P00533"}}
    hypothesis = {"parent_ligand": "erlotinib", "parent_binding_mode": "noncovalent", "warhead_class": ["acrylamide"]}
    before = set(root.rglob("*"))
    for changed in [{**evidence, "target": "BTK"}, {**evidence, "site": "C481"}, {**evidence, "taxon": 10090}]:
        with pytest.raises(ValueError, match="target/site/species"):
            isolate(root, "hyp-" + "a" * 24, hypothesis, relative, evidence=changed)
    for changed in [{**hypothesis, "parent_ligand": "gefitinib"}, {**hypothesis, "parent_binding_mode": "covalent"}]:
        with pytest.raises(ValueError, match="campaign parent"):
            isolate(root, "hyp-" + "a" * 24, changed, relative, evidence=evidence)
    with pytest.raises(ValueError, match="warheads"):
        isolate(root, "hyp-" + "a" * 24, {**hypothesis, "warhead_class": ["chloroacetamide"]}, relative, evidence=evidence)
    assert set(root.rglob("*")) == before
    receipt = isolate(root, "hyp-" + "a" * 24, hypothesis, relative, evidence=evidence)
    assert receipt["historical_runs_reused"] is False
