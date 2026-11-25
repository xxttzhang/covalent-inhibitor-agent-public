"""Preserve a historical Ligand adduct's source chain without granting MD admission.

This producer copies existing bytes only. The original pose, loop modelling,
cleanup and their receipts stay distinct. Replaying hashes establishes document
provenance, not a fresh native graph audit, canonical site mapping or parameters.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4


FILES = {
    'source_manifest': 'source-manifest.json',
    'source_inspection': 'source-inspection.json',
    'candidate_table': 'candidate-table.csv',
    'covdock_spec': 'covdock-spec.json',
    'covdock_result': 'covdock-result.json',
    'covdock_manifest': 'covdock-manifest.json',
    'pose_file': 'original-pose.maegz',
    'extracted_pose': 'extracted-pose.mae',
    'construct_fasta': 'construct.fasta',
    'loop_started': 'loop-started.json',
    'loop_finished': 'loop-finished.json',
    'filled_pose': 'filled-pose.maegz',
    'loop_qc': 'original-loop-qc.json',
    'loop_input': 'original-loop-input.mae',
    'cleanup': 'cleanup.json',
    'clean_qc': 'clean-loop-qc.json',
    'adduct_input': 'adduct-input.mae',
}
SCHEMA = 'historical-ligand-md-source-v1'
LIMITATIONS = [
    'Historical documents and file hashes are replayed; native structures have not been re-audited in this workflow.',
    'Canonical target identity and construct-to-site mapping require independent verification.',
    'Historical loop QC and cleanup are not current graph admission, new parameters or MD readiness.',
    'Historical authorization is retained as source text only and cannot authorize any new calculation.',
]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError('Historical MD source: ' + message)


def regular(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)) and path.is_file(),
            'source must be a regular file without symbolic links: ' + str(path))
    require(path.stat().st_size <= 200 * 1024 * 1024, 'source exceeds 200 MiB')
    return path


def read_json(path):
    value = json.loads(Path(path).read_text())
    require(isinstance(value, dict), 'expected JSON object')
    return value


def derive(paths, records, system_id, compound_id, input_sdf_index):
    """Replay links between original receipts; never translate them into passes."""
    require(isinstance(system_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', system_id),
            'invalid historical system identity')
    require(isinstance(compound_id, str) and compound_id.strip() and type(input_sdf_index) is int
            and input_sdf_index >= 0, 'invalid candidate identity')
    data = {role: read_json(paths[role]) for role, name in FILES.items() if name.endswith('.json')}
    digest = lambda role: records[role]['sha256']
    manifest, inspected = data['source_manifest'], data['source_inspection']
    require(manifest.get('schema') == 'ligand-three-md-source-v1', 'unsupported historical source profile')
    require(inspected.get('source_manifest_sha256') == digest('source_manifest'), 'inspection parent changed')
    select = lambda value: [r for r in value.get('records', []) if r.get('system_id') == system_id]
    original, inspection = select(manifest), select(inspected)
    require(len(original) == len(inspection) == 1 and original[0] == inspection[0], 'source inspection identity differs')
    row = original[0]
    require(row.get('compound_id') == compound_id and type(row.get('pose')) is int and row['pose'] >= 0,
            'source candidate or pose identity differs')
    for role, key in [('pose_file', 'sha256'), ('extracted_pose', 'source_mae_sha256'),
                      ('construct_fasta', 'sequence_sha256')]:
        require(row.get(key) == digest(role), role + ' differs from source inspection')
    require(row.get('path') == records['pose_file']['original_path'], 'original pose path differs')
    require(manifest.get('source_table_sha256') == digest('candidate_table') and
            manifest.get('source_table') == records['candidate_table']['original_path'], 'candidate table parent changed')
    with paths['candidate_table'].open(newline='') as stream:
        candidates = list(csv.DictReader(stream))
    require(0 < len(candidates) <= 10000, 'candidate table size unsupported')
    matches = [r for r in candidates if r.get('compound_id') == compound_id
               and r.get('input_sdf_index') == str(input_sdf_index)]
    require(len(matches) == 1, 'candidate identity is absent or duplicated')
    candidate = matches[0]
    require(candidate.get('covdock_pose_file') == row['path'] and
            candidate.get('covdock_pose_index') == str(row['pose']) and
            candidate.get('coordinate_recheck_source_sha256') == digest('pose_file'), 'candidate pose binding differs')
    spec, result, native = data['covdock_spec'], data['covdock_result'], data['covdock_manifest']
    run_id = spec.get('run_id')
    require(isinstance(run_id, str) and run_id and spec.get('route') == 'ligand-first'
            and spec.get('engine') == 'schrodinger-covdock-lo', 'wrong historical producer')
    require(all(result.get(k) == spec.get(k) for k in ('run_id', 'route', 'engine', 'project_id', 'attempt')),
            'historical result identity differs from task')
    require(result.get('state') == 'COMPLETED' and result.get('return_code') == 0, 'historical producer not complete')
    for role in ('pose_file', 'covdock_manifest'):
        suffix = '/state/jobs/' + run_id + '/'
        source_path = records[role]['original_path']
        require(suffix in source_path, 'source not inside original registered job')
        relative = 'state/jobs/' + run_id + '/' + source_path.split(suffix, 1)[1]
        entries = [a for a in result.get('artifacts', []) if a.get('path') == relative]
        require(len(entries) == 1 and entries[0].get('run_id') == run_id
                and entries[0].get('sha256') == digest(role)
                and entries[0].get('size_bytes') == records[role]['size'], 'registered artifact changed: ' + role)
    require(native.get('status') == 'completed' and row['path'] in native.get('pose_files', []),
            'pose absent from producer manifest')
    gates = native.get('gate_quality_gates')
    require(isinstance(gates, dict) and gates and all(v is True for v in gates.values()),
            'historical producer gates incomplete')
    if native.get('execution_kind') == 'retained_candidate_reparse':
        require(native.get('pose_sha256') == digest('pose_file')
                and native.get('new_docking_or_ligprep_executed') is False
                and candidate.get('covdock_source_parent_run_id') == native.get('parent_run_id')
                and candidate.get('covdock_source_kind') == 'interrupted_run_candidate_readback',
                'retained-readback origin differs')
    started, finished = data['loop_started'], data['loop_finished']
    for value in (started, finished):
        require(value.get('system_id') == system_id and value.get('source_sha256') == digest('extracted_pose')
                and value.get('sequence_sha256') == digest('construct_fasta')
                and value.get('missing_internal_residues') == row.get('missing_internal_residues'),
                'loop modelling does not bind original candidate/construct')
    require(all(finished.get(k) == v for k, v in started.items()), 'loop start/finish receipts differ')
    require(finished.get('returncode') == 0 and finished.get('status') == 'LOOP_MODEL_WRITTEN_REQUIRES_QC'
            and finished.get('output_sha256') == digest('filled_pose'), 'loop output differs')
    qc, clean, cleanup = data['loop_qc'], data['clean_qc'], data['cleanup']
    for value in (qc, clean):
        require(value.get('system_id') == system_id and value.get('source_sha256') == digest('extracted_pose')
                and value.get('filled_sha256') == digest('filled_pose'), 'loop QC sources differ')
    require(qc.get('loop_qc_input_sha256') == digest('loop_input'), 'loop input differs from QC')
    require(cleanup.get('system_id') == system_id and cleanup.get('source_QC_sha256') == digest('loop_qc')
            and cleanup.get('source_input_sha256') == digest('loop_input')
            and cleanup.get('output_sha256') == digest('adduct_input'), 'cleanup chain differs')
    require(clean.get('upstream_loop_qc_sha256') == digest('loop_qc')
            and clean.get('cleanup_sha256') == digest('cleanup')
            and clean.get('loop_qc_input_sha256') == digest('adduct_input'), 'cleaned adduct parent differs')
    return {'system_id': system_id, 'compound_id': compound_id, 'input_sdf_index': input_sdf_index,
        'historical_run_id': run_id, 'historical_source_kind': candidate.get('covdock_source_kind'),
        'original_pose_index': row['pose'], 'original_pose_sha256': digest('pose_file'),
        'adduct_input_sha256': digest('adduct_input'),
        'recorded_construct': {k: row.get(k) for k in
            ('protein_chain', 'target_residue', 'construct_start', 'construct_end', 'missing_internal_residues')},
        'recorded_candidate': {k: candidate.get(k) for k in
            ('standardized_smiles', 'warhead_class', 'nucleophilic_atom', 'af3_status', 'engine_status', 'physical_validity')},
        'recorded_qc': {k: clean.get(k) for k in ('status', 'warnings', 'prep_validated', 'protonation_protocol')}}


def load_directory(directory, manifest_sha256):
    directory = Path(directory).absolute()
    manifest_path = regular(directory / 'historical-md-source.json')
    require(re.fullmatch(r'[a-f0-9]{64}', manifest_sha256 or '') and sha(manifest_path) == manifest_sha256,
            'manifest hash differs')
    value = read_json(manifest_path)
    if value.get('schema') == 'historical-library-md-source-v1':
        from .md_historical_library_source import load_directory as load_library
        return load_library(directory, manifest_sha256)
    require(set(value) == {'schema', 'status', 'identity', 'sources', 'limitations',
            'native_graph_qc_completed', 'parameterization_completed', 'md_system_ready', 'calculation_submitted'},
            'unsupported manifest fields')
    require(value.get('schema') == SCHEMA and value.get('status') == 'source_chain_frozen_native_review_pending'
            and value.get('limitations') == LIMITATIONS, 'unsupported source state')
    require(all(value.get(k) is False for k in ('native_graph_qc_completed', 'parameterization_completed',
            'md_system_ready', 'calculation_submitted')), 'historical source cannot grant scientific completion')
    records = value['sources']
    require(isinstance(records, dict) and set(records) == set(FILES), 'source inventory differs')
    paths = {}
    for role, name in FILES.items():
        record = records[role]
        require(set(record) == {'filename', 'sha256', 'size', 'original_path'} and record['filename'] == name
                and isinstance(record['original_path'], str) and Path(record['original_path']).is_absolute(),
                'invalid source inventory entry')
        paths[role] = regular(directory / name)
        require(type(record['size']) is int and paths[role].stat().st_size == record['size']
                and sha(paths[role]) == record['sha256'], 'frozen source changed: ' + role)
    identity = value['identity']
    observed = derive(paths, records, identity['system_id'], identity['compound_id'], identity['input_sdf_index'])
    require(observed == identity, 'derived source identity differs')
    return {'directory': directory, 'manifest': value,
            'evidence_files': [manifest_path, *paths.values()]}


def load_source(root, artifact_id, manifest_sha256):
    require(isinstance(artifact_id, str) and re.fullmatch(r'historical-md-source-[a-f0-9]{32}', artifact_id),
            'invalid artifact identity')
    require(isinstance(manifest_sha256, str) and artifact_id == 'historical-md-source-' + manifest_sha256[:32],
            'artifact identity differs from manifest hash')
    return load_directory(Path(root) / 'state/historical_md_sources' / manifest_sha256[:32], manifest_sha256)


def freeze(root, sources, *, system_id, compound_id, input_sdf_index):
    require(isinstance(sources, dict) and set(sources) == set(FILES), 'provide the entire source chain')
    paths = {role: regular(path) for role, path in sources.items()}
    records = {role: {'filename': FILES[role], 'sha256': sha(path), 'size': path.stat().st_size,
                      'original_path': str(path)} for role, path in paths.items()}
    identity = derive(paths, records, system_id, compound_id, input_sdf_index)
    value = {'schema': SCHEMA, 'status': 'source_chain_frozen_native_review_pending',
        'identity': identity, 'sources': records, 'limitations': LIMITATIONS,
        'native_graph_qc_completed': False, 'parameterization_completed': False,
        'md_system_ready': False, 'calculation_submitted': False}
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
    digest = hashlib.sha256(payload).hexdigest()
    parent = Path(root).absolute() / 'state/historical_md_sources'
    require(not any(p.is_symlink() for p in (parent, *parent.parents)), 'registry has symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / digest[:32]
    staging = parent / ('.' + digest[:32] + '-' + uuid4().hex)
    if not destination.exists():
        staging.mkdir()
        try:
            for role, source in paths.items():
                shutil.copyfile(source, staging / FILES[role])
            (staging / 'historical-md-source.json').write_bytes(payload)
            load_directory(staging, digest)
            try:
                staging.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                load_directory(destination, digest)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    loaded = load_directory(destination, digest)
    return {'artifact_id': 'historical-md-source-' + digest[:32], 'manifest_sha256': digest,
            'identity': loaded['manifest']['identity'], 'calculation_submitted': False}
