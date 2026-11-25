import z from "zod"
import { type Task, type Outcome, verifyOutcome } from "./model"
import type { Store } from "./store"
import { artifact, checksum, deliveryPath, downloadSpec, type deliverArtifact } from "./delivery"
import { ReviewRequired } from "./runner"

const saved = z.object({ id: z.string().regex(/^art_[a-f0-9-]+$/), projectID: z.string(), state: z.literal("active"),
  currentVersionID: z.string().regex(/^ver_[a-f0-9-]+$/), current: z.object({ id: z.string(), artifactID: z.string(),
    sourcePath: z.string(), sha256: z.string(), size: z.number(), sessionID: z.string() }) })
export const publication = artifact.extend({ artifact_id: z.string().regex(/^art_[a-f0-9-]+$/),
  evidence_kind: z.enum(["core_result", "failure_diagnostic", "verified_prefix"]).optional(),
  version_id: z.string().regex(/^ver_[a-f0-9-]+$/), project_id: z.string(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), bytes: z.number().int().nonnegative(),
  published_at: z.number().int().positive() }).strict()

export function publications(store: Store, task: Task) {
  const rows = store.db.query("SELECT plan_digest,receipt FROM artifact_publications WHERE task_id=? AND receipt IS NOT NULL ORDER BY phase,name")
    .all(task.id) as { plan_digest: string; receipt: string }[]
  return rows.map((row) => {
    if (row.plan_digest !== task.digest) throw new ReviewRequired("Result publication belongs to another task plan")
    return publication.parse(JSON.parse(row.receipt))
  })
}
type Delivered = Awaited<ReturnType<typeof deliverArtifact>>

// Publishes already verified local bytes through the platform's real file API.
// The journal reserves a write before sending it. An uncertain response permits
// read-only reconciliation, never blindly posting the same file again.
export class Publications {
  constructor(readonly store: Store, readonly base = "http://127.0.0.1:4096", readonly fetcher: (request: Request) => Promise<Response> = fetch) {
    const url = new URL(base)
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "openscience.internal"].includes(url.hostname) ||
        url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error("Results must stay on the local platform")
  }
  async request(task: Task, route: string, init?: RequestInit) {
    const url = new URL(route, this.base)
    url.searchParams.set("directory", task.plan.directory)
    const response = await this.fetcher(new Request(url, { ...init, redirect: "error", signal: AbortSignal.timeout(20000) }))
    if (!response.ok) throw new Error(`Result publication HTTP ${response.status}; do not replay an uncertain save`)
    return response
  }
  bound(task: Task, file: Delivered) {
    const current = this.store.get(task.id)
    if (!["SUCCEEDED", "FAILED"].includes(current.state) || current.state !== task.state || current.digest !== task.digest || file.task_id !== task.id || file.plan_digest !== task.digest ||
        !current.approval || JSON.parse(current.approval).digest !== current.digest) throw new ReviewRequired("Result publication requires the unchanged completed approved task")
    const stages = this.store.stages(task.id)
    if (current.state === "FAILED") {
      const spec = downloadSpec(current, stages, { phase: file.phase, name: file.name }, this.store)
      const kind = stages.find((stage) => stage.phase === file.phase)?.state === "FAILED" ? "failure_diagnostic" : "verified_prefix"
      if (file.sha256 !== spec.sha256 || file.evidence_kind !== kind) throw new ReviewRequired("Failure Result differs from its evidence type or retained inventory")
      return spec
    }
    if (file.evidence_kind !== "core_result") throw new ReviewRequired("Successful core Result has an inconsistent evidence type")
    if (stages.length !== current.plan.steps.length) throw new ReviewRequired("Result publication lost the core stage chain")
    stages.reduce<Outcome[]>((chain, stage, index) => {
      if (stage.ordinal !== index || stage.phase !== current.plan.steps[index].phase || stage.state !== "SUCCEEDED" || !stage.manifest) {
        throw new ReviewRequired("Result publication core evidence is incomplete")
      }
      return [...chain, verifyOutcome(JSON.parse(stage.manifest), current, stage, chain[index - 1] ?? null)]
    }, [])
    const spec = downloadSpec(current, stages, { phase: file.phase, name: file.name })
    if (file.sha256 !== spec.sha256) throw new ReviewRequired("Result file differs from frozen artifact inventory")
    return spec
  }
  async publish(task: Task, file: Delivered) {
    const spec = this.bound(task, file)
    if (file.local_path !== await deliveryPath(task, spec)) throw new ReviewRequired("Result file is not the exact task delivery path")
    const local = await checksum(file.local_path)
    if (local.sha256 !== file.sha256 || local.bytes !== file.bytes) throw new ReviewRequired("Local result changed before publication")
    const project = z.object({ id: z.string() }).parse(await (await this.request(task, "/project/current")).json())
    const active = z.array(saved).parse(await (await this.request(task, "/file/artifact-store")).json())
    const matches = active.filter((row) => row.current.sourcePath === file.local_path)
    if (matches.length > 1) throw new ReviewRequired("Multiple Result records claim this exact file")
    const result = matches[0] ?? await (async () => {
      const trash = z.array(z.object({ current: z.object({ sourcePath: z.string() }) })).parse(
        await (await this.request(task, "/file/artifact-store?state=trash")).json())
      if (trash.some((row) => row.current.sourcePath === file.local_path)) throw new ReviewRequired("Result was trashed; do not automatically restore it")
      this.bound(task, file)
      const claimed = this.store.db.query("INSERT OR IGNORE INTO artifact_publications(task_id,phase,name,plan_digest) VALUES(?,?,?,?)")
        .run(task.id, file.phase, file.name, task.digest).changes
      if (!claimed) throw new ReviewRequired("Result save has a prior or uncertain outcome; inspect the existing record instead of replaying")
      return saved.parse(await (await this.request(task, "/file/artifact", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.local_path,
          sessionID: task.plan.session, summary: `${task.id} · ${file.evidence_kind ?? "core_result"} · ${file.phase}/${file.name}` }) })).json())
    })()
    this.bound(task, file)
    if (result.projectID !== project.id || result.currentVersionID !== result.current.id || result.current.artifactID !== result.id ||
        result.current.sessionID !== task.plan.session || result.current.sourcePath !== file.local_path ||
        result.current.sha256 !== file.sha256 || result.current.size !== file.bytes) throw new ReviewRequired("Published Result does not match verified bytes and session")
    // Do not trust registration metadata alone: fetch and hash the exact version.
    const response = await this.request(task, `/file/artifact-store/${result.id}/raw?versionID=${result.current.id}`)
    const reader = response.body?.getReader()
    if (!reader) throw new ReviewRequired("Published Result has no readable body")
    const hash = new Bun.CryptoHasher("sha256")
    const size = { bytes: 0 }
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        size.bytes += chunk.value.byteLength
        if (size.bytes > file.bytes) throw new ReviewRequired("Published Result exceeds verified size")
        hash.update(chunk.value)
      }
    } finally { await reader.cancel(); reader.releaseLock() }
    if (size.bytes !== file.bytes || hash.digest("hex") !== file.sha256) throw new ReviewRequired("Published Result bytes fail independent readback")
    this.bound(task, file)
    const receipt = publication.parse({ phase: file.phase, name: file.name, artifact_id: result.id,
      evidence_kind: file.evidence_kind,
      version_id: result.current.id, project_id: result.projectID, sha256: file.sha256, bytes: file.bytes, published_at: Date.now() })
    const recorded = this.store.db.query(`INSERT INTO artifact_publications(task_id,phase,name,plan_digest,receipt) VALUES(?,?,?,?,?)
      ON CONFLICT(task_id,phase,name) DO UPDATE SET receipt=excluded.receipt WHERE plan_digest=excluded.plan_digest`)
      .run(task.id, file.phase, file.name, task.digest, JSON.stringify(receipt)).changes
    if (recorded !== 1) throw new ReviewRequired("Result publication journal differs from this plan")
    return receipt
  }
}
