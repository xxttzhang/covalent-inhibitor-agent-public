import json

from covscreen.plan_readiness import plan_readiness
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call
from test_submission_identity import request


def test_plan_does_not_invent_approval_or_resource_availability(tmp_path):
    result = plan_readiness(tmp_path, request())
    assert result['plan_available'] is True
    assert result['execution_authorized_by_this_observation'] is False
    assert result['approval']['host_signing_service_availability'] == 'NOT_VERIFIED'
    assert result['approval']['host_upgrade_required'] == 'NOT_ESTABLISHED'
    assert result['resources']['availability'] == 'NOT_VERIFIED_FOR_THIS_PLAN'
    assert result['resources']['requested_budget'] == request().resource_budget.model_dump(mode='json')
    assert not (tmp_path / 'state').exists()


def test_persisted_stop_is_not_evidence_of_broken_signing_service(tmp_path):
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch,
                 environment={'COVALENT_ERROR_POLICY': 'classified-v1'})
    marker = tmp_path / 'state/task_guard/stopped.json'
    before = marker.read_bytes()
    result = plan_readiness(tmp_path, request())
    assert result['study_execution_policy'] == 'BLOCKED'
    assert result['study_stop_record_sha256']
    assert result['approval']['host_signing_service_availability'] == 'NOT_VERIFIED'
    assert marker.read_bytes() == before


def test_absent_next_request_is_explicit_not_an_empty_executable_plan(tmp_path):
    result = plan_readiness(tmp_path, None)
    assert result['plan_available'] is False
    assert result['resources']['requested_budget'] is None


def test_capabilities_count_matches_actual_registry(tmp_path):
    from covscreen.mcp.registry import TOOLS
    result = dispatch(tmp_path, 'covalent_capabilities', {})
    assert result['tool_count'] == len(result['tools']) == len(TOOLS)
    assert result['approval_service_availability'] == 'NOT_VERIFIED_BY_CAPABILITY_DISCOVERY'
