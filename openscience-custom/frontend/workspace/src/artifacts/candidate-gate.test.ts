import { expect, test } from 'bun:test'
import { consensusLabel, gateDescription } from './candidate-gate'

test('each gate preserves failure, disagreement and missing evidence separately', () => {
  expect(gateDescription('A')).toContain('物理检查通过')
  expect(gateDescription('B1')).toContain('未达到')
  expect(gateDescription('B2', 'missing strain')).toContain('missing strain')
  expect(gateDescription('C')).toContain('硬物理检查失败')
  expect(gateDescription()).toContain('未评价')
  expect(consensusLabel('LOW')).toBe('较低（已评价）')
  expect(consensusLabel('NA')).toContain('缺失')
})
