"""Describe the study-stop gate without granting native permissions."""
import os
from pathlib import Path

STOPPED_STUDY_TOOLS = frozenset({
    'covalent_verify_report_claims', 'covalent_get_guard_status',
    'covalent_lookup_submission', 'covalent_get_job', 'covalent_get_job_log',
    'covalent_list_jobs', 'covalent_wait_job', 'covalent_list_workflows',
    'covalent_cancel_job',
})
PLANNING_TOOLS = frozenset({'covalent_library_first_prepare_docking', 'covalent_resume_workflow'})


def describe_access(allowed):
    names = sorted(allowed)
    cancellation = sorted(set(names) & {'covalent_cancel_job'})
    planning = sorted(set(names) & PLANNING_TOOLS)
    observations = sorted(set(names) - set(cancellation) - set(planning))
    return {
        'gate_active': True, 'tools': names, 'tool_count': len(names),
        'observation_tools': observations, 'planning_and_workflow_tools': planning,
        'cancellation_tools': cancellation,
        'all_tools_read_only': not cancellation and not planning,
        'cancellation_requires_native_approval': True,
        'grants_native_permission': False, 'authorizes_scientific_execution': False,
        'meaning': 'These tools pass only the study-stop gate. Planning may persist workflow state. '
                   'Cancellation is a state-changing operation requiring separate native approval and backend checks. '
                   'This list is not a read-only list or a permission grant.',
        'historical_job_status_tool': 'covalent_get_job',
        'historical_job_status_guidance': 'Use covalent_get_job for historical status. A status action inside '
            'a blocked multi-action prediction tool does not bypass this gate. Do not retry the blocked tool.',
    }


def current_access(root, *, environment=None):
    env = os.environ if environment is None else environment
    if (not (Path(root) / 'state/task_guard/stopped.json').exists()
            or (env.get('COVALENT_ERROR_POLICY') != 'classified-v1'
                and env.get('COVALENT_STOP_ON_TOOL_FAILURE') != '1')):
        return {'gate_active': False, 'tool_count': None, 'all_tools_read_only': None,
                'cancellation_requires_native_approval': True, 'grants_native_permission': False,
                'authorizes_scientific_execution': False}
    if env.get('COVALENT_ERROR_POLICY') != 'classified-v1':
        return describe_access(frozenset())
    from .stop_review import active_review, REVIEWED_TOOLS
    return describe_access(STOPPED_STUDY_TOOLS | (REVIEWED_TOOLS if active_review(root) else frozenset()))
