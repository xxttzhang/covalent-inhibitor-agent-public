"""Shared physical and pose-consensus axes; engine scores remain incomparable.

Adapters must authenticate source structures before supplying measurements.
Pre-reaction generation-to-adduct RMSD is a separate descriptive measurement.
"""
import math
import pandas as pd
from .covdock_gate import assess_covdock_candidates

POLICY = {
    'version': 'three-route-evaluation-v1',
    'routes': ['library-first', 'ligand-first', 'de-novo'],
    'covalent_distance_angstrom': [1.5, 2.2],
    'severe_clash_distance_angstrom': 1.2,
    'clash_definition': 'protein-ligand-heavy-atom-distance-excluding-designated-covalent-pair',
    'local_strain_preferred_kcal_mol': 10.0,
    'local_strain_hard_limit_kcal_mol': 20.0,
    'consensus_rmsd_limit_angstrom': 3.0,
    'consensus_requires': 'same_covalent_adduct_topology_verified_and_protein_aligned',
    'generation_to_adduct_rmsd': 'descriptive_only_not_AF3_CovDock_consensus',
    'affinity_scores': 'report_by_engine_and_protocol_no_cross_route_global_rank',
    'af3_confidence': 'report_only_not_binding_affinity',
    'missing_evidence': 'unknown_not_pass',
    'gate_A': 'computational_structure_and_consensus_not_experimental_activity',
}


def evaluate(record):
    if record['route'] not in POLICY['routes'] or not record.get('compound_id'):
        raise ValueError('Explicit supported route and compound identity required')
    measured = record.get('physical_measurements')
    if measured is None:
        return {**record, 'evaluation_policy': POLICY['version'], 'physical_validity': 'NOT_EVALUATED',
                'cross_method_consensus': 'NA', 'covdock_gate_detail': 'B2',
                'covdock_gate_reason': 'covdock_physical_evidence_missing'}
    row = dict(input_sdf_index=0, compound_id=record['compound_id'], engine_status=record['covdock_status'],
        raw_score=record['covdock_score'], r_strain_Local_Strain_Energy=record.get('local_strain_kcal_mol'))
    for key in ('covalent_link_present', 'covalent_bond_length_angstrom'):
        row[key] = measured.get(key)
    distance = measured.get('minimum_nonbonded_heavy_atom_distance_angstrom')
    row['severe_clash'] = distance < POLICY['severe_clash_distance_angstrom'] if isinstance(distance, (int, float)) and math.isfinite(distance) else None
    if measured.get('geometry_inspection_status') != 'SUCCESS':
        row['covalent_link_present'] = None
    # An unverified or chemically different comparison cannot silently become Gate A.
    consensus = record.get('pose_consistency', {})
    valid = (consensus.get('chemistry') == 'same_covalent_adduct'
        and consensus.get('status') == 'SUCCESS'
        and consensus.get('topology_status') == 'VERIFIED_FIXED_NAME_GRAPH'
        and consensus.get('protein_alignment_atom_count', 0) >= 3
        and consensus.get('ligand_mapped_heavy_atom_count', 0) > 0)
    row['af3_covdock_ligand_rmsd_angstrom'] = consensus.get('rmsd_angstrom') if valid else None
    assessed = assess_covdock_candidates(pd.DataFrame([row])).iloc[0]
    return {**record, 'evaluation_policy': POLICY['version'],
        **{key: assessed[key] for key in ('physical_validity', 'cross_method_consensus', 'covdock_gate_detail', 'covdock_gate_reason')},
        'consensus_measurement_accepted': bool(valid),
        'md_interpretation': 'candidate_for_validation_not_MD_ready_or_active'}
