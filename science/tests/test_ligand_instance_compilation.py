"""The plan compiler must validate the selected worker, not a legacy default."""
import pytest

from covscreen.execution import JobManager
from covscreen.jobs import JobSpec, JobSubmissionRequest


@pytest.mark.parametrize('instance', ['asgbie3', 'asgbie4'])
def test_plan_compilation_uses_bound_ligand_instance(tmp_path, monkeypatch, instance):
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', instance)
    parameters = {'workflow_id': 'ligand-flow-' + 'a' * 24,
        'execution_instance': instance, 'dispatch_policy': 'ligand-lo-instance-v1',
        'expected_input_count': 2, 'input_row_count': 2, 'final_n': 2,
        'njobs': 2, 'host': 'localhost:2', 'input_score_source': 'core', 'resume': False}
    budget = {'max_runtime_minutes': 240, 'gpu_count': 0, 'cpu_count': 2,
        'memory_gb': 8, 'max_candidates': 2}
    request = JobSubmissionRequest(request_id='test-instance-plan', project_id='fixture',
        route='ligand-first', engine='schrodinger-covdock-lo', parameters=parameters,
        resource_budget=budget, execution_mode='plan')
    spec = JobSpec(run_id='test-instance-plan', request_id=request.request_id,
        project_id='fixture', route=request.route, engine=request.engine,
        command=['not-executed'], working_directory=str(tmp_path),
        output_directory=str(tmp_path/'out'), parameters=parameters,
        resource_budget=budget, execution_mode='plan')
    manager = JobManager(tmp_path)
    monkeypatch.setattr(manager, 'build_spec', lambda _: spec)
    assert manager._prepare_submission(request).parameters['execution_instance'] == instance
    assert not manager.registry.job_dir(spec.run_id).exists()
    spec.parameters['execution_instance'] = 'asgbie3' if instance == 'asgbie4' else 'asgbie4'
    with pytest.raises(ValueError, match='bounded'):
        manager._prepare_submission(request)
