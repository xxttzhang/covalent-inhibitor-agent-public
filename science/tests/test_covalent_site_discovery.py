import hashlib
import json

import pytest

from covscreen.covalent_site_discovery import (
    EVALUATION_SCHEMA,
    PREDICTION_ENGINE,
    PREDICTION_SCHEMA,
    SELECTION_SCHEMA,
    _prediction_request,
    evaluate_covalent_sites_tool,
    select_covalent_sites_tool,
)
from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.target_hypothesis import digest, folder, save


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prediction(root):
    run_id = "deepcosi-test-run"
    registry = ArtifactRegistry(root)
    spec = JobSpec(run_id=run_id, request_id="deepcosi-test-request",
        project_id="receptor-input-" + "a" * 32, route="shared",
        engine=PREDICTION_ENGINE, command=["deepcosi"], working_directory=str(root),
        output_directory=str(registry.job_dir(run_id) / "artifacts"),
        resource_budget=ResourceBudget(max_runtime_minutes=20, cpu_count=2,
            memory_gb=8, max_candidates=100))
    result = RunResult(run_id=run_id, state="COMPLETED", engine=PREDICTION_ENGINE,
        route="shared", project_id=spec.project_id, submitted_at=utc_now(),
        finished_at=utc_now(), message="completed", stdout_path="stdout.log",
        stderr_path="stderr.log")
    registry.create(spec, result)
    output = registry.job_dir(run_id) / "artifacts/site-prediction"
    output.mkdir(parents=True)
    record = {"schema": PREDICTION_SCHEMA, "status": "COMPLETED",
        "target": {"gene": "EGFR", "taxon": 9606, "uniprot": "P00533"},
        "structure": {"entry": "1M17", "model": 1, "author_chain": "A"},
        "candidates": [
            {"author_chain": "A", "author_residue_id": "773", "canonical_position": 797,
             "residue_name": "CYS", "reactive_atom": "SG", "model_score": 0.82,
             "blockers": []},
            {"author_chain": "A", "author_residue_id": "751", "canonical_position": 775,
             "residue_name": "CYS", "reactive_atom": "SG", "model_score": 0.63,
             "blockers": []},
            {"author_chain": "A", "author_residue_id": "999", "canonical_position": None,
             "residue_name": "CYS", "reactive_atom": "SG", "model_score": 0.91,
             "blockers": ["canonical_mapping_unavailable"]},
        ]}
    manifest = output / "site-prediction.json"
    manifest.write_text(json.dumps(record) + "\n")
    return run_id, manifest


def evidence(root):
    identifier = "hyp-" + "b" * 24
    output = folder(root, identifier)
    output.mkdir(parents=True)
    save(output / "evidence.json", {"target": "EGFR", "site": "C797", "taxon": 9606,
        "identity": {"uniprot_id": "P00533", "canonical_cys_verified": True},
        "literature": [{"id": "PMID:test", "title": "test"}]})
    save(output / "manifest.json", {"files": {"evidence.json": digest(output / "evidence.json")}})
    return identifier


def test_prediction_request_is_shared_and_bounded():
    request = _prediction_request({"receptor_input_artifact_id": "receptor-input-" + "a" * 32,
        "manifest_sha256": "b" * 64}, execution_mode="plan")
    assert request.route == "shared"
    assert request.engine == PREDICTION_ENGINE
    assert request.execution_mode == "plan"
    assert request.resource_budget.gpu_count == 0
    assert request.human_approved is False


def test_evaluate_and_select_sites_are_immutable_and_route_neutral(tmp_path):
    run_id, manifest = prediction(tmp_path)
    evidence_id = evidence(tmp_path)
    evaluated = evaluate_covalent_sites_tool(tmp_path, {"prediction_run_id": run_id,
        "prediction_manifest_sha256": sha(manifest),
        "hypothesis_evidence_ids": [evidence_id]})
    assert evaluated["result"]["schema"] == EVALUATION_SCHEMA
    rows = evaluated["result"]["candidates"]
    assert [row["canonical_position"] for row in rows] == [797, 775, None]
    assert rows[0]["evidence_status"] == "supported"
    assert rows[1]["evidence_status"] == "unknown"
    assert rows[2]["eligible_for_exploratory_selection"] is False
    repeated = evaluate_covalent_sites_tool(tmp_path, {"prediction_run_id": run_id,
        "prediction_manifest_sha256": sha(manifest), "hypothesis_evidence_ids": [evidence_id]})
    assert repeated["artifact_id"] == evaluated["artifact_id"]
    selected = select_covalent_sites_tool(tmp_path, {
        "evaluation_artifact_id": evaluated["artifact_id"],
        "evaluation_manifest_sha256": evaluated["manifest_sha256"],
        "canonical_positions": [797, 775], "max_sites": 2,
        "routes": ["library-first", "ligand-first", "de-novo"]})
    assert selected["result"]["schema"] == SELECTION_SCHEMA
    assert [row["site"] for row in selected["result"]["selected_sites"]] == ["C797", "C775"]
    assert {row["route"] for row in selected["result"]["route_handoffs"]} == {
        "library-first", "ligand-first", "de-novo"}
    assert selected["result"]["campaign_registered"] is False
    assert selected["result"]["calculation_submitted"] is False


def test_site_evaluation_and_selection_fail_closed(tmp_path):
    run_id, manifest = prediction(tmp_path)
    with pytest.raises(ValueError, match="checksum"):
        evaluate_covalent_sites_tool(tmp_path, {"prediction_run_id": run_id,
            "prediction_manifest_sha256": "0" * 64})
    evaluated = evaluate_covalent_sites_tool(tmp_path, {"prediction_run_id": run_id,
        "prediction_manifest_sha256": sha(manifest)})
    with pytest.raises(ValueError, match="eligible"):
        select_covalent_sites_tool(tmp_path, {"evaluation_artifact_id": evaluated["artifact_id"],
            "evaluation_manifest_sha256": evaluated["manifest_sha256"],
            "canonical_positions": [999], "max_sites": 1})


def test_no_literature_allows_model_based_exploratory_selection(tmp_path):
    # Synthetic completed prediction: no actual DeepCoSI execution in this test.
    run_id, manifest = prediction(tmp_path)
    evaluated = evaluate_covalent_sites_tool(tmp_path, {
        "prediction_run_id": run_id, "prediction_manifest_sha256": sha(manifest)})
    assert all(row["evidence_status"] == "unknown" for row in evaluated["result"]["candidates"])
    assert evaluated["result"]["candidates"][0]["eligible_for_exploratory_selection"]
    selected = select_covalent_sites_tool(tmp_path, {
        "evaluation_artifact_id": evaluated["artifact_id"],
        "evaluation_manifest_sha256": evaluated["manifest_sha256"]})
    assert selected["result"]["selected_sites"][0]["site"] == "C797"
    assert selected["result"]["selected_sites"][0]["evidence_status"] == "unknown"
    assert selected["result"]["selection_mode"] == "top_ranked_exploratory"
    assert selected["calculation_submitted"] is False
