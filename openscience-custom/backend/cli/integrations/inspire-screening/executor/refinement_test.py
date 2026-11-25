import unittest
from refinement import select, validate


def policy(**changes):
    return dict(max_candidates=3, max_fraction=0.25, disagreement_threshold=0.5, top_fraction=0.3,
                uncertainty_count=1, consensus_count=1, diversity_count=1, **{}) | changes


def rows(count=12):
    return [{'input_sdf_index': index, 'compound_id': str(index).zfill(3), 'carsiscore': float(count - index),
             'rtmscore': float(count - index), 'carsidock_status': 'SUCCESS', 'rtmscore_status': 'SUCCESS',
             'scaffold_smiles': 'scaffold-' + str(index)} for index in range(count)]


DIRECTIONS = {'carsiscore': 'higher_is_better', 'rtmscore': 'higher_is_better'}


class SelectionTest(unittest.TestCase):
    def test_uncertainty_and_sentinels_have_different_reasons(self):
        data = rows()
        data[0]['rtmscore'] = -10.0
        result = select(data, policy(), DIRECTIONS)
        self.assertEqual(result['selected_count'], 3)
        self.assertEqual([row['selection_channel'] for row in result['selected']],
                         ['rank_disagreement_review', 'consensus_sentinel', 'diversity_sentinel'])
        self.assertEqual(result['selected'][0]['compound_id'], '000')
        self.assertFalse(result['calibrated_uncertainty'])

    def test_order_and_score_scale_do_not_change_selection(self):
        data = rows()
        data[0]['rtmscore'] = -10.0
        first = select(data, policy(), DIRECTIONS)
        transformed = [{**row, 'carsiscore': row['carsiscore'] * 100 + 700} for row in reversed(data)]
        self.assertEqual(first, select(transformed, policy(), DIRECTIONS))

    def test_ties_do_not_manufacture_uncertainty(self):
        data = [{**row, 'carsiscore': 1.0, 'rtmscore': 1.0} for row in rows()]
        result = select(data, policy(consensus_count=0, diversity_count=0), DIRECTIONS)
        self.assertEqual(result['selected_count'], 0)
        self.assertTrue(all(row['rank_disagreement'] == 0 for row in result['audit']))

    def test_failures_and_nan_remain_in_denominator(self):
        data = rows()
        data[0]['rtmscore_status'] = 'FAILED'
        data[1]['carsiscore'] = float('nan')
        result = select(data, policy(), DIRECTIONS)
        self.assertEqual((result['input_count'], result['eligible_count'], result['excluded_count']), (12, 10, 2))
        self.assertFalse(result['audit'][0]['selected'])
        self.assertFalse(result['audit'][1]['selected'])
        self.assertLessEqual(result['selected_count'], 2)

    def test_small_pool_never_forces_one_or_all_candidates(self):
        result = select(rows(3), policy(), DIRECTIONS)
        self.assertEqual(result['effective_cap'], 0)
        self.assertEqual(result['selected_count'], 0)

    def test_direction_is_mandatory_and_lower_is_better_is_supported(self):
        data = rows()
        data[0]['rtmscore'] = -10.0
        expected = select(data, policy(), DIRECTIONS)
        reverse = [{**row, 'carsiscore': -row['carsiscore']} for row in data]
        actual = select(reverse, policy(), {**DIRECTIONS, 'carsiscore': 'lower_is_better'})
        self.assertEqual(expected['selected'], actual['selected'])
        with self.assertRaises(ValueError):
            select(data, policy(), {'carsiscore': 'unknown', 'rtmscore': 'higher_is_better'})

    def test_invalid_policy_missing_scaffold_and_duplicate_identity_fail_closed(self):
        for change in ({'max_fraction': 1}, {'uncertainty_count': 10}, {'max_candidates': True},
                       {'disagreement_threshold': 0}, {'top_fraction': float('nan')}, {'extra': True}):
            with self.assertRaises(ValueError):
                validate(policy(**change))
        with self.assertRaises(ValueError):
            select(rows() + [rows()[0]], policy(), DIRECTIONS)
        with self.assertRaises(ValueError):
            select([{**row, 'scaffold_smiles': None} for row in rows()], policy(), DIRECTIONS)


if __name__ == '__main__':
    unittest.main()
