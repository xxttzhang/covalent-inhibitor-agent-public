from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from .jobs import sha256_file
from .pose_identity import exact_pose_index


Route = Literal["library-first", "ligand-first", "de-novo"]
DocumentRole = Literal["selection_report", "traceability_report", "traceability_json"]
TASK_ID = re.compile(r"^[A-Za-z0-9._-]{6,128}$")
IDENTITY_FIELDS = (
    "compound_id", "input_sdf_index", "standardized_smiles", "target_id",
    "site_id", "parent_id", "warhead_class", "electrophilic_atom",
    "nucleophilic_atom",
)
DISPLAY_FIELDS = (
    "final_rank", "pareto_front", "pareto_eligible", "pareto_exclusion_reason",
    "ranking_policy", "selection_reason", "experimental_priority_band",
    "independent_gate", "independent_parent_mcs_fraction",
    "independent_failure_reasons", "independent_warning_reasons",
    "physical_validity",
    "cross_method_consensus", "covdock_gate", "covdock_gate_detail",
    "covdock_gate_reason", "af3_covdock_ligand_rmsd_angstrom",
    "local_strain_energy", "covalent_bond_length_angstrom",
    "minimum_nonbonded_heavy_atom_distance_angstrom", "severe_clash",
    "chemistry_gate", "parent_retained_fraction", "property_fit_score",
    "chemistry_risk_count", "scaffold_smiles",
)


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".17g")
    return str(value).strip()


def _truth(value: Any) -> bool | None:
    if value is None or _text(value) == "":
        return None
    normalized = _text(value).upper()
    if normalized in {"TRUE", "SUCCESS", "COMPLETED", "PASS", "1"}:
        return True
    if normalized in {"FALSE", "FAILED", "ERROR", "0"}:
        return False
    return None


def _method_status(row: pd.Series, method: str) -> str:
    explicit = _truth(row.get(f"{method}_success"))
    if explicit is not None:
        return "SUCCESS" if explicit else "FAILED"
    raw = _text(row.get(f"{method}_status")).upper()
    if raw in {"SUCCESS", "COMPLETED", "PASS"}:
        return "SUCCESS"
    if raw in {"FAILED", "ERROR"}:
        return "FAILED"
    if raw in {"NOT_RUN", "SKIPPED", "NOT_APPLICABLE", ""}:
        return "NOT_RUN"
    raise ValueError(f"unsupported {method} status: {raw}")


def _score(row: pd.Series, names: tuple[str, ...]) -> str:
    for name in names:
        raw = _text(row.get(name))
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError as exc:
            raise ValueError(f"non-numeric score in {name}: {raw}") from exc
        if not math.isfinite(number):
            raise ValueError(f"non-finite score in {name}: {raw}")
        return raw
    return ""


def _asset(
    assets: dict[str, dict[str, Any]], method: str, input_index: str, raw_path: Any, resolver=None
) -> str:
    path = Path(resolver(_text(raw_path)) if resolver else _text(raw_path)).resolve()
    if not path.is_file():
        raise ValueError(f"{method} structure does not exist: {path}")
    size = path.stat().st_size
    if not 0 < size <= 128 * 1024 * 1024:
        raise ValueError(f"{method} structure size outside delivery bounds: {path}")
    digest = sha256_file(path)
    for existing_key, existing in assets.items():
        if existing["path"] == str(path) and existing["sha256"] == digest:
            return existing_key
    suffix = "".join(path.suffixes[-2:]) if path.name.endswith(".mae.gz") else path.suffix
    key = f"{method}_{input_index}_{digest[:12]}{suffix or '.dat'}"
    record = {"path": str(path), "source": str(path), "sha256": digest, "size": size}
    prior = assets.get(key)
    if prior is not None and prior != record:
        raise ValueError(f"asset key collision: {key}")
    assets[key] = record
    return key


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        # Identifiers are labels, not numbers; inference would turn 007 into 7.
        # Preserve pose text as well so exact index validation precedes rounding.
        return pd.read_csv(path, keep_default_na=False,
                           dtype={field: str for field in (*IDENTITY_FIELDS, "covdock_pose_index", "pose_index")})
    raise ValueError("candidate table must be CSV or Parquet")


def _document(role: DocumentRole, raw_path: Path) -> dict[str, Any]:
    path = raw_path.resolve()
    if not path.is_file():
        raise ValueError(f"{role} does not exist: {path}")
    size = path.stat().st_size
    if not 0 < size <= 8 * 1024 * 1024:
        raise ValueError(f"{role} size outside report delivery bounds: {path}")
    suffix = path.suffix.lower()
    mime_type = {".md": "text/markdown", ".json": "application/json"}.get(suffix)
    if mime_type is None:
        raise ValueError(f"{role} must be Markdown or JSON: {path}")
    return {
        "path": str(path),
        "source": str(path),
        "sha256": sha256_file(path),
        "size": size,
        "mime_type": mime_type,
        "title": path.name,
    }


def build_candidate_results_manifest(
    table_path: Path,
    *,
    route: Route,
    task_id: str,
    expected_count: int | None = None,
    documents: dict[DocumentRole, Path] | None = None,
    asset_resolver=None,
) -> dict[str, Any]:
    if route not in {"library-first", "ligand-first", "de-novo"}:
        raise ValueError(f"unsupported route: {route}")
    if not TASK_ID.fullmatch(task_id):
        raise ValueError("invalid task_id")
    table_path = table_path.resolve()
    frame = _read_table(table_path)
    # Existing untagged tables remain readable; explicit provenance cannot be relabeled.
    if "route" in frame.columns and any(_text(value) != route for value in frame["route"]):
        raise ValueError("candidate table route differs from the requested delivery route")
    if frame.empty or len(frame) > 100:
        raise ValueError("candidate count outside delivery bounds 1..100")
    if expected_count is not None and len(frame) != expected_count:
        raise ValueError(f"candidate count mismatch: expected {expected_count}, got {len(frame)}")
    missing = sorted(set(IDENTITY_FIELDS) - set(frame.columns))
    if missing:
        raise ValueError(f"candidate table lacks unified scientific fields: {missing}")
    identities = [(_text(row.input_sdf_index), _text(row.compound_id)) for row in frame.itertuples()]
    if any(not index.isdigit() or not compound for index, compound in identities):
        raise ValueError("candidate identity is missing or invalid")
    if len(set(identities)) != len(identities):
        raise ValueError("candidate identity is duplicated")

    assets: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, str]] = []
    for _, row in frame.iterrows():
        candidate = {field: _text(row.get(field)) for field in IDENTITY_FIELDS}
        empty = [field for field, value in candidate.items() if not value]
        if empty:
            raise ValueError(f"candidate {candidate.get('compound_id')} has empty unified fields: {empty}")
        index = candidate["input_sdf_index"]
        sources: list[str] = []
        for method, score_names, path_names in (
            ("af3", ("af3_mpae", "mpae"), ("model_cif", "af3_model_cif")),
            ("covdock", ("covdock_score",), ("covdock_pose_file",)),
        ):
            status = _method_status(row, method)
            score = _score(row, score_names)
            reason = _text(row.get(f"{method}_failure_reason") or row.get("failure_reason"))
            if status == "SUCCESS" and not score:
                raise ValueError(f"{method} SUCCESS requires a finite score for {candidate['compound_id']}")
            if status != "SUCCESS" and score:
                raise ValueError(f"{method} {status} has a numeric score for {candidate['compound_id']}")
            if status == "FAILED" and not reason:
                raise ValueError(f"{method} FAILED requires a reason for {candidate['compound_id']}")
            candidate[f"{method}_status"] = status
            candidate[score_names[0]] = score
            candidate[f"{method}_failure_reason"] = reason if status == "FAILED" else ""
            structure = next((_text(row.get(name)) for name in path_names if _text(row.get(name))), "")
            if status == "SUCCESS":
                if not structure:
                    raise ValueError(f"{method} SUCCESS lacks a structure for {candidate['compound_id']}")
                candidate[f"{method}_asset"] = _asset(assets, method, index, structure, asset_resolver)
                sources.append(method)
                if method == "covdock":
                    poses = [exact_pose_index(row.get(name))
                             for name in ("covdock_pose_index", "pose_index")
                             if _text(row.get(name))]
                    if not poses or len(set(poses)) != 1:
                        raise ValueError(f"CovDock pose index is missing or conflicting for {candidate['compound_id']}")
                    candidate["covdock_structure_pose"] = str(poses[0])
            elif structure:
                raise ValueError(f"{method} non-success row must not publish a structure")
        generated = _text(row.get("generated_pose_file"))
        if generated:
            candidate["generated_asset"] = _asset(assets, "generated", index, generated, asset_resolver)
            sources.append("generated")
        candidate["structure_source"] = "+".join(sources) if sources else "none"
        for field in DISPLAY_FIELDS:
            if field in frame.columns:
                candidate[field] = _text(row.get(field))
        candidates.append(candidate)

    report_documents = {
        role: _document(role, path) for role, path in (documents or {}).items()
    }
    return {
        "schema": "candidate-results-v2",
        "route": route,
        "task_id": task_id,
        "assets": assets,
        "documents": report_documents,
        "candidates": candidates,
    }


def write_candidate_results_manifest(payload: dict[str, Any], output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
