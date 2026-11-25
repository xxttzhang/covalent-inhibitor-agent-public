import { createHash } from "node:crypto"
import { constants } from "node:fs"
import { open, realpath } from "node:fs/promises"
import z from "zod"
import { researchDigest } from "./research-loop-contract"
import { captureResearchNativeActionResult, readResearchNativeAction } from "./research-native-controller"
import { readResearchNativeReceipt, type ResearchNativeReceipt } from "./research-native"
import { createResearchObservation, type ResearchObservationInput } from "./research-observations"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const count = z.number().int().nonnegative()
const sha = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex")
const source = z.object({ path: z.string().min(1), sha256: hash, max_rows: count,
  records_read: count, record_selection: z.literal("file_order_prefix") }).passthrough()
const target = z.object({ config_path: z.string().min(1), config_sha256: hash, target_id: z.literal("EGFR"),
  site_id: z.literal("EGFR_C797") }).passthrough()
const selectionSchema = z.object({ schema_version: z.literal("1.0"), status: z.literal("completed"),
  selection_outcome: z.enum(["CANDIDATES_SELECTED", "NO_CANDIDATES_SELECTED"]), source, target,
  selection: z.object({ top_n_requested: count, top_n_selected: count }).passthrough(),
  counts: z.object({ input_rows: count, selected: count }).passthrough(),
  quality_gates: z.record(z.string(), z.boolean()) }).passthrough()
const diagnosticFile = z.object({ sha256: hash, bytes: count.max(128 * 1024 * 1024) }).strict()
const zeroSchema = z.object({ version: z.literal("screening-zero-candidates-v1"), task_id: z.string().min(1),
  phase: z.literal("prepare"), plan_digest: hash, outcome: z.literal("NO_CANDIDATES_SELECTED"), candidate_count: z.literal(0),
  input_rows: count.positive(), actual_source: source, target_config: target,
  files: z.object({ "stage2_selection_manifest.json": diagnosticFile, "stage2_candidates.parquet": diagnosticFile,
    "covindb_master.parquet": diagnosticFile, "covindb_rejected.parquet": diagnosticFile }).strict(),
  termination: z.object({ kind: z.literal("prescreen_quality_gate_exit"), exit_code: z.literal(2) }).strict(),
  input_quality_verified: z.literal(true), scientific_quality_verified: z.literal(false), activity_claim: z.string().min(1) }).strict()
const zeroError = z.object({ task_id: z.string(), phase: z.literal("prepare"), plan_digest: hash,
  status: z.literal("FAILED"), failure_kind: z.literal("zero_candidates"), exit_code: z.literal(2) }).passthrough()
type Artifact = ResearchObservationInput["artifacts"][number]
type Identity = Parameters<typeof readResearchNativeAction>[0]

// A separate read supplies the exact bytes parsed below. No later path read can
// quietly replace the bytes whose digest was compared with native Results.
async function contents(file: ResearchNativeReceipt["files"][number], artifact: Artifact): Promise<string | null> {
  if (file.status !== "verified") return null
  try {
    if (await realpath(file.local_path) !== file.local_path) throw Error("redirected native artifact")
    const handle = await open(file.local_path, constants.O_RDONLY | constants.O_NOFOLLOW)
    try {
      const before = await handle.stat()
      if (!before.isFile() || before.size > 128 * 1024 * 1024) throw Error("unbounded native artifact")
      const bytes = await handle.readFile()
      const after = await handle.stat()
      if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs ||
        bytes.length !== file.bytes || sha(bytes) !== file.expected_sha256) throw Error("native artifact changed during parsing")
      return bytes.toString("utf8")
    } finally { await handle.close() }
  } catch (error) {
    artifact.status = (error as NodeJS.ErrnoException).code === "ENOENT" ? "missing" : "mismatch"
    artifact.sha256 = null
    artifact.size_bytes = null
    return null
  }
}

/** Strict RFC 4180 reader: molecule strings can contain commas and quotes. */
function csv(value: string): Record<string, string>[] {
  const rows: string[][] = [], row: string[] = []
  let field = "", quoted = false, closed = false
  const flush = () => { row.push(field); field = ""; closed = false }
  for (let index = 0; index < value.length; index++) {
    const c = value[index]!
    if (quoted) {
      if (c !== '"') { field += c; continue }
      if (value[index + 1] === '"') { field += '"'; index++; continue }
      quoted = false; closed = true; continue
    }
    if (c === '"') { if (field || closed) throw Error("Malformed quoted CSV"); quoted = true; continue }
    if (c === ",") { flush(); continue }
    if (c === "\n" || c === "\r") {
      if (c === "\r" && value[index + 1] === "\n") index++
      flush(); rows.push(row.splice(0)); continue
    }
    if (closed) throw Error("Unexpected content after CSV quote")
    field += c
  }
  if (quoted) throw Error("Unterminated CSV quote")
  if (field || closed || row.length) { flush(); rows.push(row.splice(0)) }
  const header = rows.shift() ?? []
  if (!header.length || header.some(column => !column) || new Set(header).size !== header.length) throw Error("Invalid CSV header")
  return rows.filter(row => row.some(Boolean)).map(row => {
    if (row.length !== header.length) throw Error("CSV field count differs")
    return Object.fromEntries(header.map((name, index) => [name, row[index]!]))
  })
}

function parse<T>(text: string | undefined, parser: (text: string) => T, ref: string, errors: string[]): T | null {
  if (text === undefined) return null
  try { return parser(text) } catch { errors.push(`invalid_native_result:${ref}`); return null }
}

function observation(receipt: ResearchNativeReceipt, action: ReturnType<typeof readResearchNativeAction>,
  artifacts: Artifact[], texts: Map<string, string>): ResearchObservationInput {
  const errors: string[] = [], limitations = [
    "Scientific evaluation has not run; scores and execution success are not scientific acceptance.",
    "Local delivered bytes were freshly rehashed; remote files and publication service were not reread in this adapter.",
    "Actual billed usage is unknown; the original cumulative compute reservation remains occupied.",
  ]
  if (action.cancellation) limitations.push(`Native cancellation disposition: ${action.cancellation}. Actual resource usage remains unknown; no reservation was released.`)
  const ref = (phase: string, name: string) => `native:${receipt.task_id}:${phase}/${name}`
  const read = <T>(phase: string, name: string, parser: (text: string) => T) => parse(texts.get(ref(phase, name)), parser, ref(phase, name), errors)
  const verified = (refs: string[]) => refs.every(ref => artifacts.some(item => item.ref === ref && item.status === "verified"))
  const selected = read("prepare", "stage2_selection_manifest.json", text => selectionSchema.parse(JSON.parse(text)))
  const prepared = read("prepare", "stage2_candidates.csv", csv)
  const sdf = texts.get(ref("prepare", "stage2_candidates.sdf"))
  const carsi = read("carsidock", "scores.csv", csv)
  const rtms = read("rtmscore", "scores.csv", csv)
  const report = read("report", "candidates.csv", csv)
  const failed = receipt.stages.find(stage => stage.state === "FAILED")
  const phase = failed?.phase ?? receipt.stages.filter(stage => stage.state !== "WAITING").at(-1)?.phase ?? "prepare"
  const checks: ResearchObservationInput["execution_checks"] = []
  const candidates: ResearchObservationInput["candidates"] = []
  const counts: ResearchObservationInput["counts"] = { input_records: selected?.source.records_read ?? null,
    selected: prepared?.length ?? null, attempted: null, execution_succeeded: null, scientific_passed: null }
  let identityDigest: string | null = null, identityPass = false
  const identityRefs: string[] = []
  const plan = receipt.plan
  if (selected) {
    const library = plan.research_inputs?.library ?? { path: action.prepared.binding.library, sha256: action.loop.contract.inputs.library_sha256 }
    const config = plan.research_inputs?.science_config ?? { path: action.prepared.binding.science_config, sha256: action.loop.contract.inputs.science_config_sha256 }
    if (selected.source.path !== library.path || selected.source.sha256 !== library.sha256 || plan.assets[library.path] !== selected.source.sha256
      || selected.target.config_path !== config.path || selected.target.config_sha256 !== config.sha256
      || plan.assets[config.path] !== selected.target.config_sha256 || selected.source.max_rows !== plan.max_records
      || selected.source.records_read <= 0 || selected.source.records_read > (plan.max_records ?? 0)
      || (plan.research_inputs && selected.source.records_read !== plan.research_inputs.library.records)
      || selected.counts.input_rows !== selected.source.records_read || selected.selection.top_n_requested !== plan.shortlist
      || selected.selection.top_n_selected !== selected.counts.selected || selected.counts.selected > plan.shortlist)
      errors.push("native_selection_source_or_scope_mismatch")
    if (prepared && selected.counts.selected !== prepared.length) errors.push("native_selection_candidate_count_mismatch")
    if (selected.selection_outcome === "CANDIDATES_SELECTED" && (!Object.keys(selected.quality_gates).length
      || Object.values(selected.quality_gates).some(value => !value))) errors.push("native_selected_input_quality_failed")
  }
  if (prepared) {
    const identities = prepared.map(row => [Number(row.input_sdf_index), row.compound_id] as const).sort((a, b) => a[0] - b[0])
    if (prepared.some(row => !row.compound_id?.trim() || !row.standardized_smiles?.trim() || !/^\d+$/.test(row.input_sdf_index ?? ""))
      || identities.some(([index], i) => !Number.isSafeInteger(index) || index !== i)
      || new Set(prepared.map(row => row.compound_id)).size !== prepared.length) errors.push("native_invalid_candidate_identity")
    identityDigest = sha(JSON.stringify(identities))
    if (receipt.stages.some(stage => stage.manifest && (stage.manifest.identity_digest !== identityDigest || stage.manifest.input_count !== prepared.length)))
      errors.push("native_stage_identity_or_denominator_mismatch")
    if (selected?.selection_outcome !== "CANDIDATES_SELECTED" || prepared.length === 0) errors.push("native_missing_zero_selection_diagnostic")
    identityRefs.push(ref("prepare", "stage2_candidates.csv"))
    if (sdf !== undefined) {
      const records = sdf.split("$$$$").filter(record => record.trim()).map(record => [
        record.match(/>\s*<INPUT_SDF_INDEX>[^\n]*\n([^\n]+)/)?.[1]?.trim(),
        record.match(/>\s*<COMPOUND_ID>[^\n]*\n([^\n]+)/)?.[1]?.trim(),
      ])
      if (records.length !== prepared.length || records.some(([index, id], i) => index !== String(i) || id !== identities[i]?.[1]))
        errors.push("native_sdf_identity_mismatch")
      identityRefs.push(ref("prepare", "stage2_candidates.sdf"))
    }
    for (const [name, rows] of [["carsidock", carsi], ["rtmscore", rtms], ["report", report]] as const) {
      if (!rows) continue
      if (rows.length !== prepared.length || new Set(rows.map(row => row.compound_id)).size !== prepared.length) errors.push(`native_${name}_denominator_mismatch`)
      for (const row of rows) {
        const original = prepared.find(item => item.compound_id === row.compound_id)
        if (!original || row.standardized_smiles !== original.standardized_smiles || row.input_sdf_index !== original.input_sdf_index)
          errors.push(`native_${name}_candidate_identity_mismatch`)
      }
      identityRefs.push(ref(name, name === "report" ? "candidates.csv" : "scores.csv"))
    }
    const engine = (rows: Record<string, string>[] | null, name: string, label: string, direction: string) => {
      if (!rows) return
      if (rows.some(row => !row.engine_status || row.engine !== label || row.score_direction !== direction
        || (row.engine_status === "SUCCESS" && (!row.raw_score?.trim() || !Number.isFinite(Number(row.raw_score)))))) errors.push(`native_${name}_invalid_engine_score`)
      const stage = receipt.stages.find(stage => stage.phase === name)?.manifest
      if (!stage || stage.success_count !== rows.filter(row => row.engine_status === "SUCCESS").length) errors.push(`native_${name}_success_count_mismatch`)
    }
    engine(carsi, "carsidock", "carsidock-cov", "lower_is_better")
    engine(rtms, "rtmscore", "rtmscore", "higher_is_better")
    const completed = Boolean(carsi && rtms && report && receipt.stages.find(stage => stage.phase === "report")?.manifest)
    for (const row of prepared) {
      const docked = carsi?.find(item => item.compound_id === row.compound_id)
      const scored = rtms?.find(item => item.compound_id === row.compound_id)
      const joined = report?.find(item => item.compound_id === row.compound_id)
      const success = docked?.engine_status === "SUCCESS" && scored?.engine_status === "SUCCESS"
      if (joined && docked && scored && (joined.carsidock_status !== docked.engine_status || joined.rtmscore_status !== scored.engine_status
        || (docked.engine_status === "SUCCESS" && (!joined.carsiscore?.trim() || Number(joined.carsiscore) !== Number(docked.raw_score)))
        || (scored.engine_status === "SUCCESS" && (!joined.rtmscore?.trim() || Number(joined.rtmscore) !== Number(scored.raw_score))))) errors.push("native_report_engine_mismatch")
      const scores: ResearchObservationInput["candidates"][number]["scores"] = []
      if (docked?.engine_status === "SUCCESS" && docked.raw_score?.trim() && Number.isFinite(Number(docked.raw_score))) scores.push({ engine: "carsidock-cov", value: Number(docked.raw_score), direction: "lower_is_better" })
      if (scored?.engine_status === "SUCCESS" && scored.raw_score?.trim() && Number.isFinite(Number(scored.raw_score))) scores.push({ engine: "rtmscore", value: Number(scored.raw_score), direction: "higher_is_better" })
      candidates.push({ id: row.compound_id?.trim() || "invalid-candidate-identity", smiles: row.standardized_smiles?.trim() || null,
        execution_status: completed ? success ? "succeeded" : "failed" : "unknown", scientific_status: "unknown", scores })
    }
    const docking = receipt.stages.find(stage => stage.phase === "carsidock")!
    counts.attempted = docking.submitted !== null || docking.manifest !== null ? prepared.length : 0
    counts.execution_succeeded = completed ? candidates.filter(item => item.execution_status === "succeeded").length : counts.attempted === 0 ? 0 : null
    if (completed && receipt.stages.find(stage => stage.phase === "report")?.manifest?.success_count !== counts.execution_succeeded) errors.push("native_report_success_count_mismatch")
    identityPass = Boolean(selected && sdf !== undefined && receipt.stages.every(stage => !stage.manifest || stage.phase === "prepare"
      || (stage.phase === "carsidock" ? carsi : stage.phase === "rtmscore" ? rtms : report)))
  }
  const zero = read("prepare", "zero-candidates.json", text => zeroSchema.parse(JSON.parse(text)))
  const errorText = texts.get(ref(phase, "error.json"))
  let selectionOutcome: ResearchObservationInput["selection_outcome"]
  if (zero) {
    const error = parse(errorText, text => zeroError.parse(JSON.parse(text)), ref("prepare", "error.json"), errors)
    const selectionRef = ref("prepare", "stage2_selection_manifest.json")
    const artifact = artifacts.find(item => item.ref === selectionRef)
    const gates = { rdkit_parse_rate_ge_0_99: true, selected_warhead_mapping_rate_ge_0_98: false, identity_loss_zero: true, failure_table_present: true }
    const detail = selected && z.object({ diagnostics_version: z.literal("prescreen-rejections-v1"),
      counts: z.object({ input_rows: count.positive(), parsed: count, unique: count, duplicates: count, invalid: count,
        hard_gate_pass: z.literal(0), selected: z.literal(0) }).passthrough(),
      quality: z.object({ identity_loss_count: z.literal(0), failure_table_present: z.literal(true),
        rdkit_parse_rate: z.number().min(0.99).max(1), selected_warhead_mapping_rate: z.literal(0) }).passthrough() }).passthrough().safeParse(selected)
    if (!selected || !detail || !detail.success || !error || receipt.task_state !== "FAILED" || phase !== "prepare"
      || zero.task_id !== receipt.task_id || zero.plan_digest !== receipt.plan_digest || error.task_id !== zero.task_id || error.plan_digest !== zero.plan_digest
      || selected.selection_outcome !== "NO_CANDIDATES_SELECTED" || selected.counts.selected !== 0 || prepared !== null
      || zero.input_rows !== selected.counts.input_rows || researchDigest(zero.actual_source) !== researchDigest(selected.source)
      || researchDigest(zero.target_config) !== researchDigest(selected.target) || researchDigest(selected.quality_gates) !== researchDigest(gates)
      || zero.files["stage2_selection_manifest.json"].sha256 !== artifact?.sha256 || zero.files["stage2_selection_manifest.json"].bytes !== artifact?.size_bytes
      || (detail.success && (detail.data.counts.input_rows !== detail.data.counts.unique + detail.data.counts.duplicates + detail.data.counts.invalid
        || detail.data.quality.rdkit_parse_rate !== detail.data.counts.parsed / detail.data.counts.input_rows))) errors.push("native_zero_selection_binding_mismatch")
    const refs = [ref("prepare", "zero-candidates.json"), selectionRef, ref("prepare", "error.json")]
    if (!errors.length && verified(refs)) {
      counts.selected = 0; counts.attempted = 0; counts.execution_succeeded = 0
      identityDigest = sha("[]"); identityPass = true; identityRefs.push(...refs)
      selectionOutcome = { status: "zero_candidates", evaluator: "screening-zero-candidates-v1", evidence_refs: refs }
      limitations.push("Selection completed with no eligible candidates; native task remains FAILED and docking did not run. This says nothing about the source library's biological activity.",
        "Zero-row candidate and rejected/master Parquet evidence was verified by the native worker and retained failure witness; those remote tables were not delivered or reread by this adapter.")
    }
  }
  const requiredRefs = receipt.task_state === "SUCCEEDED" ? [ref("prepare", "stage2_selection_manifest.json"), ref("prepare", "stage2_candidates.csv"),
    ref("prepare", "stage2_candidates.sdf"), ref("carsidock", "scores.csv"), ref("rtmscore", "scores.csv"), ref("report", "candidates.csv")] : identityRefs
  const missing = requiredRefs.filter(item => !texts.has(item))
  limitations.push(...missing.map(item => `Missing required native result: ${item}`))
  if (receipt.diagnostics_error) limitations.push(`Native failure diagnostics unavailable: ${receipt.diagnostics_error}`)
  checks.push({ id: selectionOutcome ? "native_zero_selection" : "native_execution_artifacts", status: selectionOutcome ? "pass" : errors.length ? "fail"
    : receipt.task_state === "SUCCEEDED" && missing.length === 0 && requiredRefs.length > 0 ? "pass" : "unknown",
    evaluator: "drug-design-native-results-adapter-v1", evidence_refs: requiredRefs.filter(item => verified([item])),
    detail: selectionOutcome ? "Bound prescreen quality exit and zero selection verified; native execution is still failed."
      : "Checks native stage lineage, delivered source/identity/counts and execution scores only; no independent scientific assessment." })
  if (!checks[0]!.evidence_refs.length) checks[0]!.status = "unknown"
  const failedRefs = [ref(phase, "error.json")].filter(item => verified([item]))
  const started = receipt.stages.map(stage => stage.submitted).filter((time): time is number => time !== null)
  return { origin: "native_execution", run: { id: receipt.task_id, task_id: receipt.task_id, route: "library-first", release_id: receipt.source_release,
    input_identity_digest: identityDigest, started_at: started.length ? new Date(Math.min(...started)).toISOString() : null,
    status: receipt.task_state === "SUCCEEDED" ? "succeeded" : receipt.task_state === "CANCELLED" ? "cancelled" : "failed", phase }, counts, candidates, artifacts,
    identity: { status: errors.length ? "fail" : identityPass ? "pass" : "unknown", evidence_refs: [...new Set(identityRefs)].filter(item => verified([item])),
      detail: identityPass ? "Delivered source scope and candidate identities agree with the frozen native plan and verified stage lineage." : "Delivered identity evidence is incomplete or inconsistent." },
    execution_checks: checks, scientific_checks: action.loop.contract.quality.required_checks.map(id => ({ id, status: "unknown" as const,
      evaluator: null, evidence_refs: [], detail: "Independent scientific evaluator has not consumed this native result." })),
    required_scientific_checks: action.loop.contract.quality.required_checks,
    ...(selectionOutcome ? { selection_outcome: selectionOutcome } : {}),
    failure: !selectionOutcome && receipt.task_state !== "SUCCEEDED" && failedRefs.length ? { category: "unknown",
      detail: "Native task failed; consult its exact delivered diagnostic. No cause is inferred from error text.", evidence_refs: failedRefs } : null,
    integrity_errors: errors, limitations }
}

/** Host-only entry: no parsed results, file paths or scientific judgments are
 * accepted from model arguments. Repeated identical receipts produce exactly
 * one observation; late events cannot revive a terminal research loop. */
export async function observeResearchNativeAction(input: Identity) {
  const action = readResearchNativeAction(input)
  const captured = await captureResearchNativeActionResult({ ...input, decision_id: action.decision_id })
  if (!["SUCCEEDED", "FAILED", "CANCELLED"].includes(captured.receipt.task_state))
    return { observation: null, evidence_ref: null, receipt_ref: captured.evidence_ref, loop: captured.loop, accounting_status: captured.accounting_status, receipt: captured.receipt, reference: captured.reference, decision_id: captured.decision_id }
  const artifacts: Artifact[] = [], texts = new Map<string, string>()
  for (const file of captured.receipt.files) {
    const artifact: Artifact = { ref: `native:${captured.receipt.task_id}:${file.phase}/${file.name}`, path: file.local_path,
      sha256: file.actual_sha256, expected_sha256: file.expected_sha256, size_bytes: file.bytes,
      status: file.status === "verified" ? "verified" : file.status === "mismatch" ? "mismatch" : "missing" }
    artifacts.push(artifact)
    // Parse text roles only; pose binaries and potentially large Parquet/SDF
    // inventories retain the fresh native hash without needless text decoding.
    if (!["stage2_selection_manifest.json", "stage2_candidates.csv", "stage2_candidates.sdf", "scores.csv", "candidates.csv", "zero-candidates.json", "error.json"].includes(file.name)) continue
    const text = await contents(file, artifact)
    if (text !== null) texts.set(artifact.ref, text)
  }
  const value = createResearchObservation(observation(captured.receipt, action, artifacts, texts))
  const fresh = await readResearchNativeReceipt({ research: input.research, native: input.native, reference: captured.reference })
  const { observed_at: _first, ...before } = captured.receipt
  const { observed_at: _last, ...after } = fresh
  if (researchDigest(before) !== researchDigest(after)) throw Error("Native result changed while constructing its observation; retry the read only")
  return input.research.db.transaction(() => {
    const provenance = { schema: "drug-design-native-observation-binding-v1", observation_id: value.id,
      decision_id: captured.decision_id, receipt_ref: captured.evidence_ref, task_id: fresh.task_id, plan_digest: fresh.plan_digest }
    input.research.record(input.id, `native-observation-binding:${value.id}`, provenance)
    const loop = input.research.observe(input.id, value.id, value)
    return { observation: value, evidence_ref: value.id, receipt_ref: captured.evidence_ref, loop, accounting_status: captured.accounting_status, receipt: fresh, reference: captured.reference, decision_id: captured.decision_id }
  }).immediate()
}
