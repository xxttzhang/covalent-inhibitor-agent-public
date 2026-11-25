import json
import time
from types import SimpleNamespace
import pytest
from covscreen.native_approval import (
    START_ACTION_TOOLS,
    NativeApprovalReplay,
    CURRENT,
    authorize,
    decode,
    needs_approval,
    signed,
    validate_campaign_spec,
)


def test_all_registered_start_actions_require_native_approval_only_for_start():
    assert START_ACTION_TOOLS == {
        'covalent_library_receptor',
        'covalent_predict_cysteine_sites',
        'covalent_library_first_prepare_receptor',
        'covalent_library_first_parameterize_adduct',
        'covalent_library_first_prepare_openmm_system',
        'covalent_library_first_run_openmm_short_qc',
        'covalent_library_first_equilibrate_openmm_system',
        'covalent_library_first_run_openmm_production',
        'covalent_library_first_audit_openmm_trajectory',
        'covalent_library_first_analyze_openmm_replicas',
        'covalent_library_first_build_md_report',
        'covalent_library_first_deliver_md_candidates',
        'covalent_library_first_admit_md_candidate',
        'covalent_ligand_first_campaign',
    }
    for name in START_ACTION_TOOLS:
        assert needs_approval(name, {'action': 'start'}) is True
        assert needs_approval(name, {'action': 'plan'}) is False
        assert needs_approval(name, {'action': 'status'}) is False
from covscreen.approval_scope import approval_scope


def request():
    return {'request_id': 'new-approval-test', 'project_id': 'test-project', 'route': 'ligand-first',
            'engine': 'rdkit-warhead-enumeration', 'parameters': {'scaffold_smiles': 'CC', 'allowed_warheads': ['acrylamide']},
            'execution_mode': 'execute', 'human_approved': True, 'approval_note': 'Native user approved exact plan',
            'resource_budget': {'max_candidates': 2, 'max_runtime_minutes': 1}}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path / 'key'
    path.write_bytes(b'only-for-unit-test-' * 3)
    path.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(path))
    return tmp_path


def proof(args, **updates):
    return signed(dict(tool='covalent_submit_job', arguments_json=json.dumps(args), session='session1', message='message1',
                       approval_request_id='permission_test', nonce='a' * 32, expires=time.time() + 300, **updates))


def test_boolean_is_not_approval(configured):
    with pytest.raises(ValueError, match='receipt required'):
        with authorize(configured, 'covalent_submit_job', {'execution_mode': 'execute', 'human_approved': True}):
            pytest.fail('Should not execute')


def test_exact_call_and_at_most_once(configured):
    args = request()
    token = proof(args, scope=approval_scope(configured, 'covalent_submit_job', args))
    with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': token}) as accepted:
        assert accepted == args
        assert CURRENT.get() == token
    assert CURRENT.get() is None
    with pytest.raises(NativeApprovalReplay):
        with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': token}):
            pytest.fail('Replay')


def test_configuration_change_after_confirmation_is_rejected(configured):
    args = request()
    token = proof(args, scope=approval_scope(configured, 'covalent_submit_job', args))
    (configured / 'configs').mkdir()
    (configured / 'configs/local.inspire.yaml').write_text('runtime: {worker_backend: external-hpc}\n')
    with pytest.raises(ValueError, match='changed after native approval|bounded CPU HPC boundary'):
        with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': token}):
            pytest.fail('Changed configuration cannot be approved')
    assert not (configured / 'state/native_approvals').exists()


def test_scope_tamper_expiry_and_missing_key(configured, monkeypatch):
    args = {'execution_mode': 'execute'}
    token = proof(args)
    with pytest.raises(ValueError, match='scope'):
        with authorize(configured, 'covalent_submit_job', {**args, 'extra': 1, '_native_approval': token}):
            pass
    expired = decode(token)
    expired['expires'] = time.time() - 1
    with pytest.raises(ValueError, match='expiry'):
        with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': signed(expired)}):
            pass
    with pytest.raises(ValueError):
        decode(token[:-1] + ('1' if token[-1] != '1' else '2'))
    monkeypatch.delenv('COVALENT_APPROVAL_KEY_FILE')
    with pytest.raises(ValueError, match='not configured'):
        decode(token)


def test_plan_remains_read_only_without_key(tmp_path):
    with authorize(tmp_path, 'covalent_submit_job', {'execution_mode': 'plan'}) as args:
        assert args == {'execution_mode': 'plan'}


def test_campaign_task_is_rechecked_against_approved_stage_and_budget():
    plan = {
        'route': 'ligand-first', 'project_id': 'project',
        'stage_order': ['af3', 'covdock_lo'],
        'stage_engines': {'af3': 'alphafold3-mpae', 'covdock_lo': 'schrodinger-covdock-lo'},
        'executable_stage_order': ['covdock_lo'],
        'executable_stage_engines': {'covdock_lo': 'schrodinger-covdock-lo'},
        'candidate_ceiling_by_stage': {'af3': 3, 'covdock_lo': 3},
        'resource_ceiling_by_stage': {
            'covdock_lo': {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16},
        },
        'peak_gpu_count': 1, 'peak_cpu_count': 4, 'peak_memory_gb': 128,
        'dispatch_policy': 'ligand-smoke-v1',
        'dispatch_batch': 'ligand-smoke-test',
        'max_runtime_minutes': 240,
    }
    scope = {'kind': 'campaign', 'plan': {'plan': plan}}
    budget = SimpleNamespace(gpu_count=0, cpu_count=4, memory_gb=16,
                             max_runtime_minutes=180, max_candidates=3)
    spec = SimpleNamespace(route='ligand-first', project_id='project',
                           engine='schrodinger-covdock-lo', resource_budget=budget,
                           parameters={'dispatch_policy': 'ligand-smoke-v1',
                                       'dispatch_batch': 'ligand-smoke-test'})
    validate_campaign_spec(scope, spec)
    for field, value in [('max_candidates', 4), ('cpu_count', 5),
                         ('memory_gb', 17), ('max_runtime_minutes', 241)]:
        changed = SimpleNamespace(**{**budget.__dict__, field: value})
        with pytest.raises(ValueError, match=field):
            validate_campaign_spec(scope, SimpleNamespace(
                route=spec.route, project_id=spec.project_id, engine=spec.engine,
                resource_budget=changed, parameters=spec.parameters))
    with pytest.raises(ValueError, match='outside'):
        validate_campaign_spec(scope, SimpleNamespace(
            route='ligand-first', project_id='project', engine='reinvent4',
            resource_budget=budget, parameters=spec.parameters))
    with pytest.raises(ValueError, match='dispatch policy'):
        validate_campaign_spec(scope, SimpleNamespace(
            route=spec.route, project_id=spec.project_id, engine=spec.engine,
            resource_budget=budget,
            parameters={'dispatch_policy': 'ligand-smoke-v1',
                        'dispatch_batch': 'ligand-smoke-other'}))


def test_seeded_stage_cannot_be_resubmitted_under_continuation_approval():
    plan = {
        'route': 'ligand-first', 'project_id': 'project',
        'stage_order': ['af3', 'covdock_lo', 'independent_evaluation', 'pareto'],
        'stage_engines': {
            'af3': 'alphafold3-mpae',
            'covdock_lo': 'schrodinger-covdock-lo',
            'independent_evaluation': 'ligand-first-evaluation',
            'pareto': 'ligand-first-pareto',
        },
        'executable_stage_order': ['independent_evaluation', 'pareto'],
        'executable_stage_engines': {
            'independent_evaluation': 'ligand-first-evaluation',
            'pareto': 'ligand-first-pareto',
        },
        'candidate_ceiling_by_stage': {
            'af3': 3, 'covdock_lo': 3, 'independent_evaluation': 3, 'pareto': 3,
        },
        'resource_ceiling_by_stage': {
            'independent_evaluation': {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16},
            'pareto': {'gpu_count': 0, 'cpu_count': 2, 'memory_gb': 16},
        },
        'max_runtime_minutes': 240,
    }
    scope = {'kind': 'campaign', 'plan': {'plan': plan}}
    budget = SimpleNamespace(gpu_count=1, cpu_count=4, memory_gb=96,
                             max_runtime_minutes=240, max_candidates=3)
    with pytest.raises(ValueError, match='outside'):
        validate_campaign_spec(scope, SimpleNamespace(
            route='ligand-first', project_id='project', engine='alphafold3-mpae',
            resource_budget=budget))


@pytest.mark.parametrize('field,value', [
    ('resource_budget', {'max_candidates': 3, 'max_runtime_minutes': 1}),
    ('parameters', {'scaffold_smiles': 'CCC', 'allowed_warheads': ['acrylamide']}),
    ('engine', 'carsidock-cov'),
])
def test_changed_scientific_request_cannot_reuse_signed_approval(configured, field, value):
    args = request()
    token = proof(args, scope=approval_scope(configured, 'covalent_submit_job', args))
    changed = {**args, field: value, '_native_approval': token}
    with pytest.raises(ValueError, match='scope/expiry mismatch'):
        with authorize(configured, 'covalent_submit_job', changed):
            pytest.fail('Changed computation cannot inherit approval')
    assert not (configured / 'state/native_approvals').exists()


def test_real_authorization_denial_has_typed_recovery_guidance(configured):
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.error_policy import execute_with_recovery
    denied = execute_with_recovery(configured, 'covalent_submit_job', request(), dispatch)
    assert denied['failure']['category'] == 'permission_denied'
    assert denied['failure']['attempts'] == 1
    assert denied['failure']['next_action'] == 'report_permission_denial_do_not_bypass'
    assert not (configured / 'state/jobs').exists()


def test_consumed_approval_requires_reconciliation_not_reapproval_retry(configured):
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.error_policy import execute_with_recovery
    args = request()
    token = proof(args, scope=approval_scope(configured, 'covalent_submit_job', args))
    with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': token}):
        pass  # consume only the isolated test receipt; no job is submitted
    replay = execute_with_recovery(configured, 'covalent_submit_job', {**args, '_native_approval': token}, dispatch)
    assert replay['failure']['category'] == 'approval_already_consumed'
    assert replay['failure']['attempts'] == 1
    assert replay['failure']['next_action'] == 'reconcile_original_request_do_not_replay'
    assert replay['execution_outcome'] == 'unknown_do_not_resubmit'
    assert list((configured / 'state/jobs').iterdir()) == []


def test_changed_input_remains_identity_conflict_not_permission_denial(configured):
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.error_policy import execute_with_recovery
    args = request()
    token = proof(args, scope=approval_scope(configured, 'covalent_submit_job', args))
    (configured / 'configs').mkdir()
    (configured / 'configs/local.inspire.yaml').write_text('runtime: {worker_backend: external-hpc}\n')
    failure = execute_with_recovery(configured, 'covalent_submit_job', {**args, '_native_approval': token}, dispatch)
    assert failure['failure']['category'] == 'invalid_input_or_identity'
    assert failure['failure']['automatic_retry_allowed'] is False
    assert not (configured / 'state/native_approvals').exists()
