"""Exact shared-disk refinement staging, never execution or resource approval.

The host validates the complete plan schema and independent native receipt. This
module independently checks the input/report/release file chain before retaining
the plan. A staging receipt is not a runtime, license or scientific readiness flag.
"""
import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

import pandas as pd

import code_release
from worker import canonical, digest, PHASES

LIMIT = 64 * 1024 * 1024


def identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Invalid frozen identifier')
    return value


def read(root, relative):
    if not isinstance(relative, str) or not relative or re.search(r'[\\\x00-\x1f\x7f]', relative) or any(
            part in ('', '.', '..') for part in relative.split('/')):
        raise ValueError('Artifact path must be a contained canonical relative path')
    path = root / relative
    parts = Path(relative).parts
    if any((root / Path(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)):
        raise ValueError('Symbolic links are not allowed in staged evidence')
    path.resolve(strict=True).relative_to(root)
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('Missing, nonregular or oversized evidence file')
    with path.open('rb') as stream:
        contents = stream.read(LIMIT + 1)
    if len(contents) > LIMIT:
        raise ValueError('Evidence grew beyond bounded read limit')
    return contents


def frame(contents):
    return pd.read_parquet(io.BytesIO(contents))


def identities(rows):
    values = []
    for row in rows:
        index, compound = row.get('input_sdf_index'), row.get('compound_id')
        if type(index) is not int or index < 0 or not isinstance(compound, str) or not compound.strip():
            raise ValueError('Invalid candidate identity')
        values.append([index, compound])
    if len({item[0] for item in values}) != len(values) or len({item[1] for item in values}) != len(values):
        raise ValueError('Duplicate candidate identity')
    return digest(sorted(values))


def verify(root, plan, expected):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Execution root cannot be a symbolic link')
    root = root.resolve(strict=True)
    identifier(expected, r'[a-f0-9]{64}')
    if not isinstance(plan, dict) or plan.get('version') != 'screening-refinement-v1' or digest(plan) != expected:
        raise ValueError('Refinement plan digest/version mismatch')
    if 'result_contract' in plan and plan['result_contract'] != 'screening-refinement-outcome-v2':
        raise ValueError('Unsupported independently approved refinement result contract')
    task = identifier(plan['id'], r'refine-[a-f0-9]{24}')
    parent = identifier(plan['parent_id'], r'screen-[a-f0-9]{24}')
    for key in ('parent_digest', 'report_digest', 'selection_id', 'selection_manifest_digest'):
        identifier(plan[key], r'[a-f0-9]{64}')
    engine = plan['engine']['name']
    if engine not in ('af3', 'covdock') or plan['runtime']['name'] != engine:
        raise ValueError('Unregistered or mismatched refinement engine')
    if 'geometry_contract' in plan and (plan['geometry_contract'] != 'af3-geometry-evidence-v1' or engine != 'af3'):
        raise ValueError('Unsupported independently approved geometry contract')
    base = 'tasks/' + parent
    original = json.loads(read(root, base + '/plan.json'))
    if (digest(original) != plan['parent_digest'] or original.get('id') != parent or
            original.get('directory') != plan.get('directory') or original.get('session') != plan.get('session') or
            original.get('runtime', {}).get('root') != str(root)):
        raise ValueError('Refinement parent conversation, root or digest differs')
    previous = None
    manifests = {}
    for phase in PHASES:
        value = json.loads(read(root, base + '/' + phase + '/manifest.json'))
        if (value.get('task_id') != parent or value.get('plan_digest') != plan['parent_digest'] or
                value.get('phase') != phase or value.get('parent_digest') != (digest(previous) if previous else None) or
                value.get('status') != 'SUCCEEDED' or not value.get('gates') or
                any(gate is not True for gate in value['gates'].values())):
            raise ValueError('Core report phase/parent/quality chain failed')
        artifacts = value.get('artifacts')
        if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 200:
            raise ValueError('Core artifact inventory is missing or oversized')
        for name, checksum in artifacts.items():
            identifier(checksum, r'[a-f0-9]{64}')
            if hashlib.sha256(read(root, base + '/' + phase + '/' + name)).hexdigest() != checksum:
                raise ValueError('Core output artifact changed')
        manifests[phase], previous = value, value
    if digest(previous) != plan['report_digest']:
        raise ValueError('Refinement refers to another core report')
    selection = base + '/selections/' + plan['selection_id']
    manifest = json.loads(read(root, selection + '/manifest.json'))
    if digest(manifest) != plan['selection_manifest_digest'] or manifest.get('selection_id') != plan['selection_id']:
        raise ValueError('Selection manifest differs from approved refinement')
    binding = manifest.get('binding', {})
    if (digest(binding) != plan['selection_id'] or binding.get('task_id') != parent or
            binding.get('source_plan_digest') != plan['parent_digest'] or binding.get('source_manifest_digest') != plan['report_digest']):
        raise ValueError('Selection source differs from the approved parent report')
    if set(manifest.get('artifacts', {})) != {'selection.json', 'candidates.parquet'}:
        raise ValueError('Unexpected selection artifact inventory')
    contents = {}
    for name, checksum in manifest['artifacts'].items():
        contents[name] = read(root, selection + '/' + name)
        if hashlib.sha256(contents[name]).hexdigest() != checksum:
            raise ValueError('Selection artifact changed')
    source = read(root, base + '/report/candidates.parquet')
    if hashlib.sha256(source).hexdigest() != binding.get('source_table_sha256') or hashlib.sha256(source).hexdigest() != manifests['report']['artifacts'].get('candidates.parquet'):
        raise ValueError('Core candidate table changed during read')
    details = json.loads(contents['selection.json'])
    approved = plan['input']
    count = len(approved['candidates'])
    if (approved.get('path') != str(root / selection / 'candidates.parquet') or
            approved.get('sha256') != manifest['artifacts']['candidates.parquet'] or
            approved.get('identity_sha256') != manifest.get('selected_identity_digest') or
            approved.get('calibrated_uncertainty') is not False or not 1 <= count <= 100 or
            type(approved.get('eligible_count')) is not int or count > approved['eligible_count'] // 4 or
            details.get('status') != 'SUBSET_PROPOSED' or details.get('selected') != approved['candidates'] or details.get('selected_count') != count or
            details.get('eligible_count') != approved['eligible_count'] or details.get('calibrated_uncertainty') is not False):
        raise ValueError('Candidate scope, reasons or frozen input contract changed')
    selected = frame(contents['candidates.parquet'])
    if len(selected) != count or identities(selected.to_dict('records')) != approved['identity_sha256'] or identities(approved['candidates']) != approved['identity_sha256']:
        raise ValueError('Selected candidate identities/denominator differ')
    core = frame(source)
    if (not 1 <= len(core) <= 20000 or core.input_sdf_index.duplicated().any() or
            details.get('input_count') != len(core) or not 1 <= approved['eligible_count'] <= len(core) or
            details.get('excluded_count') != len(core) - approved['eligible_count']):
        raise ValueError('Invalid core candidate table')
    expected_frame = core.set_index('input_sdf_index', drop=False).loc[
        selected.input_sdf_index.tolist()].reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(selected.reset_index(drop=True), expected_frame, check_exact=True)
    except AssertionError as error:
        raise ValueError('Selected molecular contents differ from the actual core subset') from error
    release = code_release.verify(root / 'releases' / identifier(plan['code']['release_id'], r'[a-f0-9]{64}'),
                                  plan['code']['release_id'], plan['code']['manifest_sha256'])
    science = Path(release['root']) / 'science'
    if ('refinement_stage.py' not in release['files'] or
            plan['assets'].get(str(Path(release['root']) / 'refinement_stage.py')) != release['files']['refinement_stage.py']):
        raise ValueError('Frozen staging controller is missing from release assets')
    for name in ('environment/activate_carsidock.sh', 'configs/covindb/EGFR_C797_mvp_v1.yaml', 'src/covscreen/refinement_input.py',
                 'src/covscreen/refinement_output.py', 'scripts/run_' + ('af3_registered_batch.py' if engine == 'af3' else 'covdock_lo_registered.py')):
        relative = 'science/' + name
        if relative not in release['files'] or plan['assets'].get(str(science / name)) != release['files'][relative]:
            raise ValueError('Frozen refinement executable/input/output helper differs from release')
    return {'version': 'refinement-staging-v1', 'id': task, 'digest': expected, 'code': plan['code'],
            'selection_id': plan['selection_id'], 'selection_manifest_digest': plan['selection_manifest_digest'],
            'input_count': count, 'path': str(root / base / 'refinements' / task / 'plan.json'),
            'output': str(root / base / 'refinements' / task / engine),
            'runtime_verified': False, 'resource_approval_granted': False}


def stage(root, plan, expected):
    result = verify(root, plan, expected)
    destination = Path(result['path'])
    directory = destination.parent
    # Refuse redirection of the plan itself or either newly created directory.
    for item in (directory.parent, directory, destination, directory / 'stage.lock'):
        if item.is_symlink():
            raise ValueError('Staging destination cannot be a symbolic link')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        contents = (canonical({'plan': plan, 'digest': expected}) + '\n').encode()
        if destination.exists():
            if destination.stat().st_size > 1024 * 1024 or destination.read_bytes() != contents:
                raise ValueError('Existing staged plan differs; never overwrite or repair it')
        else:
            descriptor, temporary = tempfile.mkstemp(prefix='.plan-', dir=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            # Link publishes exclusively and atomically; partial files remain for
            # inspection if interrupted and cannot be mistaken for a valid plan.
            os.link(temporary, destination)
            os.unlink(temporary)
    return {**result, 'staged': True, 'plan_sha256': hashlib.sha256(contents).hexdigest()}


def inspect(root, parent, task, expected):
    identifier(parent, r'screen-[a-f0-9]{24}')
    identifier(task, r'refine-[a-f0-9]{24}')
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Execution root cannot be a symbolic link')
    root = root.resolve(strict=True)
    contents = read(root, f'tasks/{parent}/refinements/{task}/plan.json')
    packet = json.loads(contents)
    if set(packet) != {'plan', 'digest'} or packet['digest'] != expected or packet['plan'].get('id') != task or packet['plan'].get('parent_id') != parent:
        raise ValueError('Staged task identity changed')
    result = verify(root, packet['plan'], expected)
    if contents != (canonical(packet) + '\n').encode():
        raise ValueError('Staged plan encoding changed')
    return {**result, 'staged': True, 'plan_sha256': hashlib.sha256(contents).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('freeze', 'read'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--payload')
    parser.add_argument('--parent')
    parser.add_argument('--task')
    args = parser.parse_args()
    if args.action == 'freeze':
        if not args.payload or len(args.payload) > 1024 * 1024:
            raise ValueError('Bounded frozen plan payload required')
        result = stage(args.root, json.loads(base64.b64decode(args.payload, validate=True)), args.digest)
    else:
        result = inspect(args.root, args.parent, args.task, args.digest)
    print(canonical(result), flush=True)
