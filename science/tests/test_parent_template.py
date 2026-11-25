import json
from pathlib import Path
import pytest
from rdkit import Chem
from covscreen.parent_template import ccd_template_molecule, build_parent_template
from covscreen.ligand_first_inputs import LigandFirstCampaignSpec, build_parent_pose_mol

ROOT = Path(__file__).resolve().parents[1]
CCD = ROOT / "tests/fixtures/AQ4.cif"
CAMPAIGN = ROOT / "projects/egfr-c797/ligand_first/campaign.json"
KEY = "AAKJLRGGTJKAMG-UHFFFAOYSA-N"


def test_uppercase_ccd_chlorine_preserves_full_parent_identity():
    from Bio.PDB.MMCIF2Dict import MMCIF2Dict
    source = ROOT / "tests/fixtures/IRE.cif"
    data = MMCIF2Dict(str(source))
    names = [name for name, element in zip(data['_chem_comp_atom.atom_id'], data['_chem_comp_atom.type_symbol']) if element != 'H']
    assert 'CL' in data['_chem_comp_atom.type_symbol']
    mol = ccd_template_molecule(source, names, 'XGALLCVXEZPNRQ-UHFFFAOYSA-N')
    assert Chem.MolToInchiKey(mol) == 'XGALLCVXEZPNRQ-UHFFFAOYSA-N'
    assert sum(atom.GetSymbol() == 'Cl' for atom in mol.GetAtoms()) == 1


def test_real_ccd_template_respects_explicit_atom_order():
    names = json.loads(CAMPAIGN.read_text())["ligand_atom_names_in_template_order"][::-1]
    mol = ccd_template_molecule(CCD, names, KEY)
    assert [a.GetProp("pdb_atom_name") for a in mol.GetAtoms()] == names
    assert mol.GetNumAtoms() == 29
    assert Chem.MolToInchiKey(mol) == KEY


@pytest.mark.parametrize("invalid", ["key", "missing", "duplicate"])
def test_template_rejects_wrong_key_or_atom_mapping(invalid):
    names = json.loads(CAMPAIGN.read_text())["ligand_atom_names_in_template_order"]
    key = KEY
    if invalid == "key": key = "AAAAAAAAAAAAAA-BBBBBBBBBB-C"
    if invalid == "missing": names = names[:-1]
    if invalid == "duplicate": names = names[:-1] + names[:1]
    with pytest.raises(ValueError):
        ccd_template_molecule(CCD, names, key)


def test_generated_template_reconstructs_existing_experimental_parent(tmp_path):
    data = json.loads(CAMPAIGN.read_text())
    original = LigandFirstCampaignSpec.model_validate(data)
    parent = build_parent_pose_mol(original, CAMPAIGN.parent)
    mapping = {"status": "sequence_mapping_verified_coordinates_pending", "entry_id": "1M17",
               "auth_asym_id": "A", "author_residue_id": "773", "uniprot": "P00533", "uniprot_residue": 797}
    result = build_parent_template(CCD, CAMPAIGN.parent / data["parent_pose"]["structure_path"],
                                   mapping, "AQ4", KEY, tmp_path / "template")
    assert result["campaign_ready"] is False
    path = str(tmp_path / "template/parent_template.sdf")
    old = data["parent_pose"]["ligand_template_path"]
    data["parent_pose"]["ligand_template_path"] = path
    data["source_assets"] = [a for a in data["source_assets"] if a["path"] != old]
    data["source_assets"].append({"path": path, "sha256": result["template_sha256"],
                                  "source_url": "https://files.rcsb.org/ligands/download/AQ4.cif"})
    rebuilt = build_parent_pose_mol(LigandFirstCampaignSpec.model_validate(data), CAMPAIGN.parent)
    assert Chem.MolToInchiKey(rebuilt) == KEY
    assert (parent.GetConformer().GetPositions() == rebuilt.GetConformer().GetPositions()).all()
