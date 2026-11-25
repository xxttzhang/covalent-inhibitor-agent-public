"""Actual MCP review/plan recovery with controlled upstream scientific decisions.

Historical 1M17 remains rejected by the unchanged production construct gate.
The positive judgments below are test input, not a scientific endorsement.
"""
from copy import deepcopy
import json

import pytest

from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covscreen.receptor_environment_review import CONCERNS
from test_covdock_input_workflow import (
    design, graph_design, pose, inputs, bound, planned_receptor, isolated, completed,
    reviewed, args, jobs, configure)


def test_environment_review_revises_plan_and_restores_source_bound_decisions(reviewed):
    case, launches, folder, manager, run = reviewed
    dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))
    configure(case)
    original = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))['refinement_input']['covdock_plan']
    output = dispatch(case[0], DESIGN, args(case, 'read_covdock_environment'))
    assert output['result']['environment_review'] is None
    assert 'environment_review' in output['workflow']['tools'][DESIGN]['fields']
    inventory = output['result']['evidence_inventory']
    path = next(name for name in inventory if name.endswith('environment-decision.json'))
    assert 'evidence_path' in output['workflow']['tools'][DESIGN]['fields']
    assert path in {row['path'] for row in output['result']['evidence']['documents']}
    field = dispatch(case[0], DESIGN, {**args(case, 'read_covdock_environment'),
        'evidence_path': path, 'evidence_locator': '/policy'})['result']['evidence']
    assert field['complete'] and field['value'] == json.loads((case[0] / path).read_text())['policy']
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, {**args(case, 'check_covdock_runtime'), 'evidence_path': path})
    with pytest.raises(ValueError, match='bound inventory'):
        dispatch(case[0], DESIGN, {**args(case, 'read_covdock_environment'), 'evidence_path': 'configs/local.inspire.yaml'})
    review = {name: {'decision': 'evidence_insufficient',
        'rationale': 'The removal inventory records preparation policy but does not establish suitability.',
        'evidence': [{'path': path, 'sha256': inventory[path], 'locator': '/policy',
                      'observation': 'The isolated-protein policy explicitly leaves suitability unverified.'}],
        'limitations': ['This controlled software case supplies no independent biological validation.']}
        for name in CONCERNS}
    # Wrong-stage parameters cannot leak into a runtime check.
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, {**args(case, 'check_covdock_runtime'), 'environment_review': review})
    output = dispatch(case[0], DESIGN, {**args(case, 'review_covdock_environment'), 'environment_review': review})
    handoff = output['refinement_input']
    ref = handoff['environment_review']
    assert output['assessment']['scope'] == 'agent_environment_assessment'
    assert output['assessment']['state'] == 'evidence_insufficient'
    assert ref['decision'] == 'evidence_insufficient'
    assert 'covdock_plan' not in handoff
    assert handoff['environment_review_history'][-1]['plan'] == original
    assert not ref['execution_authorized'] and not ref['biological_suitability_verified']
    output = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    negative_plan = output['refinement_input']['covdock_plan']
    assert negative_plan['environment_review_completed']
    assert not negative_plan['environment_eligible_for_exploratory_refinement']
    assert negative_plan['runtime_check'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
    assert negative_plan['spec']['input_sha256'][ref['path']] == ref['manifest_sha256']
    assert negative_plan['request_id'] != original['request_id']
    repeated = dispatch(case[0], DESIGN, {**args(case, 'review_covdock_environment'), 'environment_review': review})
    assert repeated['refinement_input']['environment_review'] == ref
    assert repeated['refinement_input']['covdock_plan'] == negative_plan
    resumed = navigate(case[0], 'resume', workflow_id=case[1])
    assert resumed['refinement_input']['environment_review'] == ref
    # Controlled positive input tests accounting and scope; it is not evidence
    # that 1M17 actually meets all four scientific conditions.
    positive = deepcopy(review)
    for item in positive.values():
        item['decision'] = 'suitable_for_exploratory_refinement'
    output = dispatch(case[0], DESIGN, {**args(case, 'review_covdock_environment'), 'environment_review': positive})
    newref = output['refinement_input']['environment_review']
    assert newref != ref
    assert output['refinement_input']['environment_review_history'][-1]['review'] == ref
    output = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    plan = output['refinement_input']['covdock_plan']
    assert plan['runtime_check'] == 'REGISTERED_COVDOCK_PLAN_COMPILED'
    assert plan['environment_eligible_for_exploratory_refinement']
    assert output['assessment']['state'] == 'review_required'
    assert not plan['execution_authorized'] and not plan['compute_submitted']
    assert 'start_covdock' in output['workflow']['tools'][DESIGN]['actions']
    payload = json.loads((case[0] / newref['path']).read_text())
    payload['execution_authorized'] = True
    (case[0] / newref['path']).write_text(json.dumps(payload))
    rejected = navigate(case[0], 'status', workflow_id=case[1])['refinement_input']
    assert 'environment_review' not in rejected and 'covdock_plan' not in rejected
    assert rejected['environment_review_error']
    assert rejected['covdock_input'] and rejected['receptor_preparation']['run_id'] == run
    assert len(launches) == 1 and len(jobs(case)) == 2
