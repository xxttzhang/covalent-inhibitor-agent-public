"""At-most-once registration by request identity, with crash-safe reservations."""
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Literal
from pydantic import Field
from .site_contracts import StrictInput

from .jobs import ArtifactRegistry, sha256_file, utc_now


class SubmissionInputConflict(ValueError):
    """Input conflict attributed only after receipt/spec/result identity checks."""
    def __init__(self, run_id):
        super().__init__('Submission frozen input changed or missing; keep dependent execution blocked')
        self.run_id = run_id


def _verify_job_identity(spec, result, identity, run_id):
    if ([spec.project_id, spec.request_id, spec.execution_mode] != identity
            or spec.run_id != run_id or result.run_id != run_id
            or any(getattr(result, name) != getattr(spec, name)
                   for name in ('project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Submission job identity mismatch')


def submit_once(manager, request, submit, *, planned_run_id=None):
    identity = [request.project_id, request.request_id, request.execution_mode]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    content = request.model_dump(mode='json', exclude={'human_approved', 'approval_note'})
    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    folder = manager.project_root / 'state' / 'submissions'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.json')
    with (folder / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            receipt = json.loads(path.read_text())
            if (receipt.get('identity') != identity
                    or (receipt.get('planned_run_id') and receipt.get('run_id')
                        and receipt['planned_run_id'] != receipt['run_id'])):
                raise ValueError('Submission receipt identity mismatch')
            if receipt['fingerprint'] != fingerprint:
                raise ValueError('Request identity reused with changed inputs, parameters or budget')
            if receipt['state'] != 'REGISTERED':
                raise ValueError('Submission outcome is uncertain or failed; reconcile the existing request before any retry')
            spec = manager.registry.load_spec(receipt['run_id'])
            result = manager.registry.load_result(receipt['run_id'])
            _verify_job_identity(spec, result, identity, receipt['run_id'])
            for name, digest in spec.input_sha256.items():
                source = manager.project_root / name
                if not source.is_file() or sha256_file(source) != digest:
                    raise ValueError('Existing request frozen input changed; do not resubmit')
            return result
        # Persist before any launch. A crash leaves an explicit uncertainty record,
        # which can never silently cause a second launch after reconnecting.
        receipt = {'identity': identity, 'fingerprint': fingerprint, 'state': 'RESERVED', 'created_at': utc_now()}
        if planned_run_id is not None:
            receipt['planned_run_id'] = planned_run_id
        ArtifactRegistry._write_json(path, receipt)
        try:
            result = submit(request)
        except Exception:
            receipt['state'] = 'UNCERTAIN_OR_FAILED'
            ArtifactRegistry._write_json(path, receipt)
            raise
        receipt.update(state='REGISTERED', run_id=result.run_id)
        ArtifactRegistry._write_json(path, receipt)
        return result


class SubmissionLookupInput(StrictInput):
    project_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    execution_mode: Literal['plan', 'execute']


def lookup_submission(root, arguments):
    """Observe one exact request identity; never clear reservations or relaunch."""
    from .jobs import JobSpec, RunResult
    import re
    args = SubmissionLookupInput.model_validate(arguments)
    root = Path(root).resolve()
    identity = [args.project_id, args.request_id, args.execution_mode]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    path = root / 'state/submissions' / (key + '.json')
    response = {'status': 'PASS', 'identity': args.model_dump(),
                'calculation_submitted': False, 'automatic_resubmission_allowed': False,
                'scientific_result_verified': False}
    if not path.exists():
        return {**response, 'submission_state': 'NO_RECEIPT',
                'next_action': 'Absence of a receipt does not prove no historical execution. Inspect registered jobs before a separately approved new request.'}
    def read(bound):
        if bound.is_symlink() or not bound.resolve().is_relative_to(root):
            raise ValueError('Submission observation path escaped registry')
        return json.loads(bound.read_text())
    receipt = read(path)
    if receipt.get('identity') != identity or receipt.get('state') not in {'RESERVED', 'REGISTERED', 'UNCERTAIN_OR_FAILED'}:
        raise ValueError('Submission receipt identity or state mismatch')
    response['submission_state'] = receipt['state']
    if (receipt.get('run_id') and receipt.get('planned_run_id')
            and receipt['run_id'] != receipt['planned_run_id']):
        raise ValueError('Submission reserved/registered identity mismatch')
    run_id = receipt.get('run_id') or receipt.get('planned_run_id')
    if run_id is None:
        return {**response, 'next_action': 'Legacy uncertain receipt has no bound run_id. Keep execution blocked; reconcile registered jobs without resubmitting.'}
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{5,127}', run_id):
        raise ValueError('Invalid bound submission run_id')
    folder = root / 'state/jobs' / run_id
    if not (folder / 'spec.json').exists() or not (folder / 'result.json').exists():
        if receipt['state'] == 'REGISTERED':
            raise ValueError('Registered submission job evidence is missing')
        return {**response, 'reserved_run_id': run_id, 'registered_job_observed': False,
                'next_action': 'Registration is absent or incomplete. Outcome remains uncertain; do not resubmit.'}
    spec = JobSpec.model_validate(read(folder / 'spec.json'))
    result = RunResult.model_validate(read(folder / 'result.json'))
    _verify_job_identity(spec, result, identity, run_id)
    for name, digest in spec.input_sha256.items():
        source = root / name
        if not source.is_file() or sha256_file(source) != digest:
            raise SubmissionInputConflict(run_id)
    return {**response, 'run_id': run_id, 'registered_job_observed': True,
            'job_state': result.state, 'spec_sha256': sha256_file(folder / 'spec.json'),
            'next_action': 'Observe this existing run_id; verify outputs before advancing. This observation does not clear a failed reservation or restore approval.'}
