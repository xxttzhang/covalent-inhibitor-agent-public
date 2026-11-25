"""No scientific jobs: enforce containment refusal at real entry boundaries."""
import pytest

from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import ArtifactRegistry, JobSpec, JobSubmissionRequest, RunResult, utc_now


@pytest.mark.parametrize('policy', [None, 'denovo-independent-local-v1'])
def test_local_denovo_rejected_before_registration(tmp_path, policy):
    manager = JobManager(tmp_path)
    manager.config['runtime'] = {'worker_backend': 'local'}
    parameters = {'workflow_id': 'study-budget-fixture'}
    if policy:
        parameters['dispatch_policy'] = policy
    request = JobSubmissionRequest(request_id='budget-fixture', resource_budget={'max_runtime_minutes': 1}, project_id='fixture', route='de-novo',
        engine='schrodinger-covdock-lo', parameters=parameters)
    with pytest.raises(ValueError, match='JobControl descendants'):
        manager.submit(request)
    assert not list(manager.registry.root.glob('*/spec.json'))


def test_queued_local_job_cannot_bypass_guard_and_terminal_results_remain_readable(tmp_path):
    registry = ArtifactRegistry(tmp_path)
    spec = JobSpec(request_id='budget-fixture', resource_budget={'max_runtime_minutes': 1}, run_id='budget-fixture', project_id='fixture', route='de-novo',
        engine='schrodinger-covdock-lo', execution_mode='execute', human_approved=True,
        approval_note='Isolated software fixture; no scientific execution',
        command=['must-never-launch'], working_directory=str(tmp_path),
        output_directory=str(tmp_path/'output'), provenance={'worker_backend': 'local'})
    result = RunResult(run_id=spec.run_id, project_id=spec.project_id, route=spec.route,
        engine=spec.engine, state='QUEUED', submitted_at=utc_now(), message='test only',
        stdout_path='stdout', stderr_path='stderr')
    registry.create(spec, result)
    before = (registry.job_dir(spec.run_id)/'spec.json').read_bytes()
    with pytest.raises(ValueError, match='JobControl descendants'):
        execute_persisted_job(tmp_path, spec.run_id)
    assert registry.load_result(spec.run_id).state == 'QUEUED'
    assert not (tmp_path/'output').exists()
    result.state = 'TIMED_OUT'
    registry.write_result(result)
    assert execute_persisted_job(tmp_path, spec.run_id).state == 'TIMED_OUT'
    assert (registry.job_dir(spec.run_id)/'spec.json').read_bytes() == before


def test_other_routes_and_prereaction_keep_existing_backend_selection(tmp_path):
    manager = JobManager(tmp_path)
    manager.config['runtime'] = {'worker_backend': 'external-hpc'}
    for route in ('library-first', 'ligand-first'):
        request = JobSubmissionRequest(request_id='budget-fixture', resource_budget={'max_runtime_minutes': 1}, project_id='fixture', route=route, engine='schrodinger-covdock-lo')
        assert manager.worker_backend(request) == 'external-hpc'
    request = JobSubmissionRequest(request_id='budget-fixture', resource_budget={'max_runtime_minutes': 1}, project_id='fixture', route='de-novo', engine='de-novo-prereaction-vina')
    assert manager.worker_backend(request) == 'local'
