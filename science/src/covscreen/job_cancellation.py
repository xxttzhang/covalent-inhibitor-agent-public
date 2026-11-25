"""Cooperative cancellation: a request is not evidence of process termination."""
import json
import os
import signal
import subprocess
import time

from .jobs import ArtifactRegistry, sha256_file, utc_now

PROTOCOL = 'cooperative-cancel-v1'


class JobCancelled(Exception):
    pass


def requested(registry, run_id):
    path = registry.job_dir(run_id) / 'cancellation.json'
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError('Symlinked cancellation request')
    value = json.loads(path.read_text())
    if (value.get('protocol') != PROTOCOL or value.get('run_id') != run_id
            or value.get('spec_sha256') != sha256_file(registry.job_dir(run_id) / 'spec.json')):
        raise ValueError('Cancellation request identity mismatch')
    return True


def validate_request(registry, run_id):
    """Read-only checks shared by approval planning and execution."""
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    if (spec.run_id != run_id or result.run_id != run_id
            or any(getattr(spec, field) != getattr(result, field)
                   for field in ('project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Cancellation job identity mismatch')
    if spec.provenance.get('worker_backend') in {'external-hpc', 'external-inspire', 'external-openmm'}:
        if (spec.route == 'ligand-first' and spec.provenance.get('worker_backend') == 'external-hpc'
                and spec.engine in {'schrodinger-covdock-vs', 'schrodinger-covdock-lo'}):
            from .scheduler_cancellation import scope
            scope(registry, run_id)
            if result.state not in {'QUEUED', 'RUNNING'}:
                raise ValueError('Scheduler cancellation requires an active job')
            return spec, result
        if spec.route == 'de-novo' and spec.provenance.get('worker_backend') == 'external-hpc':
            from .denovo_dispatch_recovery import validate
            return validate(registry, run_id)
        raise ValueError('External dispatch cancellation requires the scheduler handle, not a local PID')
    if spec.provenance.get('cancellation_protocol') != PROTOCOL:
        raise ValueError('Worker lacks the cooperative cancellation protocol; no signals sent')
    if result.state not in {'QUEUED', 'RUNNING'}:
        raise ValueError(f'job {run_id} is not cancellable from state {result.state}')
    return spec, result


def request(registry, run_id):
    spec, result = validate_request(registry, run_id)
    if spec.provenance.get('worker_backend') == 'external-hpc':
        if spec.route == 'ligand-first':
            from .scheduler_cancellation import request as request_scheduler
            request_scheduler(registry, run_id)
            return registry.load_result(run_id)
        from .denovo_dispatch_recovery import cancel
        return cancel(registry, run_id)
    if not requested(registry, run_id):
        ArtifactRegistry._write_json(registry.job_dir(run_id) / 'cancellation.json', {
            'protocol': PROTOCOL, 'run_id': run_id,
            'spec_sha256': sha256_file(registry.job_dir(run_id) / 'spec.json'),
            'requested_at': utc_now(),
        })
    # Only the worker writes terminal state, after observing process termination.
    return registry.load_result(run_id)


def terminate(process, grace_seconds=5):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    # A wrapper may exit on SIGTERM while its child ignores it. Clean up the
    # original process group as well; never signal the worker's own group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait(process, registry, run_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            return code
        if requested(registry, run_id):
            terminate(process)
            raise JobCancelled('Worker confirmed engine process termination')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            pass
