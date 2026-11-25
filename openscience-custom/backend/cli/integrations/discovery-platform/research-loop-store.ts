import { Database } from "bun:sqlite"
import { randomUUID } from "node:crypto"
import { researchContract, researchDigest, researchReservation, researchResources, researchState, researchOutcome,
  emptyResources, resourceKeys, exceedsResearchLimit, type ResearchContract, type ResearchResources, type ResearchState, type ResearchOutcome } from "./research-loop-contract"

type Row = { id: string; contract: string; digest: string; revision: number; state: ResearchState;
  outcome: ResearchOutcome | null; created: number; updated: number }
type Reservation = { loop: string; key: string; payload: string; digest: string; state: "RESERVED" | "SETTLED";
  actual: string | null; receipt: string | null; task: string | null }
type Evidence = { loop: string; key: string; kind: "observation" | "decision" | "host"; payload: string; digest: string; revision: number }
type Submission = { loop: string; key: string; state: "CLAIMED"; plan_digest: string; created: number }

/** Host-owned durable state. None of the mutators is a model-callable approval API. */
export class ResearchLoopStore {
  readonly db: Database
  constructor(filename: string) {
    this.db = new Database(filename, { create: true, strict: true })
    this.db.exec(`PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS research_loops(id TEXT PRIMARY KEY, contract TEXT NOT NULL, digest TEXT NOT NULL,
        revision INTEGER NOT NULL, state TEXT NOT NULL, outcome TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS research_evidence(loop TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(loop,key));
      CREATE TABLE IF NOT EXISTS research_reservations(loop TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
        digest TEXT NOT NULL, state TEXT NOT NULL, actual TEXT, receipt TEXT, task TEXT, PRIMARY KEY(loop,key));
      CREATE TABLE IF NOT EXISTS research_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, loop TEXT NOT NULL,
        at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, previous TEXT, digest TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS research_submissions(loop TEXT NOT NULL, key TEXT NOT NULL,
        state TEXT NOT NULL, plan_digest TEXT NOT NULL, created INTEGER NOT NULL, PRIMARY KEY(loop,key));`)
  }
  close() { this.db.close() }
  create(input: unknown, now = Date.now()) {
    const contract = researchContract.parse(input)
    const id = "loop-" + randomUUID()
    this.db.transaction(() => {
      this.db.query("INSERT INTO research_loops VALUES(?,?,?,0,'READY',NULL,?,?)")
        .run(id, JSON.stringify(contract), researchDigest(contract), now, now)
      this.event(id, "created", { contract_digest: researchDigest(contract) }, now)
    }).immediate()
    return this.get(id)
  }
  get(id: string) {
    return this.db.transaction(() => {
      const row = this.db.query("SELECT * FROM research_loops WHERE id=?").get(id) as Row | null
      if (!row) throw Error("Unknown research loop")
      const contract = researchContract.parse(JSON.parse(row.contract))
      if (researchDigest(contract) !== row.digest) throw Error("Immutable research contract changed")
      researchState.parse(row.state)
      if (row.outcome !== null) researchOutcome.parse(row.outcome)
      return { ...row, contract, ledger: this.ledger(id) }
    }).deferred()
  }
  list(directory: string) {
    return (this.db.query("SELECT id FROM research_loops WHERE json_extract(contract,'$.directory')=? ORDER BY created DESC")
      .all(directory) as { id: string }[]).map(row => this.get(row.id))
  }
  private event(id: string, kind: string, payload: unknown, now = Date.now()) {
    const previous = (this.db.query("SELECT digest FROM research_events WHERE loop=? ORDER BY sequence DESC LIMIT 1")
      .get(id) as { digest: string } | null)?.digest ?? null
    const event = { loop: id, at: now, kind, payload, previous }
    this.db.query("INSERT INTO research_events(loop,at,kind,payload,previous,digest) VALUES(?,?,?,?,?,?)")
      .run(id, now, kind, JSON.stringify(payload), previous, researchDigest(event))
  }
  events(id: string) {
    const rows = this.db.query("SELECT * FROM research_events WHERE loop=? ORDER BY sequence").all(id) as
      { sequence: number; loop: string; at: number; kind: string; payload: string; previous: string | null; digest: string }[]
    return rows.map((row, index) => {
      const payload: unknown = JSON.parse(row.payload)
      if (row.previous !== (rows[index - 1]?.digest ?? null) ||
        row.digest !== researchDigest({ loop: id, at: row.at, kind: row.kind, payload, previous: row.previous }))
        throw Error("Research event history integrity failure")
      return { ...row, payload }
    })
  }
  evidence(id: string, kind?: Evidence["kind"]) {
    return this.db.transaction(() => {
      this.getContract(id)
      const rows = (kind ? this.db.query("SELECT * FROM research_evidence WHERE loop=? AND kind=? ORDER BY revision").all(id, kind)
        : this.db.query("SELECT * FROM research_evidence WHERE loop=? ORDER BY revision").all(id)) as Evidence[]
      const events = this.events(id)
      return rows.map(row => {
        const payload: unknown = JSON.parse(row.payload)
        if (researchDigest(payload) !== row.digest) throw Error("Research evidence changed")
        const expected = { observation: "observation", decision: "decision", host: "host_evidence" }[row.kind]
        if (!expected || !events.some(event => {
          const data = event.payload as { key?: unknown; sha256?: unknown }
          return event.kind === expected && data.key === row.key && data.sha256 === row.digest
        })) throw Error("Research evidence identity has no matching immutable event")
        return { ...row, payload }
      })
    }).deferred()
  }
  private getContract(id: string) {
    const row = this.db.query("SELECT contract,digest FROM research_loops WHERE id=?").get(id) as Pick<Row, "contract" | "digest"> | null
    if (!row) throw Error("Unknown research loop")
    const contract = researchContract.parse(JSON.parse(row.contract))
    if (researchDigest(contract) !== row.digest) throw Error("Immutable research contract changed")
    return contract
  }
  private active(id: string, revision: number, now = Date.now(), checkDeadline = true) {
    const current = this.get(id)
    if (current.revision !== revision) throw Error("Stale research state revision")
    if (["FINISHED", "CANCELLED"].includes(current.state)) throw Error("Research loop is terminal")
    if (checkDeadline && now >= current.created + current.contract.limits.max_elapsed_ms) throw Error("Research wall-clock budget exhausted")
    return current
  }
  private bump(id: string, state: ResearchState, now = Date.now()) {
    this.db.query("UPDATE research_loops SET revision=revision+1,state=?,updated=? WHERE id=?").run(state, now, id)
  }
  observe(id: string, key: string, payload: unknown, now = Date.now()) {
    if (!key.trim()) throw Error("Observation needs an evidence identity")
    return this.db.transaction(() => {
      const current = this.get(id)
      const previous = this.db.query("SELECT * FROM research_evidence WHERE loop=? AND key=?").get(id, key) as Evidence | null
      if (previous) {
        if (previous.kind !== "observation" || previous.digest !== researchDigest(payload)) throw Error("Observation identity reused for different evidence")
        return current
      }
      // Late completion remains auditable, but cannot revive a cancelled or finished loop.
      this.db.query("INSERT INTO research_evidence VALUES(?,?,'observation',?,?,?)")
        .run(id, key, JSON.stringify(payload), researchDigest(payload), current.revision + 1)
      this.bump(id, ["FINISHED", "CANCELLED"].includes(current.state) ? current.state : "DECIDING", now)
      this.event(id, "observation", { key, sha256: researchDigest(payload) }, now)
      return this.get(id)
    }).immediate()
  }
  /** Host receipts and plans are evidence, but never scientific observations. */
  record(id: string, key: string, payload: unknown, now = Date.now()) {
    if (!key.trim()) throw Error("Host evidence needs an identity")
    return this.db.transaction(() => {
      const current = this.get(id)
      const previous = this.db.query("SELECT * FROM research_evidence WHERE loop=? AND key=?").get(id, key) as Evidence | null
      if (previous) {
        if (previous.kind !== "host" || previous.digest !== researchDigest(payload)) throw Error("Host evidence identity reused for different evidence")
        return current
      }
      this.db.query("INSERT INTO research_evidence VALUES(?,?,'host',?,?,?)")
        .run(id, key, JSON.stringify(payload), researchDigest(payload), current.revision + 1)
      this.bump(id, current.state, now)
      this.event(id, "host_evidence", { key, sha256: researchDigest(payload) }, now)
      return this.get(id)
    }).immediate()
  }
  decide(id: string, revision: number, payload: unknown, now = Date.now()) {
    return this.db.transaction(() => {
      const current = this.active(id, revision, now, false)
      if (this.evidence(id, "decision").length >= current.contract.limits.max_decisions)
        throw Error("Research decision budget exhausted")
      const key = "decision-" + researchDigest({ id, revision, payload })
      this.db.query("INSERT INTO research_evidence VALUES(?,?,'decision',?,?,?)")
        .run(id, key, JSON.stringify(payload), researchDigest(payload), revision + 1)
      this.bump(id, "DECIDING", now)
      this.event(id, "decision", { key, based_on_revision: revision, sha256: researchDigest(payload) }, now)
      return { decision_id: key, loop: this.get(id) }
    }).immediate()
  }
  transition(id: string, revision: number, state: ResearchState, detail: unknown, now = Date.now()) {
    return this.db.transaction(() => {
      this.active(id, revision, now, false)
      if (["FINISHED", "CANCELLED"].includes(state)) throw Error("Use finish with an explicit outcome")
      this.bump(id, researchState.parse(state), now)
      this.event(id, "state", { state, detail }, now)
      return this.get(id)
    }).immediate()
  }
  reservations(id: string) {
    return this.db.transaction(() => {
      this.getContract(id)
      const events = this.events(id)
      return (this.db.query("SELECT * FROM research_reservations WHERE loop=? ORDER BY rowid").all(id) as Reservation[]).map(row => {
        const payload = researchReservation.parse(JSON.parse(row.payload))
        if (researchDigest(payload) !== row.digest || payload.key !== row.key) throw Error("Resource reservation changed")
        if (!events.some(event => event.kind === "resources_reserved" && researchDigest(event.payload) === row.digest))
          throw Error("Resource reservation has no matching immutable event")
        const actual = row.actual === null ? null : researchResources.parse(JSON.parse(row.actual))
        if (!["RESERVED", "SETTLED"].includes(row.state) ||
          (row.state === "RESERVED" && (actual !== null || row.receipt !== null)) ||
          (row.state === "SETTLED" && (actual === null || !row.receipt?.trim())))
          throw Error("Resource settlement state is inconsistent")
        if (row.state === "SETTLED" && !events.some(event => {
          const data = event.payload as { key?: unknown; actual?: unknown; receipt?: unknown }
          return event.kind === "resources_settled" && data.key === row.key && data.receipt === row.receipt
            && researchDigest(data.actual) === researchDigest(actual)
        })) throw Error("Resource settlement has no matching immutable receipt event")
        if (row.task !== null && !events.some(event => {
          const data = event.payload as { key?: unknown; task?: unknown; plan_digest?: unknown }
          return event.kind === "task_bound" && data.key === row.key && data.task === row.task && data.plan_digest === payload.plan_digest
        })) throw Error("Resource task binding has no matching immutable event")
        return { ...row, payload, actual }
      })
    }).deferred()
  }
  ledger(id: string) {
    const rows = this.reservations(id)
    const reserved = emptyResources()
    const used = emptyResources()
    for (const row of rows) for (const key of resourceKeys)
      (row.state === "SETTLED" ? used : reserved)[key] += (row.actual ?? row.payload.resources)[key]
    return { reserved, used, total: Object.fromEntries(resourceKeys.map(key => [key, reserved[key] + used[key]])) as ResearchResources,
      unsettled: rows.filter(row => row.state === "RESERVED").map(row => row.key),
      ...(this.getContract(id).limits.resources.model_cost === null ? {
        model_cost_accounting: { limit: "unbounded" as const, total_cost: null, currency: null,
          numeric_model_cost_semantics: "priced-subtotal-only; not the total bill",
          unpriced_calls: rows.filter(row => row.payload.kind === "model").map(row => row.key) },
      } : {}) }
  }
  reserve(id: string, revision: number, input: unknown, now = Date.now()) {
    const reservation = researchReservation.parse(input)
    return this.db.transaction(() => {
      const prior = this.reservations(id).find(row => row.key === reservation.key)
      if (prior) {
        if (prior.digest !== researchDigest(reservation)) throw Error("Reservation key reused with changed plan")
        return prior
      }
      const current = this.active(id, revision, now)
      if (current.state === "BLOCKED") throw Error("Blocked loop cannot reserve resources")
      const contract = current.contract
      if (this.reservations(id).some(row => row.actual && resourceKeys.some(key => row.actual![key] > row.payload.resources[key] + 1e-9)))
        throw Error("An actual resource usage breach must stop further reservations")
      if (reservation.source_release !== contract.inputs.source_release) throw Error("Reservation source release differs from contract")
      for (const key of resourceKeys)
        if (exceedsResearchLimit(current.ledger.total[key] + reservation.resources[key], contract.limits.resources[key]))
          throw Error("Cumulative research budget exceeded: " + key)
      const outstanding = this.reservations(id).filter(row => row.state === "RESERVED")
      if (outstanding.some(row => row.payload.kind === "evaluation"))
        throw Error("Previous scientific evaluation is running or its resource usage is unsettled")
      if (reservation.kind === "model") {
        const model = this.reservations(id).filter(row => row.payload.kind === "model")
        if (model.some(row => row.state === "RESERVED")) throw Error("Previous model call is running or its resource usage is unsettled")
        if (model.length >= contract.limits.max_decisions) throw Error("Research model decision budget exhausted")
      }
      if (reservation.kind === "evaluation") {
        if (contract.mode !== "native-compute") throw Error("Offline replay cannot reserve scientific evaluation")
        if (!contract.allowed_adaptations.includes("evaluate")) throw Error("Scientific evaluation is not allowed by this research contract")
        if (outstanding.some(row => row.payload.kind === "model"))
          throw Error("Previous model call is running or its resource usage is unsettled")
        // A completed native task may still have an unknown platform bill.
        // Its reservation remains charged while its delivered files are evaluated.
        // Native completion and immutable Results are checked by the evaluator adapter.
      }
      if (reservation.kind === "compute") {
        if (contract.mode !== "native-compute") throw Error("Offline replay cannot reserve scientific computation")
        const compute = this.reservations(id).filter(row => row.payload.kind === "compute")
        if (compute.some(row => row.state === "RESERVED")) throw Error("Previous compute is running or its resource usage is unsettled")
        if (reservation.round !== compute.length + 1 || reservation.round > contract.limits.max_rounds)
          throw Error("Invalid or exhausted research round")
        if (reservation.candidate_slots > contract.limits.max_candidates_per_round ||
          compute.reduce((total, row) => total + row.payload.candidate_slots, 0) + reservation.candidate_slots > contract.limits.max_total_candidates)
          throw Error("Cumulative candidate limit exceeded")
        if (reservation.resources.gpu_hours > contract.limits.max_gpu_hours_per_round + 1e-9)
          throw Error("Per-round GPU budget exceeded")
      }
      this.db.query("INSERT INTO research_reservations VALUES(?,?,?,?,'RESERVED',NULL,NULL,NULL)")
        .run(id, reservation.key, JSON.stringify(reservation), researchDigest(reservation))
      this.bump(id, current.state, now)
      this.event(id, "resources_reserved", reservation, now)
      return this.reservations(id).find(row => row.key === reservation.key)!
    }).immediate()
  }
  bindTask(id: string, key: string, task: string, planDigest: string) {
    if (!task.trim()) throw Error("Task binding requires a task identity")
    return this.db.transaction(() => {
      this.getContract(id)
      const rows = this.reservations(id)
      const row = rows.find(row => row.key === key)
      if (!row || row.payload.kind !== "compute" || row.payload.plan_digest !== planDigest) throw Error("Task has no matching frozen resource reservation")
      if (row.task && row.task !== task) throw Error("Reservation already binds another task")
      if (rows.some(other => other.key !== key && other.task === task)) throw Error("Task already binds another reservation")
      if (!row.task) {
        this.db.query("UPDATE research_reservations SET task=? WHERE loop=? AND key=?").run(task, id, key)
        this.event(id, "task_bound", { key, task, plan_digest: planDigest })
      }
      return this.reservations(id).find(row => row.key === key)!
    }).immediate()
  }
  /** Only the first durable claimant may invoke submission. A prior claim with
   * no task/receipt means uncertain submission, not permission to retry. The
   * caller must reconcile that original native task before taking further action. */
  claimSubmission(id: string, key: string, revision: number, planDigest: string, now = Date.now()) {
    return this.db.transaction(() => {
      const reservation = this.reservations(id).find(row => row.key === key)
      if (!reservation || reservation.payload.kind !== "compute" || reservation.payload.plan_digest !== planDigest)
        throw Error("Submission has no matching frozen resource reservation")
      const prior = this.db.query("SELECT * FROM research_submissions WHERE loop=? AND key=?").get(id, key) as Submission | null
      if (prior) {
        if (prior.state !== "CLAIMED" || prior.plan_digest !== planDigest || !this.events(id).some(event =>
          event.kind === "submission_claimed" && researchDigest(event.payload) === researchDigest(prior)))
          throw Error("Submission claim integrity failure")
        return { claimed: false, submission: prior, reservation }
      }
      const current = this.active(id, revision, now)
      if (current.state === "BLOCKED" || reservation.state !== "RESERVED") throw Error("Submission requires an active unsettled reservation")
      if (resourceKeys.some(name => exceedsResearchLimit(current.ledger.total[name], current.contract.limits.resources[name])) ||
        this.reservations(id).some(row => row.actual && resourceKeys.some(name => row.actual![name] > row.payload.resources[name] + 1e-9)))
        throw Error("A resource usage breach prevents submission")
      const submission: Submission = { loop: id, key, state: "CLAIMED", plan_digest: planDigest, created: now }
      this.db.query("INSERT INTO research_submissions VALUES(?,?,?,?,?)").run(id, key, "CLAIMED", planDigest, now)
      this.bump(id, current.state, now)
      this.event(id, "submission_claimed", submission, now)
      return { claimed: true, submission, reservation }
    }).immediate()
  }
  /** Actual usage comes from a host-verified receipt, never from a model proposal. */
  settle(id: string, key: string, input: unknown, receipt: string, now = Date.now()) {
    const actual = researchResources.parse(input)
    if (!receipt.trim()) throw Error("Resource settlement needs a verified receipt reference")
    return this.db.transaction(() => {
      const current = this.get(id)
      const row = this.reservations(id).find(row => row.key === key)
      if (!row) throw Error("No matching resource reservation")
      if (row.state === "SETTLED") {
        if (researchDigest(row.actual) !== researchDigest(actual) || row.receipt !== receipt) throw Error("Resource settlement is immutable")
        return current
      }
      const breached = resourceKeys.filter(name => actual[name] > row.payload.resources[name] + 1e-9 ||
        exceedsResearchLimit(current.ledger.total[name] - row.payload.resources[name] + actual[name], current.contract.limits.resources[name]))
      this.db.query("UPDATE research_reservations SET state='SETTLED',actual=?,receipt=? WHERE loop=? AND key=?")
        .run(JSON.stringify(actual), receipt, id, key)
      this.bump(id, ["FINISHED", "CANCELLED"].includes(current.state) ? current.state : breached.length ? "BLOCKED" : current.state, now)
      this.event(id, "resources_settled", { key, actual, receipt, breached }, now)
      return this.get(id)
    }).immediate()
  }
  finish(id: string, revision: number, outcome: ResearchOutcome, evidence: string[], now = Date.now()) {
    return this.db.transaction(() => {
      this.active(id, revision, now, false)
      researchOutcome.parse(outcome)
      const known = new Set(this.evidence(id).map(row => row.key))
      if (evidence.some(key => !known.has(key))) throw Error("Unknown completion evidence")
      if (outcome === "objective_met" && !evidence.length) throw Error("Completion requires evidence")
      if (outcome === "objective_met" && this.reservations(id).some(row => row.payload.kind !== "model" && row.state === "RESERVED"))
        throw Error("Objective completion requires settled scientific computation")
      const state = outcome === "user_cancelled" ? "CANCELLED" : "FINISHED"
      this.db.query("UPDATE research_loops SET revision=revision+1,state=?,outcome=?,updated=? WHERE id=?")
        .run(state, outcome, now, id)
      this.event(id, "finished", { outcome, evidence, unsettled: this.ledger(id).unsettled }, now)
      return this.get(id)
    }).immediate()
  }
}
