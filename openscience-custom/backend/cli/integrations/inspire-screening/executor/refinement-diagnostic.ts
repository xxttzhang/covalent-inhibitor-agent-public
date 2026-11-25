import z from "zod"
import path from "node:path"
import { createHash, randomBytes } from "node:crypto"
import probe from "./runtime_probe.py" with { type: "text" }
import reader from "./refinement_diagnostic.py" with { type: "text" }
import { digest, type Task } from "./model"
import { refinementRuntime } from "./refinement-runtime"
import { Inspire, quote } from "./inspire"
import { root } from "./host"
import type { Store } from "./store"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const code = z.object({ release_id: hash, manifest_sha256: hash, checker_sha256: hash }).strict()
export const refinementProfile = z.object({ version: z.literal("refinement-environment-profile-v1"),
  project: z.string().min(1), target: z.literal("EGFR"), site: z.literal("C797"), code,
  af3: refinementRuntime, covdock: refinementRuntime,
}).strict().superRefine((profile, ctx) => {
  if (profile.af3.name !== "af3" || profile.covdock.name !== "covdock") ctx.addIssue({ code: "custom", message: "Expected both registered runtime roles" })
  if (profile.af3.python !== profile.covdock.python) ctx.addIssue({ code: "custom", message: "Registered engines must use the same verified wrapper interpreter" })
})
export type RefinementProfile = z.infer<typeof refinementProfile>
const source = probe + "\n" + reader
export const refinementDiagnosticReader = createHash("sha256").update(source).digest("hex")
const check = z.enum(["PASSED", "UNVERIFIED"])
const imports = z.object({ reason: z.enum(["PASSED", "TIMEOUT", "PROCESS_ERROR", "SOURCE_MISMATCH", "IMPORT_ERROR", "PROBE_ERROR"]),
  returncode: z.number().int().nullable(), seconds_limit: z.number().positive().nullable(),
  failed_modules: z.array(z.object({ module: z.string().regex(/^[A-Za-z0-9_.]+$/), error_type: z.string().regex(/^[A-Za-z0-9_]+$/) }).strict()).max(10) }).strict()
const diagnostic = z.object({ version: z.literal("refinement-environment-diagnostic-v1"), task_id: z.string(), plan_digest: hash,
  profile_digest: hash, nonce: z.string().regex(/^[a-f0-9]{32}$/), code,
  files: z.array(z.object({ path: z.string().startsWith("/inspire/"), state: z.enum(["HASHED", "INVALID_FILE", "CHANGED", "UNAVAILABLE"]),
    bytes: z.number().int().positive().max(2 * 1024 ** 3).nullable(), sha256: hash.nullable() }).strict()).length(11), assets_stable: z.boolean(),
  af3: z.object({ wrapper_imports_passed: z.boolean(), native_imports_passed: z.boolean(), wrapper_check: imports, native_check: imports,
    protein: z.object({ state: check, code: z.enum(["TARGET_CYS_PRESENT", "AF3_TARGET_INPUT_NOT_VERIFIED"]),
      sequence_length: z.number().int().positive().nullable(), cysteine_sequence_index: z.number().int().positive(),
      unpaired_msa_present: z.boolean(), paired_msa_present: z.boolean(), template_count: z.number().int().nonnegative() }).strict(),
    cpu_parameters: z.object({ state: check, code: z.enum(["CPU_PARAMETERS_LOADED", "CPU_PARAMETERS_LOAD_FAILED",
      "CPU_PARAMETERS_UNAVAILABLE", "CPU_PARAMETERS_TIMEOUT", "CPU_PARAMETERS_INVALID_OUTPUT"]),
      tensor_count: z.number().int().positive().max(100000).nullable(), parameter_bytes: z.number().int().positive().max(2 * 1024 ** 3).nullable(),
      schema_sha256: hash.nullable(), error_type: z.string().regex(/^[A-Za-z0-9_]{1,80}$/).nullable(),
      phase: z.enum(["not_started", "imports", "loader_identity", "model_selection", "cpu_backend", "parameter_loading",
        "parameter_validation", "source_recheck", "completed"]),
      loader_path: z.string().startsWith("/inspire/").nullable(), loader_sha256: hash.nullable(), seconds_limit: z.literal(60),
      memory_limit_bytes: z.literal(16 * 1024 ** 3), returncode: z.number().int().nullable() }).strict(),
    model_loaded: z.literal(false), gpu_verified: z.literal(false) }).strict(),
  covdock: z.object({ wrapper_imports_passed: z.boolean(), wrapper_check: imports, license: z.object({ state: z.enum(["FAILED", "UNVERIFIED"]),
    code: z.enum(["COVDOCK_ENTITLEMENT_NOT_TESTED", "INVALID_LICENSE_SYNTAX", "NO_LICENSE_FEATURES", "LICENSE_CHECKOUT_FAILED",
      "LICENSE_DIAGNOSTIC_UNAVAILABLE", "LICENSE_DIAGNOSTIC_TIMEOUT", "LICENSE_DIAGNOSTIC_INVALID_OUTPUT",
      "LICENSE_CONFIGURATION_REVIEW_REQUIRED", "LICENSE_CONFIGURATION_UNAVAILABLE"]),
    feature: z.literal("MMLIBS"), returncode: z.number().int().nullable(), covdock_entitlement_verified: z.literal(false) }).strict(),
    entitlements: z.object({ state: check, covdock_available: z.boolean(), ligprep_available: z.boolean(),
      diagnostic_state: z.enum(["RETURNED", "TIMEOUT", "UNAVAILABLE", "INVALID_OUTPUT"]).optional(), seconds_limit: z.literal(60).optional(),
      source_path: z.string().startsWith("/inspire/").nullable(), source_sha256: hash.nullable(), returncode: z.number().int().nullable(),
      scope: z.literal("vendor_covdock_checkLicense_and_ligprep_availability") }).strict(),
    receptor: z.object({ state: check, attachment_residue: z.enum(["A:773", "A:797"]),
      code: z.enum(["UNIQUE_CYS_SG_CA", "RECEPTOR_MAPPING_NOT_VERIFIED"]) }).strict() }).strict(),
  blockers: z.array(z.string().regex(/^[A-Z][A-Z0-9_]+$/)).min(1).max(12), ready_to_submit: z.literal(false), compute_submitted: z.literal(false),
  scientific_quality_verified: z.literal(false), resource_approval_granted: z.literal(false),
}).strict()

export async function refinementSettings() {
  return refinementProfile.parse(await Bun.file(path.join(root, "configs/inspire-refinement.json")).json())
}

function scope(task: Task) {
  if (task.state !== "SUCCEEDED" || digest(task.plan) !== task.digest || !task.approval ||
    JSON.parse(task.approval).digest !== task.digest || task.plan.target !== "EGFR" || task.plan.site !== "C797") {
    throw new Error("Refinement environment diagnosis requires this project's completed approved EGFR C797 core")
  }
}

export function verifyRefinementDiagnostic(value: unknown, task: Task, profile: RefinementProfile, nonce: string) {
  scope(task)
  const result = diagnostic.parse(value)
  const af3 = profile.af3
  const covdock = profile.covdock
  if (af3.name !== "af3" || covdock.name !== "covdock") throw new Error("Invalid registered runtime roles")
  const paths = [af3.python, af3.af3_python, af3.runner, af3.source_data_json, `${af3.model_dir}/af3.bin`,
    covdock.receptor, covdock.license_file, ...["run", "ligprep", "covalent_docking", "utilities/lictest"].map((name) => `${covdock.schrodinger_root}/${name}`)]
  if (result.task_id !== task.id || result.plan_digest !== task.digest || result.profile_digest !== digest(profile) || result.nonce !== nonce ||
    digest(result.code) !== digest(profile.code) || digest(result.files.map((file) => file.path)) !== digest(paths) ||
    result.covdock.receptor.attachment_residue !== covdock.attachment_residue || result.af3.protein.cysteine_sequence_index !== af3.cysteine_sequence_index ||
    result.files.some((file) => file.state === "HASHED" ? file.sha256 === null || file.bytes === null : file.sha256 !== null || file.bytes !== null) ||
    (result.assets_stable && result.files.some((file) => file.state !== "HASHED"))) throw new Error("Refinement environment evidence identity or file inventory changed")
  const blockers = ["AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED",
    ...(result.covdock.entitlements.state !== "PASSED" ? ["COVDOCK_ENTITLEMENT_NOT_VERIFIED"] : []),
    ...(!result.assets_stable ? ["ASSETS_MISSING_OR_CHANGED"] : []),
    ...(!result.af3.wrapper_imports_passed || !result.af3.native_imports_passed ? ["AF3_IMPORTS_FAILED"] : []),
    ...(!result.covdock.wrapper_imports_passed ? ["COVDOCK_WRAPPER_IMPORTS_FAILED"] : []),
    ...(result.af3.protein.state !== "PASSED" ? ["AF3_TARGET_INPUT_NOT_VERIFIED"] : []),
    ...(result.af3.cpu_parameters.state !== "PASSED" ? [result.af3.cpu_parameters.code] : []),
    ...(result.covdock.receptor.state !== "PASSED" ? ["RECEPTOR_MAPPING_NOT_VERIFIED"] : []),
    ...(result.covdock.license.code !== "COVDOCK_ENTITLEMENT_NOT_TESTED" ? [result.covdock.license.code] : [])]
  if (digest(result.blockers) !== digest(blockers) ||
    result.af3.wrapper_imports_passed !== (result.af3.wrapper_check.reason === "PASSED") ||
    result.af3.native_imports_passed !== (result.af3.native_check.reason === "PASSED") ||
    result.covdock.wrapper_imports_passed !== (result.covdock.wrapper_check.reason === "PASSED") ||
    (result.af3.protein.state === "PASSED") !== (result.af3.protein.code === "TARGET_CYS_PRESENT") ||
    (result.covdock.receptor.state === "PASSED") !== (result.covdock.receptor.code === "UNIQUE_CYS_SG_CA")) throw new Error("Diagnostic findings and blockers disagree")
  const weights = result.af3.cpu_parameters
  const entitlement = result.covdock.entitlements
  if (entitlement.diagnostic_state && entitlement.diagnostic_state !== "RETURNED" && entitlement.state === "PASSED") {
    throw new Error("Interrupted vendor diagnostic cannot prove entitlement")
  }
  if ((entitlement.source_path === null) !== (entitlement.source_sha256 === null) ||
    (entitlement.source_path !== null && (!entitlement.source_path.startsWith(covdock.schrodinger_root + "/") ||
      path.posix.normalize(entitlement.source_path) !== entitlement.source_path)) ||
    (entitlement.state === "PASSED") !== (entitlement.covdock_available && entitlement.ligprep_available) ||
    ((entitlement.covdock_available || entitlement.ligprep_available) && (!entitlement.source_sha256 || entitlement.returncode !== 0))) {
    throw new Error("Vendor entitlement evidence contradicts its availability or source")
  }
  const prefix = path.posix.dirname(path.posix.dirname(af3.af3_python))
  const installed = weights.loader_path?.slice(prefix.length)
  const native = weights.loader_path?.startsWith(prefix + "/") && /^\/lib\/python3\.[0-9]+\/site-packages\/alphafold3\/model\/params\.py$/.test(installed ?? "")
  if ((weights.loader_path !== null && weights.loader_path !== path.posix.join(path.posix.dirname(af3.runner), "src/alphafold3/model/params.py") && !native) ||
    (weights.loader_path === null) !== (weights.loader_sha256 === null) ||
    (weights.state === "PASSED") !== (weights.code === "CPU_PARAMETERS_LOADED") ||
    (weights.state === "PASSED" ? weights.phase !== "completed" || weights.returncode !== 0 || weights.error_type !== null || weights.loader_sha256 === null ||
      weights.tensor_count === null || weights.parameter_bytes === null || weights.schema_sha256 === null :
      weights.phase === "completed" || weights.tensor_count !== null || weights.parameter_bytes !== null || weights.schema_sha256 !== null)) {
    throw new Error("CPU parameter loading evidence contradicts its state or registered loader")
  }
  if ((result.af3.protein.state === "PASSED" && (!result.af3.protein.sequence_length || result.af3.protein.sequence_length < af3.cysteine_sequence_index)) ||
    [result.af3.wrapper_check, result.af3.native_check, result.covdock.wrapper_check].some((value) =>
      (value.reason === "PASSED" && value.failed_modules.length > 0) || (value.reason === "TIMEOUT" && value.seconds_limit === null)) ||
    (result.covdock.license.state === "FAILED") !== ["INVALID_LICENSE_SYNTAX", "NO_LICENSE_FEATURES", "LICENSE_CHECKOUT_FAILED"].includes(result.covdock.license.code)) {
    throw new Error("Diagnostic check details contradict their reported state")
  }
  return result
}

export function refinementDiagnosticArgs(task: Task, profile: RefinementProfile, nonce: string) {
  scope(task)
  refinementProfile.parse(profile)
  if (!/^[a-f0-9]{32}$/.test(nonce) || task.plan.steps.some((step) => step.resource.project !== profile.project)) throw new Error("Refinement profile belongs to another project or diagnostic attempt")
  const packet = Buffer.from(JSON.stringify({ root: task.plan.runtime.root, task_id: task.id, plan_digest: task.digest,
    profile, profile_digest: digest(profile), nonce })).toString("base64")
  const command = ["env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
    "python3", "-I", "-B", "-c", source, packet].map(quote).join(" ")
  return ["notebook", "exec", task.plan.connection.notebook, "--workspace", task.plan.connection.workspace, "--timeout", "180", command]
}

export async function readRefinementDiagnostic(task: Task, profile: RefinementProfile, nonce: string) {
  const transport = new Inspire(task.plan.connection)
  transport.bound(task)
  const value = z.object({ returncode: z.literal(0), output: z.string().max(64 * 1024) }).parse(
    await transport.call(refinementDiagnosticArgs(task, profile, nonce), 200000))
  return JSON.parse(value.output) as unknown
}

export async function diagnoseRefinement(store: Store, id: string, signal: AbortSignal,
  settings = refinementSettings, read = readRefinementDiagnostic) {
  const task = store.get(id)
  scope(task)
  const nonce = randomBytes(16).toString("hex")
  const identity = digest({ plan: task.plan, state: task.state, approval: task.approval })
  const bound = () => {
    signal.throwIfAborted()
    const current = store.get(id)
    if (identity !== digest({ plan: current.plan, state: current.state, approval: current.approval })) throw new Error("Core changed during environment diagnosis")
  }
  try {
    bound()
    const profile = refinementProfile.parse(await settings())
    bound()
    refinementDiagnosticArgs(task, profile, nonce)
    const value = verifyRefinementDiagnostic(await read(task, profile, nonce), task, profile, nonce)
    bound()
    if (digest(await settings()) !== digest(profile)) throw new Error("Runtime profile changed during diagnosis")
    store.event(id, "refinement_environment_diagnostic", { state: "AVAILABLE", reader: refinementDiagnosticReader, profile, value })
  } catch {
    store.event(id, "refinement_environment_diagnostic", { state: "UNAVAILABLE", reader: refinementDiagnosticReader })
  }
  return refinementDiagnosticSnapshot(store, store.get(id))
}

export function refinementDiagnosticSnapshot(store: Store, task: Task) {
  const row = store.db.query("SELECT at,payload FROM events WHERE task_id=? AND kind='refinement_environment_diagnostic' ORDER BY sequence DESC LIMIT 1")
    .get(task.id) as { at: number; payload: string } | null
  if (!row) return null
  const base = { task_id: task.id, plan_digest: task.digest, recorded_at: row.at, ready_to_submit: false, compute_submitted: false,
    note: "这是记录时的环境诊断，不是资源批准或精筛完成。CPU 参数加载只核实指定权重可读取、数值有限，不验证模型架构匹配、GPU 推理或权重授权；MMLIBS 测试不是 CovDock 功能授权证明。" }
  try {
    const saved = z.object({ state: z.literal("AVAILABLE"), reader: hash, profile: refinementProfile, value: diagnostic }).strict().parse(JSON.parse(row.payload))
    if (saved.reader !== refinementDiagnosticReader) throw new Error("Diagnostic reader changed")
    const result = verifyRefinementDiagnostic(saved.value, task, saved.profile, saved.value.nonce)
    return { ...base, state: "AVAILABLE" as const, result }
  } catch {
    return { ...base, state: "UNAVAILABLE" as const, result: null,
      note: "最新环境诊断不可用或证据不一致；没有回退旧记录，也未批准或启动计算。" }
  }
}
