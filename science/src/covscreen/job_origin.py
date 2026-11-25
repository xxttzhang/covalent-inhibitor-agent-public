"""Signed origin references for explicitly reused historical jobs.

Maintenance registers references; models can only verify them. No job records,
scientific inputs, approvals or historical results are rewritten or copied.
"""
from pathlib import Path
import json

from .jobs import ArtifactRegistry, sha256_file, utc_now
from .native_approval import signed, decode


def _binding(destination, source, run_id):
    destination, source = Path(destination).resolve(), Path(source).resolve()
    if destination == source:
        raise ValueError('Historical job origin must be a distinct study')
    local = ArtifactRegistry(destination)
    original = ArtifactRegistry(source)
    local_spec, source_spec = local.load_spec(run_id), original.load_spec(run_id)
    local_result, source_result = local.load_result(run_id), original.load_result(run_id)
    if local_spec != source_spec or local_result != source_result:
        raise ValueError('Copied job metadata differs from historical origin')
    if (source_result.state != 'COMPLETED' or source_result.return_code != 0
            or source_spec.run_id != run_id or source_result.run_id != run_id):
        raise ValueError('Only completed historical jobs may register an origin')
    hashes = {}
    for name in ('spec.json', 'result.json'):
        paths = [registry.job_dir(run_id) / name for registry in (local, original)]
        if any(p.is_symlink() for p in paths) or paths[0].read_bytes() != paths[1].read_bytes():
            raise ValueError('Historical job metadata bytes differ')
        hashes[name] = sha256_file(paths[0])
    # Present copies may not silently disagree with authoritative artifacts.
    # Missing copies are explicitly resolved at the signed source, not fabricated.
    for artifact in local_result.artifacts:
        path = destination / artifact.path
        if path.is_symlink() or not path.resolve().is_relative_to(destination):
            raise ValueError('Local historical artifact path escaped study')
        if path.exists() and (not path.is_file() or sha256_file(path) != artifact.sha256):
            raise ValueError('Local historical artifact differs from registered hash')
    return {'destination_root': str(destination), 'source_root': str(source), 'run_id': run_id,
            'spec_sha256': hashes['spec.json'], 'result_sha256': hashes['result.json']}


def register_origin(destination, source, run_id, *, reason):
    """Trusted maintenance only; intentionally not exposed as an MCP tool."""
    from .result_verification import verify_job
    if len(reason.strip()) < 20:
        raise ValueError('Explicit historical reuse reason required')
    binding = _binding(destination, source, run_id)
    source_receipt = Path(source) / 'state/job_origins' / (run_id + '.json')
    if source_receipt.exists():
        raise ValueError('Chained historical origins are not supported')
    verification = verify_job(Path(source), run_id)
    if verification.get('verified') is not True:
        raise ValueError('Historical origin failed live result verification: ' + ', '.join(verification.get('errors', [])))
    payload = {'kind': 'historical-job-origin-v1', **binding, 'registered_at': utc_now(),
               'reason': reason, 'execution_authorized': False, 'historical_reuse': True}
    path = Path(destination) / 'state/job_origins' / (run_id + '.json')
    if path.exists():
        raise ValueError('Origin already registered; inspect rather than overwrite')
    proof = signed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as handle:
        json.dump({'proof': proof}, handle)
    return {**binding, 'historical_reuse': True, 'execution_authorized': False}


def verify_origin(root, run_id):
    """Return None for native jobs; fail closed on invalid registered origins."""
    root = Path(root).resolve()
    path = root / 'state/job_origins' / (run_id + '.json')
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('Symlinked historical origin receipt')
    payload = decode(json.loads(path.read_text())['proof'])
    if (payload.get('kind') != 'historical-job-origin-v1' or payload.get('destination_root') != str(root)
            or payload.get('run_id') != run_id or payload.get('execution_authorized') is not False
            or payload.get('historical_reuse') is not True):
        raise ValueError('Invalid historical origin receipt scope')
    source = Path(payload['source_root'])
    binding = _binding(root, source, run_id)
    if any(payload.get(k) != v for k, v in binding.items()):
        raise ValueError('Historical origin metadata changed after registration')
    if (source / 'state/job_origins' / (run_id + '.json')).exists():
        raise ValueError('Chained historical origins are not supported')
    from .result_verification import verify_job
    result = verify_job(source, run_id)
    return {**result, 'historical_reuse': True, 'origin': binding,
            'approval_inherited': False, 'calculation_submitted': False}
