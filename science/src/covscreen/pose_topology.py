"""Validate a fixed-name heavy-atom graph before comparing covalent poses.

No graph remapping or symmetry minimization is performed. Different aromatic
representations are left unresolved rather than silently treated as equivalent.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from Bio.PDB.MMCIF2Dict import MMCIF2Dict

from .jobs import sha256_file

ORDERS = {"SING": 1.0, "DOUB": 2.0, "TRIP": 3.0, "AROM": 1.5}


def compare_named_graphs(af3_atoms, af3_bonds, covdock_atoms, covdock_bonds,
                         af3_partner: str, covdock_partner: str) -> dict:
    def atoms(rows):
        result = {}
        for name, element in rows:
            if not name or name in result or not element:
                raise ValueError("missing_or_duplicate_topology_atom")
            result[name] = element.upper()
        if not result:
            raise ValueError("empty_heavy_atom_graph")
        return result

    left, right = atoms(af3_atoms), atoms(covdock_atoms)
    if left != right:
        raise ValueError("topology_atom_identity_mismatch")

    def bonds(rows):
        result = {}
        for first, second, order in rows:
            key = tuple(sorted((first, second)))
            if first == second or first not in left or second not in left or key in result:
                raise ValueError("invalid_or_duplicate_topology_bond")
            value = ORDERS.get(order) if isinstance(order, str) else float(order)
            if value not in {1.0, 1.5, 2.0, 3.0}:
                raise ValueError("unsupported_topology_bond_order")
            result[key] = value
        return result

    first, second = bonds(af3_bonds), bonds(covdock_bonds)
    if first != second:
        raise ValueError("heavy_atom_bond_graph_mismatch")
    if af3_partner != covdock_partner or af3_partner not in left:
        raise ValueError("covalent_partner_atom_mismatch")
    return {"pose_topology_status": "VERIFIED_FIXED_NAME_GRAPH",
            "topology_heavy_atom_count": len(left), "topology_heavy_bond_count": len(first),
            "topology_covalent_partner": af3_partner,
            "ligand_mapping_policy": "fixed_atom_names_elements_and_bond_orders_not_symmetry_corrected"}


def verify_pose_topology(model_cif: Path, extracted: dict, *, protein_chain: str,
                         ligand_chain: str, af3_residue: int) -> dict:
    if not model_cif.name.endswith("_model.cif"):
        raise ValueError("expected_AF3_model_filename")
    data_path = model_cif.with_name(model_cif.name[:-10] + "_data.json")
    data = json.loads(data_path.read_text())
    if data.get("name", "").casefold() != model_cif.name[:-10].casefold():
        raise ValueError("AF3_model_data_identity_mismatch")
    ccd = MMCIF2Dict(io.StringIO(data["userCCD"]))
    names, elements = ccd["_chem_comp_atom.atom_id"], ccd["_chem_comp_atom.type_symbol"]
    if len(names) != len(elements) or len(names) != len(set(names)):
        raise ValueError("malformed_CCD_atom_table")
    heavy = {name for name, element in zip(names, elements) if element.upper() not in {"H", "D"}}
    bond_columns = [ccd[f"_chem_comp_bond.{field}"] for field in ("atom_id_1", "atom_id_2", "value_order")]
    if len({len(column) for column in bond_columns}) != 1:
        raise ValueError("malformed_CCD_bond_table")
    af3_bonds = [(a, b, order) for a, b, order in zip(*bond_columns) if a in heavy and b in heavy]
    target = [protein_chain, af3_residue, "SG"]
    partners = []
    for pair in data.get("bondedAtomPairs", []):
        if len(pair) != 2:
            raise ValueError("malformed_AF3_bonded_atom_pair")
        if pair[0] == target:
            partners.append(pair[1])
        elif pair[1] == target:
            partners.append(pair[0])
    if len(partners) != 1 or partners[0][:2] != [ligand_chain, 1]:
        raise ValueError("AF3_requested_covalent_link_not_unique")
    model = MMCIF2Dict(str(model_cif))
    fields = ["_struct_conn.conn_type_id"] + [
        f"_struct_conn.ptnr{n}_{field}" for n in (1, 2)
        for field in ("auth_asym_id", "auth_seq_id", "label_atom_id")]
    columns = [model.get(field, []) for field in fields]
    if not columns[0] or len({len(column) for column in columns}) != 1:
        raise ValueError("AF3_model_covalent_connectivity_missing")
    model_links = []
    for kind, chain1, res1, atom1, chain2, res2, atom2 in zip(*columns):
        if kind != "covale":
            continue
        left, right = [chain1, int(res1), atom1], [chain2, int(res2), atom2]
        if left == target:
            model_links.append(right)
        elif right == target:
            model_links.append(left)
    if model_links != partners:
        raise ValueError("AF3_model_input_covalent_connectivity_mismatch")
    result = compare_named_graphs(
        [(n, e) for n, e in zip(names, elements) if n in heavy], af3_bonds,
        [(atom["atom_name"], atom["element"]) for atom in extracted["ligand_heavy_atoms"]],
        [(bond["atom_name_1"], bond["atom_name_2"], bond["order"]) for bond in extracted["ligand_heavy_bonds"]],
        partners[0][2], extracted["covalent_partner_atom_name"])
    result.update(af3_topology_source=str(data_path), af3_topology_source_sha256=sha256_file(data_path))
    return result
