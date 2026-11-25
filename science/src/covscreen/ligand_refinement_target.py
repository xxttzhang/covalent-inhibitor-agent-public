"""Bind refinement target drafts to the current sealed Ligand-first study.

The coordinate projection is an input to receptor preparation, not a prepared
receptor. No scientific engine, model data pipeline or approval is invoked.
"""
import hashlib
import json
import re
from uuid import uuid4

from .af3_target import validate_target
from .jobs import sha256_file
from .ligand_design import design_folder, seal, verify
from .ligand_first_inputs import load_campaign
from .ligand_parent_evidence import require_latest
from .ligand_refinement_input import load_refinement_input
from .receptor_preparation import project_receptor
from .target_hypothesis import folder, read


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def require_construct_scope(construct):
    """The current policy requires a fully mapped, unaltered construct."""
    if (not construct['source_species_verified'] or construct['sequence_differences']
            or construct['uncertain_residues'] or construct['unaligned_entity_positions']):
        raise ValueError('Refinement construct needs an explicit sequence/variant review')


def target_drafts(case, identity, *, construct_evidence_only=False, construct_review=None, conformer_selection=None):
    """Re-read deposited sources and project an explicitly numbered target.

    Study ownership is checked by build(); this helper checks source identity.
    """
    campaign = load_campaign(case / 'campaign.json')
    acquisition = json.loads((case / 'evidence/structure_acquisition.json').read_text())
    mappings = acquisition.get('mappings', [])
    if len(mappings) != 1:
        raise ValueError('Refinement target requires one deposited site mapping')
    mapping = mappings[0]
    entity_id, chain_id = mapping['entity_id'], mapping['label_asym_id']
    if not re.fullmatch(r'[0-9]+', str(entity_id)) or not re.fullmatch(r'[A-Za-z0-9]{1,8}', str(chain_id)):
        raise ValueError('Invalid refinement target source identity')
    sources = acquisition.get('sources', [])
    names = [row['path'] for row in sources]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate refinement acquisition source')
    checksums = {row['path']: row['sha256'] for row in sources}
    paths = {'structure.pdb': case / 'source_structures/structure.pdb',
             'entry.json': case / 'evidence/entry.json',
             f'entity_{entity_id}.json': case / f'evidence/entity_{entity_id}.json',
             f'chain_{chain_id}.json': case / f'evidence/chain_{chain_id}.json'}
    for name, path in paths.items():
        if checksums.get(name) != sha256_file(path):
            raise ValueError('Refinement acquisition source changed: ' + name)
    entity = json.loads(paths[f'entity_{entity_id}.json'].read_text())
    instance = json.loads(paths[f'chain_{chain_id}.json'].read_text())
    entry = json.loads(paths['entry.json'].read_text())
    accession = campaign.target.uniprot_id
    site = campaign.target.nucleophile.uniprot_residue_number
    taxon = identity.get('organism', {}).get('taxonId')
    if (identity.get('primaryAccession') != accession
            or identity.get('sequence', {}).get('value', '')[site - 1:site] != 'C'
            or campaign.target.nucleophile.residue_name != 'CYS'
            or campaign.target.nucleophile.atom_name != 'SG'):
        raise ValueError('Refinement canonical target identity differs from the design')
    genes = {row.get('geneName', {}).get('value', '').casefold() for row in identity.get('genes', [])}
    if campaign.target.symbol.casefold() not in genes:
        raise ValueError('Refinement gene identity differs from the design')
    parent = campaign.parent_pose
    if (entry['rcsb_entry_container_identifiers']['entry_id'] != parent.pdb_id
            or acquisition['plan']['uniprot'] != accession or acquisition['plan']['cys'] != site
            or mapping['entry_id'] != parent.pdb_id
            or mapping['auth_asym_id'] != parent.protein_chain_id
            or mapping['author_residue_id'] != str(parent.target_author_residue_number)
            or parent.target_uniprot_residue_number != site):
        raise ValueError('Refinement structure/site differs from the design')
    # Native case preparation currently admits only the unaltered construct
    # scope. Do not broaden it here based on a matching target cysteine alone.
    for key in ('rcsb_mutation_count', 'rcsb_conflict_count', 'rcsb_insertion_count', 'rcsb_deletion_count'):
        value = entity['entity_poly'].get(key)
        if type(value) is not int or value != 0:
            raise ValueError('Refinement construct needs an explicit variant policy: ' + key)
    audit = acquisition['coordinate_audit']
    # Reuse explicit, sealed whole-receptor choices when provided. A target-only
    # choice cannot resolve alternate conformers elsewhere in the protein.
    alternatives = acquisition.get('receptor_altlocs')
    if alternatives is None:
        alternatives = {mapping['author_residue_id']: audit['altloc']} if audit.get('altloc') else None
    elif (not isinstance(alternatives, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in alternatives.items())
            or audit.get('altloc') and alternatives.get(mapping['author_residue_id']) != audit['altloc']):
        raise ValueError('Explicit receptor alternate locations conflict with the acquired target selection')
    # Sequence evidence must remain readable before whole-receptor conformers
    # are chosen. Independently revalidate source mapping and the acquired site;
    # projection still enforces complete explicit choices below.
    from .construct_audit import audit_construct
    from .rcsb_mapping import map_cysteine
    from .target_site_audit import audit_target_site
    if map_cysteine(entity, instance, accession, site) != mapping:
        raise ValueError('Refinement mapping differs from deposited source metadata')
    audit_target_site(paths['structure.pdb'], mapping, model=audit['model'], altloc=audit.get('altloc'))
    construct = audit_construct(entity, identity['sequence']['value'], accession, taxon)
    reviewed_scope = None
    if construct_evidence_only or construct_review is not None or conformer_selection is not None:
        from .construct_audit import audit_construct, construct_evidence_digest, validate_construct_review
        detailed = audit_construct(entity, identity['sequence']['value'], accession, taxon,
                                   include_segment_evidence=True)
        try:
            require_construct_scope(construct)
            blockers = []
        except ValueError as error:
            blockers = [str(error)]
        evidence = {'schema': 'ligand-construct-evidence-v1', 'construct_audit': detailed,
            'source_files': {name: {'path': str(path), 'sha256': checksums[name]}
                             for name, path in paths.items()},
            'source_site_mapping': mapping, 'blockers': blockers,
            'observation': 'Sequence alignment evidence only; unmapped terminal residues are not automatically tags or harmless.',
            'target_drafts_created': False, 'construct_approved': False,
            'receptor_prepared': False, 'compute_submitted': False, 'md_ready': False}
        from .receptor_conformers import inventory as conformer_inventory
        evidence['receptor_conformers'] = conformer_inventory(
            paths['structure.pdb'], checksums['structure.pdb'], mapping,
            model=audit['model'], target_altloc=audit.get('altloc'))
        evidence['evidence_sha256'] = construct_evidence_digest(evidence)
        if construct_evidence_only:
            return evidence
        if construct_review is not None:
            reviewed_scope = validate_construct_review(evidence, construct_review['review'])
            if reviewed_scope != construct_review or reviewed_scope['review']['decision'] != 'retain_deposited_construct':
                raise ValueError('A current retain-deposited-construct review is required')
        if conformer_selection is not None:
            from .receptor_conformers import validate_selection
            current_selection = validate_selection(evidence['receptor_conformers'], conformer_selection['selection'])
            if current_selection != conformer_selection:
                raise ValueError('Conformer selection receipt changed')
            alternatives = current_selection['selection']['choices']
    projection, inventory = project_receptor(paths['structure.pdb'], checksums['structure.pdb'],
        entity, instance, accession, site, identity['sequence']['value'], taxon,
        model=audit['model'], altlocs=alternatives)
    if inventory['mapping'] != mapping:
        raise ValueError('Refinement mapping differs from deposited source metadata')
    if reviewed_scope is None:
        require_construct_scope(construct)
    sequence = ''.join(entity['entity_poly']['pdbx_seq_one_letter_code_can'].split())
    position = mapping['entity_seq_id']
    seed = {'name': 'bound-refinement-target', 'dialect': 'alphafold3', 'version': 1,
            'modelSeeds': [20260725], 'sequences': [{'protein': {'id': 'A', 'sequence': sequence}}]}
    target = {'target_id': f'{accession}-C{site}-{parent.pdb_id}', 'protein_chain': 'A',
              'ligand_chain': 'L', 'cysteine_sequence_index': position,
              'residue_number': position, 'nucleophilic_atom': 'SG',
              'protein_sequence_sha256': construct['construct_sequence_sha256']}
    seed_check = validate_target(seed, 'A', position, expected_sequence_sha256=target['protein_sequence_sha256'])
    receipt = {'schema': 'refinement-target-source-v1', 'mapping': mapping,
        **({'construct_review': reviewed_scope} if reviewed_scope is not None else {}),
        **({'conformer_selection': conformer_selection} if conformer_selection is not None else {}),
        'canonical_sequence_sha256': construct['canonical_sequence_sha256'],
        'construct_sequence_sha256': construct['construct_sequence_sha256'],
        'construct_length': len(sequence), 'source_species_verified': True,
        'observed_residue_identity_verified': True, 'source_site_mapping_verified': True,
        'source_structure_sha256': checksums['structure.pdb'],
        'projected_chain': 'A', 'projected_site': position,
        'af3_seed_check': seed_check,
        'missing_construct_residue_count': len(inventory['missing_construct_residues']),
        'excluded_atom_count': len(inventory['excluded_atoms']),
        'status': 'TARGET_INPUTS_BOUND_PREPARATION_REQUIRED',
        'receptor_prepared': False, 'scientific_suitability_verified': False,
        'af3_data_pipeline_run': False, 'compute_submitted': False, 'md_ready': False,
        'grants_resource_approval': False,
        'remaining': ['Review excluded heterogens, assembly and unresolved residues',
                      'Prepare and verify receptor chemistry and environment',
                      'Bind runtime and budget before requesting native computation']}
    return {'af3-source.json': encoded(seed), 'af3-target.json': encoded(target),
            'receptor-projection.pdb': projection.encode(),
            'receptor-inventory.json': encoded(inventory), 'target-source.json': encoded(receipt)}


def build(root, state):
    """Derive drafts only from the current study and frozen candidate handoff."""
    candidates = load_refinement_input(root, state)
    require_latest(root, state['evidence_id'])
    evidence = read(root, state['evidence_id'])['evidence']
    directory = design_folder(root, state['design_id'])
    verify(directory)
    design = json.loads((directory / 'design.json').read_text())
    evidence_sha = sha256_file(folder(root, state['evidence_id']) / 'manifest.json')
    if (design['design_id'] != state['design_id'] or design['evidence_id'] != state['evidence_id']
            or design['evidence_manifest_sha256'] != evidence_sha
            or any(evidence.get(key) != value for key, value in state['target'].items())):
        raise ValueError('Refinement target does not belong to the current study')
    # Verify the same design/evidence versions frozen into the original pose
    # plan. A newly resealed replacement cannot stand in for that original.
    manifests = candidates['source_manifests']
    for path in (directory / 'manifest.json', folder(root, state['evidence_id']) / 'manifest.json'):
        if manifests.get(str(path.relative_to(root))) != sha256_file(path):
            raise ValueError('Refinement target differs from frozen pose sources')
    identity_path = directory / 'uniprot.json'
    identity = json.loads(identity_path.read_text())
    if (identity.get('primaryAccession') != evidence['identity'].get('uniprot_id')
            or identity.get('sequence', {}).get('value') != evidence['identity'].get('sequence')
            or identity.get('organism', {}).get('taxonId') != evidence['taxon']):
        raise ValueError('Refinement target identity differs from current evidence')
    # Require source coverage, not only successful verification of whatever
    # subset a manifest happens to list.
    sealed = json.loads((directory / 'manifest.json').read_text())['files']
    required = [identity_path, directory / 'design.json', directory / 'case/campaign.json',
                directory / 'case/source_structures/structure.pdb',
                *sorted((directory / 'case/evidence').glob('*.json'))]
    for path in required:
        if (path.is_symlink() or sealed.get(str(path.relative_to(directory))) != sha256_file(path)):
            raise ValueError('Refinement target source is not sealed: ' + path.name)
    files = target_drafts(directory / 'case', identity, construct_review=state.get('construct_review'),
                          conformer_selection=state.get('conformer_selection'))
    receipt = json.loads(files['target-source.json'])
    if state['target']['site'] != 'C' + str(receipt['mapping']['uniprot_residue']):
        raise ValueError('Refinement site differs from current study')
    payload = {'schema': 'ligand-refinement-target-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
        'design_manifest_sha256': sha256_file(directory / 'manifest.json'),
        'evidence_manifest_sha256': evidence_sha,
        'candidate_artifact_id': state['refinement_input']['artifact_id'],
        'candidate_manifest_sha256': state['refinement_input']['manifest_sha256'],
        'files': {name: hashlib.sha256(contents).hexdigest() for name, contents in files.items()},
        'status': receipt['status'], 'receptor_prepared': False, 'compute_submitted': False, 'md_ready': False}
    return payload, files


def freeze(root, state):
    payload, files = build(root, state)
    identifier = 'refinement-target-' + hashlib.sha256(encoded(payload)).hexdigest()[:24]
    parent = root / 'results/refinement-targets'
    if any(path.is_symlink() for path in (root / 'results', parent)):
        raise ValueError('Refinement target cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / identifier
    if destination.is_symlink():
        raise ValueError('Refinement target cannot be a symbolic link')
    if not destination.exists():
        staging = parent / ('.' + identifier + '-' + uuid4().hex[:12])
        staging.mkdir()
        for name, contents in files.items():
            (staging / name).write_bytes(contents)
        (staging / 'target-input.json').write_bytes(encoded(payload))
        seal(staging)
        staging.rename(destination)
    binding = {'artifact_id': identifier, 'manifest_sha256': sha256_file(destination / 'manifest.json'),
               'status': payload['status'], 'receptor_prepared': False, 'compute_submitted': False,
               'md_ready': False, 'directory': str(destination.relative_to(root))}
    load(root, state, binding)
    return binding


def load(root, state, binding=None):
    """Replay the source-to-target transformation on recovery, without engines."""
    binding = binding if binding is not None else state['refinement_input'].get('target')
    if not isinstance(binding, dict) or not re.fullmatch(r'refinement-target-[a-f0-9]{24}', str(binding.get('artifact_id', ''))):
        raise ValueError('Missing frozen refinement target')
    directory = root / 'results/refinement-targets' / binding['artifact_id']
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError('Refinement target cannot be a symbolic link')
    if (sha256_file(directory / 'manifest.json') != binding.get('manifest_sha256')
            or binding.get('directory') != str(directory.relative_to(root))):
        raise ValueError('Frozen refinement target binding changed')
    verify(directory)
    payload, files = build(root, state)
    if ('refinement-target-' + hashlib.sha256(encoded(payload)).hexdigest()[:24] != binding['artifact_id']
            or (directory / 'target-input.json').read_bytes() != encoded(payload)):
        raise ValueError('Frozen refinement target differs from current study')
    for key in ('status', 'receptor_prepared', 'compute_submitted', 'md_ready'):
        if binding.get(key) != payload[key]:
            raise ValueError('Frozen refinement target status changed')
    for name, contents in files.items():
        if (directory / name).is_symlink() or (directory / name).read_bytes() != contents:
            raise ValueError('Frozen refinement target artifact changed: ' + name)
    return payload


def revalidate_saved(root, state):
    """Invalidate only the derived target on failure and preserve its diagnosis."""
    handoff = state.get('refinement_input') or {}
    if not handoff.get('target'):
        return None
    try:
        load(root, state)
    except (ValueError, KeyError, OSError, TypeError) as error:
        # A VS source can temporarily fail revalidation after downstream work
        # has been registered. Keep its immutable target identity for recovery.
        previous = (handoff['target'] if handoff.get('source_kind') in {'registered-ligand-vs-review-v1', 'sealed-ligand-direct-lo-v1'}
                    else handoff.pop('target'))
        diagnosis = {'artifact_id': previous.get('artifact_id'), 'reason': str(error),
                     'scope': 'refinement_target_inputs', 'compute_submitted': False}
        handoff['target_error'] = diagnosis
        state['pose_report'] = None
        return diagnosis
    if handoff.get('source_kind') in {'registered-ligand-vs-review-v1', 'sealed-ligand-direct-lo-v1'}:
        handoff.pop('target_error', None)
    return None
