import z from "zod"
import path from "node:path"
import { mkdir, readFile, writeFile } from "node:fs/promises"
import { createHash, randomBytes } from "node:crypto"
import type { ToolContext } from "@synsci/plugin/tool"

const root = path.resolve(import.meta.dir, "../../../../..")
const campaign = "egfr-c797-covindb-pilot-v1"
const storage = path.join(root, ".cache/inspire-screening")
const configSchema = z.object({
  shared_root: z.string().startsWith("/inspire/").optional(),
  cli: z.string().startsWith("/"), account: z.string().min(1), cpu_notebook: z.string().min(1),
  cpu_workspace: z.literal("CPU资源空间"), workspace: z.literal("分布式训练空间"),
  project: z.string().min(1), group: z.string().min(1), quota: z.string().regex(/^1,\d+,\d+$/),
  image: z.string().min(1), priority: z.number().int().min(1).max(10),
  max_candidates: z.number().int().min(1).max(500), max_minutes: z.number().int().min(1).max(240),
}).strict()
type Settings = z.infer<typeof configSchema>
const assetsSchema = z.object({ campaign: z.literal(campaign), prepared_candidates: z.number().int().positive(),
  hashes: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)), reaction: z.string(),
  canonical_site: z.string(), structure_site: z.string(), torch: z.string(), cuda_build: z.string().nullable(),
  gpu_execution_verified: z.literal(false), scope: z.string(),
})
const quotaSchema = z.object({ compute_group: z.string(), quota: z.string(),
  points_per_hour: z.number().nonnegative().nullable(), priority: z.string(),
  allowed_priority_levels: z.array(z.number()).nullable(),
})
export const inputSchema = z.object({
  action: z.enum(["targets", "preflight", "plan", "start", "status", "logs", "artifacts", "cancel"]),
  candidates: z.number().int().min(1).max(500).optional(),
  minutes: z.number().int().min(1).max(240).optional(),
  plan_id: z.string().regex(/^os-cov-[a-f0-9]{24}$/).optional(),
}).strict().superRefine((args, ctx) => {
  if (args.action === "plan" && (!args.candidates || !args.minutes)) ctx.addIssue({ code: "custom", message: "plan requires explicit candidates and minutes" })
  if (["start", "status", "logs", "artifacts", "cancel"].includes(args.action) && !args.plan_id) ctx.addIssue({ code: "custom", message: "This action requires a plan_id" })
  if (args.action !== "plan" && (args.candidates !== undefined || args.minutes !== undefined)) ctx.addIssue({ code: "custom", message: "A saved plan cannot be changed by start/status; create a new plan instead" })
})
type Input = z.infer<typeof inputSchema>
type Assets = z.infer<typeof assetsSchema>
type Plan = { name: string; campaign: typeof campaign; session: string; directory: string; created: number;
  candidates: number; minutes: number; config: Settings; assets: Assets;
  points_per_hour: number; maximum_compute_points: number; digest: string }
const quote = (value: string) => "'" + value.replaceAll("'", "'\"'\"'") + "'"
export const hash = (value: unknown): string => createHash("sha256").update(JSON.stringify(value)).digest("hex")
export function legacySharedRoot(config: { shared_root?: string }) {
  const shared = config.shared_root ?? process.env.COVSCREEN_LEGACY_SHARED_ROOT
  if (!shared?.startsWith("/inspire/") || path.posix.normalize(shared) !== shared || /[\x00-\x1f]/.test(shared))
    throw Error("Configure an absolute normalized COVSCREEN_LEGACY_SHARED_ROOT or shared_root")
  return shared
}
const settings = async () => {
  const config = configSchema.parse(await Bun.file(process.env.COVSCREEN_SCREENING_CONFIG ?? path.join(root, "configs/inspire-screening.json")).json())
  return { ...config, shared_root: legacySharedRoot(config) }
}
const folder = (name: string) => {
  if (!/^os-cov-[a-f0-9]{24}$/.test(name)) throw new Error("Invalid plan ID")
  return path.join(storage, name)
}
const checked = (response: unknown) => z.object({ success: z.literal(true), data: z.unknown() }).parse(response).data

async function cli(config: Settings, args: string[], abort?: AbortSignal) {
  if (abort?.aborted) throw new Error("Request cancelled")
  const child = Bun.spawn([config.cli, "--json", "--no-env-file", "--account", config.account, ...args],
    { stdin: "ignore", stdout: "pipe", stderr: "pipe", env: process.env })
  const kill = () => child.kill("SIGTERM")
  abort?.addEventListener("abort", kill, { once: true })
  const timer = setTimeout(() => child.kill("SIGKILL"), 110000)
  try {
    const [stdout, , code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
    if (code !== 0) throw new Error(`InspireSkill ${args.slice(0, 2).join(" ")} failed. Check CLI login, quota and connectivity. A failed submission is NOT safe to repeat; inspect the saved job name first.`)
    return checked(JSON.parse(stdout))
  } finally {
    clearTimeout(timer)
    abort?.removeEventListener("abort", kill)
  }
}

async function remote(config: Settings, action: string, args: string[] = [], abort?: AbortSignal) {
  const shared = legacySharedRoot(config)
  const command = `export COVSCREEN_LEGACY_SHARED_ROOT=${quote(shared)} COVSCREEN_RUNTIME_ROOT=${quote(shared + "/2_agent")} && cd ${quote(shared + "/2_agent/science")} && source environment/activate_carsidock.sh >/dev/null && python ${quote(shared + "/2_agent/openscience_inspire_screening_v1/worker.py")} ${quote(action)} ${args.map(quote).join(" ")}`
  const output = z.object({ returncode: z.literal(0), output: z.string() }).parse(await cli(config,
    ["notebook", "exec", config.cpu_notebook, "--workspace", config.cpu_workspace, "--timeout", "90", command], abort))
  return JSON.parse(output.output) as unknown
}

async function quota(config: Settings, abort?: AbortSignal) {
  const result = z.object({ items: z.array(quotaSchema) }).parse(await cli(config,
    ["job", "quota", "--workspace", config.workspace, "--group", config.group, "--all"], abort))
  const row = result.items.find((row) => row.compute_group === config.group && row.quota === config.quota)
  if (!row || row.points_per_hour === null) throw new Error("Live quota or price unavailable. No job may be submitted.")
  if (row.allowed_priority_levels?.length && !row.allowed_priority_levels.includes(config.priority)) throw new Error("Configured priority is not allowed for this live quota.")
  return row
}

export function createArgs(plan: Plan, preparedHash: string, dry: boolean) {
  if (!/^[a-f0-9]{64}$/.test(preparedHash)) throw new Error("Invalid prepared input hash")
  const cpus = plan.config.quota.split(",")[1]
  const shared = legacySharedRoot(plan.config)
  const command = `export COVSCREEN_LEGACY_SHARED_ROOT=${quote(shared)} COVSCREEN_RUNTIME_ROOT=${quote(shared + "/2_agent")} && cd ${quote(shared + "/2_agent/science")} && source environment/activate_carsidock.sh >/dev/null && export OMP_NUM_THREADS=${cpus} OPENBLAS_NUM_THREADS=${cpus} && exec python ${quote(shared + "/2_agent/openscience_inspire_screening_v1/worker.py")} run ${quote(plan.name)} ${quote(preparedHash)}`
  return ["job", "create", "--name", plan.name, "--workspace", plan.config.workspace,
    "--project", plan.config.project, "--group", plan.config.group, "--quota", plan.config.quota,
    "--image", plan.config.image, "--nodes", "1", "--priority", String(plan.config.priority),
    "--max-time", String(plan.minutes / 60), "--shm-size", "8", "--no-auto-fault-tolerance",
    "--no-enable-notification", "--public-path-readonly", "--command", command, ...(dry ? ["--dry-run"] : [])]
}

export function assertPlan(plan: Plan, session: string, directory: string, now = Date.now()) {
  const { digest, ...body } = plan
  if (digest !== hash(body)) throw new Error("Saved plan integrity check failed")
  if (plan.session !== session || plan.directory !== directory) throw new Error("Plan belongs to a different session/project")
  if (now - plan.created > 30 * 60 * 1000) throw new Error("Plan expired. Refresh live resources and request a new approval.")
}

function publicPlan(plan: Plan) {
  return { plan_id: plan.name, digest: plan.digest, provider: "inspire", campaign,
    candidates: plan.candidates, minutes: plan.minutes, workspace: plan.config.workspace,
    project: plan.config.project, group: plan.config.group, quota: plan.config.quota,
    image: plan.config.image, priority: plan.config.priority, points_per_hour: plan.points_per_hour,
    maximum_compute_points: plan.maximum_compute_points,
    cost_note: "Live quota rate × one instance × maximum runtime; points, not USD. Not a billing guarantee or account-wide spending cap.",
    target: plan.assets.canonical_site, structure_site: plan.assets.structure_site,
    reaction: plan.assets.reaction, input_file_count: Object.keys(plan.assets.hashes).length,
    output_directory: `${legacySharedRoot(plan.config)}/2_agent/openscience_inspire_screening_v1/runs/${plan.name}`, approval_required: true,
    expires_utc: new Date(plan.created + 30 * 60000).toISOString(),
    execution: "Prepare the approved subset with original molecular IDs; run the existing registered CarsiDock-Cov engine on one GPU. No Modal and no whole-library scan.",
    gpu_execution_verified: false }
}

export async function execute(input: unknown, context: ToolContext) {
  const args = inputSchema.parse(input)
  const config = await settings()
  const abort = context.abort
  if (args.action === "targets") {
    const [rows, capacity] = await Promise.all([
      cli(config, ["job", "quota", "--workspace", config.workspace, "--group", config.group, "--all"], abort),
      cli(config, ["resources", "availability", "--workspace", config.workspace, "--group", config.group], abort),
    ])
    return JSON.stringify({ provider: "Inspire via realZillionX/InspireSkill", selected: config, rows, capacity, campaign,
      note: "Only existing prepared EGFR pilot inputs are registered. Plan/start require explicit candidate and runtime limits." })
  }
  if (args.action === "preflight") {
    const assets = assetsSchema.parse(await remote(config, "preflight", [], abort))
    return JSON.stringify({ ...assets, hashes: undefined, input_file_count: Object.keys(assets.hashes).length, assets_digest: hash(assets.hashes) })
  }
  if (args.action === "plan") {
    if (args.candidates! > config.max_candidates || args.minutes! > config.max_minutes) throw new Error("Requested scope exceeds configured integration limits")
    const [assets, row] = await Promise.all([
      remote(config, "preflight", [], abort).then((value) => assetsSchema.parse(value)), quota(config, abort),
    ])
    if (args.candidates! > assets.prepared_candidates) throw new Error(`Only ${assets.prepared_candidates} candidates are prepared; additional preprocessing requires a separate plan.`)
    const body: Omit<Plan, "digest"> = { name: "os-cov-" + randomBytes(12).toString("hex"), campaign, session: context.sessionID,
      directory: context.directory, created: Date.now(), candidates: args.candidates!, minutes: args.minutes!,
      config, assets, points_per_hour: row.points_per_hour!, maximum_compute_points: row.points_per_hour! * args.minutes! / 60 }
    const plan: Plan = { ...body, digest: hash(body) }
    // Resolves the live image, project and quota through InspireSkill, without dispatch.
    await cli(config, createArgs(plan, "0".repeat(64), true), abort)
    await mkdir(folder(plan.name), { recursive: true, mode: 0o700 })
    await writeFile(path.join(folder(plan.name), "plan.json"), JSON.stringify(plan), { flag: "wx", mode: 0o600 })
    return JSON.stringify(publicPlan(plan))
  }
  const plan = JSON.parse(await readFile(path.join(folder(args.plan_id!), "plan.json"), "utf8")) as Plan
  const { digest, ...body } = plan
  if (plan.name !== args.plan_id || digest !== hash(body)) throw new Error("Saved plan integrity check failed")
  if (plan.directory !== context.directory || plan.session !== context.sessionID) throw new Error("Plan belongs to another session/project")
  if (hash(plan.config) !== hash(config)) throw new Error("Connection settings changed; do not reuse the old plan")
  if (args.action === "artifacts") return JSON.stringify(await remote(config, "result", [plan.name], abort))
  if (args.action === "status" || args.action === "logs") {
    return JSON.stringify(await cli(config, ["job", args.action, plan.name, "--workspace", config.workspace,
      ...(args.action === "logs" ? ["--tail", "60", "--limit", "60"] : [])], abort))
  }
  if (args.action === "cancel") {
    await context.ask({ permission: "remote_compute", patterns: [hash({ action: "cancel", name: plan.name, digest: plan.digest })], always: [],
      metadata: { purpose: `Stop only Inspire GPU job ${plan.name}`, plan: publicPlan(plan) } })
    return JSON.stringify(await cli(config, ["job", "stop", plan.name, "--workspace", config.workspace], abort))
  }
  assertPlan(plan, context.sessionID, context.directory)
  context.metadata({ title: `审批 Inspire GPU：${plan.candidates} 个候选 / ${plan.minutes} 分钟`, metadata: { plan: publicPlan(plan) } })
  // Reuse the kernel's exact-plan boundary: even a global '*: allow' cannot
  // silently grant remote_compute. Never substitute a new permissive key.
  await context.ask({ permission: "remote_compute", patterns: [plan.digest], always: [],
    metadata: { compute: { ...publicPlan(plan), purpose: `EGFR C797：${plan.candidates} 个候选，CarsiDock-Cov 对接`,
      warning: "在 Inspire 项目内消耗 GPU 点券；不是美元报价。只运行此候选子集，到达运行时限终止，不自动重试。" } } })
  assertPlan(plan, context.sessionID, context.directory)
  if (abort.aborted) throw new Error("Cancelled before submission")
  const latest = await settings()
  if (hash(latest) !== hash(plan.config)) throw new Error("Settings changed during approval")
  const row = await quota(config, abort)
  if (row.points_per_hour !== plan.points_per_hour) throw new Error("Live price changed; create a new plan and approve again")
  // Atomic per-plan lock; never auto-retry an uncertain create response.
  await mkdir(path.join(folder(plan.name), "submission.lock"))
  const prepared = z.object({ prepared: z.literal(true), plan_sha256: z.string().regex(/^[a-f0-9]{64}$/) }).parse(
    await remote(config, "prepare", [JSON.stringify(plan)], abort))
  if (abort.aborted) throw new Error("Cancelled before dispatch; inputs retained without starting a job")
  await writeFile(path.join(folder(plan.name), "submission.json"), JSON.stringify({ phase: "dispatching", name: plan.name }), { mode: 0o600 })
  const outcome = await cli(config, createArgs(plan, prepared.plan_sha256, false), abort)
  await writeFile(path.join(folder(plan.name), "submission.json"), JSON.stringify({ phase: "submitted", name: plan.name, outcome }), { mode: 0o600 })
  return JSON.stringify({ submitted: true, plan_id: plan.name, outcome,
    next: "Use status/logs/artifacts with this plan_id. Job submission is not successful docking. Never start another job to inspect this one." })
}
