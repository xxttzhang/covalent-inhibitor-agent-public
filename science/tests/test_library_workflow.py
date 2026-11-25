"""Route navigation with captured real public source bytes, not fake science."""
import hashlib
import io
import json
from pathlib import Path
import sys
import sysconfig
import time

import pytest
from jsonschema import validate

from covscreen.mcp.server import dispatch
from covscreen.mcp.registry import TOOLS
from covscreen.library_workflow import ENTRY, RESEARCH, RECEPTOR
from covscreen.workflow_store import load, save
from test_target_acquisition_handoff import handoff as discovery_fixture

ROOT = Path(__file__).resolve().parents[2]
TARGET_ID = json.loads((Path(__file__).parent / "fixtures/target_discovery/egfr-manifest.json").read_text())["plan"]["target_artifact_id"]
TARGET_SUFFIX = TARGET_ID.removeprefix("target-")


def call(root, result, tool, action, **args):
    view = result["workflow"]
    value = dispatch(root, tool, {"workflow_id": view["workflow_id"], "expected_revision": view["revision"], "action": action, **args})
    validate(value, next(t["outputSchema"] for t in TOOLS if t["name"] == tool))
    return value


def seed(root):
    """Reproduce acquisition using verified captured HTTP response bytes only."""
    from covscreen import structure_acquisition
    discovery_fixture.__wrapped__(root)
    fixtures = Path(__file__).parent / "fixtures/target_acquisition"
    sources = json.loads((fixtures / "source.json").read_text())
    pdb = Path(__file__).parent / "fixtures/structures/1M17.pdb"
    responses = {row["url"]: (pdb if row["path"] == "structure.pdb" else fixtures / row["path"]).read_bytes() for row in sources}
    assert all(hashlib.sha256(responses[row["url"]]).hexdigest() == row["sha256"] for row in sources)
    start = dispatch(root, ENTRY, {"action": "start", "request_id": "library-software-fixture", "target": "EGFR", "taxon": 9606})
    resolved = call(root, start, RESEARCH, "resolve_target", target_artifact_id=TARGET_ID,
        target_manifest_sha256=hashlib.sha256((root / "state/target_identity" / TARGET_SUFFIX / "target_identity.json").read_bytes()).hexdigest())
    selected = call(root, resolved, ENTRY, "select_site", site="C797", reason="Explore the verified canonical cysteine; no reactivity claim")
    discovered = call(root, selected, RESEARCH, "discover_structures", discovery_artifact_id="structures-" + "c" * 32,
        discovery_manifest_sha256=hashlib.sha256((root / "state/target_structures" / ("c" * 32) / "target_structure_discovery.json").read_bytes()).hexdigest())
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr(structure_acquisition.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(responses[url]))
        acquired = call(root, discovered, RESEARCH, "acquire_structure", entry="1M17")
    return acquired


def ready(root, backend="local"):
    acquired = seed(root)
    runtime = Path(sys.executable)
    if not runtime.is_file():
        raise RuntimeError("An installed Python runtime is required for the software plan fixture")
    config = root / "configs/local.inspire.yaml"
    config.parent.mkdir()
    config.write_text("receptor_preparation:\n  enabled: true\n" +
        f"  audit_python: {sys.executable}\n  openmm_python: {runtime}\n" +
        f"  openmm_pythonpath: {sysconfig.get_path('purelib')}\n" +
        f"  pdbfixer_source: bundled\nruntime:\n  worker_backend: {backend}\n")
    return call(root, acquired, RECEPTOR, "plan", altlocs={"751": "A", "831": "A"})


def state_file(root, result):
    return root / "state/library-workflows" / result["workflow"]["workflow_id"]


def test_actual_source_chain_binds_identity_coordinates_and_scoped_plan_without_execution(tmp_path):
    result = ready(tmp_path)
    assert result["workflow"]["stage"] == "RECEPTOR_PLAN_READY"
    assert result["execution"]["state"] == "not_submitted"
    assert result["assessment"]["state"] == "not_evaluated"
    assert result["result"]["spec"]["request_id"].startswith(result["workflow"]["workflow_id"] + "-receptor-r")
    state = load(state_file(tmp_path, result))
    assert state["target_identity"]["id"] == TARGET_ID
    assert state["site_selection"]["reactivity_validated"] is False
    assert state["receptor_plan"]["spec"]["engine"] == "library-first-receptor-preparation"
    assert not list((tmp_path / "state/jobs").glob("*/spec.json"))
    assert not (tmp_path / "state/submissions").exists()
    prior = (state_file(tmp_path, result) / "state.json").read_bytes()
    with pytest.raises(ValueError, match="Native user approval"):
        call(tmp_path, result, RECEPTOR, "start", human_approved=True, approval_note="Model-supplied flag is insufficient")
    assert (state_file(tmp_path, result) / "state.json").read_bytes() == prior


def test_reconsider_preserves_parents_and_archives_only_dependent_assets(tmp_path):
    acquired = seed(tmp_path)
    previous = load(state_file(tmp_path, acquired))
    reconsidered = call(tmp_path, acquired, ENTRY, "reconsider", return_to="structure", reason="Review another deposited structure and its construct")
    current = load(state_file(tmp_path, acquired))
    assert current["target_identity"] == previous["target_identity"]
    assert current["site_selection"] == previous["site_selection"]
    assert current["discovery"] is None and current["receptor_input"] is None
    assert current["branches"][0]["receptor_input"] == previous["receptor_input"]
    assert reconsidered["workflow"]["stage"] == "STRUCTURE_REVIEW"
    inspected = call(tmp_path, reconsidered, ENTRY, "inspect_branch", branch_index=0)
    assert inspected["result"]["active_handoff_restored"] is False
    with pytest.raises(ValueError, match="STALE_WORKFLOW_REVISION"):
        call(tmp_path, acquired, RESEARCH, "read_structure")


def test_rehashed_parent_substitution_or_cross_species_scope_does_not_advance_workflow(tmp_path):
    result = seed(tmp_path)
    folder = state_file(tmp_path, result)
    state = load(folder)
    acquired = tmp_path / "state/target_acquisition" / state["receptor_input"]["id"].removeprefix("receptor-input-") / "target_acquisition.json"
    value = json.loads(acquired.read_text())
    value["parents"]["discovery_artifact_id"] = "structures-" + "d" * 32
    acquired.write_text(json.dumps(value))
    state["receptor_input"]["sha256"] = hashlib.sha256(acquired.read_bytes()).hexdigest()
    save(folder, state)
    with pytest.raises(ValueError, match="another selected structure"):
        call(tmp_path, result, RESEARCH, "read_structure")
    state["target"]["taxon"] = 10090
    save(folder, state)
    with pytest.raises(ValueError, match="another gene/species"):
        call(tmp_path, result, RESEARCH, "read_target")


def test_intake_idempotence_stage_gate_and_raw_source_tampering(tmp_path):
    start = dispatch(tmp_path, ENTRY, {"action": "start", "request_id": "fixed", "target": "EGFR", "taxon": 9606})
    assert dispatch(tmp_path, ENTRY, {"action": "start", "request_id": "fixed", "target": "EGFR", "taxon": 9606}) == start
    with pytest.raises(ValueError, match="another scientific target"):
        dispatch(tmp_path, ENTRY, {"action": "start", "request_id": "fixed", "target": "BTK", "taxon": 9606})
    with pytest.raises(ValueError, match="unavailable"):
        call(tmp_path, start, RECEPTOR, "plan")
    with pytest.raises(ValueError, match="unexpected arguments"):
        call(tmp_path, start, RESEARCH, "resolve_target", target="BTK")
    acquired = seed(tmp_path)
    target = tmp_path / "state/target_identity" / TARGET_SUFFIX / "uniprot.json"
    target.write_bytes(target.read_bytes() + b"corrupt")
    with pytest.raises(ValueError):
        call(tmp_path, acquired, RESEARCH, "read_structure")


def test_new_altloc_decision_invalidates_the_previous_preparation_plan(tmp_path):
    ready_result = ready(tmp_path)
    selection = call(tmp_path, ready_result, RECEPTOR, "plan")
    assert selection["result"]["status"] == "ALTERNATE_LOCATION_SELECTION_REQUIRED"
    assert selection["workflow"]["stage"] == "RECEPTOR_INPUT_READY"
    assert load(state_file(tmp_path, selection))["receptor_plan"] is None
    assert "start" not in selection["workflow"]["tools"][RECEPTOR]["actions"]


def test_uncertain_submission_without_receipt_remains_pending_and_never_relaunches(tmp_path):
    planned = ready(tmp_path)
    folder = state_file(tmp_path, planned)
    state = load(folder)
    state["pending"] = {"tool": RECEPTOR, "action": "start", "operation_id": "f" * 32,
        "submission": {"project_id": state["receptor_input"]["id"], "request_id": state["receptor_plan"]["request_id"], "execution_mode": "execute"}}
    save(folder, state)
    observed = call(tmp_path, planned, ENTRY, "reconcile")
    assert observed["result"]["submission_state"] == "NO_RECEIPT"
    assert observed["execution"]["state"] == "outcome_unknown"
    assert observed["pending"] == state["pending"]
    assert RECEPTOR not in observed["workflow"]["tools"]
    assert not list((tmp_path / "state/jobs").glob("*/spec.json"))


def test_interrupted_local_evidence_operation_restores_committed_assets_without_adopting_partial_output(tmp_path):
    from covscreen.workflow_protocol import begin_operation
    started = dispatch(tmp_path, ENTRY, {"action": "start", "request_id": "interrupted", "target": "EGFR", "taxon": 9606})
    folder = state_file(tmp_path, started)
    state = load(folder)
    begin_operation(folder, state, RESEARCH, "resolve_target")
    observed = dispatch(tmp_path, ENTRY, {"action": "status", "workflow_id": state["workflow_id"]})
    assert "reconcile" in observed["workflow"]["tools"][ENTRY]["actions"]
    assert RESEARCH not in observed["workflow"]["tools"]
    reconciled = call(tmp_path, observed, ENTRY, "reconcile")
    assert reconciled["result"]["partial_artifacts_adopted"] is False
    assert reconciled["result"]["automatic_retry"] is False
    assert reconciled["workflow"]["tools"][RESEARCH]["actions"] == ["resolve_target"]
    assert load(folder)["target_identity"] is None
    assert not (tmp_path / "state/jobs").exists()


@pytest.mark.parametrize("backend", ["local", "external-hpc"])
def test_reconciliation_accepts_only_exact_signed_job_and_preserves_unknown_submission(tmp_path, monkeypatch, backend):
    """Register a synthetic queued receipt; exercise real sealing/lookup, never a worker."""
    from covscreen.execution import JobManager
    from covscreen.jobs import RunResult, utc_now
    from covscreen.approval_scope import approval_scope
    from covscreen.native_approval import CURRENT, signed
    from covscreen.library_workflow import receptor_start_request
    from covscreen.submission_identity import submit_once
    planned = ready(tmp_path, backend)
    folder = state_file(tmp_path, planned)
    state = load(folder)
    args = {"action": "start", "workflow_id": state["workflow_id"], "expected_revision": state["revision"],
        "human_approved": True, "approval_note": "Synthetic signed registration; no computation"}
    key = tmp_path / "test-only.key"
    key.write_bytes(b"isolated-reconciliation-test-key-123456")
    key.chmod(0o600)
    monkeypatch.setenv("COVALENT_APPROVAL_KEY_FILE", str(key))
    scope = approval_scope(tmp_path, RECEPTOR, args)
    request = receptor_start_request(tmp_path, args)
    manager = JobManager(tmp_path)
    token = CURRENT.set(signed({"scope": scope}))
    try:
        spec = manager._prepare_submission(request)
        assert spec.provenance['worker_backend'] == 'local'
    finally:
        CURRENT.reset(token)
    result = RunResult(run_id=spec.run_id, state="QUEUED", engine=spec.engine, route=spec.route,
        project_id=spec.project_id, attempt=spec.attempt, parent_run_id=spec.parent_run_id,
        submitted_at=utc_now(), stdout_path=f"state/jobs/{spec.run_id}/stdout.log",
        stderr_path=f"state/jobs/{spec.run_id}/stderr.log",
        message="Synthetic queued registry fixture; no worker launched")
    def register(value):
        manager.registry.create(spec, result)
        return result
    submit_once(manager, request, register, planned_run_id=spec.run_id)
    state["receptor_plan"]["approved_scope"] = scope
    state["pending"] = {"tool": RECEPTOR, "action": "start", "operation_id": "e" * 32,
        "submission": {"project_id": request.project_id, "request_id": request.request_id, "execution_mode": "execute"}}
    save(folder, state)
    # Even a valid signed job must belong to the exact workflow approval.
    state["receptor_plan"]["approved_scope"] = {**scope, "runtime_sha256": "f" * 64}
    save(folder, state)
    with pytest.raises(ValueError, match="exact native approval"):
        call(tmp_path, planned, ENTRY, "reconcile")
    assert load(folder)["pending"] is not None
    state["receptor_plan"]["approved_scope"] = scope
    save(folder, state)
    recovered = call(tmp_path, planned, ENTRY, "reconcile")
    assert recovered["pending"] is None
    assert recovered["workflow"]["stage"] == "RECEPTOR_PREPARATION"
    assert recovered["execution"]["task_ids"] == [spec.run_id]
    assert recovered["execution"]["state"] == "submitted"
    assert len(list((tmp_path / "state/jobs").glob("*/spec.json"))) == 1


def test_interruption_at_submit_boundary_already_has_durable_scope_and_lookup_identity(tmp_path, monkeypatch):
    from covscreen.execution import JobManager
    from covscreen.approval_scope import approval_scope
    from covscreen.native_approval import signed
    planned = ready(tmp_path)
    folder = state_file(tmp_path, planned)
    state = load(folder)
    args = {"action": "start", "workflow_id": state["workflow_id"], "expected_revision": state["revision"],
        "human_approved": True, "approval_note": "Fault injection before worker registration; no computation"}
    key = tmp_path / "test-only.key"
    key.write_bytes(b"isolated-submit-boundary-test-key-123456")
    key.chmod(0o600)
    monkeypatch.setenv("COVALENT_APPROVAL_KEY_FILE", str(key))
    scope = approval_scope(tmp_path, RECEPTOR, args)
    proof = signed({"tool": RECEPTOR, "arguments_json": json.dumps(args), "scope": scope,
        "session": "synthetic-test-session", "message": "synthetic-test-message", "approval_request_id": "permission_test",
        "nonce": "b" * 32, "expires": time.time() + 300})
    class SimulatedProcessLoss(BaseException):
        pass
    def interrupt(manager, request):
        committed = load(folder)
        assert committed["receptor_plan"]["approved_scope"] == scope
        assert committed["pending"]["submission"] == {
            "project_id": request.project_id, "request_id": request.request_id, "execution_mode": "execute"}
        raise SimulatedProcessLoss()
    monkeypatch.setattr(JobManager, "submit", interrupt)
    with pytest.raises(SimulatedProcessLoss):
        dispatch(tmp_path, RECEPTOR, {**args, "_native_approval": proof})
    restored = call(tmp_path, planned, ENTRY, "reconcile")
    assert restored["result"]["submission_state"] == "NO_RECEIPT"
    assert restored["pending"]["submission"]["request_id"] == state["receptor_plan"]["request_id"]
    assert restored["execution"]["state"] == "outcome_unknown"
    assert not list((tmp_path / "state/jobs").glob("*/spec.json"))
