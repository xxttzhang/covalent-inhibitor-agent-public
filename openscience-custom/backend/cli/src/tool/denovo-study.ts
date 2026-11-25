import { createHash } from "node:crypto"
import { z } from "zod"
import { compileDeNovoRequest } from "./denovo-plan"
import { verifyDeNovoEvidenceSnapshot } from "../../integrations/discovery-platform/denovo-evidence"
import { readStudyGenerationProfile, validateWarheadDecision } from "../../integrations/discovery-platform/study-denovo-compiler"

const sha = (text: string) => createHash("sha256").update(text).digest("hex")
const hash = z.string().regex(/^[a-f0-9]{64}$/)

/** Bind a frozen study plan to a new native user-message/approval scope.
 * This does not submit, approve, resample, or change scientific inputs. */
export async function bindStudyDeNovoRequest(plan: unknown, study: string, input: {
  directory: string; session: string; message: string; text: string; package: string; worker: string;
}, requestApproval = false) {
  if (!/^study-[a-f0-9-]{36}$/.test(study) || (!requestApproval && input.text.trim() !== `执行 de-novo 研究 ${study}`))
    throw Error("Use the exact direct study execution command; quoted or additional instructions are not accepted")
  if (!input.message || !input.text.trim()) throw Error("A real user request is required before native approval")
  const saved = z.object({ state: z.literal("PLAN_COMPILED_NOT_APPROVED"), approved: z.literal(false), submitted: z.literal(false),
    study: z.literal(study), request: z.object({ id: z.string(), profile: z.string(), slots: z.number(), seed: z.number(),
      directory: z.literal(input.directory), session_id: z.string(), package_hashes: z.record(z.string(), hash), worker_sha256: hash,
      scientific_basis: z.object({ hypothesis_json: z.string(), hypothesis_sha256: hash,
        evidence_id: z.string(), evidence_manifest_sha256: hash, manifest_json: z.string(), evidence_json: z.string(),
        execution_branch_id: z.string() }).passthrough(),
    }).passthrough() }).passthrough().parse(plan)
  const request = saved.request
  if (sha(request.scientific_basis.hypothesis_json) !== request.scientific_basis.hypothesis_sha256
    || JSON.parse(request.scientific_basis.hypothesis_json).hypothesis.study_id !== study)
    throw Error("Frozen scientific hypothesis differs")
  const basis = request.scientific_basis
  const proposal = z.object({ evidence_id: z.string(), evidence_manifest_sha256: hash,
    target: z.object({ symbol: z.string(), site: z.string(), taxon: z.number(), uniprot_id: z.string() }),
    hypothesis: z.object({ route: z.literal("de-novo"), warhead_selection: z.unknown(), warhead_class: z.array(z.string()) }),
    references: z.array(z.object({ id: z.string() })) }).parse(JSON.parse(basis.hypothesis_json))
  if (proposal.evidence_id !== basis.evidence_id || proposal.evidence_manifest_sha256 !== basis.evidence_manifest_sha256)
    throw Error("Frozen scientific evidence binding differs")
  verifyDeNovoEvidenceSnapshot(basis, basis.evidence_manifest_sha256, proposal.target, proposal.references.map(item => item.id))
  const decision = validateWarheadDecision(proposal.hypothesis.warhead_selection, proposal.references, proposal.hypothesis.warhead_class)
  if (decision.execution_branch_id !== basis.execution_branch_id) throw Error("Frozen execution branch differs")
  const profile = await readStudyGenerationProfile(input.package)
  const target = profile.study_binding
  if (request.profile !== profile.id || proposal.target.symbol !== target.gene || proposal.target.site !== "C" + target.number
    || proposal.target.taxon !== target.taxon || proposal.target.uniprot_id !== target.uniprot
    || proposal.hypothesis.warhead_class[0] !== profile.warhead) throw Error("Frozen hypothesis differs from registered package")
  const current = await compileDeNovoRequest({ ...input, id: request.id, profile: request.profile, slots: request.slots, seed: request.seed })
  if (current.worker_sha256 !== request.worker_sha256
    || Object.keys(current.package_hashes).length !== Object.keys(request.package_hashes).length
    || Object.entries(current.package_hashes).some(([name, digest]) => request.package_hashes[name] !== digest))
    throw Error("Registered execution code differs from frozen study plan; replan explicitly")
  return { ...request, planning_session_id: request.session_id, session_id: input.session, user_message_id: input.message,
    user_text: input.text, user_text_sha256: sha(input.text), study_id: study,
    study_plan_sha256: sha(JSON.stringify(saved)), native_binding_created: Date.now() }
}
