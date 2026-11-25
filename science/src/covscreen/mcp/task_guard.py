"""Opt-in, persistent stop-on-failure boundary for an isolated validation study.

This is not a substitute for native compute approval. A stopped study must be
reviewed outside the model; reconnecting the MCP cannot clear its stop record.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time
import uuid

# These operations cannot start/retry a calculation. Cancellation still passes
# through its normal native permission and backend approval checks.
from .stop_access import STOPPED_STUDY_TOOLS, describe_access, current_access


def guarded_call(root, name, arguments, execute, *, environment=None):
    env = os.environ if environment is None else environment
    classified = env.get("COVALENT_ERROR_POLICY") == "classified-v1"
    if env.get("COVALENT_STOP_ON_TOOL_FAILURE") != "1" and not classified:
        return execute(root, name, arguments)
    folder = Path(root) / "state" / "task_guard"
    folder.mkdir(parents=True, exist_ok=True)
    stopped = folder / "stopped.json"
    # Serialize callers across reconnects/processes, so a parallel call cannot
    # pass the gate while another call is recording failure.
    with (folder / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        already_stopped = stopped.exists()
        from .stop_review import active_review, REVIEWED_TOOLS, revoke_review
        review = active_review(root) if already_stopped and classified else None
        reviewed_call = bool(review and name in REVIEWED_TOOLS)
        allowed = STOPPED_STUDY_TOOLS | (REVIEWED_TOOLS if review else frozenset())
        if already_stopped and not (classified and name in allowed):
            return {"status": "FAIL", "error": "Study stopped after a tool failure; operator review required",
                    "stop_record": str(stopped), "calculation_submitted": False,
                    "allowed_tools": sorted(allowed) if classified else [],
                    "stop_gate_access": describe_access(allowed if classified else frozenset())}
        try:
            if classified:
                from .error_policy import execute_with_recovery
                result = execute_with_recovery(root, name, arguments, execute)
            else:
                result = execute(root, name, arguments)
        except Exception as exc:
            result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        if already_stopped:
            if review and (result.get('status') == 'FAIL' or result.get('verified') is False):
                revoke_review(root, review, name, result.get('failure', {}).get('category', 'verification_or_tool_failure'))
            # Diagnostics never clear or overwrite the original failure, even
            # when the job has since completed or cancellation is successful.
            return {**result, "stop_gate_access": current_access(root, environment=env),
                    "task_guard": {"execution_blocked": True,
                    "stop_record": str(stopped), "original_failure_preserved": True,
                    "maintenance_review_id": review, "reviewed_diagnostic_call": reviewed_call}}
        terminal_verification_failure = (classified and name == 'covalent_verify_job_result'
            and result.get('verified') is False
            and result.get('job_state') in {'FAILED', 'TIMED_OUT', 'CANCELLED'})
        if (result.get("status") == "FAIL" or terminal_verification_failure
                or (classified and result.get("failure", {}).get("category") == "calculation_failure")):
            if classified and result.get('failure', {}).get('category') == 'dependency_quarantined':
                return {**result, 'task_guard': {'study_stopped': False,
                                               'dependent_execution_blocked': True}}
            submission_conflict = (name == 'covalent_lookup_submission'
                and result.get('failure', {}).get('category') == 'invalid_input_or_identity'
                and result.get('failure', {}).get('verified_run_id'))
            if classified and (name == 'covalent_verify_job_result' or submission_conflict):
                from ..branch_guard import quarantine_job, guard_status
                try:
                    run_id = submission_conflict or arguments['run_id']
                    marker = quarantine_job(root, run_id, name,
                                            result.get('failure', {}).get('category', 'verification_failure'))
                except (ValueError, KeyError, TypeError, OSError):
                    # An unresolved identity cannot safely establish isolation.
                    pass
                else:
                    return {**result, 'guard_status': guard_status(root, {}),
                            'task_guard': {'study_stopped': False,
                            'dependent_execution_blocked': True, 'branch_stop_record': marker}}
            from .error_policy import RECOVERABLE_READS
            if (classified and name in RECOVERABLE_READS
                    and result.get('execution_outcome') == 'observation_failed'
                    and result.get('failure', {}).get('category') == 'transient_transport'):
                # An unavailable observation is not evidence that a job failed.
                # Persist the incident, but leave subsequent observation and
                # independent work possible. Never submit/retry a calculation.
                incidents = folder / 'observation_failures'
                incidents.mkdir(exist_ok=True)
                incident = incidents / f'{uuid.uuid4().hex}.json'
                with incident.open('x') as stream:
                    json.dump({'tool': name, 'observed_at': time.time(),
                               'status': 'FAIL', 'failure': result['failure'],
                               'job_state': 'UNKNOWN'}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                return {**result, 'task_guard': {
                    'incident_record': str(incident), 'study_stopped': False,
                    'job_state_inferred': False}}
            # No arguments or approval tokens are persisted in the stop marker.
            with stopped.open("x") as stream:
                json.dump({"tool": name, "stopped_at": time.time(), "status": "FAIL", "failure": result.get("failure")}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        return result
