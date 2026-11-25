"""AF3 chain/site consistency and frozen seed checks.

These checks bind a seed to its configuration. They do not establish UniProt
identity, canonical numbering, variant suitability or resource approval.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import yaml


TARGET_FIELDS = ("target_id", "protein_chain", "cysteine_sequence_index",
                 "residue_number", "nucleophilic_atom")


class AF3RuntimeUnavailable(ValueError):
    """A concrete runtime asset is missing; this is not a molecular verdict."""


def runtime_paths(configuration):
    paths = {}
    unavailable = []
    for key in ('runner', 'python', 'model_dir', 'database_dir'):
        value = configuration.get(key)
        if not isinstance(value, str) or not value.strip():
            unavailable.append(key + ': not configured')
            continue
        path = Path(value).expanduser()
        exists = path.is_file() if key in {'runner', 'python'} else path.is_dir()
        if not exists:
            unavailable.append(key + ': ' + str(path))
        paths[key] = path
    if unavailable:
        raise AF3RuntimeUnavailable('configured AlphaFold 3 assets are unavailable: ' + '; '.join(unavailable))
    return paths


def chain_inventory(data: dict) -> dict[str, tuple[str, dict]]:
    sequences = data.get("sequences") if isinstance(data, dict) else None
    if not isinstance(sequences, list) or not sequences:
        raise ValueError("AF3 seed requires explicit molecular entities")
    chains = {}
    for entity in sequences:
        if not isinstance(entity, dict) or len(entity) != 1:
            raise ValueError("AF3 seed has an ambiguous molecular entity")
        kind, payload = next(iter(entity.items()))
        if kind not in {"protein", "ligand", "rna", "dna"} or not isinstance(payload, dict):
            raise ValueError("AF3 seed has an unsupported molecular entity")
        identifiers = payload.get("id")
        identifiers = [identifiers] if isinstance(identifiers, str) else identifiers
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError("AF3 seed requires explicit chain IDs")
        for identifier in identifiers:
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Z]+", identifier):
                raise ValueError("AF3 chain IDs must be uppercase letters")
            if identifier in chains:
                raise ValueError("AF3 seed has duplicate chain IDs")
            chains[identifier] = kind, payload
    return chains


def validate_target(data: dict, protein_chain: str, sequence_index: int,
                    ligand_chain: str = "L", nucleophilic_atom: str = "SG",
                    expected_sequence_sha256: str | None = None) -> dict:
    chains = chain_inventory(data)
    if protein_chain not in chains or chains[protein_chain][0] != "protein":
        raise ValueError("AF3 requested protein chain is absent from the seed")
    if not isinstance(ligand_chain, str) or not re.fullmatch(r"[A-Z]+", ligand_chain):
        raise ValueError("AF3 design ligand requires an explicit chain ID")
    if ligand_chain in chains and chains[ligand_chain][0] != "ligand":
        raise ValueError("AF3 design ligand chain collides with a polymer chain")
    protein = chains[protein_chain][1]
    sequence = protein.get("sequence")
    if not isinstance(sequence, str) or not re.fullmatch(r"[A-Z]+", sequence):
        raise ValueError("AF3 target requires an explicit uppercase protein sequence")
    if type(sequence_index) is not int or not 1 <= sequence_index <= len(sequence):
        raise ValueError("AF3 cysteine sequence index must be a 1-based integer within the sequence")
    if sequence[sequence_index - 1] != "C" or nucleophilic_atom != "SG":
        raise ValueError("AF3 covalent target must be an explicit Cys SG at the selected sequence index")
    modifications = protein.get("modifications", [])
    if not isinstance(modifications, list):
        raise ValueError("AF3 protein modifications must be explicit records")
    for modification in modifications:
        position = modification.get("ptmPosition") if isinstance(modification, dict) else None
        if type(position) is not int or not 1 <= position <= len(sequence):
            raise ValueError("AF3 protein modification has an invalid position")
        if position == sequence_index:
            raise ValueError("AF3 target Cys is modified; an explicit chemical-state adapter is required")
    checksum = hashlib.sha256(sequence.encode()).hexdigest()
    if expected_sequence_sha256 is not None:
        if (not isinstance(expected_sequence_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sequence_sha256)
                or checksum != expected_sequence_sha256):
            raise ValueError("AF3 protein sequence differs from the configured sequence fingerprint")
    return {"schema": "af3-target-seed-check-v1", "protein_chain": protein_chain,
            "protein_sequence_sha256": checksum, "protein_sequence_length": len(sequence),
            "cysteine_sequence_index": sequence_index, "nucleophilic_atom": nucleophilic_atom,
            "ligand_chain": ligand_chain, "seed_site_consistent": True,
            "configured_sequence_fingerprint_verified": expected_sequence_sha256 is not None,
            "study_target_identity_verified": False, "canonical_mapping_verified": False,
            "grants_resource_approval": False}


def read_target_seed(source: Path, config: Path, *, expected_source_sha256=None,
                     expected_config_sha256=None, overrides: dict | None = None) -> dict:
    contents, config_contents = source.read_bytes(), config.read_bytes()
    source_sha = hashlib.sha256(contents).hexdigest()
    config_sha = hashlib.sha256(config_contents).hexdigest()
    expected = expected_source_sha256, expected_config_sha256
    if any(value is not None for value in expected):
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
               for value in expected):
            raise ValueError("AF3 frozen seed requires both source and target configuration SHA256")
        if (source_sha, config_sha) != expected:
            raise ValueError("AF3 source seed or target configuration differs from the frozen plan")
    target = yaml.safe_load(config_contents)
    if not isinstance(target, dict) or not set(TARGET_FIELDS).issubset(target):
        raise ValueError("AF3 target config is missing fields")
    if not isinstance(target["target_id"], str) or not target["target_id"].strip():
        raise ValueError("AF3 target ID must be explicit")
    # This runner currently supports integer author residue IDs. Do not truncate
    # floats or silently strip insertion codes; those need a separate adapter.
    if type(target["residue_number"]) is not int:
        raise ValueError("AF3 source residue number must be an explicit integer")
    if target.get("residue_insertion_code", ""):
        raise ValueError("AF3 source insertion-code support requires an explicit mapping adapter")
    target = {**target, "ligand_chain": target.get("ligand_chain", "L")}
    if overrides is not None:
        for field in (*TARGET_FIELDS, "ligand_chain"):
            if field not in overrides or overrides[field] != target[field]:
                raise ValueError(f"AF3 command differs from target configuration: {field}")
    result = validate_target(json.loads(contents), target["protein_chain"],
        target["cysteine_sequence_index"], target["ligand_chain"], target["nucleophilic_atom"],
        target.get("protein_sequence_sha256"))
    return {**result, "source_data_sha256": source_sha, "target_config_sha256": config_sha,
            "target_id": target["target_id"], "source_residue_number": target["residue_number"]}


def replace_design_ligand(data: dict, protein_chain: str, sequence_index: int,
                          ligand_chain: str, component_id: str, electrophilic_atom: str,
                          nucleophilic_atom: str = "SG") -> tuple[dict, dict]:
    """Keep explicit source polymers/cofactors; replace only the named ligand."""
    evidence = validate_target(data, protein_chain, sequence_index, ligand_chain, nucleophilic_atom)
    result = deepcopy(data)
    sequences = []
    for entity in result["sequences"]:
        kind, payload = next(iter(entity.items()))
        identifiers = payload["id"]
        identifiers = [identifiers] if isinstance(identifiers, str) else identifiers
        if ligand_chain not in identifiers:
            sequences.append(entity)
            continue
        remaining = [identifier for identifier in identifiers if identifier != ligand_chain]
        if remaining:
            payload["id"] = remaining
            sequences.append(entity)
    sequences.append({"ligand": {"id": ligand_chain, "ccdCodes": [component_id]}})
    bonds = result.get("bondedAtomPairs", [])
    if not isinstance(bonds, list):
        raise ValueError("AF3 source bonds must be explicit atom pairs")
    retained_bonds = []
    site = [protein_chain, sequence_index, nucleophilic_atom]
    for bond in bonds:
        if (not isinstance(bond, list) or len(bond) != 2
                or any(not isinstance(atom, list) or len(atom) != 3 for atom in bond)):
            raise ValueError("AF3 source has an invalid bonded atom pair")
        if any(atom[0] == ligand_chain for atom in bond):
            continue  # Remove the replaced design ligand's old connections.
        if site in bond:
            raise ValueError("AF3 target SG is already bonded to a retained source entity")
        retained_bonds.append(bond)
    result["sequences"] = sequences
    result["bondedAtomPairs"] = [*retained_bonds, [site, [ligand_chain, 1, electrophilic_atom]]]
    return result, evidence
