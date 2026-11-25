import path from "node:path"
import { readFile, realpath, writeFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { z } from "zod"
import { studyRoutes, type DiscoveryStudy } from "@synsci/util/discovery-study"
import { studyExecutionContract } from "./study-execution-contract"

const digest = (value: string) => createHash("sha256").update(value).digest("hex")
const source = z.string().regex(/^results\/target-hypotheses\/hyp-[a-f0-9]{24}\/proposal-[a-f0-9]{12}\/hypothesis\.json$/)
const proposal = z.object({
  schema_version: z.literal("target-hypothesis-v1"),
  target: z.object({ symbol: z.string(), site: z.string(), taxon: z.number(), uniprot_id: z.string() }).passthrough(),
  hypothesis: z.object({ study_id: z.string(), route: z.enum(studyRoutes) }).passthrough(),
  handoff: z.object({ state: z.string(), compute_submitted: z.literal(false) }).passthrough(),
}).passthrough()

async function inside(directory: string, relative: string) {
  const root = await realpath(directory)
  const filename = await realpath(path.join(root, relative))
  if (!filename.startsWith(root + path.sep)) throw Error("Hypothesis path escapes project")
  return filename
}

function validate(bytes: string, plan: DiscoveryStudy) {
  const value = proposal.parse(JSON.parse(bytes))
  if (value.hypothesis.study_id !== plan.id) throw Error("Hypothesis study mismatch")
  if (!plan.routes[value.hypothesis.route].selected) throw Error("Hypothesis route not selected")
  const residue = plan.target.site.residue.toUpperCase().replace(/^CYS$/, "C")
  if (value.target.symbol !== plan.target.gene || value.target.uniprot_id !== plan.target.uniprot
    || value.target.taxon !== plan.target.taxon || value.target.site !== residue + plan.target.site.number
    || plan.target.site.numbering !== "canonical") throw Error("Hypothesis target/site/species mismatch or unmapped structure numbering")
  // This legacy proposal has no chain/receptor/pocket binding. Never certify execution readiness from it.
  return value
}

export async function readHypothesis(directory: string, plan: DiscoveryStudy, route?: typeof studyRoutes[number]) {
  if (route && !studyRoutes.includes(route)) throw Error("Unknown hypothesis route")
  const scoped = `.discovery/studies/${plan.id}/${route}.hypothesis.import.json`
  const relative = route && await Bun.file(path.join(directory, scoped)).exists()
    ? scoped : `.discovery/studies/${plan.id}/hypothesis.import.json`
  if (!await Bun.file(path.join(directory, relative)).exists()) return null
  const receipt = z.object({ schema: z.literal("study-hypothesis-import-v1"), study: z.string(), project: z.string(),
    source, sha256: z.string(), original: z.string(), importedAt: z.string().datetime(),
  }).strict().parse(JSON.parse(await readFile(await inside(directory, relative), "utf8")))
  if (receipt.study !== plan.id || receipt.project !== plan.project || digest(receipt.original) !== receipt.sha256) {
    throw Error("Hypothesis snapshot identity/hash mismatch")
  }
  const value = validate(receipt.original, plan)
  if (route && value.hypothesis.route !== route) {
    if (relative === scoped) throw Error("Hypothesis snapshot route mismatch")
    return null
  }
  return { ...receipt, value, executionBinding: "pending-chain-receptor-route-input-validation",
    contractValidation: studyExecutionContract(plan, receipt.original), newComputeSubmitted: false }
}

export async function readHypotheses(directory: string, plan: DiscoveryStudy) {
  const values = await Promise.all(studyRoutes.map(route => readHypothesis(directory, plan, route)))
  return values.filter(value => value !== null)
}

export async function attachHypothesis(directory: string, plan: DiscoveryStudy, filename: string) {
  source.parse(filename)
  const attachments = await readHypotheses(directory, plan)
  const repeated = attachments.find(value => value.source === filename)
  if (repeated) return repeated
  const original = await readFile(await inside(directory, filename), "utf8")
  const value = validate(original, plan)
  const route = value.hypothesis.route
  const existing = attachments.find(value => value.value.hypothesis.route === route)
  if (existing) {
    if (existing.source !== filename) throw Error("Route already has a frozen hypothesis; create a new study for another proposal")
    return existing
  }
  const folder = await inside(directory, `.discovery/studies/${plan.id}`)
  const receipt = { schema: "study-hypothesis-import-v1", study: plan.id, project: plan.project,
    source: filename, sha256: digest(original), original, importedAt: new Date().toISOString() }
  try { await writeFile(path.join(folder, route + ".hypothesis.import.json"), JSON.stringify(receipt, null, 2), { flag: "wx" }) }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error }
  const saved = await readHypothesis(directory, plan, route)
  if (saved?.sha256 !== receipt.sha256) throw Error("Concurrent hypothesis attachment differs")
  return saved
}

/** Discover only proposals explicitly bound to this study; never infer a match from the target alone. */
export async function listHypotheses(directory: string, plan: DiscoveryStudy) {
  const paths = await Array.fromAsync(new Bun.Glob("results/target-hypotheses/hyp-*/proposal-*/hypothesis.json").scan({ cwd: directory, onlyFiles: true, followSymlinks: false }))
  const results = await Promise.all(paths.map(async filename => {
    try {
      source.parse(filename)
      const original = await readFile(await inside(directory, filename), "utf8")
      const value = validate(original, plan)
      return { source: filename, sha256: digest(original), route: value.hypothesis.route, state: value.handoff.state }
    } catch { return null }
  }))
  return results.filter(value => value !== null)
}

/** Only unambiguous, explicitly study-bound outputs may be automatically frozen. */
export async function syncHypotheses(directory: string, plan: DiscoveryStudy) {
  const proposals = await listHypotheses(directory, plan)
  const existing = await readHypotheses(directory, plan)
  for (const route of studyRoutes) {
    if (existing.some(item => item.value.hypothesis.route === route)) continue
    const matches = proposals.filter(item => item.route === route)
    if (matches.length === 1) await attachHypothesis(directory, plan, matches[0].source)
  }
  return readHypotheses(directory, plan)
}
