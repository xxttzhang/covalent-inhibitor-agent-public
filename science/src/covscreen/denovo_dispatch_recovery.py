"""End an undispatchable De novo attempt without claiming process termination.

Only the historically unsupported external-hpc route is eligible. A worker
lock excludes direct launch and the dispatch lock excludes reservation. The
old HPC boundary also rejects this route, including in frozen older releases.
No old spec, approval, input or scientific output is modified.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

from .jobs import sha256_file, utc_now
from .native_approval import CURRENT, decode, signed, verify_spec

PROTOCOL = 'denovo-undispatched-cancel-v1'
POLICY = 'denovo-independent-local-v1'


@contextmanager
def dispatch_lock(registry, run_id):
    path = registry.job_dir(run_id) / '.dispatch.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def validate(registry, run_id, *, terminal=False):
    spec, result = registry.load_spec(run_id), registry.load_result(run_id)
    verify_spec(spec)
    p, b = spec.parameters, spec.resource_budget
    if (spec.route != 'de-novo' or spec.engine != 'schrodinger-covdock-lo'
            or spec.provenance.get('worker_backend') != 'external-hpc'
            or spec.execution_mode != 'execute' or not spec.human_approved
            or spec.attempt != 1 or spec.parent_run_id is not None
            or (b.gpu_count, b.cpu_count, b.memory_gb, b.max_runtime_minutes, b.max_candidates) != (0, 1, 8, 30, 2)
            or p.get('input_row_count') != 2 or p.get('resume') is not False
            or p.get('dispatch_policy') is not None
            or not re.fullmatch('study-[a-f0-9-]+', str(p.get('workflow_id', '')))
            or not re.fullmatch('denovo-review-[a-f0-9]{32}', str(p.get('denovo_source_id', '')))
            or spec.request_id != p['denovo_source_id'] + '-covdock'
            or p['denovo_source_id'] != 'denovo-review-' + str(p.get('denovo_source_manifest_sha256', ''))[:32]):
        raise ValueError('Not an eligible undispatched De novo CovDock attempt')
    if any(getattr(spec, key) != getattr(result, key) for key in
           ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id')):
        raise ValueError('Undispatched cancellation identity mismatch')
    if (result.state != ('CANCELLED' if terminal else 'QUEUED')
            or any(getattr(result, key) is not None for key in
                   ('started_at', 'pid', 'engine_pid', 'return_code', 'runtime_seconds', 'candidate_table_path'))
            or result.artifacts
            or (not terminal and (result.finished_at is not None or result.failure_reason is not None))):
        raise ValueError('Job has execution or terminal evidence; undispatched cancellation refused')
    folder = registry.job_dir(run_id)
    allowed = {'spec.json', 'result.json', '.dispatch.lock', 'worker.lock', 'undispatched-cancellation.json'}
    if any(path.is_symlink() or not path.is_file() or path.name not in allowed for path in folder.iterdir()):
        raise ValueError('Reservation, worker claim or output exists; reconcile its actual execution')
    if Path(spec.output_directory) != folder/'artifacts' or Path(spec.output_directory).exists():
        raise ValueError('Unexpected output directory for undispatched job')
    # Check the original frozen plan, not the current executable release.
    from .layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.independent_review import validate_compiled_spec
    validate_compiled_spec(registry.project_root, spec)
    return spec, result


def receipt(registry, run_id):
    path = registry.job_dir(run_id)/'undispatched-cancellation.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing undispatched cancellation evidence')
    value = decode(json.loads(path.read_text())['proof'])
    expected = {'kind': 'cancel', 'run_id': run_id,
                'spec_sha256': sha256_file(registry.job_dir(run_id)/'spec.json')}
    approval = decode(value.get('approval'))
    if (value.get('protocol') != PROTOCOL or value.get('scope') != expected
            or approval.get('scope') != expected or approval.get('tool') != 'covalent_cancel_job'):
        raise ValueError('Undispatched cancellation receipt differs from native approval')
    return value


def confirmed(registry, run_id):
    spec, result = validate(registry, run_id, terminal=True)
    record = receipt(registry, run_id)
    if (result.failure_reason != 'authorized_cancellation_before_dispatch'
            or result.finished_at != record['confirmed_at']):
        raise ValueError('Cancellation result differs from its durable evidence')
    return spec, record


def cancel(registry, run_id):
    with dispatch_lock(registry, run_id):
        descriptor = os.open(registry.job_dir(run_id)/'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Worker owns this job; cannot prove it never launched') from error
            spec, result = validate(registry, run_id)
            proof = CURRENT.get()
            approval = decode(proof)
            expected = {'kind': 'cancel', 'run_id': run_id,
                        'spec_sha256': sha256_file(registry.job_dir(run_id)/'spec.json')}
            if approval.get('tool') != 'covalent_cancel_job' or approval.get('scope') != expected:
                raise ValueError('Exact native cancellation approval required')
            path = registry.job_dir(run_id)/'undispatched-cancellation.json'
            if not path.exists():
                record = {'protocol': PROTOCOL, 'scope': expected, 'approval': proof,
                          'confirmed_at': utc_now(), 'compute_started': False,
                          'reason': 'De novo is outside the historical external-hpc boundary'}
                registry._write_json(path, {'proof': signed(record)})
            record = receipt(registry, run_id)
            result.state = 'CANCELLED'
            result.finished_at = record['confirmed_at']
            result.failure_reason = 'authorized_cancellation_before_dispatch'
            result.message = 'Native cancellation confirmed: no reservation, worker or output; original spec retained.'
            registry.write_result(result)
            return result
