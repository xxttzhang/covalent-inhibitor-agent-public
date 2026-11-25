import { beforeEach, afterEach, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import { mkdir, realpath, writeFile } from "node:fs/promises"
import path from "node:path"
import { createHash } from "node:crypto"
import { Store } from "../inspire-screening/executor/store"
import { digest, type Outcome } from "../inspire-screening/executor/model"
import { requestHash } from "../inspire-screening/executor/request"
import { deliveryPath } from "../inspire-screening/executor/delivery"
import { Instance } from "../../src/project/instance"
import { Bus } from "../../src/bus"
import { PermissionNext } from "../../src/permission/next"
import { tmpdir } from "../../test/fixture/fixture"
import { ResearchLoopStore } from "./research-loop-store"
import { researchDigest, type ResearchContract } from "./research-loop-contract"
import { compileResearchExecution, prepareResearchExecution, type ResearchExecutionEnvironment } from "./research-execution"
import { writeSyntheticResearchProofs } from "./research-execution-test-fixture"
import { approveResearchNative, startResearchNative, readResearchNativeReceipt, type ResearchNativeInput } from "./research-native"

const hash = (value: string) => createHash("sha256").update(value).digest("hex")

async function fixture(input: string, reserve = true) {
  const directory = await realpath(input)
  const shared = "/inspire/fixture-shared"
  const release = `/inspire/fixture/releases/${"3".repeat(64)}`
  const binding = { library: `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv`,
    receptor: `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb`,
    science_config: `${release}/science/configs/covindb/EGFR_C797_mvp_v1.yaml`, mapping: path.join(directory, "mapping.json"),
    subset_manifest: path.join(directory, "subset.json"), quality_manifest: path.join(directory, "quality.json") }
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "fixture-project", session: "session_native_bridge",
    objective: "Synthetic native bridge acceptance; no chemistry or remote computation", mode: "native-compute", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: "2".repeat(64), mapping_sha256: hash("synthetic-native-bridge-proof") },
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", library_sha256: "1".repeat(64), subset_sha256: hash("synthetic-native-bridge-proof"),
      source_release: "3".repeat(64), science_config_sha256: "4".repeat(64) },
    quality: { manifest_sha256: hash("synthetic-native-bridge-proof"), required_checks: ["synthetic_gate"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 8, compute_points: 2, model_tokens: 40000, model_cost: 1 },
      max_library_records: 20000, max_rounds: 2, max_candidates_per_round: 5, max_total_candidates: 10,
      max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 12, max_elapsed_ms: 7200000 },
    allowed_adaptations: ["inspect", "stop"] }
  await writeSyntheticResearchProofs(contract, binding)
  const text = "Synthetic native approval test, no real remote execution"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: contract.project, group: "CPU",
    quota: "0,4,16", image: "fixture-image", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0.3 }
  const environment: ResearchExecutionEnvironment = {
    source: { text, origin: { user_message_id: "fixture-user", assistant_message_id: "fixture-assistant",
      part_ids: ["fixture-part"], request_sha256: requestHash(text) } },
    connection: { cli: "/nonexistent/fixture-cli", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
    runtime: { root: "/inspire/fixture", project: `${release}/science`, worker: `${release}/worker.py`, activation: `${release}/science/environment/activate_carsidock.sh` },
    assets: { [binding.library]: contract.inputs.library_sha256!, [binding.receptor]: contract.target.receptor_sha256!,
      [binding.science_config]: contract.inputs.science_config_sha256!, [`${release}/worker.py`]: "5".repeat(64), [`${release}/manifest.json`]: "6".repeat(64) },
    cpu, gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 2 } }
  const research = new ResearchLoopStore(path.join(directory, "research.sqlite"))
  const native = new Store(path.join(directory, "native.sqlite"))
  const loop = research.create(contract)
  const prepared = await compileResearchExecution(contract, environment, binding, { finish_before: loop.created + contract.limits.max_elapsed_ms })
  const reference = { loop: loop.id, key: "round-1", contract_digest: loop.digest }
  if (reserve) research.reserve(loop.id, loop.revision, { key: reference.key, kind: "compute", round: 1,
    candidate_slots: 5, plan_digest: prepared.plan_digest, source_release: contract.inputs.source_release, resources: prepared.resources })
  await prepareResearchExecution(native, prepared, reference)
  return { research, native, reference, prepared, directory, binding, close: () => { native.close(); research.close() } }
}

function context(input: Awaited<ReturnType<typeof fixture>>, controller: AbortController) {
  return { sessionID: input.prepared.contract.session, directory: input.directory, abort: controller.signal,
    metadata: () => {}, ask: (request: Parameters<Parameters<typeof approveResearchNative>[0]["context"]["ask"]>[0]) =>
      PermissionNext.ask({ ...request, sessionID: input.prepared.contract.session, signal: controller.signal,
        ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }) }
}

for (const reply of ["once", "reject", "abort", "research_cancel"] as const) {
  test(`native research approval uses the real exact-plan permission card and retains one task: ${reply}`, async () => {
    await using temporary = await tmpdir({ git: true })
    await Instance.provide({ directory: temporary.path, fn: async () => {
      const data = await fixture(temporary.path)
      const controller = new AbortController()
      const requested = Promise.withResolvers<PermissionNext.Request>()
      const count = { cards: 0 }
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => {
        if (event.properties.sessionID === data.prepared.contract.session) { count.cards++; requested.resolve(event.properties) }
      })
      try {
        const pending = approveResearchNative({ ...data, context: context(data, controller) })
        const card = await requested.promise
        expect(card.permission).toBe("remote_compute")
        expect(card.patterns).toEqual([data.prepared.plan_digest])
        expect(card.metadata.compute.stages).toEqual(data.prepared.plan.steps)
        expect(card.metadata.compute.maximum_compute_points).toBeCloseTo(data.prepared.resources.compute_points)
        expect(data.research.reservations(data.reference.loop)[0]!.task).toBe(data.prepared.plan.id)
        expect(data.native.get(data.prepared.plan.id).approval).toBeNull()
        const duplicate = await approveResearchNative({ ...data, context: context(data, controller) })
        expect(duplicate).toMatchObject({ state: "needs_reconciliation", resumed: true, approval_requested: false })
        expect(count.cards).toBe(1)
        if (reply === "abort") controller.abort()
        if (reply === "research_cancel") data.research.finish(data.reference.loop, data.research.get(data.reference.loop).revision, "user_cancelled", [])
        if (reply !== "abort") await PermissionNext.reply({ requestID: card.id, reply: reply === "reject" ? "reject" : "once" })
        const result = await pending
        expect(result.state).toBe(reply === "once" ? "approved" : "not_approved")
        expect(result.task_id).toBe(data.prepared.plan.id)
        expect(data.native.list()).toHaveLength(1)
        expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
        expect(data.native.stages(data.prepared.plan.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
        expect(await PermissionNext.list()).toEqual([])
        if (reply === "once") {
          const resumed = await approveResearchNative({ ...data, context: context(data, controller) })
          expect(resumed).toMatchObject({ state: "approved", resumed: true, approval_requested: false })
          expect(count.cards).toBe(1)
          await expect(startResearchNative({ ...data, context: context(data, controller) })).rejects.toThrow("configured task database")
          expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
        } else expect(data.native.get(data.prepared.plan.id)).toMatchObject({ state: "CANCELLED", approval: null })
      } finally { unsubscribe(); data.close() }
    } })
  })
}

test("missing reservation and interrupted durable claim never request native permission", async () => {
  await using temporary = await tmpdir({ git: true })
  await Instance.provide({ directory: temporary.path, fn: async () => {
    const data = await fixture(temporary.path, false)
    const controller = new AbortController()
    const calls = { cards: 0 }
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, () => { calls.cards++ })
    try {
      await expect(approveResearchNative({ ...data, context: context(data, controller) })).rejects.toThrow("cumulative research reservation")
      data.research.reserve(data.reference.loop, data.research.get(data.reference.loop).revision, {
        key: data.reference.key, kind: "compute", round: 1, candidate_slots: 5,
        plan_digest: data.prepared.plan_digest, source_release: data.prepared.contract.inputs.source_release, resources: data.prepared.resources })
      data.research.claimSubmission(data.reference.loop, data.reference.key, data.research.get(data.reference.loop).revision, data.prepared.plan_digest)
      const recovered = await approveResearchNative({ ...data, context: context(data, controller) })
      expect(recovered.state).toBe("needs_reconciliation")
      expect(calls.cards).toBe(0)
      expect(data.native.get(data.prepared.plan.id).state).toBe("DRAFT")
    } finally { unsubscribe(); data.close() }
  } })
})

test("proof changes after exact-plan approval prevent scheduling and retain the original task", async () => {
  await using temporary = await tmpdir({ git: true })
  await Instance.provide({ directory: temporary.path, fn: async () => {
    const data = await fixture(temporary.path)
    const controller = new AbortController()
    const requested = Promise.withResolvers<PermissionNext.Request>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => requested.resolve(event.properties))
    try {
      const approval = approveResearchNative({ ...data, context: context(data, controller) })
      const card = await requested.promise
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect((await approval).state).toBe("approved")
      const before = data.native.get(data.prepared.plan.id)
      await writeFile(data.binding.mapping, "SYNTHETIC modified proof after approval")
      await expect(startResearchNative({ ...data, context: context(data, controller) })).rejects.toThrow("proof changed")
      expect(data.native.get(before.id).approval).toBe(before.approval)
      expect(data.native.list()).toHaveLength(1)
      expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
      expect(data.native.stages(before.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
    } finally { unsubscribe(); data.close() }
  } })
})

test("native receipt rehashes the exact delivered files and keeps execution, science and billing distinct", async () => {
  await using temporary = await tmpdir({ git: true })
  await Instance.provide({ directory: temporary.path, fn: async () => {
    const data = await fixture(temporary.path)
    const controller = new AbortController()
    const requested = Promise.withResolvers<PermissionNext.Request>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, event => requested.resolve(event.properties))
    try {
      const approval = approveResearchNative({ ...data, context: context(data, controller) })
      const card = await requested.promise
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect((await approval).state).toBe("approved")
      const task = data.native.get(data.prepared.plan.id)
      const manifests: Outcome[] = []
      // Synthetic persisted execution and Results fixtures test the reader, not
      // science or real remote execution; the approval path above is genuine.
      for (const stage of data.native.stages(task.id)) {
        const contents = JSON.stringify({ fixture: "not-scientific-output", phase: stage.phase })
        const sha256 = hash(contents)
        const manifest: Outcome = { task_id: task.id, phase: stage.phase, plan_digest: task.digest, status: "SUCCEEDED",
          parent_digest: manifests.length ? digest(manifests.at(-1)) : null, input_count: 5, success_count: 5, failed_count: 0,
          identity_digest: "9".repeat(64), gates: { synthetic_reader_fixture: true }, runtime_seconds: 1, artifacts: { "fixture.json": sha256 } }
        manifests.push(manifest)
        data.native.stage(task.id, stage.ordinal, { state: "SUCCEEDED", manifest: JSON.stringify(manifest) })
        const file = await deliveryPath(task, { phase: stage.phase, name: "fixture.json", sha256 })
        await mkdir(path.dirname(file), { recursive: true })
        await writeFile(file, contents)
        data.native.db.query("INSERT INTO artifact_publications VALUES(?,?,?,?,?)").run(task.id, stage.phase, "fixture.json", task.digest,
          JSON.stringify({ phase: stage.phase, name: "fixture.json", artifact_id: `art_${stage.ordinal}`, version_id: `ver_${stage.ordinal}`,
            project_id: "synthetic-fixture-project", sha256, bytes: Buffer.byteLength(contents), published_at: Date.now(), evidence_kind: "core_result" }))
      }
      data.native.state(task.id, "SUCCEEDED")
      const receipt = await readResearchNativeReceipt(data)
      expect(receipt.files).toHaveLength(4)
      expect(receipt.files.every(file => file.status === "verified")).toBe(true)
      expect(receipt.actual_usage).toBeNull()
      expect(receipt.scientific_objective).toBe("not_evaluated_by_bridge")
      expect(receipt.publication_readback).toBe("recorded_at_delivery")
      expect(receipt.remote_artifacts_rehashed).toBe(false)
      const before = data.research.get(data.reference.loop).revision
      await writeFile(receipt.files[0]!.local_path, "tampered-result")
      const changed = await readResearchNativeReceipt(data)
      expect(changed.files[0]!.status).toBe("mismatch")
      expect(data.research.get(data.reference.loop).revision).toBe(before)
      expect(data.native.get(task.id).state).toBe("SUCCEEDED")
      expect(data.native.db.query("SELECT * FROM daemon").all()).toEqual([])
    } finally { unsubscribe(); data.close() }
  } })
})
