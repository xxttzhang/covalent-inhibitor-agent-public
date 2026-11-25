import { Database } from "bun:sqlite"
import { randomBytes } from "node:crypto"
import { digest, planSchema, type Plan, type Stage, type State, type Task } from "./model"

export class Store {
  readonly db: Database
  constructor(filename: string) {
    this.db = new Database(filename, { create: true, strict: true })
    this.db.exec(`PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, plan TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL, approval TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL,
        lease_owner TEXT, lease_until INTEGER NOT NULL DEFAULT 0, error TEXT);
      CREATE TABLE IF NOT EXISTS stages(task_id TEXT NOT NULL, ordinal INTEGER NOT NULL, phase TEXT NOT NULL,
        name TEXT UNIQUE NOT NULL, state TEXT NOT NULL, handle TEXT, manifest TEXT, error TEXT, submitted INTEGER,
        PRIMARY KEY(task_id,ordinal));
      CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS daemon(key TEXT PRIMARY KEY, pid INTEGER, heartbeat INTEGER);`)
    this.db.exec(`CREATE TABLE IF NOT EXISTS artifact_publications(task_id TEXT NOT NULL, phase TEXT NOT NULL,
      name TEXT NOT NULL, plan_digest TEXT NOT NULL, receipt TEXT,
      PRIMARY KEY(task_id,phase,name));`)
    this.db.exec(`CREATE TABLE IF NOT EXISTS deliveries(task_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL,
      inventory_digest TEXT NOT NULL, inventory TEXT NOT NULL, state TEXT NOT NULL,
      total_files INTEGER NOT NULL, completed_files INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
      error TEXT, retry_after INTEGER, owner TEXT, lease_until INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL);`)
    this.db.exec(`CREATE TABLE IF NOT EXISTS intakes(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, directory TEXT NOT NULL,
      user_message_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, origin TEXT NOT NULL, questions TEXT NOT NULL,
      state TEXT NOT NULL, created INTEGER NOT NULL, answers TEXT, resolved TEXT, resolved_digest TEXT, task_id TEXT,
      UNIQUE(session_id,user_message_id,request_sha256));`)
  }
  close() { this.db.close() }
  event(id: string, kind: string, payload: unknown, now = Date.now()) {
    this.db.query("INSERT INTO events(task_id,at,kind,payload) VALUES(?,?,?,?)").run(id, now, kind, JSON.stringify(payload))
  }
  create(value: Plan) {
    const plan = planSchema.parse(value)
    this.db.transaction(() => {
      this.db.query("INSERT INTO tasks(id,plan,digest,state,created,updated) VALUES(?,?,?,'DRAFT',?,?)")
        .run(plan.id, JSON.stringify(plan), digest(plan), plan.created, plan.created)
      plan.steps.forEach((step, ordinal) => this.db.query("INSERT INTO stages(task_id,ordinal,phase,name,state) VALUES(?,?,?,?,'WAITING')")
        .run(plan.id, ordinal, step.phase, `${plan.id}-${step.phase}`))
      this.event(plan.id, "created", { digest: digest(plan), request: plan.request })
    })()
    return this.get(plan.id)
  }
  get(id: string): Task {
    const row = this.db.query("SELECT * FROM tasks WHERE id=?").get(id) as Omit<Task, "plan"> & { plan: string } | null
    if (!row) throw new Error("Unknown screening task")
    const plan = planSchema.parse(JSON.parse(row.plan))
    if (digest(plan) !== row.digest || plan.id !== row.id) throw new Error("Stored immutable plan has changed")
    return { ...row, plan }
  }
  list(directory?: string) {
    // Never hide an older running task behind the newest 200 drafts.
    const ids = (directory
      ? this.db.query("SELECT id FROM tasks WHERE json_extract(plan,'$.directory')=? ORDER BY created DESC").all(directory)
      : this.db.query("SELECT id FROM tasks ORDER BY created DESC").all()) as { id: string }[]
    return ids.map((row) => this.get(row.id))
  }
  stages(id: string) { return this.db.query("SELECT * FROM stages WHERE task_id=? ORDER BY ordinal").all(id) as Stage[] }
  events(id: string) { return this.db.query("SELECT * FROM events WHERE task_id=? ORDER BY sequence").all(id) }
  // Called by trusted host code ONLY AFTER PermissionNext / context.ask resolves.
  // This is intentionally not part of the public model tool argument schema.
  approve(id: string, expected: string, session: string, directory: string, now = Date.now()) {
    return this.db.transaction(() => {
      const task = this.get(id)
      if (task.state !== "DRAFT" || task.digest !== expected) throw new Error("Only an unchanged draft can be approved")
      if (task.plan.session !== session || task.plan.directory !== directory) throw new Error("Approval belongs to another conversation/project")
      if (now > task.plan.approve_before) throw new Error("Resource plan expired before approval")
      const receipt = JSON.stringify({ digest: expected, session, at: now, boundary: "remote_compute", nonce: randomBytes(16).toString("hex") })
      this.db.query("UPDATE tasks SET state='APPROVED',approval=?,updated=? WHERE id=?").run(receipt, now, id)
      this.event(id, "approved", JSON.parse(receipt), now)
      return this.get(id)
    })()
  }
  acquire(id: string, owner: string, now = Date.now()) {
    return this.db.query("UPDATE tasks SET lease_owner=?,lease_until=? WHERE id=? AND (lease_until<? OR lease_owner=?)")
      .run(owner, now + 180000, id, now, owner).changes === 1
  }
  endApproval(id: string, expected: string, session: string, directory: string, reason: string) {
    return this.db.transaction(() => {
      const task = this.get(id)
      if (task.digest !== expected || task.plan.session !== session || task.plan.directory !== directory) {
        throw new Error("Interrupted approval belongs to another plan/conversation/project")
      }
      // Never roll back a separately completed approval or a running task.
      if (task.approval || !["DRAFT", "CANCEL_REQUESTED"].includes(task.state)) return task
      if (this.stages(id).some((stage) => stage.submitted !== null || stage.handle !== null)) {
        throw new Error("Unapproved task unexpectedly has submitted stages")
      }
      this.state(id, "CANCELLED", reason)
      this.event(id, "approval_ended_without_grant", { digest: expected, reason })
      return this.get(id)
    })()
  }
  release(id: string, owner: string) {
    this.db.query("UPDATE tasks SET lease_owner=NULL,lease_until=0 WHERE id=? AND lease_owner=?").run(id, owner)
  }
  state(id: string, state: State, error: string | null = null) {
    this.db.transaction(() => {
      this.db.query("UPDATE tasks SET state=?,error=?,updated=? WHERE id=?").run(state, error, Date.now(), id)
      this.event(id, "task_state", { state, error })
    })()
  }
  stage(id: string, ordinal: number, change: Partial<Pick<Stage, "state" | "handle" | "manifest" | "error" | "submitted">>) {
    const allowed = new Set(["state", "handle", "manifest", "error", "submitted"])
    if (Object.keys(change).some((key) => !allowed.has(key))) throw new Error("Invalid stage mutation")
    const entries = Object.entries(change)
    this.db.transaction(() => {
      this.db.query(`UPDATE stages SET ${entries.map(([key]) => `${key}=?`).join(",")} WHERE task_id=? AND ordinal=?`)
        .run(...entries.map(([, value]) => value!), id, ordinal)
      this.event(id, "stage_state", { ordinal, ...change })
    })()
  }
  claim(id: string, ordinal: number, owner: string) {
    return this.db.transaction(() => {
      const task = this.get(id)
      if (!["APPROVED", "ACTIVE"].includes(task.state) || !task.approval || task.lease_owner !== owner || task.lease_until <= Date.now()) throw new Error("No valid task approval/lease")
      if (JSON.parse(task.approval).digest !== task.digest) throw new Error("Approval digest mismatch")
      const stages = this.stages(id)
      if (stages.slice(0, ordinal).some((stage) => stage.state !== "SUCCEEDED" || !stage.manifest)) throw new Error("Upstream stage is not verified")
      const changed = this.db.query("UPDATE stages SET state='DISPATCHING',submitted=? WHERE task_id=? AND ordinal=? AND state='WAITING'")
        .run(Date.now(), id, ordinal).changes
      if (changed !== 1) throw new Error("Stage already dispatched; never repeat create")
      this.db.query("UPDATE tasks SET state='ACTIVE',updated=? WHERE id=?").run(Date.now(), id)
      this.event(id, "dispatch_reserved", { ordinal, name: stages[ordinal].name })
    })()
  }
  cancel(id: string, directory: string) {
    const task = this.get(id)
    if (task.plan.directory !== directory) throw new Error("Task belongs to another project")
    if (["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.state)) return
    this.state(id, "CANCEL_REQUESTED")
  }
}
