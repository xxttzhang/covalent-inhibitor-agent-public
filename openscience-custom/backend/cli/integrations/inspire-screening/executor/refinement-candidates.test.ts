import { expect, test } from 'bun:test'
import path from 'node:path'
import { Database } from 'bun:sqlite'
import { refinementCandidateFiles, type DeliveredSource } from './refinement-candidates'
import { rankingCode, scientificRoot } from './refinement-postprocess-code'
import type { combineRefinementReport } from './refinement-report'
import { tmpdir } from '../../../test/fixture/fixture'
import { refinementWorkflow } from './refinement-workflow'
import { digest } from './model'

const ledger = path.resolve(scientificRoot, '../.cache/inspire-executor/tasks.sqlite')

test.skipIf(!await Bun.file(ledger).exists())('native candidate producer freezes with bound plan context without rewriting published source tables', async () => {
  await using tmp = await tmpdir({ git: true })
  const db = new Database(ledger, { readonly: true })
  try {
    const id = 'refineflow-76378344dae197d0022d4549'
    const rows = db.query('SELECT receipt FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL').all(id) as { receipt: string }[]
    const sources = rows.map(row => JSON.parse(row.receipt) as DeliveredSource)
    const report = await Bun.file(sources.find(row => row.name === 'report/refinement-report.json')!.local_path).json() as ReturnType<typeof combineRefinementReport>
    const stored = db.query('SELECT plan FROM refinement_workflows WHERE id=?').get(id) as { plan: string }
    const plan = refinementWorkflow.parse(JSON.parse(stored.plan))
    expect(digest(plan)).toBe(report.workflow_digest)
    const source = sources.find(row => row.name === 'covdock/covdock_lo/library_first_core_subset_covdock_lo-out.maegz')!
    const stage = plan.stages[1]
    if (stage.runtime.name !== 'covdock' || stage.engine.name !== 'covdock') throw Error('Wrong archived native plan')
    const target = stage.runtime.attachment_residue.split(':')
    const context = { schema: 'library-md-native-target-v1', choice_id: 'md-choice-' + 'a'.repeat(32),
      native_workflow_id: id, native_workflow_digest: digest(plan), task_id: report.parent_task_id,
      artifact_id: source.artifact_id, version_id: source.version_id, artifact_sha256: source.sha256,
      parameters: { chain: target[0], residue: 'CYS', residue_number: Number(target[1]), reaction_type: stage.engine.reaction } }
    const before = sources.map(source => [source.local_path, Bun.file(source.local_path).size])
    const output = await refinementCandidateFiles(report, sources, rankingCode(), new AbortController().signal)
    await Bun.write(path.join(tmp.path, 'candidate-results.csv'), output['report/candidate-results.csv'])
    const candidate = report.candidates.find(row => row.status === 'BOTH_STRUCTURAL_QC_PASSED')!
    expect(candidate).toBeDefined()
    await Bun.write(path.join(tmp.path, 'context.json'), JSON.stringify({ context, source, candidate }))
    const program = `import json,sys,hashlib
from pathlib import Path
from covscreen.mcp import registry
from covscreen_library_first.md_candidate_pose import freeze_candidate_pose,load_candidate_pose_input
root=Path(sys.argv[1]); data=json.loads((root/'context.json').read_text()); native=data['context']
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
table=root/'candidate-results.csv'; source=Path(data['source']['local_path'])
assert 'covdock_target_context' not in table.read_text().splitlines()[0]
# Synthetic receiving-parent manifests isolate the producer/consumer contract.
# They do not assert that a historical prepared receptor matches these poses.
identity={'entry_id':'SOFTWARE-ONLY','uniprot':'P00533','canonical_cys':797,
 'author_chain':native['parameters']['chain'],'author_residue':str(native['parameters']['residue_number'])}
prepared=root/'prepared.json'; prepared.write_text(json.dumps({'schema':'prepared-receptor-artifact-v1',
 'status':'prepared_isolated_protein_scope',**identity,'receptor_prepared':True,'campaign_ready':False}))
context=root/'site-context.json'; context.write_text(json.dumps({'schema':'receptor-site-context-artifact-v1',
 'status':'descriptive_site_context_completed_review_pending','identity':identity,
 'parents':{'prepared_receptor_sha256':sha(prepared)},'campaign_ready':False}))
args=[table,sha(table),source,data['source']['sha256'],data['candidate']['compound_id'],data['candidate']['input_sdf_index'],prepared,sha(prepared),context,sha(context)]
try: freeze_candidate_pose(*args,root/'without-context')
except ValueError: pass
else: raise AssertionError('Missing target context must not default to a receptor')
out=root/'state/candidate_pose_input'/('f'*32)
result=freeze_candidate_pose(*args,out,native_target_context=native)
loaded=load_candidate_pose_input(root,'candidate-pose-input-'+'f'*32,sha(out/'candidate-pose-input.json'))
assert loaded['manifest']==result and result['native_target_context']==native
assert result['native_graph_qc_completed'] is False and result['md_system_ready'] is False
print(json.dumps({'frozen':True,'native_graph_qc_completed':False,'md_system_ready':False,'synthetic_receiving_parents':True}))
`
    const python = process.env.COVALENT_POSTPROCESS_PYTHON ?? path.resolve(scientificRoot, '../.cache/refinement-validation-python310/bin/python')
    const child = Bun.spawn([python, '-B', '-c', program, tmp.path], { stdout: 'pipe', stderr: 'pipe',
      env: { ...process.env, PYTHONPATH: path.join(scientificRoot, 'src') } })
    const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
    expect({ code, stderr }).toEqual({ code: 0, stderr: '' })
    expect(JSON.parse(stdout)).toEqual({ frozen: true, native_graph_qc_completed: false, md_system_ready: false, synthetic_receiving_parents: true })
    expect(sources.map(source => [source.local_path, Bun.file(source.local_path).size])).toEqual(before)
  } finally { db.close() }
}, 60000)
test.skipIf(!await Bun.file(ledger).exists())('real delivered structures produce generic, deterministic full-Pareto results without modifying source/ledger', async () => {
  const db = new Database(ledger, { readonly: true })
  try {
    const id = 'refineflow-76378344dae197d0022d4549'
    const raw = db.query('SELECT receipt FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL').all(id) as { receipt: string }[]
    const sources = raw.map((row) => JSON.parse(row.receipt) as DeliveredSource)
    const report = await Bun.file(sources.find((row) => row.name === 'report/refinement-report.json')!.local_path).json() as ReturnType<typeof combineRefinementReport>
    const before = JSON.stringify(db.query('SELECT id,submitted,handle FROM refinement_runs ORDER BY id').all())
    const fresh = { ...report, workflow_id: 'refineflow-' + 'e'.repeat(24), final_ranking: 'library-first-pareto-v2.0' as const }
    const output = await refinementCandidateFiles(fresh, sources, rankingCode(), new AbortController().signal)
    const repeated = await refinementCandidateFiles(fresh, sources, rankingCode(), new AbortController().signal)
    expect(repeated).toEqual(output)
    const result = JSON.parse(output['report/final-pareto.json'])
    expect(result.algorithm_version).toBe('library-first-pareto-v2.0')
    expect(result.workflow_id).toBe(fresh.workflow_id)
    expect(result.candidates).toHaveLength(report.candidates.length)
    expect(result.selection_policy.objectives).toEqual({ af3_mpae: 'min', property_fit_score: 'max', chemistry_risk_count: 'min',
      local_strain_energy: 'min', af3_covdock_ligand_rmsd_angstrom: 'min' })
    expect(result.candidates.every((row: Record<string, string>) => row.task_id === fresh.workflow_id && row.result_scope === 'candidate-results-v1')).toBe(true)
    expect(result.candidates.every((row: Record<string, string>) => row.covdock_artifact_id && row.af3_artifact_id)).toBe(true)
    expect(result.scientific_compute_submitted).toBe(false)
    expect(JSON.stringify(db.query('SELECT id,submitted,handle FROM refinement_runs ORDER BY id').all())).toBe(before)
    await expect(refinementCandidateFiles(fresh, sources, '0'.repeat(64), new AbortController().signal)).rejects.toThrow('implementation changed')
    await expect(refinementCandidateFiles(fresh, [...sources, sources[0]], rankingCode(), new AbortController().signal)).rejects.toThrow('Ambiguous')
    await expect(refinementCandidateFiles({ ...fresh, candidates: [fresh.candidates[0]] }, sources, rankingCode(), new AbortController().signal)).rejects.toThrow('outside approved')
  } finally { db.close() }
}, 15000)
