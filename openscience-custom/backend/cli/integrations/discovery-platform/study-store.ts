import path from "node:path"
import { mkdir, readFile, realpath, writeFile, readdir } from "node:fs/promises"
import { createHash, randomUUID } from "node:crypto"
import { discoveryProject, projectPath } from "@synsci/util/discovery-config"
import { study, studyRequest, importReceipt, importedResult, studyRoutes, inputReadiness, type StudyOverview, type DiscoveryStudy } from "@synsci/util/discovery-study"
import { candidatePool } from "./chemical-identity"
import { evaluationHandoff } from "@synsci/util/candidate-handoff"
import { studyDeNovoCandidates } from "./study-denovo-candidates"

const hash = (value: Uint8Array | string) => createHash("sha256").update(value).digest("hex")
async function inside(directory: string, relative: string) {
  const root = await realpath(directory)
  const filename = await realpath(path.join(root, projectPath.parse(relative)))
  if (!filename.startsWith(root + path.sep)) throw Error("Study source escapes project")
  return filename
}
async function location(directory: string, id: string) {
  study.shape.id.parse(id)
  return inside(directory, `.discovery/studies/${id}`)
}

export async function createStudy(directory: string, project: string, input: unknown) {
  const request = studyRequest.parse(input)
  const value = study.parse({ ...request, schema: "discovery-study-v1", project,
    id: "study-" + randomUUID(), createdAt: new Date().toISOString() })
  const root = await realpath(directory)
  await mkdir(path.join(root, ".discovery"), { recursive: true })
  const discovery = await inside(root, ".discovery")
  await mkdir(path.join(discovery, "studies"), { recursive: true })
  const resolved = await inside(root, ".discovery/studies")
  const folder = path.join(resolved, value.id)
  await mkdir(folder)
  await writeFile(path.join(folder, "plan.json"), JSON.stringify(value, null, 2), { flag: "wx" })
  return value
}

export async function readStudy(directory: string, project: string, id: string): Promise<DiscoveryStudy> {
  await location(directory, id)
  const plan = study.parse(JSON.parse(await readFile(await inside(directory, `.discovery/studies/${id}/plan.json`), "utf8")))
  if (plan.id !== id || plan.project !== project) throw Error("Study identity mismatch")
  if (plan.denovoOrigin) {
    if (plan.denovoOrigin.parent === plan.id) throw Error("Invalid branch ancestry")
    const parentFile = await inside(directory, `.discovery/studies/${plan.denovoOrigin.parent}/plan.json`)
    const parent = study.parse(JSON.parse(await readFile(parentFile, "utf8")))
    if (parent.denovoOrigin || parent.project !== project || parent.id !== plan.denovoOrigin.parent
      || hash(JSON.stringify(parent)) !== plan.denovoOrigin.parent_sha256) throw Error("Branch parent identity or hash differs")
    const { branchStudy } = await import("./study-denovo-branches")
    if (hash(JSON.stringify(branchStudy(parent, plan.denovoOrigin.branch))) !== hash(JSON.stringify(plan)))
      throw Error("Branch study differs from parent declaration")
  }
  return plan
}

export async function listStudies(directory: string, project: string) {
  const parent = path.join(directory, ".discovery/studies")
  const entries = await readdir(parent, { withFileTypes: true }).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return []
    throw error
  })
  return Promise.all(entries.filter(entry => entry.isDirectory() && study.shape.id.safeParse(entry.name).success)
    .map(entry => readStudy(directory, project, entry.name)))
}

/** Expose registered provenance for the form; never substitute study inputs. */
export async function registeredStudySource(directory: string, project: string) {
  if (!await Bun.file(path.join(directory, ".discovery/project.json")).exists()) return null
  const config = discoveryProject.parse(JSON.parse(await readFile(await inside(directory, ".discovery/project.json"), "utf8")))
  if (config.project !== project) throw Error("Registered source project mismatch")
  return { target: config.target, routes: studyRoutes.filter(route => Boolean(config.routes[route].dataPath)) }
}

/** Only registered current-project route outputs can enter this ingress. No shell or compute. */
export async function importHistorical(directory: string, project: string, id: string, route: typeof studyRoutes[number]) {
  if (!studyRoutes.includes(route)) throw Error("Unknown discovery route")
  const plan = await readStudy(directory, project, id)
  const folder = await location(directory, id)
  const receiptPath = path.join(folder, route + ".import.json")
  if (await Bun.file(receiptPath).exists()) {
    // A frozen import is independent of subsequent updates/removal of the live route result.
    return (await readImport(directory, project, id, route)).receipt
  }
  const config = discoveryProject.parse(JSON.parse(await readFile(await inside(directory, ".discovery/project.json"), "utf8")))
  if (config.project !== project || config.target?.id !== plan.target.id) throw Error("Registered project target does not match study; no default target substitution")
  const identity = config.target.identity
  if (!identity || !config.target.site) throw Error("Historical source needs registered species, UniProt and site numbering before a new import")
  if (identity.gene !== plan.target.gene || identity.uniprot !== plan.target.uniprot
    || identity.taxon !== plan.target.taxon || identity.numbering !== plan.target.site.numbering)
    throw Error("Registered target species/UniProt/numbering does not match study")
  if (config.target.site && (config.target.site.chain !== plan.target.site.chain
    || config.target.site.number !== plan.target.site.number || config.target.site.atom !== plan.target.site.atom
    || config.target.site.residue.toUpperCase().replace(/^CYS$/, "C") !== plan.target.site.residue.toUpperCase().replace(/^CYS$/, "C"))) {
    throw Error("Registered target site does not match study")
  }
  const source = config.routes[route].dataPath
  if (!source) throw Error("Route has no registered result source")
  const bytes = await readFile(await inside(directory, source))
  const data = importedResult(JSON.parse(bytes.toString("utf8")), plan, route)
  const digest = hash(bytes)
  const snapshot = `.discovery/studies/${id}/${route}-${digest}.json`
  const destination = path.join(folder, route + "-" + digest + ".json")
  try { await writeFile(destination, bytes, { flag: "wx" }) }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
    if (hash(await readFile(destination)) !== digest) throw Error("Existing snapshot differs")
  }
  const receipt = importReceipt.parse({ schema: "discovery-study-import-v1", study: id, project, target: data.target,
    targetIdentity: { gene: identity.gene, uniprot: identity.uniprot, taxon: identity.taxon,
      site: { ...config.target.site, numbering: identity.numbering } },
    route, importedAt: new Date().toISOString(), mode: "historical-reuse", snapshot, candidates: data.candidates.length,
    source: { path: source, sha256: digest, project: data.project, run: data.run, evidenceUpdatedAt: data.evidenceUpdatedAt },
    newComputeSubmitted: false })
  try { await writeFile(receiptPath, JSON.stringify(receipt, null, 2), { flag: "wx" }) }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
    return (await readImport(directory, project, id, route)).receipt
  }
  return receipt
}

export async function readImport(directory: string, project: string, id: string, route: typeof studyRoutes[number]) {
  if (!studyRoutes.includes(route)) throw Error("Unknown discovery route")
  const plan = await readStudy(directory, project, id)
  await location(directory, id)
  const receipt = importReceipt.parse(JSON.parse(await readFile(await inside(directory, `.discovery/studies/${id}/${route}.import.json`), "utf8")))
  if (receipt.study !== id || receipt.project !== project || receipt.target !== plan.target.id || receipt.route !== route
    || !receipt.snapshot.startsWith(`.discovery/studies/${id}/`)) throw Error("Import receipt identity mismatch")
  const identity = receipt.targetIdentity
  if (identity && (identity.gene !== plan.target.gene || identity.uniprot !== plan.target.uniprot
    || identity.taxon !== plan.target.taxon || Object.entries(identity.site).some(([key, value]) =>
      key === "residue" ? String(value).toUpperCase().replace(/^CYS$/, "C") !== plan.target.site.residue.toUpperCase().replace(/^CYS$/, "C")
        : value !== plan.target.site[key as keyof typeof plan.target.site])))
    throw Error("Frozen import target identity differs from study")
  const bytes = await readFile(await inside(directory, receipt.snapshot))
  if (hash(bytes) !== receipt.source.sha256) throw Error("Historical snapshot changed")
  const data = importedResult(JSON.parse(bytes.toString("utf8")), plan, route)
  if (data.project !== receipt.source.project || data.run !== receipt.source.run || data.candidates.length !== receipt.candidates
    || data.evidenceUpdatedAt !== receipt.source.evidenceUpdatedAt) throw Error("Import provenance mismatch")
  return { receipt, data }
}

export async function studyOverview(directory: string, project: string, id: string, localChemistry = true): Promise<StudyOverview> {
  const plan = await readStudy(directory, project, id)
  const folder = await location(directory, id)
  const routes = await Promise.all(studyRoutes.map(async route => {
    if (!await Bun.file(path.join(folder, route + ".import.json")).exists()) return { route, ...inputReadiness(plan, route), imported: null }
    const imported = await readImport(directory, project, id, route)
    return { route, state: "historical-imported" as const, missing: [],
      reason: "历史结果已接入；原计算状态单独保留，不代表本任务完成新计算", imported }
  }))
  const historical = routes.flatMap(route => (route.imported?.data.candidates ?? []).map(candidate => ({
      recordID: hash(JSON.stringify([id, route.route, route.imported!.receipt.source.run, candidate.id])),
      study: id, route: route.route, sourceRun: route.imported!.receipt.source.run,
      sourceSha256: route.imported!.receipt.source.sha256, mode: "historical-reuse", candidate,
    })))
  const candidates = [...historical, ...await studyDeNovoCandidates(directory, plan)]
    .map(source => ({ ...source, handoff: evaluationHandoff(source, plan) }))
  return { schema: "discovery-study-overview-v1", plan, routes, newComputeSubmitted: false,
    candidates, pool: await candidatePool(candidates, localChemistry) }
}
