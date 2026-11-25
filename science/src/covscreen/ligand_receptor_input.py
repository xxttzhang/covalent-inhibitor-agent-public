"""Adapt a sealed Ligand study to the shared receptor-preparation input.

This preserves the existing accession evidence; it does not fabricate a new
UniProt search, a Library workflow, receptor preparation or scientific approval.
"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .jobs import sha256_file
from .ligand_design import design_folder
from .ligand_refinement_target import load as load_target, encoded
from .receptor_evidence import regular
from .target_acquisition_artifact import load_target_acquisition
from .target_site_audit import audit_target_site
from .ligand_receptor_source import VS_SOURCE, context, pre_vs, source_input


SOURCE = 'sealed-ligand-study-v1'


def build(root, state, *, source_kind=None):
    is_vs = source_kind == VS_SOURCE or (source_kind is None and pre_vs(state))
    if is_vs:
        from .ligand_vs_input import load as load_vs
        ref = source_input(state)
        target = load_vs(root, {**state, 'vs_input': ref}, use_prepared=False)
        target_folder = root / ref['directory']
        parent_source = {'source_kind': VS_SOURCE,
            'vs_input_artifact_id': ref['artifact_id'], 'vs_input_manifest_sha256': ref['manifest_sha256'],
            'target_artifact_id': ref['artifact_id'], 'target_manifest_sha256': ref['manifest_sha256']}
        extra_lineage = [root / name for name in target['lineage_sha256']]
        extra_lineage += [target_folder / 'input.json', *[target_folder / name for name in target['files']]]
    else:
        target = load_target(root, state)
        handoff = state['refinement_input']
        target_folder = root / handoff['target']['directory']
        parent_source = {'source_kind': SOURCE,
            'candidate_artifact_id': handoff['artifact_id'],
            'candidate_manifest_sha256': handoff['manifest_sha256'],
            'target_artifact_id': handoff['target']['artifact_id'],
            'target_manifest_sha256': handoff['target']['manifest_sha256'],
            'design_manifest_sha256': target['design_manifest_sha256'],
            'evidence_manifest_sha256': target['evidence_manifest_sha256']}
        extra_lineage = [(root / handoff['candidate_table']).parent / 'manifest.json',
            root / handoff['candidate_table'], target_folder / 'manifest.json', target_folder / 'target-input.json',
            *[target_folder / name for name in target['files']]]
    design = design_folder(root, state['design_id'])
    original_path = design / 'case/evidence/structure_acquisition.json'
    original = json.loads(original_path.read_text())
    identity_path = design / 'uniprot.json'
    identity = json.loads(identity_path.read_text())
    inventory = json.loads((target_folder / 'receptor-inventory.json').read_text())
    sequence = identity['sequence']['value']
    files = {'study-uniprot.json': identity_path.read_bytes(),
        'study-acquisition.json': original_path.read_bytes(),
        'canonical.fasta': (f">{identity['primaryAccession']} sealed-study canonical\n{sequence}\n").encode()}
    sealed = json.loads((design / 'manifest.json').read_text())['files']
    source_paths = []
    for row in original['sources']:
        name = row['path']
        if not isinstance(name, str) or Path(name).name != name or name in files:
            raise ValueError('Invalid or duplicated study receptor source name')
        paths = [path for path in (design / 'case/evidence' / name, design / 'case/source_structures' / name)
                 if path.exists()]
        if len(paths) != 1:
            raise ValueError('Study receptor source is missing or ambiguous: ' + name)
        path = paths[0]
        regular(root, path, row['sha256'])
        if sealed.get(str(path.relative_to(design))) != row['sha256']:
            raise ValueError('Study receptor source is not sealed: ' + name)
        files[name] = path.read_bytes()
        source_paths.append(path)
    mapping = inventory['mapping']
    selection = {'model': inventory['model'], 'altloc': inventory['site_audit'].get('altloc')}
    # Keep the original parent-ligand audit unchanged in study-acquisition.json;
    # the shared receptor reader consumes the separate target-only audit.
    acquired = {**original, 'coordinate_audit': audit_target_site(
        design / 'case/source_structures/structure.pdb', mapping, **selection)}
    record = {'schema': 'target-acquisition-v1', 'acquisition': acquired,
        'selection': selection, 'construct_audit': inventory['construct_audit'],
        'parents': {**parent_source, 'workflow_id': state['workflow_id'],
            'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
            },
        'identity_scope': 'sealed_study_accession_sequence_species_and_gene',
        'new_target_search_performed': False, 'source_route': 'ligand-first',
        'canonical_fasta': 'canonical.fasta', 'taxon': state['target']['taxon'],
        'altlocs': inventory['altlocs'],
        'missing_construct_residue_count': len(inventory['missing_construct_residues']),
        'excluded_atom_count': len(inventory['excluded_atoms']),
        'receptor_prepared': False, 'campaign_ready': False, 'calculation_submitted': False,
        'bundle_sha256': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    lineage = [design / 'manifest.json', identity_path, original_path, *source_paths,
        root / 'results/target-hypotheses' / state['evidence_id'] / 'manifest.json',
        *extra_lineage]
    # Coordinate audits contain tuples in memory. Normalize once so recovery
    # compares the persisted JSON representation, not Python container types.
    return json.loads(encoded(record)), files, lineage


def load(root, directory, record):
    from .ligand_workflow import directory as workflow_directory
    from .workflow_store import load as load_state
    parents = record.get('parents', {})
    if parents.get('source_kind') not in {SOURCE, VS_SOURCE}:
        raise ValueError('Unsupported study receptor source')
    state = load_state(workflow_directory(root, parents.get('workflow_id')))
    expected, files, lineage = build(root, state, source_kind=parents['source_kind'])
    if record != expected:
        raise ValueError('Receptor input differs from current sealed study')
    checksum = hashlib.sha256(encoded(expected)).hexdigest()
    if directory.name != checksum[:32]:
        raise ValueError('Study receptor content identity changed')
    if {path.name for path in directory.iterdir()} != {*files, 'target_acquisition.json'}:
        raise ValueError('Study receptor bundle inventory differs')
    for name, value in files.items():
        path, _ = regular(root, directory / name, expected['bundle_sha256'][name])
        if path.read_bytes() != value:
            raise ValueError('Study receptor source bytes changed: ' + name)
    sequence = json.loads(files['study-uniprot.json'])['sequence']['value']
    acquired = load_target_acquisition(directory, checksum, sequence, record['taxon'])
    return {'acquisition_directory': directory, 'acquisition': acquired,
        'canonical_fasta': directory / 'canonical.fasta', 'canonical_sequence': sequence,
        'taxon': record['taxon'], 'target_artifact_id': parents['target_artifact_id'],
        'source_kind': parents['source_kind'], 'source_route': 'ligand-first', 'source_project_id': parents['design_id'],
        'evidence_files': sorted(set([*lineage, *directory.iterdir()]))}


def freeze(root, state):
    record, files, _ = build(root, state)
    raw = encoded(record)
    checksum = hashlib.sha256(raw).hexdigest()
    parent = root / 'state/target_acquisition'
    directory = parent / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', parent, directory)):
        raise ValueError('Study receptor input cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    if not directory.exists():
        staging = parent / ('.' + checksum[:32] + '-' + uuid4().hex[:12])
        staging.mkdir()
        for name, contents in {**files, 'target_acquisition.json': raw}.items():
            (staging / name).write_bytes(contents)
        staging.rename(directory)
    regular(root, directory / 'target_acquisition.json', checksum)
    load(root, directory, record)
    return present(root, directory, checksum, record)


def present(root, directory, checksum, record):
    return {'artifact_id': 'receptor-input-' + checksum[:32], 'manifest_sha256': checksum,
        'directory': str(directory.relative_to(root)), 'source_kind': record['parents']['source_kind'],
        'status': 'BOUND_RECEPTOR_INPUT_PREPARATION_PENDING',
        'altlocs': record['altlocs'], 'missing_construct_residue_count': record['missing_construct_residue_count'],
        'excluded_atom_count': record['excluded_atom_count'],
        'receptor_prepared': False, 'compute_submitted': False, 'md_ready': False}


def validate_binding(root, state):
    from .receptor_input import load_receptor_preparation_input
    binding = context(state).get('receptor_input')
    if not binding:
        raise ValueError('No study-bound receptor input is available')
    loaded = load_receptor_preparation_input(root, binding['artifact_id'], binding['manifest_sha256'])
    if binding != present(root, loaded['acquisition_directory'], binding['manifest_sha256'], loaded['acquisition']):
        raise ValueError('Study receptor input binding changed')
    return loaded
