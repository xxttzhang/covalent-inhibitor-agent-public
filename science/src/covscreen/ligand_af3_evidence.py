"""Registered AF3 readback for Ligand-first; measurements are not acceptance."""
from pathlib import Path

from .jobs import sha256_file
from .md_preparation_evidence import registered_output
from .refinement_output import (MANIFEST, SPECS, Files, read_refinement_outputs,
                                verify_af3_confidence, verify_af3_geometry)


def evaluate_outputs(root, state, manager, spec, result):
    """Caller first revalidates the signed job and current-study input lineage.

    The runner may exit 2 after writing a complete census with low coverage.
    Read that census without converting engine failure into molecular rejection.
    """
    if (result.state, result.return_code) not in {('COMPLETED', 0), ('FAILED', 2)}:
        raise ValueError('AF3 readback requires a terminal registered batch (exit 0 or coverage exit 2)')
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts'
    previous = state['af3_execution'].get('evaluation')
    manifest = registered_output(root, folder, spec, result, MANIFEST,
                                previous['manifest_sha256'] if previous else None)
    checksum = sha256_file(manifest)
    contract = spec.parameters['refinement_input_contract']
    evidence = read_refinement_outputs(folder, 'af3', contract, checksum)
    # Every consumed source must also belong to the registered task receipt.
    for name, digest in evidence['artifacts'].items():
        registered_output(root, folder, spec, result, name, digest)
    files = Files(folder)
    selection = files.json(SPECS['af3'][0], evidence['artifacts'][SPECS['af3'][0]])
    runtime = spec.parameters['af3_target_seed_check']
    for name, digest in (('source_data_json', runtime['source_data_sha256']),
                         ('target_config', runtime['target_config_sha256'])):
        if selection.get('inputs', {}).get(name, {}).get('sha256') != digest:
            raise ValueError('AF3 output source differs from approved target: ' + name)
    execution = selection.get('execution', {})
    if (type(execution.get('total')) is not int or execution['total'] != contract['input_count']
            or execution.get('run_data_pipeline') is not spec.parameters['run_data_pipeline']):
        raise ValueError('AF3 execution receipt differs from approved batch')
    confidence = verify_af3_confidence(folder, evidence,
        protein_chain=runtime['protein_chain'], ligand_chain=runtime['ligand_chain'])
    geometry = verify_af3_geometry(folder, evidence,
        candidate_table=root / spec.parameters['candidate_table'],
        target_config=root / spec.parameters['target_config'],
        target_sha256=runtime['target_config_sha256'], runtime=runtime)
    target_directory = root / state['refinement_input']['target']['directory']
    target_source = target_directory / 'target-source.json'
    target_receipt = Files(target_directory).json('target-source.json',
        spec.input_sha256[str(target_source.relative_to(root))])
    confidences = {row['input_sdf_index']: row for row in confidence['candidates']}
    geometries = {row['input_sdf_index']: row for row in geometry['candidates']}
    candidates = [{**row,
        'confidence': confidences[row['input_sdf_index']]['measurements'],
        'geometry': geometries[row['input_sdf_index']]['measurements'],
        'assessment': 'review_required' if row['engine_status'] == 'SUCCESS' else 'evidence_insufficient',
        'physical_acceptance': 'not_established', 'validated_inhibitor': False,
    } for row in evidence['candidates']]
    # Re-read the immutable witness and registered bytes after all parsing.
    read_refinement_outputs(folder, 'af3', contract, checksum)
    registered_output(root, folder, spec, result, MANIFEST, checksum)
    for name, digest in evidence['artifacts'].items():
        registered_output(root, folder, spec, result, name, digest)
    from . import af3_results, refinement_output
    successes = sum(row['engine_status'] == 'SUCCESS' for row in candidates)
    return {'schema': 'ligand-af3-measurement-review-v1', 'run_id': spec.run_id,
        'manifest_sha256': checksum, 'input_contract': contract,
        'target_artifact_id': state['refinement_input']['target']['artifact_id'],
        'candidate_artifact_id': state['refinement_input']['artifact_id'],
        'target_mapping': {'source': target_receipt['mapping'],
            'projected_chain': target_receipt['projected_chain'],
            'projected_site': target_receipt['projected_site'],
            'af3_protein_chain': runtime['protein_chain'],
            'af3_sequence_site': runtime['cysteine_sequence_index'],
            'geometry_source_residue_scope': 'projected_construct_numbering'},
        'output_sha256': evidence['artifacts'],
        'implementation_sha256': {module.__name__: sha256_file(Path(module.__file__))
            for module in (af3_results, refinement_output)},
        'candidate_count': len(candidates), 'measured_candidate_count': successes,
        'failed_candidate_count': len(candidates) - successes, 'candidates': candidates,
        'assessment_scope': 'af3_confidence_and_limited_geometry_readback',
        'assessment': 'review_required' if successes else 'evidence_insufficient',
        'output_validation': 'REGISTERED_BYTES_CONFIDENCE_AND_GEOMETRY_RECHECKED',
        'scientific_result_verified': False, 'physical_acceptance': 'not_established',
        'validated_inhibitor': False, 'md_ready': False,
        'limitations': [
            'Confidence is model uncertainty, not affinity, reaction kinetics or inhibition.',
            'The declared covalent connection is an input constraint; its presence is not evidence of spontaneous reaction.',
            'Atom inventory and limited geometry are checked; full protein identity, bond graph and stereochemistry are not independently verified here.',
            'Nonbonded distances cover the named protein and ligand heavy atoms and exclude only the declared reactive pair; they are not a calibrated clash decision.',
            'No new numerical acceptance thresholds or candidate ranking are introduced by this readback.',
            'Failed or absent candidate output remains in the denominator and does not establish molecular invalidity.',
        ]}
