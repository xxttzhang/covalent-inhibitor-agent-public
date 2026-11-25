"""Real workflow/approval/registry contracts over labelled software-only poses.

The submit fault boundary registers a real signed queued record, never a native
engine. Completed output fixtures test provenance checking, not graph science.
"""
import json
import importlib.util
from pathlib import Path
import time
from uuid import uuid4

import pytest
from jsonschema import validate
from covscreen.mcp.server import dispatch
from covscreen.mcp.registry import TOOLS
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.workflow_store import load
from covscreen.native_approval import signed, needs_approval
from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.jobs import RunResult, utc_now, sha256_file
from covscreen.submission_identity import submit_once
from test_workflow_md_transfer import research, ready, start, upload, send


def call(fixture, action, **args):
    root, folder, _, _ = fixture
    state = load(folder)
    tool = ENTRY if action == "reconcile" else RESEARCH
    response = dispatch(root, tool, {"action": action, "workflow_id": state["workflow_id"],
        "expected_revision": state["revision"], **args})
    validate(response, next(t["outputSchema"] for t in TOOLS if t["name"] == tool))
    return response


def frozen(fixture):
    files, report, _ = ready(fixture)
    choice = start(fixture)
    upload(fixture, files)
    send(fixture, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=report)
    root = fixture[0]
    runtime = root / "runtime"
    runtime.mkdir()
    (runtime / "vendor").mkdir()
    (runtime / "lib").mkdir()
    (runtime / "license").write_text("SOFTWARE TEST PLACEHOLDER")
    launcher = runtime / "run"
    launcher.write_text("#!/bin/sh\nprintf '%s\\n' '{\"runtime\":\"schrodinger-python\",\"module\":\"schrodinger\"}'\n")
    launcher.chmod(0o700)
    config = root / "configs/local.inspire.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("adduct_parameterization:\n  enabled: true\n"
        f"  audit_python: {Path(__file__).parents[2] / '.tooling/design-route-validation/bin/python'}\n"
        f"  launcher: {launcher}\n  vendor_root: {runtime / 'vendor'}\n"
        f"  license_file: {runtime / 'license'}\n  runtime_lib_dir: {runtime / 'lib'}\n"
        "runtime:\n  worker_backend: local\n")
    return load(fixture[1])


def approval(fixture, action="start_md_admission"):
    state = load(fixture[1])
    args = {"action": action, "workflow_id": state["workflow_id"],
        "expected_revision": state["revision"], "human_approved": True,
        "approval_note": "Software-only signed fixture; no scientific worker"}
    scope = approval_scope(fixture[0], RESEARCH, args)
    proof = signed({"tool": RESEARCH, "arguments_json": json.dumps(args), "scope": scope,
        "session": "test", "message": "test", "approval_request_id": "permission_test",
        "nonce": uuid4().hex, "expires": time.time() + 300})
    return {**args, "_native_approval": proof}


def register(manager, request):
    spec = manager._prepare_submission(request)
    result = RunResult(run_id=spec.run_id, state="QUEUED", engine=spec.engine, route=spec.route,
        project_id=spec.project_id, parent_run_id=spec.parent_run_id, attempt=spec.attempt,
        submitted_at=utc_now(), stdout_path="unused", stderr_path="unused",
        message="Software-only registration; no worker or native graph inspection")
    def save(_request):
        manager.registry.create(spec, result)
        return result
    return submit_once(manager, request, save, planned_run_id=spec.run_id)


def test_plan_projects_only_bound_actions_and_rejects_identity_substitution(research):
    before = frozen(research)
    result = call(research, "plan_md_admission")
    assert result["workflow"]["stage"] == "MD_ADMISSION_PLAN_READY"
    assert result["execution"]["state"] == "not_submitted"
    assert result["workflow"]["tools"][RESEARCH]["actions"] == ["plan_md_admission", "start_md_admission", "release_md_stage"]
    assert "candidate_pose_artifact_id" not in result["workflow"]["tools"][RESEARCH]["fields"]
    assert result["md_execution"]["input"] == before["md_input"]
    assert result["md_execution"]["md_system_ready"] is False
    assert len(list((research[0] / "state/jobs").iterdir())) == 1  # receiving-receptor fixture only
    with pytest.raises(ValueError, match="owns"):
        call(research, "plan_md_admission", candidate_pose_artifact_id="another")
    with pytest.raises(ValueError, match="receipt required"):
        call(research, "start_md_admission", human_approved=True)
    assert needs_approval(RESEARCH, {"action": "start_md_admission"})
    assert not needs_approval(RESEARCH, {"action": "plan_md_admission"})
    old = load(research[1])["md_execution"]["plan"]
    call(research, "plan_md_admission", native_timeout_seconds=600)
    state = load(research[1])
    assert state["branches"][-1]["md_execution"]["plan"] == old
    assert state["md_execution"]["plan"]["request_id"] != old["request_id"]


def test_lost_start_receipt_recovers_signed_original_job_without_replay(research, monkeypatch):
    frozen(research)
    call(research, "plan_md_admission")
    def interrupted(manager, request):
        pending = load(research[1])["pending"]
        assert pending["submission"]["request_id"] == request.request_id
        register(manager, request)
        raise ConnectionError("Lost software registration reply")
    monkeypatch.setattr(JobManager, "submit", interrupted)
    args = approval(research)
    with pytest.raises(ConnectionError, match="Lost software"):
        dispatch(research[0], RESEARCH, args)
    uncertain = load(research[1])
    assert uncertain["pending"]["action"] == "start_md_admission"
    recovered = call(research, "reconcile")
    assert recovered["pending"] is None
    assert recovered["execution"]["state"] == "submitted"
    run = recovered["md_execution"]["run_id"]
    assert call(research, "md_admission_status")["md_execution"]["run_id"] == run
    assert len(list((research[0] / "state/jobs").iterdir())) == 2
    with pytest.raises(ValueError, match="already consumed|unavailable|STALE"):
        dispatch(research[0], RESEARCH, args)
    with pytest.raises(ValueError):
        call(research, "plan_md_admission")


def test_unknown_submission_stays_pending_and_cannot_be_started_again(research, monkeypatch):
    frozen(research)
    call(research, "plan_md_admission")
    def interrupt(_manager, _request):
        raise ConnectionError("No observed registration")
    monkeypatch.setattr(JobManager, "submit", interrupt)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, approval(research))
    pending = load(research[1])["pending"]
    result = call(research, "reconcile")
    assert result["pending"] == pending
    assert result["result"]["submission_state"] == "NO_RECEIPT"
    assert result["execution"]["state"] == "outcome_unknown"
    assert RESEARCH not in result["workflow"]["tools"]


@pytest.mark.parametrize("corruption", ["input", "spec", "missing_output", "failed"])
def test_state_or_completion_cannot_replace_verified_graph_evidence(research, monkeypatch, corruption):
    frozen(research)
    call(research, "plan_md_admission")
    monkeypatch.setattr(JobManager, "submit", register)
    started = dispatch(research[0], RESEARCH, approval(research))
    run = started["md_execution"]["run_id"]
    manager = JobManager(research[0])
    if corruption == "input":
        state = load(research[1])
        source = research[0] / "state/candidate_pose_input" / state["md_input"]["artifact_id"][21:] / "covdock-source.maegz"
        source.write_bytes(b"changed frozen source")
    elif corruption == "spec":
        spec = manager.registry.load_spec(run)
        spec.request_id = "another-request"
        (manager.registry.job_dir(run) / "spec.json").write_text(spec.model_dump_json())
    else:
        job = manager.registry.load_result(run)
        job.state = "FAILED" if corruption == "failed" else "COMPLETED"
        job.return_code = 1 if corruption == "failed" else 0
        manager.registry.write_result(job)
    if corruption in {"input", "spec"}:
        with pytest.raises(ValueError):
            call(research, "md_admission_status")
        assert load(research[1])["md_execution"]["verification"] == "evidence_rejected"
    else:
        result = call(research, "md_admission_status")
        assert result["md_execution"]["admission"] is None
        assert result["md_execution"]["verification"] != "verified"
        assert result["workflow"]["stage"] == ("MD_ADMISSION_FAILED" if corruption == "failed" else "MD_ADMISSION_REVIEW")


def completed_output(fixture, run):
    """Explicit synthetic scientific evidence; actual admission/registry code."""
    return completed_graph_output(fixture[0], load(fixture[1])["md_input"], run)


def completed_graph_output(root, asset, run):
    """Create the same synthetic evidence from a route-independent pose ref."""
    from covscreen_library_first.md_candidate_pose import load_candidate_pose_input
    from covscreen_library_first.md_adduct_input import admit_covalent_adduct_input
    pose = load_candidate_pose_input(root, asset["artifact_id"], asset["manifest_sha256"])
    original = pose["manifest"]
    manager = JobManager(root)
    folder = manager.registry.job_dir(run) / "artifacts/md-adduct-admission"
    native = folder / "native-handoff"
    native.mkdir(parents=True)
    module = importlib.util.spec_from_file_location("adduct_contract_fixture",
        Path(__file__).parents[2] / "routes/library-first/tests/test_md_adduct_input.py")
    helpers = importlib.util.module_from_spec(module)
    module.loader.exec_module(helpers)
    values = helpers.inputs(native)
    candidate = json.loads(values[4].read_text())
    candidate.update(candidate_id=original["candidate_id"], compound_id=original["compound_id"], target=original["target"])
    candidate["pose"]["pose_index"] = original["pose_index"]
    candidate["parents"] = {key: original["parents"][key] for key in ("prepared_receptor_sha256", "receptor_context_sha256")}
    values[4].write_text(json.dumps(candidate))
    values[5] = sha256_file(values[4])
    qc = json.loads(values[2].read_text())
    qc.update(system_id=original["candidate_id"], candidate_evidence_sha256=values[5], **candidate["parents"])
    qc["target_atom"].update(chain=original["target"]["author_chain"], residue=original["target"]["author_residue"])
    values[2].write_text(json.dumps(qc)); values[3] = sha256_file(values[2])
    values[6:] = [pose["directory"] / "prepared-receptor.json", original["parents"]["prepared_receptor_sha256"],
                  pose["directory"] / "receptor-context.json", original["parents"]["receptor_context_sha256"]]
    admitted = admit_covalent_adduct_input(*values, folder / "adduct-input")
    handoff = {"schema": "candidate-md-handoff-artifact-v1", "status": "native_graph_qc_passed_admission_pending",
        "native_graph_qc_completed": True, "candidate_id": original["candidate_id"], "compound_id": original["compound_id"],
        "pose_index": original["pose_index"], "parents": {"candidate_pose_manifest_sha256": asset["manifest_sha256"]},
        "artifacts": {key: admitted["artifacts"][key] for key in ("adduct.mae", "graph-qc.json", "candidate-evidence.json")}}
    (native / "candidate-md-handoff.json").write_text(json.dumps(handoff))
    receipt = {"schema": "library-first-md-adduct-admission-v1", "status": "ADDUCT_ADMITTED_PARAMETERIZATION_PENDING",
        "candidate_id": original["candidate_id"], "system_id": admitted["system_id"], "native_graph_qc_completed": True,
        "parameterization_completed": False, "md_system_ready": False,
        "parents": {"candidate_pose_manifest_sha256": asset["manifest_sha256"],
            "native_handoff_manifest_sha256": sha256_file(native / "candidate-md-handoff.json")},
        "artifacts": {"adduct-input/adduct-input.json": sha256_file(folder / "adduct-input/adduct-input.json")}}
    (folder / "md-adduct-admission.json").write_text(json.dumps(receipt))
    result = manager.registry.load_result(run)
    result.state = "COMPLETED"; result.return_code = 0
    result.artifacts = [manager.registry.register_file(run, path, "other") for path in folder.rglob("*") if path.is_file()]
    manager.registry.write_result(result)
    return folder


@pytest.mark.parametrize("changed", ["adduct-input/adduct.mae", "md-adduct-admission.json", "native-handoff/candidate-md-handoff.json"])
def test_verified_native_receipt_replays_parents_and_loses_acceptance_when_bytes_change(research, monkeypatch, changed):
    frozen(research)
    call(research, "plan_md_admission")
    monkeypatch.setattr(JobManager, "submit", register)
    started = dispatch(research[0], RESEARCH, approval(research))
    output = completed_output(research, started["md_execution"]["run_id"])
    accepted = call(research, "md_admission_status")
    assert accepted["md_execution"]["verification"] == "verified"
    assert accepted["md_execution"]["admission"]["native_graph_qc_completed"] is True
    assert accepted["md_execution"]["md_system_ready"] is False
    assert accepted["assessment"]["state"] == "not_evaluated"
    witness = load(research[1])["md_execution"]["admission"]
    (output / changed).write_text("changed output")
    rejected = call(research, "md_admission_status")
    assert rejected["md_execution"]["verification"] == "evidence_rejected"
    assert rejected["md_execution"]["admission"] is None
    assert load(research[1])["md_execution"]["admission"] == witness  # historical witness retained, not accepted


@pytest.mark.parametrize("values", [{"run_id": "other"}, {"native_timeout_seconds": True},
    {"resource_budget": {"max_runtime_minutes": 60, "gpu_count": 1}},
    {"resource_budget": {"max_runtime_minutes": 61}}])
def test_admission_plan_rejects_unbound_identity_or_excess_resources(research, values):
    frozen(research)
    with pytest.raises(ValueError):
        call(research, "plan_md_admission", **values)
    assert not load(research[1]).get("md_execution")
