import z from "zod"
import { digest, verifyOutcome, type Outcome, type Stage, type Task } from "./model"
import { ReviewRequired, type Transport } from "./runner"
import type { Store } from "./store"

const file = z.object({ sha256: z.string().regex(/^[a-f0-9]{64}$/),
  bytes: z.number().int().nonnegative().max(128 * 1024 * 1024) }).strict()
export const failureEvidence = z.object({ version: z.literal("screening-failure-evidence-v1"),
  task_id: z.string(), plan_digest: z.string().regex(/^[a-f0-9]{64}$/),
  phase: z.enum(["prepare", "carsidock", "rtmscore", "report"]), status: z.literal("FAILED"),
  started_at: z.number().finite().nonnegative(), runtime_seconds: z.number().finite().nonnegative(),
  artifacts: z.object({ "error.json": file, "started.json": file,
    "environment.json": file.optional(), "engine.log": file.optional(), "failures.csv": file.optional(),
    "zero-candidates.json": file.optional(), "stage2_selection_manifest.json": file.optional() }).strict(),
  scientific_quality_verified: z.literal(false), contents_included: z.literal(false),
}).strict()
type TransportFailure = Pick<Transport, "verify"> & {
  failure(task: Task, stage: Stage, witness?: string): Promise<unknown>
}

function chain(task: Task, stages: Stage[]) {
  if (task.state !== "FAILED" || digest(task.plan) !== task.digest || !task.approval ||
      JSON.parse(task.approval).digest !== task.digest || stages.length !== task.plan.steps.length) {
    throw new ReviewRequired("Failure evidence requires an unchanged approved failed core task")
  }
  const index = stages.findIndex((stage) => stage.state !== "SUCCEEDED")
  const failed = stages[index]
  if (!failed || failed.state !== "FAILED" || failed.manifest !== null || failed.submitted === null ||
      stages.some((stage, ordinal) => stage.task_id !== task.id || stage.ordinal !== ordinal ||
        stage.phase !== task.plan.steps[ordinal].phase || stage.name !== `${task.id}-${stage.phase}`) ||
      stages.slice(index + 1).some((stage) => stage.state !== "WAITING" || stage.submitted !== null ||
        stage.handle !== null || stage.manifest !== null)) {
    throw new ReviewRequired("Failure chain must stop at one reserved failed stage with untouched downstream phases")
  }
  const upstream = stages.slice(0, index).reduce<Outcome[]>((saved, stage) => {
    if (!stage.manifest) throw new ReviewRequired("Verified upstream manifest is missing")
    return [...saved, verifyOutcome(JSON.parse(stage.manifest), task, stage, saved.at(-1) ?? null)]
  }, [])
  return { failed, upstream }
}

export function verifyFailureEvidence(value: unknown, task: Task, stages: Stage[]) {
  const { failed } = chain(task, stages)
  const result = failureEvidence.parse(value)
  if (result.task_id !== task.id || result.plan_digest !== task.digest || result.phase !== failed.phase) {
    throw new ReviewRequired("Failure files belong to another task, plan or phase")
  }
  if (result.artifacts["error.json"].bytes > 65536 || result.artifacts["started.json"].bytes > 65536 ||
      result.artifacts["error.json"].bytes === 0 || result.artifacts["started.json"].bytes === 0) {
    throw new ReviewRequired("Failure identity files must be nonempty bounded JSON")
  }
  const zero = result.artifacts["zero-candidates.json"]
  const selection = result.artifacts["stage2_selection_manifest.json"]
  if (!!zero !== !!selection || ((zero || selection) && result.phase !== "prepare") ||
      (zero && (zero.bytes === 0 || zero.bytes > 65536)) ||
      (selection && (selection.bytes === 0 || selection.bytes > 1024 * 1024))) {
    throw new ReviewRequired("Zero-selection evidence requires its paired bounded preparation diagnostic and source manifest")
  }
  return result
}

// Local inventory is not a fresh remote read. Transfer/finalization must also
// call readFailureReport; this binding is reused by native Result publication.
export function failureInventory(store: Store, task: Task) {
  const current = store.get(task.id)
  if (current.digest !== task.digest || current.plan.delivery?.failure !== "diagnostics-and-verified-prefix-v1") {
    throw new ReviewRequired("Failure delivery requires its own frozen policy; old approvals are not expanded")
  }
  const stages = store.stages(task.id)
  const state = chain(current, stages)
  const row = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='failure_evidence_witness' ORDER BY sequence DESC LIMIT 1")
    .get(task.id) as { payload: string } | null
  if (!row) throw new ReviewRequired("Failure delivery has no retained diagnostic witness")
  const evidence = verifyFailureEvidence(JSON.parse(row.payload), current, stages)
  return [...state.upstream.flatMap((stage) => Object.entries(stage.artifacts)
    .sort(([a], [b]) => a.localeCompare(b)).map(([name, sha256]) => ({ phase: stage.phase, name, sha256 }))),
    ...Object.entries(evidence.artifacts).sort(([a], [b]) => a.localeCompare(b))
      .map(([name, value]) => ({ phase: evidence.phase, name, sha256: value!.sha256 }))]
}

// Failure diagnostics are a separate evidence product: report_ready always stays
// false. No partial pose, success gate, execution mutation or submission is allowed.
export async function readFailureReport(store: Store, id: string, transport: TransportFailure,
  abort = new AbortController().signal) {
  const task = store.get(id)
  const stages = store.stages(id)
  const base = { task_id: id, plan_digest: task.digest, report_ready: false as const, verification_started_at: Date.now() }
  try {
    abort.throwIfAborted()
    const state = chain(task, stages)
    const binding = digest({ task, stages })
    const bound = () => {
      abort.throwIfAborted()
      if (digest({ task: store.get(id), stages: store.stages(id) }) !== binding) throw new ReviewRequired("Failure task changed during evidence read")
    }
    const verify = async () => {
      for (const [index, saved] of state.upstream.entries()) {
        bound()
        const fresh = verifyOutcome(await transport.verify(task, stages[index]), task, stages[index], state.upstream[index - 1] ?? null)
        if (digest(fresh) !== digest(saved)) throw new ReviewRequired("Previously verified upstream evidence changed")
      }
      bound()
    }
    await verify()
    const retained = () => {
      const row = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='failure_evidence_witness' ORDER BY sequence DESC LIMIT 1")
        .get(id) as { payload: string } | null
      return row ? verifyFailureEvidence(JSON.parse(row.payload), task, stages) : null
    }
    const initial = retained() ?? verifyFailureEvidence(await transport.failure(task, state.failed), task, stages)
    bound()
    // Save before the next I/O: timeout/reopen cannot authorize a new witness.
    store.db.transaction(() => {
      bound()
      const saved = retained()
      if (saved && digest(saved) !== digest(initial)) throw new ReviewRequired("Retained failure witness differs")
      if (!saved) store.event(id, "failure_evidence_witness", initial)
    })()
    const fresh = verifyFailureEvidence(await transport.failure(task, state.failed, digest(initial)), task, stages)
    bound()
    if (digest(fresh) !== digest(initial)) throw new ReviewRequired("Failure evidence changed from the retained witness")
    await verify()
    return { ...base, failure_evidence_ready: true as const, verification: "diagnostics_verified" as const,
      verified_at: Date.now(), failed_phase: state.failed.phase, evidence_digest: digest(fresh),
      diagnostics: fresh, upstream_manifest_digests: state.upstream.map(digest),
      note: "Verified diagnostic inventory only, not a usable screening report or validated molecular outputs. These are remote files; consult separate delivery state and native Results receipts for local copies. No compute was started or retried." }
  } catch (error) {
    return { ...base, failure_evidence_ready: false as const,
      verification: error instanceof ReviewRequired || error instanceof z.ZodError ? "evidence_rejected" as const : "verification_unavailable" as const,
      reason: error instanceof Error ? error.message : "Failure evidence unavailable",
      instruction: "Do not infer missing diagnostics, mark screening successful, or restart compute. Retain the failed task and retry only the read when appropriate." }
  }
}
