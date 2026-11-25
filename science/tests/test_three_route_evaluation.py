import copy
import csv
import json
from pathlib import Path

from covscreen.three_route_evaluation import evaluate

ROOT = Path(__file__).resolve().parents[2]


def real_library_candidate():
    folder = ROOT / 'reports/library-consistency-20261001'
    row = next(csv.DictReader((folder / 'library_evaluated_candidates.csv').open()))
    audit = json.loads((folder / 'v2-consistency-audit.json').read_text())['records'][0]
    return {'route': 'library-first', 'compound_id': row['compound_id'], 'covdock_status': 'SUCCESS',
        'covdock_score': float(row['covdock_score']), 'local_strain_kcal_mol': float(row['local_strain_energy']),
        'physical_measurements': {'geometry_inspection_status': row['geometry_inspection_status'],
            'covalent_link_present': row['covalent_link_present'] == 'True',
            'covalent_bond_length_angstrom': float(row['covalent_bond_length_angstrom']),
            'minimum_nonbonded_heavy_atom_distance_angstrom': float(row['minimum_nonbonded_heavy_atom_distance_angstrom'])},
        'pose_consistency': {'chemistry': 'same_covalent_adduct', 'status': audit['pose_consistency_status'],
            'topology_status': audit['pose_topology_status'], 'rmsd_angstrom': audit['af3_covdock_ligand_rmsd_angstrom'],
            'protein_alignment_atom_count': audit['protein_alignment_atom_count'],
            'ligand_mapped_heavy_atom_count': audit['ligand_mapped_heavy_atom_count']}}


def test_real_candidate_requires_audited_adduct_comparison():
    record = real_library_candidate()
    assert evaluate(record)['covdock_gate_detail'] == 'A'
    for field, value in [('chemistry', 'precursor_to_covalent_adduct'), ('status', 'FAILED'),
                         ('topology_status', 'UNKNOWN'), ('protein_alignment_atom_count', 0)]:
        changed = copy.deepcopy(record)
        changed['pose_consistency'][field] = value
        result = evaluate(changed)
        assert result['physical_validity'] == 'PASS'
        assert result['cross_method_consensus'] == 'NA'
        assert result['covdock_gate_detail'] == 'B2'


def test_missing_geometry_is_not_a_pass_or_a_failed_docking():
    record = real_library_candidate()
    record['physical_measurements'] = None
    result = evaluate(record)
    assert result['physical_validity'] == 'NOT_EVALUATED'
    assert result['covdock_gate_detail'] == 'B2'


def test_actual_cross_method_disagreement_is_b1():
    record = real_library_candidate()
    folder = ROOT / 'reports/library-consistency-20261001'
    audit = json.loads((folder / 'v2-consistency-audit.json').read_text())['records'][1]
    row = list(csv.DictReader((folder / 'library_evaluated_candidates.csv').open()))[1]
    record.update(compound_id=row['compound_id'], covdock_score=float(row['covdock_score']), local_strain_kcal_mol=float(row['local_strain_energy']))
    record['pose_consistency'].update(rmsd_angstrom=audit['af3_covdock_ligand_rmsd_angstrom'],
        protein_alignment_atom_count=audit['protein_alignment_atom_count'], ligand_mapped_heavy_atom_count=audit['ligand_mapped_heavy_atom_count'])
    record['physical_measurements'].update(covalent_bond_length_angstrom=float(row['covalent_bond_length_angstrom']),
        minimum_nonbonded_heavy_atom_distance_angstrom=float(row['minimum_nonbonded_heavy_atom_distance_angstrom']))
    assert evaluate(record)['covdock_gate_detail'] == 'B1'
