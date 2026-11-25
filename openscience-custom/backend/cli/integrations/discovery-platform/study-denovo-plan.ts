import path from "node:path"
import { realpath } from "node:fs/promises"
import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { digest } from "../inspire-screening/executor/model"
import { readHypothesis } from "./study-hypothesis"
import { readReasoning } from "./study-reasoning"
import { compileStudyDeNovo } from "./study-denovo-compiler"
import { registeredDeNovoSource } from "../../src/tool/denovo-source"
import { publishFrozenJSON } from "./frozen-json"

export async function registeredGenerationPackage() {
  return (await registeredDeNovoSource()).package
}

const receipt = z.object({ schema: z.literal("study-denovo-plan-v1"), study: z.string(), project: z.string(),
  createdAt: z.string().datetime(), studyDigest: z.string(), hypothesisDigest: z.string(), planDigest: z.string(),
  plan: z.object({ state: z.literal("PLAN_COMPILED_NOT_APPROVED"), compiled: z.literal(true),
    approved: z.literal(false), submitted: z.literal(false), study: z.string(),
    sourceKind: z.literal("immutable-study-not-native-compute-request"), request: z.record(z.string(), z.unknown()),
    inputHashes: z.object({ receptor: z.string(), pocket: z.string() }), pending: z.array(z.string()) }).strict(),
}).strict()
async function folder(directory: string, study: DiscoveryStudy) {
  const root = await realpath(directory)
  const result = await realpath(path.join(root, ".discovery/studies", study.id))
  if (!result.startsWith(root + path.sep)) throw Error("Study directory escapes project")
  return result
}
export async function readDeNovoPlan(directory: string, study: DiscoveryStudy) {
  const filename = path.join(await folder(directory, study), "denovo-execution-plan.json")
  if (!await Bun.file(filename).exists()) return null
  if (await realpath(filename) !== filename) throw Error("De novo receipt must not be a symlink")
  const saved = receipt.parse(await Bun.file(filename).json())
  const hypothesis = await readHypothesis(directory, study, "de-novo")
  if (saved.study !== study.id || saved.plan.study !== study.id || saved.project !== study.project
    || saved.studyDigest !== digest(study) || saved.hypothesisDigest !== hypothesis?.sha256 || saved.planDigest !== digest(saved.plan))
    throw Error("De novo plan identity or evidence changed")
  return saved
}
export async function compileDeNovoPlan(directory: string, study: DiscoveryStudy) {
  const previous = await readDeNovoPlan(directory, study)
  if (previous) return previous
  const hypothesis = await readHypothesis(directory, study, "de-novo")
  if (!hypothesis) throw Error("Attach this study's De novo hypothesis first")
  const reasoning = await readReasoning(directory, study)
  if (!reasoning) throw Error("Study reasoning session required")
  const parent = await folder(directory, study)
  {
    // Registered host asset path, never supplied as an executable path by the model.
    const source = await registeredDeNovoSource()
    const plan = await compileStudyDeNovo(study, hypothesis.original, { directory, session: reasoning.session,
      package: source.package, worker: source.worker, source_release: source.source_release })
    // Match the JSON representation used for both hashing and persisted verification.
    const canonical = JSON.parse(JSON.stringify(plan))
    const saved = receipt.parse({ schema: "study-denovo-plan-v1", study: study.id, project: study.project,
      createdAt: new Date().toISOString(), studyDigest: digest(study), hypothesisDigest: hypothesis.sha256,
      planDigest: digest(canonical), plan: canonical })
    await publishFrozenJSON(path.join(parent, "denovo-execution-plan.json"), saved)
    // Concurrent read-only compilers may produce different task IDs. Every caller
    // must return the same published winner, never its unpublished local plan.
    const published = await readDeNovoPlan(directory, study)
    if (!published) throw Error("Published De novo receipt is missing")
    return published
  }
}
