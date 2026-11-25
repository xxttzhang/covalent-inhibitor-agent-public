import { Database } from "bun:sqlite"
import { digest, type Task } from "./model"
import { refinementInventory } from "./refinement-delivery"
import { combineRefinementReport } from "./refinement-report"
import type { RefinementReadback } from "./refinement-readback"

const root = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!root?.startsWith("/")) throw Error("Set an absolute COVSCREEN_ACCEPTANCE_ROOT for this historical acceptance")
const workflow = "refineflow-76378344dae197d0022d4549"
const session = "ses_f6007fb23ffe8VLlztrSdnvB6b"
const db = new Database(`${root}/.cache/inspire-executor/tasks.sqlite`, { readonly: true })
const rows = db.query("SELECT name,sha256,bytes,receipt FROM refinement_delivery_files WHERE workflow_id=? ORDER BY name").all(workflow) as
  { name: string; sha256: string; bytes: number; receipt: string | null }[]
const queue = db.query("SELECT state,report_digest,inventory_digest FROM refinement_delivery WHERE id=?").get(workflow) as
  { state: string; report_digest: string; inventory_digest: string }
async function request(route: string) {
  const url = new URL(route, "http://127.0.0.1:4096")
  url.searchParams.set("directory", `${root}/openscience-custom`)
  const response = await fetch(url, { signal: AbortSignal.timeout(60000), redirect: "error" })
  if (!response.ok) throw new Error(`Native result HTTP ${response.status}`)
  return response
}
try {
  if (queue.state !== "COMPLETE" || rows.some((row) => !row.receipt)) throw new Error("Native delivery not complete")
  const active = await (await request("/file/artifact-store")).json()
  const checked = []
  const reports = []
  for (const row of rows) {
    const saved = JSON.parse(row.receipt!)
    const matches = active.filter((item: { current: { sourcePath: string } }) => item.current.sourcePath === saved.local_path)
    if (matches.length !== 1) throw new Error(`Missing or duplicate native record: ${row.name}`)
    const match = matches[0]
    if (match.id !== saved.artifact_id || match.currentVersionID !== saved.version_id || match.current.sessionID !== session ||
      match.current.sha256 !== row.sha256 || match.current.size !== row.bytes || match.projectID !== saved.project_id) throw new Error(`Native identity mismatch: ${row.name}`)
    const body = await (await request(`/file/artifact-store/${saved.artifact_id}/raw?versionID=${saved.version_id}&download=true`)).arrayBuffer()
    if (body.byteLength !== row.bytes || new Bun.CryptoHasher("sha256").update(body).digest("hex") !== row.sha256) throw new Error(`Native bytes mismatch: ${row.name}`)
    if (row.name === "report/refinement-report.json") reports.push(JSON.parse(new TextDecoder().decode(body)))
    checked.push({ name: row.name, bytes: row.bytes, sha256: row.sha256, artifact_id: saved.artifact_id, version_id: saved.version_id })
  }
  if (reports.length !== 1 || digest(reports[0]) !== queue.report_digest) throw new Error("Frozen report mismatch")
  // JSON is canonicalized on disk; recreating Markdown directly from that JSON
  // changes interpretation field ordering. Reconstruct with the same platform
  // report builder and verify its canonical identity before checking inventory.
  const workflowRow = db.query("SELECT plan FROM refinement_workflows WHERE id=?").get(workflow) as { plan: string }
  const plan = JSON.parse(workflowRow.plan)
  const parentRow = db.query("SELECT * FROM tasks WHERE id=?").get(plan.stages[0].parent_id) as Omit<Task, "plan"> & { plan: string }
  const parent = { ...parentRow, plan: JSON.parse(parentRow.plan) }
  const executions = plan.stages.map((stage: { id: string }) => JSON.parse((db.query("SELECT manifest FROM refinement_runs WHERE id=?").get(stage.id) as { manifest: string }).manifest))
  const witnesses = plan.stages.map((stage: { id: string }) => JSON.parse((db.query("SELECT receipt FROM refinement_readback WHERE id=?").get(stage.id) as { receipt: string }).receipt))
  const original = combineRefinementReport(plan, parent, executions as [unknown, unknown], witnesses as [RefinementReadback, RefinementReadback])
  if (digest(original) !== queue.report_digest) throw new Error("Report builder no longer matches frozen evidence")
  const inventory = refinementInventory(original)
  if (digest(inventory.map(({ text, ...entry }) => entry)) !== queue.inventory_digest ||
    inventory.length !== rows.length || inventory.some((entry) => !rows.some((row) => row.name === entry.name && row.sha256 === entry.sha256))) throw new Error("Incomplete frozen inventory")
  const result = { observed: new Date().toISOString(), workflow, session, verified_files: checked.length,
    verified_bytes: checked.reduce((sum, row) => sum + row.bytes, 0), complete_inventory: true, native_identity_and_download_verified: true,
    counts: reports[0].counts, candidates: reports[0].candidates.map((row: { input_sdf_index: number }) => row.input_sdf_index), files: checked }
  await Bun.write(`${root}/reports/refinement-native-download-audit-20260922.json`, JSON.stringify(result, null, 2) + "\n")
  console.log(JSON.stringify({ ...result, files: undefined }))
} finally { db.close() }
