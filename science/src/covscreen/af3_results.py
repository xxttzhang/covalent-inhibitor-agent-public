from __future__ import annotations

import json
import io
from pathlib import Path

import numpy as np
import yaml
from Bio.PDB import MMCIFParser
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from rdkit import Chem

from .chemistry import select_warhead_match
from .af3_adduct import transform_adduct


def adduct_electrophile_name(smiles: str, warhead_class: str) -> str:
    """Use the same post-reaction atom ordering as AF3 custom CCD preparation."""
    molecule, atom_index, _ = transform_adduct(smiles, warhead_class)
    return af3_atom_name_for_rdkit_index(molecule, atom_index)


def af3_atom_name_for_rdkit_index(mol: Chem.Mol, atom_index: int) -> str:
    """Return AF3's element-counter atom name for a SMILES-derived ligand."""
    counts: dict[str, int] = {}
    for atom in mol.GetAtoms():
        symbol = atom.GetSymbol()
        counts[symbol] = counts.get(symbol, 0) + 1
        if atom.GetIdx() == atom_index:
            return f"{symbol}{counts[symbol]}"
    raise IndexError(atom_index)


def mapped_warhead_atom_name(
    smiles: str,
    warhead_smarts: str,
    atom_map_number: int = 1,
) -> str:
    mol = Chem.MolFromSmiles(smiles)
    query = Chem.MolFromSmarts(warhead_smarts)
    if mol is None or query is None:
        raise ValueError("Invalid ligand SMILES or warhead SMARTS")
    match, _ = select_warhead_match(
        mol, query, electrophilic_map_number=atom_map_number
    )
    query_index = next(
        (
            atom.GetIdx()
            for atom in query.GetAtoms()
            if atom.GetAtomMapNum() == atom_map_number
        ),
        None,
    )
    if query_index is None:
        raise ValueError(f"SMARTS has no atom map {atom_map_number}")
    return af3_atom_name_for_rdkit_index(mol, match[query_index])


def confidence_summary(
    summary_path: str | Path,
    confidence_path: str | Path,
    protein_chain: str = "A",
    ligand_chain: str = "L",
) -> dict[str, float]:
    """Validate and extract confidence metrics, not accept a candidate pose.

    Finite mPAE is not calibrated binding affinity, and a reported clash is kept
    as evidence for the separate frozen quality policy rather than hidden here.
    """
    summary = json.loads(Path(summary_path).read_text())
    confidence = json.loads(Path(confidence_path).read_text())
    return confidence_metrics(summary, confidence, protein_chain, ligand_chain)


def confidence_metrics(summary: dict, confidence: dict, protein_chain: str, ligand_chain: str) -> dict[str, float]:
    """Compute metrics from already-read bytes so a verifier need not reopen files."""
    if not isinstance(summary, dict) or not isinstance(confidence, dict):
        raise ValueError("AF3 confidence documents must be objects")
    if not protein_chain or not ligand_chain or protein_chain == ligand_chain:
        raise ValueError("Distinct protein and ligand chains are required")
    for field in ("token_chain_ids", "atom_chain_ids"):
        values = confidence[field]
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f"AF3 {field} must be a nonempty one-dimensional chain list")
    token_chains = np.asarray(confidence["token_chain_ids"])
    pae = np.asarray(confidence["pae"], dtype=float)
    if pae.shape != (len(token_chains), len(token_chains)) or not np.isfinite(pae).all() or (pae < 0).any():
        raise ValueError("AF3 PAE must be a finite nonnegative square matrix matching all tokens")
    protein_tokens = np.flatnonzero(token_chains == protein_chain)
    ligand_tokens = np.flatnonzero(token_chains == ligand_chain)
    if not len(protein_tokens) or not len(ligand_tokens):
        raise ValueError("Protein or ligand chain missing from AF3 confidence JSON")
    protein_to_ligand = pae[np.ix_(protein_tokens, ligand_tokens)]
    ligand_to_protein = pae[np.ix_(ligand_tokens, protein_tokens)]

    atom_chains = np.asarray(confidence["atom_chain_ids"])
    atom_plddts = np.asarray(confidence["atom_plddts"], dtype=float)
    if (atom_plddts.shape != atom_chains.shape or not np.isfinite(atom_plddts).all() or
            (atom_plddts < 0).any() or (atom_plddts > 100).any()):
        raise ValueError("AF3 atom pLDDT must match atom chains and lie in [0, 100]")
    ligand_plddts = atom_plddts[atom_chains == ligand_chain]
    if not len(ligand_plddts) or protein_chain not in atom_chains:
        raise ValueError("Protein or ligand atoms missing from AF3 confidence JSON")
    chain_order = list(dict.fromkeys(token_chains.tolist()))
    protein_chain_index = chain_order.index(protein_chain)
    ligand_chain_index = chain_order.index(ligand_chain)
    summary_mpae = summary.get("chain_pair_pae_min")
    if summary_mpae is not None:
        summary_mpae = np.asarray(summary_mpae, dtype=float)
        if (summary_mpae.shape != (len(chain_order), len(chain_order)) or
                not np.isfinite(summary_mpae).all() or (summary_mpae < 0).any()):
            raise ValueError("AF3 chain-pair PAE must match chain order and be finite/nonnegative")
    for field in ("ranking_score", "iptm", "ptm", "has_clash"):
        value = summary[field]
        if (type(value) not in (int, float, bool) or not np.isfinite(value) or
                (field in ("iptm", "ptm") and not 0 <= value <= 1) or
                (field == "has_clash" and value not in (0, 1)) or
                (field != "has_clash" and isinstance(value, bool))):
            raise ValueError(f"Invalid AF3 confidence summary field: {field}")
    mpae = (
        float(summary_mpae[ligand_chain_index][protein_chain_index])
        if summary_mpae is not None
        else float(ligand_to_protein.min())
    )
    return {
        "ranking_score": float(summary["ranking_score"]),
        "iptm": float(summary["iptm"]),
        "ptm": float(summary["ptm"]),
        # COValid SI defines mPAE as chain_pair_pae_min[ligand, protein]:
        # protein-token error when aligned using ligand-token frames.
        "mpae": mpae,
        "protein_to_ligand_pae_min": float(protein_to_ligand.min()),
        "ligand_to_protein_pae_min": float(ligand_to_protein.min()),
        "protein_ligand_pae_mean": float(
            np.mean([protein_to_ligand.mean(), ligand_to_protein.mean()])
        ),
        "ligand_plddt_mean": float(ligand_plddts.mean()),
        "has_clash": float(summary["has_clash"]),
    }


def af3_sequence_residue_from_config(config: dict[str, object]) -> int:
    """Resolve the residue number used in an AF3 model CIF.

    Current target configs freeze the AF3 sequence-local residue directly as
    ``cysteine_sequence_index``.  Older configs instead describe a source
    numbering offset with ``residue_number`` and ``sequence_start_residue``.
    Prefer the explicit AF3 index and retain the legacy calculation so existing
    benchmark configs remain readable.
    """
    def positive(value):
        if type(value) is not int or value < 1:
            raise ValueError("AF3 residue mapping must use positive integer positions")
        return value
    if config.get("cysteine_sequence_index") is not None:
        return positive(config["cysteine_sequence_index"])
    if (
        config.get("residue_number") is not None
        and config.get("sequence_start_residue") is not None
    ):
        return positive(positive(config["residue_number"]) - positive(config["sequence_start_residue"]) + 1)
    raise ValueError(
        "Target config must define cysteine_sequence_index or both "
        "residue_number and sequence_start_residue"
    )


def reactive_distance(
    model_cif: str | Path,
    ligand_smiles: str,
    target_config: str | Path,
    *,
    warhead_class: str | None = None,
) -> dict[str, object]:
    """Measure the declared reactive atoms after model/atom identity checks.

    Matching names/elements and a positive distance do not establish bond
    topology, stereochemical correctness, clash freedom or physical validity.
    """
    return _reactive_atoms(Path(model_cif).read_text(), ligand_smiles,
        yaml.safe_load(Path(target_config).read_text()), warhead_class)[-1]


def _reactive_atoms(contents, ligand_smiles, config, warhead_class):
    """Parse the exact caller-read model bytes, preserving the registered mapping."""
    sequence_residue = af3_sequence_residue_from_config(config)
    structure = MMCIFParser(QUIET=True).get_structure("af3", io.StringIO(contents))
    models = list(structure.get_models())
    if len(models) != 1:
        raise ValueError("AF3 output must contain exactly one selected model")
    model = models[0]
    residue = model[config["protein_chain"]][sequence_residue]
    if residue.get_resname() != config.get("residue_name", "CYS") or residue.is_disordered():
        raise ValueError("AF3 nucleophile residue identity is wrong or ambiguous")
    nucleophile = residue[config["nucleophilic_atom"]]
    ligand_chain = model[config.get("ligand_chain", "L")]
    if ligand_chain.id == config["protein_chain"]:
        raise ValueError("AF3 protein and ligand must use distinct chains")
    ligands = list(ligand_chain.get_residues())
    if len(ligands) != 1 or ligands[0].is_disordered():
        raise ValueError("AF3 ligand chain must contain exactly one unambiguous residue")
    ligand = ligands[0]
    molecule = transform_adduct(ligand_smiles, warhead_class)[0] if warhead_class is not None else Chem.MolFromSmiles(ligand_smiles)
    if molecule is None or len(Chem.GetMolFrags(molecule)) != 1:
        raise ValueError("AF3 ligand requires one valid molecular component")
    expected_atoms = {af3_atom_name_for_rdkit_index(molecule, atom.GetIdx()): atom.GetSymbol().upper()
                      for atom in molecule.GetAtoms() if atom.GetAtomicNum() > 1}
    observed_atoms = {atom.get_name(): atom.element.upper() for atom in ligand.get_atoms() if atom.element.upper() not in ("H", "D")}
    if observed_atoms != expected_atoms:
        raise ValueError("AF3 ligand heavy-atom inventory differs from the declared molecular input")
    for atom in [nucleophile, *ligand.get_atoms()]:
        if atom.is_disordered() or atom.coord.shape != (3,) or not np.isfinite(atom.coord).all():
            raise ValueError("AF3 reactive atoms require unambiguous finite 3D coordinates")
    if config["nucleophilic_atom"] == "SG" and nucleophile.element != "S":
        raise ValueError("AF3 SG nucleophile must be sulfur")
    electrophile_name = (
        adduct_electrophile_name(ligand_smiles, warhead_class)
        if warhead_class is not None
        else mapped_warhead_atom_name(ligand_smiles, config["warhead_smarts"])
    )
    electrophile = ligand[electrophile_name]
    distance = float(electrophile - nucleophile)
    if not np.isfinite(distance) or distance <= 0:
        raise ValueError("AF3 reactive distance must be finite and positive")
    return model, nucleophile, ligand, electrophile, {
        "source_residue": int(config["residue_number"]),
        "af3_sequence_residue": sequence_residue,
        "nucleophilic_atom": config["nucleophilic_atom"],
        "electrophilic_atom": electrophile_name,
        "reactive_distance_angstrom": distance,
    }


def reactive_geometry(contents: str, ligand_smiles: str, config: dict, *, warhead_class: str) -> dict:
    """Measure a witnessed model; do not infer a covalent bond from proximity.

    Atom names/elements are checked against the input-derived adduct. This is
    not a stereochemistry, complete bond-graph, strain or affinity validator.
    Missing/wrong model connectivity is retained as a negative observation.
    """
    model, nucleophile, ligand, electrophile, result = _reactive_atoms(contents, ligand_smiles, config, warhead_class)
    target = (config["protein_chain"], str(result["af3_sequence_residue"]), config["nucleophilic_atom"])
    partner = (config.get("ligand_chain", "L"), str(ligand.id[1]), result["electrophilic_atom"])
    document = MMCIF2Dict(io.StringIO(contents))
    fields = ["_struct_conn.conn_type_id"] + [f"_struct_conn.ptnr{number}_{field}"
        for number in (1, 2) for field in ("auth_asym_id", "auth_seq_id", "label_atom_id")]
    columns = [document.get(field, []) for field in fields]
    if any(columns) and (not all(columns) or len({len(column) for column in columns}) != 1):
        raise ValueError("AF3 model connectivity table is malformed")
    links = []
    qualifiers = {f"_struct_conn.{field}": allowed for number in (1, 2) for field, allowed in (
        (f"pdbx_ptnr{number}_PDB_ins_code", {".", "?"}),
        (f"ptnr{number}_label_alt_id", {".", "?"}),
        (f"pdbx_ptnr{number}_label_alt_id", {".", "?"}),
        (f"pdbx_ptnr{number}_auth_alt_id", {".", "?"}),
        (f"ptnr{number}_symmetry", {"1_555"}))}
    for field in qualifiers:
        if field in document and len(document[field]) != len(columns[0]):
            raise ValueError("AF3 model connectivity qualifiers are malformed")
    for index, (kind, chain1, residue1, atom1, chain2, residue2, atom2) in enumerate(zip(*columns)):
        if kind != "covale":
            continue
        first, second = (chain1, residue1, atom1), (chain2, residue2, atom2)
        if target in (first, second) and any(document[field][index] not in allowed for field, allowed in qualifiers.items() if field in document):
            raise ValueError("AF3 target connectivity uses unsupported insertion, alternate or symmetry qualifiers")
        if first == target:
            links.append(second)
        if second == target:
            links.append(first)
    protein = [atom for atom in model[config["protein_chain"]].get_atoms() if atom.element.upper() not in ("H", "D")]
    atoms = [atom for atom in ligand.get_atoms() if atom.element.upper() not in ("H", "D")]
    if any(atom.is_disordered() or atom.coord.shape != (3,) or not np.isfinite(atom.coord).all() for atom in protein):
        raise ValueError("AF3 protein coordinates are missing, nonfinite or ambiguous")
    coords = np.asarray([atom.coord for atom in protein], dtype=float)
    distances = []
    for atom in atoms:
        values = np.linalg.norm(coords - np.asarray(atom.coord, dtype=float), axis=1)
        distances.extend(float(value) for other, value in zip(protein, values)
            if not (other is nucleophile and atom is electrophile))
    return {**result, "model_target_covalent_link_count": len(links),
        "model_has_exact_covalent_link": links == [partner],
        "ligand_heavy_atom_count": len(atoms), "protein_heavy_atom_count": len(protein),
        "minimum_nonbonded_heavy_atom_distance_angstrom": min(distances) if distances else None,
        "nonbonded_pair_count": len(distances),
        "distance_scope": "protein_ligand_heavy_atoms_excluding_declared_reactive_pair",
        "full_topology_and_stereochemistry_verified": False, "scientific_quality_verified": False}


def parse_af3_job(
    job_dir: str | Path,
    ligand_smiles: str,
    target_config: str | Path,
    *,
    warhead_class: str | None = None,
) -> dict[str, object]:
    job_dir = Path(job_dir)
    name = job_dir.name
    model_cif = job_dir / f"{name}_model.cif"
    summary_path = job_dir / f"{name}_summary_confidences.json"
    confidence_path = job_dir / f"{name}_confidences.json"
    required = (model_cif, summary_path, confidence_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing AF3 output files: {missing}")
    result: dict[str, object] = {
        "engine": "alphafold3",
        "job_name": name,
        "status": "SUCCESS",
        "model_cif": str(model_cif),
        "score_direction": "higher_is_better",
    }
    config = yaml.safe_load(Path(target_config).read_text())
    result.update(confidence_summary(summary_path, confidence_path,
                                     protein_chain=config["protein_chain"],
                                     ligand_chain=config.get("ligand_chain", "L")))
    result.update(reactive_distance(
        model_cif, ligand_smiles, target_config, warhead_class=warhead_class
    ))
    return result
