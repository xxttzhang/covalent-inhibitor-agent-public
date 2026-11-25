import pytest
from covscreen.parent_chemical_identity import audit_parent_chemical_identity

KEY = "AAAAAAAAAAAAAA-BBBBBBBBBB-C"
CCD = '''data_ABC
_chem_comp.id ABC
loop_
_chem_comp_atom.comp_id
_chem_comp_atom.atom_id
_chem_comp_atom.type_symbol
ABC C1 C
ABC O1 O
ABC H1 H
loop_
_chem_comp_bond.atom_id_1
_chem_comp_bond.atom_id_2
_chem_comp_bond.value_order
C1 O1 SING
C1 H1 SING
loop_
_pdbx_chem_comp_descriptor.comp_id
_pdbx_chem_comp_descriptor.type
_pdbx_chem_comp_descriptor.descriptor
ABC InChIKey AAAAAAAAAAAAAA-BBBBBBBBBB-C
'''


def audit():
    return {"status": "coordinate_checks_passed_linkage_review_pending", "component": "ABC",
            "structure_sha256": "a" * 64,
            "ligand_heavy_atoms": [{"name": "O1", "element": "O"}, {"name": "C1", "element": "C"}]}


def test_declared_identity_keeps_coordinate_order_and_limits_claim(tmp_path):
    path = tmp_path / "ABC.cif"
    path.write_text(CCD)
    result = audit_parent_chemical_identity(path, audit(), KEY)
    assert result["heavy_atom_count"] == 2
    assert result["coordinate_atom_order"] == ["O1", "C1"]
    assert result["campaign_ready"] is False


@pytest.mark.parametrize("failure", ["key", "element", "missing", "duplicate", "component", "bond"])
def test_mismatched_identity_cannot_pass(tmp_path, failure):
    text, evidence, key = CCD, audit(), KEY
    if failure == "key": key = "CCCCCCCCCCCCCC-BBBBBBBBBB-C"
    if failure == "element": evidence["ligand_heavy_atoms"][0]["element"] = "N"
    if failure == "missing": evidence["ligand_heavy_atoms"].pop()
    if failure == "duplicate": evidence["ligand_heavy_atoms"].append(evidence["ligand_heavy_atoms"][0])
    if failure == "component": evidence["component"] = "XYZ"
    if failure == "bond": text = text.replace("C1 O1 SING", "C1 X1 SING")
    path = tmp_path / "ABC.cif"
    path.write_text(text)
    with pytest.raises(ValueError):
        audit_parent_chemical_identity(path, evidence, key)
