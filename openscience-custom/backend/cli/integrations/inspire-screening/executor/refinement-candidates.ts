import path from 'node:path'
import { candidateTable } from '../../../../../frontend/workspace/src/artifacts/candidate-table'
import { maestroGeometry, maestroOffsets } from '../../../../../frontend/workspace/src/artifacts/maestro-preview'
import { gzipPreview } from '../../../../../frontend/workspace/src/artifacts/gzip-preview'
import { rankingCode, rankingScript, scientificRoot } from './refinement-postprocess-code'
import type { combineRefinementReport } from './refinement-report'
import { digest } from './model'

type Report = ReturnType<typeof combineRefinementReport>
export type DeliveredSource = { name: string; sha256: string; bytes: number; local_path: string;
  artifact_id: string; version_id: string; project_id: string }
export const candidateResultNames = ['report/candidate-results.csv', 'report/final-pareto.csv', 'report/final-pareto.json']
const csv = (rows: Record<string, string>[]) => {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))]
  const quote = (raw: string) => '"' + String(raw ?? '').replaceAll('"', '""') + '"'
  return [columns, ...rows.map((row) => columns.map((key) => row[key] ?? ''))].map((row) => row.map(quote).join(',')).join('\n') + '\n'
}

// Only previously delivered, native-version-verified files enter this function.
// No remote paths are fetched and no scientific inference/docking is invoked.
export async function refinementCandidateFiles(report: Report, sources: DeliveredSource[], code: string, signal: AbortSignal) {
  if (rankingCode() !== code) throw new Error('Approved Pareto implementation changed')
  if (new Set(sources.map((entry) => entry.name)).size !== sources.length
    || new Set(sources.map((entry) => entry.project_id)).size !== 1) throw new Error('Ambiguous delivered source scope')
  const verified = new Map<string, Uint8Array>()
  const complexes = new Map<string, { text: string; offsets: ReturnType<typeof maestroOffsets> }>()
  const get = (name: string) => {
    const source = sources.find((entry) => entry.name === name)
    if (!source) throw new Error(`Missing delivered source: ${name}`)
    return source
  }
  const read = async (source: DeliveredSource) => {
    if (verified.has(source.name)) return verified.get(source.name)!
    const file = Bun.file(source.local_path)
    if (file.size !== source.bytes || file.size > 128 * 1024 * 1024) throw new Error('Delivered source size changed')
    const bytes = new Uint8Array(await file.arrayBuffer())
    if (new Bun.CryptoHasher('sha256').update(bytes).digest('hex') !== source.sha256) throw new Error('Delivered source hash changed')
    verified.set(source.name, bytes)
    return bytes
  }
  const table = candidateTable(new TextDecoder().decode(await read(get('covdock/stage5_gated_candidates.csv'))))
  const scores = candidateTable(new TextDecoder().decode(await read(get('af3/af3_all_scores.csv'))))
  if (!table || !scores || !report.candidates.length || report.candidates.length > 100) throw new Error('Missing bounded candidate tables')
  for (const rows of [table.rows, scores.rows]) {
    if (new Set(rows.map((row) => row.compound_id)).size !== rows.length
      || new Set(rows.map((row) => row.input_sdf_index)).size !== rows.length) throw new Error('Duplicate source identity')
    if (rows.some((row) => !report.candidates.some((item) => item.compound_id === row.compound_id && String(item.input_sdf_index) === row.input_sdf_index)))
      throw new Error('Source contains candidates outside approved refinement')
  }
  const rows: Record<string, string>[] = []
  for (const candidate of report.candidates) {
    signal.throwIfAborted()
    const match = (row: Record<string, string>) => row.compound_id === candidate.compound_id && row.input_sdf_index === String(candidate.input_sdf_index)
    const original = table.rows.find(match)
    const score = scores.rows.find(match)
    if (!original && !score) throw new Error('Approved candidate lost from source tables')
    const row: Record<string, string> = { ...(original ?? score!), route: 'library-first', task_id: report.workflow_id,
      result_scope: 'candidate-results-v1', online_structural_status: candidate.status,
      af3_mpae: candidate.af3.score === null ? '' : String(candidate.af3.score),
      source_table_sha256: get('covdock/stage5_gated_candidates.csv').sha256,
      af3_score_source_sha256: get('af3/af3_all_scores.csv').sha256 }
    if (!row.standardized_smiles) throw new Error('Candidate SMILES missing')
    // Never trust pre-existing display links in an input table.
    for (const key of Object.keys(row)) if (/_artifact_(id|version|sha256)$/.test(key)) delete row[key]
    const link = (method: string, source: DeliveredSource) => {
      row[`${method}_artifact_id`] = source.artifact_id
      row[`${method}_artifact_version`] = source.version_id
      row[`${method}_artifact_sha256`] = source.sha256
    }
    if (original && /^\d+$/.test(original.covdock_pose_index ?? '')) {
      const source = get('covdock/covdock_lo/library_first_core_subset_covdock_lo-out.maegz')
      if (!complexes.has(source.name)) {
        const text = new TextDecoder().decode(await gzipPreview(new Uint8Array(await read(source))))
        complexes.set(source.name, { text, offsets: maestroOffsets(text) })
      }
      const complex = complexes.get(source.name)!
      const pose = Number(original.covdock_pose_index)
      const geometry = maestroGeometry(complex.text, pose, complex.offsets)
      if (geometry.compound !== candidate.compound_id || geometry.inputIndex !== String(candidate.input_sdf_index)) throw new Error('CovDock pose identity mismatch')
      row.covdock_structure_pose = String(pose)
      link('covdock', source)
    }
    if (score?.status === 'SUCCESS') {
      if (Number(score.mpae) !== candidate.af3.score || !score.mpae.trim()) throw new Error('AF3 score differs from validated report')
      const start = score.model_cif?.indexOf('/af3/predictions/') ?? -1
      if (start < 0) throw new Error('AF3 structure path outside delivered prediction inventory')
      const source = get(score.model_cif.slice(start + 1))
      await read(source)
      link('af3', source)
    }
    rows.push(row)
  }
  const python = process.env.COVALENT_POSTPROCESS_PYTHON ?? path.resolve(scientificRoot, '../.cache/refinement-validation-python310/bin/python')
  const child = Bun.spawn([python, rankingScript], { stdin: new Blob([JSON.stringify(rows)]), stdout: 'pipe', stderr: 'pipe',
    env: { PATH: process.env.PATH, PYTHONPATH: path.join(scientificRoot, 'src'), PYTHONNOUSERSITE: '1', OPENBLAS_NUM_THREADS: '1', OMP_NUM_THREADS: '1' } })
  const stop = () => child.kill()
  signal.addEventListener('abort', stop, { once: true })
  const timer = setTimeout(stop, 60000)
  try {
    const [output, errors, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
    signal.throwIfAborted()
    if (status !== 0) throw new Error(`Local Pareto postprocessing failed (no compute submitted): ${errors.slice(-1000)}`)
    if (rankingCode() !== code) throw new Error('Pareto implementation changed during processing')
    const result = JSON.parse(output) as { ranking: Record<string, unknown>; candidates: Record<string, string>[] }
    if (result.ranking.algorithm_version !== 'library-first-pareto-v2.0' || result.candidates.length !== rows.length)
      throw new Error('Pareto result contract mismatch')
    const ordered = result.candidates.map((ranked) => {
      const source = rows.find((row) => row.compound_id === ranked.compound_id && row.input_sdf_index === ranked.input_sdf_index)
      if (!source) throw new Error('Pareto candidate identity changed')
      return { ...source, final_rank: ranked.final_rank, pareto_front: ranked.pareto_front,
        pareto_eligible: ranked.pareto_eligible, pareto_exclusion_reason: source.online_structural_status === 'BOTH_STRUCTURAL_QC_PASSED'
          ? ranked.pareto_exclusion_reason : 'online_structural_quality_not_passed',
        ranking_policy: 'library-first-pareto-v2.0' }
    })
    const table = csv(ordered)
    return { 'report/candidate-results.csv': table, 'report/final-pareto.csv': table,
      'report/final-pareto.json': JSON.stringify({ workflow_id: report.workflow_id, workflow_digest: report.workflow_digest,
        report_sha256: digest(report), postprocess_code_sha256: code,
        source_files: sources.filter((source) => verified.has(source.name)).map((source) => ({ name: source.name,
          artifact_id: source.artifact_id, version_id: source.version_id, sha256: source.sha256, bytes: source.bytes })), ...result.ranking,
        candidates: ordered, online_quality_filter: 'BOTH_STRUCTURAL_QC_PASSED required in addition to scientific physical gate',
        scientific_compute_submitted: false }, null, 2) + '\n' }
  } finally { clearTimeout(timer); signal.removeEventListener('abort', stop); if (child.exitCode === null) child.kill() }
}
