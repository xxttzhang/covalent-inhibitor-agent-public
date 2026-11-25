import { expect, test } from "bun:test"
import { mkdtemp, realpath, mkdir, rm } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { createHash } from "node:crypto"
import { readDeNovoResults } from "../../src/tool/denovo-results"

const id = "denovo-aaaaaaaaaaaaaaaaaaaaaaaa"
const hash = (text: string) => createHash("sha256").update(text).digest("hex")

test("native result readback preserves zero candidates and rejects changed or misbound evidence", async () => {
  const base = await realpath(await mkdtemp(path.join(os.tmpdir(), "denovo-results-")))
  const root = path.join(base, id)
  try {
    await mkdir(path.join(root, "acceptance"), { recursive: true })
    const request = JSON.stringify({ id, directory: base, session_id: "ses_fixture", user_message_id: "msg_fixture",
      profile: "egfr-c797-acrylamide-v1", slots: 1, seed: 123,
      package_hashes: { "assets/protein_A.pdb": hash("engineering receptor fixture") } })
    await Bun.write(path.join(root, "request.json"), request)
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "AWAITING_APPROVAL" }))
    const expected = { directory: base, session: "ses_fixture" }
    expect((await readDeNovoResults(base, id, expected)).state).toBe("RESULTS_NOT_READY")
    // Engineering receipt only; no platform permission or scientific compute occurs.
    await Bun.write(path.join(root, "authorization.json"), JSON.stringify({ request_sha256: hash(request),
      session_id: "ses_fixture", user_message_id: "msg_fixture", mechanism: "native_remote_compute" }))
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 0 }, relaxed: { pass_count: 0 } } }))
    const proof = { state: "STRUCTURAL_QC_VERIFIED", task_id: id, request_sha256: hash(request),
      requested_slots: 1, seed: 123,
      package_hashes: { "assets/protein_A.pdb": hash("engineering receptor fixture") },
      raw_pass_count: 0, relaxed_pass_count: 0,
      ready_for_scientific_handoff: false,
      candidates: ["raw", "relaxed"].map(representation => ({
        candidate_id: `${id}:0:${representation}`, slot: 0, representation,
        structural_qc_passed: false, smiles: null, source_sha256: null, failures: ["unparseable_molecule"],
        independent_prereaction_evaluation: "NOT_PERFORMED", independent_adduct_evaluation: "NOT_PERFORMED",
        scientific_status: "STRUCTURAL_QC_FAILED" })) }
    const save = async (value: unknown) => {
      const bytes = JSON.stringify(value)
      await Bun.write(path.join(root, "acceptance/verification.json"), bytes)
      await Bun.write(path.join(root, "publication.json"), JSON.stringify({
        "verification.json": { state: "VERIFIED", sha256: hash(bytes) } }))
    }
    await save(proof)
    const result = await readDeNovoResults(base, id, expected)
    expect(result).toMatchObject({ state: "RESULTS_READ", raw_pass_count: 0, relaxed_pass_count: 0,
      compute_submitted: false, assessment: "structural_qc_receipt_read_not_recomputed" })
    expect(result.candidates?.every(row => row.sourceAssessment.status === "UNKNOWN")).toBe(true)
    expect(result.candidates?.every(row => row.sourceAssessment.producerVerdict === "FAIL")).toBe(true)
    expect(result.candidates?.every(row => row.metrics.length === 2 && row.metrics.every(metric => metric.value === null))).toBe(true)
    expect(result.candidates).toHaveLength(2) // retain both failed slot representations
    expect(await readDeNovoResults(base, id, expected)).toEqual(result)
    await Bun.write(path.join(root, "acceptance/verification.json"), JSON.stringify(proof) + "\n")
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("checksum")
    await save({ ...proof, candidates: [proof.candidates[0], proof.candidates[0]] })
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("each raw and relaxed slot")
    await save({ ...proof, task_id: "denovo-bbbbbbbbbbbbbbbbbbbbbbbb" })
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow()
    await save({ ...proof, raw_pass_count: 1 })
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("counts differ")
    await expect(readDeNovoResults(base, id, { ...expected, session: "ses_other" })).rejects.toThrow("another session")
    // Parser/readback fixture, deliberately not a chemistry validation test.
    await mkdir(path.join(root, "package/assets"), { recursive: true })
    await Bun.write(path.join(root, "package/assets/protein_A.pdb"), "engineering receptor fixture")
    await Bun.write(path.join(root, "molecule.sdf"), "engineering molecule fixture")
    const positive = { ...proof, raw_pass_count: 1, candidates: [
      { ...proof.candidates[0], smiles: "C=CC(=O)NC", source_sha256: hash("engineering molecule fixture"),
        structural_qc_passed: true, failures: [], scientific_status: "PENDING_INDEPENDENT_EVALUATION" },
      proof.candidates[1],
    ] }
    await save(positive)
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 1 }, relaxed: { pass_count: 0 } } }))
    const slot = { slot: 0, source: path.join(root, "molecule.sdf"),
      raw: { smiles: positive.candidates[0].smiles, passed: true, core: { mapping: [3, 2, 4, 1, 0] },
        covalent_geometry: { SG_beta_distance_A: 3.3, SG_beta_alpha_angle_deg: 110 } }, relaxed: {} }
    await Bun.write(path.join(root, "acceptance/slot_000.json"), JSON.stringify(slot))
    const mapped = await readDeNovoResults(base, id, expected)
    expect(mapped.candidates?.[0].scientificEvidence?.reactionAtoms?.atoms.at(-1))
      .toEqual({ role: "reactive_beta_C", index: 0 })
    expect(mapped.candidates?.[0].metrics[0].value).toBe(3.3)
    expect(mapped.candidates?.[0].scientificEvidence?.measurements[0].inputs).toHaveLength(2)
    expect(mapped.candidates?.[0].scientificEvidence?.pose).toMatchObject({
      state: "reported", representation: "prereaction", recordIndex: 0,
      coordinateBinding: "not_independently_verified",
    })
    expect(mapped.candidates?.[0].scientificEvidence?.pose?.molecule)
      .toEqual(mapped.candidates?.[0].scientificEvidence?.reactionAtoms?.molecule)
    expect(mapped.candidates?.[0].scientificEvidence?.pose?.receptor)
      .toEqual(mapped.candidates?.[0].scientificEvidence?.measurements[0].inputs[1])
    const partial = { ...slot, raw: { ...slot.raw,
      covalent_geometry: { SG_beta_distance_A: 0, SG_beta_alpha_angle_deg: null } } }
    await Bun.write(path.join(root, "acceptance/slot_000.json"), JSON.stringify(partial))
    const measured = await readDeNovoResults(base, id, expected)
    expect(measured.candidates?.[0].metrics.map(metric => metric.value)).toEqual([0, null])
    expect(measured.candidates?.[0].scientificEvidence?.measurements).toHaveLength(1)
    expect(measured.candidates?.[0].sourceAssessment).toMatchObject({
      status: "PASS", physicalValidity: "not_assessed_by_reader", verification: "receipt_consistency_only",
    })
    const failure = { ...positive, raw_pass_count: 0, candidates: [
      { ...positive.candidates[0], smiles: null, structural_qc_passed: false,
        scientific_status: "STRUCTURAL_QC_FAILED", failures: ["evaluation_error"] }, proof.candidates[1],
    ] }
    await save(failure)
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 0 }, relaxed: { pass_count: 0 } } }))
    const errored = await readDeNovoResults(base, id, expected)
    expect(errored.candidates?.[0].sourceAssessment).toMatchObject({
      status: "NOT_EVALUATED", evaluationState: "ERROR", sourceAvailability: "UNPARSED_SOURCE",
    })
    await Bun.write(path.join(root, "molecule.sdf"), "changed")
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("Unparsed candidate source differs")
    await Bun.write(path.join(root, "molecule.sdf"), "engineering molecule fixture")
    await save(positive)
    await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 1 }, relaxed: { pass_count: 0 } } }))
    await Bun.write(path.join(root, "molecule.sdf"), "changed")
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("molecule differs")
    await Bun.write(path.join(root, "acceptance/slot_000.json"),
      JSON.stringify({ ...slot, source: path.join(base, "outside.sdf") }))
    await expect(readDeNovoResults(base, id, expected)).rejects.toThrow("escapes task")
  } finally { await rm(base, { recursive: true, force: true }) }
})
