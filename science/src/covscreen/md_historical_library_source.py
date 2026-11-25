"""Import retained native Library evidence without adopting historical execution.

The operator freezes existing files after native report/publication inspection.
This verifies provenance only; native graph admission must run in the new study.
"""
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .md_historical_source import FILES as LIGAND_FILES, LIMITATIONS, read_json, regular, require, sha

SCHEMA = 'historical-library-md-source-v1'
FILES = {**{k: v for k, v in LIGAND_FILES.items()
         if k not in {'covdock_spec', 'covdock_result', 'covdock_manifest'}},
    'native_report': 'native-refinement-report.json',
    'native_readback': 'native-readback.json',
    'native_delivery': 'native-delivery.json',
}
FALSE_FIELDS = ('native_graph_qc_completed', 'parameterization_completed', 'md_system_ready', 'calculation_submitted')


def one(rows, predicate, description):
    found = [row for row in rows if predicate(row)]
    require(len(found) == 1, description + ' is absent or duplicated')
    return found[0]


def derive(paths, records, system_id, compound_id, input_sdf_index):
    require(isinstance(system_id, str) and re.fullmatch(r'library_[0-9]{3,}', system_id), 'invalid Library system identity')
    require(isinstance(compound_id, str) and compound_id and type(input_sdf_index) is int
            and input_sdf_index >= 0, 'invalid Library candidate identity')
    data = {role: read_json(paths[role]) for role, name in FILES.items() if name.endswith('.json')}
    checksum = lambda role: records[role]['sha256']
    source, inspection = data['source_manifest'], data['source_inspection']
    require(source.get('schema') == 'library-eight-md-source-v1', 'unsupported Library source profile')
    require(inspection.get('source_manifest_sha256') == checksum('source_manifest'), 'source inspection parent changed')
    select = lambda value: one(value.get('records', []), lambda row: row.get('system_id') == system_id, 'source candidate')
    original, row = select(source), select(inspection)
    require(all(row.get(k) == v for k, v in original.items()), 'inspection changed the original candidate')
    require(row.get('compound_id') == compound_id and row.get('input_sdf_index') == input_sdf_index
            and type(row.get('pose')) is int and row['pose'] >= 0, 'candidate/pose identity differs')
    for role, key in [('pose_file', 'sha256'), ('extracted_pose', 'source_mae_sha256'), ('construct_fasta', 'sequence_sha256')]:
        require(row.get(key) == checksum(role), role + ' differs from source inspection')
    require(row.get('path') == records['pose_file']['original_path'], 'original pose path differs')

    report, observed, delivery = data['native_report'], data['native_readback'], data['native_delivery']
    result = observed.get('result', {})
    require(observed.get('compute_submitted') is False and observed.get('execution_records_unchanged') is True
            and result.get('verification') == 'verified' and result.get('report_ready') is True
            and result.get('compute_restarted') is False and result.get('report') == report,
            'native report lacks a matching verified readback')
    require(report.get('version') == 'screening-refinement-report-v1'
            and result.get('task_id') == report.get('parent_task_id')
            and result.get('workflow_id') == report.get('workflow_id'), 'native report identity differs')
    require(delivery.get('schema') == 'refinement-publication-observation-v1'
            and delivery.get('state') == 'COMPLETE' and delivery.get('verification') == 'verified'
            and delivery.get('workflow_id') == report.get('workflow_id')
            and delivery.get('workflow_digest') == report.get('workflow_digest')
            and delivery.get('task_id') == report.get('parent_task_id')
            and delivery.get('compute_submitted') is False and delivery.get('platform_files_published') is False,
            'native publication identity is not verified')
    matches = lambda r: r.get('compound_id') == compound_id and r.get('input_sdf_index') == input_sdf_index
    candidate = one(report.get('candidates', []), matches, 'native candidate')
    require(candidate.get('status') == 'BOTH_STRUCTURAL_QC_PASSED'
            and all(candidate.get(engine, {}).get('status') == 'SUCCESS'
                    and candidate[engine].get('quality', {}).get('status') == 'PASSED'
                    for engine in ('af3', 'covdock')), 'native candidate structural review did not pass')
    require(candidate['covdock'].get('structure_index') == row['pose'], 'native pose index differs')
    stage = one(report.get('stages', []), lambda s: s.get('engine') == 'covdock', 'CovDock stage')
    require(re.fullmatch(r'refine-[a-f0-9]{24}', stage.get('task_id', ''))
            and '/refinements/' + stage['task_id'] + '/covdock/' + candidate['covdock']['structure'] ==
            row['path'][row['path'].index('/refinements/'):], 'pose does not belong to the original refinement')
    with paths['candidate_table'].open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    require(0 < len(rows) <= 10000, 'candidate table size unsupported')
    chemical = one(rows, lambda r: r.get('compound_id') == compound_id
                   and r.get('input_sdf_index') == str(input_sdf_index), 'native chemical identity')
    require(chemical.get('standardized_smiles') == row.get('reactant_smiles_for_provenance_only')
            and chemical.get('warhead_class') == 'acrylamide' and chemical.get('warhead_count') == '1',
            'Library import requires the original single acrylamide identity')
    for role, name in [('pose_file', 'covdock/' + candidate['covdock']['structure']),
                       ('candidate_table', 'covdock/core_subset_identity.csv'),
                       ('native_report', 'report/refinement-report.json')]:
        published = one(delivery.get('files', []), lambda f: f.get('name') == name, 'published ' + role)
        require(published.get('sha256') == checksum(role) and published.get('bytes') == records[role]['size']
                and all(published.get(k) for k in ('artifact_id', 'version_id', 'project_id')),
                'published bytes differ: ' + role)
        if role != 'native_report':
            native = one(stage.get('artifacts', []), lambda a: a.get('name') == name[len('covdock/'):],
                         'native ' + role)
            require(native.get('sha256') == checksum(role)
                    and native.get('remote_path') == records[role]['original_path'], 'native source differs: ' + role)

    started, finished = data['loop_started'], data['loop_finished']
    for value in (started, finished):
        require(value.get('system_id') == system_id and value.get('source_sha256') == checksum('extracted_pose')
                and value.get('sequence_sha256') == checksum('construct_fasta')
                and value.get('missing_internal_residues') == row.get('missing_internal_residues'),
                'loop source identity differs')
    require(all(finished.get(k) == v for k, v in started.items())
            and finished.get('returncode') == 0 and finished.get('status') == 'LOOP_MODEL_WRITTEN_REQUIRES_QC'
            and finished.get('output_sha256') == checksum('filled_pose'), 'loop output receipt differs')
    qc, clean, cleanup = data['loop_qc'], data['clean_qc'], data['cleanup']
    for value in (qc, clean):
        require(value.get('system_id') == system_id and value.get('source_sha256') == checksum('extracted_pose')
                and value.get('filled_sha256') == checksum('filled_pose'), 'loop QC source differs')
    require(qc.get('loop_qc_input_sha256') == checksum('loop_input'), 'original loop input differs')
    require(cleanup.get('system_id') == system_id and cleanup.get('source_QC_sha256') == checksum('loop_qc')
            and cleanup.get('source_input_sha256') == checksum('loop_input')
            and cleanup.get('output_sha256') == checksum('adduct_input'), 'cleanup chain differs')
    require(clean.get('upstream_loop_qc_sha256') == checksum('loop_qc')
            and clean.get('cleanup_sha256') == checksum('cleanup')
            and clean.get('loop_qc_input_sha256') == checksum('adduct_input'), 'clean adduct parent differs')
    return {'system_id': system_id, 'compound_id': compound_id, 'input_sdf_index': input_sdf_index,
        'historical_run_id': stage['task_id'], 'historical_source_kind': 'native_library_refinement',
        'native_task_id': report['parent_task_id'], 'native_workflow_id': report['workflow_id'],
        'native_workflow_digest': report['workflow_digest'], 'native_report_sha256': checksum('native_report'),
        'original_pose_index': row['pose'], 'original_pose_sha256': checksum('pose_file'),
        'adduct_input_sha256': checksum('adduct_input'),
        'recorded_construct': {k: row.get(k) for k in
            ('protein_chain', 'target_residue', 'construct_start', 'construct_end', 'missing_internal_residues')},
        'recorded_candidate': {'standardized_smiles': chemical['standardized_smiles'], 'warhead_class': chemical['warhead_class'],
            'nucleophilic_atom': 'SG', 'af3_status': candidate['af3']['status'], 'engine_status': candidate['covdock']['status'],
            'physical_validity': 'PASS'},
        'recorded_qc': {k: clean.get(k) for k in ('status', 'warnings', 'prep_validated', 'protonation_protocol')}}


def load_directory(directory, manifest_sha256):
    directory = Path(directory).absolute()
    path = regular(directory/'historical-md-source.json')
    require(re.fullmatch(r'[a-f0-9]{64}', manifest_sha256 or '') and sha(path) == manifest_sha256, 'manifest hash differs')
    value = read_json(path)
    require(set(value) == {'schema', 'status', 'identity', 'sources', 'limitations', *FALSE_FIELDS}
            and value.get('schema') == SCHEMA and value.get('status') == 'source_chain_frozen_native_review_pending'
            and value.get('limitations') == LIMITATIONS and all(value.get(k) is False for k in FALSE_FIELDS),
            'historical Library intake cannot grant execution or scientific completion')
    records = value['sources']
    require(isinstance(records, dict) and set(records) == set(FILES), 'source inventory differs')
    paths = {}
    for role, name in FILES.items():
        record = records[role]
        require(set(record) == {'filename', 'sha256', 'size', 'original_path'} and record['filename'] == name
                and isinstance(record['original_path'], str) and Path(record['original_path']).is_absolute(),
                'invalid source inventory entry')
        paths[role] = regular(directory/name)
        require(type(record['size']) is int and paths[role].stat().st_size == record['size']
                and sha(paths[role]) == record['sha256'], 'frozen source changed: ' + role)
    identity = value['identity']
    require(derive(paths, records, identity['system_id'], identity['compound_id'], identity['input_sdf_index']) == identity,
            'derived Library source identity differs')
    return {'directory': directory, 'manifest': value, 'evidence_files': [path, *paths.values()]}


def freeze(root, sources, *, system_id, compound_id, input_sdf_index, original_paths=None):
    """Operator intake, not a model-supplied path API; copied native bytes retain origins."""
    require(isinstance(sources, dict) and set(sources) == set(FILES), 'provide the entire Library source chain')
    paths = {role: regular(path) for role, path in sources.items()}
    origins = original_paths or {role: str(path) for role, path in paths.items()}
    require(set(origins) == set(FILES) and all(isinstance(v, str) and Path(v).is_absolute() for v in origins.values()),
            'complete original source paths required')
    records = {role: {'filename': FILES[role], 'sha256': sha(path), 'size': path.stat().st_size,
                      'original_path': origins[role]} for role, path in paths.items()}
    identity = derive(paths, records, system_id, compound_id, input_sdf_index)
    value = {'schema': SCHEMA, 'status': 'source_chain_frozen_native_review_pending', 'identity': identity,
             'sources': records, 'limitations': LIMITATIONS, **dict.fromkeys(FALSE_FIELDS, False)}
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n').encode()
    digest = hashlib.sha256(payload).hexdigest()
    parent = Path(root).absolute()/'state/historical_md_sources'
    require(not any(p.is_symlink() for p in (parent, *parent.parents)), 'registry has symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent/digest[:32]
    staging = parent/('.'+digest[:32]+'-'+uuid4().hex)
    if not destination.exists():
        staging.mkdir()
        try:
            for role, source in paths.items():
                shutil.copyfile(source, staging/FILES[role])
            (staging/'historical-md-source.json').write_bytes(payload)
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
    return {'artifact_id': 'historical-md-source-'+digest[:32], 'manifest_sha256': digest,
            'identity': loaded['manifest']['identity'], 'calculation_submitted': False}
