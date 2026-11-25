import z from 'zod'
import path from 'node:path'

const asset = z.object({ path: z.string().refine(path.isAbsolute), source: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), size: z.number().int().positive().max(128 * 1024 * 1024) }).strict()
const document = z.object({ path: z.string().refine(path.isAbsolute), source: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), size: z.number().int().positive().max(8 * 1024 * 1024),
  mime_type: z.enum(['text/markdown', 'application/json']), title: z.string().min(1).max(256) }).strict()
const taskID = z.string().regex(/^[A-Za-z0-9._-]{6,128}$/)
const route = z.enum(['library-first', 'ligand-first', 'de-novo'])

const legacyCandidateResultsManifest = z.object({
  schema: z.literal('candidate-results-v1'),
  route: z.enum(['library-first', 'ligand-first']),
  task_id: taskID,
  assets: z.record(z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/), asset),
  candidates: z.array(z.record(z.string(), z.string())).min(1).max(100),
}).strict()

const candidateResultsV2Manifest = z.object({
  schema: z.literal('candidate-results-v2'),
  route,
  task_id: taskID,
  assets: z.record(z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/), asset),
  documents: z.object({ selection_report: document.optional(), traceability_report: document.optional(),
    traceability_json: document.optional() }).strict().optional(),
  candidates: z.array(z.record(z.string(), z.string())).min(1).max(100),
}).strict()

export const candidateResultsManifest = z.union([
  legacyCandidateResultsManifest,
  candidateResultsV2Manifest,
]).superRefine((manifest, context) => {
  const fail = (message: string) => context.addIssue({ code: 'custom', message })
  if (Object.keys(manifest.assets).length > 200) fail('Too many structure assets')
  if (new Set(manifest.candidates.map((r) => r.compound_id)).size !== manifest.candidates.length
    || new Set(manifest.candidates.map((r) => r.input_sdf_index)).size !== manifest.candidates.length) fail('Duplicate identity')
  for (const row of manifest.candidates) {
    if (!row.compound_id || !/^\d+$/.test(row.input_sdf_index ?? '') || !row.standardized_smiles) fail('Missing candidate identity/SMILES')
    for (const method of ['covdock', 'af3', 'generated']) {
      if (row[`${method}_asset`] && !manifest.assets[row[`${method}_asset`]]) fail('Unknown structure reference')
    }
    if (row.covdock_asset && !/^\d+$/.test(row.covdock_structure_pose ?? '')) fail('Exact CovDock pose required')
    if (manifest.schema === 'candidate-results-v2') {
      for (const field of ['target_id', 'site_id', 'parent_id', 'warhead_class',
        'electrophilic_atom', 'nucleophilic_atom', 'structure_source']) {
        if (!row[field]?.trim()) fail(`Missing unified scientific field: ${field}`)
      }
      for (const [method, score] of [['af3', 'af3_mpae'], ['covdock', 'covdock_score']] as const) {
        const status = row[`${method}_status`]
        if (!['SUCCESS', 'FAILED', 'NOT_RUN'].includes(status)) {
          fail(`Invalid ${method}_status`)
          continue
        }
        const raw = row[score]?.trim() ?? ''
        const finite = raw !== '' && Number.isFinite(Number(raw))
        if (status === 'SUCCESS' && !finite) fail(`${method} SUCCESS requires a finite score`)
        if (status !== 'SUCCESS' && raw !== '') fail(`${method} missing score must be empty, never zero or another number`)
        if (status === 'FAILED' && !row[`${method}_failure_reason`]?.trim()) fail(`${method} FAILED requires a reason`)
      }
    }
  }
})
