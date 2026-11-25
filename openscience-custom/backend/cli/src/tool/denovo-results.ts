import path from "node:path"
import { lstat, realpath, readFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { isDeepStrictEqual } from "node:util"
import z from "zod"
import { readDeNovoTask } from "./denovo-task"
import { candidateEvidence } from "@synsci/util/candidate-evidence"
import { deNovoSourceAssessment } from "@synsci/util/candidate-assessment"
import { prereactionGeometry } from "@synsci/util/metric-semantics"

const sha = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex")
const digest = z.string().regex(/^[a-f0-9]{64}$/)
const candidate = z.object({
  candidate_id: z.string(), slot: z.number().int().nonnegative(),
  representation: z.enum(["raw", "relaxed"]), structural_qc_passed: z.boolean(),
  smiles: z.string().nullable(), failures: z.array(z.string()),
  source_sha256: digest.nullable(),
  independent_prereaction_evaluation: z.literal("NOT_PERFORMED"),
  independent_adduct_evaluation: z.literal("NOT_PERFORMED"),
  scientific_status: z.enum(["PENDING_INDEPENDENT_EVALUATION", "STRUCTURAL_QC_FAILED"]),
}).passthrough()

/** Read the existing worker's published verification, never execute task-owned code.
 * Hash readback verifies receipt consistency; this does not repeat scientific QC.
 */
export async function readDeNovoResults(base: string, id: string, expected: {
  directory: string; session?: string; study?: string; request?: Record<string, unknown>;
}) {
  const task = await readDeNovoTask(base, id, expected)
  if (!task) throw Error("Native task does not exist")
  if (task.state !== "COMPLETED") return {
    state: "RESULTS_NOT_READY", task_id: id, task_state: task.state, compute_submitted: false,
  }
  const root = path.join(base, id)
  const read = async (name: string) => {
    const filename = path.resolve(root, name)
    if (!filename.startsWith(root + path.sep)) throw Error("Result evidence escapes task")
    if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
      throw Error("Result evidence must be a regular file without symlinks")
    return readFile(filename)
  }
  const requestBytes = await read("request.json")
  const request = z.object({ id: z.literal(id), slots: z.number().int().min(1).max(96),
    seed: z.number().int().nonnegative(), package_hashes: z.record(z.string(), digest),
  }).passthrough().parse(JSON.parse(requestBytes.toString()))
  const proofBytes = await read("acceptance/verification.json")
  const ledger = z.record(z.string(), z.object({ state: z.literal("VERIFIED"), sha256: digest })
    .passthrough()).parse(JSON.parse((await read("publication.json")).toString()))
  if (ledger["verification.json"]?.sha256 !== sha(proofBytes))
    throw Error("Published verification checksum differs")
  const proof = z.object({ state: z.literal("STRUCTURAL_QC_VERIFIED"), task_id: z.literal(id),
    request_sha256: z.literal(sha(requestBytes)), requested_slots: z.literal(request.slots),
    seed: z.literal(request.seed), raw_pass_count: z.number().int().nonnegative(),
    package_hashes: z.record(z.string(), digest),
    relaxed_pass_count: z.number().int().nonnegative(), candidates: z.array(candidate),
    ready_for_scientific_handoff: z.literal(false),
  }).passthrough().parse(JSON.parse(proofBytes.toString()))
  if (!isDeepStrictEqual(proof.package_hashes, request.package_hashes))
    throw Error("Verification package differs from frozen request")
  const keys = proof.candidates.map(row => `${row.slot}:${row.representation}`)
  if (proof.candidates.length !== request.slots * 2 || new Set(keys).size !== keys.length)
    throw Error("Verification does not cover each raw and relaxed slot")
  for (const row of proof.candidates) {
    if (row.slot >= request.slots || row.candidate_id !== `${id}:${row.slot}:${row.representation}`
      || row.structural_qc_passed !== (row.scientific_status === "PENDING_INDEPENDENT_EVALUATION")
      || row.smiles && !row.source_sha256
      || !row.structural_qc_passed && !row.failures.length
      || row.structural_qc_passed && (!row.smiles || !row.source_sha256 || row.failures.length))
      throw Error("Verification candidate identity or assessment differs")
  }
  for (const stage of ["raw", "relaxed"] as const) {
    const count = proof.candidates.filter(row => row.representation === stage && row.structural_qc_passed).length
    if (count !== proof[`${stage}_pass_count`] || !("summary" in task)
      || task.summary?.[stage].pass_count !== count || task.summary.requested_slots !== request.slots)
      throw Error("Verification counts differ from native task")
  }
  const evaluated = await Promise.all(proof.candidates.map(async row => {
    const sourceAssessment = deNovoSourceAssessment(row, {
      path: path.join(root, "acceptance/verification.json"), sha256: sha(proofBytes),
    })
    if (!row.smiles || !row.source_sha256) {
      if (row.source_sha256) {
        const slot = z.object({ slot: z.literal(row.slot), source: z.string() }).passthrough()
          .parse(JSON.parse((await read(`acceptance/slot_${String(row.slot).padStart(3, "0")}.json`)).toString()))
        const filename = row.representation === "raw" ? slot.source : `acceptance/relaxed/${row.slot}.sdf`
        if (sha(await read(filename)) !== row.source_sha256) throw Error("Unparsed candidate source differs from verification")
      }
      return { ...row, sourceAssessment, metrics: prereactionGeometry.map(entry => ({
        id: entry.id, label: entry.label, unit: entry.unit, definition: entry.definition, value: null,
      })), scientificEvidence: undefined }
    }
    const filename = `acceptance/slot_${String(row.slot).padStart(3, "0")}.json`
    const bytes = await read(filename)
    const slot = z.object({ slot: z.literal(row.slot), source: z.string(),
      raw: z.record(z.string(), z.unknown()), relaxed: z.record(z.string(), z.unknown()),
    }).passthrough().parse(JSON.parse(bytes.toString()))
    const saved = z.object({ smiles: z.literal(row.smiles), passed: z.literal(row.structural_qc_passed),
      core: z.object({ mapping: z.array(z.number().int().nonnegative()).length(5).nullable() }).passthrough().optional(),
      covalent_geometry: z.object({ SG_beta_distance_A: z.number().finite().nullable().optional(),
        SG_beta_alpha_angle_deg: z.number().finite().nullable().optional() }).passthrough().optional(),
    }).passthrough().parse(slot[row.representation])
    const moleculePath = row.representation === "raw" ? slot.source : `acceptance/relaxed/${row.slot}.sdf`
    const molecule = await read(moleculePath)
    if (sha(molecule) !== row.source_sha256) throw Error("Candidate molecule differs from verification")
    const evidence = { path: path.join(root, filename), sha256: sha(bytes) }
    const input = { path: path.resolve(root, moleculePath), sha256: row.source_sha256 }
    const metrics = prereactionGeometry.map(entry => ({
      id: entry.id, label: entry.label, unit: entry.unit, definition: entry.definition,
      value: saved.covalent_geometry?.[entry.id] ?? null,
    }))
    const measured = metrics.filter(metric => metric.value !== null)
    const receptor = measured.length ? await read("package/assets/protein_A.pdb") : null
    if (receptor && sha(receptor) !== request.package_hashes["assets/protein_A.pdb"])
      throw Error("Geometry receptor differs from frozen package")
    const inputs = [input, ...(receptor ? [{
      path: path.join(root, "package/assets/protein_A.pdb"), sha256: sha(receptor),
    }] : [])]
    const mapping = saved.core?.mapping
    const scientificEvidence = candidateEvidence.parse({
      schema: "candidate-scientific-evidence-v1", candidateID: row.candidate_id, smiles: row.smiles,
      pose: { state: "reported", representation: "prereaction", molecule: input, recordIndex: 0,
        ...(receptor ? { receptor: inputs[1] } : {}),
        method: row.representation === "raw" ? "Generator raw coordinates recorded by slot evaluator"
          : "Relaxed coordinates recorded by slot evaluator",
        evidence, coordinateBinding: "not_independently_verified" },
      ...(mapping && request.profile === "egfr-c797-acrylamide-v1" ? {
        reactionAtoms: { state: "reported", warhead: "acrylamide", representation: "prereaction",
          molecule: input, recordIndex: 0, indexConvention: "zero_based_rdkit_removeHs",
          atoms: ["growth_N", "carbonyl_C", "carbonyl_O", "alpha_C", "reactive_beta_C"]
            .map((role, index) => ({ role, index: mapping[index] })),
          method: "Registered evaluator core mapping; receipt readback, not independently recomputed", evidence },
      } : {}),
      measurements: measured.map(metric => ({ metricID: metric.id, scope: "candidate", stage: "prereaction",
        method: "Registered evaluator anchored geometry; receipt readback, not independently recomputed",
        unit: metric.unit, definition: metric.definition, inputs, evidence })),
    })
    return { ...row, sourceAssessment, metrics, scientificEvidence }
  }))
  return { schema: "native-denovo-evaluation-v1", state: "RESULTS_READ", task_id: id,
    provenance: { request_sha256: sha(requestBytes), verification_sha256: sha(proofBytes) },
    candidates: evaluated, requested_slots: request.slots,
    raw_pass_count: proof.raw_pass_count, relaxed_pass_count: proof.relaxed_pass_count,
    assessment: "structural_qc_receipt_read_not_recomputed",
    independent_prereaction_evaluation: "NOT_PERFORMED", independent_adduct_evaluation: "NOT_PERFORMED",
    compute_submitted: false }
}
