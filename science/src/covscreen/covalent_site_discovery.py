"""Shared, artifact-bound cysteine-site prediction, evaluation and selection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .jobs import JobSubmissionRequest, ResourceBudget, sha256_file
from .site_contracts import EvaluationInput, SelectionInput, validate_prediction


PREDICTION_ENGINE = "shared-deepcosi-site-prediction"
PREDICTION_SCHEMA = "deepcosi-site-prediction-artifact-v1"
EVALUATION_SCHEMA = "covalent-site-evaluation-artifact-v1"
SELECTION_SCHEMA = "covalent-site-selection-artifact-v1"


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def _safe_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


def _prediction_request(arguments: dict[str, Any], *, execution_mode: str) -> JobSubmissionRequest:
    arguments = validate_prediction(arguments)
    if arguments["action"] == "status":
        raise ValueError("status cannot create an execution request")
    artifact = arguments["receptor_input_artifact_id"]
    if not isinstance(artifact, str) or not re.fullmatch(r"receptor-input-[a-f0-9]{32}", artifact):
        raise ValueError("Use a verified receptor-input artifact")
    manifest = _safe_sha(arguments["manifest_sha256"], "manifest_sha256")
    timeout = arguments["native_timeout_seconds"]
    payload = {
        "receptor_input_artifact_id": artifact,
        "manifest_sha256": manifest,
        "native_timeout_seconds": timeout,
    }
    identity = hashlib.sha256(_canonical_json(payload)).hexdigest()[:20]
    budget = ResourceBudget.model_validate(arguments["resource_budget"])
    return JobSubmissionRequest(
        request_id=f"deepcosi-site-prediction-{identity}",
        project_id=artifact,
        route="shared",
        engine=PREDICTION_ENGINE,
        parameters=payload,
        resource_budget=budget,
        execution_mode=execution_mode,
        human_approved=arguments.get("human_approved", False),
        approval_note=arguments.get("approval_note"),
    )


def predict_cysteine_sites_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile, start or inspect one approved DeepCoSI prediction."""
    arguments = validate_prediction(arguments)
    from .execution import JobManager
    action = arguments["action"]
    manager = JobManager(root)
    if action == "status":
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("status requires run_id")
        spec = manager.registry.load_spec(run_id)
        if spec.engine != PREDICTION_ENGINE:
            raise ValueError("run_id is not a DeepCoSI site-prediction job")
        result = manager.registry.load_result(run_id)
        response = {"status": "PASS", "spec": spec.model_dump(mode="json"),
                    "job": result.model_dump(mode="json")}
        manifest = manager.registry.job_dir(run_id) / "artifacts/site-prediction/site-prediction.json"
        if manifest.is_file() and not manifest.is_symlink():
            response.update(prediction=json.loads(manifest.read_text()),
                            prediction_manifest_sha256=sha256_file(manifest))
        return response
    if action not in {"plan", "start"}:
        raise ValueError("action must be plan, start, or status")
    request = _prediction_request(arguments,
        execution_mode="plan" if action == "plan" else "execute")
    if action == "plan":
        spec = manager.build_spec(request)
        return {"status": "PLANNED", "spec": spec.model_dump(mode="json"),
                "model_output_interpretation": "uncalibrated ranking score",
                "calculation_submitted": False}
    result = manager.submit(request)
    spec = manager.registry.load_spec(result.run_id)
    return {"status": "QUEUED", "spec": spec.model_dump(mode="json"),
            "job": result.model_dump(mode="json"), "calculation_submitted": True}


def _completed_prediction(root: Path, run_id: str, manifest_sha256: str) -> tuple[dict, Path]:
    from .execution import JobManager
    manager = JobManager(root)
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    if spec.engine != PREDICTION_ENGINE or result.state != "COMPLETED":
        raise ValueError("A completed DeepCoSI site-prediction job is required")
    path = manager.registry.job_dir(run_id) / "artifacts/site-prediction/site-prediction.json"
    if path.is_symlink() or not path.is_file() or sha256_file(path) != _safe_sha(
            manifest_sha256, "prediction_manifest_sha256"):
        raise ValueError("Prediction manifest checksum mismatch")
    value = json.loads(path.read_text())
    if value.get("schema") != PREDICTION_SCHEMA or value.get("status") != "COMPLETED":
        raise ValueError("Prediction artifact is not complete")
    return value, path


def evaluate_covalent_sites_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Rank model-scored sites while preserving evidence gaps and conflicts."""
    arguments = EvaluationInput.model_validate(arguments).model_dump()
    prediction, manifest = _completed_prediction(Path(root), arguments["prediction_run_id"],
                                                  arguments["prediction_manifest_sha256"])
    from .target_hypothesis import read as read_hypothesis
    evidence_by_site: dict[str, list[dict[str, Any]]] = {}
    evidence_refs = arguments.get("hypothesis_evidence_ids", [])
    if not isinstance(evidence_refs, list) or any(not isinstance(x, str) for x in evidence_refs):
        raise ValueError("hypothesis_evidence_ids must be a list of evidence IDs")
    for evidence_id in evidence_refs:
        item = read_hypothesis(Path(root), evidence_id)
        if item.get("state") != "EVIDENCE_READY":
            raise ValueError("Hypothesis evidence is not ready: " + evidence_id)
        evidence = item["evidence"]
        if (evidence.get("target") != prediction["target"]["gene"]
                or evidence.get("taxon") != prediction["target"]["taxon"]):
            raise ValueError("Hypothesis evidence target differs from prediction")
        evidence_by_site.setdefault(evidence.get("site", ""), []).append({
            "evidence_id": evidence_id,
            "source_ids": sorted(item.get("source_index", {})),
            "canonical_cys_verified": bool(evidence.get("identity", {}).get("canonical_cys_verified")),
        })
    candidates = []
    for row in prediction["candidates"]:
        canonical = row.get("canonical_position")
        site = f"C{canonical}" if isinstance(canonical, int) else None
        evidence = evidence_by_site.get(site or "", [])
        blockers = list(row.get("blockers", []))
        if canonical is None:
            blockers.append("canonical_mapping_unavailable")
        source_ids = sorted({source for item in evidence for source in item["source_ids"]})
        evidence_dimensions = {
            "literature": [source for source in source_ids if source.startswith(("PMID:", "DOI:"))],
            "structure_database": [source for source in source_ids if source.startswith("PDB:")],
            "compound_database": [source for source in source_ids if source.startswith("ChEMBL:")],
        }
        evidence_support_count = sum(bool(values) for values in evidence_dimensions.values())
        score = row.get("model_score")
        candidates.append({**row, "site": site,
            "evidence_status": "supported" if evidence else "unknown",
            "evidence": evidence, "evidence_dimensions": evidence_dimensions,
            "evidence_support_count": evidence_support_count,
            "structural_evidence": {"cysteine_sg_present": "cysteine_sg_missing" not in blockers,
                "deepcosi_pocket_present": bool(row.get("pocket_sha256")),
                "pocket_atom_count": row.get("pocket_atom_count")},
            "blockers": sorted(set(blockers)),
            "eligible_for_exploratory_selection": canonical is not None
                and isinstance(score, (int, float)) and not blockers})
    candidates.sort(key=lambda x: (not x["eligible_for_exploratory_selection"],
        -float(x["model_score"] or 0), -x["evidence_support_count"],
        x.get("canonical_position") or 10**9,
        x["author_residue_id"]))
    for rank, row in enumerate(candidates, 1):
        row["rank"] = rank
    parent = {"prediction_run_id": arguments["prediction_run_id"],
              "prediction_manifest_sha256": sha256_file(manifest),
              "hypothesis_evidence_ids": sorted(evidence_refs)}
    identifier = hashlib.sha256(_canonical_json({"parent": parent, "candidates": candidates})).hexdigest()[:32]
    record = {"schema": EVALUATION_SCHEMA, "status": "EVALUATED_SELECTION_REQUIRED",
        "target": prediction["target"], "structure": prediction["structure"],
        "parents": parent, "candidates": candidates,
        "ranking_policy": ["exclude blocked or unscored sites from automatic selection",
            "rank eligible sites by uncalibrated DeepCoSI score",
            "use independent literature/PDB/ChEMBL support as a tie-breaker",
            "retain unknown evidence as unknown rather than negative"],
        "limitations": ["DeepCoSI scores are uncalibrated ranking values, not probabilities.",
            "Missing literature evidence is unknown, not negative evidence.",
            "A selected site remains a computational hypothesis until experimentally tested."],
        "campaign_ready": False, "calculation_submitted": False}
    output = Path(root).resolve() / "state/covalent_site_evaluation" / identifier
    path = output / "site-evaluation.json"
    raw = _canonical_json(record)
    if path.is_file() and not path.is_symlink():
        if path.read_bytes() != raw:
            raise ValueError("Existing site evaluation differs from deterministic result")
    else:
        output.mkdir(parents=True, exist_ok=False)
        path.write_bytes(raw)
    return {"status": "PASS", "artifact_id": "site-evaluation-" + identifier,
        "manifest_sha256": sha256_file(path), "result": record,
        "calculation_submitted": False}


def _load_evaluation(root: Path, artifact_id: str, manifest_sha256: str) -> tuple[dict, Path]:
    if not isinstance(artifact_id, str) or not re.fullmatch(r"site-evaluation-[a-f0-9]{32}", artifact_id):
        raise ValueError("Use a site-evaluation artifact ID")
    path = root.resolve() / "state/covalent_site_evaluation" / artifact_id[16:] / "site-evaluation.json"
    if path.is_symlink() or not path.is_file() or sha256_file(path) != _safe_sha(
            manifest_sha256, "evaluation_manifest_sha256"):
        raise ValueError("Site-evaluation manifest checksum mismatch")
    value = json.loads(path.read_text())
    if value.get("schema") != EVALUATION_SCHEMA or value.get("status") != "EVALUATED_SELECTION_REQUIRED":
        raise ValueError("Completed site evaluation is required")
    return value, path


def select_covalent_sites_tool(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Freeze explicit or ranked exploratory sites into a route-neutral handoff."""
    arguments = SelectionInput.model_validate(arguments).model_dump()
    evaluation, path = _load_evaluation(Path(root), arguments["evaluation_artifact_id"],
                                        arguments["evaluation_manifest_sha256"])
    positions = arguments.get("canonical_positions")
    max_sites = arguments["max_sites"]
    if not 1 <= max_sites <= 10:
        raise ValueError("max_sites must be between 1 and 10")
    eligible = [x for x in evaluation["candidates"] if x["eligible_for_exploratory_selection"]]
    if positions is None:
        selected = eligible[:max_sites]
        mode = "top_ranked_exploratory"
    else:
        if (not isinstance(positions, list) or not positions or len(positions) > max_sites
                or any(type(x) is not int or x < 1 for x in positions)
                or len(set(positions)) != len(positions)):
            raise ValueError("canonical_positions must contain unique positive positions within max_sites")
        by_position = {x["canonical_position"]: x for x in eligible}
        if any(x not in by_position for x in positions):
            raise ValueError("Every selected position must be eligible in the evaluation artifact")
        selected = [by_position[x] for x in positions]
        mode = "explicit_positions"
    if not selected:
        raise ValueError("No eligible candidate site can be selected")
    routes = arguments.get("routes", ["library-first", "ligand-first", "de-novo"])
    allowed = {"library-first", "ligand-first", "de-novo"}
    if not isinstance(routes, list) or not routes or set(routes) - allowed:
        raise ValueError("routes must be a nonempty subset of the three registered routes")
    parent = {"evaluation_artifact_id": arguments["evaluation_artifact_id"],
              "evaluation_manifest_sha256": sha256_file(path)}
    payload = {"parent": parent, "mode": mode, "routes": sorted(set(routes)),
               "sites": [{k: row[k] for k in ("site", "canonical_position", "author_chain",
                   "author_residue_id", "reactive_atom", "model_score", "rank", "evidence_status")}
                         for row in selected]}
    identifier = hashlib.sha256(_canonical_json(payload)).hexdigest()[:32]
    record = {"schema": SELECTION_SCHEMA, "status": "SITES_FROZEN_ROUTE_INPUT_READY",
        "target": evaluation["target"], "structure": evaluation["structure"],
        "parents": parent, "selection_mode": mode, "selected_sites": payload["sites"],
        "route_handoffs": [{"route": route, "target": evaluation["target"],
            "site_artifact_id": "site-selection-" + identifier,
            "sites": [row["site"] for row in payload["sites"]],
            "state": "ROUTE_INPUT_READY_ADDITIONAL_ROUTE_PREREQUISITES_MAY_APPLY"}
            for route in sorted(set(routes))],
        "limitations": ["Selection prioritizes an exploratory computation; it does not validate reactivity, activity or selectivity."],
        "campaign_registered": False, "calculation_submitted": False}
    output = Path(root).resolve() / "state/covalent_site_selection" / identifier
    manifest = output / "site-selection.json"
    raw = _canonical_json(record)
    if manifest.is_file() and not manifest.is_symlink():
        if manifest.read_bytes() != raw:
            raise ValueError("Existing site selection differs from deterministic result")
    else:
        output.mkdir(parents=True, exist_ok=False)
        manifest.write_bytes(raw)
    return {"status": "PASS", "artifact_id": "site-selection-" + identifier,
        "manifest_sha256": sha256_file(manifest), "result": record,
        "next_calls": ([{"tool": "covalent_library_first_plan_from_sites", "arguments": {
            "site_selection_artifact_id": "site-selection-" + identifier,
            "site_selection_manifest_sha256": sha256_file(manifest)}}]
            if "library-first" in routes else []),
        "calculation_submitted": False}
