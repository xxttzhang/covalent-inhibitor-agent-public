import { expect, test } from "bun:test"
import path from "node:path"
import { Progress } from "./progress"
import { digest, planSchema } from "./model"
import { Store } from "./store"
import { Instance } from "../../../src/project/instance"
import { Server } from "../../../src/server/server"
import { Session } from "../../../src/session"
import { MessageV2 } from "../../../src/session/message-v2"
import { Identifier } from "../../../src/id/id"
import { Bus } from "../../../src/bus"
import { requestHash } from "./request"
import { tmpdir } from "../../../test/fixture/fixture"
import { selectionSnapshot, verifySelectionProposal } from "./selection-proposal"
import { inspireSelection } from "../../../../../frontend/ui/src/components/inspire-selection"
import { diagnosePreparation, preparationSnapshot } from "./preparation-diagnostic"
import { preparationDiagnostic } from "../../../../../frontend/ui/src/components/inspire-preparation"
import { snapshot as toolSnapshot } from "./tool"

async function setup(directory: string, withOrigin = false, size = 3) {
  const session = await Session.create({})
  const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
    agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
  const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
    parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
    path: { cwd: directory, root: directory }, time: { created: Date.now(), completed: Date.now() },
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
  const resource = { kind: "hpc", workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
    image: "fixture", image_type: "SOURCE_OFFICIAL", priority: 4, points_per_hour: 0 }
  const plan = planSchema.parse({ version: "screening-executor-v1", id: "screen-" + "e".repeat(24), request: "fixture only",
    directory, session: session.id, target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv",
    ...(withOrigin ? { origin: { user_message_id: user.id, assistant_message_id: assistant.id,
      part_ids: ["fixture-origin-part"], request_sha256: requestHash("fixture only") } } : {}),
    scope: "bounded", max_records: 10, shortlist: size, seed: 1,
    connection: { cli: "/never-invoked", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    assets: { ...Object.fromEntries(["a", "b", "c", "d", "e"].map((x) => ["/inspire/" + x, "a".repeat(64)])),
      "/inspire/project/configs/covindb/EGFR_C797_mvp_v1.yaml": "a".repeat(64) },
    runtime: { project: "/inspire/project", root: "/inspire/root", worker: "/inspire/worker", activation: "/inspire/activation" },
    steps: ["prepare", "carsidock", "rtmscore", "report"].map((phase) => ({ phase, minutes: 1, max_candidates: size,
      resource: ["carsidock", "rtmscore"].includes(phase) ? { ...resource, kind: "job", workspace: "分布式训练空间", quota: "1,10,200" } : resource })),
    max_points: 1, gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 },
    created: Date.now(), approve_before: Date.now() + 60000, finish_before: Date.now() + 120000 })
  const file = path.join(directory, "tasks.sqlite")
  const store = new Store(file)
  const task = store.create(plan)
  const part = await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: assistant.id,
    type: "tool", tool: "inspire_screening_task", callID: "fixture-progress-call",
    state: { status: "completed", input: { action: "create" }, title: "original title",
      output: JSON.stringify({ task_id: task.id, digest: task.digest, original: true }),
      metadata: { preserved: "original metadata" }, time: { start: Date.now(), end: Date.now() } } })
  if (part.type !== "tool") throw new Error("Fixture did not create a tool part")
  const read = () => MessageV2.get({ sessionID: session.id, messageID: assistant.id })
  return { store, file, task, session, assistant, part, read }
}

test("preparation diagnosis persists on the real native card without approving, resubmitting or hiding changed evidence", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, true)
    data.store.approve(data.task.id, data.task.digest, data.session.id, tmp.path)
    const first = data.store.stages(data.task.id)[0]
    data.store.stage(data.task.id, 0, { state: "VERIFYING", handle: first.name, submitted: Date.now() })
    data.store.state(data.task.id, "NEEDS_REVIEW", "Fixture: preparation output not verified")
    const task = data.store.get(data.task.id)
    const reasons = { mw_below_min: 1, mw_above_max: 0, clogp_below_min: 1, clogp_above_max: 0,
      tpsa_above_max: 0, rotors_above_max: 0, charge_above_max: 0 }
    const proof = { version: "preparation-diagnostic-v1", task_id: task.id, plan_digest: task.digest,
      rows: 2, input_rows: 2, eligible: 0, incompatible: 1, ambiguous: 0, valid_warhead: 1,
      limits: { min_mw: 180, max_mw: 650, min_logp: -1, max_logp: 6, max_tpsa: 160, max_rotors: 15, max_abs_formal_charge: 2 },
      property_failures: reasons, valid_warhead_property_failures: reasons,
      files: Object.fromEntries(["plan.json", "started.json", "error.json", "target.yaml", "selection.json", "master.parquet"]
        .map((name) => [name, { sha256: "a".repeat(64), bytes: 100 }])), scientific_quality_verified: false, compute_submitted: false }
    const filename = path.join(tmp.path, "diagnostic.json")
    await Bun.write(filename, JSON.stringify(proof))
    const read = () => Bun.file(filename).json()
    expect(preparationSnapshot(data.store, task)).toBeNull()
    const result = await diagnosePreparation(data.store, task.id, read)
    expect(result?.state).toBe("AVAILABLE")
    const publisher = new Progress(data.store, "http://openscience.internal", Server.internalFetch())
    expect((await publisher.publish(task)).delivered).toBe(true)
    const saved = (await data.read()).parts.find((item) => item.id === data.part.id)!
    if (saved.type !== "tool" || saved.state.status !== "completed") throw new Error("Original tool changed")
    const snapshot = preparationDiagnostic(saved.state.metadata.inspire_preparation_diagnostic ?? null, task.id, task.digest)
    if (!snapshot) throw new Error("Fixture diagnostic was not published")
    expect(snapshot?.result?.eligible).toBe(0)
    expect(snapshot?.scientific_quality_verified).toBe(false)
    expect(snapshot?.compute_submitted).toBe(false)
    expect(saved.state.metadata.inspire_progress.preparation_diagnostic).toEqual(snapshot)
    const before = data.store.events(task.id).length
    const status = toolSnapshot(data.store, task)
    expect(status.preparation_diagnostic).toEqual(snapshot as typeof status.preparation_diagnostic)
    expect(status.state).toBe("NEEDS_REVIEW")
    expect(status.stages?.slice(1).every((stage) => stage.job === null)).toBe(true)
    expect(data.store.events(task.id).length).toBe(before)
    expect(preparationDiagnostic(snapshot ?? null, "screen-" + "b".repeat(24), task.digest)).toBeUndefined()
    expect(preparationDiagnostic(snapshot ? { ...snapshot, scientific_quality_verified: true } : null, task.id, task.digest)).toBeUndefined()
    expect(preparationDiagnostic(snapshot ? { ...snapshot, result: { ...proof, rows: 3 } } : null, task.id, task.digest)).toBeUndefined()
    data.store.close()
    const store = new Store(data.file)
    try {
      const persisted = preparationSnapshot(store, store.get(task.id))
      expect(persisted).toEqual(snapshot as typeof persisted)
      const storedStatus = toolSnapshot(store, store.get(task.id)).preparation_diagnostic
      expect(storedStatus).toEqual(snapshot as typeof storedStatus)
      // New content must not silently replace the first observed file witness.
      await Bun.write(filename, JSON.stringify({ ...proof, files: { ...proof.files, "master.parquet": { sha256: "c".repeat(64), bytes: 100 } } }))
      expect((await diagnosePreparation(store, task.id, read))?.state).toBe("UNAVAILABLE")
      expect(store.db.query("SELECT count(*) AS n FROM events WHERE task_id=? AND kind='preparation_diagnostic_witness'").get(task.id)).toEqual({ n: 1 })
      expect(store.get(task.id).state).toBe("NEEDS_REVIEW")
      expect(store.get(task.id).approval).toBe(task.approval)
      expect(store.stages(task.id).slice(1).every((stage) => stage.state === "WAITING" && stage.handle === null)).toBe(true)
      expect(store.events(task.id).some((item) => (item as { kind: string }).kind === "dispatch_reserved")).toBe(false)
      const update = new Progress(store, "http://openscience.internal", Server.internalFetch())
      expect((await update.publish(store.get(task.id))).delivered).toBe(true)
      const changed = (await data.read()).parts.find((item) => item.id === data.part.id)!
      if (changed.type !== "tool" || changed.state.status !== "completed") throw new Error("Original tool changed")
      expect(changed.state.output).toBe(saved.state.output)
      expect(changed.state.metadata.inspire_preparation_diagnostic.state).toBe("UNAVAILABLE")
      expect(changed.state.metadata.inspire_preparation_diagnostic.result).toBeUndefined()
      expect(toolSnapshot(store, store.get(task.id)).preparation_diagnostic?.state).toBe("UNAVAILABLE")
      expect(toolSnapshot(store, store.get(task.id)).preparation_diagnostic).toEqual(changed.state.metadata.inspire_preparation_diagnostic)
    } finally { store.close() }
  } })
})

test("verified selection persists on the originating native card, survives reopen, and never hides a bad newest proposal behind an old list", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, true, 8)
    data.store.approve(data.task.id, data.task.digest, data.session.id, tmp.path)
    const manifests: Record<string, unknown>[] = []
    for (const stage of data.store.stages(data.task.id)) {
      const manifest = { task_id: data.task.id, phase: stage.phase, plan_digest: data.task.digest,
        status: "SUCCEEDED", parent_digest: manifests.length ? digest(manifests.at(-1)) : null,
        input_count: 8, success_count: 8, failed_count: 0, identity_digest: "a".repeat(64),
        artifacts: { "fixture.txt": "b".repeat(64) }, gates: { local_presentation_fixture: true }, runtime_seconds: 1 }
      manifests.push(manifest)
      data.store.stage(data.task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest) })
    }
    data.store.state(data.task.id, "SUCCEEDED")
    const task = data.store.get(data.task.id)
    const publisher = new Progress(data.store, "http://openscience.internal", Server.internalFetch())
    expect(selectionSnapshot(data.store, task)).toBeNull()
    const candidates = [{ input_sdf_index: 3, compound_id: "fixture-A", selection_channel: "rank_disagreement_review",
      carsidock_rank_fraction: 0, rtmscore_rank_fraction: 1, rank_disagreement: 1 }]
    const policy = { max_candidates: 2, max_fraction: 0.25, disagreement_threshold: 0.5,
      top_fraction: 0.2, uncertainty_count: 1, consensus_count: 1, diversity_count: 0 }
    const directory = `${task.plan.runtime.root}/tasks/${task.id}/selections/${"c".repeat(64)}`
    const proof = verifySelectionProposal({ selection_verified: true, selection_id: "c".repeat(64), source_task_id: task.id,
      source_plan_digest: task.digest, source_manifest_digest: digest(manifests[3]), selection_manifest_digest: "d".repeat(64),
      status: "SUBSET_PROPOSED", policy, input_count: 8, eligible_count: 8, excluded_count: 0,
      selected_count: 1, effective_cap: 2, artifact_directory: directory,
      input: { path: `${directory}/candidates.parquet`, sha256: "e".repeat(64), eligible_count: 8, candidates,
        identity_sha256: digest([[3, "fixture-A"]]), calibrated_uncertainty: false },
      calibrated_uncertainty: false, gpu_submitted: false, refinement_approval_required: true,
      limitations: ["Local presentation fixture, not a scientific result"] }, task, data.store.stages(task.id), policy)
    data.store.event(task.id, "refinement_subset_proposed", proof, 1700000000000)
    expect((await publisher.publish(task)).delivered).toBe(true)
    const saved = (await data.read()).parts.find((item) => item.id === data.part.id)!
    if (saved.type !== "tool" || saved.state.status !== "completed") throw new Error("Original part changed")
    const snapshot = inspireSelection(saved.state.metadata.inspire_selection, task.id, task.digest)
    expect(snapshot?.state).toBe("SUBSET_PROPOSED")
    expect(snapshot?.additional_budget_points).toBeNull()
    expect(snapshot?.grants_resource_approval).toBe(false)
    expect(saved.state.metadata.inspire_progress.selection).toEqual(snapshot)
    expect(saved.state.metadata.preserved).toBe("original metadata")
    expect(snapshot?.recorded_at).toBe(1700000000000)
    expect(await publisher.publish(task)).toEqual({ delivered: false, reason: "unchanged" })
    data.store.close()
    const store = new Store(data.file)
    try {
      expect(snapshot).toEqual(selectionSnapshot(store, store.get(task.id)) ?? undefined)
      const recovered = new Progress(store, "http://openscience.internal", Server.internalFetch())
      const empty = { ...proof, status: "NO_CANDIDATES_MEET_POLICY", selected_count: 0,
        input: { ...proof.input, candidates: [], identity_sha256: digest([]) } }
      store.event(task.id, "refinement_subset_proposed", empty)
      expect((await recovered.publish(store.get(task.id))).delivered).toBe(true)
      expect(selectionSnapshot(store, task)?.state).toBe("NO_CANDIDATES_MEET_POLICY")
      store.event(task.id, "refinement_subset_proposed", { ...proof, source_plan_digest: "0".repeat(64) })
      expect((await recovered.publish(store.get(task.id))).delivered).toBe(true)
      const latest = (await data.read()).parts.find((item) => item.id === data.part.id)!
      if (latest.type !== "tool" || latest.state.status !== "completed") throw new Error("Original part changed")
      expect(latest.state.output).toBe(saved.state.output)
      expect(latest.state.metadata.inspire_progress.state).toBe("SUCCEEDED")
      expect(latest.state.metadata.inspire_selection.state).toBe("UNAVAILABLE")
      expect(latest.state.metadata.inspire_selection.candidates).toBeUndefined()
      expect(store.get(task.id).approval).toBe(task.approval)
      expect(store.events(task.id).some((row) => (row as { kind: string }).kind === "dispatch_reserved")).toBe(false)
    } finally { store.close() }
  } })
})

test("real platform part API persists progress without changing tool results or approval and survives reopen", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path)
    const publisher = new Progress(data.store, "http://openscience.internal", Server.internalFetch())
    expect(await publisher.publish(data.task)).toEqual({ delivered: false, reason: "unapproved" })
    data.store.approve(data.task.id, data.task.digest, data.session.id, tmp.path)
    const approval = data.store.get(data.task.id).approval
    const changes: string[] = []
    const unsubscribe = Bus.subscribe(MessageV2.Event.PartUpdated, (event) => changes.push(event.properties.part.id))
    expect((await publisher.publish(data.store.get(data.task.id))).delivered).toBe(true)
    const part = (await data.read()).parts.find((item) => item.id === data.part.id)!
    expect(part.type).toBe("tool")
    if (part.type !== "tool" || part.state.status !== "completed") throw new Error("Original tool state changed")
    expect(part.state.output).toBe(data.part.state.status === "completed" ? data.part.state.output : "")
    expect(part.state.title).toBe("original title")
    expect(part.state.metadata.preserved).toBe("original metadata")
    expect(part.state.metadata.inspire_progress.state).toBe("APPROVED")
    expect(part.state.metadata.inspire_results).toEqual([])
    expect(changes).toContain(data.part.id)
    expect(await publisher.publish(data.store.get(data.task.id))).toEqual({ delivered: false, reason: "unchanged" })
    expect(changes.length).toBe(1)
    data.store.close()
    const reopened = new Store(data.file)
    const next = new Progress(reopened, "http://openscience.internal", Server.internalFetch())
    expect(await next.publish(reopened.get(data.task.id))).toEqual({ delivered: false, reason: "unchanged" })
    reopened.stage(data.task.id, 0, { state: "FAILED", error: "fixture failure, not a scientific run" })
    reopened.state(data.task.id, "FAILED", "fixture failure, not a scientific run")
    expect((await next.publish(reopened.get(data.task.id))).delivered).toBe(true)
    const failed = (await data.read()).parts.find((item) => item.id === data.part.id)!
    if (failed.type !== "tool" || failed.state.status !== "completed") throw new Error("Original call mutated")
    expect(failed.state.metadata.inspire_progress.state).toBe("FAILED")
    expect(failed.state.metadata.inspire_progress.stages[0].input_count).toBeNull()
    expect(reopened.get(data.task.id).approval).toBe(approval)
    expect(reopened.events(data.task.id).some((row) => (row as { kind: string }).kind === "dispatch_reserved")).toBe(false)
    unsubscribe()
    reopened.close()
  } })
})

test("mismatched or missing task card cannot be overwritten; delivery failure is isolated from execution", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path)
    data.store.approve(data.task.id, data.task.digest, data.session.id, tmp.path)
    const publisher = new Progress(data.store, "http://openscience.internal", Server.internalFetch())
    const original = data.part
    if (original.state.status !== "completed") throw new Error("Fixture error")
    await Session.updatePart({ ...original, state: { ...original.state, output: JSON.stringify({ task_id: data.task.id, digest: "0".repeat(64) }) } })
    expect((await publisher.publish(data.store.get(data.task.id))).delivered).toBe(false)
    expect(await publisher.publish(data.store.get(data.task.id))).toEqual({ delivered: false, reason: "backoff" })
    const part = (await data.read()).parts[0]
    if (part.type !== "tool" || part.state.status !== "completed") throw new Error("Fixture error")
    expect(part.state.metadata.inspire_progress).toBeUndefined()
    expect(data.store.get(data.task.id).state).toBe("APPROVED")
    expect(data.store.stages(data.task.id).every((stage) => stage.state === "WAITING")).toBe(true)
    expect(() => new Progress(data.store, "https://example.com")).toThrow("local platform")
    data.store.close()
  } })
})

test("origin-bound delivery retries an unusable HTTP response without changing execution or original message", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, true)
    data.store.approve(data.task.id, data.task.digest, data.session.id, tmp.path)
    const unavailable = new Progress(data.store, "http://openscience.internal/unavailable", Server.internalFetch())
    const result = await unavailable.publish(data.store.get(data.task.id))
    expect(result.delivered).toBe(false)
    expect(result.reason).toContain("JSON") // Wrong route serves HTML, never a valid part response.
    expect((await data.read()).parts[0]).toEqual(data.part)
    const restored = new Progress(data.store, "http://openscience.internal", Server.internalFetch())
    expect((await restored.publish(data.store.get(data.task.id))).reason).toBe("backoff")
    data.store.db.query("UPDATE presentation SET retry_after=0 WHERE task_id=?").run(data.task.id)
    expect((await restored.publish(data.store.get(data.task.id))).delivered).toBe(true)
    expect(data.store.get(data.task.id).state).toBe("APPROVED")
    expect(data.store.stages(data.task.id).every((stage) => stage.submitted === null)).toBe(true)
    data.store.close()
  } })
})
