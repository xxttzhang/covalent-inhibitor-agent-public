import path from "node:path"
import { randomUUID } from "node:crypto"
import { chmod, link, lstat, mkdir, mkdtemp, realpath, rmdir, unlink } from "node:fs/promises"
import z from "zod"
import type { Store } from "./store"
import { digest, type Task } from "./model"
import { Refinements } from "./refinement-approval"
import { RefinementWorkflows } from "./refinement-workflow"
import { RefinementRuns } from "./refinement-runs"
import { readRefinementReport, type combineRefinementReport } from "./refinement-report"
import { refinementReportFiles } from "./refinement-report-files"
import { Inspire } from "./inspire"
import { checksum } from "./delivery"
import { Publications } from "./publication"
import { ReviewRequired } from "./runner"
import { refinementCandidateFiles, candidateResultNames } from './refinement-candidates'

type Report = ReturnType<typeof combineRefinementReport>
type Entry = { name: string; sha256: string; remote: string | null; text: string | null }
type Queue = { id: string; digest: string; report_digest: string; inventory_digest: string; state: string;
  error: string | null; updated: number; lease_owner: string | null; lease_until: number }
const record = z.object({ id: z.string(), projectID: z.string(), state: z.literal("active"), currentVersionID: z.string(),
  current: z.object({ id: z.string(), artifactID: z.string(), sourcePath: z.string(), sha256: z.string(), size: z.number(), sessionID: z.string() }) })
const receipt = z.object({ name: z.string(), artifact_id: z.string(), version_id: z.string(), project_id: z.string(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), bytes: z.number().int().nonnegative(), local_path: z.string(),
  published_at: z.number().int().positive() }).strict()

export const refinementDeliveryObservation = z.object({
  schema: z.literal("refinement-publication-observation-v1"),
  workflow_id: z.string().min(1).max(100), workflow_digest: z.string().regex(/^[a-f0-9]{64}$/),
  task_id: z.string().regex(/^screen-[a-f0-9]{24}$/), checked_at: z.number().int().positive(),
  state: z.enum(["NOT_STARTED", "WAITING", "ACTIVE", "PAUSED", "COMPLETE"]),
  verification: z.enum(["verified", "not_ready", "evidence_rejected", "verification_unavailable"]),
  report_digest: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
  inventory_digest: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
  files: z.array(receipt.omit({ local_path: true, published_at: true }).extend({
    name: z.string().min(1).max(1000), artifact_id: z.string().min(1).max(200),
    version_id: z.string().min(1).max(200), project_id: z.string().min(1).max(200),
    bytes: z.number().int().min(0).max(128 * 1024 * 1024),
  })).max(2000),
  compute_submitted: z.literal(false), platform_files_published: z.literal(false), md_ready: z.literal(false),
}).strict()

export function refinementInventory(report: Report): Entry[] {
  const generated = Object.entries(refinementReportFiles(report)).map(([name, text]) => ({ name: `report/${name}`,
    sha256: new Bun.CryptoHasher("sha256").update(text).digest("hex"), remote: null, text }))
  const originals = report.stages.flatMap((stage) => stage.artifacts.map((file) => ({
    name: `${stage.engine}/${file.name}`, sha256: file.sha256, remote: file.remote_path, text: null })))
  const entries = [...generated, ...originals]
  if (new Set(entries.map((entry) => entry.name)).size !== entries.length || entries.some((entry) =>
    !/^[a-f0-9]{64}$/.test(entry.sha256) || /[\\\x00-\x1f\x7f]/.test(entry.name) ||
    entry.name.split("/").some((part) => !part || part === "." || part === ".."))) throw new ReviewRequired("Invalid complete refinement delivery inventory")
  return entries
}

export function refinementDeliveries(store: Store, task: Task) {
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_delivery'").get()) return []
  const rows = store.db.query("SELECT d.* FROM refinement_delivery d JOIN refinement_workflows w ON w.id=d.id WHERE json_extract(w.plan,'$.stages[0].parent_id')=? ORDER BY d.updated DESC")
    .all(task.id) as Queue[]
  const workflows = new RefinementWorkflows(new Refinements(store))
  return rows.map((row) => {
    const workflow = workflows.get(row.id)
    if (workflow.digest !== row.digest || workflow.plan.stages[0].session !== task.plan.session || !workflow.plan.delivery) throw new ReviewRequired("Refinement delivery snapshot identity changed")
    workflows.authorize(workflow.plan.stages[1].id)
    if (workflow.plan.recovery_of) workflows.verifyRetry(workflow.plan)
    const files = store.db.query("SELECT receipt FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL ORDER BY name")
      .all(row.id) as { receipt: string }[]
    return { workflow_id: row.id, state: row.state, error: row.error, recorded_at: row.updated,
      report_digest: row.report_digest, completed_files: files.length, files: files.map((file) => receipt.parse(JSON.parse(file.receipt))),
      note: "交付历史快照，不是当前文件可用性；计算状态与交付分开，恢复交付不会重跑计算。" }
  })
}

// The daemon supplies only stored workflow IDs. No user/model path, arbitrary
// output, compute callback or old core delivery grant can enter this boundary.
export class RefinementDelivery {
  readonly owner = randomUUID()
  constructor(readonly store: Store, readonly api = new Publications(store),
    readonly transport: (task: Task) => Pick<Inspire, "download"> = (task) => new Inspire(task.plan.connection),
    readonly read = readRefinementReport,
    readonly candidates = refinementCandidateFiles) {
    store.db.exec(`CREATE TABLE IF NOT EXISTS refinement_delivery(id TEXT PRIMARY KEY,digest TEXT NOT NULL,
      report_digest TEXT NOT NULL,inventory_digest TEXT NOT NULL,state TEXT NOT NULL,error TEXT,updated INTEGER NOT NULL,
      lease_owner TEXT,lease_until INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS refinement_delivery_files(workflow_id TEXT NOT NULL,name TEXT NOT NULL,sha256 TEXT NOT NULL,
      local_path TEXT NOT NULL,bytes INTEGER NOT NULL,attempted INTEGER,receipt TEXT,PRIMARY KEY(workflow_id,name))`)
  }
  scope(id: string) {
    const workflows = new RefinementWorkflows(new Refinements(this.store))
    const workflow = workflows.get(id)
    workflows.authorize(workflow.plan.stages[1].id)
    if (workflow.plan.recovery_of) workflows.verifyRetry(workflow.plan)
    if (!workflow.plan.delivery) throw new ReviewRequired("No independently approved refinement delivery policy")
    const runs = new RefinementRuns(workflows.refinements)
    workflow.plan.stages.forEach((stage) => {
      workflows.refinements.parent(stage)
      const run = runs.get(stage.id)
      if (run.state !== "SUCCEEDED" || !run.manifest || run.submitted === null || run.handle !== run.name) throw new ReviewRequired("Refinement delivery requires both actual completed executions")
    })
    return { workflow, task: this.store.get(workflow.plan.stages[0].parent_id), policy: workflow.plan.delivery }
  }
  get(id: string) { return this.store.db.query("SELECT * FROM refinement_delivery WHERE id=?").get(id) as Queue | null }
  /** Inspect existing publications without taking a delivery lease or saving,
   * restoring, regenerating or reposting a file. COMPLETE is only history. */
  async inspect(id: string, parent: Task, abort: AbortSignal) {
    abort.throwIfAborted()
    const { workflow, task, policy } = this.scope(id)
    if (parent.id !== task.id || parent.digest !== task.digest || parent.plan.directory !== task.plan.directory ||
      parent.plan.session !== task.plan.session) throw new ReviewRequired("Publication inspection belongs to another task")
    const queue = this.get(id)
    if (queue && queue.digest !== workflow.digest) throw new ReviewRequired("Publication workflow digest changed")
    const result = (verification: z.infer<typeof refinementDeliveryObservation>["verification"],
      files: z.infer<typeof refinementDeliveryObservation>["files"] = []) => refinementDeliveryObservation.parse({
      schema: "refinement-publication-observation-v1", workflow_id: id, workflow_digest: workflow.digest, task_id: task.id,
      checked_at: Date.now(), state: queue?.state ?? "NOT_STARTED", verification,
      report_digest: queue?.report_digest ?? null, inventory_digest: queue?.inventory_digest ?? null,
      files, compute_submitted: false, platform_files_published: false, md_ready: false,
    })
    if (!queue || queue.state !== "COMPLETE") return result("not_ready")
    const snapshot = () => this.store.db.query("SELECT * FROM refinement_delivery_files WHERE workflow_id=? ORDER BY name")
      .all(id) as { name: string; sha256: string; local_path: string; bytes: number; attempted: number | null; receipt: string | null }[]
    const saved = snapshot()
    const context = { directory: task.plan.directory, sessionID: task.plan.session, abort }
    try {
      const source = await this.read(this.store, task, context, id)
      abort.throwIfAborted()
      if (!source.report_ready || !source.report) return result(refinementDeliveryObservation.shape.verification.parse(
        source.verification === "verified" ? "evidence_rejected" : source.verification))
      const report = source.report
      if (report.workflow_id !== id || report.workflow_digest !== workflow.digest || report.parent_task_id !== task.id ||
        report.parent_digest !== task.digest || digest(report) !== queue.report_digest)
        throw new ReviewRequired("Published report no longer matches its approved source")
      const entries = refinementInventory(report)
      const derived = policy.candidate_results ? candidateResultNames : []
      const names = [...entries.map(entry => entry.name), ...derived].sort()
      if (queue.inventory_digest !== digest(entries.map(({ text, ...entry }) => entry)) ||
        names.length > policy.max_files || digest(saved.map(file => file.name)) !== digest(names) ||
        saved.reduce((sum, file) => sum + file.bytes, 0) > policy.max_total_bytes)
        throw new ReviewRequired("Published inventory differs from approved delivery")
      const files = saved.map(file => {
        const parsed = receipt.safeParse(file.receipt ? JSON.parse(file.receipt) : null)
        if (!parsed.success) throw new ReviewRequired("Publication receipt is missing or invalid")
        const value = parsed.data
        if (value.name !== file.name || value.sha256 !== file.sha256 || value.local_path !== file.local_path ||
          value.bytes !== file.bytes || value.bytes > policy.max_file_bytes ||
          entries.some(entry => entry.name === file.name && entry.sha256 !== value.sha256))
          throw new ReviewRequired("Publication receipt differs from frozen delivery file")
        return value
      })
      if (new Set(files.map(file => file.artifact_id)).size !== files.length ||
        new Set(files.map(file => file.local_path)).size !== files.length)
        throw new ReviewRequired("Publication receipts alias another delivered file")
      const project = z.object({ id: z.string() }).parse(await (await this.api.request(task, "/project/current")).json())
      const active = async () => z.array(record).parse(await (await this.api.request(task, "/file/artifact-store")).json())
      const validate = (items: z.infer<typeof record>[]) => files.map(file => {
        const matches = items.filter(item => item.id === file.artifact_id || item.current.sourcePath === file.local_path)
        const item = matches[0]
        if (matches.length !== 1 || !item || file.project_id !== project.id || item.projectID !== project.id ||
          item.id !== file.artifact_id || item.currentVersionID !== file.version_id || item.current.id !== file.version_id ||
          item.current.artifactID !== file.artifact_id || item.current.sessionID !== task.plan.session ||
          item.current.sourcePath !== file.local_path || item.current.sha256 !== file.sha256 || item.current.size !== file.bytes)
          throw new ReviewRequired("Published file is absent or its native version changed")
        return item
      })
      const before = validate(await active())
      for (const file of files) {
        abort.throwIfAborted()
        await this.verifyBytes(task, file.artifact_id, file.version_id, file.sha256, file.bytes, abort)
      }
      const fresh = await this.read(this.store, task, context, id)
      if (!fresh.report_ready || !fresh.report || digest(fresh.report) !== queue.report_digest)
        throw new ReviewRequired("Refinement source changed during publication inspection")
      if (digest(validate(await active())) !== digest(before) || digest(this.get(id)) !== digest(queue) ||
        digest(snapshot()) !== digest(saved) || this.scope(id).workflow.digest !== workflow.digest ||
        z.object({ id: z.string() }).parse(await (await this.api.request(task, "/project/current")).json()).id !== project.id)
        throw new ReviewRequired("Publication identity changed during inspection")
      abort.throwIfAborted()
      return result("verified", files.map(({ local_path, published_at, ...file }) => file))
    } catch (error) {
      abort.throwIfAborted()
      return result(error instanceof ReviewRequired || error instanceof z.ZodError || error instanceof SyntaxError
        ? "evidence_rejected" : "verification_unavailable")
    }
  }
  async verifyBytes(task: Task, artifact: string, version: string, sha256: string, bytes: number, abort?: AbortSignal) {
    abort?.throwIfAborted()
    const response = await this.api.request(task, `/file/artifact-store/${encodeURIComponent(artifact)}/raw?versionID=${encodeURIComponent(version)}`)
    const stream = response.body?.getReader()
    if (!stream) throw new ReviewRequired("Published refinement Result is unreadable")
    const hash = new Bun.CryptoHasher("sha256")
    const size = { value: 0 }
    try {
      while (true) {
        abort?.throwIfAborted()
        const next = await stream.read()
        if (next.done) break
        size.value += next.value.byteLength
        if (size.value > bytes) throw new ReviewRequired("Published refinement Result exceeds witnessed size")
        hash.update(next.value)
      }
    } finally { await stream.cancel(); stream.releaseLock() }
    if (size.value !== bytes || hash.digest("hex") !== sha256) throw new ReviewRequired("Published refinement bytes differ from the original")
  }
  bound(id: string) {
    const row = this.get(id)
    const scope = this.scope(id)
    if (!row || row.digest !== scope.workflow.digest || row.lease_owner !== this.owner || row.lease_until <= Date.now() || row.state !== "ACTIVE") throw new ReviewRequired("Refinement delivery ownership or scope changed")
    return scope
  }
  async publish(id: string, entry: Entry, file: string, bytes: number) {
    const { task } = this.bound(id)
    const before = await checksum(file)
    if (before.sha256 !== entry.sha256 || before.bytes !== bytes) throw new ReviewRequired("Local refinement file changed before publication")
    const project = z.object({ id: z.string() }).parse(await (await this.api.request(task, "/project/current")).json())
    const active = z.array(record).parse(await (await this.api.request(task, "/file/artifact-store")).json())
    const matches = active.filter((item) => item.current.sourcePath === file)
    if (matches.length > 1) throw new ReviewRequired("Ambiguous refinement Result publication")
    const result = matches[0] ?? await (async () => {
      const trash = z.array(z.object({ current: z.object({ sourcePath: z.string() }) })).parse(await (await this.api.request(task, "/file/artifact-store?state=trash")).json())
      if (trash.some((item) => item.current.sourcePath === file)) throw new ReviewRequired("Refinement Result was trashed; do not restore automatically")
      this.bound(id)
      const reserved = this.store.db.query("UPDATE refinement_delivery_files SET attempted=? WHERE workflow_id=? AND name=? AND attempted IS NULL")
        .run(Date.now(), id, entry.name).changes
      if (reserved !== 1) throw new ReviewRequired("Prior Result save is unresolved; reconcile without reposting")
      return record.parse(await (await this.api.request(task, "/file/artifact", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: file, sessionID: task.plan.session, summary: `${id} · refinement_result · ${entry.name}` }) })).json())
    })()
    this.bound(id)
    if (result.projectID !== project.id || result.currentVersionID !== result.current.id || result.current.artifactID !== result.id ||
      result.current.sessionID !== task.plan.session || result.current.sourcePath !== file || result.current.sha256 !== entry.sha256 || result.current.size !== bytes) throw new ReviewRequired("Published refinement Result identity changed")
    await this.verifyBytes(task, result.id, result.current.id, entry.sha256, bytes)
    this.bound(id)
    const saved = receipt.parse({ name: entry.name, artifact_id: result.id, version_id: result.current.id, project_id: result.projectID,
      sha256: entry.sha256, bytes, local_path: file, published_at: Date.now() })
    this.store.db.query("UPDATE refinement_delivery_files SET receipt=? WHERE workflow_id=? AND name=? AND sha256=?")
      .run(JSON.stringify(saved), id, entry.name, entry.sha256)
    return saved
  }
  async copy(id: string, entry: Entry, limit: number, abort: AbortSignal) {
    const { task } = this.bound(id)
    const root = await realpath(task.plan.directory)
    const parts = ["inspire-results", task.id, "refinements", id, entry.name.split("/")[0]]
    for (const index of parts.keys()) {
      const directory = path.join(root, ...parts.slice(0, index + 1))
      await mkdir(directory, { mode: 0o700 }).catch((error: NodeJS.ErrnoException) => { if (error.code !== "EEXIST") throw error })
      if (!(await lstat(directory)).isDirectory() || await realpath(directory) !== directory) throw new ReviewRequired("Refinement destination is redirected")
    }
    const directory = path.join(root, ...parts)
    const file = path.join(directory, `${entry.sha256}-${path.basename(entry.name).replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100)}`)
    const exists = await lstat(file).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; return null })
    if (!exists) {
      const temporary = await mkdtemp(path.join(directory, ".refinement-"))
      const partial = path.join(temporary, "artifact.part")
      try {
        if (entry.text !== null) {
          if (Buffer.byteLength(entry.text) > limit) throw new ReviewRequired("Generated report exceeds approved delivery bytes")
          await Bun.write(partial, entry.text)
        } else await this.transport(task).download(task, entry.remote!, partial, limit)
        abort.throwIfAborted()
        const copied = await checksum(partial)
        if (copied.bytes > limit || copied.sha256 !== entry.sha256) throw new ReviewRequired("Copied refinement file differs from verified inventory or byte limit")
        this.bound(id)
        if (await realpath(directory) !== directory) throw new ReviewRequired("Refinement destination changed during copy")
        await chmod(partial, 0o400)
        await link(partial, file).catch((error: NodeJS.ErrnoException) => { if (error.code !== "EEXIST") throw error })
      } finally {
        await unlink(partial).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error })
        await rmdir(temporary)
      }
    }
    const checked = await checksum(file)
    if (checked.bytes > limit || checked.sha256 !== entry.sha256) throw new ReviewRequired("Existing refinement copy changed; never overwrite")
    return { file, bytes: checked.bytes }
  }
  async tick(id: string, abort: AbortSignal) {
    abort.throwIfAborted()
    const { workflow, task, policy } = this.scope(id)
    const previous = this.get(id)
    if (previous && ["COMPLETE", "PAUSED"].includes(previous.state)) return { worked: false }
    const context = { directory: task.plan.directory, sessionID: task.plan.session, abort }
    const evidence = await this.read(this.store, task, context, id)
    if (!evidence.report_ready || !evidence.report) {
      const diagnostic = { workflow_id: id, verification: evidence.verification,
        reason: "reason" in evidence ? evidence.reason : "Report unavailable", compute_restarted: false }
      const last = this.store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='refinement_delivery_verification_deferred' AND json_extract(payload,'$.workflow_id')=? ORDER BY sequence DESC LIMIT 1")
        .get(task.id, id) as { payload: string } | null
      if (!last || digest(JSON.parse(last.payload)) !== digest(diagnostic)) {
        this.store.event(task.id, "refinement_delivery_verification_deferred", diagnostic)
      }
      return { worked: false }
    }
    const report = evidence.report
    if (report.workflow_id !== id || report.workflow_digest !== workflow.digest || report.parent_digest !== task.digest) throw new ReviewRequired("Delivery report belongs to another workflow")
    const entries = refinementInventory(report)
    const identity = digest(entries.map(({ text, ...entry }) => entry))
    this.store.db.query("INSERT OR IGNORE INTO refinement_delivery(id,digest,report_digest,inventory_digest,state,updated) VALUES(?,?,?,?,'WAITING',?)")
      .run(id, workflow.digest, digest(report), identity, Date.now())
    const acquired = this.store.db.query("UPDATE refinement_delivery SET lease_owner=?,lease_until=?,state='ACTIVE' WHERE id=? AND state IN ('WAITING','ACTIVE') AND lease_until<?")
      .run(this.owner, Date.now() + 180000, id, Date.now()).changes
    if (!acquired) return { worked: false }
    const heartbeat = setInterval(() => this.store.db.query("UPDATE refinement_delivery SET lease_until=? WHERE id=? AND lease_owner=? AND lease_until>?")
      .run(Date.now() + 180000, id, this.owner, Date.now()), 10000)
    try {
      this.bound(id)
      const row = this.get(id)!
      if (row.report_digest !== digest(report) || row.inventory_digest !== identity) throw new ReviewRequired("Frozen refinement delivery inventory changed")
      const derived = policy.candidate_results ? candidateResultNames : []
      if (entries.length + derived.length > policy.max_files) throw new ReviewRequired("Complete refinement inventory exceeds approved file count; no truncation")
      const saved = this.store.db.query("SELECT name,sha256,local_path,bytes,receipt FROM refinement_delivery_files WHERE workflow_id=?")
        .all(id) as { name: string; sha256: string; local_path: string; bytes: number; receipt: string | null }[]
      if (saved.some((file) => (!derived.includes(file.name) && !entries.some((entry) => entry.name === file.name && entry.sha256 === file.sha256)) || file.bytes > policy.max_file_bytes)) throw new ReviewRequired("Stored delivery differs from complete inventory")
      const remaining = { bytes: policy.max_total_bytes - saved.reduce((sum, file) => sum + file.bytes, 0) }
      if (remaining.bytes < 0) throw new ReviewRequired("Stored refinement files exceed approved total bytes")
      for (const entry of entries.filter((entry) => !saved.some((file) => file.name === entry.name && file.receipt)).slice(0, 4)) {
        abort.throwIfAborted()
        const retained = saved.find((file) => file.name === entry.name)
        const file = await this.copy(id, entry, Math.min(policy.max_file_bytes, remaining.bytes + (retained?.bytes ?? 0)), abort)
        this.bound(id)
        if (retained && (retained.local_path !== file.file || retained.bytes !== file.bytes)) throw new ReviewRequired("Saved refinement copy identity changed")
        if (!retained) {
          this.store.db.query("INSERT INTO refinement_delivery_files(workflow_id,name,sha256,local_path,bytes) VALUES(?,?,?,?,?)")
            .run(id, entry.name, entry.sha256, file.file, file.bytes)
          remaining.bytes -= file.bytes
        }
        await this.publish(id, entry, file.file, file.bytes)
      }
      const originals = this.store.db.query('SELECT name,receipt FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL')
        .all(id) as { name: string; receipt: string }[]
      const ready = entries.every((entry) => originals.some((row) => row.name === entry.name))
      if (ready && policy.candidate_results) {
        const sources = originals.filter((row) => !derived.includes(row.name)).map((row) => receipt.parse(JSON.parse(row.receipt)))
        // Re-read native versions before deriving links. A receipt alone is not proof.
        for (const entry of entries) {
          const source = sources.find((source) => source.name === entry.name)!
          await this.publish(id, entry, source.local_path, source.bytes)
        }
        const files = await this.candidates(report, sources, policy.candidate_results.code_sha256, abort)
        if (Object.keys(files).length !== derived.length || derived.some((name) => !(name in files)))
          throw new ReviewRequired('Candidate-results inventory differs from approved policy')
        for (const [name, text] of Object.entries(files)) {
          const entry = { name, text, remote: null, sha256: new Bun.CryptoHasher('sha256').update(text).digest('hex') }
          const retained = saved.find((file) => file.name === name)
          if (retained && retained.sha256 !== entry.sha256) throw new ReviewRequired('Frozen derived candidate result changed')
          const file = await this.copy(id, entry, Math.min(policy.max_file_bytes, remaining.bytes + (retained?.bytes ?? 0)), abort)
          if (retained && (retained.local_path !== file.file || retained.bytes !== file.bytes)) throw new ReviewRequired('Derived copy identity changed')
          if (!retained) {
            this.store.db.query('INSERT INTO refinement_delivery_files(workflow_id,name,sha256,local_path,bytes) VALUES(?,?,?,?,?)')
              .run(id, name, entry.sha256, file.file, file.bytes)
            remaining.bytes -= file.bytes
          }
          await this.publish(id, entry, file.file, file.bytes)
          entries.push(entry)
        }
      }
      const count = this.store.db.query("SELECT COUNT(*) AS count FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL")
        .get(id) as { count: number }
      const complete = ready && count.count === entries.length && (!policy.candidate_results || entries.some((entry) => entry.name === candidateResultNames[0]))
      if (complete) {
        // A previous tick's receipt is not proof that the file still exists.
        // Reconcile exact native versions (no blind repost) and hash their bytes.
        const retained = this.store.db.query("SELECT name,local_path,bytes FROM refinement_delivery_files WHERE workflow_id=?").all(id) as
          { name: string; local_path: string; bytes: number }[]
        for (const entry of entries) {
          abort.throwIfAborted()
          const file = retained.find((item) => item.name === entry.name)!
          await this.publish(id, entry, file.local_path, file.bytes)
        }
        const fresh = await this.read(this.store, task, context, id)
        if (!fresh.report_ready || !fresh.report || digest(fresh.report) !== digest(report)) throw new ReviewRequired("Refinement source changed before delivery completion")
      }
      this.bound(id)
      const state = complete ? "COMPLETE" : "WAITING"
      this.store.db.query("UPDATE refinement_delivery SET state=?,error=NULL,updated=? WHERE id=? AND lease_owner=?")
        .run(state, Date.now(), id, this.owner)
      this.store.event(task.id, "refinement_delivery_progress", { workflow_id: id, state, completed_files: count.count,
        total_files: entries.length, total_bytes: policy.max_total_bytes - remaining.bytes })
      return { worked: true }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Refinement delivery unavailable"
      this.store.db.query("UPDATE refinement_delivery SET state='PAUSED',error=?,updated=? WHERE id=? AND lease_owner=?")
        .run(message, Date.now(), id, this.owner)
      this.store.event(task.id, "refinement_delivery_paused", { workflow_id: id, reason: message, compute_restarted: false })
      return { worked: false }
    } finally {
      clearInterval(heartbeat)
      this.store.db.query("UPDATE refinement_delivery SET lease_owner=NULL,lease_until=0 WHERE id=? AND lease_owner=?").run(id, this.owner)
    }
  }
  resume(id: string, task: Task) {
    const { workflow } = this.scope(id)
    if (workflow.plan.stages[0].parent_id !== task.id || workflow.plan.stages[0].directory !== task.plan.directory ||
      workflow.plan.stages[0].session !== task.plan.session) throw new ReviewRequired("Delivery recovery belongs to another task")
    this.store.db.query("UPDATE refinement_delivery SET state='WAITING',error=NULL,updated=? WHERE id=? AND state='PAUSED' AND lease_until<?")
      .run(Date.now(), id, Date.now())
    // Never clear publication attempts, byte reservations or compute history.
  }
}

export function approvedRefinementDeliveries(store: Store) {
  if (!store.db.query("SELECT name FROM sqlite_master WHERE name='refinement_workflows'").get()) return []
  return (store.db.query("SELECT id FROM refinement_workflows WHERE state='APPROVED' AND json_extract(plan,'$.delivery.mode')='all-refinement-artifacts-v1'")
    .all() as { id: string }[]).map((row) => row.id)
}

export async function deliverRefinements(store: Store, abort: AbortSignal) {
  const ids = approvedRefinementDeliveries(store)
  if (!ids.length) return false
  const queue = new RefinementDelivery(store)
  const activity = { worked: false }
  for (const id of ids) {
    if (abort.aborted) break
    await queue.tick(id, abort).then((result) => { activity.worked ||= result.worked }, (error: unknown) => {
      const workflow = new RefinementWorkflows(new Refinements(store)).get(id)
      const diagnostic = { workflow_id: id, reason: error instanceof Error ? error.message : "Delivery inspection failed", compute_restarted: false }
      const task = workflow.plan.stages[0].parent_id
      const last = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='refinement_delivery_inspection_error' AND json_extract(payload,'$.workflow_id')=? ORDER BY sequence DESC LIMIT 1")
        .get(task, id) as { payload: string } | null
      if (!last || digest(JSON.parse(last.payload)) !== digest(diagnostic)) store.event(task, "refinement_delivery_inspection_error", diagnostic)
    })
  }
  return activity.worked
}
