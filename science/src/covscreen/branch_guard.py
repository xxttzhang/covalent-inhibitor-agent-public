"""Persistent quarantine of registered job outputs and their dependents.

No model-supplied project label or approval flag can clear a quarantine.
Unresolved failures remain the responsibility of the study-wide guard.
"""
import json
import hashlib
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .jobs import ArtifactRegistry, sha256_file, utc_now
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class GuardStatusInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: StrictInt = Field(default=50, ge=1, le=100)


def guard_status(root, arguments):
    """Read persisted guard evidence without clearing or approving anything."""
    request = GuardStatusInput.model_validate(arguments)
    root = Path(root).resolve()
    folder = root / 'state' / 'branch_guard'
    branches = []
    record_hashes = {}
    for marker in sorted(folder.glob('*.json')):
        if marker.is_symlink() or not marker.resolve().is_relative_to(root):
            raise ValueError('Invalid quarantine registry entry')
        raw = marker.read_bytes()
        record = json.loads(raw)
        if record['schema'] != 'job-quarantine-v1' or marker.stem != record['run_id']:
            raise ValueError('Invalid quarantine registry identity')
        record_hashes[marker.name] = hashlib.sha256(raw).hexdigest()
        branches.append({key: record[key] for key in
            ('run_id', 'recorded_at', 'tool', 'category', 'spec_sha256')})
    branches.sort(key=lambda item: (item['recorded_at'], item['run_id']), reverse=True)
    stopped = root / 'state' / 'task_guard' / 'stopped.json'
    global_stop = None
    if stopped.exists():
        if stopped.is_symlink():
            raise ValueError('Invalid study stop entry')
        raw = stopped.read_bytes()
        record = json.loads(raw)
        global_stop = {'tool': record['tool'], 'stopped_at': record['stopped_at'],
                       'category': (record.get('failure') or {}).get('category', 'unclassified'),
                       'record_sha256': hashlib.sha256(raw).hexdigest()}
    snapshot = {'quarantines': record_hashes,
                'study_stop': (global_stop or {}).get('record_sha256')}
    snapshot_sha256 = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    scope = 'study_wide' if global_stop is not None else 'dependent_branches' if branches else 'none_observed'
    return {'status': 'PASS', 'study_stop': global_stop,
            'execution_stop_scope': scope,
            'scope_meaning': {
                'study_wide': 'New scientific execution is blocked throughout this study, including independent branches. '
                              'Zero quarantined jobs does not narrow a study-wide stop. Permitted diagnostics are not execution approval.',
                'dependent_branches': 'Recorded branches and their dependencies are blocked. Independent branches still require normal checks and native approval.',
                'none_observed': 'No persisted study or branch stop was observed. This does not establish readiness or grant approval.',
            }[scope],
            'observed_at': utc_now(), 'guard_snapshot_sha256': snapshot_sha256,
            'quarantined_jobs': branches[:request.limit], 'total_quarantined_jobs': len(branches),
            'truncated': len(branches) > request.limit,
            'execution_authorized': False, 'calculation_submitted': False,
            'policy': 'Original records remain active. Quarantine extends to dependent jobs and copied registered outputs. '
                      'Reverification does not clear it; independent work still requires normal input checks and native approval. '
                      'A study-wide stop takes precedence; a diagnostic review is never execution approval.',
            'next_action': 'This is a point-in-time snapshot. After any failure or quarantine, refresh it before reporting current guard state. '
                           'Verify execution_stop_scope, quarantined_job_count and guard_snapshot_sha256 with covalent_verify_report_claims before final reporting. '
                           'Report blockers; inspect original jobs/logs and generate only permitted plans. '
                           'Do not retry or change inputs to evade a stop.'}


class BranchStopped(ValueError):
    pass


@contextmanager
def execution_barrier(root):
    """Serialize quarantine publication with registration and process launch.

    Hold only through the boundary, never through the lifetime of a worker.
    Callers must recheck dependencies while holding this lock.
    """
    folder = Path(root).resolve() / 'state' / 'branch_guard'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _outputs(root, spec, result):
    output = (root / spec.output_directory).resolve()
    paths = {(root / value).resolve() for value in spec.expected_output_paths}
    paths.add(output)
    digests = set()
    for artifact in result.artifacts:
        path = (root / artifact.path).resolve()
        if path.is_relative_to(output) or any(path == p or path.is_relative_to(p) for p in paths):
            paths.add(path)
            digests.add(artifact.sha256)
    return paths, digests


def _uses_outputs(request, paths, digests):
    inputs = getattr(request, 'input_sha256', {})
    # All paths in compiled specs are resolved against the same study root by
    # the caller; hashes also catch unchanged copies under unrelated names.
    return bool(set(inputs.values()) & digests) or any(
        p == o or p.is_relative_to(o) for p in request._guard_input_paths for o in paths)


def quarantine_job(root, run_id, tool, category):
    with execution_barrier(root):
        return _quarantine_job(root, run_id, tool, category)


def _quarantine_job(root, run_id, tool, category):
    root = Path(root).resolve()
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    if (spec.run_id != run_id or result.run_id != run_id
            or any(getattr(spec, field) != getattr(result, field)
                   for field in ('project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Cannot attribute quarantine: registered job identity mismatch')
    paths, digests = _outputs(root, spec, result)
    marker = root / 'state' / 'branch_guard' / f'{run_id}.json'
    payload = {'schema': 'job-quarantine-v1', 'run_id': run_id,
               'recorded_at': utc_now(), 'tool': tool, 'category': category,
               'spec_sha256': sha256_file(registry.job_dir(run_id) / 'spec.json'),
               'output_paths': sorted(map(str, paths)), 'output_sha256': sorted(digests),
               'execution_authorized': False}
    marker.parent.mkdir(parents=True, exist_ok=True)
    # guarded_call serializes recording. Subsequent verification never clears
    # or replaces the first failure, including after apparent repair.
    if not marker.exists():
        ArtifactRegistry._write_json(marker, payload)
    return str(marker)


def require_unquarantined(root, request):
    root = Path(root).resolve()
    folder = root / 'state' / 'branch_guard'
    markers = list(folder.glob('*.json'))
    if not markers:
        return
    registry = ArtifactRegistry(root)
    ancestry = set()
    current = getattr(request, 'run_id', None) or request.parent_run_id
    while current:
        if current in ancestry or len(ancestry) >= 100:
            raise ValueError('Invalid or oversized job ancestry')
        ancestry.add(current)
        # An unregistered freshly compiled spec is still checked by its parent.
        if current == getattr(request, 'run_id', None):
            current = request.parent_run_id
        else:
            current = registry.load_spec(current).parent_run_id
    def input_view(value):
        from types import SimpleNamespace
        inputs = getattr(value, 'input_sha256', {})
        paths = {(root / p).resolve() for p in inputs}
        paths.update((root / p).resolve() for p in getattr(value, 'input_paths', []))
        return SimpleNamespace(input_sha256=inputs, _guard_input_paths=paths)

    # Recompute the dependency closure from registered identities, including
    # previously created intermediate jobs. A caller cannot evade a stopped
    # ancestor by omitting parent_run_id while reusing a descendant's outputs.
    specs = []
    for path in sorted(registry.root.glob('*/spec.json')):
        spec = registry.load_spec(path.parent.name)
        if spec.run_id != path.parent.name:
            raise ValueError('Registered job identity mismatch')
        specs.append((spec, input_view(spec)))
    for marker in markers:
        if marker.is_symlink():
            raise ValueError('Invalid quarantine registry entry')
        record = json.loads(marker.read_text())
        if record['schema'] != 'job-quarantine-v1' or marker.stem != record['run_id']:
            raise ValueError('Invalid quarantine registry identity')
        stopped = {record['run_id']}
        paths = {Path(p) for p in record['output_paths']}
        digests = set(record['output_sha256'])
        changed = True
        while changed:
            changed = False
            for spec, view in specs:
                if spec.run_id not in stopped and (spec.parent_run_id in stopped
                        or _uses_outputs(view, paths, digests)):
                    stopped.add(spec.run_id)
                    outputs, hashes = _outputs(root, spec, registry.load_result(spec.run_id))
                    paths.update(outputs)
                    digests.update(hashes)
                    changed = True
        affected = bool(stopped & ancestry) or _uses_outputs(input_view(request), paths, digests)
        if affected:
            raise BranchStopped('Dependency quarantined: ' + record['run_id']
                                + '; review required; changing project labels or approval does not clear it')
