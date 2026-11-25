import { requireDockingScope, requireDockingTool } from './docking-policy'
import { createHmac, randomBytes } from 'node:crypto'
import { lstat, readFile } from 'node:fs/promises'

/** Execution tools whose explicit `start` action crosses the same native
 * approval boundary enforced by covscreen.native_approval.needs_approval.
 * Keep this list literal and reviewable: a newly registered compute tool must
 * fail closed until both the Python server and the host bridge name it. */
export const covalentStartTools = new Set([
  'covalent_library_receptor',
  'covalent_predict_cysteine_sites',
  'covalent_library_first_prepare_receptor',
  'covalent_library_first_parameterize_adduct',
  'covalent_library_first_prepare_openmm_system',
  'covalent_library_first_run_openmm_short_qc',
  'covalent_library_first_equilibrate_openmm_system',
  'covalent_library_first_run_openmm_production',
  'covalent_library_first_audit_openmm_trajectory',
  'covalent_library_first_analyze_openmm_replicas',
  'covalent_library_first_build_md_report',
  'covalent_library_first_deliver_md_candidates',
  'covalent_library_first_admit_md_candidate',
  'covalent_ligand_first_campaign',
])

export function covalentExecution(key: string, args: Record<string, unknown>) {
  const name = key.slice(key.lastIndexOf('covalent_'))
  if (!key.includes('covalent_')) return false
  if (name === 'covalent_library_research' && args.action === 'start_lo') return true
  return covalentStartTools.has(name) && args.action === 'start'
    || name === 'covalent_ligand_design_step' && ['start_vs', 'start_pose', 'start_af3', 'start_receptor_preparation', 'start_covdock', 'start_md_graph', 'start_md_parameterization', 'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration'].includes(String(args.action))
    || name === 'covalent_library_research' && ['start_vs', 'start_vs_prescreen', 'start_md_admission', 'start_md_parameterization', 'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration', 'cancel_md_stage'].includes(String(args.action))
    || ['covalent_ligand_first_chain', 'covalent_library_first_chain'].includes(name) && args.submit === true && args.execution_mode === 'execute'
    || ['covalent_submit_job', 'covalent_retry_job'].includes(name) && (args.execution_mode ?? 'execute') === 'execute'
    || name === 'covalent_cancel_job'
}

export function covalentPreparer(key: string, tools: Record<string, unknown>) {
  const name = key.slice(0, key.lastIndexOf('covalent_')) + 'covalent_prepare_execution'
  const prepare = tools[name]
  if (typeof prepare !== 'function') throw new Error('Backend approval protocol unavailable; upgrade MCP before any execution')
  return prepare as (args: Record<string, unknown>, opts: unknown) => Promise<unknown>
}

export async function covalentSigningKey() {
  const filename = process.env.COVALENT_APPROVAL_KEY_FILE
  if (!filename) throw new Error('Native approval key not configured; no execution')
  const stat = await lstat(filename)
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077)) throw new Error('Approval key must be owner-only')
  const secret = await readFile(filename)
  if (secret.length < 32) throw new Error('Approval key too short')
  return secret
}

export async function approveCovalent(key: string, args: Record<string, unknown>, session: string, message: string,
  ask: (metadata: Record<string, unknown>) => Promise<string>,
  prepare: (args: Record<string, unknown>) => Promise<unknown>) {
  requireDockingTool(key, args)
  if ('_native_approval' in args) throw new Error('Model-provided approval receipt rejected')
  const secret = await covalentSigningKey()
  const values = { ...args, human_approved: true, approval_note: args.approval_note || 'Approved through native user confirmation' }
  const text = JSON.stringify(values)
  const name = key.slice(key.lastIndexOf('covalent_'))
  const response = await prepare({ tool: name, arguments: values }) as {
    isError?: boolean; structuredContent?: { status?: string; scope?: unknown; scope_sha256?: string; compute_submitted?: boolean }
  }
  const evidence = response?.structuredContent
  if (response?.isError || evidence?.status !== 'NATIVE_APPROVAL_PREPARED' || !evidence.scope
    || !/^[a-f0-9]{64}$/.test(evidence.scope_sha256 ?? '') || evidence.compute_submitted !== false)
    throw new Error('Backend did not provide a frozen execution plan; no approval or execution')
  requireDockingScope(evidence.scope)
  const scope = JSON.stringify(evidence.scope)
  const approval = await ask({ route: 'covalent-discovery', tool: name, arguments: JSON.parse(text),
    execution_plan: JSON.parse(scope), execution_plan_sha256: evidence.scope_sha256,
    note: '批准此精确调用及其参数；参数中的布尔值本身不是授权。拒绝或取消不会执行。' })
  if (!approval.startsWith('per')) throw new Error('Native approval record missing; no execution')
  const data = Buffer.from(JSON.stringify({ tool: name, arguments_json: text, scope: JSON.parse(scope), session, message,
    approval_request_id: approval,
    nonce: randomBytes(16).toString('hex'), expires: Math.floor(Date.now() / 1000) + 600 })).toString('base64url')
  const proof = `${data}.${createHmac('sha256', secret).update(data).digest('hex')}`
  return { ...values, _native_approval: proof }
}
