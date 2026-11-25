import pytest
import json

from covscreen.pose_topology import compare_named_graphs


def test_named_graph_matches_numeric_and_ccd_orders():
    atoms = [("C1", "C"), ("C2", "C"), ("O1", "O")]
    result = compare_named_graphs(atoms, [("C1", "C2", "SING"), ("C2", "O1", "DOUB")],
        atoms[::-1], [("O1", "C2", 2), ("C2", "C1", 1)], "C1", "C1")
    assert result["topology_heavy_bond_count"] == 2
    assert result["pose_topology_status"] == "VERIFIED_FIXED_NAME_GRAPH"


@pytest.mark.parametrize("failure", ["bond_order", "partner", "element", "missing_bond", "duplicate_atom", "duplicate_bond"])
def test_topology_mismatch_never_returns_verified(failure):
    atoms = [("C1", "C"), ("C2", "C"), ("O1", "O")]
    other = list(atoms)
    bonds = [("C1", "C2", 1), ("C2", "O1", 2)]
    partner = "C1"
    if failure == "bond_order": bonds[0] = ("C1", "C2", 2)
    if failure == "partner": partner = "C2"
    if failure == "element": other[-1] = ("O1", "N")
    if failure == "missing_bond": bonds.pop()
    if failure == "duplicate_atom": other.append(other[0])
    if failure == "duplicate_bond": bonds.append(bonds[0])
    with pytest.raises(ValueError):
        compare_named_graphs(atoms, [("C1", "C2", "SING"), ("C2", "O1", "DOUB")], other, bonds, "C1", partner)


@pytest.mark.parametrize("bad_link", [False, True])
def test_model_connectivity_must_match_input(tmp_path, monkeypatch, bad_link):
    from covscreen.pose_topology import verify_pose_topology
    model = tmp_path / "example_model.cif"
    model.write_text("placeholder")
    (tmp_path / "example_data.json").write_text(json.dumps({"name": "example", "userCCD": "ccd",
        "bondedAtomPairs": [[["A", 105, "SG"], ["L", 1, "C1"]]]}))
    ccd = {"_chem_comp_atom.atom_id": ["C1", "C2"], "_chem_comp_atom.type_symbol": ["C", "C"],
           "_chem_comp_bond.atom_id_1": ["C1"], "_chem_comp_bond.atom_id_2": ["C2"],
           "_chem_comp_bond.value_order": ["SING"]}
    model_dict = {"_struct_conn.conn_type_id": ["covale"]}
    for n, (chain, residue, atom) in enumerate([("A", "105", "SG"), ("L", "1", "C2" if bad_link else "C1")], 1):
        for field, value in zip(("auth_asym_id", "auth_seq_id", "label_atom_id"), (chain, residue, atom)):
            model_dict[f"_struct_conn.ptnr{n}_{field}"] = [value]
    monkeypatch.setattr("covscreen.pose_topology.MMCIF2Dict", lambda value: model_dict if isinstance(value, str) else ccd)
    extracted = {"ligand_heavy_atoms": [{"atom_name": n, "element": "C"} for n in ("C1", "C2")],
        "ligand_heavy_bonds": [{"atom_name_1": "C1", "atom_name_2": "C2", "order": 1}],
        "covalent_partner_atom_name": "C1"}
    if bad_link:
        with pytest.raises(ValueError, match="model_input_covalent"):
            verify_pose_topology(model, extracted, protein_chain="A", ligand_chain="L", af3_residue=105)
    else:
        assert verify_pose_topology(model, extracted, protein_chain="A", ligand_chain="L", af3_residue=105)["topology_heavy_bond_count"] == 1
