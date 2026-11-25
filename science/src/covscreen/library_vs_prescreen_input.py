"""Immutable input contract for the Library VS pilot's native CPU prescreen.

The configured full library is never substituted for the study's frozen prefix.
This module prepares inputs and argv only; it cannot authorize or launch compute.
"""
import csv
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .covdock_vs import bound_file
from .jobs import JobSubmissionRequest, sha256_file
from .layout import enable_library_first

BUDGET = {'max_runtime_minutes': 15, 'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16.0, 'max_candidates': 5}
LIMITS = {'max_rows': 20000, 'top_n': 5, 'chunk_size': 5000, 'seed': 20260725,
          'record_selection': 'file_order_prefix'}


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def folder(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'library-vs-prescreen-[a-f0-9]{32}', identifier):
        raise ValueError('Invalid Library VS prescreen input identity')
    location = root / 'state/library-vs-prescreens' / identifier[-32:]
    if any(path.is_symlink() for path in (root / 'state', location.parent, location)):
        raise ValueError('Library VS prescreen inputs cannot traverse symlinks')
    return location


def load(root, reference):
    root = Path(root).resolve()
    if (not isinstance(reference, dict) or set(reference) != {'artifact_id', 'manifest_sha256'}
        or not re.fullmatch(r'[a-f0-9]{64}', reference.get('manifest_sha256', ''))
        or reference['artifact_id'] != 'library-vs-prescreen-' + reference['manifest_sha256'][:32]):
        raise ValueError('Invalid Library VS prescreen input reference')
    location = folder(root, reference['artifact_id'])
    path = bound_file(root, (location / 'prescreen.json').relative_to(root).as_posix())
    if sha256_file(path) != reference['manifest_sha256']:
        raise ValueError('Frozen Library VS prescreen plan changed')
    record = json.loads(path.read_text())
    if (set(record) != {'schema', 'workflow_id', 'screening_inputs', 'scientific_review', 'budget', 'limits', 'roles', 'input_sha256'}
        or record['schema'] != 'library-vs-prescreen-input-v1'
        or not re.fullmatch(r'library-flow-[a-f0-9]{24}', record['workflow_id'])
        or record['budget'] != BUDGET or record['limits'] != LIMITS
        or set(record['roles']) != {'library', 'prefix_manifest', 'science_config', 'receptor', 'reference', 'screening_inputs'}
        or not set(record['roles'].values()) <= set(record['input_sha256'])):
        raise ValueError('Unsupported Library VS prescreen contract')
    for name, expected in record['input_sha256'].items():
        if sha256_file(bound_file(root, name)) != expected:
            raise ValueError('Library VS prescreen source changed: ' + name)
    manifest = json.loads(bound_file(root, record['roles']['prefix_manifest']).read_text())
    source = bound_file(root, record['roles']['library'])
    if (manifest.get('schema') != 'drug-design-frozen-library-prefix-v1'
        or manifest.get('selection', {}).get('method') != 'file_order_prefix'
        or manifest.get('selection', {}).get('records') != 20000
        or manifest.get('subset', {}).get('sha256') != sha256_file(source)):
        raise ValueError('VS prescreen requires the original frozen 20000-record prefix')
    # Recheck row identities, not just a claimed row count or mutable full-library path.
    with source.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        if next(reader) != manifest['header']:
            raise ValueError('VS prescreen library header changed')
        rows = [{'source_record_index': index, 'row_sha256': hashlib.sha256(
            json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
            for index, row in enumerate(reader)]
    if len(rows) != 20000 or rows != manifest['records']:
        raise ValueError('VS prescreen prefix row lineage changed')
    return record, {**record['input_sha256'], str(path.relative_to(root)): reference['manifest_sha256']}


def freeze(root, state, scientific_review):
    enable_library_first()
    from covscreen_library_first import screening_inputs, screening_plan
    root = Path(root).resolve()
    review = screening_plan.proposal(scientific_review, 0)
    inputs = screening_inputs.load_record(root, state)
    if inputs['binding']['profile'] != screening_inputs.PROFILE:
        raise ValueError('VS prescreen requires the supported frozen Library profile')
    roles = {key: Path(inputs['research_inputs'][key]['path']).relative_to(root).as_posix()
             for key in ('library', 'receptor', 'reference', 'science_config')}
    roles['prefix_manifest'] = Path(inputs['research_inputs']['library']['manifest']['path']).relative_to(root).as_posix()
    directory = screening_inputs.folder(root, state['screening_inputs']['id'])
    roles['screening_inputs'] = str((directory / 'screening-inputs.json').relative_to(root))
    inventory = {str((directory / name).relative_to(root)): checksum for name, checksum in inputs['inventory'].items()}
    inventory[roles['screening_inputs']] = state['screening_inputs']['sha256']
    record = {'schema': 'library-vs-prescreen-input-v1', 'workflow_id': state['workflow_id'],
        'screening_inputs': dict(state['screening_inputs']), 'scientific_review': review,
        'budget': BUDGET, 'limits': LIMITS, 'roles': roles, 'input_sha256': inventory}
    raw = encoded(record)
    checksum = hashlib.sha256(raw).hexdigest()
    reference = {'artifact_id': 'library-vs-prescreen-' + checksum[:32], 'manifest_sha256': checksum}
    location = folder(root, reference['artifact_id'])
    location.parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        staging = location.with_name('.' + location.name + '-' + uuid4().hex)
        staging.mkdir()
        (staging / 'prescreen.json').write_bytes(raw)
        staging.rename(location)
    load(root, reference)
    return reference


def parameters(root, reference):
    record, _ = load(root, reference)
    return {'vs_prescreen': dict(reference), 'target_config': record['roles']['science_config'],
        'source_sha256': record['input_sha256'][record['roles']['library']], **LIMITS}


def request(root, reference, request_id):
    record, _ = load(root, reference)
    return JobSubmissionRequest(request_id=request_id, project_id=record['workflow_id'], route='library-first',
        engine='covindb-prescreen', parameters=parameters(root, reference), resource_budget=BUDGET, execution_mode='plan')


def validate_request(root, value):
    record, _ = load(root, value.parameters['vs_prescreen'])
    if (value.route != 'library-first' or value.project_id != record['workflow_id'] or value.attempt != 1
        or value.parent_run_id is not None or value.resource_budget.model_dump(mode='json') != BUDGET
        or value.parameters != parameters(root, value.parameters['vs_prescreen'])):
        raise ValueError('VS prescreen request differs from its frozen study, first attempt or CPU budget')


def command(root, values, python, script, output):
    record, inventory = load(root, values['vs_prescreen'])
    if values != parameters(root, values['vs_prescreen']):
        raise ValueError('VS prescreen command differs from the frozen input')
    argv = [str(python), str(script), '--input', str(bound_file(root, record['roles']['library'])),
        '--target-config', str(bound_file(root, record['roles']['science_config'])), '--output-dir', str(output),
        '--input-sha256', values['source_sha256']]
    for name, value in LIMITS.items():
        argv.extend(['--' + name.replace('_', '-'), str(value)])
    return argv, [str(bound_file(root, name)) for name in inventory], [str(output)]
