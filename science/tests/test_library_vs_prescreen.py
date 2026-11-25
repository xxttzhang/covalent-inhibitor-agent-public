"""Real frozen inputs and signed native registration; no scientific worker runs."""
import json
import time
from uuid import uuid4

import pytest
import yaml

from covscreen.approval_scope import approval_scope
from covscreen.docking_policy import POLICY
from covscreen.execution import JobManager
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.library_vs_prescreen_input import load as load_input, BUDGET, request as input_request
from covscreen.mcp.server import dispatch
from covscreen.native_approval import needs_approval, signed
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_receptor_handoff import bound
from test_workflow_screening_plan import prepared, REVIEW


def grant(root, current, action='start_vs_prescreen'):
    args = {'action': action, 'workflow_id': current['workflow']['workflow_id'],
        'expected_revision': current['workflow']['revision'], 'human_approved': True,
        'approval_note': 'Isolated test ledger only; no scientific worker or scheduler'}
    scope = approval_scope(root, RESEARCH, args)
    token = signed({'tool': RESEARCH, 'arguments_json': json.dumps(args), 'scope': scope,
        'session': 'fixture', 'message': 'fixture', 'approval_request_id': 'permission_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**args, '_native_approval': token}


def test_frozen_prefix_native_plan_approval_and_lost_registration_recovery(bound, monkeypatch):
    root, current = prepared(bound)
    # Global configuration is deliberately unusable: the frozen study prefix
    # must be the only source used by the new execution builder.
    config = root / 'configs/local.inspire.yaml'
    config.write_text(yaml.safe_dump({'covindb': {'virtual_screening_library': '/missing/full-library.csv'}}))
    planned = call(root, current, RESEARCH, 'plan_vs_prescreen', scientific_review=REVIEW)
    assert planned['workflow']['stage'] == 'VS_PRESCREEN_PLAN_READY'
    assert planned['vs_prescreen']['resource_budget'] == BUDGET
    assert planned['execution']['state'] == 'not_submitted'
    assert 'plan' not in planned['vs_prescreen'] and str(root) not in json.dumps(planned)
    state = load(state_file(root, planned))
    record, inventory = load_input(root, state['vs_prescreen']['input'])
    request = input_request(root, state['vs_prescreen']['input'], state['vs_prescreen']['request_id'])
    spec = JobManager(root)._prepare_submission(request)
    assert spec.provenance['worker_backend'] == 'external-hpc'
    assert spec.provenance['dataset_path'] == str(root / record['roles']['library'])
    assert spec.command[spec.command.index('--input') + 1] == str(root / record['roles']['library'])
    assert spec.command[spec.command.index('--max-rows') + 1] == '20000'
    assert spec.command[spec.command.index('--top-n') + 1] == '5'
    assert all(spec.input_sha256[name] == checksum for name, checksum in inventory.items())
    for altered in (request.model_copy(update={'project_id': 'library-flow-' + 'f' * 24}),
        request.model_copy(update={'parameters': {**request.parameters, 'max_rows': 10}}),
        request.model_copy(update={'resource_budget': request.resource_budget.model_copy(update={'cpu_count': 8})})):
        with pytest.raises(ValueError, match='frozen study'):
            JobManager(root)._prepare_submission(altered)
    assert needs_approval(RESEARCH, {'action': 'start_vs_prescreen'})
    assert not needs_approval(RESEARCH, {'action': 'plan_vs_prescreen'})
    with pytest.raises(ValueError, match='migration is not yet complete'):
        grant(root, planned)
    # Exercise the unreleased native boundary only in this isolated test.
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', True)
    with pytest.raises(ValueError, match='approval'):
        call(root, planned, RESEARCH, 'start_vs_prescreen')
    original = JobManager.submit
    registrations = []
    def lost(manager, request):
        result = original(manager, request)
        registrations.append(result.run_id)
        raise OSError('Controlled loss after native registration')
    monkeypatch.setattr(JobManager, 'submit', lost)
    with pytest.raises(OSError, match='Controlled loss'):
        dispatch(root, RESEARCH, grant(root, planned))
    saved = load(state_file(root, planned))
    assert saved['pending']['action'] == 'start_vs_prescreen'
    recovered = dispatch(root, ENTRY, {'action': 'reconcile', 'workflow_id': saved['workflow_id'], 'expected_revision': saved['revision']})
    assert recovered['pending'] is None
    assert recovered['vs_prescreen']['run_id'] == registrations[0]
    assert recovered['vs_prescreen']['job_identity_verified']
    assert recovered['vs_prescreen']['job_state'] == 'QUEUED'
    job = JobManager(root).registry.job_dir(registrations[0])
    assert not (job / 'external_hpc.json').exists()
    assert len(list((root / 'state/jobs').glob('*/spec.json'))) == 2  # original receptor + queued prescreen
    assert 'reconsider' not in recovered['workflow']['tools'][ENTRY]['actions']
    observed = call(root, recovered, RESEARCH, 'vs_prescreen_status')
    assert observed['vs_prescreen']['run_id'] == registrations[0]
    source = root / record['roles']['library']
    original_source = source.read_bytes()
    source.write_bytes(original_source + b'changed')
    with pytest.raises(ValueError, match='source changed'):
        call(root, observed, RESEARCH, 'vs_prescreen_status')
    rejected = load(state_file(root, observed))
    assert not rejected['vs_prescreen']['job_identity_verified']
    assert rejected['vs_prescreen']['run_id'] == registrations[0]
    assert len(registrations) == 1
    status = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': rejected['workflow_id']})
    assert status['execution']['state'] == 'outcome_unknown'
    source.write_bytes(original_source)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': rejected['workflow_id']})
    assert restored['vs_prescreen']['job_identity_verified']
    assert 'evidence_error' not in restored['vs_prescreen']
    repeated = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': rejected['workflow_id']})
    assert repeated['workflow']['revision'] == restored['workflow']['revision']
