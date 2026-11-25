import path from "node:path"
import { realpath, writeFile, unlink } from "node:fs/promises"
import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { digest, planSchema, resource } from "../inspire-screening/executor/model"
import { Inspire } from "../inspire-screening/executor/inspire"
import { compileStudyLibrary } from "./study-library-compiler"
import { readHypothesis } from "./study-hypothesis"
import { studyExecutionContract } from "./study-execution-contract"
import { readReasoning } from "./study-reasoning"

const receipt = z.object({ schema: z.literal("study-library-plan-v1"), study: z.string(), project: z.string(),
  createdAt: z.string().datetime(), studyDigest: z.string(), hypothesisDigest: z.string(),
  planDigest: z.string(), plan: planSchema, approved: z.literal(false), submitted: z.literal(false),
  sourceKind: z.literal("immutable-study-not-native-compute-request"),
  evidence: z.literal("live-asset-snapshot-and-quota-rates"),
}).strict()

async function folder(directory: string, plan: DiscoveryStudy) {
  const root = await realpath(directory)
  const parent = await realpath(path.join(root, ".discovery/studies", plan.id))
  if (!parent.startsWith(root + path.sep)) throw Error("Study plan path escapes project")
  return parent
}

export async function readLibraryPlan(directory: string, plan: DiscoveryStudy) {
  const filename = path.join(await folder(directory, plan), "library-execution-plan.json")
  if (!await Bun.file(filename).exists()) return null
  if (await realpath(filename) !== filename) throw Error("Plan receipt must not be a symlink")
  const saved = receipt.parse(await Bun.file(filename).json())
  const hypothesis = await readHypothesis(directory, plan, "library-first")
  if (saved.study !== plan.id || saved.project !== plan.project || saved.studyDigest !== digest(plan)
    || saved.planDigest !== digest(saved.plan) || saved.hypothesisDigest !== hypothesis?.sha256)
    throw Error("Compiled plan identity or evidence changed")
  return saved
}

/** Requires trusted host remote-read authorization. No create/freeze/submit call,
 * no task database, no daemon, no approval. A saved preview is not dispatchable. */
export async function compileLiveLibrary(directory: string, plan: DiscoveryStudy, authorize: () => Promise<void>) {
  await authorize()
  const previous = await readLibraryPlan(directory, plan)
  if (previous) return previous
  const route = plan.routes["library-first"]
  if (!route.selected || route.mode !== "new") throw Error("Select a new Library-first plan; historical reuse never compiles automatically")
  const hypothesis = await readHypothesis(directory, plan, "library-first")
  if (!hypothesis) throw Error("Attach this study's Library-first hypothesis first")
  const contract = studyExecutionContract(plan, hypothesis.original)
  if (contract.state !== "CONTRACT_VALIDATED_NOT_COMPILED") throw Error(contract.blockers.join("; "))
  const reasoning = await readReasoning(directory, plan)
  if (!reasoning) throw Error("Start the study reasoning session before compiling")
  const parent = await folder(directory, plan)
  const lock = path.join(parent, "library-plan.lock")
  await writeFile(lock, "Read-only plan compilation in progress", { flag: "wx" })
  try {
    // Fixed host configuration, not a path or command provided by a model.
    const config = z.object({ connection: planSchema.shape.connection, runtime: planSchema.shape.runtime,
      cpu: z.object(resource.shape).omit({ points_per_hour: true }), gpu: z.object(resource.shape).omit({ points_per_hour: true }) }).strict().parse(
        await Bun.file(path.resolve(import.meta.dir, "../../../../../configs/inspire-executor.json")).json())
    const transport = new Inspire(config.connection)
    const snapshot = z.object({ assets: planSchema.shape.assets, runtime: planSchema.shape.runtime }).passthrough().parse(
      await transport.remote(config.runtime, ["snapshot"]))
    if (digest(snapshot.runtime) !== digest(config.runtime)) throw Error("Remote snapshot runtime differs from host configuration")
    const cpu = resource.parse({ ...config.cpu, points_per_hour: 0 })
    const gpu = resource.parse({ ...config.gpu, points_per_hour: 0 })
    const [cpuRate, gpuRate] = await Promise.all([transport.live(cpu), transport.live(gpu)])
    const compiled = compileStudyLibrary(plan, hypothesis.original, { directory, session: reasoning.session,
      source: { text: JSON.stringify(plan) }, runtime: config.runtime, connection: config.connection, assets: snapshot.assets,
      cpu: { ...cpu, points_per_hour: cpuRate }, gpu: { ...gpu, points_per_hour: gpuRate } })
    if (!compiled.compiled) throw Error(compiled.blockers.join("; "))
    const saved = receipt.parse({ schema: "study-library-plan-v1", study: plan.id, project: plan.project,
      createdAt: new Date().toISOString(), studyDigest: digest(plan), hypothesisDigest: hypothesis.sha256,
      planDigest: digest(compiled.plan), plan: compiled.plan, approved: false, submitted: false,
      sourceKind: "immutable-study-not-native-compute-request", evidence: "live-asset-snapshot-and-quota-rates" })
    await writeFile(path.join(parent, "library-execution-plan.json"), JSON.stringify(saved, null, 2), { flag: "wx" })
    return saved
  } finally { await unlink(lock) }
}
