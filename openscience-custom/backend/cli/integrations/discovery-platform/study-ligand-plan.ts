import path from "node:path"
import { realpath, writeFile } from "node:fs/promises"
import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { digest } from "../inspire-screening/executor/model"
import { readHypothesis } from "./study-hypothesis"
import { studyLigandContract } from "./study-ligand-contract"

const receipt = z.object({ schema: z.literal("study-ligand-plan-v1"), study: z.string(), project: z.string(),
  createdAt: z.string().datetime(), studyDigest: z.string(), hypothesisDigest: z.string(), planDigest: z.string(),
  approved: z.literal(false), submitted: z.literal(false), evidence: z.literal("upstream-compiler-snapshot"),
  plan: z.record(z.string(), z.unknown()),
}).strict()
async function filename(directory: string, study: DiscoveryStudy) {
  const root = await realpath(directory)
  const folder = await realpath(path.join(root, ".discovery/studies", study.id))
  if (!folder.startsWith(root + path.sep)) throw Error("Ligand study folder escapes project")
  return path.join(folder, "ligand-execution-plan.json")
}
export async function readLigandPlan(directory: string, study: DiscoveryStudy) {
  const file = await filename(directory, study)
  if (!await Bun.file(file).exists()) return null
  if (await realpath(file) !== file) throw Error("Ligand plan must not be a symlink")
  const saved = receipt.parse(await Bun.file(file).json())
  const hypothesis = await readHypothesis(directory, study, "ligand-first")
  if (saved.study !== study.id || saved.project !== study.project || saved.studyDigest !== digest(study)
    || saved.hypothesisDigest !== hypothesis?.sha256 || saved.planDigest !== digest(saved.plan))
    throw Error("Ligand plan identity or evidence changed")
  return saved
}
/** Freeze the actual upstream compiler response only after explicit study binding.
 * This does not re-run the compiler, revalidate remote files, or submit a stage. */
export async function bindLigandPlan(directory: string, study: DiscoveryStudy) {
  const previous = await readLigandPlan(directory, study)
  if (previous) return previous
  const hypothesis = await readHypothesis(directory, study, "ligand-first")
  if (!hypothesis) throw Error("Attach this study's Ligand-first hypothesis first")
  const validated = studyLigandContract(study, hypothesis.original)
  if (validated.state !== "UPSTREAM_PLAN_BINDING_VALIDATED") throw Error(validated.blockers.join("; "))
  const plan = JSON.parse(hypothesis.original).handoff.route_plan
  const saved = receipt.parse({ schema: "study-ligand-plan-v1", study: study.id, project: study.project,
    createdAt: new Date().toISOString(), studyDigest: digest(study), hypothesisDigest: hypothesis.sha256,
    planDigest: digest(plan), plan, approved: false, submitted: false, evidence: "upstream-compiler-snapshot" })
  try { await writeFile(await filename(directory, study), JSON.stringify(saved, null, 2), { flag: "wx" }) }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error }
  const final = await readLigandPlan(directory, study)
  if (final?.planDigest !== saved.planDigest) throw Error("Concurrent Ligand plan differs")
  return final
}
