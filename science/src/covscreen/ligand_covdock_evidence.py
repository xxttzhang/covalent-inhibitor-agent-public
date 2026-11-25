"""Registered CovDock measurements, frozen-policy replay and explicit limits."""
import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from . import covdock_readback
from .covdock_gate import STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION, LIBRARY_ALGORITHM_VERSION, STRAIN_COLUMNS, assess_covdock_candidates, REVIEWABLE_BATCH_OUTCOMES
from .covdock_receptor import inspect_receptor, read_effective_receptor, verify_reused_inputs
from .jobs import sha256_file
from .md_preparation_evidence import registered_output
from .receptor_evidence import regular
from .refinement_output import (MANIFEST, SPECS, Files, canonical, measurement,
    read_refinement_outputs, verify_covdock_structures)
from .refinement_quality import read_measurements, STRICT_POLICY


def same_receptor_measurements(left, right):
    """Reader versions are provenance; all measured fields must match exactly."""
    if not all(isinstance(value, dict) and value.get('schema') == 'covdock-receptor-input-check-v1'
               and value.get('reader') == 'rdkit' and isinstance(value.get('reader_version'), str)
               and value['reader_version'] for value in (left, right)):
        return False
    return ({key: value for key, value in left.items() if key != 'reader_version'}
            == {key: value for key, value in right.items() if key != 'reader_version'})


def compare_prepared_residues(original, prepared):
    """Compare observed identity while retaining explicit histidine microstates.

    Schrödinger's HID/HIE/HIP denote HIS protonation states, not mutations:
    https://learn.schrodinger.com/public/python_api/2025-4/api/schrodinger.ui.residueselector.html
    This does not validate which microstate is biologically appropriate.
    """
    def inventory(check):
        rows = check['observed_residues']
        digest = hashlib.sha256(canonical(rows)).hexdigest()
        if digest != check['observed_residue_identity_sha256']:
            raise ValueError('Receptor residue inventory differs from its inspection')
        indexed = {(r['chain'], r['residue_number'], r['insertion_code']): r['residue_name'] for r in rows}
        if len(indexed) != len(rows) or len(rows) != check['observed_residue_count']:
            raise ValueError('Receptor residue inventory is ambiguous')
        return indexed
    before, after = inventory(original), inventory(prepared)
    if before.keys() != after.keys():
        raise ValueError('Vendor-prepared receptor added, removed or renumbered observed residues')
    changes = []
    histidine = {'HIS', 'HID', 'HIE', 'HIP'}
    for key in sorted(before):
        if before[key] == after[key]:
            continue
        if before[key] not in histidine or after[key] not in histidine:
            raise ValueError('Vendor-prepared receptor changed observed residue identity: '
                             + str(key) + ' ' + before[key] + ' -> ' + after[key])
        changes.append({'chain': key[0], 'residue_number': key[1], 'insertion_code': key[2],
                        'input_name': before[key], 'prepared_name': after[key]})
    return {'observed_identity_preserved': True, 'histidine_microstate_changes': changes,
            'protonation_suitability_verified': False}


def witness(root, value, ref=None):
    raw = canonical(value) + b'\n'
    checksum = hashlib.sha256(raw).hexdigest()
    folder = root / 'state/covdock-reviews' / checksum[:32]
    expected = {'artifact_id': 'covdock-review-' + checksum[:32], 'manifest_sha256': checksum,
                'path': str((folder / 'review.json').relative_to(root))}
    if ref is not None:
        if ref != expected:
            raise ValueError('CovDock review differs from its immutable witness')
        regular(root, folder / 'review.json', checksum)
        return ref
    if any(path.is_symlink() for path in (root / 'state', folder.parent, folder)):
        raise ValueError('CovDock review cannot traverse symbolic links')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'review.json'
    if not path.exists():
        # Compact canonical encoding is the identity, not pretty-print order.
        with path.open('xb') as stream:
            stream.write(raw)
    regular(root, path, checksum)
    return expected


def approved_policy(spec, gate):
    if (spec.parameters['gate_algorithm_version'] == LIGAND_ALGORITHM_VERSION
            and gate['policy'].get('cross_method_consensus_required') is not False):
        raise ValueError('Ligand v4 requires explicitly optional cross-method evidence')
    if (gate['policy'].get('thresholds_calibrated') is not False
            or gate['policy'].get('strain_source_field') != STRAIN_COLUMNS[0]):
        raise ValueError('CovDock output changed its provisional policy or strain definition')
    def number(flag):
        if spec.command.count(flag) != 1:
            raise ValueError('Frozen CovDock command lacks an unambiguous policy flag')
        return float(spec.command[spec.command.index(flag) + 1])
    expected = {'final_n': int(number('--final-n')), 'min_success_rate': number('--min-success-rate'),
        'max_local_strain': number('--max-local-strain'), 'hard_max_strain': number('--hard-max-strain'),
        'covalent_bond_length_range': [number('--min-covalent-bond-length'), number('--max-covalent-bond-length')],
        'max_pose_rmsd': number('--max-pose-rmsd')}
    if (gate['algorithm_version'] != spec.parameters['gate_algorithm_version']
            or canonical({key: gate['policy'].get(key) for key in expected}) != canonical(expected)):
        # Python int/float JSON spelling differs legitimately for integer counts.
        if gate['algorithm_version'] != spec.parameters['gate_algorithm_version'] or any(
                isinstance(gate['policy'].get(key), bool) or gate['policy'].get(key) != value
                for key, value in expected.items()):
            raise ValueError('CovDock output thresholds differ from the approved command')
    return gate['policy']


def evaluate_outputs(root, state, manager, spec, result, *, reuse=False, route='ligand-first'):
    if route not in {'ligand-first', 'library-first', 'de-novo'} or spec.route != route:
        raise ValueError('CovDock review route differs from its registered job')
    # The registered LO runner freezes outputs before returning 4 when its
    # batch gates are not met. Legacy receipts use 2. Neither exit establishes
    # valid evidence: all registered hashes, identities and gates below must
    # still be independently replayed, including zero-selected batches.
    if (result.state, result.return_code) not in REVIEWABLE_BATCH_OUTCOMES:
        raise ValueError('CovDock readback requires a terminal batch (exit 0 or quality/coverage exit 2/4)')
    if spec.parameters['gate_algorithm_version'] not in {STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION}:
        raise ValueError('Current-study CovDock readback requires its explicit provisional policy')
    record = state['covdock_execution']
    previous = record.get('evaluation')
    if previous:
        witness(root, previous, record['evaluation_artifact'])
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts'
    def registered(name, digest=None):
        return registered_output(root, folder, spec, result, name, digest)
    manifest = registered(MANIFEST, previous['manifest_sha256'] if previous else None)
    checksum = sha256_file(manifest)
    contract = spec.parameters['refinement_input_contract']
    evidence = read_refinement_outputs(folder, 'covdock', contract, checksum,
                                      expected_gate_version=spec.parameters['gate_algorithm_version'])
    hashes = dict(evidence['artifacts'])
    for name, digest in hashes.items():
        registered(name, digest)
    files = Files(folder)
    selection = files.json(SPECS['covdock'][0], hashes[SPECS['covdock'][0]])
    for key, expected in {'input_receptor_sha256': spec.parameters['covdock_receptor_input_check']['receptor_sha256'],
            'receptor_input_check': spec.parameters['covdock_receptor_input_check'],
            'input_count': contract['input_count'], 'attachment_residue': spec.parameters['attachment_residue'],
            'nucleophilic_atom': spec.parameters['nucleophilic_atom'], 'reaction_type': spec.parameters['reaction_type'],
            'input_score_source': spec.parameters['input_score_source'],
            'pose_consistency_table': None, 'pose_consistency_table_sha256': None}.items():
        same = (same_receptor_measurements(selection.get(key), expected) if key == 'receptor_input_check'
                else selection.get(key) == expected)
        if not same:
            raise ValueError('CovDock output differs from approved source: ' + key)
    gate = files.json('stage5_gate_manifest.json', hashes['stage5_gate_manifest.json'])
    policy = approved_policy(spec, gate)
    rows = pd.read_parquet(io.BytesIO(files.read(SPECS['covdock'][1], hashes[SPECS['covdock'][1]], parse=True)))
    if 'af3_covdock_ligand_rmsd_angstrom' in rows and rows['af3_covdock_ligand_rmsd_angstrom'].notna().any():
        raise ValueError('This CovDock plan has no approved cross-method pose-comparison evidence')
    effective = None
    residue_identity = None
    if any(row['engine_status'] == 'SUCCESS' for row in evidence['candidates']):
        invocations = list((folder / 'covdock_lo').glob('*.run_manifest.json'))
        if len(invocations) != 1:
            raise ValueError('CovDock output requires one registered invocation manifest')
        invocation = invocations[0]
        relative = str(invocation.relative_to(folder))
        registered(relative)
        receipt = files.json(relative)
        if receipt.get('prepare_receptor') is not True:
            raise ValueError('CovDock invocation omitted approved vendor receptor preparation')
        ligands = list((folder / 'ligprep').glob('*_ligprep.mae'))
        if len(ligands) != 1:
            raise ValueError('CovDock output requires its exact registered LigPrep input')
        ligand = registered(str(ligands[0].relative_to(folder)))
        files.read(str(ligand))
        verify_reused_inputs(receipt, selection['input_receptor_sha256'], sha256_file(ligand),
            spec.parameters['attachment_residue'], spec.parameters['reaction_type'], 'leadopt', prepare_receptor=True)
        prepared = invocation.with_name(invocation.name[:-len('.run_manifest.json')] + '_receptor_prepared.mae')
        registered(str(prepared.relative_to(folder)), receipt['effective_receptor_sha256'])
        files.read(str(prepared), receipt['effective_receptor_sha256'])
        effective = read_effective_receptor(invocation, root / spec.parameters['receptor'],
            spec.parameters['attachment_residue'], spec.parameters['nucleophilic_atom'])
        if not same_receptor_measurements(effective, selection.get('effective_receptor_input_check')):
            raise ValueError('Effective receptor differs from the registered inspection')
        original_inventory = inspect_receptor(root / spec.parameters['receptor'],
            spec.parameters['attachment_residue'], spec.parameters['nucleophilic_atom'],
            expected_sha256=selection['input_receptor_sha256'], include_residue_inventory=True)
        prepared_inventory = inspect_receptor(prepared, spec.parameters['attachment_residue'],
            spec.parameters['nucleophilic_atom'], expected_sha256=receipt['effective_receptor_sha256'],
            include_residue_inventory=True)
        if not same_receptor_measurements(
                {k: v for k, v in original_inventory.items() if k != 'observed_residues'},
                selection['receptor_input_check']) or not same_receptor_measurements(
                {k: v for k, v in prepared_inventory.items() if k != 'observed_residues'}, effective):
            raise ValueError('Receptor residue inventory differs from the registered measurements')
        residue_identity = compare_prepared_residues(original_inventory, prepared_inventory)
    hashes.update(files.artifacts)
    implementation = {**covdock_readback.sources(), __name__: sha256_file(Path(__file__))}
    if previous and previous['output_sha256'] != hashes:
        raise ValueError('Previously verified CovDock outputs changed; retain the original witness')
    binding = covdock_readback.validate(spec.parameters.get('covdock_readback'), spec.parameters['attachment_residue'],
                                       allow_reassessment=route in {'ligand-first', 'library-first'})
    if reuse and previous and previous['implementation_sha256'] == implementation:
        return previous
    verify_covdock_structures(folder, evidence, runtime=binding['runtime'],
                             script=Path(binding['script']), assets=binding['assets'])
    measured = read_measurements(folder, evidence, STRICT_POLICY)
    metrics = {row['input_sdf_index']: row['metrics'] for row in measured['candidates']}
    gated = pd.read_parquet(io.BytesIO(files.read('stage5_gated_candidates.parquet',
                            hashes['stage5_gated_candidates.parquet'], parse=True)))
    original_indexed = {row['input_sdf_index']: row for row in gated.to_dict(orient='records')}
    original_policy = policy
    current_version = spec.parameters['gate_algorithm_version']
    if route in {'ligand-first', 'library-first'}:
        current_version = LIGAND_ALGORITHM_VERSION if route == 'ligand-first' else LIBRARY_ALGORITHM_VERSION
        # Original gates above remain verified and immutable. User-approved v4
        # is a separate interpretation of the same independently read structures.
        gated = assess_covdock_candidates(rows, max_local_strain=policy['max_local_strain'],
            hard_max_strain=policy['hard_max_strain'],
            min_covalent_bond_length=policy['covalent_bond_length_range'][0],
            max_covalent_bond_length=policy['covalent_bond_length_range'][1],
            max_pose_rmsd=policy['max_pose_rmsd'], algorithm_version=current_version)
        policy = {**policy, 'cross_method_consensus_required': False,
            'selection_rule': 'successful execution and complete physical evidence with PASS or WARN; AF3 optional; review only',
            'primary_order_within_gate': 'stable input order; optional AF3 comparison does not grant or block admission',
            'gate_semantics': {'A': 'physical PASS; AF3 optional', 'B1': 'physical soft warning',
                              'B2': 'required physical evidence missing', 'C': 'physical failure or execution unavailable'}}
    indexed = {row['input_sdf_index']: row for row in gated.to_dict(orient='records')}
    candidates = []
    for candidate in evidence['candidates']:
        row = indexed[candidate['input_sdf_index']]
        assessment = ('evidence_insufficient' if candidate['engine_status'] != 'SUCCESS' or row['physical_validity'] == 'UNKNOWN'
                      else 'rejected' if row['physical_validity'] == 'FAIL' else 'review_required')
        candidates.append({**candidate, 'measurements': metrics[candidate['input_sdf_index']], 'assessment': assessment,
            'covdock_score': measurement(row.get('covdock_score')),
            'original_gate': {key: measurement(original_indexed[candidate['input_sdf_index']].get(key))
                for key in ('covdock_gate', 'covdock_gate_detail', 'review_eligible', 'covdock_gate_reason')},
            'gate': {key: measurement(row.get(key)) for key in ('covdock_gate', 'covdock_gate_detail',
                'physical_validity', 'physical_evidence_complete', 'cross_method_consensus', 'review_eligible',
                'covdock_gate_reason', 'strain_measurement_status')}, 'validated_inhibitor': False})
    registered(MANIFEST, checksum)
    for name, digest in hashes.items():
        registered(name, digest)
    covdock_readback.validate(binding, spec.parameters['attachment_residue'], allow_reassessment=route in {'ligand-first', 'library-first'})
    successes = sum(row['engine_status'] == 'SUCCESS' for row in candidates)
    return {'schema': {'ligand-first': 'ligand', 'library-first': 'library', 'de-novo': 'denovo'}[route] + '-covdock-measurement-review-v1', 'run_id': spec.run_id,
        'manifest_sha256': checksum, 'input_contract': contract, 'output_sha256': hashes,
        'implementation_sha256': implementation, 'candidate_artifact_id': state['refinement_input']['artifact_id'],
        'frozen_readback_sources': binding['sources'],
        'target': state['target'], 'attachment_residue': spec.parameters['attachment_residue'],
        'environment_review': spec.parameters['environment_review'], 'effective_receptor_check': effective,
        'prepared_residue_identity': residue_identity,
        'receptor_reader_versions': {
            'approved_input': spec.parameters['covdock_receptor_input_check']['reader_version'],
            'producer_input': selection['receptor_input_check']['reader_version'],
            'producer_effective': (selection.get('effective_receptor_input_check') or {}).get('reader_version'),
            'independent_effective': (effective or {}).get('reader_version')},
        'gate_algorithm_version': current_version, 'gate_policy': policy,
        'original_gate_algorithm_version': spec.parameters['gate_algorithm_version'], 'original_gate_policy': original_policy,
        'policy_reassessment': current_version != spec.parameters['gate_algorithm_version'],
        'thresholds_calibrated': False, 'candidate_count': len(candidates), 'measured_candidate_count': successes,
        'failed_candidate_count': len(candidates)-successes, 'candidates': candidates,
        'assessment_scope': 'covdock_registered_structure_readback_and_provisional_gates',
        'assessment': 'review_required' if successes else 'evidence_insufficient',
        'output_validation': ('REGISTERED_STRUCTURES_AND_FROZEN_GATES_RECHECKED' if successes
            else 'REGISTERED_BATCH_AND_FROZEN_GATES_RECHECKED_NO_SUCCESSFUL_STRUCTURES'),
        'scientific_result_verified': False, 'physical_acceptance': 'not_established',
        'validated_inhibitor': False, 'md_ready': False,
        'limitations': ['Vendor structure measurements and frozen row gates were rechecked; full topology, stereochemistry and affinity are not validated.',
            'The score is supporting pose evidence, not a binding free energy or a cross-engine ranking.',
            'Absent AF3/CovDock comparison remains NA and does not become high agreement.',
            'Missing measurements remain unknown; failed engine outputs do not prove molecular inactivity.',
            'A modeled covalent bond does not establish spontaneous reaction, kinetics or selectivity.']}
