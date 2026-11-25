import { expect, test } from 'bun:test'
import { refinementPareto } from './refinement-pareto'

const row = (i: number, m: number | null, s: number | null, status = 'BOTH_STRUCTURAL_QC_PASSED') => ({ input_sdf_index: i,
  compound_id: `candidate-${i}`, status, af3: { score: m }, covdock: { measurements: { strain_kcal_mol: s } } })
test('Pareto preserves tradeoffs, ties, failures and missing evidence', () => {
  const result = refinementPareto([row(1, 1, 4), row(2, 2, 2), row(3, 3, 5), row(4, null, 1),
    row(5, 0, 0, 'BOTH_STRUCTURAL_QC_REJECTED'), row(6, 1, 4)])
  expect(result.candidates.filter((r) => r.pareto_front === 1).map((r) => r.input_sdf_index)).toEqual([1, 2, 6])
  expect(result.candidates.find((r) => r.input_sdf_index === 3)?.pareto_front).toBe(2)
  expect(result.candidates.filter((r) => !r.eligible)).toHaveLength(2)
  expect(() => refinementPareto([row(1, 1, 1), row(1, 1, 1)])).toThrow()
  expect(refinementPareto([]).candidates).toEqual([])
})
