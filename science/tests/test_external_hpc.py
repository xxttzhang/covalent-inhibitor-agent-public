from pathlib import Path
import json
import pytest
from covscreen.external_hpc import inspect, reserve, fail, pending
from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import CURRENT, signed, seal_spec


def approved(spec):
    from covscreen.approval_scope import normalize_spec
    token = CURRENT.set(signed({'test': 'isolated host approval fixture', 'scope': {'kind': 'job', 'spec': normalize_spec(spec)}}))
    try:
        spec.provenance['native_approval'] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    return spec


@pytest.fixture
def queued(tmp_path, monkeypatch):
    key = tmp_path / 'approval.key'
    key.write_bytes(b'test-key-for-isolated-approval-12345')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    registry = ArtifactRegistry(tmp_path)
    spec = JobSpec(run_id="test-external-001", request_id="new-ligand-request",
        project_id="egfr-c797", route="ligand-first", engine="schrodinger-covdock-lo",
        command=["not-executed"], working_directory=str(tmp_path),
        output_directory=str(tmp_path / "outputs"), parameters={"input_row_count": 1},
        provenance={"worker_backend": "external-hpc"}, execution_mode="execute",
        human_approved=True, approval_note="Approved isolated queue test.",
        resource_budget=ResourceBudget(max_runtime_minutes=360, cpu_count=4, memory_gb=16,max_candidates=1))
    result = RunResult(run_id=spec.run_id, state="QUEUED", engine=spec.engine,
        route=spec.route, project_id=spec.project_id, submitted_at=utc_now(), message="queued",
        stdout_path="stdout", stderr_path="stderr")
    registry.create(approved(spec), result)
    return tmp_path, spec.run_id


def test_reservation_is_exclusive(queued):
    root, run = queued
    record = inspect(root, run)
    receipt = reserve(root, run, record["spec_sha256"])
    assert receipt["name"] == record["name"]
    with pytest.raises(FileExistsError):
        reserve(root, run, record["spec_sha256"])


def test_new_request_with_two_candidates_and_signed_resources(queued):
    root, run = queued
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run)
    spec.request_id = 'new-target-independent-request'
    spec.parameters['input_row_count'] = 2
    spec.resource_budget.max_candidates = 2
    spec.resource_budget.cpu_count = 8
    spec.resource_budget.memory_gb = 32
    registry.write_spec(approved(spec))
    record = inspect(root, run)
    assert record['budget']['cpu_count'] == 8
    assert reserve(root, run, record['spec_sha256'])['name'] == record['name']


def test_old_boolean_only_job_cannot_dispatch(queued):
    root, run = queued
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run)
    spec.provenance.pop('native_approval')
    registry.write_spec(spec)
    with pytest.raises(ValueError, match='no native approval'):
        inspect(root, run)


def test_candidate_api_distinguishes_pending_and_failed_results(queued):
    from covscreen.tool_api import get_job_candidates_tool
    root, run = queued
    response = get_job_candidates_tool(root, run)
    assert response["result_availability"] == "no_candidate_table_yet"
    assert not response["calculation_completed"]
    registry = ArtifactRegistry(root)
    result = registry.load_result(run)
    result.state = "FAILED"
    result.failure_reason = "test_startup_failure"
    registry.write_result(result)
    response = get_job_candidates_tool(root, run)
    assert response["result_availability"] == "no_candidate_table_produced"
    assert response["failure_reason"] == "test_startup_failure"
    table = registry.job_dir(run) / "partial.csv"
    table.write_text("compound_id,score\npartial,1\n")
    result.candidate_table_path = str(table.relative_to(root))
    registry.write_result(result)
    response = get_job_candidates_tool(root, run)
    assert response["count"] == 1
    assert response["result_availability"] == "partial_or_unvalidated_table"
    assert not response["calculation_completed"]


def test_completed_b2_report_preserves_missing_consensus(queued):
    from covscreen.tool_api import get_job_candidates_tool
    root, run = queued
    registry = ArtifactRegistry(root)
    result = registry.load_result(run)
    table = registry.job_dir(run) / "candidates.json"
    table.write_text(json.dumps([{"compound_id": "candidate57", "covdock_gate": "B",
        "covdock_gate_detail": "B2", "physical_validity": "PASS", "cross_method_consensus": "NA",
        "af3_covdock_ligand_rmsd_angstrom": None,
        "covdock_gate_reason": "af3_covdock_pose_consistency_not_evaluated"}]))
    result.state = "COMPLETED"
    result.candidate_table_path = str(table.relative_to(root))
    registry.write_result(result)
    response = get_job_candidates_tool(root, run)
    assert response["calculation_completed"]
    assert response["gate_semantics"]["A"] == "physical PASS and AF3-CovDock consensus HIGH"
    assert response["gate_semantics"]["C"] == "one or more hard physical-validity checks failed"
    assert "B2不等于全部门控通过" in response["authoritative_report_markdown"]
    assert "cross_method_consensus=NA" in response["authoritative_report_markdown"]
    assert "RMSD=None" in response["authoritative_report_markdown"]


def test_changed_spec_refused(queued):
    root, run = queued
    with pytest.raises(ValueError, match="exact approved"):
        reserve(root, run, "0" * 64)


def test_pending_excludes_terminal_jobs_and_blocks_duplicates(queued):
    root, run = queued
    registry = ArtifactRegistry(root)
    assert [r["run_id"] for r in pending(root)] == [run]
    spec = registry.load_spec(run)
    result = registry.load_result(run)
    duplicate = spec.model_copy(update={"run_id": "test-external-duplicate"})
    registry.create(approved(duplicate), result.model_copy(update={"run_id": duplicate.run_id}))
    assert len(pending(root)) == 2
    result.state = "FAILED"
    registry.write_result(result)
    assert [r["run_id"] for r in pending(root)] == [duplicate.run_id]


def test_failure_needs_reserved_identity(queued):
    root, run = queued
    record = inspect(root, run)
    with pytest.raises(ValueError):
        fail(root, run, record["spec_sha256"], record["name"], "FAILED")
    reserve(root, run, record["spec_sha256"])
    assert fail(root, run, record["spec_sha256"], record["name"], "FAILED")["state"] == "FAILED"


def test_rejects_larger_scope(queued):
    root, run = queued
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run)
    spec.resource_budget.max_candidates = 2
    registry.write_spec(spec)
    with pytest.raises(ValueError, match="changed"):
        inspect(root, run)


def test_retry_requires_failed_parent_and_identical_inputs(queued):
    root, run = queued
    registry = ArtifactRegistry(root)
    parent = registry.load_spec(run)
    result = registry.load_result(run)
    child = parent.model_copy(update={"run_id": "test-external-002", "attempt": 2,
        "parent_run_id": run, "request_id": parent.request_id + "-retry-2"})
    registry.create(approved(child), result.model_copy(update={"run_id": child.run_id, "attempt": 2, "parent_run_id": run}))
    with pytest.raises(ValueError):
        inspect(root, child.run_id)
    result.state = "FAILED"
    registry.write_result(result)
    assert inspect(root, child.run_id)["state"] == "QUEUED"
    child_result = registry.load_result(child.run_id)
    child_result.state = "FAILED"
    registry.write_result(child_result)
    third = child.model_copy(update={"run_id": "test-external-003", "attempt": 3,
        "parent_run_id": child.run_id, "request_id": child.request_id + "-retry-3"})
    registry.create(approved(third), child_result.model_copy(update={"run_id": third.run_id, "state": "QUEUED", "attempt": 3}))
    assert inspect(root, third.run_id)["state"] == "QUEUED"
    child.input_sha256 = {"different.csv": "0" * 64}
    registry.write_spec(child)
    with pytest.raises(ValueError):
        inspect(root, child.run_id)
    with pytest.raises(ValueError):
        inspect(root, third.run_id)
