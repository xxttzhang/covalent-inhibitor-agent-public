import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from covscreen.execution import JobManager, execute_persisted_job
from covscreen.job_cancellation import PROTOCOL
from covscreen.jobs import ArtifactRegistry, JobSpec, RunResult, ResourceBudget, utc_now
from covscreen.tool_api import cancel_job_tool


def queued(root):
    registry = ArtifactRegistry(root)
    run = 'test-cancel-000001'
    spec = JobSpec(run_id=run, request_id='request-cancel', project_id='test-project',
        route='library-first', engine='rdkit-warhead-enumeration',
        command=[sys.executable, '-c', "from pathlib import Path; import time; Path('started').write_text('yes'); time.sleep(20)"],
        working_directory=str(root), output_directory=str(registry.job_dir(run) / 'artifacts'),
        provenance={'cancellation_protocol': PROTOCOL}, resource_budget=ResourceBudget(max_runtime_minutes=1),
        execution_mode='execute', human_approved=True, approval_note='Synthetic subprocess cancellation test only.')
    registry.create(spec, RunResult(run_id=run, state='QUEUED', engine=spec.engine, route=spec.route,
        project_id=spec.project_id, submitted_at=utc_now(), message='queued', stdout_path='stdout.log', stderr_path='stderr.log'))
    return registry, run


def test_request_is_not_terminal_and_queued_worker_never_launches(tmp_path):
    registry, run = queued(tmp_path)
    response = cancel_job_tool(tmp_path, run, human_approved=True)
    assert response['cancellation_requested'] is True
    assert response['cancellation_confirmed'] is False
    assert registry.load_result(run).state == 'QUEUED'
    final = execute_persisted_job(tmp_path, run)
    assert final.state == 'CANCELLED'
    assert final.engine_pid is None
    assert not (tmp_path / 'started').exists()


def test_running_worker_confirms_exit_and_retains_logs(tmp_path):
    registry, run = queued(tmp_path)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(execute_persisted_job, tmp_path, run)
        deadline = time.monotonic() + 5
        while not (tmp_path / 'started').exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        response = cancel_job_tool(tmp_path, run, human_approved=True)
        assert response['cancellation_requested']
        final = future.result(timeout=10)
    assert final.state == 'CANCELLED'
    assert final.return_code != 0
    with pytest.raises(ProcessLookupError):
        os.kill(final.engine_pid, 0)
    assert registry.load_result(run).state == 'CANCELLED'
    assert any(a.role == 'log' for a in final.artifacts)


def test_old_protocol_and_changed_identity_are_not_cancelled(tmp_path):
    registry, run = queued(tmp_path)
    spec = registry.load_spec(run)
    spec.provenance.clear()
    registry.write_spec(spec)
    with pytest.raises(ValueError, match='lacks'):
        JobManager(tmp_path).cancel(run)
    assert not (registry.job_dir(run) / 'cancellation.json').exists()
    spec.provenance['cancellation_protocol'] = PROTOCOL
    registry.write_spec(spec)
    JobManager(tmp_path).cancel(run)
    path = registry.job_dir(run) / 'cancellation.json'
    value = json.loads(path.read_text()); value['spec_sha256'] = '0' * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='identity'):
        execute_persisted_job(tmp_path, run)
    assert not (tmp_path / 'started').exists()


@pytest.mark.parametrize('mutation', ['external-hpc', 'external-inspire', 'protocol', 'terminal', 'identity'])
def test_invalid_cancellation_is_rejected_before_approval_plan(tmp_path, mutation):
    from covscreen.approval_scope import approval_scope
    registry, run = queued(tmp_path)
    spec = registry.load_spec(run)
    result = registry.load_result(run)
    if mutation.startswith('external'):
        spec.provenance['worker_backend'] = mutation
    elif mutation == 'protocol':
        spec.provenance.pop('cancellation_protocol')
    elif mutation == 'terminal':
        result.state = 'COMPLETED'
    else:
        result.project_id = 'different-project'
    registry.write_spec(spec)
    registry.write_result(result)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError):
        approval_scope(tmp_path, 'covalent_cancel_job', {'run_id': run})
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert not (registry.job_dir(run) / 'cancellation.json').exists()


def test_valid_cancel_plan_does_not_request_cancellation(tmp_path):
    from covscreen.approval_scope import approval_scope
    registry, run = queued(tmp_path)
    plan = approval_scope(tmp_path, 'covalent_cancel_job', {'run_id': run})
    assert plan['kind'] == 'cancel'
    assert plan['run_id'] == run
    assert registry.load_result(run).state == 'QUEUED'
    assert not (registry.job_dir(run) / 'cancellation.json').exists()


def test_cancel_execution_rechecks_identity_after_plan(tmp_path):
    from covscreen.approval_scope import approval_scope
    from covscreen.job_cancellation import request
    registry, run = queued(tmp_path)
    assert approval_scope(tmp_path, 'covalent_cancel_job', {'run_id': run})['kind'] == 'cancel'
    result = registry.load_result(run)
    result.project_id = 'changed-after-plan'
    registry.write_result(result)
    with pytest.raises(ValueError, match='identity mismatch'):
        request(registry, run)
    assert not (registry.job_dir(run) / 'cancellation.json').exists()
