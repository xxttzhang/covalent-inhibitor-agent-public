from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterable

import yaml
from rdkit import Chem, DataStructs
from rdkit.Chem import Descriptors, FilterCatalog, Lipinski, rdDepictor, rdMolDescriptors
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold


ALGORITHM_VERSION = "covindb-prescreen-v1.3"


def validate_library_columns(fieldnames):
    """The workflow and prescreen admit the same source table contract."""
    required_columns = {"SMILES", "InChI_Key", "Source", "CompoundType"}
    missing = sorted(required_columns - set(fieldnames or []))
    if missing:
        raise ValueError(f"CovalentInDB library missing columns: {missing}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def load_target_config(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text()) or {}
    required = {
        "target_id",
        "site_id",
        "protein_chain",
        "residue_name",
        "residue_number",
        "reaction_type",
        "warhead_class",
        "warhead_smarts",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"target config missing fields: {missing}")
    if str(payload["residue_name"]).upper() != "CYS":
        raise ValueError("CovalentInDB prescreen v1 supports CYS targets only")
    if "MICHAEL" not in str(payload["reaction_type"]).upper():
        raise ValueError("CovalentInDB prescreen v1 supports Michael addition only")
    if Chem.MolFromSmarts(str(payload["warhead_smarts"])) is None:
        raise ValueError("warhead_smarts is not a valid SMARTS query")
    return payload


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parent(mol: Chem.Mol) -> Chem.Mol:
    cleaned = rdMolStandardize.Cleanup(mol)
    parent = rdMolStandardize.FragmentParent(cleaned)
    neutral = rdMolStandardize.Uncharger().uncharge(parent)
    # Neutralization may invalidate ring/property caches. Rebuild and validate
    # them before any descriptors, SMARTS matching or identity are computed.
    # Failures remain standardization_failed; no relaxed sanitization fallback.
    Chem.SanitizeMol(neutral)
    return neutral


def _scaffold(mol: Chem.Mol, smiles: str) -> str:
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    if scaffold.GetNumAtoms():
        return Chem.MolToSmiles(scaffold, canonical=True)
    return f"acyclic:{hashlib.sha256(smiles.encode()).hexdigest()[:16]}"


def _property_score(mw: float, logp: float, tpsa: float, rotors: int) -> float:
    targets = ((mw, 360.0, 150.0), (logp, 2.5, 2.0), (tpsa, 75.0, 55.0), (float(rotors), 5.0, 5.0))
    return sum(math.exp(-0.5 * ((value - center) / width) ** 2) for value, center, width in targets) / len(targets)


def _pains_catalog() -> FilterCatalog.FilterCatalog:
    params = FilterCatalog.FilterCatalogParams()
    params.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
    return FilterCatalog.FilterCatalog(params)


def _push(heap: list[tuple[float, int, dict[str, Any]]], limit: int, score: float, serial: int, row: dict[str, Any]) -> None:
    item = (score, serial, row)
    if len(heap) < limit:
        heapq.heappush(heap, item)
        return
    if item[:2] > heap[0][:2]:
        heapq.heapreplace(heap, item)


def _sorted(heap: Iterable[tuple[float, int, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [item[2] for item in sorted(heap, key=lambda item: (item[0], item[1]), reverse=True)]


@dataclass
class PrescreenCounts:
    input_rows: int = 0
    parsed: int = 0
    standardized: int = 0
    unique: int = 0
    duplicates: int = 0
    invalid: int = 0
    compatible_warhead: int = 0
    unambiguous_electrophile: int = 0
    hard_gate_pass: int = 0
    hard_gate_rejected: int = 0
    selected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    property_rejection_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    def payload(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "parse_success_rate": self.parsed / self.input_rows if self.input_rows else 0.0,
            "warhead_mapping_rate": (
                self.unambiguous_electrophile / self.compatible_warhead
                if self.compatible_warhead
                else 0.0
            ),
        }


class ParquetSink:
    def __init__(self, path: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.path = path
        self.pa = pa
        self.pq = pq
        self.writer: Any | None = None
        self.count = 0

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        table = self.pa.Table.from_pylist(rows)
        if self.writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.writer = self.pq.ParquetWriter(self.path, table.schema, compression="zstd")
        else:
            table = table.cast(self.writer.schema)
        self.writer.write_table(table)
        self.count += len(rows)

    def close(self, *, empty_schema: dict[str, Any] | None = None) -> None:
        if self.writer is not None:
            self.writer.close()
            return
        if empty_schema is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            table = self.pa.Table.from_pydict(
                {name: self.pa.array([], type=data_type) for name, data_type in empty_schema.items()}
            )
            self.pq.write_table(table, self.path, compression="zstd")


def _write_candidates(output: Path, candidates: list[dict[str, Any]]) -> None:
    import pandas as pd

    frame = pd.DataFrame(candidates)
    frame.to_parquet(output / "stage2_candidates.parquet", index=False)
    frame.to_csv(output / "stage2_candidates.csv", index=False)
    writer = Chem.SDWriter(str(output / "stage2_candidates.sdf"))
    for row in candidates:
        mol = Chem.MolFromSmiles(str(row["standardized_smiles"]))
        if mol is None:
            continue
        rdDepictor.Compute2DCoords(mol)
        mol.SetProp("_Name", str(row["compound_id"]))
        mol.SetIntProp("INPUT_SDF_INDEX", int(row["input_sdf_index"]))
        for key in ("compound_id", "selection_channel", "warhead_class", "parent_inchikey"):
            mol.SetProp(key.upper(), str(row[key]))
        mol.SetDoubleProp("REFERENCE_SIMILARITY", float(row["reference_similarity"]))
        mol.SetDoubleProp("CHANNEL_SCORE", float(row["channel_score"]))
        writer.write(mol)
    writer.close()


def source_records(reader, max_rows, record_selection, seed, audit):
    """Keep original record indices; never inspect chemistry to select the sample."""
    if record_selection == "file_order_prefix":
        for index, row in enumerate(islice(reader, max_rows)):
            audit["records_scanned"] += 1
            yield index, row
        return
    if record_selection != "reservoir" or type(max_rows) is not int or max_rows < 1:
        raise ValueError("reservoir sampling requires a positive bounded max_rows")
    rng = random.Random(seed)
    sample = []
    for index, row in enumerate(reader):
        audit["records_scanned"] += 1
        if len(sample) < max_rows:
            sample.append((index, row))
        else:
            slot = rng.randrange(index + 1)
            if slot < max_rows:
                sample[slot] = (index, row)
    yield from sorted(sample, key=lambda item: item[0])


def run_prescreen(
    input_csv: Path,
    target_config_path: Path,
    output_dir: Path,
    *,
    top_n: int = 20_000,
    max_rows: int | None = None,
    chunk_size: int = 5_000,
    seed: int = 20260725,
    record_selection: str = "file_order_prefix",
) -> dict[str, Any]:
    if record_selection not in {"file_order_prefix", "reservoir"}:
        raise ValueError("Unsupported record selection")
    if record_selection == "reservoir" and (type(max_rows) is not int or max_rows < 1):
        raise ValueError("reservoir sampling requires positive max_rows")
    sampling_audit = {"records_scanned": 0}
    started = time.monotonic()
    target = load_target_config(target_config_path)
    query = Chem.MolFromSmarts(str(target["warhead_smarts"]))
    if query is None:
        raise ValueError("warhead SMARTS could not be compiled")
    reference = Chem.MolFromSmiles(str(target.get("reference_smiles", "")))
    reference_fp = rdMolDescriptors.GetMorganFingerprintAsBitVect(reference, 2, nBits=2048) if reference else None
    limits = target.get("lead_like", {})
    min_mw = _float(limits.get("min_mw"), 180.0)
    max_mw = _float(limits.get("max_mw"), 650.0)
    min_logp = _float(limits.get("min_logp"), -1.0)
    max_logp = _float(limits.get("max_logp"), 6.0)
    max_tpsa = _float(limits.get("max_tpsa"), 160.0)
    max_rotors = int(limits.get("max_rotors", 15))
    max_charge = int(limits.get("max_abs_formal_charge", 2))
    output_dir.mkdir(parents=True, exist_ok=True)
    master = ParquetSink(output_dir / "covindb_master.parquet")
    rejected = ParquetSink(output_dir / "covindb_rejected.parquet")
    counts = PrescreenCounts()
    seen: set[str] = set()
    pains = _pains_catalog()
    quota_exploit = top_n // 2
    quota_diversity = (top_n * 3) // 10
    quota_explore = top_n - quota_exploit - quota_diversity
    exploit_heap: list[tuple[float, int, dict[str, Any]]] = []
    explore_heap: list[tuple[float, int, dict[str, Any]]] = []
    overall_heap: list[tuple[float, int, dict[str, Any]]] = []
    diversity: dict[str, tuple[float, int, dict[str, Any]]] = {}
    master_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []

    def reject(index: int, source_id: str, smiles: str, reason: str) -> None:
        counts.reject(reason)
        rejected_rows.append(
            {
                "input_index": index,
                "source_record_id": source_id,
                "original_smiles": smiles,
                "failure_reason": reason,
            }
        )

    def flush() -> None:
        master.write(master_rows)
        rejected.write(rejected_rows)
        master_rows.clear()
        rejected_rows.clear()
        atomic_json(
            output_dir / "progress.json",
            {
                "schema_version": "1.0",
                "algorithm_version": ALGORITHM_VERSION,
                "status": "running",
                "updated_at": utc_now(),
                "max_rows": max_rows,
                "counts": counts.payload(),
            },
        )

    with input_csv.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        validate_library_columns(reader.fieldnames)
        # Bound the iterator before pulling the next CSV record. Checking inside
        # the loop parses one extra row, which can fail outside the approved scope.
        for index, row in source_records(reader, max_rows, record_selection, seed, sampling_audit):
            counts.input_rows += 1
            source_id = str(row.get("") or row.get("Compound") or index)
            original = str(row.get("SMILES") or "").strip()
            if not original:
                counts.invalid += 1
                reject(index, source_id, original, "missing_smiles")
                continue
            mol = Chem.MolFromSmiles(original)
            if mol is None:
                counts.invalid += 1
                reject(index, source_id, original, "invalid_smiles")
                continue
            counts.parsed += 1
            try:
                mol = _parent(mol)
                standardized = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)
                key = Chem.MolToInchiKey(mol) or hashlib.sha256(standardized.encode()).hexdigest()
            except Exception:
                counts.invalid += 1
                reject(index, source_id, original, "standardization_failed")
                continue
            counts.standardized += 1
            if key in seen:
                counts.duplicates += 1
                reject(index, source_id, original, "duplicate_parent")
                continue
            seen.add(key)
            counts.unique += 1
            matches = mol.GetSubstructMatches(query, uniquify=True)
            warhead_count = len(matches)
            compatible = warhead_count > 0
            if compatible:
                counts.compatible_warhead += 1
            electrophiles = sorted({int(match[0]) for match in matches if match})
            unambiguous = compatible and len(electrophiles) == 1
            if unambiguous:
                counts.unambiguous_electrophile += 1
            mw = float(Descriptors.MolWt(mol))
            logp = float(Descriptors.MolLogP(mol))
            tpsa = float(rdMolDescriptors.CalcTPSA(mol))
            rotors = int(Lipinski.NumRotatableBonds(mol))
            charge = int(Chem.GetFormalCharge(mol))
            hbd = int(Lipinski.NumHDonors(mol))
            hba = int(Lipinski.NumHAcceptors(mol))
            rings = int(Lipinski.RingCount(mol))
            properties = {
                "mw_below_min": mw < min_mw,
                "mw_above_max": mw > max_mw,
                "clogp_below_min": logp < min_logp,
                "clogp_above_max": logp > max_logp,
                "tpsa_above_max": tpsa > max_tpsa,
                "rotors_above_max": rotors > max_rotors,
                "charge_above_max": abs(charge) > max_charge,
            }
            # Keep the original acceptance expression: diagnostic detail must
            # not change boundary/NaN behavior or relax a scientific filter.
            property_ok = (min_mw <= mw <= max_mw and min_logp <= logp <= max_logp
                           and tpsa <= max_tpsa and rotors <= max_rotors and abs(charge) <= max_charge)
            property_reasons = [reason for reason, failed in properties.items() if failed]
            if not all(math.isfinite(value) for value in (mw, logp, tpsa, rotors, charge)):
                property_reasons.append("nonfinite_descriptor")
            for reason in property_reasons:
                counts.property_rejection_reasons[reason] = counts.property_rejection_reasons.get(reason, 0) + 1
            gate_reasons = []
            if not compatible:
                gate_reasons.append("no_compatible_warhead")
            if compatible and not unambiguous:
                gate_reasons.append("ambiguous_electrophile")
            if not property_ok:
                gate_reasons.append("outside_lead_like_window")
            eligible = not gate_reasons
            if eligible:
                counts.hard_gate_pass += 1
            else:
                counts.hard_gate_rejected += 1
                reject(index, source_id, original, ";".join(gate_reasons))
            fp = rdMolDescriptors.GetMorganFingerprintAsBitVect(mol, 2, nBits=2048)
            similarity = float(DataStructs.TanimotoSimilarity(fp, reference_fp)) if reference_fp else 0.0
            prop = _property_score(mw, logp, tpsa, rotors)
            pains_match = pains.GetFirstMatch(mol)
            pains_alert = pains_match.GetDescription() if pains_match else ""
            high_reactivity = warhead_count > 1
            aggregation_risk = logp > 5.0 and mw > 500.0
            alert_penalty = (0.20 if high_reactivity else 0.0) + (0.10 if pains_alert else 0.0) + (0.10 if aggregation_risk else 0.0)
            exploit_score = (0.75 * similarity + 0.25 * prop if reference_fp else prop) - alert_penalty
            explore_score = 0.45 * (1.0 - similarity) + 0.45 * prop + 0.10 * min(rings / 4.0, 1.0) - alert_penalty
            scaffold = _scaffold(mol, standardized)
            record = {
                "input_index": index,
                "compound_id": f"covindb-{key.lower()}",
                "source_record_id": source_id,
                "source": str(row.get("Source") or ""),
                "compound_type": str(row.get("CompoundType") or ""),
                "original_smiles": original,
                "standardized_smiles": standardized,
                "parent_inchikey": key,
                "formula": rdMolDescriptors.CalcMolFormula(mol),
                "mw": mw,
                "clogp": logp,
                "tpsa": tpsa,
                "hbd": hbd,
                "hba": hba,
                "rotatable_bonds": rotors,
                "ring_count": rings,
                "formal_charge": charge,
                "warhead_class": str(target["warhead_class"]),
                "warhead_count": warhead_count,
                "electrophile_atom_index": electrophiles[0] if unambiguous else -1,
                "scaffold_smiles": scaffold,
                "pains_alert": pains_alert,
                "high_reactivity_alert": high_reactivity,
                "aggregation_risk": aggregation_risk,
                "reference_similarity": similarity,
                "property_fit_score": prop,
                "alert_penalty": alert_penalty,
                "diversity_score": prop - alert_penalty + 0.10 * (1.0 - similarity),
                "exploit_score": exploit_score,
                "explore_score": explore_score,
                "backfill_score": max(exploit_score, explore_score),
                "eligible": eligible,
                "gate_status": "PASS" if eligible else "REJECT",
                "gate_reason": ";".join(gate_reasons),
                "property_gate_reason": ";".join(property_reasons),
            }
            master_rows.append(record)
            if eligible:
                candidate = dict(record)
                serial = counts.input_rows
                _push(exploit_heap, max(top_n * 2, 1), exploit_score, serial, candidate)
                _push(explore_heap, max(top_n * 2, 1), explore_score, serial, candidate)
                _push(overall_heap, max(top_n * 3, 1), max(exploit_score, explore_score), serial, candidate)
                diversity_score = prop - alert_penalty + 0.10 * (1.0 - similarity)
                current = diversity.get(scaffold)
                item = (diversity_score, serial, candidate)
                if current is None or item[:2] > current[:2]:
                    diversity[scaffold] = item
            if len(master_rows) + len(rejected_rows) >= chunk_size:
                flush()
    flush()
    master.close()
    rejected.close(
        empty_schema={
            "input_index": rejected.pa.int64(),
            "source_record_id": rejected.pa.string(),
            "original_smiles": rejected.pa.string(),
            "failure_reason": rejected.pa.string(),
        }
    )

    chosen: list[dict[str, Any]] = []
    selected: set[str] = set()

    def take(rows: Iterable[dict[str, Any]], quota: int, channel: str, score_key: str) -> None:
        for row in rows:
            if len([item for item in chosen if item["selection_channel"] == channel]) >= quota:
                return
            identity = str(row["compound_id"])
            if identity in selected:
                continue
            item = dict(row)
            item["selection_channel"] = channel
            item["channel_score"] = float(item[score_key])
            item["score_key"] = score_key
            item["score_version"] = ALGORITHM_VERSION
            item["selection_reason"] = {
                "exploitation": "reference_similarity_and_property_fit",
                "diversity": "scaffold_representative_ranked_by_diversity_score",
                "exploration": "novelty_property_fit_and_ring_reward",
                "backfill": "remaining_pool_backfill",
            }[channel]
            chosen.append(item)
            selected.add(identity)

    take(_sorted(exploit_heap), quota_exploit, "exploitation", "exploit_score")
    diversity_rows = [item[2] for item in sorted(diversity.values(), key=lambda item: (item[0], item[1]), reverse=True)]
    take(diversity_rows, quota_diversity, "diversity", "diversity_score")
    take(_sorted(explore_heap), quota_explore, "exploration", "explore_score")
    if len(chosen) < top_n:
        take(_sorted(overall_heap), top_n - len(chosen), "backfill", "backfill_score")
    for rank, row in enumerate(chosen[:top_n], start=1):
        row["rank"] = rank
        row["input_sdf_index"] = rank - 1
    chosen = chosen[:top_n]
    counts.selected = len(chosen)
    _write_candidates(output_dir, chosen)
    selected_mapping_rate = (
        sum(int(row["electrophile_atom_index"]) >= 0 for row in chosen) / len(chosen)
        if chosen
        else 0.0
    )
    source_unambiguous_rate = (
        counts.unambiguous_electrophile / counts.compatible_warhead
        if counts.compatible_warhead
        else 0.0
    )
    quality = {
        "rdkit_parse_rate": counts.parsed / counts.input_rows if counts.input_rows else 0.0,
        "selected_warhead_mapping_rate": selected_mapping_rate,
        "source_unambiguous_warhead_rate": source_unambiguous_rate,
        "identity_loss_count": counts.input_rows - counts.unique - counts.duplicates - counts.invalid,
        "failure_table_present": (output_dir / "covindb_rejected.parquet").is_file(),
    }
    gates = {
        "rdkit_parse_rate_ge_0_99": quality["rdkit_parse_rate"] >= 0.99,
        "selected_warhead_mapping_rate_ge_0_98": quality["selected_warhead_mapping_rate"] >= 0.98,
        "identity_loss_zero": quality["identity_loss_count"] == 0,
        "failure_table_present": quality["failure_table_present"],
    }
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSION,
        "diagnostics_version": "prescreen-rejections-v1",
        "status": "completed",
        "selection_outcome": "CANDIDATES_SELECTED" if chosen else "NO_CANDIDATES_SELECTED",
        "filters": {"min_mw": min_mw, "max_mw": max_mw, "min_logp": min_logp,
                    "max_logp": max_logp, "max_tpsa": max_tpsa, "max_rotors": max_rotors,
                    "max_abs_formal_charge": max_charge},
        "diagnostic_note": "Rejection rows include input failures and unique molecules failing hard gates. "
                           "Property reason counts can overlap and cover all standardized unique molecules. "
                           "No candidates is not evidence of inactivity; quality gates still control advancement.",
        "generated_at": utc_now(),
        "runtime_seconds": time.monotonic() - started,
        "source": {
            "path": str(input_csv),
            "size_bytes": input_csv.stat().st_size,
            "sha256": sha256_file(input_csv),
            "max_rows": max_rows,
            "record_selection": record_selection if max_rows is not None else "complete_file",
            "sampling_seed": seed if record_selection == "reservoir" else None,
            "records_scanned": sampling_audit["records_scanned"],
            "records_read": counts.input_rows,
            "sampling_note": (
                "Scans the complete CSV and uniformly samples bounded rows without replacement before chemistry; retains original row indices."
                if record_selection == "reservoir" else
                "Reads only the approved file-order prefix; not a random or representative sample. "
                "Zero eligible candidates does not establish absence in the rest of the library."
                if max_rows is not None else "Reads the complete source file in file order."
            ),
        },
        "target": {
            "config_path": str(target_config_path),
            "config_sha256": sha256_file(target_config_path),
            "target_id": target["target_id"],
            "site_id": target["site_id"],
            "reaction_type": target["reaction_type"],
            "warhead_class": target["warhead_class"],
        },
        "selection": {
            "top_n_requested": top_n,
            "top_n_selected": len(chosen),
            "quotas": {
                "exploitation": quota_exploit,
                "diversity": quota_diversity,
                "exploration": quota_explore,
            },
            "reference_mode": "similarity" if reference_fp else "property_fallback",
            "score_contract": {
                "version": ALGORITHM_VERSION,
                "reference_smiles": target.get("reference_smiles"),
                "similarity": "Morgan radius=2 bits=2048; Tanimoto",
                "property_fit": "mean(exp(-0.5*((value-center)/width)^2))",
                "property_centers_widths": {"mw": [360, 150], "clogp": [2.5, 2], "tpsa": [75, 55], "rotors": [5, 5]},
                "penalty": "0.20*multiple_warhead_matches + 0.10*PAINS + 0.10*(clogp>5 and mw>500)",
                "exploitation": "0.75*similarity + 0.25*property_fit - penalty; without reference: property_fit - penalty",
                "diversity": "property_fit - penalty + 0.10*(1-similarity); one representative per scaffold before cross-channel deduplication",
                "exploration": "0.45*(1-similarity) + 0.45*property_fit + 0.10*min(ring_count/4,1) - penalty",
                "backfill": "max(exploit_score, explore_score)",
                "rank_semantics": "channel concatenation order, not global activity ranking",
                "cross_channel_scores_comparable": False,
                "limitations": "Heuristic chemical prioritization, not docking, binding probability or biological activity. No activity-calibrated weights.",
            },
        },
        "counts": counts.payload(),
        "quality": quality,
        "quality_gates": gates,
        "outputs": {
            "master": "covindb_master.parquet",
            "rejected": "covindb_rejected.parquet",
            "candidates_parquet": "stage2_candidates.parquet",
            "candidates_csv": "stage2_candidates.csv",
            "candidates_sdf": "stage2_candidates.sdf",
        },
    }
    atomic_json(output_dir / "stage2_selection_manifest.json", manifest)
    atomic_json(
        output_dir / "progress.json",
        {
            "schema_version": "1.0",
            "algorithm_version": ALGORITHM_VERSION,
            "status": "completed",
            "updated_at": utc_now(),
            "counts": counts.payload(),
            "quality_gates": gates,
        },
    )
    return manifest
