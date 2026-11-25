"""Exercise real admission/worker boundaries without launching docking software."""
import pytest

from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import JobSubmissionRequest
from covscreen.ligand_workflow import view, outcome, DESIGN
from covscreen.ligand_design import plan_pose
from covscreen.ligand_pose_execution import bound_request
from test_job_cancellation import queued


def test_library_reenabled_without_changing_other_routes():
    from covscreen.docking_policy import require_engine_enabled
    require_engine_enabled('carsidock-cov', route='library-first')
    for route in (None, 'ligand-first', 'de-novo', 'unknown'):
        with pytest.raises(ValueError, match='docking_engine_disabled'):
            require_engine_enabled('carsidock-cov', route=route)


def test_library_compiler_reaches_input_validation_without_submitting(tmp_path):
    request = JobSubmissionRequest(request_id='library-enabled', project_id='test',
        route='library-first', engine='carsidock-cov', parameters={},
        resource_budget={'max_runtime_minutes': 1}, execution_mode='plan')
    with pytest.raises((ValueError, KeyError)) as error:
        JobManager(tmp_path).build_spec(request)
    assert 'docking_engine_disabled' not in str(error.value)
    assert not list(tmp_path.rglob('spec.json'))


@pytest.mark.parametrize('method', ['build_spec', '_prepare_submission', 'submit'])
@pytest.mark.parametrize('mode', ['plan', 'execute'])
def test_disabled_engine_rejected_before_inputs_or_registration(tmp_path, method, mode):
    request = JobSubmissionRequest(request_id='disabled-request', project_id='test',
        route='ligand-first', engine='carsidock-cov', parameters={}, resource_budget={'max_runtime_minutes':1},
        execution_mode=mode, human_approved=True, approval_note='No compute test')
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        getattr(JobManager(tmp_path), method)(request)
    assert not list(tmp_path.rglob('spec.json'))


def stored_carsi(root):
    registry, run = queued(root)
    spec, result = registry.load_spec(run), registry.load_result(run)
    spec.engine = result.engine = 'carsidock-cov'
    spec.route = result.route = 'ligand-first'
    registry.write_spec(spec)
    registry.write_result(result)
    return registry, run


def test_previously_queued_engine_is_denied_without_launch(tmp_path):
    registry, run = stored_carsi(tmp_path)
    result = execute_persisted_job(tmp_path, run)
    assert result.state == 'FAILED'
    assert result.failure_reason == 'docking_engine_disabled'
    assert result.started_at is None
    assert not (tmp_path/'started').exists()
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        JobManager(tmp_path).compile_retry_request(run, human_approved=True, approval_note='retry')


@pytest.mark.parametrize('state', ['COMPLETED', 'FAILED', 'RUNNING', 'CANCELLED'])
def test_historical_observation_is_preserved(tmp_path, state):
    registry, run = stored_carsi(tmp_path)
    result = registry.load_result(run)
    result.state = state
    registry.write_result(result)
    before = (registry.job_dir(run)/'result.json').read_bytes()
    assert execute_persisted_job(tmp_path, run).state == state
    assert (registry.job_dir(run)/'result.json').read_bytes() == before
    assert not (tmp_path/'started').exists()


@pytest.mark.parametrize('function', [plan_pose, bound_request])
def test_old_pose_planner_cannot_write_or_compile(tmp_path, function):
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        function(tmp_path, 'design-not-used')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('stage', ['NEEDS_POSE_EVALUATION', 'POSE_PLAN_READY'])
def test_workflow_explains_vs_gap_and_hides_disabled_actions(stage, monkeypatch):
    from covscreen.docking_policy import POLICY
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', False)
    state = {'workflow_id':'ligand-flow-'+'a'*24, 'stage':stage, 'revision':1,
             'evidence_id':'hyp-test', 'pending':None, 'branches':[], 'pose_execution':{'request_id':'old-plan'}}
    result = view(state)
    assert result['tools'][DESIGN]['actions'] == ['deliver']
    assert any(b['scope'] == 'covdock_vs_pose_workflow' for b in outcome(state)['blockers'])
