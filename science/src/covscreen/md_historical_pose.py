"""Bind historical adduct bytes and verified target sequence for new native review."""
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from .md_historical_source import load_directory as load_history, regular, require, sha
from .target_identity import load_target_identity
from .md_adduct_input import SUPPORTED_REACTIONS

SCHEMA = 'historical-candidate-pose-input-v1'
SOURCE_SCHEMA = 'historical-adduct-receptor-source-v1'
CONTEXT_SCHEMA = 'historical-adduct-site-context-v1'
GRAPH_POLICY = 'loop-junction-backbone-v2'


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def sequence(path):
    lines = Path(path).read_text().splitlines()
    require(lines and lines[0].startswith('>') and sum(line.startswith('>') for line in lines) == 1,
            'expected exactly one FASTA record')
    return ''.join(line.strip() for line in lines[1:])


def derive(history, target, history_hash, target_hash, site, graph_policy=GRAPH_POLICY):
    require(graph_policy in (None, GRAPH_POLICY), 'unsupported historical graph policy')
    identity = history['manifest']['identity']
    route = 'library-first' if history['manifest']['schema'] == 'historical-library-md-source-v1' else 'ligand-first'
    definition = identity['recorded_construct']
    hits = target['identity']['candidates']
    require(len(hits) == 1 and target['identity']['unique_verified_target'] == hits[0]['uniprot_id'],
            'unique canonical identity required')
    start, end = definition['construct_start'], definition['construct_end']
    require(type(start) is int and type(end) is int and type(site) is int and start <= site <= end,
            'canonical site outside recorded construct')
    canonical = sequence(target['_directory'] / hits[0]['sequence_path'])
    construct = sequence(history['directory'] / 'construct.fasta')
    require(len(construct) == end - start + 1 and canonical[start - 1:end] == construct,
            'historical construct differs from verified canonical sequence')
    require(canonical[site - 1] == 'C' and definition['target_residue'] == site,
            'this import profile requires exact canonical numbering and a cysteine')
    chemical = identity['recorded_candidate']
    require(chemical.get('af3_status') == 'SUCCESS' and chemical.get('engine_status') == 'SUCCESS'
            and chemical.get('physical_validity') in {'PASS', 'WARN'}
            and chemical.get('warhead_class') in SUPPORTED_REACTIONS
            and isinstance(chemical.get('standardized_smiles'), str) and chemical['standardized_smiles'],
            'historical candidate lacks source structural/chemical evidence')
    qc = identity['recorded_qc']
    require(qc.get('status') == 'LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION' and qc.get('warnings') == [],
            'historical QC warnings require review before native execution')
    context_identity = {'entry_id': 'historical:' + identity['historical_run_id'],
        'uniprot': hits[0]['uniprot_id'], 'canonical_cys': site,
        'author_chain': definition['protein_chain'], 'author_residue': site}
    require(isinstance(context_identity['author_chain'], str) and context_identity['author_chain'], 'missing protein chain')
    receptor = {'schema': SOURCE_SCHEMA, 'status': 'sequence_verified_native_structure_pending',
        **context_identity, 'receptor_prepared': False, 'campaign_ready': False,
        'source_manifest_sha256': history_hash, 'target_manifest_sha256': target_hash,
        'construct_start': start, 'construct_end': end,
        'construct_sequence_sha256': sha(history['directory'] / 'construct.fasta'),
        'adduct_input_sha256': identity['adduct_input_sha256']}
    receptor_hash = hashlib.sha256(encoded(receptor)).hexdigest()
    context = {'schema': CONTEXT_SCHEMA, 'status': 'sequence_scope_bound_native_structure_pending',
        'identity': context_identity, 'parents': {'prepared_receptor_sha256': receptor_hash},
        'campaign_ready': False, 'native_coordinate_mapping_verified': False}
    context_hash = hashlib.sha256(encoded(context)).hexdigest()
    manifest = {'schema': SCHEMA, 'status': 'pose_selected_native_graph_qc_pending',
        'candidate_id': '%s-%06d-%s' % (route, identity['input_sdf_index'], identity['adduct_input_sha256'][:12]),
        'compound_id': identity['compound_id'], 'input_sdf_index': identity['input_sdf_index'],
        'engine': 'CovDock-LO', 'pose_index': identity['original_pose_index'],
        'target': {**context_identity, 'atom_name': 'SG'},
        'chemistry': {'standardized_smiles': chemical['standardized_smiles'],
            'warhead_class': chemical['warhead_class'], 'reaction_type': SUPPORTED_REACTIONS[chemical['warhead_class']],
            'covalent_mode': 'Cys-S-C', 'product_state': 'covalent_adduct'},
        'origin': {'route': route, 'task_id': identity['historical_run_id'],
            'source_profile': SCHEMA, 'source_manifest_sha256': history_hash},
        'parents': {'historical_source_sha256': history_hash, 'target_identity_sha256': target_hash,
            'prepared_receptor_sha256': receptor_hash, 'receptor_context_sha256': context_hash},
        'artifacts': {'prepared-receptor.json': receptor_hash, 'receptor-context.json': context_hash},
        'native_graph_qc_completed': False, 'parameterization_completed': False,
        'md_system_ready': False, 'campaign_ready': False, 'calculation_submitted': False}
    if graph_policy is not None:
        manifest['historical_graph_policy'] = graph_policy
    return manifest, receptor, context


def receptor_identity(receptor, context, receptor_hash):
    require(receptor.get('schema') == SOURCE_SCHEMA
            and receptor.get('status') == 'sequence_verified_native_structure_pending'
            and receptor.get('receptor_prepared') is False and receptor.get('campaign_ready') is False,
            'unsupported historical receptor source')
    identity = {k: receptor.get(k) for k in ('entry_id', 'uniprot', 'canonical_cys', 'author_chain', 'author_residue')}
    require(context == {'schema': CONTEXT_SCHEMA, 'status': 'sequence_scope_bound_native_structure_pending',
        'identity': identity, 'parents': {'prepared_receptor_sha256': receptor_hash},
        'campaign_ready': False, 'native_coordinate_mapping_verified': False}, 'historical receptor context differs')
    return identity


def load(directory, manifest_sha256):
    directory = Path(directory)
    path = regular(directory / 'candidate-pose-input.json')
    require(sha(path) == manifest_sha256, 'historical pose manifest differs')
    manifest = json.loads(path.read_text())
    parents = manifest['parents']
    history = load_history(directory / 'history', parents['historical_source_sha256'])
    target_dir = directory / 'target'
    target = load_target_identity(target_dir, parents['target_identity_sha256'])
    target['_directory'] = target_dir
    expected, receptor, context = derive(history, target, parents['historical_source_sha256'],
        parents['target_identity_sha256'], manifest['target']['canonical_cys'],
        manifest.get('historical_graph_policy'))
    require(manifest == expected, 'historical pose identity differs from replayed source')
    for name, value in [('prepared-receptor.json', receptor), ('receptor-context.json', context)]:
        require(regular(directory / name).read_bytes() == encoded(value), 'historical source descriptor differs')
    # Target loader verifies the saved public response and exact FASTA. Include
    # all copied target bytes in the new job's frozen input list.
    target_files = [regular(p) for p in sorted(target_dir.iterdir())]
    return {'directory': directory, 'manifest': manifest, 'history': history, 'target_identity': target,
        'evidence_files': [path, directory / 'prepared-receptor.json', directory / 'receptor-context.json',
                           *history['evidence_files'], *target_files]}


def freeze(root, historical, target_reference, site):
    from .md_historical_source import load_source
    from .tool_api import _load_target_artifact
    history = load_source(root, historical['artifact_id'], historical['manifest_sha256'])
    target = _load_target_artifact(root, **target_reference)
    target_dir = Path(root) / 'state/target_identity' / target_reference['target_artifact_id'][7:]
    target['_directory'] = target_dir
    manifest, receptor, context = derive(history, target, historical['manifest_sha256'],
        target_reference['target_manifest_sha256'], site)
    digest = hashlib.sha256(encoded(manifest)).hexdigest()
    parent = Path(root) / 'state/candidate_pose_input'
    require(not any(p.is_symlink() for p in (parent, *parent.parents)), 'pose registry has symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / digest[:32]
    staging = parent / ('.' + digest[:32] + '-' + uuid4().hex)
    if not destination.exists():
        staging.mkdir()
        try:
            for name, value in [('candidate-pose-input.json', manifest), ('prepared-receptor.json', receptor),
                                ('receptor-context.json', context)]:
                (staging / name).write_bytes(encoded(value))
            (staging / 'history').mkdir()
            for path in history['evidence_files']:
                shutil.copyfile(path, staging / 'history' / path.name)
            (staging / 'target').mkdir()
            for path in target_dir.iterdir():
                shutil.copyfile(regular(path), staging / 'target' / path.name)
            load(staging, digest)
            try:
                staging.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                load(destination, digest)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    verified = load(destination, digest)
    return {'artifact_id': 'candidate-pose-input-' + digest[:32], 'manifest_sha256': digest,
        'candidate_id': verified['manifest']['candidate_id'], 'compound_id': manifest['compound_id'],
        'input_sdf_index': manifest['input_sdf_index'], 'source_manifest_sha256': historical['manifest_sha256'],
        'target_reference': target_reference, 'native_graph_qc_completed': False, 'calculation_submitted': False}
