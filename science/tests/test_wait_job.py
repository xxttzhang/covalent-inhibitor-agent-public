from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.mcp.server import dispatch
from covscreen.tool_api import wait_job_tool


def register(root, state):
    registry = ArtifactRegistry(root)
    spec = JobSpec(run_id='wait-test-001', request_id='wait-request', project_id='test',
                   route='library-first', engine='covindb-prescreen', command=['never-executed'],
                   working_directory=str(root), output_directory=str(root / 'out'),
                   resource_budget=ResourceBudget(max_runtime_minutes=1))
    result = RunResult(run_id=spec.run_id, project_id=spec.project_id, route=spec.route,
                       engine=spec.engine, state=state, submitted_at=utc_now(),
                       message='test registry only', stdout_path='', stderr_path='')
    registry.create(spec, result)
    return registry, result


@pytest.mark.parametrize('state', ['PLANNED', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED'])
def test_wait_does_not_mutate_or_claim_verified_success(tmp_path, state):
    registry, result = register(tmp_path, state)
    before = {p.name: p.read_bytes() for p in registry.job_dir(result.run_id).iterdir()}
    response = dispatch(tmp_path, 'covalent_wait_job', {'run_id': result.run_id, 'timeout_seconds': 0})
    assert response['status'] == 'PASS'
    assert response['observation_timed_out'] == (state in {'QUEUED', 'RUNNING'})
    assert response['terminal'] == (state in {'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED'})
    assert response['scientific_result_verified'] is False
    assert response['calculation_submitted'] is False
    assert before == {p.name: p.read_bytes() for p in registry.job_dir(result.run_id).iterdir()}
    assert len(registry.list_results()) == 1


def test_wait_observes_atomic_completion_from_another_writer(tmp_path):
    registry, result = register(tmp_path, 'RUNNING')
    def finish():
        time.sleep(0.1)
        registry.write_result(result.model_copy(update={'state': 'COMPLETED', 'return_code': 0}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(finish)
        response = wait_job_tool(tmp_path, result.run_id, timeout_seconds=2)
        future.result()
    assert response['terminal'] is True
    assert response['next_action'] == 'verify_outputs_before_advancing'


@pytest.mark.parametrize('timeout', [-1, 31, 1.5, True, '1'])
def test_invalid_wait_budget(tmp_path, timeout):
    with pytest.raises(ValueError, match='timeout_seconds'):
        wait_job_tool(tmp_path, 'wait-test-001', timeout_seconds=timeout)


@pytest.mark.parametrize('tool', ['covalent_wait_job', 'covalent_get_job'])
@pytest.mark.parametrize('field,value', [('project_id', 'another-project'),
    ('run_id', 'different-run-001'), ('engine', 'rdkit-warhead-enumeration'),
    ('route', 'ligand-first')])
def test_rejects_identity_mismatch(tmp_path, tool, field, value):
    registry, result = register(tmp_path, 'COMPLETED')
    # Write to the original result address to simulate a mismatched persisted record.
    path = registry.job_dir(result.run_id) / 'result.json'
    path.write_text(result.model_copy(update={field: value}).model_dump_json())
    before = path.read_bytes()
    arguments = {'run_id': result.run_id}
    if tool == 'covalent_wait_job':
        arguments['timeout_seconds'] = 0
    with pytest.raises(ValueError, match='identity'):
        dispatch(tmp_path, tool, arguments)
    assert path.read_bytes() == before


def test_missing_handle_is_not_restarted(tmp_path):
    with pytest.raises(FileNotFoundError):
        wait_job_tool(tmp_path, 'missing-001', timeout_seconds=0)
    assert ArtifactRegistry(tmp_path).list_results() == []


@pytest.mark.parametrize('tool', ['covalent_wait_job', 'covalent_get_job'])
def test_matching_spec_and_result_must_match_requested_handle(tmp_path, tool):
    registry, result = register(tmp_path, 'COMPLETED')
    import json
    for name in ('spec.json', 'result.json'):
        path = registry.job_dir(result.run_id) / name
        payload = json.loads(path.read_text())
        payload['run_id'] = 'other-valid-run-001'
        path.write_text(json.dumps(payload))
    arguments = {'run_id': result.run_id}
    if tool == 'covalent_wait_job':
        arguments['timeout_seconds'] = 0
    with pytest.raises(ValueError, match='requested handle'):
        dispatch(tmp_path, tool, arguments)
