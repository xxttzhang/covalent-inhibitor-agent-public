"""Current workflow handoff with real parsers and artificial AF3/CovDock outputs.

Native approvals are isolated test signatures; no scientific worker executes.
Historical construct admission remains a controlled upstream test decision.
"""
import json

import pytest

from covscreen import refinement_output
from covscreen.jobs import sha256_file
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate
from covscreen.execution import JobManager
from covscreen.workflow_store import load
from covscreen.md_candidate_pose import freeze_delivered_candidate_pose, load_candidate_pose_input
from covscreen.mcp.server import dispatch
from covdock_readback_fixture import outputs
from test_covdock_execution_workflow import (ready, approval, design, graph_design, pose,
    bound, planned_receptor, isolated, completed, reviewed, args, jobs)
from test_af3_evidence_workflow import inputs as base_inputs, registered_results
from test_af3_execution_workflow import configure
from test_ligand_md_selection import runtime
from test_ligand_md_graph_execution import approve as graph_approval
from test_workflow_md_execution import register, completed_graph_output
from test_ligand_md_parameterization import approve as parameterization_approval
from test_workflow_md_parameterization import parameterized


@pytest.fixture
def inputs(base_inputs):
    configure(base_inputs)
    dispatch(base_inputs[0], DESIGN, args(base_inputs, 'check_af3_runtime'))
    registered_results(base_inputs)
    dispatch(base_inputs[0], DESIGN, args(base_inputs, 'evaluate_af3'))
    return base_inputs


def test_af3_and_covdock_feed_shared_pose_selection_without_md_submission(ready, monkeypatch):
    case, _, _, manager, _ = ready
    response = dispatch(case[0], DESIGN, approval(case))
    run = response['covdock_execution']['run_id']
    _, sdk = outputs(case[0], manager, manager.registry.load_spec(run), missing_strain=True)
    monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
    dispatch(case[0], DESIGN, args(case, 'evaluate_covdock'))
    before = jobs(case)
    response = dispatch(case[0], DESIGN, args(case, 'prepare_md_handoff'))
    delivery = response['covdock_execution']['md_handoff']
    assert response['workflow']['stage'] == 'COVDOCK_EXECUTION_REVIEW'
    assert response['md_admission']['state'] == 'not_evaluated'
    assert jobs(case) == before
    path = case[0] / delivery['path']
    value = json.loads(path.read_text())
    assert len(value['candidates']) == 3
    assert [row['af3_status'] for row in value['candidates']] == ['SUCCESS', 'SUCCESS', 'FAILED']
    eligible = [row for row in delivery['pose_selection_assessment']
                if row['state'] == 'eligible_for_pose_selection']
    assert len(eligible) == 1
    selected = eligible[0]
    prepared = path.parent / 'prepared-receptor.json'
    context = path.parent / 'receptor-context.json'
    folder = case[0] / 'state/candidate_pose_input' / ('c' * 32)
    pose_input = freeze_delivered_candidate_pose(path, sha256_file(path), selected['compound_id'],
        selected['input_sdf_index'], prepared, sha256_file(prepared), context, sha256_file(context),
        folder, expected_route='ligand-first', expected_task_id=run)
    assert not pose_input['native_graph_qc_completed']
    loaded = load_candidate_pose_input(case[0], 'candidate-pose-input-' + 'c' * 32,
                                      sha256_file(folder / 'candidate-pose-input.json'))
    assert loaded['manifest'] == pose_input
    restored = navigate(case[0], 'resume', workflow_id=case[1])
    assert restored['covdock_execution']['md_handoff'] == delivery
    assert jobs(case) == before
    choice = {'compound_id': selected['compound_id'], 'input_sdf_index': selected['input_sdf_index'],
              'reason': 'Software-only decision: preserve this eligible pose for independent graph checking'}
    assert 'select_md_candidate' in restored['workflow']['tools'][DESIGN]['actions']
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, {**args(case, 'select_md_candidate'), **choice, 'candidate_path': 'foreign'})
    response = dispatch(case[0], DESIGN, {**args(case, 'select_md_candidate'), **choice})
    selection = response['covdock_execution']['md_selection']
    assert response['md_candidate_selection']['state'] == 'selected'
    assert response['covdock_execution']['md_selection_verification'] == 'verified'
    assert any(item['kind'] == 'candidate_pose_input' for item in response['artifacts'])
    assert dispatch(case[0], DESIGN, {**args(case, 'select_md_candidate'), **choice})['covdock_execution']['md_selection'] == selection
    waiting = dispatch(case[0], DESIGN, args(case, 'check_md_graph_runtime'))
    assert waiting['result']['state'] == 'runtime_configuration_required'
    assert waiting['md_admission']['state'] == 'not_evaluated'
    runtime(case[0])
    response = dispatch(case[0], DESIGN, args(case, 'check_md_graph_runtime'))
    plan = response['covdock_execution']['md_graph_plan']
    assert plan['state'] == 'plan_compiled' and not plan['execution_authorized']
    assert plan['spec']['provenance']['discovery_origin']['route'] == 'ligand-first'
    resumed = navigate(case[0], 'resume', workflow_id=case[1])
    assert resumed['covdock_execution']['md_selection'] == selection
    assert resumed['covdock_execution']['md_graph_plan'] == plan
    assert 'start_md_graph' in resumed['workflow']['tools'][DESIGN]['actions']
    selected_path = case[0] / 'state/candidate_pose_input' / selection['artifact_id'][21:] / 'covdock-source.maegz'
    original = selected_path.read_bytes()
    selected_path.write_bytes(b'Controlled corruption')
    rejected = navigate(case[0], 'status', workflow_id=case[1])
    assert rejected['covdock_execution']['md_selection_verification'] == 'evidence_rejected'
    assert rejected['covdock_execution']['evaluation_verification'] == 'verified'
    assert not any(item['kind'] == 'candidate_pose_input' for item in rejected['artifacts'])
    selected_path.write_bytes(original)
    recovered = navigate(case[0], 'resume', workflow_id=case[1])
    assert recovered['covdock_execution']['md_selection_verification'] == 'verified'
    assert recovered['covdock_execution']['md_selection'] == selection
    released = dispatch(case[0], DESIGN, {**args(case, 'release_md_candidate'), 'reason': 'Keep the unexecuted decision in history'})
    assert 'md_selection' not in released['covdock_execution']
    assert released['md_candidate_selection']['state'] == 'no_active_selection'
    assert released['md_candidate_selection']['selected_identity'] is None
    assert released['result']['selection_status'] == 'deselected_and_archived'
    assert released['covdock_execution']['md_selection_history'][-1]['selection'] == selection
    assert jobs(case) == before
    # Continue the actual upstream workflow into the shared graph lifecycle.
    # Registration and native scientific output remain explicit software fixtures.
    response = dispatch(case[0], DESIGN, {**args(case, 'select_md_candidate'), **choice})
    selected = response['covdock_execution']['md_selection']
    dispatch(case[0], DESIGN, args(case, 'check_md_graph_runtime'))
    with pytest.raises(ValueError, match='approval'):
        dispatch(case[0], DESIGN, {**args(case, 'start_md_graph'), 'human_approved': True})
    proof = graph_approval((case[0], case[2].parent))
    def interrupted(manager, request):
        register(manager, request)
        raise ConnectionError('Lost graph registration response; no worker executed')
    monkeypatch.setattr(JobManager, 'submit', interrupted)
    with pytest.raises(ConnectionError):
        dispatch(case[0], DESIGN, proof)
    assert load(case[2].parent)['pending']['action'] == 'start_md_graph'
    restored = dispatch(case[0], ENTRY, args(case, 'reconcile'))
    graph_run = restored['md_graph_execution']['run_id']
    assert len(jobs(case)) == len(before) + 1
    assert restored['workflow']['stage'] == 'MD_GRAPH_RUNNING'
    resumed = navigate(case[0], 'resume', workflow_id=case[1])
    assert resumed['execution']['task_ids'] == [graph_run]
    assert resumed['workflow']['tools'][DESIGN]['actions'] == ['md_graph_status']
    graph_output = completed_graph_output(case[0], selected, graph_run)
    checked = dispatch(case[0], DESIGN, args(case, 'md_graph_status'))
    receipt = checked['md_graph_execution']['admission']
    assert receipt['native_graph_qc_completed'] and not receipt['md_system_ready']
    assert checked['workflow']['stage'] == 'MD_GRAPH_REVIEW'
    assert checked['md_admission']['state'] == 'not_evaluated'
    graph_manifest = graph_output / 'md-adduct-admission.json'
    graph_bytes = graph_manifest.read_bytes()
    graph_manifest.write_bytes(graph_bytes + b'\n')
    rejected = navigate(case[0], 'resume', workflow_id=case[1])
    assert rejected['md_graph_execution']['admission'] is None
    assert rejected['md_graph_execution']['historical_admission'] == receipt
    graph_manifest.write_bytes(graph_bytes)
    recovered = navigate(case[0], 'resume', workflow_id=case[1])
    assert recovered['md_graph_execution']['admission'] == receipt
    assert len(jobs(case)) == len(before) + 1
    parent_state = load(case[2].parent)
    parent_evaluation = parent_state['covdock_execution']['evaluation']
    source_name = next(name for name in parent_evaluation['output_sha256'] if name.endswith('.maegz'))
    source_path = manager.registry.job_dir(run) / 'artifacts' / source_name
    source_bytes = source_path.read_bytes()
    source_path.write_bytes(b'Controlled upstream CovDock output corruption')
    withdrawn = navigate(case[0], 'resume', workflow_id=case[1])
    assert withdrawn['md_graph_execution']['admission'] is None
    assert not withdrawn['covdock_execution'].get('md_handoff')
    source_path.write_bytes(source_bytes)
    restored = navigate(case[0], 'resume', workflow_id=case[1])
    assert restored['md_graph_execution']['admission'] == receipt
    assert restored['covdock_execution']['md_handoff'] == delivery
    assert restored['workflow']['stage'] == 'MD_GRAPH_REVIEW'
    assert len(jobs(case)) == len(before) + 1
    # Continue through the same real upstream verification boundaries. Outputs
    # are synthetic contract fixtures, not native force-field acceptance.
    dispatch(case[0], DESIGN, args(case, 'plan_md_parameterization'))
    monkeypatch.setattr(JobManager, 'submit', register)
    started = dispatch(case[0], DESIGN, parameterization_approval((case[0], case[2].parent)))
    parameter_run = started['md_parameterization']['run_id']
    parameterized((case[0], case[2].parent), parameter_run, parent=receipt)
    checked = dispatch(case[0], DESIGN, args(case, 'md_parameterization_status'))
    witness = checked['md_parameterization']['parameterization']
    assert witness['candidate_id'] == receipt['candidate_id']
    assert checked['md_parameterization']['parameterization_completed']
    assert not checked['md_parameterization']['md_system_ready']
    source_path.write_bytes(b'Controlled upstream CovDock output corruption')
    withdrawn = navigate(case[0], 'resume', workflow_id=case[1])
    assert withdrawn['md_parameterization']['parameterization'] is None
    source_path.write_bytes(source_bytes)
    restored = navigate(case[0], 'resume', workflow_id=case[1])
    assert restored['md_parameterization']['parameterization'] == witness
    assert restored['workflow']['stage'] == 'MD_PARAMETERIZATION_REVIEW'
    assert len(jobs(case)) == len(before) + 2
