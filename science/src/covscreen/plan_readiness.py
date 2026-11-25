"""Evidence-bounded execution prerequisites, never an execution authorization."""
from .branch_guard import guard_status
from .jobs import utc_now


def plan_readiness(root, request):
    guard = guard_status(root, {})
    blocked = guard['study_stop'] is not None
    return {
        'schema': 'plan-readiness-v1', 'observed_at': utc_now(),
        'plan_available': request is not None,
        'execution_authorized_by_this_observation': False,
        'observation_phase': 'BEFORE_ANY_SUBMISSION',
        'study_execution_policy': 'BLOCKED' if blocked else 'NO_STUDY_STOP_OBSERVED',
        'study_stop_record_sha256': (guard['study_stop'] or {}).get('record_sha256'),
        'branch_policy': 'RECHECK_DEPENDENCIES_AT_EXECUTION',
        'approval': {
            'requirement': 'EXACT_NATIVE_SIGNED_RECEIPT',
            'approval_granted_by_this_plan': False,
            'host_signing_service_availability': 'NOT_VERIFIED',
            'host_upgrade_required': 'NOT_ESTABLISHED',
            'evidence': 'Planning neither probes the host signing service nor grants approval. '
                        'A study stop or an unupgraded host version does not prove that service is unavailable.',
        },
        'resources': {
            'availability': 'NOT_VERIFIED_FOR_THIS_PLAN',
            'requested_budget': request.resource_budget.model_dump(mode='json') if request else None,
            'required_check_tool': 'covalent_runtime_preflight',
            'evidence': 'This plan compilation does not probe CPU/GPU availability or reserve resources. '
                        'Registered assets, health PASS and historical probes do not establish current resource readiness.',
        },
        'reporting_requirements': [
            'Use covalent_verify_report_claims to check the five prerequisite claims before final reporting; '
            'its PASS does not certify the rest of the report.',
            'If reporting stop-gate permissions, also verify stop_gate_tool_count, stop_gate_all_tools_read_only '
            'and cancellation_requires_native_approval. A tool passing the stop gate is not an approval grant.',
            'Distinguish planned, submitted, running, completed and independently verified results.',
            'Report unverified prerequisites as unknown, never as available or unavailable.',
            'Preserve study stops; diagnostic review is not execution authorization.',
            'Require a current resource check matching this budget before describing execution as ready.',
        ],
    }
