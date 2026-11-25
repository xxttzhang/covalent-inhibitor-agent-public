"""Maintenance-only, signed recovery of diagnostics after a reviewed false stop.

No MCP tool exposes issuance. This never authorizes execution or deletes a stop.
"""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time

from ..jobs import ArtifactRegistry, sha256_file
from ..native_approval import decode, signed

REVIEWED_TOOLS = frozenset({
    'covalent_verify_job_result', 'covalent_get_candidates',
    'covalent_library_first_prepare_docking', 'covalent_resume_workflow',
    'covalent_capabilities', 'covalent_platform_health', 'covalent_runtime_preflight',
})


def issue_observation_review(root, *, expected_stop_sha256, evidence_sha256, reason):
    """Called by trusted maintenance after auditing the historical-read defect."""
    root = Path(root).resolve()
    folder = root / 'state/task_guard'
    if not re.fullmatch(r'[a-f0-9]{64}', evidence_sha256) or len(reason.strip()) < 20:
        raise ValueError('An audit evidence hash and meaningful review reason are required')
    with (folder / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stop = folder / 'stopped.json'
        if stop.is_symlink() or sha256_file(stop) != expected_stop_sha256:
            raise ValueError('Stop record changed before maintenance review')
        record = json.loads(stop.read_text())
        if record.get('tool') != 'covalent_get_job' or record.get('failure', {}).get('category') != 'calculation_failure':
            raise ValueError('This review only applies to the historical-job observation defect')
        payload = {'kind': 'historical-observation-review-v1', 'root': str(root),
            'stop_sha256': expected_stop_sha256, 'evidence_sha256': evidence_sha256,
            'reason': reason, 'reviewed_at': time.time(), 'expires': time.time() + 86400,
            'execution_authorized': False}
        proof = signed(payload)
        identity = hashlib.sha256(proof.encode()).hexdigest()
        # Keep every signed decision; active pointer is not itself authority.
        directory = folder / 'reviews'
        directory.mkdir(exist_ok=True)
        ArtifactRegistry._write_json(directory / (identity + '.json'), {'proof': proof})
        ArtifactRegistry._write_json(folder / 'review.json', {'proof': proof})
        return {'review_id': identity, 'stop_sha256': expected_stop_sha256,
                'execution_authorized': False, 'allowed_tools': sorted(REVIEWED_TOOLS)}


def active_review(root):
    root = Path(root).resolve()
    folder = root / 'state/task_guard'
    path = folder / 'review.json'
    if not path.exists():
        return None
    try:
        if path.is_symlink():
            return None
        proof = json.loads(path.read_text())['proof']
        payload = decode(proof)
        identity = hashlib.sha256(proof.encode()).hexdigest()
        if (payload.get('kind') != 'historical-observation-review-v1'
                or payload.get('root') != str(root)
                or payload.get('stop_sha256') != sha256_file(folder / 'stopped.json')
                or payload.get('execution_authorized') is not False
                or not 0 < payload['expires'] - time.time() <= 86400
                or (folder / 'reviews' / (identity + '.revoked.json')).exists()):
            return None
        return identity
    except (ValueError, KeyError, TypeError, OSError):
        return None


def revoke_review(root, identity, tool, category):
    ArtifactRegistry._write_json(Path(root) / 'state/task_guard/reviews' / (identity + '.revoked.json'),
        {'review_id': identity, 'tool': tool, 'failure_category': category, 'revoked_at': time.time()})
