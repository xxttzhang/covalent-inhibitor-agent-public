import path from "node:path"
import { mkdir, realpath } from "node:fs/promises"
import { createHash } from "node:crypto"
import { study, type DiscoveryStudy } from "@synsci/util/discovery-study"
import { deNovoEvaluation } from "@synsci/util/denovo-evaluation"
import { warheadDefinition, warheadRegistry } from "@synsci/util/warhead-registry"
import { readStudy, studyOverview } from "./study-store"
import { readStudyGenerationProfile, assertGenerationBinding } from "./study-denovo-compiler"
import { registeredDeNovoSource } from "../../src/tool/denovo-source"
import { publishFrozenJSON } from "./frozen-json"
import { checkWarheads } from "./warhead-check"
import { deNovoWorkflow } from "./study-denovo-workflow"

const digest = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex")

/** Deterministic child identity makes retries/concurrent creation recover the same study.
 * Each child uses the existing independent evidence, plan, approval and task ledger. */
export function branchStudy(parent: DiscoveryStudy, id: string): DiscoveryStudy {
  const branch = parent.denovoBranches?.find(branch => branch.id === id)
  if (!branch) throw Error("Unknown declared De novo branch")
  const hash = digest([parent, branch.id])
  const identity = `study-${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20, 32)}`
  return study.parse({ denovoOrigin: { parent: parent.id, parent_sha256: digest(parent), branch: id }, schema: "discovery-study-v1", id: identity, project: parent.project,
    createdAt: parent.createdAt, title: `${parent.title.slice(0, 220)} · ${branch.id}`,
    target: branch.target ?? parent.target,
    hypothesis: { state: "pending", rationale: branch.rationale,
      evidence: branch.evidence.map(source => ({ reference: source.reference })) },
    routes: { "de-novo": { selected: true, mode: "new", inputs: { warhead: branch.warhead, pocket: branch.pocket },
      generation: branch.generation } } })
}

export async function materializeDeNovoBranch(directory: string, project: string, parentID: string, id: string) {
  const parent = await readStudy(directory, project, parentID)
  const child = branchStudy(parent, id)
  const root = await realpath(directory)
  const container = await realpath(path.join(root, ".discovery/studies"))
  if (!container.startsWith(root + path.sep)) throw Error("Study container escapes project")
  const destination = path.join(container, child.id)
  await mkdir(destination, { recursive: true })
  if (await realpath(destination) !== destination) throw Error("Branch directory must not be a symlink")
  await publishFrozenJSON(path.join(destination, "plan.json"), child)
  const actual = await readStudy(directory, project, child.id)
  if (digest(actual) !== digest(child)) throw Error("Branch child differs from frozen declaration")
  const lineage = { schema: "study-denovo-branch-v1", parent: parent.id, parent_sha256: digest(parent),
    branch: id, child: child.id, child_sha256: digest(child), declaration: parent.denovoBranches!.find(branch => branch.id === id) }
  await publishFrozenJSON(path.join(destination, "denovo-parent.json"), lineage)
  if (await realpath(path.join(destination, "denovo-parent.json")) !== path.join(destination, "denovo-parent.json")
    || digest(await Bun.file(path.join(destination, "denovo-parent.json")).json()) !== digest(lineage))
    throw Error("Branch lineage differs")
  return { study_id: child.id, parent_study_id: parent.id, branch_id: id,
    evidence_state: "declared_not_verified", compute_submitted: false,
    next: { tool: "denovo_generate", action: "workflow", study_id: child.id } }
}

/** Checks installed registered source identity, not remote runtime assets or scientific validity. */
export async function deNovoCapability(value: DiscoveryStudy, folder?: string) {
  const definition = warheadDefinition(value.routes["de-novo"].inputs.warhead ?? "")
  if (!definition) return { state: "WAITING_CAPABILITY", blockers: ["Unregistered warhead definition"], definition: null }
  // The current native worker and product/QC assumptions only implement acrylamide.
  // Merely adding a profile JSON cannot enable other reaction classes.
  if (definition.id !== "acrylamide") return { state: "WAITING_CAPABILITY", definition,
    blockers: ["Validated generation profile", "Reaction atom mapping and product template", "Reaction-specific pose evaluation", "Adduct parameterization and MD admission"],
    reusable_acrylamide_geometry: false }
  try {
    const profile = await readStudyGenerationProfile(folder ?? (await registeredDeNovoSource()).package)
    assertGenerationBinding(value, profile)
    return { state: "REGISTERED_PROFILE_MATCH", profile: profile.id, definition, blockers: [],
      remote_runtime_verified: false, scientific_validity_established: false,
      pending: ["Verified branch-specific hypothesis", "Frozen plan and native approval", "Remote runtime preflight", "Independent pose/adduct evaluation", "MD admission"] }
  } catch (error) {
    return { state: "WAITING_CAPABILITY", definition,
      blockers: [error instanceof Error ? error.message : String(error)] }
  }
}

export async function deNovoBranches(directory: string, project: string, id: string) {
  const parent = await readStudy(directory, project, id)
  if (!parent.denovoBranches) throw Error("Study has no declared De novo branches")
  const branches = await Promise.all(parent.denovoBranches.map(async branch => {
    const child = branchStudy(parent, branch.id)
    const capability = await deNovoCapability(child)
    const filename = path.join(directory, ".discovery/studies", child.id, "plan.json")
    if (!await Bun.file(filename).exists()) return { branch_id: branch.id, study_id: child.id, declaration: branch,
      capability, stage: "NOT_MATERIALIZED", candidates: [], next: { action: "branch_study", study_id: parent.id, branch_id: branch.id } }
    try {
      const actual = await readStudy(directory, project, child.id)
      if (digest(actual) !== digest(child)) throw Error("Branch child differs from declaration")
      const workflow = await deNovoWorkflow(directory, project, child.id)
      const overview = await studyOverview(directory, project, child.id, false)
      const chemistry = overview.candidates.length ? await checkWarheads(overview.candidates.map(source => source.candidate.smiles))
        .then(result => ({ state: "checked", result, records: result.rows.map((row, index) => ({
          record_id: overview.candidates[index]!.recordID, candidate_id: overview.candidates[index]!.candidate.id,
          requested_warhead: branch.warhead,
          requested_motif_count: row.matches.filter(match => match.warhead === branch.warhead).length,
          single_requested_motif: row.state === "recognized" && row.matches.filter(match => match.warhead === branch.warhead).length === 1,
          other_recognized_motifs: row.matches.filter(match => match.warhead !== branch.warhead),
          scope: "Substructure recognition only; other motifs require designation review, not automatic rejection or reactivity inference",
        })) }), error => ({ state: "unavailable", reason: String(error) })) : { state: "no_candidates" }
      return { branch_id: branch.id, study_id: child.id, declaration: branch, capability, chemistry,
        stage: workflow.stage, workflow, candidates: overview.candidates.map(source => ({ ...source, evaluation: deNovoEvaluation(source, actual) })) }
    } catch (error) {
      return { branch_id: branch.id, study_id: child.id, declaration: branch, capability,
        stage: "NEEDS_REVIEW", error: error instanceof Error ? error.message : String(error), candidates: [] }
    }
  }))
  return { schema: "study-denovo-branches-v1", study_id: parent.id, branches,
    budget: { requested_generation_slots: parent.denovoBranches.reduce((sum, branch) => sum + branch.generation.slots, 0),
      scope: "Declared slots, not approved compute; each child freezes its own supported resource limits", approved: false },
    comparison: { ranked: false, raw_scores_comparable_across_warheads: false,
      policy: "Preserve branch/site/warhead, task and candidate evidence; compare only explicitly compatible metrics. Missing evidence is not a zero score." },
    registry_version: warheadRegistry.schema, compute_submitted: false }
}
