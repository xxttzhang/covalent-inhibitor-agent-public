import { Database } from "bun:sqlite"
import { filename } from "./host"
import { digest, planSchema } from "./model"
import { targetContext } from "@synsci/util/screening-target"

type Artifact = { id: string; projectID: string; current: { id: string; sha256: string; size: number; sessionID: string; sourcePath: string } }

// Join immutable Result version -> publication receipt -> frozen task. Never
// infer ownership from a filename, a user-editable title or the active session.
export function artifactTarget(db: Database, artifact: Artifact, directory: string) {
  const rows = db.query(`SELECT t.plan,t.digest,p.receipt FROM artifact_publications p
    JOIN tasks t ON t.id=p.task_id WHERE json_extract(p.receipt,'$.artifact_id')=?`).all(artifact.id)
  const refinements = db.query("SELECT name FROM sqlite_master WHERE name='refinement_delivery_files'").get()
  const extra = refinements ? db.query(`SELECT t.plan,t.digest,p.receipt FROM refinement_delivery_files p
    JOIN refinement_workflows w ON w.id=p.workflow_id
    JOIN tasks t ON t.id=json_extract(w.plan,'$.stages[0].parent_id')
    WHERE json_extract(p.receipt,'$.artifact_id')=?`).all(artifact.id) : []
  const matches = [...rows, ...extra].flatMap((item) => {
    const row = item as { plan: string; digest: string; receipt: string }
    const receipt = JSON.parse(row.receipt)
    const plan = planSchema.parse(JSON.parse(row.plan))
    if (digest(plan) !== row.digest) throw new Error("Task parameter integrity check failed")
    if (plan.directory !== directory || plan.session !== artifact.current.sessionID || receipt.project_id !== artifact.projectID ||
      receipt.version_id !== artifact.current.id || receipt.sha256 !== artifact.current.sha256 || receipt.bytes !== artifact.current.size) return []
    if (receipt.local_path && receipt.local_path !== artifact.current.sourcePath) return []
    return [targetContext.parse({ task_id: plan.id, plan_digest: row.digest, artifact_sha256: artifact.current.sha256,
      target: plan.target, site: plan.site, parameters: plan.target_parameters ?? null })]
  })
  if (matches.length > 1) throw new Error("Ambiguous Result task binding")
  return matches[0] ?? null
}

export async function readArtifactTarget(artifact: Artifact, directory: string) {
  if (!await Bun.file(filename).exists()) return null
  const db = new Database(filename, { readonly: true })
  try { return artifactTarget(db, artifact, directory) } finally { db.close() }
}
