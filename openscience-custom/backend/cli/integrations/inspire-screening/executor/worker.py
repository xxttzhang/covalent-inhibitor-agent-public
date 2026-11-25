"""Fixed-command shared-disk stage executor. No model-supplied shell or paths.

The host controls approval and scheduling; this worker independently verifies the
frozen task, source/engine hashes, parents and molecular identity before advancing.
"""
import base64
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import importlib.metadata
import io
import zlib
from pathlib import Path

# Immutable releases reject unrecorded bytecode. Child Python processes inherit
# this setting; source hashes, not timestamp-valid .pyc files, define this runtime.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

SHARED = Path(os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '/inspire/unconfigured'))
ROOT = SHARED / '2_agent/openscience_screening_executor_v1'
# An immutable release belongs to the execution root that contains it. Old
# releases retain their frozen implementation and original task root.
_entry = Path(__file__).resolve()
if _entry.parent.parent.name == 'releases' and re.fullmatch(r'[a-f0-9]{64}', _entry.parent.name):
    ROOT = _entry.parents[2]
DATA_PROJECT = SHARED / '2_agent/science'


def source_layout(entry, root, legacy):
    """An installed release selects its own science sources, never mutable defaults."""
    parent = Path(entry).resolve().parent
    if parent.parent != Path(root).resolve() / 'releases':
        return Path(legacy), None
    if not re.fullmatch(r'[a-f0-9]{64}', parent.name):
        raise ValueError('Invalid core source release directory')
    return parent / 'science', parent


PROJECT, RELEASE = source_layout(__file__, ROOT, DATA_PROJECT)
SOURCE = SHARED / '1_zhang/CovInDB/Virtual_Screening_Library.csv'
CARSI = SHARED / '1_zhang/CarsiDock-Cov'
TARGET = PROJECT / 'configs/covindb/EGFR_C797_mvp_v1.yaml'
PDB = DATA_PROJECT / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb'
REFERENCE = DATA_PROJECT / 'projects/egfr-c797/ligand_first/derived/1M17_AQ4_parent_pose.sdf'
RESEARCH_INPUT_ROOT = Path('/inspire')
PHASES = ('prepare', 'carsidock', 'rtmscore', 'report')


def canonical(value):
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        return item
    return json.dumps(normalize(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write(path, value):
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with temporary.open('x') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def research_inputs(value, assets=None):
    """Independently validate the host-only, versioned input role extension."""
    if not isinstance(value, dict) or set(value) != {'version', 'library', 'receptor', 'science_config', 'reference'} or value['version'] != 'screening-research-inputs-v1':
        raise ValueError('Unsupported research input roles/version')
    def bound_file(item, extra=()):
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', *extra}:
            raise ValueError('Research input file binding has unknown or missing fields')
        filename = item.get('path')
        if (not isinstance(filename, str) or not filename.startswith(str(RESEARCH_INPUT_ROOT) + '/')
                or re.search(r'[\\\x00-\x1f\x7f]', filename)
                or any(part in ('', '.', '..') for part in filename[1:].split('/'))
                or not isinstance(item.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            raise ValueError('Research input requires a canonical absolute path and content hash')
    library = value['library']
    bound_file(library, ('records', 'selection', 'source', 'manifest'))
    if type(library['records']) is not int or library['records'] != 20000 or library['selection'] != 'frozen-file-order-prefix':
        raise ValueError('Research input requires the exact frozen 20000-record prefix')
    bound_file(library['source'])
    bound_file(library['manifest'])
    for role in ('receptor', 'science_config', 'reference'):
        bound_file(value[role])
    files = [library, library['manifest'], *(value[role] for role in ('receptor', 'science_config', 'reference'))]
    if len({item['path'] for item in files}) != len(files):
        raise ValueError('Research input roles cannot replace one another')
    if assets is not None and any(assets.get(item['path']) != item['sha256'] for item in files):
        raise ValueError('Research input roles differ from the frozen asset hashes')
    return value


def input_paths(inputs=None):
    if inputs is None:
        return {'library': SOURCE, 'science_config': TARGET, 'receptor': PDB, 'reference': REFERENCE}
    roles = research_inputs(inputs)
    return {role: Path(roles[role]['path']) for role in ('library', 'science_config', 'receptor', 'reference')}


def regular_bytes(filename, limit=128 * 1024 * 1024):
    """Read the same bounded regular file without following redirected paths."""
    filename = Path(filename)
    if filename.resolve(strict=True) != filename:
        raise ValueError('Research input file was redirected')
    with os.fdopen(os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Research input is not a bounded regular file')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = filename.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current) or len(data) != before.st_size:
        raise ValueError('Research input changed during verification')
    return data


def checked_input(item):
    """Hash the exact bytes before parsing or handing the input to an engine."""
    data = regular_bytes(item['path'])
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Research input checksum differs from the frozen role')
    return data


def verify_research_inputs(inputs, assets=None):
    roles = research_inputs(inputs, assets)
    contents = {role: checked_input(roles[role]) for role in ('library', 'science_config', 'receptor', 'reference')}
    manifest = json.loads(checked_input(roles['library']['manifest']))
    if not isinstance(manifest, dict) or any(not isinstance(manifest.get(key), dict) for key in ('selection', 'source', 'subset')):
        raise ValueError('Malformed frozen research prefix manifest')
    library = roles['library']
    selection, source, subset = (manifest.get(key, {}) for key in ('selection', 'source', 'subset'))
    records = manifest.get('records', [])
    if (manifest.get('schema') != 'drug-design-frozen-library-prefix-v1'
            or manifest.get('operation') != 'data-freeze-only-no-molecule-screening'
            or source.get('path') != library['source']['path'] or source.get('sha256') != library['source']['sha256']
            or subset.get('sha256') != library['sha256'] or subset.get('size_bytes') != len(contents['library'])
            or subset.get('encoding') != 'utf-8' or subset.get('line_ending') != 'LF'
            or selection.get('records') != library['records'] or selection.get('method') != 'file_order_prefix'
            or selection.get('source_record_index_base') != 0 or selection.get('duplicates_preserved') is not True
            or not isinstance(records, list) or len(records) != library['records']):
        raise ValueError('Frozen research prefix manifest differs from its actual input or original source provenance')
    reader = csv.reader(io.StringIO(contents['library'].decode('utf-8'), newline=''), strict=True)
    header = next(reader, None)
    if not header or len(set(header)) != len(header) or header != manifest.get('header'):
        raise ValueError('Frozen research prefix header differs from its manifest')
    seen = set()
    for index, expected in enumerate(records):
        row = next(reader, None)
        if (row is None or len(row) != len(header) or not isinstance(expected, dict)
                or set(expected) != {'source_record_index', 'row_sha256'}
                or type(expected['source_record_index']) is not int or expected['source_record_index'] != index
                or digest(row) != expected['row_sha256']):
            raise ValueError('Frozen research prefix row identity/order differs from its manifest')
        seen.add(expected['row_sha256'])
    if next(reader, None) is not None or selection.get('unique_row_hashes') != len(seen):
        raise ValueError('Frozen research prefix has extra rows or an inconsistent distinct-row count')
    return contents


def paths(inputs=None):
    import yaml
    released = []
    if RELEASE is not None:
        from code_release import verify as verify_release
        manifest = RELEASE / 'manifest.json'
        version = verify_release(RELEASE, RELEASE.name, sha(manifest))
        released = [manifest, *(RELEASE / name for name in version['files'])]
    config = yaml.safe_load((PROJECT / 'configs/local.inspire.yaml').read_text())
    rtms = Path(config['carsidock']['rtmscore_checkpoint'])
    roles = input_paths(inputs)
    return sorted(set([*roles.values(), *([Path(inputs['library']['manifest']['path'])] if inputs is not None else []),
            PROJECT / 'environment/activate_carsidock.sh',
            PROJECT / 'configs/local.inspire.yaml', *released,
            CARSI / 'checkpoints/carsidock_230731.ckpt', rtms, Path(__file__).resolve(),
            Path(__file__).resolve().with_name('refinement.py'),
            Path(__file__).resolve().with_name('engine_interface.py'),
            Path(__file__).resolve().with_name('code_release.py'),
            *sorted((PROJECT / 'scripts').glob('*.py')),
            *sorted((PROJECT / 'src/covscreen').rglob('*.py')),
            *sorted((CARSI / 'src').rglob('*.py')),
            *sorted((CARSI / 'RTMScore').rglob('*.py')),
            CARSI / 'run_carsidockcov_screening.py']))


def snapshot(inputs=None):
    import yaml
    roles = input_paths(inputs)
    contents = verify_research_inputs(inputs) if inputs is not None else None
    target = yaml.safe_load(contents['science_config'] if contents is not None else roles['science_config'].read_text())
    if (target.get('target_id'), target.get('residue_number'), target.get('warhead_class'),
            target.get('reaction_type'), target.get('structure', {}).get('carsidock_covres')) != (
            'EGFR', 797, 'acrylamide', 'Michael Addition', 'A:CYS:773'):
        raise ValueError('Registered target/site/reaction changed; scientific review required')
    if not any(line.startswith('ATOM') and line[21:22] == 'A' and line[22:26].strip() == '773'
               and line[17:20] == 'CYS' and line[12:16].strip() == 'SG' for line in
               (contents['receptor'].decode('utf-8') if contents is not None else roles['receptor'].read_text()).splitlines()):
        raise ValueError('Receptor lacks registered reactive sulfur')
    files = paths(inputs)
    if any(not path.is_file() for path in files):
        raise ValueError('Required source/model/code asset missing')
    assets = {str(path): sha(path) for path in files}
    if inputs is not None:
        research_inputs(inputs, assets)
    return {'assets': assets,
            'runtime': {'project': str(PROJECT), 'root': str(ROOT), 'worker': str(Path(__file__).resolve()),
                        'activation': str(PROJECT / 'environment/activate_carsidock.sh')},
            'source': str(roles['library']),
            'structure_site': ('A:CYS:773 → EGFR C797 (+24)' if inputs is not None else '1M17 A:CYS:773 → EGFR C797 (+24)'),
            **({'research_inputs': inputs, 'original_library_source': inputs['library']['source']} if inputs is not None else {}),
            'reused_pilot': False, 'gpu_execution_verified': False}


def directory(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'screen-[a-f0-9]{24}', task_id):
        raise ValueError('Invalid task ID')
    return ROOT / 'tasks' / task_id


def validate(plan, expected):
    if digest(plan) != expected or plan.get('version') != 'screening-executor-v1':
        raise ValueError('Frozen plan digest/version mismatch')
    directory(plan['id'])
    if plan.get('target') != 'EGFR' or plan.get('site') != 'C797' or plan.get('library') != 'CovInDB/Virtual_Screening_Library.csv':
        raise ValueError('Unregistered source or target; never fall back to a pilot')
    if [step['phase'] for step in plan['steps']] != list(PHASES):
        raise ValueError('Unsupported or reordered pipeline')
    if plan['scope'] not in ('full', 'bounded') or ((plan['scope'] == 'full') != (plan.get('max_records') is None)):
        raise ValueError('Invalid source scope')
    if type(plan['shortlist']) is not int or not 1 <= plan['shortlist'] <= 20000:
        raise ValueError('Invalid shortlist')
    if plan['scope'] == 'bounded' and (type(plan['max_records']) is not int or plan['max_records'] < plan['shortlist']):
        raise ValueError('Invalid source row cap')
    if not 0.75 <= plan['gates']['min_carsidock_success'] <= 1 or not 0.95 <= plan['gates']['min_rtmscore_coverage'] <= 1:
        raise ValueError('Invalid quality gates')
    for step in plan['steps']:
        if type(step['minutes']) is not int or not 1 <= step['minutes'] <= 10080 or step['max_candidates'] != plan['shortlist']:
            raise ValueError('Invalid stage budget')
    automation = plan.get('automation', {})
    if 'research_inputs' in plan:
        if automation.get('selected_mode') != 'research_pilot_v1' or automation.get('requested_mode') != 'research_pilot_v1':
            raise ValueError('Explicit research inputs are restricted to the research pilot mode')
        research_inputs(plan['research_inputs'], plan.get('assets', {}))
    if 'research_pilot_v1' in (automation.get('selected_mode'), automation.get('requested_mode')):
        if (automation.get('version') != 'screening-resource-planner-v1'
                or automation.get('selected_mode') != 'research_pilot_v1'
                or automation.get('requested_mode') != 'research_pilot_v1'
                or automation.get('adjusted') is not False
                or plan['scope'] != 'bounded' or plan['max_records'] != 20000 or plan['shortlist'] != 5
                or [step['minutes'] for step in plan['steps']] != [15, 10, 5, 5]
                or type(plan.get('max_points')) not in (int, float)
                or not math.isfinite(plan['max_points']) or plan['max_points'] < 0):
            raise ValueError('Research pilot v1 scope or stage budget changed')
        for step in plan['steps']:
            quota = step.get('resource', {}).get('quota', '')
            expected_gpu = '1' if step['phase'] in ('carsidock', 'rtmscore') else '0'
            if not re.fullmatch(expected_gpu + r',[1-9][0-9]*,[1-9][0-9]*', quota):
                raise ValueError('Research pilot requires sequential single-GPU stages and CPU preparation/report')
            rate = step['resource'].get('points_per_hour')
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                raise ValueError('Research pilot requires finite nonnegative live quota rates')
        reserved = sum(step['minutes'] / 60 * step['resource']['points_per_hour'] for step in plan['steps'])
        if not math.isfinite(reserved) or reserved > plan['max_points'] + 1e-8:
            raise ValueError('Research pilot reservations exceed its finite points cap')
    if plan['runtime'] != {'project': str(PROJECT), 'root': str(ROOT), 'worker': str(Path(__file__).resolve()),
                           'activation': str(PROJECT / 'environment/activate_carsidock.sh')}:
        raise ValueError('Runtime paths differ from deployed contract')


def freeze(payload):
    packet = json.loads(base64.b64decode(payload, validate=True))
    return freeze_packet(packet)


def decode_staging(payload):
    """Bound command size and decompression before parsing any task data."""
    if not isinstance(payload, str) or len(payload) > 64 * 1024:
        raise ValueError('Compressed staging argument exceeds its bound')
    try:
        compressed = base64.b64decode(payload, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, 4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Compressed staging body is oversized, incomplete or has trailing data')
        packet = json.loads(raw)
    except (zlib.error, UnicodeDecodeError) as error:
        raise ValueError('Invalid compressed staging body') from error
    if not isinstance(packet, dict) or set(packet) != {'plan','digest'}:
        raise ValueError('Invalid staging packet fields')
    return packet


def freeze_packet(packet):
    plan, expected = packet['plan'], packet['digest']
    validate(plan, expected)
    dest = directory(plan['id'])
    dest.mkdir(parents=True, exist_ok=True)
    lock = (dest / 'install.lock').open('a')
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = dest / 'plan.json'
        if path.is_file():
            if digest(json.loads(path.read_text())) != expected:
                raise ValueError('Existing task has different immutable plan')
        else:
            write(path, plan)
    return {'staged': True, 'id': plan['id'], 'digest': expected}


def load(task_id, expected):
    plan = json.loads((directory(task_id) / 'plan.json').read_text())
    validate(plan, expected)
    return plan


def verify_assets(plan):
    # Compare the complete current source set as well as each digest: newly added
    # Python modules cannot silently alter the approved implementation.
    current = snapshot(plan.get('research_inputs'))['assets']
    if current != plan['assets']:
        raise ValueError('Source, receptor, weights or executable changed after approval')


def identities(frame):
    required = {'input_sdf_index', 'compound_id'}
    if not required.issubset(frame.columns) or frame.input_sdf_index.duplicated().any() or frame.compound_id.duplicated().any():
        raise ValueError('Missing or duplicate molecular identity')
    rows = sorted([[int(row.input_sdf_index), str(row.compound_id)] for row in frame.itertuples()])
    return digest(rows)


def verify_manifest(task_id, phase, expected):
    dest = directory(task_id)
    manifest = json.loads((dest / phase / 'manifest.json').read_text())
    if manifest['task_id'] != task_id or manifest['plan_digest'] != expected or manifest['phase'] != phase:
        raise ValueError('Artifact belongs to another task/phase')
    for name, value in manifest['artifacts'].items():
        path = (dest / phase / name).resolve()
        path.relative_to((dest / phase).resolve())
        if sha(path) != value:
            raise ValueError('Output artifact checksum changed')
    index = PHASES.index(phase)
    parent = verify_manifest(task_id, PHASES[index - 1], expected) if index else None
    if manifest['parent_digest'] != (digest(parent) if parent else None):
        raise ValueError('Parent chain mismatch')
    if manifest['status'] != 'SUCCEEDED' or not manifest['gates'] or not all(manifest['gates'].values()):
        raise ValueError('Parent scientific gate did not pass')
    return manifest


def read_failure(task_id, phase, expected, witness=None):
    """Witness bounded diagnostic files, never accept partial molecular output.

    This read does not create a manifest, repair outputs or acquire compute. The
    host must have observed a terminal failure and retains the first digest.
    """
    if phase not in PHASES:
        raise ValueError('Unsupported failed phase')
    plan = load(task_id, expected)
    if plan.get('assets', {}).get(str(Path(__file__).resolve())) != sha(Path(__file__).resolve()):
        raise ValueError('Failure reader differs from the approved worker')
    dest = directory(task_id) / phase
    if dest.resolve(strict=True) != dest or not dest.is_dir():
        raise ValueError('Failure directory cannot be redirected')

    def read(name, parse=False):
        path = dest / name
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > (65536 if parse else 128 * 1024 * 1024):
            raise ValueError('Failure artifact must be a bounded regular file')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise ValueError('Failure artifact changed before reading')
            checksum, size, chunks = hashlib.sha256(), 0, []
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(block)
                if size > before.st_size:
                    raise ValueError('Failure artifact grew during reading')
                checksum.update(block)
                if parse:
                    chunks.append(block)
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size:
            raise ValueError('Failure artifact changed during reading')
        return {'sha256': checksum.hexdigest(), 'bytes': size}, json.loads(b''.join(chunks)) if parse else None

    lockpath = dest / 'process.lock'
    if not stat.S_ISREG(lockpath.lstat().st_mode):
        raise ValueError('Failure process lock must be a regular file')
    with os.fdopen(os.open(lockpath, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (dest / 'manifest.json').exists() or (dest / 'manifest.json').is_symlink():
            raise ValueError('A success manifest exists; cannot classify it as worker failure')
        artifacts = {}
        artifacts['error.json'], error = read('error.json', True)
        artifacts['started.json'], started = read('started.json', True)
        required = {'task_id', 'phase', 'plan_digest', 'error', 'runtime_seconds', 'status'}
        zero_failure = set(error) == required | {'failure_kind', 'exit_code'}
        if (set(error) != required and not zero_failure or
                error['task_id'] != task_id or error['phase'] != phase or error['plan_digest'] != expected or
                error['status'] != 'FAILED' or not isinstance(error['error'], str) or not error['error'].strip()):
            raise ValueError('Failure evidence belongs to another task/phase or lacks a reason')
        if zero_failure and (phase != 'prepare' or error['failure_kind'] != 'zero_candidates' or type(error['exit_code']) is not int or error['exit_code'] != 2):
            raise ValueError('Unsupported structured failure termination')
        if (set(started) != {'pid', 'at', 'plan_digest'} or started['plan_digest'] != expected or
                type(started['pid']) is not int or started['pid'] <= 0):
            raise ValueError('Failure attempt differs from the approved plan')
        for value in (error['runtime_seconds'], started['at']):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid failure timing evidence')
        for name in ('environment.json', 'engine.log', 'failures.csv'):
            if (dest / name).exists() or (dest / name).is_symlink():
                artifacts[name], _ = read(name)
        zero_present = (dest / 'zero-candidates.json').exists() or (dest / 'zero-candidates.json').is_symlink()
        if zero_failure != zero_present:
            raise ValueError('Zero-candidate diagnostic requires the matching structured failure termination')
        if zero_present:
            artifacts['zero-candidates.json'], zero = read('zero-candidates.json', True)
            if zero != zero_candidates(plan, dest):
                raise ValueError('Zero-candidate diagnostic differs from recomputed preparation evidence')
            artifacts['stage2_selection_manifest.json'], _ = read('stage2_selection_manifest.json')
        for name, recorded in artifacts.items():
            fresh, _ = read(name)
            if fresh != recorded:
                raise ValueError('Failure artifacts changed during collection')
        result = {'version': 'screening-failure-evidence-v1', 'task_id': task_id, 'phase': phase,
                  'plan_digest': expected, 'status': 'FAILED', 'started_at': started['at'],
                  'runtime_seconds': error['runtime_seconds'], 'artifacts': artifacts,
                  'scientific_quality_verified': False, 'contents_included': False}
        if witness is not None and (not re.fullmatch(r'[a-f0-9]{64}', witness) or digest(result) != witness):
            raise ValueError('Failure evidence changed from the retained witness')
        if dest.resolve(strict=True) != dest or load(task_id, expected) != plan or plan['assets'][str(Path(__file__).resolve())] != sha(Path(__file__).resolve()):
            raise ValueError('Failure directory, plan or reader changed during collection')
        return result


def command(plan, phase, dest):
    import yaml
    inputs = plan.get('research_inputs')
    if inputs is not None:
        verify_research_inputs(inputs, plan.get('assets', {}))
    roles = input_paths(inputs)
    config = yaml.safe_load((PROJECT / 'configs/local.inspire.yaml').read_text())
    common = [sys.executable]
    base = dest.parent
    if phase == 'prepare':
        route_entry = PROJECT.parent / 'routes/library-first/scripts/run_covindb_prescreen.py'
        args = common + [str(route_entry), '--input', str(roles['library']),
            '--target-config', str(roles['science_config']), '--output-dir', str(dest), '--top-n', str(plan['shortlist']),
            '--chunk-size', '5000', '--seed', str(plan['seed'])]
        return args + (['--max-rows', str(plan['max_records'])] if plan['max_records'] is not None else [])
    if phase == 'carsidock':
        return common + [str(PROJECT / 'scripts/run_carsidock_registered.py'), '--chunk', '0', '--device', 'cuda',
            '--pdb', str(roles['receptor']), '--reference-ligand', str(roles['reference']), '--ligand-sdf', str(base / 'prepare/stage2_candidates.sdf'),
            '--ligand-table', str(base / 'prepare/stage2_candidates.parquet'), '--covres', 'A:CYS:773',
            '--reaction-type', 'Michael Addition', '--output-dir', str(dest), '--output-scores', str(dest / 'scores.parquet'),
            '--carsidock-repo', str(CARSI), '--checkpoint', str(CARSI / 'checkpoints/carsidock_230731.ckpt'),
            '--num-conformer', '1', '--cuda-device-index', '0', '--seed', str(plan['seed'])]
    if phase == 'rtmscore':
        return common + [str(PROJECT / 'scripts/run_rtmscore_existing_poses.py'), '--project-root', str(PROJECT),
            '--carsidock-repo', str(CARSI), '--checkpoint', str(config['carsidock']['rtmscore_checkpoint']),
            '--pdb', str(roles['receptor']), '--reference-ligand', str(roles['reference']), '--carsidock-scores', str(base / 'carsidock/scores.parquet'),
            '--output', str(dest / 'scores.csv'), '--cuda-device-index', '0']
    raise ValueError('Report is generated internally; arbitrary commands are forbidden')


class ChildExit(ValueError):
    def __init__(self, code):
        super().__init__('Scientific engine exited with code ' + str(code))
        self.code = code


def run_child(args, logfile, timeout):
    with logfile.open('x') as stream:
        child = subprocess.Popen(args, cwd=PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait(timeout=timeout)
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
    if code:
        raise ChildExit(code)


def environment(phase):
    versions = {}
    for package in ('torch', 'rdkit', 'pandas', 'numpy', 'pyarrow', 'dgl'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    # Conda-built interpreters can expose vendor-decorated sys.version strings
    # that the container's platform.py cannot parse. version_info is the
    # interpreter's structured source of truth, not a guessed/rewritten string.
    result = {'python': '.'.join(str(value) for value in sys.version_info[:3]),
              'python_build': sys.version, 'executable': sys.executable,
              'packages': versions, 'thread_limit': os.environ.get('OMP_NUM_THREADS'),
              'slurm_cpus_per_task': os.environ.get('SLURM_CPUS_PER_TASK'), 'gpu': None}
    if phase in ('carsidock', 'rtmscore'):
        import torch
        if not torch.cuda.is_available():
            raise ValueError('GPU phase has no CUDA; no CPU fallback')
        properties = torch.cuda.get_device_properties(0)
        result['gpu'] = {'model': properties.name, 'memory_bytes': properties.total_memory,
                         'cuda_runtime': torch.version.cuda, 'visible_count': torch.cuda.device_count()}
    return result


def verify_prepared_source(plan, source):
    inputs = plan.get('research_inputs')
    roles = input_paths(inputs)
    if source['source']['sha256'] != plan['assets'][str(roles['library'])] or source['source']['max_rows'] != plan['max_records']:
        raise ValueError('Prepared candidates were not generated from the approved source/scope')
    if inputs is not None:
        # The prescreen reads the frozen prefix itself. Its source receipt must
        # never be rewritten to pretend that it read the full original library.
        if (source['source'].get('path') != str(roles['library'])
                or source['source'].get('records_read') != inputs['library']['records']
                or source['source'].get('record_selection') != 'file_order_prefix'
                or source.get('target', {}).get('config_path') != str(roles['science_config'])
                or source.get('target', {}).get('config_sha256') != inputs['science_config']['sha256']):
            raise ValueError('Prepared source or target config differs from the explicit research input roles')


def zero_candidates(plan, dest):
    """Prove a completed zero-selection result without upgrading stage success.

    Other failed attempts have no such product. The original error is retained;
    this result says only what the completed prescreen selected under its fixed
    input and quality policy, never that the source lacks active compounds.
    """
    import pyarrow.parquet as pq
    verify_assets(plan)
    if dest != directory(plan['id']) / 'prepare':
        raise ValueError('Zero-candidate diagnostic path differs from its task')
    names = ('stage2_selection_manifest.json', 'stage2_candidates.parquet', 'covindb_master.parquet', 'covindb_rejected.parquet')
    data = {name: regular_bytes(dest / name, 1024 * 1024 if name.endswith('.json') else 128 * 1024 * 1024) for name in names}
    source = json.loads(data['stage2_selection_manifest.json'])
    verify_prepared_source(plan, source)
    roles = input_paths(plan.get('research_inputs'))
    if (source.get('schema_version') != '1.0' or source.get('diagnostics_version') != 'prescreen-rejections-v1'
            or source.get('status') != 'completed' or source.get('selection_outcome') != 'NO_CANDIDATES_SELECTED'
            or source['source'].get('path') != str(roles['library'])
            or source.get('target', {}).get('config_path') != str(roles['science_config'])
            or source['target'].get('config_sha256') != plan['assets'][str(roles['science_config'])]
            or source.get('selection', {}).get('top_n_requested') != plan['shortlist']
            or source['selection'].get('top_n_selected') != 0):
        raise ValueError('Completed zero-selection metadata differs from the approved plan')
    counts, quality, gates = (source.get(name, {}) for name in ('counts', 'quality', 'quality_gates'))
    required = ('input_rows', 'parsed', 'unique', 'duplicates', 'invalid', 'hard_gate_pass', 'selected')
    if (any(type(counts.get(name)) is not int or counts[name] < 0 for name in required)
            or counts['input_rows'] <= 0 or counts['parsed'] > counts['input_rows']
            or counts['input_rows'] != counts['unique'] + counts['duplicates'] + counts['invalid']
            or counts['input_rows'] != source['source'].get('records_read')
            or (plan['max_records'] is not None and counts['input_rows'] > plan['max_records'])
            or counts['selected'] != 0 or counts['hard_gate_pass'] != 0
            or counts['parsed'] / counts['input_rows'] < 0.99
            or quality.get('identity_loss_count') != 0 or quality.get('failure_table_present') is not True
            or quality.get('rdkit_parse_rate') != counts['parsed'] / counts['input_rows']
            or quality.get('selected_warhead_mapping_rate') != 0
            or gates != {'rdkit_parse_rate_ge_0_99': True, 'selected_warhead_mapping_rate_ge_0_98': False,
                         'identity_loss_zero': True, 'failure_table_present': True}):
        raise ValueError('Zero selection cannot conceal incomplete input or failed input-quality gates')
    selected = pq.ParquetFile(io.BytesIO(data['stage2_candidates.parquet']))
    master = pq.ParquetFile(io.BytesIO(data['covindb_master.parquet']))
    rejected = pq.ParquetFile(io.BytesIO(data['covindb_rejected.parquet']))
    eligible = master.read(columns=['eligible']).column('eligible')
    if (selected.metadata.num_rows != 0 or master.metadata.num_rows != counts['unique']
            or rejected.metadata.num_rows < counts['invalid'] or eligible.null_count
            or str(eligible.type) != 'bool' or any(eligible.to_pylist())):
        raise ValueError('Zero-selection tables disagree with the completed source manifest')
    files = {name: {'sha256': hashlib.sha256(contents).hexdigest(), 'bytes': len(contents)} for name, contents in data.items()}
    for name, info in files.items():
        current = regular_bytes(dest / name)
        if len(current) != info['bytes'] or hashlib.sha256(current).hexdigest() != info['sha256']:
            raise ValueError('Zero-selection evidence changed during verification')
    return {'version': 'screening-zero-candidates-v1', 'task_id': plan['id'], 'phase': 'prepare',
            'plan_digest': digest(plan), 'outcome': 'NO_CANDIDATES_SELECTED', 'candidate_count': 0,
            'termination': {'kind': 'prescreen_quality_gate_exit', 'exit_code': 2},
            'input_rows': counts['input_rows'], 'actual_source': source['source'],
            'target_config': source['target'], 'files': files,
            'input_quality_verified': True, 'scientific_quality_verified': False,
            'activity_claim': 'No activity or full-library coverage conclusion follows from this bounded selection.'}


def acceptance(plan, phase, dest, parent):
    import pandas as pd
    from rdkit import Chem
    base = dest.parent
    prepared = pd.read_parquet(base / 'prepare/stage2_candidates.parquet')
    identity = identities(prepared)
    if not 0 < len(prepared) <= plan['shortlist']:
        raise ValueError('Empty or oversized candidate set')
    source = json.loads((base / 'prepare/stage2_selection_manifest.json').read_text())
    gates = {'identity_preserved': True, 'budget_respected': True}
    verify_prepared_source(plan, source)
    if phase == 'prepare':
        gates.update(source['quality_gates'])
        molecules = list(Chem.SDMolSupplier(str(dest / 'stage2_candidates.sdf'), removeHs=False, sanitize=False))
        observed = sorted([[int(mol.GetProp('INPUT_SDF_INDEX')), mol.GetProp('COMPOUND_ID')] for mol in molecules if mol is not None])
        if len(molecules) != len(prepared) or digest(observed) != identity:
            raise ValueError('SDF identity/count differs from prepared table')
        return prepared, len(prepared), gates, identity
    carsi = pd.read_parquet(base / 'carsidock/scores.parquet')
    if identities(carsi) != identity or len(carsi) != len(prepared):
        raise ValueError('CarsiDock lost/replaced candidate identities')
    good = carsi.engine_status.eq('SUCCESS')
    if any(not math.isfinite(float(value)) for value in carsi.loc[good, 'raw_score']):
        raise ValueError('Successful CarsiDock rows contain invalid scores')
    gates['carsidock_success_rate'] = float(good.mean()) >= plan['gates']['min_carsidock_success']
    if phase == 'carsidock':
        # Every promoted pose must refer to a parseable record belonging to this task.
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location('rtms_pose_loader', PROJECT / 'scripts/run_rtmscore_existing_poses.py')
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in carsi.loc[good, 'pose_file']:
            path = Path(value)
            path = path.resolve() if path.is_absolute() else (PROJECT / path).resolve()
            path.relative_to(dest.resolve())
        poses = module._load_selected_poses(carsi.loc[good], PROJECT)
        gates['traceable_poses'] = len(poses) == int(good.sum())
        carsi.loc[~good].to_csv(dest / 'failures.csv', index=False)
        return carsi, int(good.sum()), gates, identity
    rtms = pd.read_parquet(base / 'rtmscore/scores.parquet')
    if identities(rtms) != identity or len(rtms) != len(prepared):
        raise ValueError('RTMScore lost/replaced candidate identities')
    scored = rtms.engine_status.eq('SUCCESS')
    if not set(rtms.loc[scored, 'compound_id']).issubset(set(carsi.loc[good, 'compound_id'])):
        raise ValueError('RTMScore generated scores without an upstream successful pose')
    if any(not math.isfinite(float(value)) for value in rtms.loc[scored, 'raw_score']):
        raise ValueError('Successful RTMScore rows contain invalid scores')
    gates['rtmscore_coverage'] = int(scored.sum()) / max(1, int(good.sum())) >= plan['gates']['min_rtmscore_coverage']
    if phase == 'rtmscore':
        rtms.loc[~scored].to_csv(dest / 'failures.csv', index=False)
    if phase == 'report':
        summary = {'task_id': plan['id'], 'request': plan['request'], 'target': plan['target'], 'site': plan['site'],
            'source': source['source'], 'source_counts': source['counts'], 'source_quality': source['quality'],
            **({'research_inputs': plan['research_inputs'], 'original_library_source': plan['research_inputs']['library']['source']}
               if plan.get('research_inputs') is not None else {}),
            'selected_count': len(prepared), 'carsidock_success': int(good.sum()), 'rtmscore_success': int(scored.sum()),
            'identity_digest': identity, 'gates': gates, 'plan_digest': digest(plan),
            'stage_evidence': [{**verify_manifest(plan['id'], name, digest(plan)),
                               'resource_reservation': plan['steps'][index]}
                              for index, name in enumerate(PHASES[:-1])],
            'maximum_reserved_points': plan['max_points'],
            'billing_note': 'Reservations and measured worker runtimes are not an authoritative platform bill.',
            'result_claim': 'Computational screening only; not experimentally validated hits.',
            'next_stage': 'Uncertainty-based refinement requires explicit selection evidence and its own resource approval.'}
        write(dest / 'report.json', summary)
        cols = ['input_sdf_index', 'compound_id', 'engine_status', 'raw_score']
        joined = prepared.merge(carsi[cols].rename(columns={'engine_status': 'carsidock_status', 'raw_score': 'carsiscore'}),
                               on=['input_sdf_index', 'compound_id'], validate='one_to_one')
        joined = joined.merge(rtms[cols].rename(columns={'engine_status': 'rtmscore_status', 'raw_score': 'rtmscore'}),
                              on=['input_sdf_index', 'compound_id'], validate='one_to_one')
        joined.to_csv(dest / 'candidates.csv', index=False)
        joined.to_parquet(dest / 'candidates.parquet', index=False)
        text = '# Screening acceptance report\n\n' + '\n'.join(f'- {key}: {value}' for key, value in summary.items()) + '\n'
        (dest / 'report.md').write_text(text)
    return rtms, int(scored.sum()), gates, identity


def execute(task_id, phase, expected):
    plan = load(task_id, expected)
    if phase not in PHASES:
        raise ValueError('Unsupported phase')
    dest = directory(task_id) / phase
    dest.mkdir(exist_ok=True)
    with (dest / 'process.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (dest / 'manifest.json').is_file():
            verify_manifest(task_id, phase, expected)
            return {'already_verified': True}
        if (dest / 'started.json').exists():
            raise ValueError('Previous attempt exists; refuse implicit rerun')
        started = time.monotonic()
        write(dest / 'started.json', {'pid': os.getpid(), 'at': time.time(), 'plan_digest': expected})
        try:
            if phase == 'carsidock':
                sys.path.insert(0, str(PROJECT / 'src'))
                from covscreen.docking_policy import require_engine_enabled
                require_engine_enabled('carsidock-cov', route='library-first')
            verify_assets(plan)
            if time.time() * 1000 > plan['finish_before']:
                raise ValueError('Approved task deadline exceeded')
            index = PHASES.index(phase)
            parent = verify_manifest(task_id, PHASES[index - 1], expected) if index else None
            runtime = environment(phase)
            write(dest / 'environment.json', runtime)
            if plan.get('research_inputs') is not None:
                write(dest / 'research-inputs.json', {'research_inputs': plan['research_inputs'], 'plan_digest': expected,
                      'actual_library_input': plan['research_inputs']['library']['path'],
                      'original_library_source': plan['research_inputs']['library']['source']})
            args = command(plan, phase, dest) if phase != 'report' else None
            budget = plan['steps'][index]['minutes'] * 60 - (time.monotonic() - started)
            if budget <= 0:
                raise ValueError('Verification exceeded stage time cap')
            if args is not None:
                run_child(args, dest / 'engine.log', budget)
            if plan.get('research_inputs') is not None:
                verify_research_inputs(plan['research_inputs'], plan['assets'])
            frame, successful, gates, identity = acceptance(plan, phase, dest, parent)
            if not all(gates.values()):
                raise ValueError('Scientific quality gates failed: ' + canonical(gates))
            files = [path for path in sorted(dest.rglob('*')) if path.is_file()
                     and path.name not in {'process.lock', 'started.json', 'manifest.json', 'error.json'}
                     and not path.name.startswith('.')]
            manifest = {'task_id': task_id, 'phase': phase, 'plan_digest': expected, 'status': 'SUCCEEDED',
                'parent_digest': digest(parent) if parent else None, 'input_count': len(frame),
                'success_count': successful, 'failed_count': len(frame) - successful,
                'artifacts': {str(path.relative_to(dest)): sha(path) for path in files}, 'identity_digest': identity,
                'gates': gates, 'runtime_seconds': round(time.monotonic() - started, 3),
                'environment': runtime, 'resource_reservation': plan['steps'][index]}
            if manifest['runtime_seconds'] > plan['steps'][index]['minutes'] * 60:
                raise ValueError('Execution and verification exceeded the approved stage time cap')
            write(dest / 'manifest.json', manifest)
            return manifest
        except BaseException as error:
            reason = {}
            if phase == 'prepare' and isinstance(error, ChildExit) and error.code == 2:
                try:
                    zero = zero_candidates(plan, dest)
                except Exception:
                    # Missing or invalid diagnostic evidence must never hide the
                    # original failure or be interpreted as a zero-candidate fact.
                    pass
                else:
                    write(dest / 'zero-candidates.json', zero)
                    reason = {'failure_kind': 'zero_candidates', 'exit_code': 2}
            write(dest / 'error.json', {'task_id': task_id, 'phase': phase, 'plan_digest': expected,
                'error': str(error), 'runtime_seconds': round(time.monotonic() - started, 3), 'status': 'FAILED', **reason})
            raise


def selection_source(task_id, expected):
    from io import BytesIO
    import pandas as pd
    plan = load(task_id, expected)
    verify_assets(plan)
    parent = verify_manifest(task_id, 'report', expected)
    base = directory(task_id)
    table = base / 'report/candidates.parquet'
    contents = table.read_bytes()
    checksum = hashlib.sha256(contents).hexdigest()
    if checksum != parent['artifacts'].get('candidates.parquet'):
        raise ValueError('Core candidate table changed during selection read')
    frame = pd.read_parquet(BytesIO(contents))
    directions = {}
    for phase, name in (('carsidock', 'carsiscore'), ('rtmscore', 'rtmscore')):
        scored = pd.read_parquet(base / phase / 'scores.parquet')
        if identities(scored) != identities(frame):
            raise ValueError('Selection source identity mismatch')
        if 'score_direction' not in scored or scored.score_direction.isna().any():
            raise ValueError('Missing score direction in verified engine results')
        values = list(scored.score_direction.unique())
        if len(values) != 1:
            raise ValueError('Mixed engine score directions')
        directions[name] = str(values[0])
    binding = {'task_id': task_id, 'source_plan_digest': expected,
               'source_manifest_digest': digest(parent), 'source_table_sha256': checksum,
               'algorithm_sha256': sha(Path(__file__).with_name('refinement.py')),
               'directions': directions}
    return frame, binding


def inspect_selection(dest, binding, frame):
    """Read-only file and scientific-selection check, not a resource approval."""
    from io import BytesIO
    import pandas as pd
    from refinement import select
    # Compare actual bytes with the manifest, then parse those same bytes. Only
    # these two exact contained files are allowed, never manifest-supplied paths.
    files = {}
    for name in ('manifest.json', 'selection.json', 'candidates.parquet'):
        file = (dest / name).resolve(strict=True)
        file.relative_to(dest.resolve(strict=True))
        files[name] = file.read_bytes()
    manifest = json.loads(files['manifest.json'])
    if set(manifest) != {'selection_id', 'binding', 'selected_identity_digest', 'artifacts'}:
        raise ValueError('Unexpected selection manifest fields')
    if manifest['selection_id'] != digest(binding) or manifest['binding'] != binding:
        raise ValueError('Selection binding differs from current verified core source')
    expected = {name: hashlib.sha256(files[name]).hexdigest() for name in ('selection.json', 'candidates.parquet')}
    if manifest['artifacts'] != expected:
        raise ValueError('Selection artifact checksum/inventory changed')
    result = json.loads(files['selection.json'])
    regenerated = select(frame.to_dict(orient='records'), binding['policy'], binding['directions'])
    if canonical(result) != canonical(regenerated):
        raise ValueError('Selection list, reasons or policy evidence differs from recomputed core results')
    selected = pd.read_parquet(BytesIO(files['candidates.parquet']))
    indices = [row['input_sdf_index'] for row in regenerated['selected']]
    expected_frame = frame.set_index('input_sdf_index', drop=False).loc[indices].reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(selected, expected_frame, check_exact=True)
    except AssertionError as error:
        raise ValueError('Selected molecular table differs from exact core subset') from error
    identity = identities(selected)
    if identity != manifest['selected_identity_digest']:
        raise ValueError('Selection molecular identity digest changed')
    # Bounded candidate IDs/rank reasons are required for informed approval.
    # Do not return the full audit, SMILES, source tables or raw molecular scores.
    return {'selection_verified': True, 'selection_id': manifest['selection_id'], 'source_task_id': binding['task_id'],
            'source_plan_digest': binding['source_plan_digest'], 'source_manifest_digest': binding['source_manifest_digest'],
            'status': regenerated['status'], 'policy': binding['policy'],
            'input_count': regenerated['input_count'], 'eligible_count': regenerated['eligible_count'],
            'excluded_count': regenerated['excluded_count'], 'selected_count': regenerated['selected_count'],
            'effective_cap': regenerated['effective_cap'], 'artifact_directory': str(dest),
            'selection_manifest_digest': digest(manifest),
            'input': {'path': str(dest / 'candidates.parquet'), 'sha256': expected['candidates.parquet'],
                      'identity_sha256': identity, 'eligible_count': regenerated['eligible_count'],
                      'candidates': regenerated['selected'], 'calibrated_uncertainty': False},
            'calibrated_uncertainty': False, 'gpu_submitted': False, 'refinement_approval_required': True,
            'limitations': regenerated['limitations']}


def read_selection(task_id, expected, selection_id, expected_manifest):
    # This path never creates/repairs files, regenerates a selection artifact,
    # starts engines or opens an approval. Missing evidence is an error.
    from refinement import validate as validate_policy
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
           for value in (selection_id, expected_manifest)):
        raise ValueError('Explicit selection and manifest digests required')
    frame, source = selection_source(task_id, expected)
    base = directory(task_id) / 'selections'
    dest = base / selection_id
    dest.resolve(strict=True).relative_to(base.resolve(strict=True))
    for name in ('manifest.json', 'selection.lock'):
        (dest / name).resolve(strict=True).relative_to(dest.resolve(strict=True))
    with (dest / 'selection.lock').open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        manifest = json.loads((dest / 'manifest.json').read_text())
        if digest(manifest) != expected_manifest:
            raise ValueError('Frozen selection manifest changed')
        binding = {**source, 'policy': validate_policy(manifest['binding']['policy'])}
        if digest(binding) != selection_id:
            raise ValueError('Selection belongs to a different source/policy')
        result = inspect_selection(dest, binding, frame)
        if result['selection_manifest_digest'] != expected_manifest:
            raise ValueError('Selection manifest changed during read')
        return result


def select_refinement(task_id, expected, payload):
    # Selection never starts AF3/CovDock and never reads a historical candidate pool.
    from refinement import select, validate as validate_policy
    frame, source = selection_source(task_id, expected)
    policy = validate_policy(json.loads(base64.b64decode(payload, validate=True)))
    binding = {**source, 'policy': policy}
    result = select(frame.to_dict(orient='records'), policy, binding['directions'])
    selection_id = digest(binding)
    base = directory(task_id) / 'selections'
    dest = base / selection_id
    dest.mkdir(parents=True, exist_ok=True)
    dest.resolve(strict=True).relative_to(base.resolve(strict=True))
    with (dest / 'selection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = dest / 'manifest.json'
        if manifest.exists():
            return inspect_selection(dest, binding, frame)
        else:
            if (dest / 'selection.json').exists() or (dest / 'candidates.parquet').exists():
                raise ValueError('Unfinished selection output exists; inspect instead of overwriting')
            indices = [row['input_sdf_index'] for row in result['selected']]
            selected = frame.set_index('input_sdf_index', drop=False).loc[indices].reset_index(drop=True)
            if len(selected) != result['selected_count'] or (len(selected) and identities(selected) != digest(sorted(
                    [[row['input_sdf_index'], row['compound_id']] for row in result['selected']]))):
                raise ValueError('Refinement subset identity loss')
            selected.to_parquet(dest / 'candidates.parquet', index=False)
            write(dest / 'selection.json', result)
            write(manifest, {'selection_id': selection_id, 'binding': binding,
                             'selected_identity_digest': identities(selected),
                             'artifacts': {name: sha(dest / name) for name in ('selection.json', 'candidates.parquet')}})
        return inspect_selection(dest, binding, frame)


if __name__ == '__main__':
    if not os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '').startswith('/inspire/'):
        raise ValueError('Configure COVSCREEN_LEGACY_SHARED_ROOT before running the legacy worker')
    action = sys.argv[1]
    if action == 'snapshot':
        # Optional host-produced role object; no shell or executable parameters.
        result = snapshot(json.loads(base64.b64decode(sys.argv[2], validate=True)) if len(sys.argv) > 2 else None)
    elif action == 'freeze':
        result = freeze(sys.argv[2])
    elif action == 'freeze-compressed':
        result = freeze_packet(decode_staging(sys.argv[2]))
    elif action == 'run':
        result = execute(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == 'verify':
        load(sys.argv[2], sys.argv[4])
        result = verify_manifest(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == 'read-failure':
        result = read_failure(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
    elif action == 'select-refinement':
        result = select_refinement(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == 'read-selection':
        result = read_selection(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif action == 'refinement-interface':
        from engine_interface import inspect
        from code_release import verify as verify_release
        release = verify_release(ROOT / 'releases' / sys.argv[3], sys.argv[3], sys.argv[4])
        result = inspect(Path(release['root']) / 'science', sys.argv[2])
        result['code'] = {key: release[key] for key in ('release_id', 'manifest_sha256')}
    else:
        raise ValueError('Unsupported worker action')
    print(canonical(result), flush=True)
