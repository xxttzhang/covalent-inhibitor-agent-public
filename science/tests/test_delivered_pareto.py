import importlib.util
from pathlib import Path


def test_delivered_ranking_reuses_full_objectives_and_preserves_exclusions():
    spec = importlib.util.spec_from_file_location('delivered', Path(__file__).parents[1] / 'scripts/rank_delivered_candidates.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {'standardized_smiles': 'CCO', 'covdock_gate': 'A', 'physical_validity': 'PASS',
            'cross_method_consensus': 'HIGH', 'property_fit_score': 1.0, 'local_strain_energy': 2.0,
            'af3_covdock_ligand_rmsd_angstrom': 1.0, 'high_reactivity_alert': False,
            'aggregation_risk': False, 'pains_alert': '', 'online_structural_status': 'BOTH_STRUCTURAL_QC_PASSED'}
    rows = [dict(base, compound_id='one', input_sdf_index=0, af3_mpae=1.0),
            dict(base, compound_id='two', input_sdf_index=1, af3_mpae=2.0),
            dict(base, compound_id='missing', input_sdf_index=2, af3_mpae=''),
            dict(base, compound_id='failed', input_sdf_index=3, af3_mpae=0.5, online_structural_status='STRUCTURAL_QC_DISAGREEMENT')]
    result = module.rank(rows)
    assert module.rank(rows) == result
    assert result['ranking']['algorithm_version'] == 'library-first-pareto-v2.0'
    assert len(result['ranking']['selection_policy']['objectives']) == 5
    assert [row['final_rank'] for row in result['candidates']] == ['1', '2', '', '']
    assert [row['pareto_front'] for row in result['candidates']][:2] == ['1.0', '2.0']
    assert result['candidates'][2]['pareto_exclusion_reason'] == 'incomplete_required_objective'
    assert result['candidates'][3]['pareto_exclusion_reason'] == 'physical_validity_FAIL_or_unknown'
    assert rows[3]['physical_validity'] == 'PASS'  # Original scientific evidence is never rewritten.
