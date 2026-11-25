import z from "zod"
import { createHash } from "node:crypto"
import source from "./preparation_diagnostic.py" with { type: "text" }
import { digest, type Task, type Stage } from "./model"
import { Inspire, quote } from "./inspire"
import type { Store } from "./store"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const count = z.number().int().min(0).max(20000)
const reasons = z.object({ mw_below_min: count, mw_above_max: count, clogp_below_min: count,
  clogp_above_max: count, tpsa_above_max: count, rotors_above_max: count, charge_above_max: count }).strict()
const file = z.object({ sha256: hash, bytes: z.number().int().positive().max(32 * 1024 * 1024) }).strict()
export const diagnostic = z.object({ version: z.literal("preparation-diagnostic-v1"), task_id: z.string(), plan_digest: hash,
  rows: count, input_rows: count, eligible: count, incompatible: count, ambiguous: count, valid_warhead: count,
  limits: z.object({ min_mw: z.number().finite(), max_mw: z.number().finite(), min_logp: z.number().finite(),
    max_logp: z.number().finite(), max_tpsa: z.number().finite(), max_rotors: z.number().finite(), max_abs_formal_charge: z.number().finite() }).strict(),
  property_failures: reasons, valid_warhead_property_failures: reasons,
  files: z.object({ "plan.json": file, "started.json": file, "error.json": file,
    "target.yaml": file, "selection.json": file, "master.parquet": file }).strict(),
  scientific_quality_verified: z.literal(false), compute_submitted: z.literal(false),
}).strict()
export const readerDigest = createHash("sha256").update(source).digest("hex")

export function diagnosticScope(task: Task, stages: Stage[]) {
  if (!["FAILED", "NEEDS_REVIEW"].includes(task.state) || digest(task.plan) !== task.digest ||
    !task.approval || JSON.parse(task.approval).digest !== task.digest || stages.length !== 4 ||
    !["FAILED", "VERIFYING"].includes(stages[0].state) || stages[0].submitted === null || !stages[0].handle ||
    stages[0].manifest !== null || stages.some((stage, index) => stage.task_id !== task.id || stage.ordinal !== index ||
      stage.phase !== task.plan.steps[index].phase || stage.name !== `${task.id}-${stage.phase}`) ||
    stages.slice(1).some((stage) => stage.state !== "WAITING" || stage.handle !== null || stage.submitted !== null || stage.manifest !== null)) {
    throw new Error("Preparation diagnosis requires a stopped approved preparation attempt and untouched downstream stages")
  }
}

export function verifyDiagnostic(value: unknown, task: Task, stages: Stage[]) {
  diagnosticScope(task, stages)
  const result = diagnostic.parse(value)
  if (result.task_id !== task.id || result.plan_digest !== task.digest || task.plan.scope !== "bounded" ||
    !task.plan.max_records || result.input_rows > task.plan.max_records || result.rows > result.input_rows ||
    result.rows !== result.incompatible + result.ambiguous + result.valid_warhead || result.eligible > result.valid_warhead ||
    result.files["target.yaml"].sha256 !== task.plan.assets[`${task.plan.runtime.project}/configs/covindb/EGFR_C797_mvp_v1.yaml`] ||
    Object.values(result.property_failures).some((count) => count > result.rows) ||
    Object.entries(result.valid_warhead_property_failures).some(([key, count]) => count > result.valid_warhead ||
      count > result.property_failures[key as keyof typeof result.property_failures])) {
    throw new Error("Preparation diagnosis scope, source or counts disagree")
  }
  return result
}

export async function readPreparationDiagnostic(task: Task) {
  const transport = new Inspire(task.plan.connection)
  transport.bound(task)
  const command = `cd ${quote(task.plan.runtime.project)} && source ${quote(task.plan.runtime.activation)} >/dev/null && export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 && python -B -c ${quote(source)} ${[task.plan.runtime.root, task.plan.runtime.project, task.id, task.digest].map(quote).join(" ")}`
  const result = z.object({ returncode: z.literal(0), output: z.string().max(65536) }).parse(await transport.call([
    "notebook", "exec", task.plan.connection.notebook, "--workspace", task.plan.connection.workspace, "--timeout", "60", command]))
  return JSON.parse(result.output) as unknown
}

export async function diagnosePreparation(store: Store, id: string, read = readPreparationDiagnostic,
  signal = new AbortController().signal) {
  const task = store.get(id)
  const stages = store.stages(id)
  diagnosticScope(task, stages)
  const identity = digest({ plan: task.plan, approval: task.approval, state: task.state, stages })
  const bound = () => {
    signal.throwIfAborted()
    const current = store.get(id)
    if (digest({ plan: current.plan, approval: current.approval, state: current.state, stages: store.stages(id) }) !== identity)
      throw new Error("Task changed during preparation diagnosis")
  }
  try {
    bound()
    const row = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='preparation_diagnostic_witness' ORDER BY sequence DESC LIMIT 1")
      .get(id) as { payload: string } | null
    const witness = row ? z.object({ reader: hash, value: diagnostic }).strict().parse(JSON.parse(row.payload)) : null
    if (witness && witness.reader !== readerDigest) throw new Error("Retained diagnostic reader changed; inspect before replacing evidence")
    const initial = witness ? verifyDiagnostic(witness.value, task, stages) : verifyDiagnostic(await read(task), task, stages)
    bound()
    store.db.transaction(() => {
      bound()
      const saved = store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='preparation_diagnostic_witness' ORDER BY sequence DESC LIMIT 1")
        .get(id) as { payload: string } | null
      const packet = { reader: readerDigest, value: initial }
      if (saved && digest(JSON.parse(saved.payload)) !== digest(packet)) throw new Error("Concurrent diagnostic witness differs")
      if (!saved) store.event(id, "preparation_diagnostic_witness", packet)
    })()
    const current = verifyDiagnostic(await read(task), task, stages)
    bound()
    if (digest(current) !== digest(initial)) throw new Error("Preparation diagnostic evidence changed from its retained witness")
    store.event(id, "preparation_diagnostic", { reader: readerDigest, state: "AVAILABLE", value: current })
  } catch {
    // No raw remote errors or molecular values in the model-facing snapshot.
    store.event(id, "preparation_diagnostic", { reader: readerDigest, state: "UNAVAILABLE" })
  }
  return preparationSnapshot(store, store.get(id))
}

export function preparationSnapshot(store: Store, task: Task) {
  const row = store.db.query("SELECT at,payload FROM events WHERE task_id=? AND kind='preparation_diagnostic' ORDER BY sequence DESC LIMIT 1")
    .get(task.id) as { at: number; payload: string } | null
  if (!row) return null
  const base = { version: 1 as const, task_id: task.id, plan_digest: task.digest, recorded_at: row.at,
    scientific_quality_verified: false as const, compute_submitted: false as const }
  try {
    const saved = z.object({ reader: hash, state: z.literal("AVAILABLE"), value: diagnostic }).strict().parse(JSON.parse(row.payload))
    const result = verifyDiagnostic(saved.value, task, store.stages(task.id))
    return { ...base, state: "AVAILABLE" as const, reader: saved.reader, result,
      note: "已保存的准备阶段诊断，不是科学成功或分子活性结论；性质统计可能重叠。未读取更多源库记录、放宽门槛或重跑计算。文件哈希是记录时的见证，不保证文件此后未变。" }
  } catch {
    return { ...base, state: "UNAVAILABLE" as const,
      note: "准备阶段诊断不可用或与当前任务不一致；未用旧结果替代，也未重新启动计算。" }
  }
}
