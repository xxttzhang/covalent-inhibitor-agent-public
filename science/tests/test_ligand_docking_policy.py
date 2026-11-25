import unittest

from covscreen.ligand_docking_policy import POLICY, decide


class RoutingTests(unittest.TestCase):
    def test_small_batch_direct_lo_including_six(self):
        for count in (1, 2, 6):
            result = decide(count)
            self.assertEqual(result['selected_route'], 'direct_lo')
            self.assertEqual(result['candidate_count'], count)
            self.assertFalse(result['execution_authorized'])
            self.assertFalse(result['md_ready'])

    def test_large_batch_vs_then_lo_without_truncating_count(self):
        for count in (7, 1000):
            result = decide(count)
            self.assertEqual(result['selected_route'], 'vs_then_lo')
            self.assertEqual(result['candidate_count'], count)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            decide(7, requested_route='direct_lo', reason='skip screening')

    def test_small_batch_optional_vs_requires_a_reason(self):
        with self.assertRaisesRegex(ValueError, 'requires a reason'):
            decide(2, requested_route='vs_then_lo')
        result = decide(2, requested_route='vs_then_lo', reason=' compare sampling ')
        self.assertEqual(result['selected_route'], 'vs_then_lo')
        self.assertEqual(result['selection_reason'], 'compare sampling')

    def test_no_candidates_never_produces_a_compute_route(self):
        self.assertEqual(decide(0)['selected_route'], 'no_candidates')
        self.assertEqual(decide(0, requested_route='direct_lo', reason='empty')['selected_route'],
                         'no_candidates')

    def test_reject_invalid_inputs(self):
        for count in (True, False, -1, 1.5, '2', None):
            with self.assertRaises(ValueError):
                decide(count)
        for route in ('lo', 'carsidock', None):
            with self.assertRaises(ValueError):
                decide(2, requested_route=route)
        for reason in (' ', 3, 'x' * 8001):
            with self.assertRaises(ValueError):
                decide(2, requested_route='vs_then_lo', reason=reason)

    def test_receipt_does_not_mutate_global_policy(self):
        first = decide(2)
        first['policy']['direct_lo_max_candidates'] = 99
        self.assertEqual(POLICY['direct_lo_max_candidates'], 6)
        self.assertEqual(decide(7)['selected_route'], 'vs_then_lo')


if __name__ == '__main__':
    unittest.main()
