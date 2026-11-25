"""Shared receipt boundary over real registries and labelled synthetic output.

No graph engine or molecular dynamics is executed. Approval remains owned by
the workflow; the shared reader additionally checks exact job/pose identity.
"""
from copy import deepcopy

import pytest

from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, RunResult, sha256_file, utc_now
from covscreen.library_workflow import RESEARCH
from covscreen.mcp.server import dispatch
from covscreen.md_graph_evidence import graph_admission_receipt
from covscreen.workflow_store import load
from test_workflow_md_execution import research, frozen, call, approval, register, completed_output, completed_graph_output
from test_ligand_md_selection import state as ligand_state, choose, runtime
from test_md_review_delivery import case


@pytest.fixture
def graph_case(research, monkeypatch):
    frozen(research)
    call(research, 'plan_md_admission')
    monkeypatch.setattr(JobManager, 'submit', register)
    started = dispatch(research[0], RESEARCH, approval(research))
    run = started['md_execution']['run_id']
    completed_output(research, run)
    manager = JobManager(research[0])
    state = load(research[1])
    return research, manager, manager.registry.load_spec(run), manager.registry.load_result(run), state


def check(case, *, spec=None, result=None, pose=None, prior=None):
    research, manager, original_spec, original_result, state = case
    return graph_admission_receipt(research[0], prior or {}, manager,
        spec or original_spec, result or original_result, pose or state['md_input'])


def test_shared_receipt_needs_only_explicit_pose_and_prior_receipt(graph_case):
    research, _, _, _, state = graph_case
    # No route-specific workflow state is passed to the common reader.
    value = check(graph_case, pose={key: state['md_input'][key]
                                  for key in ('artifact_id', 'manifest_sha256')})
    observed = call(research, 'md_admission_status')['md_execution']['admission']
    assert value == observed
    assert value['native_graph_qc_completed'] is True
    assert value['parameterization_completed'] is False
    assert value['md_system_ready'] is False
    assert check(graph_case, prior={'admission': value}) == value
    with pytest.raises(ValueError, match='Previously verified'):
        check(graph_case, prior={'admission': {**value, 'manifest_sha256': '0' * 64}})


@pytest.mark.parametrize('field', ['run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'])
def test_result_identity_cannot_be_borrowed_from_another_job(graph_case, field):
    result = graph_case[3].model_copy(deep=True)
    setattr(result, field, result.attempt + 1 if field == 'attempt' else 'different-job')
    with pytest.raises(ValueError, match='job identity'):
        check(graph_case, result=result)


@pytest.mark.parametrize('field', ['candidate_pose_artifact_id', 'manifest_sha256'])
def test_receipt_must_belong_to_the_pose_in_the_job_spec(graph_case, field):
    spec = graph_case[2].model_copy(deep=True)
    spec.parameters[field] = 'different-pose'
    with pytest.raises(ValueError, match='job pose'):
        check(graph_case, spec=spec)


def test_conflicting_duplicate_artifact_cannot_be_hidden_by_list_order(graph_case):
    research, manager, spec, result, _ = graph_case
    pristine = deepcopy(result.artifacts)
    receipt = next(item for item in pristine if item.path.endswith('/md-adduct-admission.json'))
    conflicting = receipt.model_copy(update={'sha256': '0' * 64})
    for records in ([conflicting, *pristine], [*pristine, conflicting]):
        result.artifacts = records
        manager.registry.write_result(result)
        observed = call(research, 'md_admission_status')['md_execution']
        assert observed['verification'] == 'evidence_rejected'
        assert observed['admission'] is None
    # Recover the same registered task and evidence, never submit another job.
    result.artifacts = pristine
    manager.registry.write_result(result)
    restored = call(research, 'md_admission_status')['md_execution']
    assert restored['verification'] == 'verified'
    assert restored['run_id'] == spec.run_id


def synthetic_registered_result(root, asset):
    """Compile real input bindings, register only inert artificial result data."""
    runtime(root)
    manager = JobManager(root)
    request = JobSubmissionRequest(request_id='software-graph-receipt', project_id='software-fixture',
        route='library-first', engine='library-first-md-adduct-admission', execution_mode='plan',
        parameters={'candidate_pose_artifact_id': asset['artifact_id'], 'manifest_sha256': asset['manifest_sha256']},
        resource_budget={'max_runtime_minutes': 30, 'gpu_count': 0, 'cpu_count': 2, 'memory_gb': 8, 'max_candidates': 1})
    spec = manager._prepare_submission(request)
    result = RunResult(run_id=spec.run_id, project_id=spec.project_id, route=spec.route, engine=spec.engine,
        state='QUEUED', submitted_at=utc_now(), stdout_path='unused', stderr_path='unused',
        message='Synthetic receipt fixture: no execution approval, worker, native graph calculation or MD')
    manager.registry.create(spec, result)
    completed_graph_output(root, asset, spec.run_id)
    return manager, spec, manager.registry.load_result(spec.run_id)


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_shared_graph_reader_accepts_preserved_discovery_origin(tmp_path, route):
    from covscreen.md_candidate_pose import freeze_delivered_candidate_pose
    from test_shared_md_pose import fixture
    args = fixture(tmp_path, route)
    folder = tmp_path / 'state/candidate_pose_input' / ('c' * 32)
    original = freeze_delivered_candidate_pose(*args, folder, expected_route=route, expected_task_id='campaign-001')
    asset = {'artifact_id': 'candidate-pose-input-' + 'c' * 32,
             'manifest_sha256': sha256_file(folder / 'candidate-pose-input.json')}
    manager, spec, result = synthetic_registered_result(tmp_path, asset)
    receipt = graph_admission_receipt(tmp_path, {}, manager, spec, result, asset)
    assert receipt['candidate_id'] == original['candidate_id']
    assert spec.parameters['discovery_origin']['route'] == route
    assert not receipt['md_system_ready'] and not receipt['parameterization_completed']


def test_current_ligand_selection_uses_same_reader_without_library_state(tmp_path, ligand_state):
    selected = choose(tmp_path, ligand_state)
    manager, spec, result = synthetic_registered_result(tmp_path, selected)
    receipt = graph_admission_receipt(tmp_path, {}, manager, spec, result, selected)
    assert receipt['candidate_id'] == selected['candidate_id']
    assert spec.parameters['compound_id'] == '007'
    assert spec.parameters['discovery_origin']['route'] == 'ligand-first'
    # Reading artificial evidence cannot mutate the workflow or grant MD admission.
    assert selected['md_admission']['state'] == 'not_evaluated'
    assert not receipt['md_system_ready']
