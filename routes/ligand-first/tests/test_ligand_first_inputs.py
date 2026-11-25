from pathlib import Path

from rdkit import Chem
import pytest

from covscreen.ligand_first_inputs import (
    audit_ligand_first_inputs,
    build_parent_pose_mol,
    load_campaign,
    verify_campaign_assets,
    LigandFirstCampaignSpec,
)


ROOT = (Path(__file__).resolve().parents[3] / "science")
CAMPAIGN_PATH = ROOT / "projects/egfr-c797/ligand_first/campaign.json"


def test_native_mode_does_not_require_fabricated_references_or_vectors():
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data.update(planning_mode="upstream-native", covalent_references=[], exit_vectors=[])
    campaign = LigandFirstCampaignSpec.model_validate(data)
    audit, mol = audit_ligand_first_inputs(campaign, CAMPAIGN_PATH.parent)
    assert audit.status == "PASS", audit.errors
    assert mol is not None and audit.scaffold_core_atom_indices
    assert audit.exit_vectors == []
    assert any("does not establish attachment feasibility" in w for w in audit.warnings)
    from covscreen.rdkit_covalentizer import covalentize_parent
    with pytest.raises(ValueError, match="requires upstream-native"):
        covalentize_parent(mol, campaign, "unused", "unused")


@pytest.mark.parametrize("field", ["covalent_references", "exit_vectors"])
def test_default_curated_mode_still_requires_manual_inputs(field):
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data.pop("planning_mode")
    data[field] = []
    with pytest.raises(ValueError, match="curated-v1 requires"):
        LigandFirstCampaignSpec.model_validate(data)


def test_native_parent_only_keeps_real_coordinates_without_inventing_protection():
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data.update(planning_mode="upstream-native")
    for field in ("protection", "covalent_references", "exit_vectors"):
        data.pop(field)
    campaign = LigandFirstCampaignSpec.model_validate(data)
    audit, mol = audit_ligand_first_inputs(campaign, CAMPAIGN_PATH.parent)
    assert audit.status == "PASS", audit.errors
    assert mol.GetNumAtoms() == 29
    assert audit.protected_interactions == []
    assert audit.scaffold_core_atom_indices == []
    assert any("NOT_ASSESSED" in warning for warning in audit.warnings)
    assert any("full-parent graph" in warning for warning in audit.warnings)


@pytest.mark.parametrize("field", ["scaffold_core_atom_names", "protected_pharmacophore_atom_names", "protected_interactions"])
def test_curated_protection_cannot_be_silently_omitted(field):
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data["protection"].pop(field)
    with pytest.raises(ValueError, match="curated-v1 requires scaffold"):
        LigandFirstCampaignSpec.model_validate(data)


@pytest.mark.parametrize("mode", ["curated-v1", "upstream-native"])
@pytest.mark.parametrize("field", ["structure_path", "ligand_template_path"])
def test_consumed_parent_assets_must_be_frozen(mode, field):
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data["planning_mode"] = mode
    data["source_assets"] = [asset for asset in data["source_assets"]
                             if asset["path"] != data["parent_pose"][field]]
    with pytest.raises(ValueError, match="require frozen source assets"):
        LigandFirstCampaignSpec.model_validate(data)


def test_duplicate_source_paths_cannot_hide_conflicting_hashes():
    data = load_campaign(CAMPAIGN_PATH).model_dump()
    data["source_assets"].append({**data["source_assets"][0], "sha256": "0" * 64})
    with pytest.raises(ValueError, match="duplicate source asset paths"):
        LigandFirstCampaignSpec.model_validate(data)


def test_real_egfr_campaign_assets_and_residue_mapping_are_locked():
    campaign = load_campaign(CAMPAIGN_PATH)
    assert campaign.target.symbol == "EGFR"
    assert campaign.target.nucleophile.uniprot_residue_number == 797
    assert campaign.parent_pose.target_author_residue_number == 773
    assert campaign.parent_pose.target_author_residue_number + campaign.parent_pose.residue_number_offset == 797
    assert all(verify_campaign_assets(campaign, CAMPAIGN_PATH.parent).values())


def test_parent_pose_uses_experimental_coordinates_and_audited_atom_names():
    campaign = load_campaign(CAMPAIGN_PATH)
    mol = build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent)
    assert mol.GetNumAtoms() == 29
    assert mol.GetNumConformers() == 1
    assert [atom.GetProp("pdb_atom_name") for atom in mol.GetAtoms()] == campaign.ligand_atom_names_in_template_order
    assert Chem.MolToSmiles(mol, canonical=True) == "C#Cc1cccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)c1"


def test_real_parent_pose_protection_interaction_and_exit_vector_audit():
    campaign = load_campaign(CAMPAIGN_PATH)
    audit, mol = audit_ligand_first_inputs(campaign, CAMPAIGN_PATH.parent)
    assert mol is not None
    assert audit.status == "PASS", audit.errors
    assert len(audit.scaffold_core_atom_indices) == 17
    assert len(audit.protected_pharmacophore_atom_indices) == 4
    assert audit.protected_interactions[0].interaction_id == "hinge-met793"
    assert abs(audit.protected_interactions[0].distance_angstrom - 2.699) < 0.01
    primary, negative = audit.exit_vectors
    assert primary.exit_vector_id == "quinazoline-c6-primary"
    assert primary.advance is True
    assert abs(primary.distance_to_target_angstrom - 7.019) < 0.01
    assert abs(primary.vector_to_target_angle_degrees - 57.581) < 0.02
    assert negative.advance is False
    assert abs(negative.vector_to_target_angle_degrees - 90.260) < 0.02
