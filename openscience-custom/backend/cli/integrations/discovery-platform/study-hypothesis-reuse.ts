import path from "node:path"
import { realpath, writeFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { z } from "zod"
import { study, studyRoutes, type DiscoveryStudy } from "@synsci/util/discovery-study"
import { listStudies, readStudy } from "./study-store"
import { readHypothesis } from "./study-hypothesis"

const receipt = z.object({
  schema: z.literal("study-hypothesis-reuse-v1"), study: study.shape.id, project: z.string(),
  sourcePlan: study, source: z.string(), sha256: z.string().regex(/^[a-f0-9]{64}$/), original: z.string(),
  route: z.enum(studyRoutes), importedAt: z.string().datetime(), executionEligible: z.literal(false),
}).strict()

function compatible(plan: DiscoveryStudy, source: DiscoveryStudy, route: typeof studyRoutes[number]) {
  if (plan.id === source.id || plan.project !== source.project || !plan.routes[route].selected) throw Error("Invalid reuse study/project/route")
  const a = plan.target
  const b = source.target
  if (a.gene !== b.gene || a.uniprot !== b.uniprot || a.taxon !== b.taxon ||
    a.site.chain !== b.site.chain || a.site.residue !== b.site.residue || a.site.number !== b.site.number ||
    a.site.atom !== b.site.atom || a.site.numbering !== b.site.numbering) throw Error("Reuse target/site/species mismatch")
}

async function folder(directory: string, plan: DiscoveryStudy) {
  const root = await realpath(directory)
  const result = await realpath(path.join(root, ".discovery/studies", plan.id))
  if (!result.startsWith(root + path.sep)) throw Error("Reuse path escapes project")
  return result
}

export async function readHypothesisReuses(directory: string, plan: DiscoveryStudy) {
  const root = await folder(directory, plan)
  const values = await Promise.all(studyRoutes.map(async route => {
    const filename = path.join(root, `${route}.hypothesis.reuse.json`)
    if (!await Bun.file(filename).exists()) return null
    if (!((await realpath(filename)).startsWith(root + path.sep))) throw Error("Reuse receipt escapes study")
    const value = receipt.parse(await Bun.file(filename).json())
    compatible(plan, value.sourcePlan, route)
    if (value.study !== plan.id || value.project !== plan.project || value.route !== route ||
      createHash("sha256").update(value.original).digest("hex") !== value.sha256) throw Error("Reuse identity/hash mismatch")
    const payload = JSON.parse(value.original)
    if (payload.hypothesis.study_id !== value.sourcePlan.id || payload.hypothesis.route !== route) throw Error("Reuse origin mismatch")
    return value
  }))
  return values.filter(value => value !== null)
}

export async function listReusableHypotheses(directory: string, plan: DiscoveryStudy) {
  const plans = await listStudies(directory, plan.project)
  const values = await Promise.all(plans.flatMap(source => studyRoutes.map(async route => {
    try { compatible(plan, source, route) } catch { return null }
    const attachment = await readHypothesis(directory, source, route)
    if (!attachment) return null
    return { sourceStudy: source.id, title: source.title, route, sha256: attachment.sha256,
      source: attachment.source, receptor: source.target.receptor.reference }
  })))
  return values.filter(value => value !== null)
}

/** Explicit evidence-only reuse; never creates a native hypothesis or execution plan. */
export async function reuseHypothesis(directory: string, plan: DiscoveryStudy, sourceID: string, route: typeof studyRoutes[number], sha256: string) {
  const sourcePlan = await readStudy(directory, plan.project, sourceID)
  compatible(plan, sourcePlan, route)
  const attachment = await readHypothesis(directory, sourcePlan, route)
  if (!attachment || attachment.sha256 !== sha256) throw Error("Reuse source missing or changed; refresh selection")
  const value = receipt.parse({ schema: "study-hypothesis-reuse-v1", study: plan.id, project: plan.project,
    sourcePlan, source: attachment.source, sha256, original: attachment.original, route,
    importedAt: new Date().toISOString(), executionEligible: false })
  const root = await folder(directory, plan)
  try { await writeFile(path.join(root, `${route}.hypothesis.reuse.json`), JSON.stringify(value, null, 2), { flag: "wx" }) }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error }
  const saved = (await readHypothesisReuses(directory, plan)).find(item => item.route === route)
  if (saved?.sha256 !== sha256 || saved.sourcePlan.id !== sourceID) throw Error("Route already has another frozen reuse")
  return saved
}
