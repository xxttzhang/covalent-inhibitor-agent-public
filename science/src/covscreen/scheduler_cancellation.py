"""Signed cancellation intent for an already reserved Ligand HPC workload.

This module never signals a PID or stops a scheduler. A terminal result is
written only after an exact host scheduler observation and proof that the
scientific worker never started. Started jobs need separate vendor evidence.
"""
import json
import fcntl
import os
from pathlib import Path

from .jobs import ArtifactRegistry, utc_now
from .native_approval import CURRENT, decode, signed

PROTOCOL = 'reserved-hpc-cancellation-v1'


def scope(registry: ArtifactRegistry, run_id: str) -> dict:
    from .external_hpc import inspect

    record = inspect(registry.project_root, run_id)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    if spec.provenance.get('source_release'):
        from .source_release import historical_release
        historical_release(spec.provenance['source_release'])
    if (spec.route != 'ligand-first'
            or spec.engine not in {'schrodinger-covdock-vs', 'schrodinger-covdock-lo'}
            or spec.provenance.get('worker_backend') != 'external-hpc'
            or any(getattr(spec, field) != getattr(result, field) for field in
                   ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id'))):
        raise ValueError('Cancellation requires the exact Ligand HPC job identity')
    reservation = registry.job_dir(run_id) / 'external_hpc.json'
    if reservation.is_symlink() or not reservation.is_file():
        raise ValueError('Cancellation requires a regular scheduler reservation')
    receipt = record.get('receipt')
    if (not isinstance(receipt, dict) or receipt.get('name') != record['name']
            or receipt.get('spec_sha256') != record['spec_sha256']):
        raise ValueError('Cancellation scheduler reservation differs from the approved job')
    return {'kind': 'cancel', 'run_id': run_id, 'spec_sha256': record['spec_sha256'],
            'scheduler': {'backend': 'external-hpc', 'name': record['name']}}


def read_intent(registry: ArtifactRegistry, run_id: str) -> dict:
    path = registry.job_dir(run_id) / 'scheduler-cancellation.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing regular scheduler cancellation intent')
    value = decode(json.loads(path.read_text())['proof'])
    approval = decode(value.get('approval'))
    expected = scope(registry, run_id)
    if (value.get('protocol') != PROTOCOL or value.get('scope') != expected
            or approval.get('tool') != 'covalent_cancel_job'
            or approval.get('scope') != expected):
        raise ValueError('Scheduler cancellation intent differs from native approval')
    return value


def request(registry: ArtifactRegistry, run_id: str) -> dict:
    from .denovo_dispatch_recovery import dispatch_lock

    with dispatch_lock(registry, run_id):
        expected = scope(registry, run_id)
        if registry.load_result(run_id).state not in {'QUEUED', 'RUNNING'}:
            raise ValueError('Scheduler cancellation requires an active job')
        proof = CURRENT.get()
        approval = decode(proof)
        if approval.get('tool') != 'covalent_cancel_job' or approval.get('scope') != expected:
            raise ValueError('Exact native scheduler cancellation approval required')
        path: Path = registry.job_dir(run_id) / 'scheduler-cancellation.json'
        if path.is_symlink():
            raise ValueError('Symlinked scheduler cancellation intent')
        if not path.exists():
            value = {'protocol': PROTOCOL, 'scope': expected, 'approval': proof,
                     'requested_at': utc_now(), 'termination_confirmed': False}
            with path.open('x') as stream:
                path.chmod(0o600)
                json.dump({'proof': signed(value)}, stream)
        return read_intent(registry, run_id)


def confirm_unstarted(registry: ArtifactRegistry, run_id: str, observation: dict) -> dict:
    """Reconcile a scheduler stop only when no scientific worker ever started.

    Called by the trusted host adapter after querying the exact scheduler, not
    exposed as a model tool. Started workloads require separate vendor evidence.
    """
    from .denovo_dispatch_recovery import dispatch_lock

    with dispatch_lock(registry, run_id):
        intent = read_intent(registry, run_id)
        expected = intent['scope']
        if (observation.get('name') != expected['scheduler']['name']
                or observation.get('run_id') != run_id
                or observation.get('spec_sha256') != expected['spec_sha256']
                or observation.get('status') not in {'STOPPED', 'CANCELLED'}):
            raise ValueError('Exact stopped scheduler observation required')
        folder = registry.job_dir(run_id)
        descriptor = os.open(folder / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'cancellation_confirmed': False, 'reason': 'worker_still_owns_job'}
            result = registry.load_result(run_id)
            receipt = folder / 'scheduler-cancellation-confirmed.json'
            if receipt.is_symlink():
                raise ValueError('Symlinked cancellation confirmation')
            if receipt.exists():
                proof = decode(json.loads(receipt.read_text())['proof'])
                if proof.get('scope') != expected or result.state not in {'QUEUED', 'CANCELLED'}:
                    raise ValueError('Cancellation confirmation/result mismatch')
                if result.state == 'CANCELLED':
                    return proof
            if (result.state != 'QUEUED' or result.artifacts
                    or any(getattr(result, key) is not None for key in
                           ('started_at', 'pid', 'engine_pid', 'return_code', 'runtime_seconds', 'finished_at'))
                    or any((folder / name).exists() or (folder / name).is_symlink() for name in
                           ('external_worker_claim', 'vs_worker_claim', 'artifacts', 'stdout.log', 'stderr.log'))):
                return {'cancellation_confirmed': False,
                        'reason': 'execution_evidence_requires_worker_and_vendor_reconciliation'}
            proof = decode(json.loads(receipt.read_text())['proof']) if receipt.exists() else {
                     'scope': expected, 'scheduler_observation': observation,
                     'confirmed_at': utc_now(), 'cancellation_confirmed': True,
                     'worker_never_started': True, 'vendor_execution': 'not_started'}
            registry._write_json(receipt, {'proof': signed(proof)})
            result.state = 'CANCELLED'
            result.finished_at = proof['confirmed_at']
            result.failure_reason = 'authorized_scheduler_stop_before_worker_start'
            result.message = 'Scheduler stopped; no scientific worker or vendor execution was started.'
            registry.write_result(result)
            return proof
