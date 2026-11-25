import pytest

from covscreen.library_instance import budget, validate_lo
from covscreen.jobs import JobSpec, JobSubmissionRequest
from covscreen.execution import JobManager


def test_parallel_budget_respects_allocation_and_candidate_count():
    assert budget(1, stage='lo')['cpu_count'] == 1
    assert budget(100, stage='lo')['cpu_count'] == 55
    assert budget(2000, stage='vs')['memory_gb'] == 220
    assert budget(100, stage='lo', cpu_limit=24)['cpu_count'] == 24
    for count, stage, cpus in [(101, 'lo', 55), (2001, 'vs', 55), (1, 'lo', 56), (True, 'lo', 4)]:
        with pytest.raises(ValueError):
            budget(count, stage=stage, cpu_limit=cpus)


def spec_for(root):
    workflow = 'library-flow-' + 'a' * 24
    return JobSpec(run_id='library-instance-test', request_id='library-instance-test',
        project_id=workflow, route='library-first', engine='schrodinger-covdock-lo',
        command=['false'], working_directory=str(root), output_directory=str(root / 'out'),
        resource_budget=budget(100, stage='lo'), provenance={'worker_backend': 'external-instance'},
        parameters={'workflow_id': workflow, 'dispatch_policy': 'library-lo-instance-v1',
            'execution_instance': 'asgbie2', 'expected_input_count': 100, 'input_row_count': 100,
            'final_n': 100, 'njobs': 55, 'host': 'localhost:55', 'input_score_source': 'core', 'resume': False})


def test_instance_boundary_and_opt_in(tmp_path):
    spec = spec_for(tmp_path)
    validate_lo(spec, 'asgbie2')
    with pytest.raises(ValueError):
        validate_lo(spec, 'asgbie3')
    request = JobSubmissionRequest(request_id=spec.request_id, project_id=spec.project_id,
        route=spec.route, engine=spec.engine, parameters=spec.parameters, resource_budget=spec.resource_budget)
    manager = JobManager(tmp_path)
    assert manager.worker_backend(request) == 'external-instance'
    request = request.model_copy(update={'execution_mode': 'execute'})
    with pytest.raises(ValueError, match='not enabled'):
        manager.worker_backend(request)
    manager.config = {'runtime': {'enable_library_lo_instance': True}}
    assert manager.worker_backend(request) == 'external-instance'
    assert not list(tmp_path.rglob('spec.json'))


@pytest.mark.parametrize('field,value', [('execution_instance', 'asgbie3'), ('host', 'localhost:128'),
    ('resume', True), ('input_row_count', 99), ('workflow_id', 'ligand-flow-' + 'a' * 24)])
def test_reject_changed_instance_recipe(tmp_path, field, value):
    spec = spec_for(tmp_path)
    spec.parameters[field] = value
    with pytest.raises(ValueError):
        validate_lo(spec, 'asgbie2')
