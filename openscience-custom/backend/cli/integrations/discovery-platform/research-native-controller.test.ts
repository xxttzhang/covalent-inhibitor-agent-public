import { beforeEach, afterEach, afterAll, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import { mkdtemp, realpath, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { Store } from "../inspire-screening/executor/store"
import { requestHash } from "../inspire-screening/executor/request"
import { ResearchLoopStore } from "./research-loop-store"
import { researchDigest, type ResearchContract } from "./research-loop-contract"
import { compileResearchExecution, prepareResearchExecution, type ResearchExecutionEnvironment } from "./research-execution"
import { writeSyntheticResearchProofs } from "./research-execution-test-fixture"
import { PermissionNext } from "../../src/permission/next"
import { observeResearchNativeAction } from "./research-native-observation"
import { researchSnapshot, commitResearchDecision } from "./research-loop"
import { registerResearchNativePlan, advanceResearchNativeAction, readResearchNativeAction,
  captureResearchNativeActionResult, nativeResearchCapability, researchNativeApprovalDeclined } from "./research-native-controller"

const scratch = await realpath(await mkdtemp(path.join(tmpdir(), "research-native-controller-")))
afterAll(() => rm(scratch, { recursive: true, force: true }))
const hash = (value: string) => createHash("sha256").update(value).digest("hex")

async function setup() {
  const data = await fixture()
  const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
  const native = new Store(path.join(data.directory, "native.sqlite"))
  const loop = research.create(data.contract)
  const prepared = await compileResearchExecution(data.contract, data.environment, data.binding,
    { finish_before: loop.created + loop.contract.limits.max_elapsed_ms })
  const identity = { research, native, id: loop.id }
  const offered = await registerResearchNativePlan({ research, id: loop.id, key: "round-1", round: 1, prepared })
  return { ...data, ...identity, prepared, offered, close: () => { native.close(); research.close() } }
}

function proposal(data: Awaited<ReturnType<typeof setup>>) {
  // Synthetic host-boundary fixture only. Live entry independently validates
  // provider receipts; no fixture claims an actual model invocation.
  data.research.record(data.id, "synthetic-receipt", { schema: "synthetic-model-receipt-fixture", model_called: false })
  const snapshot = researchSnapshot(data.research, data.id)
  return { store: data.research, id: data.id, context: snapshot.context, expected_revision: snapshot.loop.revision,
    provenance: { kind: "model_response" as const, receipt_ref: "synthetic-receipt", request_digest: hash("synthetic") },
    value: { schema: "drug-design-decision-v1", loop_id: data.id, state_revision: snapshot.loop.revision,
      action: "execute_prepared_action", capability_id: nativeResearchCapability, plan_id: data.prepared.plan.id,
      arguments: {}, evidence_refs: [data.offered.evidence_ref], rationale: "SYNTHETIC control-path test; no model or scientific result",
      alternatives: [], expected_observation: "Native permission result, not scientific success", outcome: null, missing_inputs: [] } }
}

function context(data: Awaited<ReturnType<typeof setup>>, ask?: Parameters<typeof advanceResearchNativeAction>[0]["context"]["ask"]) {
  return { sessionID: data.contract.session, directory: data.directory, abort: new AbortController().signal,
    metadata: () => {}, ask: ask ?? (async () => { throw Error("SYNTHETIC permission refusal; no real approval") }) }
}

function followup(data: Awaited<ReturnType<typeof setup>>, outcome = "approval_declined") {
  const receipt = `synthetic-followup-${data.research.evidence(data.id, "decision").length}`
  data.research.record(data.id, receipt, { schema: "synthetic-model-receipt-fixture", model_called: false })
  const snapshot = researchSnapshot(data.research, data.id)
  const refs = researchNativeApprovalDeclined(data.research, data.id).evidence_refs
  return { store: data.research, id: data.id, context: snapshot.context, expected_revision: snapshot.loop.revision,
    provenance: { kind: "model_response" as const, receipt_ref: receipt, request_digest: hash("synthetic-followup") },
    value: { schema: "drug-design-decision-v1", loop_id: data.id, state_revision: snapshot.loop.revision,
      action: "finish", capability_id: null, plan_id: null, arguments: {}, rationale: "SYNTHETIC refusal completion test",
      evidence_refs: refs.length ? refs : [snapshot.observations[0]!.id],
      alternatives: [], expected_observation: null, outcome, missing_inputs: [] } }
}

test.each(["rejected", "corrected"] as const)("typed native %s is observed, restored and finished while unknown resources remain reserved", async kind => {
  const data = await setup()
  try {
    const selected = await commitResearchDecision(proposal(data))
    const result = await advanceResearchNativeAction({ ...data, phase: "approve", context: context(data, async request => {
      expect(request.patterns).toEqual([data.prepared.plan_digest])
      // SYNTHETIC permission-reply fixture; the actual native approval/cleanup
      // implementation writes the requested/ended task journal below.
      throw kind === "rejected" ? new PermissionNext.RejectedError() : new PermissionNext.CorrectedError("PRIVATE_SYNTHETIC_USER_FEEDBACK")
    }) })
    expect(result).toMatchObject({ cancellation: "approval_declined", reservation_released: false, accounting_complete: false })
    expect(readResearchNativeAction(data)).toMatchObject({ decision_id: selected.decision_id, status: "CANCELLED", observation_required: true,
      cancellation: "approval_declined" })
    expect((await captureResearchNativeActionResult(data)).ready_for_evaluation).toBe(true)
    expect(JSON.stringify(data.research.evidence(data.id))).not.toContain("PRIVATE_SYNTHETIC_USER_FEEDBACK")
    const observed = await observeResearchNativeAction(data)
    expect(observed.observation).toMatchObject({ run: { status: "cancelled" }, classification: "execution_failure" })
    expect(observed.loop.state).toBe("DECIDING")
    const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
    const native = new Store(path.join(data.directory, "native.sqlite"))
    try {
      expect(readResearchNativeAction({ research, native, id: data.id }).cancellation).toBe("approval_declined")
      expect(researchNativeApprovalDeclined(research, data.id)).toMatchObject({ declined: true, unsubmitted_reservations: 1 })
    } finally { native.close(); research.close() }
    const finished = await commitResearchDecision(followup(data))
    expect(finished).toMatchObject({ accepted: true, loop: { state: "FINISHED", outcome: "approval_declined" } })
    expect(data.research.reservations(data.id)[0]).toMatchObject({ state: "RESERVED", actual: null })
    expect(finished.loop.ledger.unsettled).toEqual(["round-1"])
    // The newest decision is now finish; implicit native recovery still resolves
    // the original registered selection. A late read cannot reopen FINISHED.
    expect(readResearchNativeAction(data).decision_id).toBe(selected.decision_id)
    expect((await observeResearchNativeAction(data)).loop.state).toBe("FINISHED")
    expect(data.research.evidence(data.id, "observation")).toHaveLength(1)
    expect(data.native.list()).toHaveLength(1)
  } finally { data.close() }
})

test.each(["generic", "abort", "aborted_rejection", "policy"] as const)("a native %s error is cancellation, never proof of user refusal", async kind => {
  const data = await setup()
  try {
    await commitResearchDecision(proposal(data))
    const controller = new AbortController()
    await advanceResearchNativeAction({ ...data, phase: "approve", context: { ...context(data, async () => {
      if (kind === "policy") throw new PermissionNext.DeniedError([])
      if (kind === "abort") throw new DOMException("Synthetic interrupted ask", "AbortError")
      if (kind === "aborted_rejection") { controller.abort(); throw new PermissionNext.RejectedError() }
      throw Error("The user rejected permission to use this specific tool call.")
    }), abort: controller.signal } })
    expect(readResearchNativeAction(data)).toMatchObject({ status: "CANCELLED", cancellation: "cancelled_before_submission", observation_required: true })
    await observeResearchNativeAction(data)
    expect(researchNativeApprovalDeclined(data.research, data.id)).toEqual({ declined: false, evidence_refs: [], unsubmitted_reservations: 0 })
    const result = await commitResearchDecision(followup(data))
    expect(result.accepted).toBe(false)
    expect(result.reasons).toContain("Approval refusal needs the native permission result")
    expect(result.reasons).toContain("Cannot finish while compute completion or accounting is unresolved")
    expect(data.research.reservations(data.id)[0]).toMatchObject({ state: "RESERVED", actual: null })
  } finally { data.close() }
})

test("submitted cancellation remains distinct and cannot finish as an unsubmitted permission refusal", async () => {
  const data = await setup()
  try {
    await commitResearchDecision(proposal(data))
    const reference = readResearchNativeAction(data).reference
    await prepareResearchExecution(data.native, data.prepared, reference)
    data.research.bindTask(data.id, reference.key, data.prepared.plan.id, data.prepared.plan_digest)
    // SYNTHETIC persisted execution state only; no actual approval/job.
    data.native.approve(data.prepared.plan.id, data.prepared.plan_digest, data.contract.session, data.directory)
    data.native.stage(data.prepared.plan.id, 0, { state: "CANCELLED", submitted: 1000, handle: "SYNTHETIC-job" })
    data.native.state(data.prepared.plan.id, "CANCELLED")
    expect(readResearchNativeAction(data)).toMatchObject({ cancellation: "cancelled_after_submission", observation_required: true })
    const observed = await observeResearchNativeAction(data)
    expect(observed.observation!.limitations.some(value => value.includes("cancelled_after_submission"))).toBe(true)
    expect(researchNativeApprovalDeclined(data.research, data.id).declined).toBe(false)
    expect((await commitResearchDecision(followup(data))).accepted).toBe(false)
    expect(data.research.reservations(data.id)[0]).toMatchObject({ state: "RESERVED", actual: null })
  } finally { data.close() }
})

test("host registration verifies and persists offers without reserving budget or creating native drafts", async () => {
  const data = await setup()
  try {
    expect(data.research.reservations(data.id)).toEqual([])
    expect(data.native.list()).toEqual([])
    const snapshot = researchSnapshot(data.research, data.id)
    expect(snapshot.context.prepared_plans).toEqual([{ id: data.prepared.plan.id,
      capability_id: nativeResearchCapability, evidence_ref: data.offered.evidence_ref }])
    expect(snapshot.context.allowed_actions).toContainEqual({ action: "execute_prepared_action", capability_id: nativeResearchCapability,
      plan_id: data.prepared.plan.id })
    expect(snapshot.prompt.user).not.toContain(data.prepared.plan.runtime.worker)
    expect(await registerResearchNativePlan({ research: data.research, id: data.id, key: "round-1", round: 1,
      prepared: data.prepared })).toMatchObject({ registered: false })
    const changed = await compileResearchExecution(data.contract, data.environment, data.binding,
      { finish_before: data.prepared.plan.finish_before })
    await expect(registerResearchNativePlan({ research: data.research, id: data.id, key: "round-1", round: 1,
      prepared: changed })).rejects.toThrow("already binds")
    await expect(registerResearchNativePlan({ research: data.research, id: data.id, key: "round-2", round: 2,
      prepared: changed })).rejects.toThrow("identity-exclusion")
  } finally { data.close() }
})

test("selecting a host plan atomically reserves and queues one exact task without approval or submission", async () => {
  const data = await setup()
  try {
    const input = proposal(data)
    const result = await commitResearchDecision(input)
    expect(result).toMatchObject({ accepted: true, pending_native_action: true, loop: { state: "PREPARING" } })
    expect(data.research.reservations(data.id)).toHaveLength(1)
    expect(data.research.reservations(data.id)[0]!.payload.resources).toEqual(data.prepared.resources)
    expect(data.native.list()).toEqual([])
    const before = data.research.get(data.id).revision
    expect(readResearchNativeAction(data)).toMatchObject({ status: "not_prepared", task_id: data.prepared.plan.id })
    expect(data.research.get(data.id).revision).toBe(before)
    await expect(commitResearchDecision(input)).rejects.toThrow("Stale")
    expect(data.research.evidence(data.id, "decision")).toHaveLength(1)
  } finally { data.close() }
})

test("unknown plans and model-authored commands fail without changing budget or decisions", async () => {
  const data = await setup()
  try {
    const input = proposal(data)
    await expect(commitResearchDecision({ ...input, value: { ...input.value, plan_id: "invented-plan" } })).rejects.toThrow("allowed_actions")
    await expect(commitResearchDecision({ ...input, value: { ...input.value, arguments: { command: "arbitrary command" } } })).rejects.toThrow()
    expect(data.research.evidence(data.id, "decision")).toEqual([])
    expect(data.research.reservations(data.id)).toEqual([])
  } finally { data.close() }
})

test("a model may stop before selecting an offer without retaining unused compute reservations", async () => {
  const data = await setup()
  try {
    const input = proposal(data)
    const result = await commitResearchDecision({ ...input, value: { ...input.value, action: "finish", capability_id: null,
      plan_id: null, expected_observation: null, outcome: "insufficient_evidence" } })
    expect(result.loop.state).toBe("FINISHED")
    expect(data.research.reservations(data.id)).toEqual([])
    expect(data.native.list()).toEqual([])
  } finally { data.close() }
})

test("explicit host dispatch invokes the real native permission bridge once and recovers its original cancelled task", async () => {
  const data = await setup()
  const asked = Promise.withResolvers<void>()
  const reply = Promise.withResolvers<void>()
  const requests: string[][] = []
  try {
    await commitResearchDecision(proposal(data))
    const host = context(data, async request => { requests.push(request.patterns); asked.resolve(); await reply.promise })
    const first = advanceResearchNativeAction({ ...data, context: host, phase: "approve" })
    await asked.promise
    expect(requests).toEqual([[data.prepared.plan_digest]])
    const duplicate = await advanceResearchNativeAction({ ...data, context: host, phase: "approve" })
    expect(duplicate.result.state).toBe("needs_reconciliation")
    expect(requests).toHaveLength(1)
    reply.reject(Error("SYNTHETIC permission refused; no real approval"))
    expect((await first).result.state).toBe("not_approved")
    const recovered = readResearchNativeAction(data)
    expect(recovered).toMatchObject({ status: "CANCELLED", task_id: data.prepared.plan.id })
    expect(data.native.list()).toHaveLength(1)
    expect(data.native.stages(data.prepared.plan.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
    expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
    const again = await advanceResearchNativeAction({ ...data, context: host, phase: "approve" })
    expect(again.result.state).toBe("not_approved")
    expect(requests).toHaveLength(1)
    expect(data.research.reservations(data.id)[0]!.state).toBe("RESERVED")
  } finally { data.close() }
})

test("an uncertain once claim without the original task cannot create another draft", async () => {
  const data = await setup()
  try {
    await commitResearchDecision(proposal(data))
    data.research.claimSubmission(data.id, "round-1", data.research.get(data.id).revision, data.prepared.plan_digest)
    await expect(advanceResearchNativeAction({ ...data, phase: "approve", context: context(data) })).rejects.toThrow("instead of recreating")
    expect(data.native.list()).toEqual([])
  } finally { data.close() }
})

test("native draft recovery and Results readback preserve identity without inventing scientific observations or usage", async () => {
  const data = await setup()
  try {
    const selected = await commitResearchDecision(proposal(data))
    const reference = readResearchNativeAction(data).reference
    await prepareResearchExecution(data.native, data.prepared, reference)
    data.research.bindTask(data.id, reference.key, data.prepared.plan.id, data.prepared.plan_digest)
    expect(readResearchNativeAction(data)).toMatchObject({ task_id: data.prepared.plan.id, status: "DRAFT" })
    await expect(advanceResearchNativeAction({ ...data, phase: "start", context: context(data) })).rejects.toThrow("exact-plan approval")
    const result = await captureResearchNativeActionResult(data)
    expect(result.decision_id).toBe(selected.decision_id)
    expect(result.receipt).toMatchObject({ task_id: data.prepared.plan.id, actual_usage: null, scientific_objective: "not_evaluated_by_bridge" })
    expect((await captureResearchNativeActionResult(data)).evidence_ref).toBe(result.evidence_ref)
    expect(data.research.evidence(data.id, "observation")).toEqual([])
    expect(data.research.reservations(data.id)[0]!.actual).toBeNull()
    data.research.finish(data.id, data.research.get(data.id).revision, "user_cancelled", [])
    await captureResearchNativeActionResult(data)
    expect(data.research.get(data.id).state).toBe("CANCELLED")
    await expect(advanceResearchNativeAction({ ...data, phase: "approve", context: context(data) })).rejects.toThrow("does not permit")
  } finally { data.close() }
})

test("a reopened controller recovers the exact selected plan and native draft without asking or resubmitting", async () => {
  const data = await setup()
  try {
    const selected = await commitResearchDecision(proposal(data))
    const reference = readResearchNativeAction(data).reference
    await prepareResearchExecution(data.native, data.prepared, reference)
    const research = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
    const native = new Store(path.join(data.directory, "native.sqlite"))
    try {
      const recovered = readResearchNativeAction({ research, native, id: data.id, decision_id: selected.decision_id })
      expect(recovered.task_id).toBe(data.prepared.plan.id)
      expect(recovered.execution?.prepared).toEqual(data.prepared)
      expect(recovered.reservation.task).toBeNull() // Interrupted before the late bind is recoverable.
      const aborted = new AbortController()
      aborted.abort()
      await expect(advanceResearchNativeAction({ research, native, id: data.id,
        phase: "approve", context: { ...context(data), abort: aborted.signal } })).rejects.toThrow()
      expect(native.list()).toHaveLength(1)
      expect(research.events(data.id).some(event => event.kind === "submission_claimed")).toBe(false)
    } finally { native.close(); research.close() }
  } finally { data.close() }
})

test("start delegates an already bound approved task to the native database guard before any daemon or network", async () => {
  const data = await setup()
  try {
    await commitResearchDecision(proposal(data))
    const reference = readResearchNativeAction(data).reference
    await prepareResearchExecution(data.native, data.prepared, reference)
    data.research.bindTask(data.id, reference.key, data.prepared.plan.id, data.prepared.plan_digest)
    data.research.claimSubmission(data.id, reference.key, data.research.get(data.id).revision, data.prepared.plan_digest)
    // Synthetic SQLite recovery state only; no permission service or user approval.
    data.native.approve(data.prepared.plan.id, data.prepared.plan_digest, data.contract.session, data.directory)
    await expect(advanceResearchNativeAction({ ...data, phase: "start", context: context(data) })).rejects.toThrow("configured task database")
    expect(readResearchNativeAction(data)).toMatchObject({ task_id: data.prepared.plan.id, status: "APPROVED" })
    expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
    expect(data.native.stages(data.prepared.plan.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
  } finally { data.close() }
})
async function fixture() {
  const directory = await realpath(await mkdtemp(path.join(scratch, "case-")))
  const shared = "/inspire/fixture-shared"
  const binding = { library: `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv`,
    receptor: `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb`,
    science_config: `/inspire/fixture/releases/${"3".repeat(64)}/science/configs/covindb/EGFR_C797_mvp_v1.yaml`, mapping: path.join(directory, "mapping.json"),
    subset_manifest: path.join(directory, "subset.json"), quality_manifest: path.join(directory, "quality.json") }
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "fixture-project", session: "fixture-session",
    objective: "Test native plan binding, not chemistry", mode: "native-compute", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: "2".repeat(64), mapping_sha256: hash("synthetic-test-proof") },
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", library_sha256: "1".repeat(64), subset_sha256: hash("synthetic-test-proof"),
      source_release: "3".repeat(64), science_config_sha256: "4".repeat(64) },
    quality: { manifest_sha256: hash("synthetic-test-proof"), required_checks: ["synthetic_gate"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 8, compute_points: 2, model_tokens: 40000, model_cost: 1 },
      max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
      max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 12, max_elapsed_ms: 7200000 },
    allowed_adaptations: ["inspect", "stop"] }
  const proofs = await writeSyntheticResearchProofs(contract, binding)
  const release = `/inspire/fixture/releases/${contract.inputs.source_release}`
  const request = "Synthetic research execution adapter test, not authorized remote computation"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: contract.project, group: "CPU",
    quota: "0,4,16", image: "fixture-image", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0.3 }
  const environment: ResearchExecutionEnvironment = {
    source: { text: request, origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant",
      part_ids: ["fixture-part"], request_sha256: requestHash(request) } },
    connection: { cli: "/nonexistent/fixture-cli", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    runtime: { root: "/inspire/fixture", project: `${release}/science`, worker: `${release}/worker.py`, activation: `${release}/science/environment/activate_carsidock.sh` },
    assets: { [binding.library]: contract.inputs.library_sha256!, [binding.receptor]: contract.target.receptor_sha256!,
      [binding.science_config]: contract.inputs.science_config_sha256!, [`${release}/worker.py`]: "5".repeat(64), [`${release}/manifest.json`]: "6".repeat(64) },
    cpu, gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 2 } }
  return { directory, contract, environment, binding, proofs, ref: { loop: "loop-fixture", key: "round-1", contract_digest: researchDigest(contract) } }
}
