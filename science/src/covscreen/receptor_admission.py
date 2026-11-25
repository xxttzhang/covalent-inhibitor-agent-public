"""Admit a prepared, isolated protein receptor with explicit scope limitations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from .target_acquisition_artifact import load_target_acquisition


ISOLATED_PROTEIN_POLICY_V1 = {
    'schema': 'isolated-protein-receptor-policy-v1',
    'purpose': 'protein receptor for covalent docking preparation',
    'selected_chain_action': 'retain_verified_mapped_chain',
    'other_polymer_chain_action': 'remove_preserve_source',
    'nonwater_heterogen_action': 'remove_preserve_source',
    'crystal_water_action': 'remove_preserve_source',
    'biological_assembly_suitability_validated': False,
    'cofactor_dependence_validated': False,
    'binding_site_suitability_validated': False,
    'reactivity_validated': False,
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def admit_isolated_receptor(acquisition, acquisition_sha256, canonical_sequence, taxon,
                            heavy, relaxation, relaxation_sha256, quality, quality_sha256,
                            geometry, geometry_sha256, output, *, policy=None):
    return _admit_isolated_receptor(acquisition, acquisition_sha256, canonical_sequence, taxon,
        heavy, relaxation, relaxation_sha256, quality, quality_sha256,
        geometry, geometry_sha256, output, policy=policy)


def admit_restrained_receptor(root, review_reference, bound_input, review_inventory,
        acquisition, acquisition_sha256, canonical_sequence, taxon,
        heavy, relaxation, relaxation_sha256, quality, quality_sha256,
        geometry, geometry_sha256, output):
    from .receptor_environment_review import load
    from .receptor_quality import audit_restrained_receptor
    from .receptor_geometry import audit_restrained_geometry
    review = load(root, review_reference, bound_input, review_inventory, scope='restrained-receptor')
    if review['decision'] != 'suitable_for_exploratory_receptor_preparation':
        raise ValueError('Restrained receptor admission requires all motion review concerns to be suitable')
    witnesses = [root / name for name, digest in review_inventory.items()
        if digest == bound_input.get('evaluation_sha256')]
    if len(witnesses) != 1:
        raise ValueError('Motion review requires one bound independent evaluation')
    evaluation = json.loads(witnesses[0].read_text())
    if (evaluation.get('schema') != 'ligand-receptor-review-v2'
            or evaluation.get('run_id') != bound_input.get('run_id')
            or evaluation.get('workflow_id') != bound_input.get('workflow_id')
            or evaluation.get('parents', {}).get('acquisition_sha256') != acquisition_sha256
            or evaluation.get('result', {}).get('quality') != json.loads(Path(quality).read_text())
            or evaluation.get('result', {}).get('geometry') != json.loads(Path(geometry).read_text())):
        raise ValueError('Motion review is not bound to these receptor measurements')
    source = Path(acquisition) / 'structure.pdb'
    replayed = audit_restrained_receptor(relaxation, relaxation_sha256, heavy, source)
    if json.loads(json.dumps(replayed)) != json.loads(Path(quality).read_text()):
        raise ValueError('Restrained admission identity differs from independent replay')
    measured = audit_restrained_geometry(relaxation, relaxation_sha256, quality,
        quality_sha256, source, heavy)
    if json.loads(json.dumps(measured)) != json.loads(Path(geometry).read_text()):
        raise ValueError('Restrained admission geometry differs from independent replay')
    return _admit_isolated_receptor(acquisition, acquisition_sha256, canonical_sequence, taxon,
        heavy, relaxation, relaxation_sha256, quality, quality_sha256,
        geometry, geometry_sha256, output, motion_review={
            'reference': review_reference, 'bound_input': bound_input, 'inventory': review_inventory})


def _admit_isolated_receptor(acquisition, acquisition_sha256, canonical_sequence, taxon,
        heavy, relaxation, relaxation_sha256, quality, quality_sha256,
        geometry, geometry_sha256, output, *, policy=None, motion_review=None):
    acquisition, heavy, relaxation, quality, geometry, output = map(Path,
        (acquisition, heavy, relaxation, quality, geometry, output))
    if output.exists():
        raise ValueError('Prepared receptor output must be a new directory')
    selected = ISOLATED_PROTEIN_POLICY_V1 if policy is None else policy
    if selected != ISOLATED_PROTEIN_POLICY_V1:
        raise ValueError('Only the registered isolated-protein receptor policy is supported')
    acquired = load_target_acquisition(acquisition, acquisition_sha256, canonical_sequence, taxon)
    relaxation_receipt = relaxation / 'receipt.json'
    if _sha(relaxation_receipt) != relaxation_sha256:
        raise ValueError('Relaxation receipt checksum mismatch')
    relaxed = json.loads(relaxation_receipt.read_text())
    restrained = motion_review is not None
    expected_status = ('position_restrained_relaxation_completed_quality_pending' if restrained
        else 'unrestrained_relaxation_completed_quality_pending')
    if (relaxed.get('status') != expected_status
            or relaxed.get('artificial_forces_in_final_stage') is not restrained):
        raise ValueError('A completed unrestrained relaxation is required')
    source = next(item for item in acquired['acquisition']['sources'] if item['path'] == 'structure.pdb')
    if relaxed.get('source_sha256') != source['sha256']:
        raise ValueError('Relaxed receptor differs from the acquired coordinate source')
    heavy_receipt = heavy / 'receipt.json'
    if (_sha(heavy_receipt) != relaxed.get('parent_receipt_sha256')
            or json.loads(heavy_receipt.read_text()).get('status') != 'heavy_atoms_built_pending_independent_qc'):
        raise ValueError('Relaxation heavy-atom parent changed')
    prior = json.loads(Path(quality).read_text())
    if (_sha(quality) != quality_sha256
            or prior.get('status') != 'identity_and_stereochemistry_passed_geometry_pending'
            or prior.get('relaxation_receipt_sha256') != relaxation_sha256):
        raise ValueError('Independent identity and stereochemistry audit did not pass')
    geometry_result = json.loads(Path(geometry).read_text())
    if (_sha(geometry) != geometry_sha256
            or geometry_result.get('status') != 'geometry_passed_environment_review_pending'
            or geometry_result.get('relaxation_receipt_sha256') != relaxation_sha256
            or geometry_result.get('identity_quality_sha256') != quality_sha256):
        raise ValueError('Independent receptor geometry audit did not pass')
    inventory_path = heavy / 'inventory.json'
    heavy_result = json.loads(heavy_receipt.read_text())
    if _sha(inventory_path) != heavy_result['artifacts']['inventory.json']:
        raise ValueError('Heavy-atom environment inventory changed')
    inventory = json.loads(inventory_path.read_text())
    excluded = inventory['excluded_atoms']
    groups = {}
    for item in excluded:
        atom, reason = item['atom'], item['reason']
        if reason not in {'heterogen_requires_retention_decision', 'other_chain_requires_assembly_decision'}:
            continue
        key = (reason, atom['record'], atom['resname'], atom['chain'], atom['residue'])
        group = groups.setdefault(key, {'reason': reason, 'record': atom['record'],
            'resname': atom['resname'], 'chain': atom['chain'], 'residue': atom['residue'],
            'atom_count': 0, 'atom_names': []})
        group['atom_count'] += 1
        group['atom_names'].append(atom['name'])
    environment = sorted(groups.values(), key=lambda row: (
        row['reason'], row['chain'], row['residue'], row['resname']))
    relaxed_pdb = relaxation / 'relaxed.pdb'
    mapping = relaxation / 'atom-mapping.json'
    topology = relaxation / 'topology.json'
    for name, path in [('relaxed.pdb', relaxed_pdb), ('atom-mapping.json', mapping),
                       ('topology.json', topology)]:
        if path.is_symlink() or not path.is_file() or _sha(path) != relaxed['artifacts'][name]:
            raise ValueError('Relaxation artifact changed: ' + name)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(relaxed_pdb, output / 'prepared-receptor.pdb')
    shutil.copyfile(mapping, output / 'atom-mapping.json')
    shutil.copyfile(topology, output / 'topology.json')
    shutil.copyfile(quality, output / 'identity-quality.json')
    shutil.copyfile(geometry, output / 'geometry-quality.json')
    environment_record = {'schema': 'receptor-environment-decision-v1',
        'policy': selected, 'excluded_components': environment,
        'source_preserved': True,
        'nonwater_components': [row for row in environment if row['resname'] != 'HOH'],
        'crystal_waters': [row for row in environment if row['resname'] == 'HOH'],
        'interpretation': ('The output is an isolated selected-chain protein receptor. Removal does not prove '
                           'that excluded ligands, waters, ions, cofactors or other chains are biologically dispensable.')}
    (output / 'environment-decision.json').write_text(json.dumps(environment_record, indent=2) + '\n')
    files = {path.name: _sha(path) for path in output.iterdir() if path.is_file()}
    plan = acquired['acquisition']['plan']
    manifest = {'schema': 'prepared-receptor-artifact-v1',
        'status': 'prepared_isolated_protein_scope',
        'entry_id': plan['entry'], 'uniprot': plan['uniprot'], 'canonical_cys': plan['cys'],
        'author_chain': acquired['acquisition']['coordinate_audit']['author_chain'],
        'author_residue': acquired['acquisition']['coordinate_audit']['author_residue'],
        'parents': {'target_acquisition_sha256': acquisition_sha256,
                    'heavy_atom_receipt_sha256': relaxed['parent_receipt_sha256'],
                    'relaxation_receipt_sha256': relaxation_sha256,
                    'identity_quality_sha256': quality_sha256,
                    'geometry_quality_sha256': geometry_sha256},
        'environment_policy': selected, 'artifacts': files,
        'receptor_prepared': True, 'prepared_scope': 'isolated_selected_chain_protein',
        'campaign_ready': False, 'calculation_submitted': False,
        'remaining_checks': ['biological assembly and excluded-component relevance',
                             'binding-site suitability and accessibility',
                             'supported covalent reaction chemistry and candidate identity']}
    if restrained:
        manifest.update(schema='prepared-receptor-artifact-v2',
            coordinate_scope='position_restrained_computational_proposal',
            motion_review=motion_review, observed_motion_acceptance='agent_reviewed_exploratory_scope',
            binding_site_preservation_validated=False)
    (output / 'prepared-receptor.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
