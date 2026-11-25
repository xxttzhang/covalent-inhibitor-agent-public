import path from "node:path"
import { createHash, randomBytes } from "node:crypto"
import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { compileDeNovoRequest } from "../../src/tool/denovo-plan"
import { verifyDeNovoEvidence } from "./denovo-evidence"

const claim = z.object({ statement: z.string().min(1), evidence_ids: z.array(z.string()).min(1),
  interpretation: z.enum(["reported", "inference"]) })
const selection = claim.extend({ status: z.enum(["prioritized", "unresolved"]).default("prioritized"),
  selected_warhead: z.string().min(1).nullable().default(null), evidence_ids: z.array(z.string()).default([]),
  alternatives: z.array(claim.extend({ warhead: z.string().min(1), disposition: z.enum(["backup", "rejected"]) }).strict()).max(10).default([]),
  branches: z.array(claim.extend({ branch_id: z.string().regex(/^(?!primary$)[a-z][a-z0-9-]{0,63}$/),
    warhead: z.string().min(1) }).strict()).max(10).default([]),
  no_alternatives_reason: z.string().min(1).nullable().optional(),
  pending_reason: z.string().min(1).nullable().optional(),
  execution_branch_id: z.string().nullable().default("primary") }).strict()

export function validateWarheadDecision(input: unknown, references: { id: string }[], warheads: string[]) {
  const decision = selection.parse(input)
  if (decision.status !== "prioritized" || !decision.selected_warhead || !decision.evidence_ids.length
    || decision.execution_branch_id === null) throw Error("De novo scientific decision is unresolved; no execution branch")
  if (!decision.alternatives.length && !decision.branches.length && !decision.no_alternatives_reason?.trim())
    throw Error("No alternatives requires an explicit evidence coverage reason")
  const ids = decision.branches.map((item) => item.branch_id)
  if (new Set(ids).size !== ids.length) throw Error("Research branch IDs must be unique")
  const names = [decision.selected_warhead, ...decision.alternatives.map((item) => item.warhead)].map((name) => name.trim().toLowerCase())
  if (names.some((name) => !name) || new Set(names).size !== names.length) throw Error("Warhead alternatives must be distinct")
  const warhead = decision.execution_branch_id === "primary" ? decision.selected_warhead
    : decision.branches.find((item) => item.branch_id === decision.execution_branch_id)?.warhead
  if (!warhead || warheads.length !== 1 || warheads[0] !== warhead) throw Error("Execution branch warhead differs")
  const sources = new Set(references.map((item) => item.id))
  const citations = [...decision.evidence_ids, ...decision.alternatives.flatMap((item) => item.evidence_ids),
    ...decision.branches.flatMap((item) => item.evidence_ids)]
  if (citations.some((id) => !sources.has(id))) throw Error("Unknown warhead evidence ID")
  return { ...decision, execution_branch_id: decision.execution_branch_id }
}

export async function readStudyGenerationProfile(folder: string) {
  return z.object({ id: z.string(), warhead: z.string(), study_binding: z.object({ gene: z.string(),
    taxon: z.number(), uniprot: z.string(), chain: z.string(), residue: z.string(), number: z.number(),
    atom: z.string(), numbering: z.string(), receptor: z.string(), receptor_asset: z.string(),
    pocket: z.string(), pocket_asset: z.string() }).strict() }).passthrough().parse(
      await Bun.file(path.join(folder, "profile.json")).json())
}

export function assertGenerationBinding(study: DiscoveryStudy, profile: Awaited<ReturnType<typeof readStudyGenerationProfile>>) {
  const target = study.target
  const binding = profile.study_binding
  if (target.gene !== binding.gene || target.taxon !== binding.taxon || target.uniprot !== binding.uniprot
    || target.receptor.reference !== binding.receptor
    || Object.entries(target.site).some(([key, value]) => key === "residue"
      ? binding.residue.toUpperCase() !== String(value).toUpperCase()
      : binding[key as keyof typeof binding] !== value))
    throw Error("De novo target/structure/site has no matching registered execution package; no fallback")
  if (study.routes["de-novo"].inputs.pocket !== binding.pocket)
    throw Error(`Explicit pocket does not match registered generation region: expected ${binding.pocket}. Read denovo_generate registered_profile before creating the study; do not rewrite a frozen study or silently substitute a different pocket.`)
  if (study.routes["de-novo"].inputs.warhead !== profile.warhead)
    throw Error("De novo warhead differs from registered package")
}

/** Compile the actual PocketXMol worker request, not the legacy route diagram.
 * Package paths are host supplied; studies may select but cannot rewrite them. */
export async function compileStudyDeNovo(study: DiscoveryStudy, original: string,
  environment: { directory: string; session: string; package: string; worker: string; evidenceRoot?: string; source_release?: { release_id: string; manifest_sha256: string; root: string } }) {
  if (study.denovoBranches) throw Error("Compile an individual child study, not a multi-branch parent")
  const route = study.routes["de-novo"]
  if (route.inputs.warhead !== "acrylamide") throw Error("WAITING_CAPABILITY: native generator only implements acrylamide; no reaction fallback")
  if (!route.selected || route.mode !== "new") throw Error("Select a new De novo route")
  const proposal = z.object({ hypothesis: z.object({ study_id: z.literal(study.id), route: z.literal("de-novo"),
    warhead_class: z.array(z.string()), warhead_selection: z.unknown() }), target: z.object({ symbol: z.string(), site: z.string(),
    taxon: z.number(), uniprot_id: z.string() }), evidence_id: z.string().regex(/^hyp-[a-f0-9]{24}$/),
    evidence_manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    references: z.array(z.object({ id: z.string() }).passthrough()) }).passthrough().parse(JSON.parse(original))
  const decision = validateWarheadDecision(proposal.hypothesis.warhead_selection, proposal.references, proposal.hypothesis.warhead_class)
  const evidence = await verifyDeNovoEvidence(environment.evidenceRoot ?? environment.directory,
    proposal.evidence_id, proposal.evidence_manifest_sha256, proposal.target, proposal.references.map(item => item.id))
  const profile = await readStudyGenerationProfile(environment.package)
  const target = study.target
  const binding = profile.study_binding
  if (proposal.target.symbol !== target.gene || proposal.target.site !== "C" + target.site.number
    || proposal.target.taxon !== target.taxon || proposal.target.uniprot_id !== target.uniprot)
    throw Error("De novo hypothesis target differs from study")
  assertGenerationBinding(study, profile)
  if (route.inputs.warhead !== profile.warhead || proposal.hypothesis.warhead_class.length !== 1
    || proposal.hypothesis.warhead_class[0] !== profile.warhead) throw Error("De novo warhead differs from registered package")
  if (!route.generation) throw Error("Explicit generation slots and seed required")
  const request = await compileDeNovoRequest({ ...environment, id: "denovo-" + randomBytes(12).toString("hex"),
    profile: profile.id, ...route.generation, text: JSON.stringify(study) })
  const receptor = request.package_hashes[binding.receptor_asset]
  const pocket = request.package_hashes[binding.pocket_asset]
  if (!receptor || !pocket) throw Error("Registered receptor or pocket asset missing from package")
  if (target.receptor.sha256 && target.receptor.sha256 !== receptor) throw Error("Study receptor hash differs from package")
  return { state: "PLAN_COMPILED_NOT_APPROVED", compiled: true, approved: false, submitted: false,
    sourceKind: "immutable-study-not-native-compute-request", study: study.id,
    request: { ...request, scientific_basis: { ...(study.denovoOrigin ? { branch_origin: study.denovoOrigin } : {}), hypothesis_sha256: createHash("sha256").update(original).digest("hex"),
      evidence_id: proposal.evidence_id, evidence_manifest_sha256: proposal.evidence_manifest_sha256,
      execution_branch_id: decision.execution_branch_id, hypothesis_json: original, ...evidence } },
    inputHashes: { receptor, pocket },
    pending: ["Live resource and remote asset verification", "Native compute request and approval", "Real generation execution"] }
}
