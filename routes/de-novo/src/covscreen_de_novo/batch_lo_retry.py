"""A separately approved second attempt after the known pre-engine supervisor failure."""
import fcntl
import hashlib
import os
from pathlib import Path

from .batch_lo_input import encoded

FAILURE = 'RuntimeError: Instance process supervision requires its original single-threaded worker'


def parent(root, digest):
    from .batch_lo_input import load
    from covscreen.jobs import ArtifactRegistry, sha256_file
    from covscreen.native_approval import verify_spec
    from covscreen.source_release import historical_release
    from covscreen.submission_identity import lookup_submission
    payload, folder = load(root, digest)
    if payload.get('retry_of'):
        raise ValueError('Only one explicit pre-engine LO recovery is supported')
    found = lookup_submission(root, {'project_id': payload['workflow_id'],
        'request_id': payload['workflow_id'] + '-lo-' + digest[:12], 'execution_mode': 'execute'})
    if not found.get('registered_job_observed'):
        raise ValueError('No original LO task to recover')
    registry = ArtifactRegistry(root)
    run = found['run_id']
    job = registry.job_dir(run)
    descriptor = os.open(job / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            # Concurrent readers may share proof validation; a worker's exclusive lock still blocks them.
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Original LO worker is still active') from error
        spec, result = registry.load_spec(run), registry.load_result(run)
        if result.pid is not None:
            process = Path('/proc') / str(result.pid) / 'cmdline'
            if not Path('/proc').is_dir():
                raise ValueError('Original worker liveness must be checked on its Linux instance')
            try:
                argv = process.read_bytes().split(b'\0')
            except FileNotFoundError:
                argv = []
            if run.encode() in argv:
                raise ValueError('Original LO worker is still active')
        verify_spec(spec)
        if spec.provenance.get('source_release'):
            historical_release(spec.provenance['source_release'])
        if (spec.route != 'de-novo' or spec.engine != 'schrodinger-covdock-lo'
                or spec.attempt != 1 or spec.parent_run_id is not None
                or spec.parameters.get('denovo_batch_lo_sha256') != digest
                or spec.input_sha256.get(str(folder.relative_to(root) / 'input.json')) != digest
                or result.state != 'FAILED' or result.failure_reason != FAILURE
                or result.return_code is not None or result.engine_pid is not None
                or result.candidate_table_path is not None or not result.finished_at
                or any(getattr(spec, key) != getattr(result, key) for key in
                    ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id'))):
            raise ValueError('LO recovery requires the verified pre-engine supervisor failure')
        for name in ('stdout.log', 'stderr.log'):
            file = job / name
            if file.is_symlink() or not file.is_file() or file.stat().st_size:
                raise ValueError('LO recovery found engine log output')
        output = Path(spec.output_directory)
        if output != job / 'artifacts' or output.is_symlink() or (output.exists() and any(output.iterdir())):
            raise ValueError('LO recovery found possible engine products')
        witness = {'run_id': run, 'manifest_sha256': digest,
            'spec_sha256': sha256_file(job / 'spec.json'), 'result_sha256': sha256_file(job / 'result.json'),
            'reason': 'pre-engine-single-threaded-supervisor-failure', 'attempt': 2}
    return payload, folder, witness


def validate(root, payload):
    original, _, witness = parent(root, payload['retry_of']['manifest_sha256'])
    if payload != {**original, 'retry_of': witness}:
        raise ValueError('LO retry changed its original inputs or failed parent')


def freeze(root, digest):
    from .batch_lo_input import load
    root = Path(root).resolve(strict=True)
    payload, folder = load(root, digest)
    if payload.get('retry_of'):
        return {**payload, 'directory': str(folder.relative_to(root)), 'manifest_sha256': digest}
    payload, original, witness = parent(root, digest)
    payload = {**payload, 'retry_of': witness}
    digest = hashlib.sha256(encoded(payload)).hexdigest()
    folder = root / 'state/de-novo-lo-inputs' / digest
    for name, raw in [('input.json', encoded(payload)),
                      ('review.json', (original / 'review.json').read_bytes()),
                      ('candidates.csv', (original / 'candidates.csv').read_bytes())]:
        file = folder / name
        if any(p.is_symlink() for p in (file, *file.parents)):
            raise ValueError('LO recovery input cannot traverse symlinks')
        folder.mkdir(parents=True, exist_ok=True)
        if not file.exists():
            with file.open('xb') as stream:
                stream.write(raw)
        if file.read_bytes() != raw:
            raise ValueError('Frozen LO recovery input changed')
    load(root, digest)
    return {**payload, 'directory': str(folder.relative_to(root)), 'manifest_sha256': digest}
