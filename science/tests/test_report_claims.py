import pytest

from covscreen.mcp.registry import TOOLS
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call


def supported(blocked=False):
    return {'claims': {'tool_count': len(TOOLS), 'study_execution_blocked': blocked,
        'host_approval_service_status': 'NOT_VERIFIED', 'host_upgrade_required': 'NOT_ESTABLISHED',
        'resource_budget_status': 'NOT_VERIFIED'}}


def test_rejects_actual_v1245_report_errors_without_poisoning_study(tmp_path):
    arguments = supported()
    arguments['claims'].update(tool_count=58, host_approval_service_status='UNAVAILABLE',
                               host_upgrade_required='REQUIRED', resource_budget_status='READY')
    response = guarded_call(tmp_path, 'covalent_verify_report_claims', arguments, dispatch,
                            environment={'COVALENT_ERROR_POLICY': 'classified-v1'})
    assert response['status'] == 'CORRECTION_REQUIRED'
    assert response['claims_verified'] is False
    assert {item['field'] for item in response['discrepancies']} == {
        'tool_count', 'host_approval_service_status', 'host_upgrade_required', 'resource_budget_status'}
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()
    assert not (tmp_path / 'state/jobs').exists()


def test_known_study_stop_survives_report_correction(tmp_path):
    env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch, environment=env)
    marker = tmp_path / 'state/task_guard/stopped.json'
    before = marker.read_bytes()
    bad = guarded_call(tmp_path, 'covalent_verify_report_claims', supported(False), dispatch, environment=env)
    assert bad['status'] == 'CORRECTION_REQUIRED'
    assert bad['discrepancies'][0]['field'] == 'study_execution_blocked'
    good = guarded_call(tmp_path, 'covalent_verify_report_claims', supported(True), dispatch, environment=env)
    assert good['claims_verified'] is True
    assert good['execution_authorized'] is False
    assert good['task_guard']['execution_blocked'] is True
    assert marker.read_bytes() == before


def test_pass_certifies_only_explicit_fields_and_does_not_create_state(tmp_path):
    response = dispatch(tmp_path, 'covalent_verify_report_claims', supported())
    assert response['status'] == 'PASS'
    assert len(response['validated_fields']) == 5
    assert 'does not validate narrative prose' in response['verification_scope']
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('key,value', [('tool_count', True), ('tool_count', '60'),
    ('study_execution_blocked', 'false'), ('approval', True),
    ('stop_gate_tool_count', True), ('stop_gate_tool_count', '16'),
    ('stop_gate_all_tools_read_only', 'false'), ('cancellation_requires_native_approval', 'true')])
def test_report_contract_rejects_coercions_and_extra_claims(tmp_path, key, value):
    arguments = supported()
    arguments['claims'][key] = value
    with pytest.raises(ValueError):
        dispatch(tmp_path, 'covalent_verify_report_claims', arguments)


def test_report_rejects_pre_failure_guard_snapshot(tmp_path):
    from covscreen.branch_guard import guard_status, quarantine_job
    from covscreen.execution import JobManager
    from covscreen.jobs import JobSubmissionRequest, ResourceBudget
    before = guard_status(tmp_path, {})
    assert before['total_quarantined_jobs'] == 0
    request = JobSubmissionRequest(request_id='report-fixture', project_id='test',
        route='library-first', engine='rdkit-warhead-enumeration', execution_mode='plan',
        parameters={'scaffold_smiles': 'CCO', 'attachment_atom_indices': [1],
                    'allowed_warheads': ['acrylamide']},
        resource_budget=ResourceBudget(max_runtime_minutes=1, max_candidates=2))
    result = JobManager(tmp_path).submit(request)
    quarantine_job(tmp_path, result.run_id, 'test', 'invalid_input_or_identity')
    claims = supported()
    claims['claims'].update(quarantined_job_count=0,
                           guard_snapshot_sha256=before['guard_snapshot_sha256'])
    observed = dispatch(tmp_path, 'covalent_verify_report_claims', claims)
    assert observed['status'] == 'CORRECTION_REQUIRED'
    assert {item['field'] for item in observed['discrepancies']} == {
        'quarantined_job_count', 'guard_snapshot_sha256'}
    assert observed['guard_status']['total_quarantined_jobs'] == 1
    assert observed['guard_status']['execution_stop_scope'] == 'dependent_branches'
    assert observed['supported_facts']['study_execution_blocked'] is False
    # A limited display cannot change the identity of the full guard snapshot.
    latest = guard_status(tmp_path, {'limit': 1})
    assert latest['guard_snapshot_sha256'] == observed['supported_facts']['guard_snapshot_sha256']
    claims['claims'].update(quarantined_job_count=1,
                           guard_snapshot_sha256=latest['guard_snapshot_sha256'])
    corrected = dispatch(tmp_path, 'covalent_verify_report_claims', claims)
    assert corrected['claims_verified'] is True
    assert corrected['execution_authorized'] is False
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()
    # Matching quarantine counts cannot hide a later study-wide stop.
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch,
                 environment={'COVALENT_ERROR_POLICY': 'classified-v1'})
    claims['claims']['study_execution_blocked'] = True
    stale = dispatch(tmp_path, 'covalent_verify_report_claims', claims)
    assert [item['field'] for item in stale['discrepancies']] == ['guard_snapshot_sha256']


def test_zero_quarantines_does_not_narrow_a_study_stop(tmp_path):
    from covscreen.branch_guard import guard_status
    assert guard_status(tmp_path, {})['execution_stop_scope'] == 'none_observed'
    env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch, environment=env)
    observed = guard_status(tmp_path, {})
    assert observed['total_quarantined_jobs'] == 0
    assert observed['execution_stop_scope'] == 'study_wide'
    assert 'including independent branches' in observed['scope_meaning']
    claims = supported(True)
    claims['claims'].update(quarantined_job_count=0, execution_stop_scope='dependent_branches')
    checked = dispatch(tmp_path, 'covalent_verify_report_claims', claims)
    assert checked['status'] == 'CORRECTION_REQUIRED'
    assert checked['discrepancies'] == [{'field': 'execution_stop_scope',
        'claimed': 'dependent_branches', 'supported': 'study_wide'}]
    claims['claims']['execution_stop_scope'] = 'study_wide'
    assert dispatch(tmp_path, 'covalent_verify_report_claims', claims)['claims_verified']
