from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem import Descriptors, rdMolDescriptors

from covscreen.chemistry import select_warhead_match
from covscreen.af3_identity import safe_token, validate_component_id
from covscreen.af3_adduct import PATTERNS, transform_adduct
from covscreen.af3_target import replace_design_ligand, validate_target


WARHEAD_SMARTS = "[C:1]=[C:2][C:3](=[O:4])[N:5]"


def atom_name(molecule: Chem.Mol, atom_index: int) -> str:
    counts: dict[str, int] = {}
    for atom in molecule.GetAtoms():
        symbol = atom.GetSymbol()
        counts[symbol] = counts.get(symbol, 0) + 1
        name = f"{symbol}{counts[symbol]}"
        atom.SetProp("atom_name", name)
        if atom.GetIdx() == atom_index:
            selected = name
    return selected


def make_adduct(
    smiles: str,
    random_seed: int,
    warhead_class: str | None = None,
) -> tuple[Chem.Mol, str, int]:
    if warhead_class is not None:
        molecule, beta_index, evidence = transform_adduct(smiles, warhead_class)
        beta_name = atom_name(molecule, beta_index)
        molecule = Chem.AddHs(molecule)
        params = AllChem.ETKDGv3()
        params.randomSeed = random_seed
        if AllChem.EmbedMolecule(molecule, params) != 0:
            raise ValueError("ETKDGv3 embedding failed")
        optimize = AllChem.MMFFOptimizeMolecule if AllChem.MMFFHasAllMoleculeParams(molecule) else AllChem.UFFOptimizeMolecule
        if optimize(molecule) < 0:
            raise ValueError("Ligand force-field optimization failed")
        return molecule, beta_name, evidence["warhead_match_count"]
    molecule = Chem.MolFromSmiles(smiles)
    query = Chem.MolFromSmarts(WARHEAD_SMARTS)
    if molecule is None or query is None:
        raise ValueError("Invalid SMILES or warhead SMARTS")
    match, match_count = select_warhead_match(molecule, query)
    query_map = {
        atom.GetAtomMapNum(): atom.GetIdx() for atom in query.GetAtoms()
    }
    beta_index = match[query_map[1]]
    alpha_index = match[query_map[2]]
    editable = Chem.RWMol(molecule)
    bond = editable.GetBondBetweenAtoms(beta_index, alpha_index)
    if bond is None or bond.GetBondType() != Chem.BondType.DOUBLE:
        raise ValueError("Acrylamide Cbeta-Calpha double bond not found")
    bond.SetBondType(Chem.BondType.SINGLE)
    molecule = editable.GetMol()
    Chem.SanitizeMol(molecule)
    beta_name = atom_name(molecule, beta_index)
    molecule = Chem.AddHs(molecule)
    params = AllChem.ETKDGv3()
    params.randomSeed = random_seed
    if AllChem.EmbedMolecule(molecule, params) != 0:
        raise ValueError("ETKDGv3 embedding failed")
    if AllChem.MMFFHasAllMoleculeParams(molecule):
        optimization_status = AllChem.MMFFOptimizeMolecule(molecule)
    else:
        optimization_status = AllChem.UFFOptimizeMolecule(molecule)
    if optimization_status < 0:
        raise ValueError("Ligand force-field optimization failed")
    return molecule, beta_name, match_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-data-json", type=Path, required=True)
    ap.add_argument("--expected-source-data-sha256")
    ap.add_argument("--ligand-smiles", required=True)
    ap.add_argument("--warhead-class", choices=sorted(PATTERNS))
    ap.add_argument("--compound-id", required=True)
    ap.add_argument("--protomer-id", required=True)
    ap.add_argument("--job-name")
    ap.add_argument("--component-id", default="LIG")
    ap.add_argument("--protein-chain", default="A")
    ap.add_argument("--ligand-chain", default="L")
    ap.add_argument("--cysteine-sequence-index", type=int, default=88)
    ap.add_argument("--target-id", default="BTK")
    ap.add_argument("--source-residue-number", type=int, default=481)
    ap.add_argument("--nucleophilic-atom", default="SG")
    ap.add_argument("--random-seed", type=int, default=20260725)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()

    source_bytes = args.source_data_json.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if args.expected_source_data_sha256 is not None and source_sha != args.expected_source_data_sha256:
        raise ValueError("AF3 source seed differs from the frozen preparation")
    data = json.loads(source_bytes)
    validate_target(data, args.protein_chain, args.cysteine_sequence_index,
                    args.ligand_chain, args.nucleophilic_atom)
    # Validate the seed before importing the scientific runtime or embedding.
    from alphafold3.data.tools import rdkit_utils

    molecule, beta_name, warhead_match_count = make_adduct(
        args.ligand_smiles, args.random_seed, args.warhead_class
    )
    component_id = validate_component_id(args.component_id)
    job_name = args.job_name or (
        f"{safe_token(args.target_id, max_length=20)}_"
        f"{safe_token(args.compound_id, max_length=40)}_protomer_"
        f"{safe_token(args.protomer_id, max_length=20)}_covalent"
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,127}", job_name):
        raise ValueError("job_name must be a safe 6-128 character identifier")
    ccd_dict = rdkit_utils.mol_to_ccd_cif(
        molecule,
        component_id=component_id,
        pdbx_smiles=Chem.MolToSmiles(
            Chem.RemoveHs(molecule), isomericSmiles=True
        ),
        include_hydrogens=True,
    )
    ccd_dict = ccd_dict.copy_and_update(
        {
            "_chem_comp.name": [component_id],
            "_chem_comp.type": ["non-polymer"],
            "_chem_comp.formula": [
                rdMolDescriptors.CalcMolFormula(molecule)
            ],
            "_chem_comp.formula_weight": [
                f"{Descriptors.MolWt(molecule):.3f}"
            ],
            "_chem_comp.mon_nstd_parent_comp_id": ["?"],
            "_chem_comp.pdbx_synonyms": ["?"],
        }
    )
    ccd = ccd_dict.to_string()

    data, seed_check = replace_design_ligand(data, args.protein_chain,
        args.cysteine_sequence_index, args.ligand_chain, component_id, beta_name,
        args.nucleophilic_atom)
    source_cofactors = [entity for entity in data["sequences"]
        if "protein" not in entity and entity.get("ligand", {}).get("id") != args.ligand_chain]
    data["name"] = job_name
    data["modelSeeds"] = [args.random_seed]
    data["userCCD"] = ccd

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{data['name']}.json"
    ccd_path = args.output_dir / f"{component_id}.cif"
    manifest_path = args.output_dir / f"{data['name']}.manifest.json"
    json_path.write_text(json.dumps(data, indent=2) + "\n")
    ccd_path.write_text(ccd)
    manifest = {
        "af3_target_seed_check": {**seed_check, "source_data_sha256": source_sha},
        "job_name": data["name"],
        "input_json": str(json_path.resolve()),
        "user_ccd": str(ccd_path.resolve()),
        "compound_id": args.compound_id,
        "protomer_id": args.protomer_id,
        "model_seed": args.random_seed,
        "num_diffusion_samples": 5,
        "protein_chain": args.protein_chain,
        "cysteine_sequence_index": args.cysteine_sequence_index,
        "source_residue_number": args.source_residue_number,
        "ligand_chain": args.ligand_chain,
        "component_id": component_id,
        "electrophilic_atom": beta_name,
        "nucleophilic_atom": args.nucleophilic_atom,
        "bonded_atom_pairs": data["bondedAtomPairs"],
        "warhead_transformation": "acrylamide Cbeta=Calpha reduced to single bond",
        "warhead_match_count": warhead_match_count,
        "warhead_selection_rule": (
            "least_substituted_electrophilic_atom_then_rdkit_atom_index"
        ),
        "protein_msa_templates_reused_from": str(
            args.source_data_json.resolve()
        ),
        "retained_source_cofactor_count": len(source_cofactors),
        "retained_source_cofactor_entities": source_cofactors,
    }
    if args.warhead_class is not None:
        _, _, reaction_evidence = transform_adduct(args.ligand_smiles, args.warhead_class)
        manifest.update(reaction_evidence)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
