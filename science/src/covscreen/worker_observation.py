"""Observe local worker ownership without creating locks or changing job state."""
import fcntl
import os
import stat


def observe_worker(registry, spec, result):
    response = {'recorded_job_state': result.state, 'engine_liveness_verified': False,
                'automatic_resubmission_allowed': False,
                'meaning': 'A persisted RUNNING state is not proof of a live engine. '
                           'Lock observation never proves engine termination or authorizes a restart.'}
    if result.state != 'RUNNING' or spec.provenance.get('worker_backend') != 'local':
        return {**response, 'status': 'NOT_CHECKED', 'reconciliation_required': False}
    # Only probe RUNNING: taking a lock on QUEUED could race the first worker claim.
    path = registry.job_dir(spec.run_id) / 'worker.lock'
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Worker lock is not a regular file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**response, 'status': 'LOCK_HELD_OWNER_UNVERIFIED',
                    'reconciliation_required': False}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        return {**response, 'status': 'OBSERVATION_UNAVAILABLE',
                'error_type': type(error).__name__, 'reconciliation_required': True}
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {**response, 'status': 'NO_WORKER_LOCK_OBSERVED', 'reconciliation_required': True,
            'next_action': 'reconcile_original_job_do_not_resubmit',
            'meaning': response['meaning'] + ' The local coordinator was not observed holding its lock. '
                       'The engine may still be running independently; preserve the original run_id and inspect it.'}
