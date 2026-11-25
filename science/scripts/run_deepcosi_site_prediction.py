#!/usr/bin/env python3
"""Run hash-pinned DeepCoSI on one verified receptor-input artifact."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from covscreen.covalent_site_discovery import PREDICTION_SCHEMA
from covscreen.deepcosi_identity import verified_result_keys, result_number
from covscreen.receptor_input import load_receptor_preparation_input


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atom_choice(lines: list[str], chain: str, model: int) -> list[str]:
    current_model = 1
    chosen: dict[tuple[str, str, str], tuple[float, int, str]] = {}
    order = 0
    for line in lines:
        if line.startswith("MODEL"):
            try:
                current_model = int(line[10:14])
            except ValueError:
                current_model += 1
            continue
        if not line.startswith("ATOM") or current_model != model or line[21].strip() != chain:
            continue
        residue = line[22:27]
        atom = line[12:16]
        altloc = line[16]
        try:
            occupancy = float(line[54:60])
        except ValueError:
            occupancy = 0.0
        preference = 2 if altloc == " " else 1 if altloc == "A" else 0
        key = (residue, atom, line[17:20])
        previous = chosen.get(key)
        value = (occupancy, preference, line)
        if previous is None or value[:2] > previous[:2]:
            chosen[key] = value
        order += 1
    return [value[2] for _, value in sorted(chosen.items(), key=lambda item: int(item[1][2][6:11]))]


def _normalize_structure(source: Path, destination: Path, chain: str, model: int) -> dict[int, dict]:
    lines = _atom_choice(source.read_text(errors="replace").splitlines(), chain, model)
    if not lines:
        raise ValueError("Selected model/author chain has no protein ATOM records")
    residue_numbers: dict[tuple[str, str, str], int] = {}
    mapping: dict[int, dict] = {}
    output = []
    for line in lines:
        original = (line[22:26].strip(), line[26].strip(), line[17:20].strip())
        if original not in residue_numbers:
            normalized = len(residue_numbers) + 1
            residue_numbers[original] = normalized
            mapping[normalized] = {"author_residue_number": original[0],
                "insertion_code": original[1] or None, "residue_name": original[2],
                "has_sg": False}
        normalized = residue_numbers[original]
        if line[12:16].strip() == "SG":
            mapping[normalized]["has_sg"] = True
        rewritten = line[:16] + " " + line[17:21] + "A" + f"{normalized:4d}" + " " + line[27:]
        output.append(rewritten)
    destination.write_text("\n".join(output) + "\nTER\nEND\n")
    return mapping


def _canonical_map(entity: dict, instance: dict, accession: str, *, cysteine_only: bool = True) -> dict[str, int]:
    identifiers = instance["rcsb_polymer_entity_instance_container_identifiers"]
    entity_ids = entity["rcsb_polymer_entity_container_identifiers"]
    if any(entity_ids.get(k) != identifiers.get(k) for k in ("entry_id", "entity_id")):
        raise ValueError("Entity and chain identity mismatch")
    if accession not in entity_ids.get("uniprot_ids", []):
        raise ValueError("Target accession absent from entity")
    author = identifiers["auth_to_entity_poly_seq_mapping"]
    sequence = "".join(entity["entity_poly"]["pdbx_seq_one_letter_code_can"].split())
    if len(author) != len(sequence):
        raise ValueError("Incomplete author numbering map")
    regions = [region for alignment in entity.get("rcsb_polymer_entity_align", [])
               if alignment.get("reference_database_name") == "UniProt"
               and alignment.get("reference_database_accession") == accession
               for region in alignment.get("aligned_regions", [])]
    result: dict[str, int] = {}
    duplicates: set[str] = set()
    for entity_position, author_id in enumerate(author, 1):
        if not author_id or (cysteine_only and sequence[entity_position - 1] != "C"):
            continue
        positions = {int(region["ref_beg_seq_id"]) + entity_position - int(region["entity_beg_seq_id"])
                     for region in regions if int(region["entity_beg_seq_id"]) <= entity_position
                     < int(region["entity_beg_seq_id"]) + int(region["length"])}
        if len(positions) > 1:
            raise ValueError("Ambiguous canonical cysteine alignment")
        if not positions:
            continue
        canonical = positions.pop()
        author_id = str(author_id)
        if author_id in result and result[author_id] != canonical:
            duplicates.add(author_id)
        result[author_id] = canonical
    for value in duplicates:
        result.pop(value, None)
    return result


def collect_candidates(raw: Path, normalized: Path, residue_map: dict, canonical: dict,
                       validation: dict, chain: str, output: Path) -> list[dict]:
    result_path = raw / "build/prediction_result.csv"
    rows = list(csv.DictReader(result_path.open()))
    keys = verified_result_keys(raw, normalized, residue_map, validation)
    if validation.get("result_sha256") != sha(result_path):
        raise RuntimeError("DeepCoSI validation result checksum mismatch")
    scored: dict[int, dict] = {}
    for row in rows:
        normalized_number = result_number(row.get("key", ""), keys)
        source = residue_map.get(normalized_number)
        if not source or source["residue_name"] != "CYS":
            raise RuntimeError("DeepCoSI result does not map to one normalized CYS")
        if normalized_number in scored:
            raise RuntimeError("DeepCoSI returned duplicate rows for one normalized CYS")
        score = float(row["probability"])
        if not math.isfinite(score):
            raise RuntimeError("DeepCoSI produced a non-finite score")
        author_id = source["author_residue_number"] + (source["insertion_code"] or "")
        pocket = raw / "build/prediction_pocket" / (row["key"] + "_pocket.pdb")
        pocket_exists = pocket.is_file()
        blockers = []
        if not source["has_sg"]:
            blockers.append("cysteine_sg_missing")
        # RCSB's auth-to-entity mapping does not expose insertion codes. Treat
        # inserted residues as unmapped rather than silently assigning the base
        # author residue's canonical position.
        canonical_position = (None if source["insertion_code"] else
                              canonical.get(source["author_residue_number"]))
        if source["insertion_code"]:
            blockers.append("author_insertion_code_requires_manual_mapping")
        if canonical_position is None:
            blockers.append("canonical_mapping_unavailable")
        if not pocket_exists:
            blockers.append("deepcosi_pocket_missing")
        scored[normalized_number] = {"raw_key": row["key"], "normalized_chain": "A",
            "normalized_residue_number": normalized_number, "author_chain": chain,
            "author_residue_id": author_id, "canonical_position": canonical_position,
            "residue_name": "CYS", "reactive_atom": "SG", "model_score": score,
            "model_score_label": "DeepCoSI upstream probability field plus documented 0.2 correction; uncalibrated",
            "pocket_sha256": sha(pocket) if pocket_exists else None,
            "pocket_path": str(pocket.relative_to(output)) if pocket_exists else None,
            "pocket_atom_count": sum(1 for line in pocket.read_text(errors="replace").splitlines()
                                     if line.startswith(("ATOM", "HETATM"))) if pocket_exists else None,
            "prediction_status": "SCORED" if pocket_exists else "FAILED",
            "failure_reason": None if pocket_exists else "DeepCoSI pocket output is missing",
            "blockers": blockers}
    candidates = []
    for normalized_number, source in residue_map.items():
        if source["residue_name"] != "CYS":
            continue
        candidate = scored.get(normalized_number)
        if candidate is not None:
            candidates.append(candidate)
            continue
        author_id = source["author_residue_number"] + (source["insertion_code"] or "")
        canonical_position = (None if source["insertion_code"] else
                              canonical.get(source["author_residue_number"]))
        blockers = ["deepcosi_score_missing"]
        if not source["has_sg"]:
            blockers.append("cysteine_sg_missing")
        if source["insertion_code"]:
            blockers.append("author_insertion_code_requires_manual_mapping")
        if canonical_position is None:
            blockers.append("canonical_mapping_unavailable")
        candidates.append({"raw_key": None, "normalized_chain": "A",
            "normalized_residue_number": normalized_number, "author_chain": chain,
            "author_residue_id": author_id, "canonical_position": canonical_position,
            "residue_name": "CYS", "reactive_atom": "SG", "model_score": None,
            "model_score_label": "DeepCoSI did not return a score",
            "pocket_sha256": None, "pocket_path": None, "pocket_atom_count": None,
            "prediction_status": "FAILED", "failure_reason": "No DeepCoSI result row",
            "blockers": blockers})
    candidates.sort(key=lambda row: (row["model_score"] is None,
        -float(row["model_score"] or 0), row["author_residue_id"]))
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--chimera-bin", type=Path, required=True)
    parser.add_argument("--entry-sha256", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    root = args.root.resolve()
    loaded = load_receptor_preparation_input(root, args.artifact_id, args.manifest_sha256)
    acquisition = loaded["acquisition"]
    mapping, = acquisition["acquisition"]["mappings"]
    chain = mapping["auth_asym_id"]
    model = acquisition["selection"].get("model") or 1
    structure = loaded["acquisition_directory"] / "structure.pdb"
    entity_path = loaded["acquisition_directory"] / f"entity_{mapping['entity_id']}.json"
    chain_path = loaded["acquisition_directory"] / f"chain_{mapping['label_asym_id']}.json"
    # The runner itself validates its configured repository. The explicit entry/model
    # hashes are passed through and checked again against its validation receipt.
    args.output.mkdir(parents=True, exist_ok=False)
    normalized = args.output / "normalized-selected-chain.pdb"
    residue_map = _normalize_structure(structure, normalized, chain, model)
    mapping_path = args.output / "residue-number-mapping.json"
    mapping_path.write_text(json.dumps({"schema": "deepcosi-residue-map-v1",
        "source_sha256": sha(structure), "normalized_sha256": sha(normalized),
        "author_chain": chain, "model": model, "normalized_chain": "A",
        "residues": residue_map}, indent=2) + "\n")
    raw = args.output / "raw-deepcosi"
    env = os.environ.copy()
    env.update(DEEPCOSI_PYTHON=args.python, DEEPCOSI_CHIMERA_BIN=str(args.chimera_bin))
    process = subprocess.run([sys.executable, str(args.runner), str(normalized), str(raw)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=args.timeout, check=False)
    (args.output / "deepcosi-wrapper.log").write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"DeepCoSI wrapper failed with exit code {process.returncode}")
    validation = json.loads((raw / "validation.json").read_text())
    if (validation.get("status") != "passed"
            or validation.get("entry_sha256") != args.entry_sha256
            or validation.get("model_sha256") != args.model_sha256):
        raise RuntimeError("DeepCoSI runtime or hashed model differs from registration")
    canonical = _canonical_map(json.loads(entity_path.read_text()), json.loads(chain_path.read_text()), mapping["uniprot"])
    candidates = collect_candidates(raw, normalized, residue_map, canonical, validation, chain, args.output)
    result_path = raw / "build/prediction_result.csv"
    target_candidate, = loaded["identity"]["identity"]["candidates"]
    record = {"schema": PREDICTION_SCHEMA, "status": "COMPLETED",
        "target": {"gene": loaded["identity"]["plan"]["gene"],
            "taxon": loaded["identity"]["plan"]["taxon"],
            "uniprot": target_candidate["uniprot_id"]},
        "structure": {"entry": acquisition["acquisition"]["plan"]["entry"],
            "model": model, "author_chain": chain, "input_sha256": sha(structure),
            "normalized_input_sha256": sha(normalized),
            "residue_mapping_path": mapping_path.name, "residue_mapping_sha256": sha(mapping_path)},
        "parents": {"receptor_input_artifact_id": args.artifact_id,
            "target_acquisition_manifest_sha256": args.manifest_sha256},
        "runtime": {"entry_sha256": validation["entry_sha256"],
            "model_sha256": validation["model_sha256"],
            "python": validation["python"], "chimera_bin": validation["chimera_bin"]},
        "candidate_count": len(candidates),
        "scored_candidate_count": sum(row["prediction_status"] == "SCORED" for row in candidates),
        "failure_count": sum(row["prediction_status"] == "FAILED" for row in candidates),
        "candidates": candidates,
        "raw_result_sha256": sha(result_path),
        "limitations": ["DeepCoSI evaluates Cys only.",
            "The upstream score is uncalibrated and includes its documented 0.2 correction.",
            "Prediction alone does not establish covalent reactivity, activity or selectivity."],
        "campaign_ready": False, "calculation_submitted": True}
    (args.output / "site-prediction.json").write_text(json.dumps(record, indent=2,
        ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
