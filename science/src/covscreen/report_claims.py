"""Verify bounded report prerequisites against code-owned evidence.

This deliberately does not certify free prose, scientific conclusions, current
GPU visibility, or a historical job's results. Those require their own evidence.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, Field

from .branch_guard import guard_status
from .jobs import utc_now


class ReportClaims(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool_count: StrictInt = Field(ge=0)
    study_execution_blocked: StrictBool
    host_approval_service_status: Literal['AVAILABLE', 'UNAVAILABLE', 'NOT_VERIFIED']
    host_upgrade_required: Literal['REQUIRED', 'NOT_ESTABLISHED']
    resource_budget_status: Literal['READY', 'UNAVAILABLE', 'NOT_VERIFIED']
    stop_gate_tool_count: StrictInt | None = Field(default=None, ge=0)
    stop_gate_all_tools_read_only: StrictBool | None = None
    cancellation_requires_native_approval: StrictBool | None = None
    quarantined_job_count: StrictInt | None = Field(default=None, ge=0)
    guard_snapshot_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    execution_stop_scope: Literal['study_wide', 'dependent_branches', 'none_observed'] | None = None


class ReportClaimsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claims: ReportClaims


def verify_report_claims(root, arguments):
    claims = ReportClaimsInput.model_validate(arguments).claims.model_dump(exclude_none=True)
    from .mcp.registry import SERVER_VERSION, TOOLS
    from .mcp.stop_access import current_access
    guard = guard_status(root, {})
    access = current_access(root)
    actual = {'tool_count': len(TOOLS),
              'study_execution_blocked': guard['study_stop'] is not None,
              'host_approval_service_status': 'NOT_VERIFIED',
              'host_upgrade_required': 'NOT_ESTABLISHED',
              'resource_budget_status': 'NOT_VERIFIED',
              'stop_gate_tool_count': access['tool_count'],
              'stop_gate_all_tools_read_only': access['all_tools_read_only'],
              'cancellation_requires_native_approval': access['cancellation_requires_native_approval'],
              'quarantined_job_count': guard['total_quarantined_jobs'],
              'guard_snapshot_sha256': guard['guard_snapshot_sha256'],
              'execution_stop_scope': guard['execution_stop_scope']}
    differences = [{'field': key, 'claimed': value, 'supported': actual[key]}
                   for key, value in claims.items() if value != actual[key]]
    return {'status': 'CORRECTION_REQUIRED' if differences else 'PASS',
            'claims_verified': not differences, 'observed_at': utc_now(),
            'validated_fields': list(claims), 'supported_facts': actual,
            'stop_gate_access': access, 'guard_status': guard,
            'discrepancies': differences,
            'evidence': {'server_version': SERVER_VERSION, 'source': 'live MCP registry and persisted study guard',
                         'study_stop_record_sha256': (guard['study_stop'] or {}).get('record_sha256')},
            'verification_scope': 'Only explicitly supplied fields listed in validated_fields are checked. This tool does not test the host signing '
                'service or allocate resources; unverified assertions cannot be upgraded to facts. '
                'PASS does not validate narrative prose, scientific results, all branch restrictions or GPU visibility.',
            'execution_authorized': False, 'calculation_submitted': False,
            'next_action': 'Correct discrepant claims and retain unknown prerequisites; use separate result verification '
                           'and current resource probes for their own claims. No retry, stop reset or execution is authorized.'}
