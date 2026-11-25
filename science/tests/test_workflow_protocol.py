"""Verify shared protocol against real persisted Ligand workflows and MCP schemas."""
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from covscreen.ligand_workflow import ENTRY, EVIDENCE, DESIGN, directory, navigate, step
from covscreen.mcp.ligand_workflow import TOOLS
from covscreen.workflow_protocol import Artifact, archive_branch, begin_operation, changed, revision
from covscreen.workflow_store import load
from test_ligand_workflow import case


def validate(result):
    for tool in TOOLS:
        Draft202012Validator.check_schema(tool['outputSchema'])
        Draft202012Validator(tool['outputSchema']).validate(result)


def test_existing_state_resumes_without_migration_or_scientific_success_claim(case):
    root, started, hypothesis = case
    folder = directory(root, started['workflow']['workflow_id'])
    original = (folder/'state.json').read_bytes()
    assert 'schema_version' not in json.loads(original)
    current = navigate(root, 'status', workflow_id=started['workflow']['workflow_id'])
    validate(current)
    assert current['schema_version'] == 'research-workflow-result-v1'
    assert current['execution'] == {'scope': 'registered_scientific_compute', 'state': 'not_submitted', 'task_ids': []}
    assert current['assessment']['state'] == 'not_evaluated'
    assert (folder/'state.json').read_bytes() == original
    finalized = step(root, EVIDENCE, workflow_id=current['workflow']['workflow_id'],
        expected_revision=current['workflow']['revision'], action='finalize', hypothesis=hypothesis)
    validate(finalized)
    artifact = next(x for x in finalized['artifacts'] if x['kind'] == 'parent_proposal')
    assert artifact['sha256'] == finalized['proposal']['proposal_sha256']
    assert artifact['hash_scope'] == 'artifact'  # Proposal JSON bytes are not a manifest.
    assert finalized['assessment']['state'] == 'not_evaluated'


def test_persisted_uncertain_operation_blocks_replay_after_reload(case):
    root, current, _ = case
    folder = directory(root, current['workflow']['workflow_id'])
    state = load(folder)
    begin_operation(folder, state, EVIDENCE, 'collect')
    resumed = navigate(root, 'status', workflow_id=state['workflow_id'])
    validate(resumed)
    assert resumed['blockers'][0]['code'] == 'operation_reconciliation_required'
    assert list(resumed['workflow']['tools']) == [ENTRY]
    assert resumed['workflow']['tools'][ENTRY]['actions'] == ['status']
    with pytest.raises(ValueError, match='reconciliation'):
        begin_operation(folder, load(folder), EVIDENCE, 'collect')
    with pytest.raises(ValueError, match='unavailable'):
        step(root, DESIGN, workflow_id=state['workflow_id'], expected_revision=state['revision'], action='generate')


def test_branch_snapshot_cannot_change_with_later_in_memory_asset_changes(case):
    root, current, hypothesis = case
    finalized = step(root, EVIDENCE, workflow_id=current['workflow']['workflow_id'],
        expected_revision=current['workflow']['revision'], action='finalize', hypothesis=hypothesis)
    folder = directory(root, current['workflow']['workflow_id'])
    state = load(folder)
    proposal = state['proposal']
    archive_branch(state, 'New comparison', retain=('evidence_id','proposal','design_id'), invalidate=('proposal','design_id'))
    proposal['proposal_id'] = 'changed-in-caller'
    assert state['branches'][-1]['proposal']['proposal_id'] == finalized['proposal']['proposal_id']
    assert state['evidence_id'] == finalized['evidence_id']
    with pytest.raises(ValueError, match='replace'):
        changed(folder, state, 'reconsider', {'revision': 999})
    assert load(folder)['revision'] == finalized['workflow']['revision']
    with pytest.raises(ValueError, match='STALE'):
        revision({'revision': 1}, True)


def test_output_contract_rejects_wrong_route_unknown_tools_and_unscoped_hash(case):
    _, current, _ = case
    schema = next(tool for tool in TOOLS if tool['name'] == ENTRY)['outputSchema']
    validator = Draft202012Validator(schema)
    wrong = {**current, 'route': 'de-novo'}
    assert list(validator.iter_errors(wrong))
    wrong = {**current, 'workflow': {**current['workflow'], 'tools': {
        'covalent_submit_job': {'actions': ['execute'], 'fields': ['action']}}}}
    assert list(validator.iter_errors(wrong))
    with pytest.raises(ValidationError, match='hash scope'):
        Artifact(kind='test', id='test', sha256='a'*64)


@pytest.mark.parametrize('name', ['action', 'workflow_id', 'expected_revision', 'request_id', 'arbitrary_path'])
def test_stage_constraints_cannot_target_identity_or_unexposed_fields(name):
    from covscreen.workflow_protocol import ToolView
    with pytest.raises(ValidationError, match='exposed scientific fields'):
        ToolView(actions=['plan'], fields=['action', 'resource_budget'], field_constraints={name: {}})


def test_empty_constraints_preserve_existing_response_shape(case):
    _, result, _ = case
    assert all('field_constraints' not in tool for tool in result['workflow']['tools'].values())
    validate(result)
