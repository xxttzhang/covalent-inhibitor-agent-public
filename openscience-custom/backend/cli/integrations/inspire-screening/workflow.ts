import path from "node:path"
import { createHash, randomBytes } from "node:crypto"
import { mkdir, writeFile } from "node:fs/promises"
import z from "zod"
import type { ToolContext } from "@synsci/plugin/tool"
import { legacySharedRoot } from "./core"

const root = path.resolve(import.meta.dir, "../../../../..")
const quote = (value: string) => "'" + value.replaceAll("'", "'\"'\"'") + "'"
const sha = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex")

export const schema = z.object({
  action: z.enum(["describe", "inspect", "draft"]),
  target: z.string().regex(/^[A-Za-z0-9_-]{1,40}$/).optional(),
  site: z.string().regex(/^C[1-9][0-9]{0,4}$/).optional(),
  source_library: z.literal("virtual_screening").optional(),
  scope: z.enum(["full", "bounded"]).optional(),
  max_records: z.number().int().positive().max(100000000).optional(),
  shortlist: z.number().int().positive().max(20000).optional(),
  gpu_minutes: z.number().int().positive().max(1000000).optional(),
  cpu_minutes: z.number().int().positive().max(1000000).optional(),
  af3_max: z.number().int().min(0).max(100).optional(),
  covdock_max: z.number().int().min(0).max(50).optional(),
}).strict().superRefine((args, ctx) => {
  if (args.scope === "full" && args.max_records !== undefined) ctx.addIssue({ code: "custom", message: "A full-library request must not silently truncate max_records" })
  if (args.scope === "bounded" && !args.max_records) ctx.addIssue({ code: "custom", message: "bounded scope requires explicit max_records" })
  if (args.max_records && args.shortlist && args.shortlist > args.max_records) ctx.addIssue({ code: "custom", message: "shortlist exceeds source scope" })
  if (args.shortlist && ((args.af3_max ?? 0) > args.shortlist || (args.covdock_max ?? 0) > args.shortlist)) ctx.addIssue({ code: "custom", message: "Refinement cap exceeds shortlist" })
  if (args.action !== "draft" && [args.scope, args.max_records, args.shortlist, args.gpu_minutes, args.cpu_minutes, args.af3_max, args.covdock_max].some((value) => value !== undefined)) ctx.addIssue({ code: "custom", message: "Scope and budgets belong to draft only" })
})
type Input = z.infer<typeof schema>

export const stages = [
  { id: "intake", title: "确定靶点与候选库", engine: null, gate: "冻结靶点/位点映射、受体选择、原始库清单和范围；不得自动选择旧 pilot", output: "source_manifest + target/site audit" },
  { id: "prepare", title: "准备和质量检查", engine: "CovInDB / RDKit", gate: "从所选虚拟筛选库重新准备；保留身份、标准化失败、warhead 映射和三维准备失败表", output: "candidate SDF/table + QC manifest + failure table" },
  { id: "carsidock", title: "CarsiDock 主筛", engine: "CarsiDock-Cov", gate: "准备质量门通过；批准候选集合和 GPU 时限后执行", output: "traceable poses + scores + complete success/failure denominator" },
  { id: "rtmscore", title: "RTMScore 重评分", engine: "RTMScore", gate: "仅重评分已有 CarsiDock 姿势；无姿势失败仍保留，不伪造补齐", output: "scores linked by molecular identity + ranking disagreement evidence" },
  { id: "uncertainty", title: "按不确定性选择少量候选", engine: null, gate: "冻结排序分歧阈值、几何疑点、多样性哨兵规则及每个候选的理由；禁止自动把全池送入 AF3", output: "explicit selected IDs + per-candidate reasons + evidence hashes" },
  { id: "af3", title: "AF3 定向复核", engine: "AlphaFold 3", gate: "仅审批后的不确定性子集；独立 GPU/输入/模型门禁，mPAE 不是亲和力", output: "placement confidence + coverage/failure report" },
  { id: "covdock", title: "CovDock 精筛", engine: "CovDock-LO", gate: "仅批准的 finalists/未解决冲突；许可证实际检出与 CPU 预算门禁，不全库运行", output: "refined poses + geometry gates + failures" },
  { id: "finalize", title: "汇总与人工复核", engine: null, gate: "保留全流程来源、失败分母、证据和实际消耗；不直接混加各引擎原始分数", output: "auditable shortlist, not experimentally validated hits" },
] as const

const reportSchema = z.object({ target_registered: z.boolean(), source: z.object({ exists: z.boolean() }).passthrough(),
  engine_assets_present: z.record(z.string(), z.boolean()), existing_project_route: z.string().nullable(),
  submitted: z.literal(false), engine_runtime_verified: z.literal(false) }).passthrough()

export function compile(input: Input, report: z.infer<typeof reportSchema>) {
  const missing = ["scope", "shortlist", "gpu_minutes", "cpu_minutes", "af3_max", "covdock_max"]
    .filter((key) => input[key as keyof Input] === undefined)
  const blockers = [
    ...(!report.target_registered ? ["Target/site is not uniquely registered"] : []),
    ...(!report.source.exists ? ["Requested virtual-screening library is unavailable"] : []),
    ...missing.map((key) => `Explicit scope/budget required: ${key}`),
    "New campaign, source hash and receptor/site audit have not been frozen; legacy seeds are never reused",
    "Uncertainty thresholds, selected IDs and their evidence must be derived after RTMScore, not fabricated now",
    "Full-chain native-approved dispatch adapters are not yet validated; this is a planning/inspection tool, not a job submission tool",
  ]
  return { protocol: "library-first-uncertainty-v2", status: "DRAFT_NOT_EXECUTABLE", route: "library-first",
    target: input.target, site: input.site, source_library: "virtual_screening", scope: input.scope ?? null,
    limits: { max_records: input.max_records ?? null, shortlist: input.shortlist ?? null,
      gpu_minutes: input.gpu_minutes ?? null, cpu_minutes: input.cpu_minutes ?? null,
      af3_max: input.af3_max ?? null, covdock_max: input.covdock_max ?? null },
    stages: stages.map((stage) => ({ ...stage, state: stage.id === "intake" ? "NEEDS_INPUT_REVIEW" : "WAITING_FOR_PARENT",
      assets_present: stage.id in report.engine_assets_present ? report.engine_assets_present[stage.id] : null,
      runtime_verified: false })),
    blockers, existing_project_route: report.existing_project_route,
    route_note: report.existing_project_route !== "library-first" ? "Create an isolated Library-first campaign; do not overwrite the existing project route or reuse its outputs" : null,
    reuse_legacy_pilot: false, submitted: false, approved: false, dispatch_enabled: false,
    inspection: report }
}

async function inspect(input: Input, context: ToolContext) {
  const config = z.object({ cli: z.string().startsWith("/"), account: z.string(), cpu_notebook: z.string(),
    shared_root: z.string().optional(), cpu_workspace: z.literal("CPU资源空间") }).parse(await Bun.file(process.env.COVSCREEN_SCREENING_CONFIG ?? path.join(root, "configs/inspire-screening.json")).json())
  const payload = JSON.stringify({ target: input.target, site: input.site })
  const shared = legacySharedRoot(config)
  const command = `export COVSCREEN_LEGACY_SHARED_ROOT=${quote(shared)} COVSCREEN_RUNTIME_ROOT=${quote(shared + "/2_agent")} && cd ${quote(shared + "/2_agent/covalent-library-first")} && source environment/activate_carsidock.sh >/dev/null && python ${quote(shared + "/2_agent/openscience_inspire_screening_v1/workflow_inspect_v2.py")} ${quote(payload)}`
  if (context.abort.aborted) throw new Error("Cancelled")
  const child = Bun.spawn([config.cli, "--json", "--no-env-file", "--account", config.account, "notebook", "exec",
    config.cpu_notebook, "--workspace", config.cpu_workspace, "--timeout", "60", command], { stdin: "ignore", stdout: "pipe", stderr: "pipe" })
  const kill = () => child.kill("SIGTERM")
  context.abort.addEventListener("abort", kill, { once: true })
  const timer = setTimeout(() => child.kill("SIGKILL"), 85000)
  try {
    const [stdout, , code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
    if (code) throw new Error("InspireSkill read-only inspection failed; no job submitted. Check the CPU connection.")
    const envelope = z.object({ success: z.literal(true), data: z.object({ returncode: z.literal(0), output: z.string() }) }).parse(JSON.parse(stdout))
    return reportSchema.parse(JSON.parse(envelope.data.output))
  } finally {
    clearTimeout(timer)
    context.abort.removeEventListener("abort", kill)
  }
}

export async function execute(input: unknown, context: ToolContext) {
  const args = schema.parse(input)
  const defaults = z.object({ target: z.string(), site: z.string(), source_library: z.literal("virtual_screening") })
    .parse(await Bun.file(path.join(root, "configs/inspire-library-workflow.json")).json())
  if (args.action === "describe") return JSON.stringify({ protocol: "library-first-uncertainty-v2", stages, confirmed_request: defaults,
    scope: "not yet frozen", connection: "realZillionX/InspireSkill", legacy_pilot_is_default: false,
    dispatch_enabled: false, note: "Describe strategy separately from actual execution. Use inspect/draft; no implied 130-molecule default." })
  const selected = schema.parse({ ...defaults, ...args })
  const report = await inspect(selected, context)
  if (args.action === "inspect") return JSON.stringify({ requested_route: "library-first", ...report })
  const body = { ...compile(selected, report), id: "os-library-" + randomBytes(12).toString("hex"),
    session: context.sessionID, directory: context.directory, created_utc: new Date().toISOString() }
  const draft = { ...body, digest: sha(body) }
  const directory = path.join(root, ".cache/inspire-library-workflow")
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const file = path.join(directory, draft.id + ".json")
  await writeFile(file, JSON.stringify(draft, null, 2), { flag: "wx", mode: 0o600 })
  return JSON.stringify({ ...draft, file, note: "Draft saved locally, not an execution approval. Never pass this draft to the legacy pilot tool." })
}
