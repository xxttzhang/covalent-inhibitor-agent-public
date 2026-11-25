import { expect, test } from 'bun:test'
import { candidateResultsManifest } from './candidate-results-manifest'

test('new task manifests accept missing structures but reject guessed identities/references', () => {
  const value = { schema: 'candidate-results-v1', route: 'ligand-first', task_id: 'new-task-001', assets: {},
    candidates: [{ compound_id: 'new-1', input_sdf_index: '0', standardized_smiles: 'CCO', covdock_gate_detail: 'C' }] }
  expect(candidateResultsManifest.parse(value).candidates).toHaveLength(1)
  expect(() => candidateResultsManifest.parse({ ...value, candidates: [...value.candidates, ...value.candidates] })).toThrow()
  expect(() => candidateResultsManifest.parse({ ...value, candidates: [{ ...value.candidates[0], covdock_asset: 'missing' }] })).toThrow()
  expect(() => candidateResultsManifest.parse({ ...value, task_id: '../bad' })).toThrow()
})

const scientific = {
  compound_id: 'candidate-1', input_sdf_index: '0', standardized_smiles: 'C=CC(=O)N',
  target_id: 'EGFR', site_id: 'A:CYS:797', parent_id: 'erlotinib',
  warhead_class: 'acrylamide', electrophilic_atom: 'C1', nucleophilic_atom: 'A:CYS:797:SG',
  structure_source: 'registered-run', af3_status: 'SUCCESS', af3_mpae: '1.20',
  covdock_status: 'FAILED', covdock_score: '', covdock_failure_reason: 'no accepted pose',
}

test('v2 provides one scientific and missing-score contract for all three routes', () => {
  for (const route of ['library-first', 'ligand-first', 'de-novo']) {
    const value = { schema: 'candidate-results-v2', route, task_id: `unified-${route}`, assets: {}, candidates: [scientific] }
    expect(candidateResultsManifest.parse(value).candidates).toHaveLength(1)
    expect(() => candidateResultsManifest.parse({ ...value, candidates: [{ ...scientific, target_id: '' }] })).toThrow('target_id')
    expect(() => candidateResultsManifest.parse({ ...value, candidates: [{ ...scientific, covdock_score: '0' }] })).toThrow('never zero')
    expect(() => candidateResultsManifest.parse({ ...value, candidates: [{ ...scientific, af3_mpae: '' }] })).toThrow('finite score')
    expect(() => candidateResultsManifest.parse({ ...value, candidates: [{ ...scientific, covdock_failure_reason: '' }] })).toThrow('requires a reason')
  }
})

test('v2 accepts bounded immutable task reports but rejects unknown document roles', () => {
  const report = { path: '/tmp/report.md', source: '/tmp/report.md', sha256: 'a'.repeat(64),
    size: 12, mime_type: 'text/markdown', title: 'Selection report' }
  const value = { schema: 'candidate-results-v2', route: 'ligand-first', task_id: 'unified-ligand-reports',
    assets: {}, documents: { selection_report: report }, candidates: [scientific] }
  const parsed = candidateResultsManifest.parse(value)
  if (parsed.schema !== 'candidate-results-v2') throw new Error('Expected v2 manifest')
  expect(parsed.documents?.selection_report?.sha256).toBe('a'.repeat(64))
  expect(() => candidateResultsManifest.parse({ ...value, documents: { unknown: report } })).toThrow()
  expect(() => candidateResultsManifest.parse({ ...value, documents: {
    selection_report: { ...report, mime_type: 'text/html' },
  } })).toThrow()
})
