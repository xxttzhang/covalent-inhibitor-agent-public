#!/usr/bin/env python3
"""Build and cryptographically seal the ERBB2 Cys805 lock-box pilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import random
import re
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
from rdkit import Chem, DataStructs
from rdkit.Chem import Crippen, Descriptors, Lipinski, inchi, rdMolDescriptors
from rdkit.Chem.Scaffolds import MurckoScaffold


ACRYLAMIDE = Chem.MolFromSmarts("C=CC(=O)N")
ERBB_FAMILY = {"EGFR", "ERBB2", "ERBB4"}


@dataclass(frozen=True)
class Properties:
    molecular_weight: float
    clogp: float
    hbond_donors: int
    hbond_acceptors: int
    rotatable_bonds: int
    formal_charge: int


@dataclass
class Active:
    identity: str
    smiles: str
    inhibitor_id: str
    best_micromolar: float
    scaffold: str
    properties: Properties
    source_rows: list[int]


@dataclass(frozen=True)
class Decoy:
    identity: str
    smiles: str
    source: str
    compound_id: str
    properties: Properties
    maximum_active_tanimoto: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_molecule(smiles: str) -> tuple[Chem.Mol, str] | None:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return None
    fragments = Chem.GetMolFrags(molecule, asMols=True, sanitizeFrags=True)
    molecule = max(fragments, key=lambda fragment: fragment.GetNumHeavyAtoms())
    return molecule, Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)


def molecule_properties(molecule: Chem.Mol) -> Properties:
    return Properties(
        molecular_weight=float(Descriptors.MolWt(molecule)),
        clogp=float(Crippen.MolLogP(molecule)),
        hbond_donors=int(Lipinski.NumHDonors(molecule)),
        hbond_acceptors=int(Lipinski.NumHAcceptors(molecule)),
        rotatable_bonds=int(Lipinski.NumRotatableBonds(molecule)),
        formal_charge=int(Chem.GetFormalCharge(molecule)),
    )


def fingerprint(molecule: Chem.Mol):
    return rdMolDescriptors.GetMorganFingerprintAsBitVect(molecule, 2, nBits=2048)


def molecule_identity(molecule: Chem.Mol, canonical_smiles: str) -> str:
    """Return a stable identity without depending on a populated source field."""
    try:
        key = inchi.MolToInchiKey(molecule)
    except Exception:
        key = ""
    return key or hashlib.sha256(canonical_smiles.encode("utf-8")).hexdigest()


def scaffold_smiles(molecule: Chem.Mol) -> str:
    scaffold = MurckoScaffold.GetScaffoldForMol(molecule)
    return Chem.MolToSmiles(scaffold, canonical=True) if scaffold.GetNumAtoms() else ""


def concentration_micromolar(value: str, unit: str, relation: str) -> float | None:
    if ">" in relation:
        return None
    try:
        number = float(re.sub(r"[^0-9.eE+-]", "", value))
    except ValueError:
        return None
    normalized = (
        unit.strip().lower().replace("μ", "u").replace("µ", "u").replace("&micro;", "u")
    )
    factor = {"pm": 1e-6, "nm": 1e-3, "um": 1.0, "mm": 1e3, "m": 1e6}.get(normalized)
    return None if factor is None else number * factor


def collect_known_erbb_keys(covindb: Path) -> set[str]:
    keys: set[str] = set()
    with covindb.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Target_Gene", "").strip().upper() not in ERBB_FAMILY:
                continue
            identity = row.get("InChI_Key", "").strip()
            if identity:
                keys.add(identity)
    return keys


def collect_erbb2_actives(covindb: Path, threshold: float) -> list[Active]:
    evidence: dict[str, dict[str, object]] = {}
    with covindb.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for source_row, row in enumerate(csv.DictReader(handle), start=2):
            if row.get("Target_Gene", "").strip().upper() != "ERBB2":
                continue
            if row.get("Site", "").strip().upper() != "CYS-805":
                continue
            if "MICHAEL" not in f'{row.get("Reaction_type", "")} {row.get("War_head", "")}'.upper():
                continue
            parsed = canonical_molecule(row.get("SMILES", "").strip())
            if parsed is None:
                continue
            molecule, smiles = parsed
            if ACRYLAMIDE is None or len(molecule.GetSubstructMatches(ACRYLAMIDE)) != 1:
                continue
            potency = concentration_micromolar(
                row.get("Value", ""), row.get("Unit", ""), row.get("Relation", "")
            )
            if potency is None or potency > threshold:
                continue
            identity = row.get("InChI_Key", "").strip() or molecule_identity(molecule, smiles)
            current = evidence.setdefault(
                identity,
                {
                    "smiles": smiles,
                    "inhibitor_id": row.get("Inhibitor", "").strip(),
                    "best_micromolar": potency,
                    "source_rows": [],
                },
            )
            current["best_micromolar"] = min(float(current["best_micromolar"]), potency)
            current["source_rows"].append(source_row)

    actives: list[Active] = []
    for identity, item in evidence.items():
        molecule = Chem.MolFromSmiles(str(item["smiles"]))
        if molecule is None:
            continue
        scaffold = scaffold_smiles(molecule)
        if not scaffold:
            continue
        actives.append(
            Active(
                identity=identity,
                smiles=str(item["smiles"]),
                inhibitor_id=str(item["inhibitor_id"]),
                best_micromolar=float(item["best_micromolar"]),
                scaffold=scaffold,
                properties=molecule_properties(molecule),
                source_rows=sorted(set(int(x) for x in item["source_rows"])),
            )
        )
    return sorted(actives, key=lambda item: item.identity)


def select_diverse_actives(candidates: list[Active], count: int, seed: int) -> list[Active]:
    representatives: dict[str, Active] = {}
    for candidate in candidates:
        current = representatives.get(candidate.scaffold)
        if current is None or (candidate.best_micromolar, candidate.identity) < (
            current.best_micromolar,
            current.identity,
        ):
            representatives[candidate.scaffold] = candidate
    pool = sorted(representatives.values(), key=lambda item: item.identity)
    if len(pool) < count:
        raise ValueError(f"Need {count} distinct scaffolds, found {len(pool)}")
    fps = [fingerprint(Chem.MolFromSmiles(item.smiles)) for item in pool]
    rng = random.Random(seed)
    selected = [rng.randrange(len(pool))]
    remaining = set(range(len(pool))) - set(selected)
    while len(selected) < count:
        best_index = max(
            remaining,
            key=lambda index: (
                min(1.0 - DataStructs.TanimotoSimilarity(fps[index], fps[chosen]) for chosen in selected),
                pool[index].identity,
            ),
        )
        selected.append(best_index)
        remaining.remove(best_index)
    return [pool[index] for index in selected]


def property_match(decoy: Properties, active: Properties, tolerances: dict[str, float]) -> bool:
    return (
        decoy.formal_charge == active.formal_charge
        and abs(decoy.molecular_weight - active.molecular_weight) <= tolerances["molecular_weight"]
        and abs(decoy.clogp - active.clogp) <= tolerances["clogp"]
        and abs(decoy.hbond_donors - active.hbond_donors) <= tolerances["hbond_donors"]
        and abs(decoy.hbond_acceptors - active.hbond_acceptors) <= tolerances["hbond_acceptors"]
        and abs(decoy.rotatable_bonds - active.rotatable_bonds) <= tolerances["rotatable_bonds"]
    )


def property_distance(decoy: Properties, active: Properties, tolerances: dict[str, float]) -> float:
    fields = ("molecular_weight", "clogp", "hbond_donors", "hbond_acceptors", "rotatable_bonds")
    return sum(abs(getattr(decoy, field) - getattr(active, field)) / tolerances[field] for field in fields)


def stream_decoy_candidates(
    library: Path,
    actives: list[Active],
    known_erbb_keys: set[str],
    tolerances: dict[str, float],
    maximum_similarity: float,
    heap_size: int,
) -> tuple[list[list[tuple[float, Decoy]]], dict[str, int]]:
    active_fps = [fingerprint(Chem.MolFromSmiles(active.smiles)) for active in actives]
    heaps: list[list[tuple[float, str, Decoy]]] = [[] for _ in actives]
    statistics = defaultdict(int)
    seen_identities: set[str] = set()
    with library.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=1):
            statistics["rows_scanned"] += 1
            if row_number % 250000 == 0:
                print(json.dumps({"decoy_scan_progress": row_number, **statistics}), flush=True)
            try:
                approximate = Properties(
                    molecular_weight=float(row.get("Weight", "nan")),
                    clogp=float(row.get("LogP", "nan")),
                    hbond_donors=int(float(row.get("NumHDonors", "nan"))),
                    hbond_acceptors=int(float(row.get("HAcceptors", "nan"))),
                    rotatable_bonds=int(float(row.get("RotatableBonds", "nan"))),
                    formal_charge=0,
                )
            except ValueError:
                statistics["invalid_property_rows"] += 1
                continue
            if any(math.isnan(value) for value in (approximate.molecular_weight, approximate.clogp)):
                statistics["invalid_property_rows"] += 1
                continue
            approximate_matches = [
                index
                for index, active in enumerate(actives)
                if abs(approximate.molecular_weight - active.properties.molecular_weight)
                <= tolerances["molecular_weight"] + 2.0
                and abs(approximate.clogp - active.properties.clogp) <= tolerances["clogp"] + 0.2
                and abs(approximate.hbond_donors - active.properties.hbond_donors)
                <= tolerances["hbond_donors"]
                and abs(approximate.hbond_acceptors - active.properties.hbond_acceptors)
                <= tolerances["hbond_acceptors"]
                and abs(approximate.rotatable_bonds - active.properties.rotatable_bonds)
                <= tolerances["rotatable_bonds"]
            ]
            if not approximate_matches:
                continue
            statistics["property_prefilter_rows"] += 1
            identity = row.get("InChI_Key", "").strip()
            if identity and identity in known_erbb_keys:
                statistics["known_erbb_excluded"] += 1
                continue
            parsed = canonical_molecule(row.get("SMILES", "").strip())
            if parsed is None:
                statistics["invalid_smiles"] += 1
                continue
            molecule, smiles = parsed
            if ACRYLAMIDE is None or len(molecule.GetSubstructMatches(ACRYLAMIDE)) != 1:
                continue
            statistics["single_acrylamide_rows"] += 1
            identity = identity or molecule_identity(molecule, smiles)
            if identity in seen_identities:
                statistics["duplicate_identity_rows"] += 1
                continue
            seen_identities.add(identity)
            properties = molecule_properties(molecule)
            matching = [
                index
                for index in approximate_matches
                if property_match(properties, actives[index].properties, tolerances)
            ]
            if not matching:
                continue
            fp = fingerprint(molecule)
            similarities = DataStructs.BulkTanimotoSimilarity(fp, active_fps)
            max_similarity = max(float(value) for value in similarities)
            if max_similarity >= maximum_similarity:
                statistics["similarity_excluded"] += 1
                continue
            decoy = Decoy(
                identity=identity,
                smiles=smiles,
                source=row.get("CompoundType", "").strip(),
                compound_id=row.get("Compound", "").strip() or str(row_number),
                properties=properties,
                maximum_active_tanimoto=max_similarity,
            )
            for index in matching:
                score = property_distance(properties, actives[index].properties, tolerances)
                entry = (-score, identity, decoy)
                if len(heaps[index]) < heap_size:
                    heapq.heappush(heaps[index], entry)
                elif score < -heaps[index][0][0]:
                    heapq.heapreplace(heaps[index], entry)
    ranked = [sorted([(-score, decoy) for score, _, decoy in heap], key=lambda x: (x[0], x[1].identity)) for heap in heaps]
    statistics["candidate_identity_union"] = len({item.identity for group in ranked for _, item in group})
    return ranked, dict(statistics)


def assign_decoys(
    ranked: list[list[tuple[float, Decoy]]], per_active: int
) -> list[tuple[int, float, Decoy]]:
    order = sorted(range(len(ranked)), key=lambda index: (len(ranked[index]), index))
    used: set[str] = set()
    assigned: list[tuple[int, float, Decoy]] = []
    for index in order:
        available = [(score, item) for score, item in ranked[index] if item.identity not in used]
        if len(available) < per_active:
            raise ValueError(
                f"Active index {index} has only {len(available)} unused decoys; need {per_active}"
            )
        for score, item in available[:per_active]:
            used.add(item.identity)
            assigned.append((index, score, item))
    return sorted(assigned, key=lambda item: (item[0], item[1], item[2].identity))


def encrypt_file(plain: Path, encrypted: Path, key_file: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-in",
            str(plain),
            "-out",
            str(encrypted),
            "-pass",
            f"file:{key_file}",
        ],
        check=True,
    )


def summarize_balance(active_rows: pd.DataFrame, decoy_rows: pd.DataFrame) -> dict[str, object]:
    columns = ["molecular_weight", "clogp", "hbond_donors", "hbond_acceptors", "rotatable_bonds", "formal_charge"]
    summary: dict[str, object] = {}
    for column in columns:
        active_mean = float(active_rows[column].mean())
        decoy_mean = float(decoy_rows[column].mean())
        pooled_sd = math.sqrt((float(active_rows[column].var(ddof=1)) + float(decoy_rows[column].var(ddof=1))) / 2)
        summary[column] = {
            "active_mean": active_mean,
            "decoy_mean": decoy_mean,
            "standardized_mean_difference": 0.0 if pooled_sd == 0 else (active_mean - decoy_mean) / pooled_sd,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--candidate-heap-size", type=int, default=1000)
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    dataset = config["dataset"]
    target = config["target"]
    covindb = Path(dataset["covindb_source"])
    library = Path(dataset["decoy_source"])
    seed = int(dataset["seed"])
    active_count = int(dataset["active_count"])
    decoys_per_active = int(dataset["decoys_per_active"])
    threshold = float(dataset["active_activity_threshold_micromolar"])
    tolerances = {key: float(value) for key, value in dataset["property_tolerances"].items()}
    maximum_similarity = 0.40

    candidates = collect_erbb2_actives(covindb, threshold)
    selected_actives = select_diverse_actives(candidates, active_count, seed)
    known_erbb_keys = collect_known_erbb_keys(covindb)
    ranked_decoys, scan_statistics = stream_decoy_candidates(
        library,
        selected_actives,
        known_erbb_keys,
        tolerances,
        maximum_similarity,
        args.candidate_heap_size,
    )
    assigned_decoys = assign_decoys(ranked_decoys, decoys_per_active)

    labeled_rows: list[dict[str, object]] = []
    for active_index, active in enumerate(selected_actives):
        labeled_rows.append(
            {
                "label": "active",
                "source_identity": active.identity,
                "source_compound_id": active.inhibitor_id,
                "standardized_smiles": active.smiles,
                "matched_active_identity": active.identity,
                "best_micromolar": active.best_micromolar,
                "scaffold": active.scaffold,
                "maximum_active_tanimoto": 1.0,
                **asdict(active.properties),
                "source_rows": ";".join(str(value) for value in active.source_rows),
                "active_index": active_index,
            }
        )
    for active_index, score, decoy in assigned_decoys:
        labeled_rows.append(
            {
                "label": "decoy",
                "source_identity": decoy.identity,
                "source_compound_id": decoy.compound_id,
                "standardized_smiles": decoy.smiles,
                "matched_active_identity": selected_actives[active_index].identity,
                "best_micromolar": math.nan,
                "scaffold": "",
                "maximum_active_tanimoto": decoy.maximum_active_tanimoto,
                **asdict(decoy.properties),
                "source_rows": "",
                "active_index": active_index,
                "property_distance": score,
                "decoy_source": decoy.source,
            }
        )
    answer_key = pd.DataFrame(labeled_rows)
    expected_rows = active_count * (decoys_per_active + 1)
    if len(answer_key) != expected_rows:
        raise ValueError(f"Expected {expected_rows} rows, found {len(answer_key)}")
    order = list(range(len(answer_key)))
    random.Random(seed).shuffle(order)
    answer_key = answer_key.iloc[order].reset_index(drop=True)
    answer_key.insert(0, "input_sdf_index", range(len(answer_key)))
    answer_key.insert(0, "blind_id", [f"ERBB2B{index:04d}" for index in range(len(answer_key))])
    answer_key["target_id"] = target["target_id"]
    answer_key["site_id"] = target["site_id"]

    operational = pd.DataFrame(
        {
            "standardized_smiles": answer_key["standardized_smiles"],
            "label": "blinded",
            "target_id": target["target_id"],
            "site_id": target["site_id"],
            "compound_id": answer_key["blind_id"],
            "parent_id": answer_key["blind_id"],
            "protomer_id": "0",
            "matched_active_id": "",
            "input_sdf_index": answer_key["input_sdf_index"],
        }
    )
    forbidden = {"active", "decoy"}
    for column in operational.columns:
        if set(operational[column].astype(str).str.lower().unique()) & forbidden:
            raise ValueError(f"Operational column leaks labels: {column}")

    args.output_root.mkdir(parents=True, exist_ok=False)
    args.sealed_root.mkdir(parents=True, exist_ok=False)
    operational_parquet = args.output_root / "erbb2_blind_mini.parquet"
    operational_csv = args.output_root / "erbb2_blind_mini.csv"
    operational.to_parquet(operational_parquet, index=False)
    operational.to_csv(operational_csv, index=False)

    active_rows = answer_key.loc[answer_key["label"].eq("active")]
    decoy_rows = answer_key.loc[answer_key["label"].eq("decoy")]
    balance = summarize_balance(active_rows, decoy_rows)
    with tempfile.TemporaryDirectory(prefix="erbb2_c805_lockbox_") as temporary_name:
        temporary = Path(temporary_name)
        plain_parquet = temporary / "answer_key.parquet"
        plain_csv = temporary / "answer_key.csv"
        data_key = temporary / "data_key.bin"
        answer_key.to_parquet(plain_parquet, index=False)
        answer_key.to_csv(plain_csv, index=False)
        data_key.write_bytes(os.urandom(48))
        parquet_enc = args.sealed_root / "answer_key.parquet.enc"
        csv_enc = args.sealed_root / "answer_key.csv.enc"
        wrapped_key = args.sealed_root / "answer_key_data_key.rsa"
        encrypt_file(plain_parquet, parquet_enc, data_key)
        encrypt_file(plain_csv, csv_enc, data_key)
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-encrypt",
                "-pubin",
                "-inkey",
                str(args.public_key),
                "-in",
                str(data_key),
                "-out",
                str(wrapped_key),
            ],
            check=True,
        )
        plaintext_commitments = {
            "answer_key_parquet_sha256": sha256_file(plain_parquet),
            "answer_key_csv_sha256": sha256_file(plain_csv),
        }

    manifest = {
        "schema_version": 1,
        "panel_id": config["panel_id"],
        "status": "BLINDED_AND_SEALED",
        "selection_frozen_before_scoring": True,
        "config": str(args.config),
        "config_sha256": sha256_file(args.config),
        "sources": {
            "covindb": str(covindb),
            "covindb_sha256": sha256_file(covindb),
            "decoy_library": str(library),
            "decoy_library_sha256": sha256_file(library),
        },
        "selection": {
            "eligible_active_compounds": len(candidates),
            "selected_active_compounds": len(selected_actives),
            "selected_active_scaffolds": len(set(item.scaffold for item in selected_actives)),
            "selected_decoys": len(assigned_decoys),
            "decoys_per_active": decoys_per_active,
            "maximum_observed_active_decoy_tanimoto": float(decoy_rows["maximum_active_tanimoto"].max()),
            "scan_statistics": scan_statistics,
            "property_balance": balance,
        },
        "operational_files": {
            "parquet": str(operational_parquet),
            "parquet_sha256": sha256_file(operational_parquet),
            "csv": str(operational_csv),
            "csv_sha256": sha256_file(operational_csv),
        },
        "sealed_files": {
            "answer_key_parquet": str(parquet_enc),
            "answer_key_parquet_encrypted_sha256": sha256_file(parquet_enc),
            "answer_key_csv": str(csv_enc),
            "answer_key_csv_encrypted_sha256": sha256_file(csv_enc),
            "wrapped_data_key": str(wrapped_key),
            "wrapped_data_key_sha256": sha256_file(wrapped_key),
            **plaintext_commitments,
        },
        "public_key_sha256": sha256_file(args.public_key),
        "rows": len(operational),
        "labels_exposed_to_scoring_pipeline": False,
    }
    manifest_path = args.output_root / "blinded_panel_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    for path in (parquet_enc, csv_enc, wrapped_key):
        os.chmod(path, 0o400)
    print(json.dumps(manifest, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
