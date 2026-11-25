"""Bounded recovery for explicitly safe observations, never for job submission."""
import errno
import time
from ..native_approval import NativeApprovalDenied, NativeApprovalReplay
from ..branch_guard import BranchStopped
from ..submission_identity import SubmissionInputConflict

# Code-owned allowlist, not tool-provided hints or model arguments.
RECOVERABLE_READS = frozenset({
    'covalent_get_job', 'covalent_get_job_log', 'covalent_list_jobs', 'covalent_wait_job',
})


def classify_exception(error):
    if isinstance(error, BranchStopped):
        return 'dependency_quarantined', False
    if isinstance(error, NativeApprovalReplay):
        return 'approval_already_consumed', False
    if isinstance(error, NativeApprovalDenied):
        return 'permission_denied', False
    if isinstance(error, PermissionError):
        return 'permission_denied', False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return 'transient_transport', True
    if isinstance(error, OSError) and error.errno in {errno.EAGAIN, errno.ECONNRESET, errno.ETIMEDOUT}:
        return 'transient_transport', True
    if isinstance(error, (ValueError, KeyError, FileNotFoundError)):
        return 'invalid_input_or_identity', False
    return 'internal_error', False


def execute_with_recovery(root, name, arguments, execute):
    started = time.monotonic()
    for attempt in range(1, 4):
        try:
            result = execute(root, name, arguments)
        except Exception as error:
            if isinstance(error, SubmissionInputConflict):
                return {'status': 'FAIL', 'error': str(error),
                        'execution_outcome': 'observation_failed',
                        'failure': {'category': 'invalid_input_or_identity', 'attempts': attempt,
                                    'automatic_retry_allowed': False,
                                    'verified_run_id': error.run_id,
                                    'next_action': 'stop_dependent_stages'}}
            category, transient = classify_exception(error)
            retryable = transient and name in RECOVERABLE_READS
            delay = 0.1 * attempt
            if retryable and attempt < 3 and time.monotonic() - started + delay < 5:
                time.sleep(delay)
                # Scheduling delays must not start another attempt after the
                # recovery window. This does not preempt an in-flight read.
                if time.monotonic() - started < 5:
                    continue
            return {'status': 'FAIL', 'error': f'{type(error).__name__}: {error}',
                    'failure': {'category': category, 'attempts': attempt,
                                'retry_exhausted': retryable, 'automatic_retry_allowed': False,
                                'next_action': ('stop_dependent_stages' if category == 'dependency_quarantined'
                                    else 'observe_original_job_later_do_not_resubmit' if retryable
                                    else 'report_permission_denial_do_not_bypass' if category == 'permission_denied'
                                    else 'reconcile_original_request_do_not_replay' if category == 'approval_already_consumed'
                                    else 'stop_and_report' if name in RECOVERABLE_READS else 'reconcile_submission_before_any_retry')},
                    'execution_outcome': 'observation_failed' if name in RECOVERABLE_READS else 'unknown_do_not_resubmit'}
        if result.get('chain', {}).get('terminal_state') == 'quality_gate_failed':
            result = {**result, 'status': 'FAIL', 'failure': {
                'category': 'scientific_quality_failure', 'attempts': attempt,
                'automatic_retry_allowed': False, 'next_action': 'stop_dependent_stages'}}
            return result
        if result.get('status') == 'FAIL':
            # Unstructured FAIL is never guessed to be transient.
            errors = result.get('errors')
            identity_failure = (name == 'covalent_verify_job_result' and isinstance(errors, list)
                and any(isinstance(error, str) and error.startswith('identity_mismatch:') for error in errors))
            result = {**result, 'failure': {
                'category': 'invalid_input_or_identity' if identity_failure else 'tool_or_quality_failure',
                'attempts': attempt, 'automatic_retry_allowed': False,
                'next_action': 'stop_dependent_stages' if identity_failure else 'stop_and_report'}}
        elif isinstance(result.get('job'), dict) and result['job'].get('state') in {'FAILED', 'TIMED_OUT', 'CANCELLED'}:
            # Observation cannot establish that this historical job belongs to
            # the active branch. Dependency verification owns that decision.
            # Keep failed states explicit without poisoning unrelated workflows.
            result = {**result, 'job_failure': {'category': 'calculation_failure', 'attempts': attempt,
                                          'automatic_retry_allowed': False,
                                          'next_action': 'report_failed_job; dependent_stages_require_verified_success; retries_require_separate_approval'}}
        elif attempt > 1:
            result = {**result, 'recovery': {'category': 'transient_transport', 'attempts': attempt}}
        return result
