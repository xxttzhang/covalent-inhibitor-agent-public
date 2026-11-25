import path from "node:path"
import z from "zod"

export const base = path.resolve(process.env.COVSCREEN_MD_ROOT ?? path.resolve(import.meta.dir, "../../../../.."))
export const folder = path.join(base, "reports/library-md-20261008-v1/platform")
export const hash = (bytes: string | Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")

export const profile = z.object({
  schema: z.literal("registered-library-md-v1"),
  directory: z.literal(base),
  engine: z.literal("OpenMM"), forcefield: z.literal("OPLS_2005"),
  chemical_state: z.literal("covalent_adduct"),
  systems: z.array(z.string().regex(/^library_\d{3}$/)).length(8),
  replicas_per_system: z.literal(3), production_ns: z.literal(10),
  max_gpus: z.literal(8), max_gpu_hours: z.literal(60),
  account: z.literal("default"), workspace: z.literal("分布式训练空间"),
  project: z.literal("原子级化学反应基座模型2.0"),
  remote_root: z.string().startsWith("/inspire/").refine(value => {
    const prefix = process.env.COVSCREEN_MD_REMOTE_PREFIX
    return !!prefix?.startsWith("/inspire/") && prefix.endsWith("/") && path.posix.normalize(prefix) === prefix
      && value.startsWith(prefix) && path.posix.normalize(value) === value
  }, "Configure the exact COVSCREEN_MD_REMOTE_PREFIX for this installation"),
  local_python: z.string(), runner: z.string(), runner_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  plan: z.string(), plan_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  helpers: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)),
  recovery_receipts: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)).optional(),
  readiness: z.literal("FROZEN_INPUTS_VERIFIED"),
}).strict()

export async function registered(directory: string) {
  const file = Bun.file(path.join(folder, "profile.json"))
  if (!await file.exists()) throw Error("MD profile is not ready: scientific input preparation/validation is still in progress. No jobs submitted.")
  const settings = profile.parse(await file.json())
  if (![settings.directory, path.join(settings.directory, "openscience-custom")].includes(path.resolve(directory))) throw Error("This project is not registered for the Library-first MD batch")
  for (const [filename, expected] of [[settings.runner, settings.runner_sha256], [settings.plan, settings.plan_sha256]]) {
    if (!path.isAbsolute(filename) || !filename.startsWith(base + path.sep)) throw Error("Registered input escapes this installation")
    if (hash(new Uint8Array(await Bun.file(filename).arrayBuffer())) !== expected) throw Error("Registered MD input changed")
  }
  return { settings, digest: hash(await file.text()) }
}

export function directRequest(text: string) {
  // This is only an intent filter. Exact-budget native permission is still required.
  if (/[?？]|是否|怎么|如何|为什么|吗|不要|停止|取消/.test(text)) return false
  if (/\b(?:don't|do not|never|stop|cancel|why|how|whether|should|could|would)\b/i.test(text)) return false
  return /(?:Library[-\s]?first|文库优先)/i.test(text)
    && /(?:\bMD\b|分子动力学)/i.test(text)
    && /(?:开始|运行|提交|启动|执行|run|start|submit)/i.test(text)
}

export async function invoke(action: "plan" | "status" | "start", authorization?: string) {
  const { settings } = await registered(base)
  const command = [settings.local_python, settings.runner, action]
  if (authorization) command.push("--authorization", authorization)
  const child = Bun.spawn(command, { cwd: base, stdout: "pipe", stderr: "pipe", env: { ...Bun.env, PYTHONDONTWRITEBYTECODE: "1" } })
  // Submission includes live resource checks and a batch API call (up to 300 s).
  // Do not kill the controller before its bounded submission/reconciliation path.
  const timer = setTimeout(() => child.kill(), action === "start" ? 900_000 : 180_000)
  const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  clearTimeout(timer)
  if (code !== 0) throw Error("Registered MD controller: " + stderr.slice(-1800) + stdout.slice(-1800))
  return JSON.parse(stdout) as Record<string, unknown>
}
