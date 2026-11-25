import { expect, test } from "bun:test"
import { mkdtemp, mkdir, copyFile, symlink, readdir, chmod } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { defaultDelivery, digest, planSchema, verifyOutcome, type Plan, type Task, type Stage } from "./model"
import { Store } from "./store"
import { Runner, ReviewRequired, type Transport, type Observation } from "./runner"
import { createArgs, observation, projectObservation, Inspire, cliResponse, CLIError, stagingPayload } from "./inspire"
import { schema, selection, summary, manualDeliveryLimit } from "./tool"
import { propose } from "./approval"
import { Instance } from "../../../src/project/instance"
import { PermissionNext } from "../../../src/permission/next"
import { Bus } from "../../../src/bus"
import { tmpdir as fixture } from "../../../test/fixture/fixture"
import { requestHash } from "./request"
import { minutes, plannerVersion, policy } from "./resource-planner"
import { readReport } from "./report"
import { failureInventory, readFailureReport, verifyFailureEvidence } from "./failure-report"
import { deliverArtifact, downloadSpec, MAX_DOWNLOAD_BYTES } from "./delivery"
import { FileRoutes } from "../../../src/server/routes/file"
import { Session } from "../../../src/session"
import { Server } from "../../../src/server/server"
import { ArtifactStore } from "../../../src/artifact/store"
import { Publications, publications } from "./publication"
import { Progress } from "./progress"
import { MessageV2 } from "../../../src/session/message-v2"
import { Identifier } from "../../../src/id/id"
import { DeliveryQueue, deliveryStatus } from "./delivery-queue"
import { deliverCompleted } from "./delivery-observer"
import { SessionProcessor } from "../../../src/session/processor"
import { inspireProgress } from "../../../../../frontend/ui/src/components/inspire-progress"
import { artifactTarget } from "./artifact-target"

function plan(): Plan {
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: "research", group: "CPU", quota: "0,4,16",
    image: "slurm", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0 }
  const gpu = { ...cpu, kind: "job" as const, workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", image: "pytorch", points_per_hour: 1 }
  return planSchema.parse({ version: "screening-executor-v1", id: "screen-" + "a".repeat(24), request: "针对 EGFR C797 从虚拟筛选库筛选",
    directory: "/project", session: "session", target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv",
    scope: "bounded", max_records: 10000, shortlist: 3, seed: 20260725,
    connection: { cli: "/nonexistent/inspire-test", account: "test-only", notebook: "cpu", workspace: "CPU资源空间" },
    assets: Object.fromEntries(["source", "target", "receptor", "engine", "worker"].map((key) => ["/inspire/" + key, "b".repeat(64)])),
    runtime: { project: "/inspire/project", root: "/inspire/root", worker: "/inspire/root/worker.py", activation: "/inspire/project/activate.sh" },
    steps: ["prepare", "carsidock", "rtmscore", "report"].map((phase) => ({ phase, minutes: 10, max_candidates: 3,
      resource: ["carsidock", "rtmscore"].includes(phase) ? gpu : cpu })),
    max_points: 1, gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 },
    created: Date.now(), approve_before: Date.now() + 1800000, finish_before: Date.now() + 86400000 })
}

test("large source inventories round-trip through the actual Python staging decoder with the same digest", async () => {
  const source = plan()
  source.assets = Object.fromEntries(Array.from({ length: 660 }, (_, index) => [
    "/inspire/software-fixture/" + "release/".repeat(24) + index + ".py", digest({ index }),
  ]))
  const packet = { plan: source, digest: digest(source) }
  expect(Buffer.byteLength(Buffer.from(JSON.stringify(packet)).toString("base64"))).toBeGreaterThan(131072)
  const payload = stagingPayload(packet)
  expect(payload.length).toBeLessThanOrEqual(64 * 1024)
  const child = Bun.spawn(["python3", "-B", "-c",
    "import runpy,sys,json; m=runpy.run_path(sys.argv[1]); p=m['decode_staging'](sys.argv[2]); assert m['digest'](p['plan'])==p['digest']; print(json.dumps(p))",
    path.join(import.meta.dir, "worker.py"), payload], { stdout: "pipe", stderr: "pipe" })
  const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  expect(err).toBe("")
  expect(code).toBe(0)
  expect(JSON.parse(out)).toEqual(packet)
})

test("oversized staging cannot become a repeated remote command failure", async () => {
  const source = plan()
  source.request = "x".repeat(4 * 1024 * 1024)
  expect(() => stagingPayload({ plan: source, digest: digest(source) })).toThrow(ReviewRequired)
  const transport = new Inspire(source.connection)
  // The CLI is intentionally nonexistent: a local boundary must reject first.
  await expect(transport.remote(source.runtime, ["snapshot", "x".repeat(100 * 1024)]))
    .rejects.toThrow("no remote action sent")
})

test("task target metadata is immutable and only joins its exact published artifact version", () => {
  const store = new Store(":memory:")
  try {
    const source = plan()
    const parameters = { target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
      insertion_code: "", numbering: "canonical" as const, reaction_type: "Michael Addition" }
    const task = store.create({ ...source, target_parameters: parameters })
    expect(store.get(task.id).plan.target_parameters).toEqual(parameters)
    expect(task.digest).not.toBe(digest(source))
    expect(() => planSchema.parse({ ...source, target_parameters: { ...parameters, chain: "B" } })).toThrow()
    const artifact = { id: "art_test", projectID: "project", current: { id: "ver_test", sha256: "c".repeat(64),
      size: 42, sessionID: source.session, sourcePath: "/project/result.cif" } }
    const receipt = { artifact_id: artifact.id, version_id: artifact.current.id, project_id: artifact.projectID,
      sha256: artifact.current.sha256, bytes: artifact.current.size }
    store.db.query("INSERT INTO artifact_publications(task_id,phase,name,plan_digest,receipt) VALUES(?,?,?,?,?)")
      .run(task.id, "report", "result.cif", task.digest, JSON.stringify(receipt))
    expect(artifactTarget(store.db, artifact, source.directory)?.parameters).toEqual(parameters)
    expect(artifactTarget(store.db, artifact, "/another-project")).toBeNull()
    expect(artifactTarget(store.db, { ...artifact, current: { ...artifact.current, id: "another-version" } }, source.directory)).toBeNull()
    expect(artifactTarget(store.db, { ...artifact, current: { ...artifact.current, sha256: "d".repeat(64) } }, source.directory)).toBeNull()
    store.db.query("UPDATE tasks SET plan=? WHERE id=?").run(JSON.stringify({ ...task.plan, target_parameters: { ...parameters, reaction_type: "Acylation" } }), task.id)
    expect(() => artifactTarget(store.db, artifact, source.directory)).toThrow()
  } finally { store.close() }
})

test("test100 null budget survives storage and requires exactly 100 prepared candidates", async () => {
  await using tmp = await fixture({ git: true })
  const source = plan()
  const selected = policy("test100")
  const times = minutes(selected)
  const value = planSchema.parse({ ...source, max_points: null, max_records: selected.max_records,
    shortlist: 100, origin: { user_message_id: "user", assistant_message_id: "assistant", part_ids: ["part"],
      request_sha256: requestHash(source.request) },
    steps: source.steps.map((step) => ({ ...step, minutes: times[step.phase as keyof typeof times], max_candidates: 100 })),
    automation: { version: plannerVersion, requested_mode: "test100", selected_mode: "test100", adjusted: false,
      intent_digest: "b".repeat(64), estimated_points: 3, budget_headroom: null, basis: "versioned-profile-plus-live-quota-rates" } })
  expect(() => planSchema.parse({ ...value, automation: undefined })).toThrow()
  expect(() => planSchema.parse({ ...value, shortlist: 99 })).toThrow()
  const file = path.join(tmp.path, "test100.sqlite")
  const first = new Store(file)
  const created = first.create(value)
  first.close()
  const store = new Store(file)
  try {
    const task = store.get(created.id)
    expect(task.plan.max_points).toBeNull()
    expect(task.digest).toBe(created.digest)
    expect(task.approval).toBeNull()
    const stage = store.stages(task.id)[0]
    const result = { task_id: task.id, phase: "prepare", plan_digest: task.digest, status: "SUCCEEDED", parent_digest: null,
      input_count: 100, success_count: 100, failed_count: 0, artifacts: { "candidates.sdf": "a".repeat(64) },
      identity_digest: "a".repeat(64), gates: { qc: true }, runtime_seconds: 1 }
    expect(() => verifyOutcome(result, task, stage, null)).not.toThrow()
    expect(() => verifyOutcome({ ...result, input_count: 99, success_count: 99 }, task, stage, null)).toThrow("100 quality-checked")
  } finally { store.close() }
})

test("CLI failures on stderr retain safe typed rejection rather than empty-stdout JSON errors", () => {
  const failed = JSON.stringify({ success: false, error: { type: "CommandFailed", code: 1,
    message: "private diagnostic must not be included" } })
  expect(() => cliResponse("", failed, 1)).toThrow("InspireSkill: CommandFailed")
  expect(() => cliResponse("", failed, 1)).not.toThrow("private diagnostic")
  expect(() => cliResponse(failed, "", 1)).toThrow(CLIError)
  expect(() => cliResponse("not-json", failed, 1)).toThrow("CommandFailed")
  expect(() => cliResponse('{"success":true,"data":{}}', failed, 1)).toThrow("CommandFailed")
  expect(() => cliResponse("", "", 0)).toThrow("invalid_response")
  expect(() => cliResponse("", "arbitrary credentials", 1)).toThrow("request_failed")
  expect(() => cliResponse("", JSON.stringify({ success: false, error: { type: "APIError", code: 13,
    message: "Could not resolve the selected workspace's scheduling policy." } }), 13))
    .toThrow("workspace_scheduling_policy_unavailable")
  expect(() => cliResponse("", JSON.stringify({ success: false, error: { code: "secret text", type: "Timeout" } }), 1)).toThrow("Timeout")
  expect(cliResponse('{"success":true,"data":{"status":"ready"}}', "warning", 0)).toEqual({ status: "ready" })
})

test("Live HPC QUEUEING is pending, never running or a terminal failure", () => {
  const spec = plan().steps[0].resource
  const name = "screen-" + "a".repeat(24) + "-prepare"
  expect(observation({ name, status: "QUEUEING", project: spec.project,
    compute_group: spec.group, resource: { gpu: 0, cpu: 4, memory_gib: 16, nodes: 1 } },
    { name }, spec)).toEqual({ state: "pending", handle: name })
})

for (const mode of ["verified", "changed", "unavailable", "wrong_phase", "upstream_changed", "aborted"] as const) {
  test(`failed core diagnostics retain a witness across SQLite reopen without resubmitting: ${mode}`, async () => {
    const data = await setup()
    const source = new LocalProcesses(path.join(data.folder, "jobs"))
    data.store.approve(data.task.id, data.task.digest, "session", "/project")
    const runner = new Runner(data.store, source)
    await runner.tick(data.task.id)
    await runner.tick(data.task.id)
    source.fail = true
    await runner.tick(data.task.id)
    await runner.tick(data.task.id)
    const task = data.store.get(data.task.id)
    const stages = data.store.stages(task.id)
    expect(task.state).toBe("FAILED")
    const receipt = path.join(data.folder, "failure.json")
    const evidence = { version: "screening-failure-evidence-v1", task_id: task.id, plan_digest: task.digest,
      phase: "carsidock", status: "FAILED", started_at: 10, runtime_seconds: 0.2,
      artifacts: { "error.json": { sha256: "a".repeat(64), bytes: 100 }, "started.json": { sha256: "b".repeat(64), bytes: 70 } },
      scientific_quality_verified: false, contents_included: false }
    await Bun.write(receipt, JSON.stringify(evidence))
    // A filesystem transport fixture, independent of the actual Python file
    // collector tests. Actual core subprocesses/SQLite establish failure state.
    const calls: (string | undefined)[] = []
    const transport = { verify: source.verify.bind(source),
      async failure(_task: Task, _stage: Stage, witness?: string) {
        calls.push(witness)
        if (witness && mode === "unavailable") throw new Error("Fixture read interrupted")
        const value = await Bun.file(receipt).json()
        return witness && mode === "changed" ? { ...value, runtime_seconds: 1 } : value
      } }
    const controller = new AbortController()
    if (mode === "aborted") controller.abort()
    if (mode === "wrong_phase") await Bun.write(receipt, JSON.stringify({ ...evidence, phase: "rtmscore" }))
    if (mode === "upstream_changed") await Bun.write(path.join(source.root, stages[0].name, "artifact.txt"), "changed")
    try {
      const result = await readFailureReport(data.store, task.id, transport, controller.signal)
      expect(result.report_ready).toBe(false)
      expect(result.failure_evidence_ready).toBe(mode === "verified")
      expect((await readReport(task, stages, source)).report_ready).toBe(false)
      const rows = data.store.db.query("SELECT payload FROM events WHERE task_id=? AND kind='failure_evidence_witness'").all(task.id)
      expect(rows).toHaveLength(["verified", "changed", "unavailable"].includes(mode) ? 1 : 0)
      if (rows.length) {
        const filename = data.store.db.filename
        const reopened = new Store(filename)
        try {
          const count = calls.length
          const next = await readFailureReport(reopened, task.id, transport)
          expect(next.failure_evidence_ready).toBe(mode === "verified")
          expect(calls.slice(count)).toEqual([digest(evidence)])
          expect(reopened.get(task.id)).toEqual(task)
          expect(reopened.stages(task.id)).toEqual(stages)
        } finally { reopened.close() }
      }
      expect(data.store.get(task.id)).toEqual(task)
      expect(data.store.stages(task.id)).toEqual(stages)
      expect(stages.slice(2).every((stage) => stage.submitted === null)).toBe(true)
      expect(data.store.db.query("SELECT * FROM artifact_publications").all()).toEqual([])
      expect(data.store.db.query("SELECT * FROM deliveries").all()).toEqual([])
      for (const value of [{ ...evidence, scientific_quality_verified: true },
        { ...evidence, artifacts: { ...evidence.artifacts, "poses.sdf": { sha256: "c".repeat(64), bytes: 20 } } },
        { ...evidence, artifacts: { ...evidence.artifacts, "error.json": { sha256: "a".repeat(64), bytes: 0 } } }]) {
        expect(() => verifyFailureEvidence(value, task, stages)).toThrow()
      }
      expect(() => verifyFailureEvidence(evidence, { ...task, approval: null }, stages)).toThrow()
      expect(() => verifyFailureEvidence(evidence, task, stages.map((stage, index) => index === 2
        ? { ...stage, state: "RUNNING", submitted: Date.now() } : stage))).toThrow()
    } finally { data.store.close() }
  })
}

// Files are authoritative observations from actual subprocesses. These tests
// cover orchestration only; they make NO claim about chemistry/GPU validation.
test("paired zero-selection diagnostics join failed-task delivery without granting stage success", async () => {
  const folder = await mkdtemp(path.join(tmpdir(), "zero-diagnostic-delivery-"))
  const data = await setup({ ...plan(), directory: folder, delivery: defaultDelivery })
  const source = new LocalProcesses(path.join(data.folder, "jobs"))
  source.fail = true
  data.store.approve(data.task.id, data.task.digest, "session", folder)
  const runner = new Runner(data.store, source)
  await runner.tick(data.task.id)
  await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stages = data.store.stages(task.id)
  const files = ["error.json", "started.json", "zero-candidates.json", "stage2_selection_manifest.json"]
  const directory = path.join(source.root, stages[0].name)
  for (const name of files) await Bun.write(path.join(directory, name), JSON.stringify({ synthetic: true, name }))
  const evidence = { version: "screening-failure-evidence-v1", task_id: task.id, plan_digest: task.digest,
    phase: "prepare", status: "FAILED", started_at: 10, runtime_seconds: 0.2,
    artifacts: Object.fromEntries(await Promise.all(files.map(async (name) => {
      const content = await Bun.file(path.join(directory, name)).arrayBuffer()
      return [name, { sha256: new Bun.CryptoHasher("sha256").update(content).digest("hex"), bytes: content.byteLength }]
    }))), scientific_quality_verified: false, contents_included: false }
  const transport = { verify: source.verify.bind(source), async failure() { return evidence },
    async download(_task: Task, remote: string, target: string) { await copyFile(path.join(directory, path.basename(remote)), target) } }
  try {
    // Python tests independently recompute the scientific diagnostic. This
    // boundary tests only witness allowlisting, actual bytes and native delivery.
    expect(task.state).toBe("FAILED")
    expect((await readFailureReport(data.store, task.id, transport)).failure_evidence_ready).toBe(true)
    expect(failureInventory(data.store, task).map(item => item.name).sort()).toEqual(files.sort())
    const delivered = await deliverArtifact(task, stages, { phase: "prepare", name: "zero-candidates.json" },
      { directory: folder, sessionID: "session", abort: new AbortController().signal }, transport, MAX_DOWNLOAD_BYTES, data.store)
    expect(delivered.evidence_kind).toBe("failure_diagnostic")
    expect(delivered.sha256).toBe(evidence.artifacts["zero-candidates.json"].sha256)
    expect(data.store.get(task.id).state).toBe("FAILED")
    expect(data.store.stages(task.id).every(stage => stage.manifest === null)).toBe(true)
    expect(() => downloadSpec(task, stages, { phase: "prepare", name: "covindb_master.parquet" }, data.store)).toThrow()
    for (const name of ["zero-candidates.json", "stage2_selection_manifest.json"]) {
      const artifacts = { ...evidence.artifacts }
      delete artifacts[name]
      expect(() => verifyFailureEvidence({ ...evidence, artifacts }, task, stages)).toThrow()
    }
    expect(() => verifyFailureEvidence({ ...evidence, artifacts: { ...evidence.artifacts,
      "../zero-candidates.json": evidence.artifacts["zero-candidates.json"] } }, task, stages)).toThrow()
    expect(() => verifyFailureEvidence({ ...evidence, artifacts: { ...evidence.artifacts,
      "zero-candidates.json": { sha256: "a".repeat(64), bytes: 65537 } } }, task, stages)).toThrow()
  } finally { data.store.close() }
})

class LocalProcesses implements Transport {
  loseReply = false
  fail = false
  extraReports = 0
  constructor(readonly root: string) {}
  async prepare() { await mkdir(this.root, { recursive: true }) }
  async preflight() {}
  async submit(task: Task, stage: Stage) {
    const request = path.join(this.root, stage.name + ".dispatch.json")
    // Exclusive reservation verifies that the real runner never dispatches twice.
    const file = Bun.file(request)
    if (await file.exists()) throw new Error("DUPLICATE DISPATCH")
    await Bun.write(file, JSON.stringify({ task, stage, root: this.root, fail: this.fail, extraReports: this.extraReports }))
    const child = Bun.spawn([process.execPath, path.join(import.meta.dir, "local-fixture.ts"), request], { stdout: "pipe", stderr: "pipe" })
    const [code, error] = await Promise.all([child.exited, new Response(child.stderr).text()])
    if (code) throw new Error(error)
    if (this.loseReply) { this.loseReply = false; throw new Error("Reply lost after actual subprocess completed") }
    return { handle: stage.name }
  }
  async observe(_: Task, stage: Stage): Promise<Observation> {
    const file = Bun.file(path.join(this.root, stage.name, "state.json"))
    return await file.exists() ? file.json() : { state: "missing" }
  }
  async verify(_: Task, stage: Stage) {
    const manifest = await Bun.file(path.join(this.root, stage.name, "manifest.json")).json()
    for (const [file, hash] of Object.entries(manifest.artifacts)) {
      const bytes = await Bun.file(path.join(this.root, stage.name, file)).arrayBuffer()
      if (new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== hash) throw new ReviewRequired("Artifact checksum changed")
    }
    return manifest
  }
  async stop(_: Task, stage: Stage) { await Bun.write(path.join(this.root, stage.name, "state.json"), JSON.stringify({ state: "cancelled", handle: stage.name })) }
}

async function setup(value = plan()) {
  const folder = await mkdtemp(path.join(tmpdir(), "screening-executor-test-"))
  const file = path.join(folder, "tasks.sqlite")
  const store = new Store(file)
  const task = store.create(value)
  return { folder, file, store, task }
}

test("plan requires real scope, ordered phases and matching resources; caps cannot be exceeded", () => {
  const base = plan()
  expect(digest({ b: 2, a: 1 })).toBe(digest({ a: 1, b: 2 }))
  for (const value of [ { ...base, scope: "full" }, { ...base, shortlist: 10001 }, { ...base, max_points: 0 },
    { ...base, assets: {} }, { ...base, steps: [...base.steps].reverse() },
    { ...base, steps: base.steps.map((step) => ({ ...step, resource: base.steps[0].resource })) },
    { ...base, human_approved: true }, { ...base, command: "anything" },
  ]) expect(planSchema.safeParse(value).success).toBe(false)
  const source = { user_message_id: "user", assistant_message_id: "assistant", part_ids: ["part"], request_sha256: requestHash(base.request) }
  expect(planSchema.safeParse({ ...base, origin: source }).success).toBe(true)
  expect(planSchema.safeParse({ ...base, origin: source, request: "model rewrote the request" }).success).toBe(false)
  expect(planSchema.parse(base).origin).toBeUndefined() // Old frozen plans keep their original digest.
})

test("refinement requires an explicit small-subset policy and is not a compute approval", () => {
  const policy = { max_candidates: 3, max_fraction: 0.25, disagreement_threshold: 0.5, top_fraction: 0.3,
    uncertainty_count: 1, consensus_count: 1, diversity_count: 1 }
  const input = { action: "select_refinement", task_id: "screen-" + "a".repeat(24), selection_policy: policy }
  expect(schema.safeParse(input).success).toBe(true)
  for (const invalid of [{ ...input, selection_policy: undefined }, { ...input, human_approved: true },
    { ...input, action: "approve" }, { ...input, task_id: undefined }, { ...input, action: "create" }]) {
    expect(schema.safeParse(invalid).success).toBe(false)
  }
  for (const invalid of [{ ...policy, max_fraction: 1 }, { ...policy, max_candidates: 101 },
    { ...policy, uncertainty_count: 3 }, { ...policy, uncertainty_count: 0 }, { ...policy, disagreement_threshold: 0 }]) {
    expect(selection.safeParse(invalid).success).toBe(false)
  }
})

test("SQLite persistence retains approval, immutable plan and event trail across reopen", async () => {
  const data = await setup()
  expect(() => data.store.approve(data.task.id, "c".repeat(64), "session", "/project")).toThrow()
  expect(() => data.store.approve(data.task.id, data.task.digest, "another", "/project")).toThrow()
  expect(() => data.store.approve(data.task.id, data.task.digest, "session", "/project", data.task.plan.approve_before + 1)).toThrow()
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  data.store.close()
  const store = new Store(data.file)
  expect(store.get(data.task.id).state).toBe("APPROVED")
  expect(JSON.parse(store.get(data.task.id).approval!).digest).toBe(data.task.digest)
  expect(store.events(data.task.id).length).toBe(2)
  expect(() => store.approve(data.task.id, data.task.digest, "session", "/project")).toThrow()
  store.db.query("UPDATE tasks SET plan=? WHERE id=?").run(JSON.stringify({ ...data.task.plan, shortlist: 2 }), data.task.id)
  expect(() => store.get(data.task.id)).toThrow()
  store.close()
})

test("a changed account cannot stage, submit, observe, verify or stop an approved task", async () => {
  const data = await setup()
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  const transport = new Inspire({ ...data.task.plan.connection, account: "different-account" })
  const stage = data.store.stages(data.task.id)[0]
  for (const action of [() => transport.prepare(data.task), () => transport.preflight(data.task, stage),
    () => transport.submit(data.task, stage), () => transport.observe(data.task, stage),
    () => transport.verify(data.task, stage), () => transport.stop(data.task, stage)]) {
    await expect(action()).rejects.toThrow("Connection differs")
  }
  await new Runner(data.store, transport).tick(data.task.id)
  expect(data.store.get(data.task.id).state).toBe("NEEDS_REVIEW")
  expect(data.store.stages(data.task.id)[0].state).toBe("WAITING")
  expect(data.store.events(data.task.id).some((row) => (row as { kind: string }).kind === "dispatch_reserved")).toBe(false)
  data.store.close()
})

test("two SQLite connections cannot dispatch the same task or skip a parent", async () => {
  const data = await setup()
  const other = new Store(data.file)
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  expect(data.store.acquire(data.task.id, "worker1")).toBe(true)
  expect(other.acquire(data.task.id, "worker2")).toBe(false)
  expect(() => data.store.claim(data.task.id, 1, "worker1")).toThrow()
  data.store.claim(data.task.id, 0, "worker1")
  expect(() => data.store.claim(data.task.id, 0, "worker1")).toThrow()
  expect(other.stages(data.task.id)[0].state).toBe("DISPATCHING")
  other.close(); data.store.close()
})

test("real child-process stages advance after lost reply and process state reopen without replay", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  transport.loseReply = true
  await new Runner(data.store, transport).tick(data.task.id)
  expect(data.store.stages(data.task.id)[0].state).toBe("WAITING")
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  await new Runner(data.store, transport).tick(data.task.id)
  expect(data.store.stages(data.task.id)[0].state).toBe("DISPATCHING")
  data.store.close()
  const store = new Store(data.file)
  const runner = new Runner(store, transport)
  for (let i = 0; i < 10; i++) await runner.tick(data.task.id)
  expect(store.get(data.task.id).state).toBe("SUCCEEDED")
  expect(store.stages(data.task.id).every((stage) => stage.state === "SUCCEEDED")).toBe(true)
  expect(store.events(data.task.id).filter((row) => (row as { kind: string }).kind === "dispatch_reserved").length).toBe(4)
  store.close()
})

test("submission acknowledgment and pending observations do not claim actual running", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  const runner = new Runner(data.store, transport)
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  await runner.tick(data.task.id)
  expect(data.store.stages(data.task.id)[0].state).toBe("SUBMITTED")
  const stage = data.store.stages(data.task.id)[0]
  const state = path.join(transport.root, stage.name, "state.json")
  await Bun.write(state, JSON.stringify({ state: "pending", handle: stage.name }))
  await runner.tick(data.task.id)
  expect(data.store.stages(data.task.id)[0].state).toBe("QUEUED")
  await Bun.write(state, JSON.stringify({ state: "running", handle: stage.name }))
  await runner.tick(data.task.id)
  expect(data.store.stages(data.task.id)[0].state).toBe("RUNNING")
  expect(data.store.events(data.task.id).filter((event) => (event as { kind: string }).kind === "dispatch_reserved")).toHaveLength(1)
  data.store.close()
})

test("cancelled unapproved draft requires no resources and never dispatches", async () => {
  const data = await setup()
  data.store.cancel(data.task.id, "/project")
  await new Runner(data.store, new LocalProcesses(path.join(data.folder, "jobs"))).tick(data.task.id)
  expect(data.store.get(data.task.id).state).toBe("CANCELLED")
  expect(data.store.events(data.task.id).some((row) => (row as { kind: string }).kind === "dispatch_reserved")).toBe(false)
  data.store.close()
})

test("report readiness requires fresh real files, matching saved evidence and complete core phases", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  const runner = new Runner(data.store, transport)
  const read = () => readReport(data.store.get(data.task.id), data.store.stages(data.task.id), transport)
  // No manifest files exist. An unapproved report read must not even try to read them.
  expect((await read()).verification).toBe("not_ready")
  expect(await Bun.file(path.join(transport.root, `${data.task.id}-prepare.dispatch.json`)).exists()).toBe(false)
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  for (let i = 0; i < 9; i++) await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stages = data.store.stages(task.id)
  const before = data.store.events(task.id).length
  const result = await read()
  expect(result.report_ready).toBe(true)
  expect(result.verification).toBe("verified")
  expect(result.artifacts.map((entry) => entry.phase)).toEqual(["prepare", "carsidock", "rtmscore", "report"])
  expect(result.artifacts[0].files[0].path).toBe(`/inspire/root/tasks/${task.id}/prepare/artifact.txt`)
  expect(result.artifacts[0].files[0].sha256).toBe(JSON.parse(stages[0].manifest!).artifacts["artifact.txt"])
  expect(JSON.stringify(result)).not.toContain("orchestration-fixture-not-scientific-output")
  expect(data.store.events(task.id).length).toBe(before)
  expect((await readReport(task, stages.slice(1), transport)).report_ready).toBe(false)
  expect((await readReport(task, [...stages].reverse(), transport)).verification).toBe("evidence_rejected")
  expect((await readReport({ ...task, approval: null }, stages, transport)).verification).toBe("evidence_rejected")

  const manifest = path.join(transport.root, stages[3].name, "manifest.json")
  const original = await Bun.file(manifest).text()
  await Bun.write(manifest, JSON.stringify({ ...JSON.parse(original), runtime_seconds: 2 }))
  expect((await read()).verification).toBe("evidence_rejected")
  await Bun.write(manifest, original)
  const artifact = path.join(transport.root, stages[3].name, "artifact.txt")
  const bytes = await Bun.file(artifact).text()
  await Bun.write(artifact, "changed after successful execution")
  const corrupt = await read()
  expect(corrupt.report_ready).toBe(false)
  expect(corrupt.verification).toBe("evidence_rejected")
  expect(corrupt.artifacts).toEqual([])
  await Bun.write(artifact, bytes)
  // A disappeared artifact is unavailable, not evidence that a remote job failed.
  await Bun.file(artifact).delete()
  expect((await read()).verification).toBe("verification_unavailable")
  expect(data.store.get(task.id).state).toBe("SUCCEEDED")
  expect(data.store.stages(task.id)).toEqual(stages)
  expect(data.store.events(task.id).length).toBe(before)
  await Bun.write(artifact, bytes)
  expect((await read()).report_ready).toBe(true)
  const upstream = path.join(transport.root, stages[0].name, "artifact.txt")
  await Bun.write(upstream, "changed upstream evidence")
  expect((await read()).verification).toBe("evidence_rejected")
  await Bun.write(upstream, bytes)
  expect((await read()).report_ready).toBe(true)
  data.store.close()
})

test("report inventories are bounded but every listed manifest artifact is rehashed", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  const runner = new Runner(data.store, transport)
  for (let i = 0; i < 9; i++) await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stage = data.store.stages(task.id)[3]
  const manifest = JSON.parse(stage.manifest!)
  for (let i = 0; i < 60; i++) {
    const name = `extra-${String(i).padStart(2, "0")}.txt`
    const content = `non-scientific inventory fixture ${i}`
    await Bun.write(path.join(transport.root, stage.name, name), content)
    manifest.artifacts[name] = new Bun.CryptoHasher("sha256").update(content).digest("hex")
  }
  // Only test fixtures are altered; production saved manifests remain immutable.
  await Bun.write(path.join(transport.root, stage.name, "manifest.json"), JSON.stringify(manifest))
  data.store.stage(task.id, 3, { manifest: JSON.stringify(manifest) })
  const read = () => readReport(task, data.store.stages(task.id), transport)
  const result = await read()
  expect(result.report_ready).toBe(true)
  expect(result.artifacts[3].artifact_count).toBe(61)
  expect(result.artifacts[3].truncated).toBe(true)
  expect(result.artifacts[3].files.length).toBe(50)
  await Bun.write(path.join(transport.root, stage.name, "extra-59.txt"), "not in displayed inventory, but still checked")
  expect((await read()).report_ready).toBe(false)
  data.store.close()
})

test("artifact inventory rejects escaping, ambiguous and control-character paths", async () => {
  const data = await setup()
  const stage = data.store.stages(data.task.id)[0]
  const base = { task_id: data.task.id, phase: "prepare", plan_digest: data.task.digest, status: "SUCCEEDED", parent_digest: null,
    input_count: 3, success_count: 3, failed_count: 0, identity_digest: "a".repeat(64), gates: { qc: true }, runtime_seconds: 1 }
  for (const name of ["", "/etc/passwd", "../escape", "a/../../escape", "a/./b", "a//b", "a\\b", "a\nb", "a\u0000b", "a/"]) {
    expect(() => verifyOutcome({ ...base, artifacts: { [name]: "a".repeat(64) } }, data.task, stage, null)).toThrow()
  }
  expect(() => verifyOutcome({ ...base, artifacts: { "poses/molecule-1.sdf": "a".repeat(64) } }, data.task, stage, null)).not.toThrow()
  data.store.close()
})

test("execution summaries distinguish reserved names from submissions and expose automatic snapshot semantics", async () => {
  const data = await setup()
  try {
    const draft = summary(data.task, data.store.stages(data.task.id))
    expect(draft.execution.approval_recorded).toBe(false)
    expect(draft.execution.controller).toBe("platform-daemon")
    expect(draft.execution.mode).toBe("automatic-after-native-approval")
    expect(draft.execution.snapshot_at).toBeGreaterThan(0)
    expect(draft.stages?.every((stage) => stage.job === null && stage.submitted_at === null && stage.planned_job)).toBe(true)
    data.store.approve(data.task.id, data.task.digest, "session", "/project")
    const approved = summary(data.store.get(data.task.id), data.store.stages(data.task.id))
    expect(approved.execution.approval_recorded).toBe(true)
    expect(approved.execution.note).toContain("WAITING does not mean the user must call wake")
    expect(approved.stages?.every((stage) => stage.job === null)).toBe(true)
    const runner = new Runner(data.store, new LocalProcesses(path.join(data.folder, "jobs")))
    await runner.tick(data.task.id)
    const started = summary(data.store.get(data.task.id), data.store.stages(data.task.id))
    expect(started.stages?.[0].job).toBe(data.store.stages(data.task.id)[0].handle)
    expect(started.stages?.[0].job).not.toBeNull()
    expect(started.stages?.[0].submitted_at).toBeGreaterThan(0)
    expect(started.stages?.slice(1).every((stage) => stage.job === null && stage.submitted_at === null)).toBe(true)
    expect(started.execution.note).toContain("RUNNING requires a live running observation")
  } finally { data.store.close() }
})

test("real failed subprocess stops downstream phases and persists a task-level reason", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  transport.fail = true
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  const runner = new Runner(data.store, transport)
  await runner.tick(data.task.id)
  await runner.tick(data.task.id)
  await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stages = data.store.stages(task.id)
  expect(task.state).toBe("FAILED")
  expect(task.error).toContain("prepare: authoritative remote terminal state failed")
  expect(stages.slice(1).every((stage) => stage.state === "WAITING" && stage.submitted === null)).toBe(true)
  expect(data.store.events(task.id).filter((row) => (row as { kind: string }).kind === "dispatch_reserved").length).toBe(1)
  expect(summary({ ...task, error: null }, stages).error).toBe(stages[0].error)
  expect(summary(task, stages).failed_stage).toBe("prepare")
  data.store.close()
})

test("changed real output pauses verification and cannot start the next stage", async () => {
  const data = await setup()
  const transport = new LocalProcesses(path.join(data.folder, "jobs"))
  const runner = new Runner(data.store, transport)
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  await runner.tick(data.task.id)
  const stage = data.store.stages(data.task.id)[0]
  await Bun.write(path.join(transport.root, stage.name, "artifact.txt"), "corrupted fixture")
  await runner.tick(data.task.id)
  expect(data.store.get(data.task.id).state).toBe("NEEDS_REVIEW")
  expect(data.store.stages(data.task.id)[0].state).toBe("VERIFYING")
  const count = data.store.events(data.task.id).length
  await runner.tick(data.task.id)
  expect(data.store.events(data.task.id).length).toBe(count)
  expect(data.store.stages(data.task.id)[1].state).toBe("WAITING")
  data.store.cancel(data.task.id, "/project")
  await runner.tick(data.task.id)
  expect(data.store.get(data.task.id).state).toBe("CANCELLED")
  data.store.close()
})

test("older active tasks remain visible after more than 200 newer drafts", async () => {
  const data = await setup()
  data.store.approve(data.task.id, data.task.digest, "session", "/project")
  for (let index = 0; index < 201; index++) {
    data.store.create({ ...plan(), id: "screen-" + index.toString(16).padStart(24, "0"), directory: "/another-project" })
  }
  expect(data.store.list().length).toBe(202)
  expect(data.store.list("/project").map((task) => task.id)).toEqual([data.task.id])
  data.store.close()
})

test("identity, failure denominator and parent hashes are verified independently of success flags", async () => {
  const data = await setup()
  const stage = data.store.stages(data.task.id)[0]
  const result = { task_id: data.task.id, phase: "prepare", plan_digest: data.task.digest, status: "SUCCEEDED", parent_digest: null,
    input_count: 3, success_count: 3, failed_count: 0, artifacts: { "a.txt": "a".repeat(64) }, identity_digest: "a".repeat(64), gates: { qc: true }, runtime_seconds: 1 }
  expect(() => verifyOutcome(result, data.task, stage, null)).not.toThrow()
  for (const change of [{ failed_count: 1 }, { input_count: 4, success_count: 4 }, { artifacts: {} }, { gates: {} },
    { gates: { qc: false } }, { task_id: "elsewhere" }, { parent_digest: "a".repeat(64) }]) {
    expect(() => verifyOutcome({ ...result, ...change }, data.task, stage, null)).toThrow()
  }
  data.store.close()
})

test("actual CLI vectors use bounded srun HPC and single GPU jobs; status is not business completion", async () => {
  const data = await setup()
  const stages = data.store.stages(data.task.id)
  for (const stage of stages) {
    const args = createArgs(data.task.plan, stage, true)
    expect(args).toContain("--dry-run")
    expect(args).toContain("--max-time")
    expect(args).toContain("--public-path-readonly")
    expect(args).toContain("--no-enable-notification")
    if (stage.phase === "prepare" || stage.phase === "report") expect(args[args.indexOf("--entrypoint") + 1]).toStartWith("srun ")
    else expect(args).toContain("--no-auto-fault-tolerance")
  }
  expect(() => observation({ name: stages[0].name, status: "SUCCEEDED", steps: "0/0" }, stages[0], data.task.plan.steps[0].resource)).toThrow()
  expect(observation({ name: stages[0].name, status: "SUCCEEDED", steps: "1/1" }, stages[0], data.task.plan.steps[0].resource).state).toBe("succeeded")
  expect(() => observation({ name: stages[0].name, status: "UNKNOWN" }, stages[0], data.task.plan.steps[0].resource)).toThrow()
  for (const status of ["job_queuing", "job_creating"]) {
    expect(observation({ name: stages[1].name, status }, stages[1], data.task.plan.steps[1].resource).state).toBe("pending")
  }
  data.store.close()
})

test("Live namespace mapping preserves project, workload and resource identity checks", async () => {
  const data = await setup()
  const stage = data.store.stages(data.task.id)[0]
  const spec = data.task.plan.steps[0].resource
  const identity = { name: spec.project, english_name: "research-namespace" }
  const raw = { name: stage.name, project: identity.english_name, status: "SUCCEEDED", steps: "1/1",
    compute_group: spec.group, resource: { gpu: 0, cpu: 4, memory_gib: 16, nodes: 1 } }
  expect(observation(projectObservation(raw, spec.project, identity), stage, spec).state).toBe("succeeded")
  expect(() => projectObservation(raw, spec.project, { ...identity, name: "another" })).toThrow()
  expect(() => projectObservation({ ...raw, project: "another" }, spec.project, identity)).toThrow()
  expect(() => projectObservation(raw, spec.project, { name: spec.project })).toThrow()
  for (const changed of [{ name: "another" }, { compute_group: "another" }, { steps: "0/0" },
    { resource: { ...raw.resource, nodes: 2 } }, { resource: { ...raw.resource, cpu: 8 } }]) {
    expect(() => observation(projectObservation({ ...raw, ...changed }, spec.project, identity), stage, spec)).toThrow()
  }
  data.store.close()
})

test("approval interruption cannot cancel another scope or roll back an already granted task", async () => {
  const data = await setup()
  try {
    const task = data.task
    expect(() => data.store.endApproval(task.id, "f".repeat(64), "session", "/project", "test")).toThrow()
    expect(() => data.store.endApproval(task.id, task.digest, "other-session", "/project", "test")).toThrow()
    expect(() => data.store.endApproval(task.id, task.digest, "session", "/other-project", "test")).toThrow()
    expect(data.store.get(task.id).state).toBe("DRAFT")
    data.store.approve(task.id, task.digest, "session", "/project")
    const receipt = data.store.get(task.id).approval
    expect(data.store.endApproval(task.id, task.digest, "session", "/project", "late error").state).toBe("APPROVED")
    expect(data.store.get(task.id).approval).toBe(receipt)
    expect(data.store.get(task.id).error).toBeNull()
  } finally { data.store.close() }
})

test("native abort closes and persists the real task card through the host outcome coordinator", async () => {
  await using tmp = await fixture({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    const value = { ...plan(), directory: tmp.path, session: session.id, delivery: defaultDelivery }
    const store = new Store(path.join(tmp.path, "tasks.sqlite"))
    const controller = new AbortController()
    const args = { action: "create" }
    const part: MessageV2.ToolPart = { id: Identifier.ascending("part"), messageID: Identifier.ascending("message"),
      sessionID: session.id, type: "tool", tool: "inspire_screening_task", callID: "native-abort-test",
      state: { status: "running", input: args, time: { start: Date.now() } } }
    const coordinator = SessionProcessor.createToolOutcomeCoordinator({ abort: controller.signal, updatePart: Session.updatePart })
    await coordinator.running(part)
    const asked = Promise.withResolvers<PermissionNext.Request>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
      if (event.properties.sessionID === session.id) asked.resolve(event.properties)
    })
    try {
      const result = coordinator.execute(part.callID, args, async () => {
        const task = await propose(store, value, { sessionID: session.id, directory: tmp.path, abort: controller.signal,
          metadata: (metadata) => coordinator.metadata(part.callID, args, metadata),
          ask: (input) => PermissionNext.ask({ ...input, sessionID: session.id, signal: controller.signal, ruleset: [] }),
        })
        return { title: "approved", output: JSON.stringify(task), metadata: {} }
      }).then(() => "unexpected approval", () => "interrupted")
      const card = await asked.promise
      controller.abort()
      expect(await result).toBe("interrupted")
      expect(await PermissionNext.list()).toEqual([])
      const saved = (await MessageV2.parts(part.messageID)).find((item) => item.id === part.id)!
      if (saved.type !== "tool" || saved.state.status !== "error") throw new Error("Native interrupted result was not retained")
      const data = inspireProgress(saved.state.metadata?.inspire_progress)
      expect(data?.state).toBe("CANCELLED")
      expect(data?.error).toContain("未批准计算")
      expect(data?.delivery).toBeNull()
      expect(data?.stages.every((stage) => stage.state === "WAITING" && stage.job === null)).toBe(true)
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect(store.get(value.id).approval).toBeNull()
      await new Runner(store, new LocalProcesses(path.join(tmp.path, "never-launched"))).tick(value.id)
      expect(store.stages(value.id).every((stage) => stage.submitted === null)).toBe(true)
      expect(store.get(value.id).state).toBe("CANCELLED")
    } finally { unsubscribe(); store.close() }
  } })
})

// Actual native permission service + SQLite + local process runner. No model,
// remote CLI or paid compute is involved in these boundary tests.
for (const decision of ["once", "reject", "abort", "cancel", "expire", "tamper"] as const) {
  test(`create automatically persists then requests native approval: ${decision}`, async () => {
    await using tmp = await fixture({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const store = new Store(path.join(tmp.path, "tasks.sqlite"))
      const value = { ...plan(), delivery: defaultDelivery, directory: tmp.path, session: `session_handoff_${decision}`,
        ...(decision === "expire" ? { approve_before: Date.now() + 300 } : {}) }
      const controller = new AbortController()
      const requested = Promise.withResolvers<PermissionNext.Request>()
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID === value.session) requested.resolve(event.properties)
      })
      const pending = propose(store, value, { sessionID: value.session, directory: tmp.path, abort: controller.signal,
        metadata: (metadata) => { expect(metadata.metadata?.task_id).toBe(value.id) },
        ask: (input) => PermissionNext.ask({ ...input, sessionID: value.session, signal: controller.signal,
          // Even a global wildcard allow must not bypass exact-plan approval.
          ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }),
      }).then((task) => ({ task, error: undefined }), (error: unknown) => ({ task: undefined, error }))
      try {
        const card = await requested.promise
        expect(card.permission).toBe("remote_compute")
        expect(card.patterns).toEqual([digest(value)])
        expect(card.metadata.compute.task_id).toBe(value.id)
        expect(card.metadata.compute.stages).toEqual(value.steps)
        expect(card.metadata.compute.library).toBe(value.library)
        expect(card.metadata.compute.source_scope).toBe(value.scope)
        expect(card.metadata.compute.max_records).toBe(value.max_records)
        expect(card.metadata.compute.quality_gates).toEqual(value.gates)
        expect(card.metadata.compute.seed).toBe(value.seed)
        expect(card.metadata.compute.delivery_policy).toEqual(defaultDelivery)
        expect(store.get(value.id).state).toBe("DRAFT")
        expect(store.get(value.id).approval).toBeNull()
        const reopened = new Store(path.join(tmp.path, "tasks.sqlite"))
        expect(reopened.get(value.id).state).toBe("DRAFT")
        reopened.close()
        await new Runner(store, new LocalProcesses(path.join(tmp.path, "jobs"))).tick(value.id)
        expect(store.stages(value.id).every((stage) => stage.state === "WAITING" && stage.handle === null && stage.submitted === null)).toBe(true)
        expect(store.db.query("SELECT * FROM daemon").all()).toEqual([])
        if (decision === "abort") controller.abort()
        if (decision === "cancel") store.cancel(value.id, tmp.path)
        if (decision === "expire") {
          await Bun.sleep(Math.max(0, value.approve_before - Date.now() + 10))
        }
        if (decision === "tamper") store.db.query("UPDATE tasks SET plan=? WHERE id=?").run(JSON.stringify({ ...value, shortlist: 2 }), value.id)
        if (decision !== "abort") await PermissionNext.reply({ requestID: card.id, reply: decision === "reject" ? "reject" : "once" })
        const result = await pending
        if (decision === "once") {
          expect(result.error).toBeUndefined()
          expect(result.task?.state).toBe("APPROVED")
          expect(JSON.parse(result.task!.approval!).digest).toBe(digest(value))
        } else {
          expect(result.error).toBeDefined()
          expect(store.db.query("SELECT approval FROM tasks WHERE id=?").get(value.id)).toEqual({ approval: null })
          if (decision !== "tamper") {
            expect(store.get(value.id).state).toBe("CANCELLED")
            expect(store.get(value.id).error).toContain("未批准计算")
            const recovered = new Store(path.join(tmp.path, "tasks.sqlite"))
            expect(recovered.get(value.id).state).toBe("CANCELLED")
            expect(() => recovered.approve(value.id, digest(value), value.session, tmp.path)).toThrow("unchanged draft")
            recovered.close()
          }
        }
        expect((await PermissionNext.list()).filter((request) => request.sessionID === value.session)).toEqual([])
        expect(store.stages(value.id).every((stage) => stage.submitted === null)).toBe(true)
      } finally { unsubscribe(); store.close() }
    } })
  })
}

test("artifact delivery copies witnessed bytes and the actual platform file route serves them without compute replay", async () => {
  await using tmp = await fixture({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup({ ...plan(), directory: tmp.path })
    const source = new LocalProcesses(path.join(data.folder, "jobs"))
    const context = { directory: tmp.path, sessionID: "session", abort: new AbortController().signal }
    const input = { phase: "report" as const, name: "artifact.txt" }
    const downloads: string[] = []
    const transport = { verify: source.verify.bind(source), download: async (task: Task, remote: string, file: string, limit: number) => {
      expect(remote).toBe(`${task.plan.runtime.root}/tasks/${task.id}/report/artifact.txt`)
      expect(limit).toBe(MAX_DOWNLOAD_BYTES)
      downloads.push(remote)
      await copyFile(path.join(source.root, task.id + "-report/artifact.txt"), file)
    } }
    const deliver = () => deliverArtifact(data.store.get(data.task.id), data.store.stages(data.task.id), input, context, transport)
    try {
      await expect(deliver()).rejects.toThrow("completed task")
      expect(downloads).toHaveLength(0)
      data.store.approve(data.task.id, data.task.digest, "session", tmp.path)
      const runner = new Runner(data.store, source)
      for (let index = 0; index < 9; index++) await runner.tick(data.task.id)
      const before = data.store.events(data.task.id).length
      const result = await deliver()
      expect(await Bun.file(result.local_path).text()).toBe(await Bun.file(path.join(source.root, data.task.id + "-report/artifact.txt")).text())
      expect(result.sha256).toBe(downloadSpec(data.store.get(data.task.id), data.store.stages(data.task.id), input).sha256)
      const response = await FileRoutes().request("/file/raw?path=" + encodeURIComponent(result.local_path))
      expect(response.status).toBe(200)
      expect(new Bun.CryptoHasher("sha256").update(await response.arrayBuffer()).digest("hex")).toBe(result.sha256)
      expect((await deliver()).local_path).toBe(result.local_path)
      expect(downloads).toHaveLength(1)
      expect(data.store.events(data.task.id)).toHaveLength(before)
      expect(await readdir(path.dirname(result.local_path))).toEqual([path.basename(result.local_path)])
      await chmod(result.local_path, 0o600)
      await Bun.write(result.local_path, "local file edited after delivery")
      await expect(deliver()).rejects.toThrow("never overwrite")
      expect(downloads).toHaveLength(1)
    } finally { data.store.close() }
  } })
})

test("artifact delivery rejects wrong scope, missing inventory, symlink destinations and changed transfer bytes", async () => {
  await using tmp = await fixture({ git: true })
  const data = await setup({ ...plan(), directory: tmp.path })
  const source = new LocalProcesses(path.join(data.folder, "jobs"))
  const context = { directory: tmp.path, sessionID: "session", abort: new AbortController().signal }
  const input = { phase: "report" as const, name: "artifact.txt" }
  data.store.approve(data.task.id, data.task.digest, "session", tmp.path)
  const runner = new Runner(data.store, source)
  for (let index = 0; index < 9; index++) await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stages = data.store.stages(data.task.id)
  const transport = { verify: source.verify.bind(source), download: async (_: Task, remote: string, file: string) => {
    await Bun.write(file, "bytes inconsistent with the witnessed report")
  } }
  try {
    await expect(deliverArtifact(task, stages, input, { ...context, sessionID: "other" }, transport)).rejects.toThrow("another conversation")
    for (const name of ["../plan.json", "unlisted.txt", "/etc/passwd", "a\\b"]) {
      await expect(deliverArtifact(task, stages, { ...input, name }, context, transport)).rejects.toThrow()
    }
    await expect(deliverArtifact(task, stages, input, context, transport)).rejects.toThrow("checksum differs")
    const destination = path.join(tmp.path, "inspire-results", task.id, "report")
    expect(await readdir(destination)).toEqual([])
    // Replacing an unused phase directory with a link must not write outside it.
    await symlink(data.folder, path.join(tmp.path, "inspire-results", task.id, "prepare"))
    await expect(deliverArtifact(task, stages, { ...input, phase: "prepare" }, context, transport)).rejects.toThrow("redirected")
  } finally { data.store.close() }
})

test("artifact delivery never publishes when remote evidence changes or the user aborts during transfer", async () => {
  await using tmp = await fixture({ git: true })
  const data = await setup({ ...plan(), directory: tmp.path })
  const source = new LocalProcesses(path.join(data.folder, "jobs"))
  data.store.approve(data.task.id, data.task.digest, "session", tmp.path)
  const runner = new Runner(data.store, source)
  for (let index = 0; index < 9; index++) await runner.tick(data.task.id)
  const task = data.store.get(data.task.id)
  const stages = data.store.stages(task.id)
  const original = path.join(source.root, task.id + "-report/artifact.txt")
  const bytes = await Bun.file(original).arrayBuffer()
  try {
    for (const action of ["change", "abort"] as const) {
      const controller = new AbortController()
      const transport = { verify: source.verify.bind(source), download: async (_: Task, remote: string, destination: string) => {
        await copyFile(original, destination)
        if (action === "change") await Bun.write(original, "remote bytes changed after transfer")
        if (action === "abort") controller.abort()
      } }
      await expect(deliverArtifact(task, stages, { phase: "report", name: "artifact.txt" },
        { directory: tmp.path, sessionID: "session", abort: controller.signal }, transport)).rejects.toThrow()
      expect(await readdir(path.join(tmp.path, "inspire-results", task.id, "report"))).toEqual([])
      await Bun.write(original, bytes)
    }
    expect(data.store.get(task.id).state).toBe("SUCCEEDED")
  } finally { data.store.close() }
})

test("download arguments cannot change the plan, and InspireSkill uses only bound exec and download subprocesses", async () => {
  const id = plan().id
  const valid = { action: "download", task_id: id, artifact: { phase: "report", name: "candidates.csv" } }
  expect(schema.safeParse(valid).success).toBe(true)
  for (const input of [{ action: "download", task_id: id }, { ...valid, max_points: 99 },
    { ...valid, artifact: { phase: "report", name: "../../secret" } }, { ...valid, destination: "/tmp/arbitrary" }]) {
    expect(schema.safeParse(input).success).toBe(false)
  }
  await using tmp = await fixture()
  const cli = path.join(tmp.path, "inspire-fixture")
  const source = path.join(tmp.path, "source.txt")
  const log = path.join(tmp.path, "argv.jsonl")
  await Bun.write(source, "explicit local transfer fixture")
  await Bun.write(cli, `#!${process.execPath}\nimport fs from "node:fs/promises";
const args = process.argv.slice(2); await fs.appendFile(${JSON.stringify(log)}, JSON.stringify(args)+"\\n");
if (args[5] === "exec") console.log(JSON.stringify({success:true,data:{returncode:0,output:"31\\n"}}));
else if (args[5] === "scp") { await fs.copyFile(${JSON.stringify(source)}, args.at(-1)); console.log(JSON.stringify({success:true,data:{}})); }
else process.exit(2);
`)
  await chmod(cli, 0o700)
  const value = { ...plan(), connection: { ...plan().connection, cli } }
  const task = { id: value.id, plan: value, digest: digest(value), state: "SUCCEEDED", approval: null, error: null } as Task
  const transport = new Inspire(value.connection)
  const remote = `${value.runtime.root}/tasks/${id}/report/candidates.csv`
  const destination = path.join(tmp.path, "copy.txt")
  await transport.download(task, remote, destination, MAX_DOWNLOAD_BYTES)
  expect(await Bun.file(destination).text()).toBe(await Bun.file(source).text())
  const args = (await Bun.file(log).text()).trim().split("\n").map((line) => JSON.parse(line) as string[])
  expect(args).toHaveLength(2)
  expect(args[0].slice(0, 9)).toEqual(["--json", "--no-env-file", "--account", "test-only", "notebook", "exec", "cpu", "--workspace", "CPU资源空间"])
  expect(args[1]).toEqual(["--json", "--no-env-file", "--account", "test-only", "notebook", "scp", "cpu", "--workspace", "CPU资源空间", "--download", "--timeout", "300", remote, destination])
  await expect(new Inspire({ ...value.connection, account: "other" }).download(task, remote, destination, MAX_DOWNLOAD_BYTES)).rejects.toThrow("Connection differs")
  await expect(transport.download(task, "/inspire/other-file", destination, MAX_DOWNLOAD_BYTES)).rejects.toThrow("outside")
  expect((await Bun.file(log).text()).trim().split("\n")).toHaveLength(2)
})

for (const mode of ["normal", "lost_reply", "corrupt_readback", "not_sent"] as const) {
  test(`verified delivery becomes a durable native Result without uncertain save replay: ${mode}`, async () => {
    await using tmp = await fixture({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const data = await setup({ ...plan(), directory: tmp.path, session: session.id })
      const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
        agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
      const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
        parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
        path: { cwd: tmp.path, root: tmp.path }, time: { created: Date.now(), completed: Date.now() },
        tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
      const original = await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: assistant.id,
        type: "tool", tool: "inspire_screening_task", callID: "fixture-publication-call",
        state: { status: "completed", input: { action: "create" }, title: "Original task approval",
          output: JSON.stringify({ task_id: data.task.id, digest: data.task.digest }),
          metadata: { preserved: "original" }, time: { start: Date.now(), end: Date.now() } } })
      if (original.type !== "tool") throw new Error("Fixture did not create a tool part")
      const source = new LocalProcesses(path.join(data.folder, "jobs"))
      data.store.approve(data.task.id, data.task.digest, session.id, tmp.path)
      const runner = new Runner(data.store, source)
      for (let index = 0; index < 9; index++) await runner.tick(data.task.id)
      const task = data.store.get(data.task.id)
      const transport = { verify: source.verify.bind(source), download: async (_: Task, remote: string, target: string) => {
        await copyFile(path.join(source.root, task.id + "-report/artifact.txt"), target)
      } }
      const file = await deliverArtifact(task, data.store.stages(task.id), { phase: "report", name: "artifact.txt" },
        { directory: tmp.path, sessionID: session.id, abort: new AbortController().signal }, transport)
      const fetcher = Server.internalFetch()
      const writes: string[] = []
      const intercepted = async (request: Request) => {
        if (request.method === "POST") {
          writes.push(request.url)
          if (mode === "not_sent") throw new Error("uncertain save before transmission")
        }
        const response = await fetcher(request)
        if (mode === "lost_reply" && request.method === "POST") throw new Error("simulated lost save response")
        if (mode === "corrupt_readback" && new URL(request.url).pathname.endsWith("/raw")) return new Response("wrong bytes")
        return response
      }
      try {
        const publishing = new Publications(data.store, "http://openscience.internal", intercepted)
        if (mode === "normal") await publishing.publish(task, file)
        if (mode !== "normal") {
          await expect(publishing.publish(task, file)).rejects.toThrow()
          expect(publications(data.store, task)).toEqual([])
        }
        expect(writes).toHaveLength(1)
        data.store.close()
        const reopened = new Store(data.file)
        try {
          const recovered = new Publications(reopened, "http://openscience.internal", fetcher)
          if (mode === "not_sent") {
            await expect(recovered.publish(task, file)).rejects.toThrow("uncertain outcome")
            expect(await ArtifactStore.list(Instance.project.id)).toEqual([])
            return
          }
          const result = await recovered.publish(task, file)
          expect(publications(reopened, task)).toEqual([result])
          expect((await ArtifactStore.get(Instance.project.id, result.artifact_id))?.versionCount).toBe(1)
          const bytes = await ArtifactStore.read(Instance.project.id, result.artifact_id, result.version_id)
          expect(new Bun.CryptoHasher("sha256").update(await bytes!.content.arrayBuffer()).digest("hex")).toBe(file.sha256)
          expect(reopened.get(task.id).state).toBe("SUCCEEDED")
          const progress = new Progress(reopened, "http://openscience.internal", fetcher)
          expect((await progress.publish(reopened.get(task.id))).delivered).toBe(true)
          const refreshed = MessageV2.WithParts.parse(await progress.request(task, `/message/${assistant.id}`))
          const card = refreshed.parts.find((part) => part.id === original.id)
          if (card?.type !== "tool" || card.state.status !== "completed" || original.state.status !== "completed") {
            throw new Error("Originating completed task card was lost")
          }
          expect(card.state.output).toBe(original.state.output)
          expect(card.state.input).toEqual(original.state.input)
          expect(card.state.metadata.preserved).toBe("original")
          expect(card.state.metadata.inspire_progress.results).toEqual([result])
          expect(card.state.metadata.inspire_results).toEqual([result])
          expect(card.state.metadata.inspire_progress.state).toBe("SUCCEEDED")
          // Reopen the journal as a replacement observer would, then read the
          // actual persisted message through the same API used after refresh.
          const restarted = new Store(data.file)
          try {
            const next = new Progress(restarted, "http://openscience.internal", fetcher)
            expect(await next.publish(restarted.get(task.id))).toEqual({ delivered: false, reason: "unchanged" })
            expect(await next.request(task, `/message/${assistant.id}`)).toEqual(refreshed)
            expect(restarted.events(task.id).filter((row) => (row as { kind: string }).kind === "dispatch_reserved")).toHaveLength(4)
          } finally { restarted.close() }
          await ArtifactStore.trash(Instance.project.id, result.artifact_id)
          await expect(recovered.publish(task, file)).rejects.toThrow("trashed")
          expect((await ArtifactStore.list(Instance.project.id, "trash"))).toHaveLength(1)
        } finally { reopened.close() }
      } finally { try { data.store.close() } catch {} }
    } })
  })
}

for (const mode of ["complete", "lost_reply", "missing", "changed", "corrupt", "file_limit", "byte_limit", "old_policy"] as const) {
  test(`automatic failed-core evidence delivery preserves failure and native Result provenance: ${mode}`, async () => {
    await using tmp = await fixture({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const policy = { ...defaultDelivery, ...(mode === "old_policy" ? { failure: undefined } : {}),
        ...(mode === "file_limit" ? { max_files: 2 } : {}), ...(mode === "byte_limit" ? { max_total_bytes: 10 } : {}) }
      const data = await setup({ ...plan(), directory: tmp.path, session: session.id, delivery: policy })
      const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
        agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
      const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
        parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
        path: { cwd: tmp.path, root: tmp.path }, time: { created: Date.now(), completed: Date.now() },
        tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
      const original = await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: assistant.id,
        type: "tool", tool: "inspire_screening_task", callID: "failure-delivery-fixture",
        state: { status: "completed", input: { action: "create" }, title: "Original approval",
          output: JSON.stringify({ task_id: data.task.id, digest: data.task.digest }),
          metadata: { preserved: "original" }, time: { start: Date.now(), end: Date.now() } } })
      const source = new LocalProcesses(path.join(data.folder, "jobs"))
      data.store.approve(data.task.id, data.task.digest, session.id, tmp.path)
      const runner = new Runner(data.store, source)
      await runner.tick(data.task.id)
      await runner.tick(data.task.id)
      source.fail = true
      await runner.tick(data.task.id)
      await runner.tick(data.task.id)
      const task = data.store.get(data.task.id)
      const stages = data.store.stages(task.id)
      const directory = path.join(source.root, stages[1].name)
      const names = ["error.json", "started.json", "engine.log"]
      for (const name of names) if (mode !== "missing" || name !== "error.json")
        await Bun.write(path.join(directory, name), JSON.stringify({ fixture: name, task: task.id }))
      await Bun.write(path.join(directory, "unverified-poses.sdf"), "must not be delivered")
      const copies: string[] = []
      const reads: (string | undefined)[] = []
      const transport = { verify: source.verify.bind(source),
        async failure(_task: Task, _stage: Stage, witness?: string) {
          reads.push(witness)
          const artifacts = Object.fromEntries(await Promise.all(names.map(async (name) => {
            const bytes = await Bun.file(path.join(directory, name)).arrayBuffer()
            return [name, { sha256: new Bun.CryptoHasher("sha256").update(bytes).digest("hex"), bytes: bytes.byteLength }]
          })))
          return { version: "screening-failure-evidence-v1", task_id: task.id, plan_digest: task.digest,
            phase: "carsidock", status: "FAILED", started_at: 10, runtime_seconds: 0.2, artifacts,
            scientific_quality_verified: false, contents_included: false }
        },
        async download(_task: Task, remote: string, target: string, limit: number) {
          copies.push(remote)
          const relative = remote.slice(remote.indexOf(`/tasks/${task.id}/`) + `/tasks/${task.id}/`.length).split("/")
          const input = path.join(source.root, `${task.id}-${relative[0]}`, ...relative.slice(1))
          if (Bun.file(input).size > limit) throw new ReviewRequired("Delivery limit exceeded")
          await copyFile(input, target)
          if (mode === "corrupt") await Bun.write(target, "changed copy")
          if (mode === "changed") await Bun.write(path.join(directory, "engine.log"), "changed remote witness")
        } }
      const actual = Server.internalFetch()
      const posts: string[] = []
      const fetcher = async (request: Request) => {
        const response = await actual(request)
        if (request.method === "POST") {
          posts.push(request.url)
          if (mode === "lost_reply" && posts.length === 1) throw new Error("Lost native save reply")
        }
        return response
      }
      try {
        const publisher = new Publications(data.store, "http://openscience.internal", fetcher)
        const progress = new Progress(data.store, "http://openscience.internal", fetcher)
        await deliverCompleted(data.store, new AbortController().signal, () => transport, publisher, progress)
        if (mode === "old_policy") {
          expect(deliveryStatus(data.store, task)).toBeNull()
          expect(reads).toHaveLength(0)
          expect(copies).toHaveLength(0)
          return
        }
        if (["missing", "changed", "corrupt", "file_limit", "byte_limit"].includes(mode)) {
          const status = deliveryStatus(data.store, task)
          expect(status?.state).toBe("PAUSED")
          expect(status?.scope).toBe("failure_evidence")
          if (mode === "missing") expect(status?.total_files).toBeNull()
          expect(await ArtifactStore.list(Instance.project.id)).toHaveLength(0)
          expect(data.store.get(task.id)).toEqual(task)
          const count = reads.length
          await deliverCompleted(data.store, new AbortController().signal, () => transport, publisher, progress)
          expect(reads).toHaveLength(count) // Backoff/pause applies even before inventory is known.
          return
        }
        const reopened = new Store(data.file)
        try {
          const resumed = new DeliveryQueue(reopened, transport, new Publications(reopened, "http://openscience.internal", fetcher))
          if (mode === "lost_reply") {
            expect(deliveryStatus(reopened, task)?.state).toBe("PAUSED")
            resumed.resume(reopened.get(task.id))
          }
          for (let step = 0; step < 6; step++) await resumed.tick(task.id)
          expect(deliveryStatus(reopened, task)).toMatchObject({ state: "COMPLETE", scope: "failure_evidence",
            total_files: 4, completed_files: 4 })
          const receipts = publications(reopened, task)
          expect(receipts.filter((item) => item.evidence_kind === "failure_diagnostic")).toHaveLength(3)
          expect(receipts.filter((item) => item.evidence_kind === "verified_prefix")).toHaveLength(1)
          expect(receipts.some((item) => item.name.endsWith(".sdf"))).toBe(false)
          expect(posts).toHaveLength(4) // Uncertain first save was reconciled, never reposted.
          for (const receipt of receipts) {
            const saved = await ArtifactStore.read(Instance.project.id, receipt.artifact_id, receipt.version_id)
            expect(new Bun.CryptoHasher("sha256").update(await saved!.content.arrayBuffer()).digest("hex")).toBe(receipt.sha256)
          }
          const presentation = new Progress(reopened, "http://openscience.internal", fetcher)
          await presentation.publish(reopened.get(task.id))
          const message = MessageV2.WithParts.parse(await presentation.request(task, `/message/${assistant.id}`))
          const part = message.parts.find((part) => part.id === original.id)
          if (part?.type !== "tool" || part.state.status !== "completed") throw new Error("Original task card missing")
          const card = inspireProgress(part.state.metadata.inspire_progress)
          expect(card?.state).toBe("FAILED")
          expect(card?.delivery?.state).toBe("COMPLETE")
          expect(card?.delivery?.scope).toBe("failure_evidence")
          expect(part.state.metadata.preserved).toBe("original")
          expect(card?.stages.slice(2).every((stage) => stage.state === "WAITING")).toBe(true)
          expect(reopened.get(task.id)).toEqual(task)
          expect(reopened.stages(task.id)).toEqual(stages)
          expect(reopened.events(task.id).filter((row) => (row as { kind: string }).kind === "dispatch_reserved")).toHaveLength(2)
          if (mode === "complete") {
            reopened.db.query("UPDATE deliveries SET inventory='invalid JSON' WHERE task_id=?").run(task.id)
            await expect(resumed.tick(task.id)).rejects.toThrow("inventory changed")
            expect(deliveryStatus(reopened, task)?.state).toBe("PAUSED")
            expect(reopened.get(task.id).state).toBe("FAILED")
          }
        } finally { reopened.close() }
      } finally { data.store.close() }
    } })
  })
}

for (const mode of ["complete", "inventory_over_50", "lost_reply", "file_limit", "byte_limit", "concurrent", "corrupt_transfer"] as const) {
  test(`automatic complete-inventory delivery through native Results with durable recovery: ${mode}`, async () => {
    await using tmp = await fixture({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const policy = { ...defaultDelivery, ...(mode === "file_limit" ? { max_files: 3 } : {}),
        ...(mode === "byte_limit" ? { max_total_bytes: 10 } : {}) }
      const data = await setup({ ...plan(), directory: tmp.path, session: session.id, delivery: policy })
      const source = new LocalProcesses(path.join(data.folder, "jobs"))
      source.extraReports = mode === "inventory_over_50" ? 51 : 0
      const copies: string[] = []
      const waiting = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const transport = { verify: source.verify.bind(source), download: async (_: Task, remote: string, target: string, limit: number) => {
        copies.push(remote)
        if (mode === "concurrent" && copies.length === 1) { waiting.resolve(); await release.promise }
        const parts = remote.slice(remote.indexOf(`/tasks/${data.task.id}/`) + `/tasks/${data.task.id}/`.length).split("/")
        const file = path.join(source.root, `${data.task.id}-${parts[0]}`, ...parts.slice(1))
        if (Bun.file(file).size > limit) throw new ReviewRequired("Approved delivery byte limit exceeded")
        await copyFile(file, target)
        if (mode === "corrupt_transfer" && copies.length === 1) await Bun.write(target, "corrupted copy")
      } }
      const actual = Server.internalFetch()
      const posts: string[] = []
      const fetcher = async (request: Request) => {
        const response = await actual(request)
        if (request.method === "POST") {
          posts.push(request.url)
          if (mode === "lost_reply" && posts.length === 1) throw new Error("uncertain native save response")
        }
        return response
      }
      const queue = new DeliveryQueue(data.store, transport, new Publications(data.store, "http://openscience.internal", fetcher))
      expect(await queue.tick(data.task.id)).toEqual({ worked: false, reason: "not_eligible" })
      expect(copies).toHaveLength(0)
      data.store.approve(data.task.id, data.task.digest, session.id, tmp.path)
      const runner = new Runner(data.store, source)
      for (let step = 0; step < 9; step++) await runner.tick(data.task.id)
      const task = data.store.get(data.task.id)
      expect(task.state).toBe("SUCCEEDED")
      if (mode === "inventory_over_50") {
        const report = await readReport(task, data.store.stages(task.id), source)
        expect(report.artifacts.find((item) => item.phase === "report")?.truncated).toBe(true)
      }
      if (mode === "concurrent") {
        const first = queue.tick(task.id)
        await waiting.promise
        const peer = new Store(data.file)
        try {
          const other = new DeliveryQueue(peer, transport, new Publications(peer, "http://openscience.internal", fetcher))
          expect(await other.tick(task.id)).toEqual({ worked: false, reason: "busy" })
          expect(copies).toHaveLength(1)
        } finally { peer.close(); release.resolve() }
        await first
      } else await queue.tick(task.id)
      if (mode === "file_limit" || mode === "byte_limit") {
        expect(deliveryStatus(data.store, task)?.state).toBe("PAUSED")
        expect(deliveryStatus(data.store, task)?.completed_files).toBe(0)
        expect(await queue.tick(task.id)).toEqual({ worked: false, reason: "paused" })
        expect(await ArtifactStore.list(Instance.project.id)).toHaveLength(0)
        expect(data.store.get(task.id).state).toBe("SUCCEEDED")
        data.store.close()
        return
      }
      if (mode === "lost_reply") {
        expect(deliveryStatus(data.store, task)?.state).toBe("PAUSED")
        expect(await ArtifactStore.list(Instance.project.id)).toHaveLength(1)
        expect(publications(data.store, task)).toHaveLength(0)
        expect(await queue.tick(task.id)).toEqual({ worked: false, reason: "paused" })
        data.store.db.query("UPDATE deliveries SET retry_after=0 WHERE task_id=?").run(task.id)
      }
      if (mode === "corrupt_transfer") {
        expect(deliveryStatus(data.store, task)?.state).toBe("PAUSED")
        expect(await ArtifactStore.list(Instance.project.id)).toHaveLength(0)
        expect(await queue.tick(task.id)).toEqual({ worked: false, reason: "paused" })
        expect(data.store.get(task.id).state).toBe("SUCCEEDED")
        queue.resume(task)
      }
      data.store.close()
      // Restart the controlling object and SQLite connection after each file.
      for (let step = 0; step < 60; step++) {
        const store = new Store(data.file)
        try {
          const resumed = new DeliveryQueue(store, transport, new Publications(store, "http://openscience.internal", fetcher))
          // Use the actual background observer's cycle, not a model/tool call.
          await deliverCompleted(store, new AbortController().signal, () => transport,
            new Publications(store, "http://openscience.internal", fetcher), new Progress(store, "http://openscience.internal", actual))
          if (deliveryStatus(store, task)?.state !== "COMPLETE") continue
          const expected = mode === "inventory_over_50" ? 55 : 4
          expect(deliveryStatus(store, task)).toMatchObject({ state: "COMPLETE", total_files: expected, completed_files: expected, error: null })
          expect(publications(store, task)).toHaveLength(expected)
          expect(copies).toHaveLength(expected + (mode === "corrupt_transfer" ? 1 : 0))
          expect(posts).toHaveLength(expected)
          expect((await ArtifactStore.list(Instance.project.id)).every((item) => item.versionCount === 1)).toBe(true)
          expect(store.events(task.id).filter((event) => (event as { kind: string }).kind === "dispatch_reserved")).toHaveLength(4)
          expect(await resumed.tick(task.id)).toEqual({ worked: false, reason: "complete" })
          expect(store.get(task.id).state).toBe("SUCCEEDED")
          return
        } finally { store.close() }
      }
      throw new Error("Automatic delivery did not finish")
    } })
  }, 30000)
}

test("old plans never gain automatic delivery and frozen storage limits cannot be changed after approval", async () => {
  const data = await setup()
  const source = new LocalProcesses(path.join(data.folder, "jobs"))
  const transport = { verify: source.verify.bind(source), download: async () => { throw new Error("Must not copy") } }
  data.store.approve(data.task.id, data.task.digest, data.task.plan.session, data.task.plan.directory)
  const runner = new Runner(data.store, source)
  for (let step = 0; step < 9; step++) await runner.tick(data.task.id)
  expect(deliveryStatus(data.store, data.store.get(data.task.id))).toBeNull()
  expect(manualDeliveryLimit(data.task, { phase: "report", name: "artifact.txt" }, [])).toBeUndefined()
  expect(await new DeliveryQueue(data.store, transport).tick(data.task.id)).toEqual({ worked: false, reason: "not_eligible" })
  expect(() => new DeliveryQueue(data.store, transport).resume(data.store.get(data.task.id))).toThrow("own approved delivery policy")
  data.store.db.query("UPDATE tasks SET plan=? WHERE id=?").run(JSON.stringify({ ...data.task.plan, delivery: defaultDelivery }), data.task.id)
  expect(() => data.store.get(data.task.id)).toThrow("immutable plan")
  data.store.close()
})

test("model-triggered downloads cannot bypass a frozen automatic delivery queue or race its storage cap", async () => {
  const data = await setup({ ...plan(), delivery: defaultDelivery })
  expect(() => manualDeliveryLimit(data.task, { phase: "report", name: "artifact.txt" }, [])).toThrow("automatic delivery queue")
  const saved = { phase: "report" as const, name: "artifact.txt", artifact_id: "art_abcd", version_id: "ver_abcd",
    project_id: "fixture", sha256: "a".repeat(64), bytes: 50, published_at: Date.now() }
  expect(manualDeliveryLimit(data.task, { phase: saved.phase, name: saved.name }, [saved])).toBe(defaultDelivery.max_file_bytes)
  expect(() => manualDeliveryLimit(data.task, { phase: "prepare", name: "artifact.txt" }, [saved])).toThrow("automatic delivery queue")
  data.store.close()
})

test("suspended docking pauses an approved queued screening stage before any transport call", async () => {
  const store = new Store(":memory:")
  try {
    const task = store.create(plan())
    store.approve(task.id, task.digest, task.plan.session, task.plan.directory)
    store.state(task.id, "ACTIVE")
    store.stage(task.id, 0, { state: "SUCCEEDED" })
    const transport = new Inspire(task.plan.connection)
    await new Runner(store, transport).tick(task.id)
    expect(store.get(task.id).state).toBe("NEEDS_REVIEW")
    expect(store.get(task.id).error).toContain("docking_engine_disabled")
    expect(store.stages(task.id)[1].state).toBe("WAITING")
    expect(store.stages(task.id)[1].submitted).toBeNull()
    await expect(transport.submit(task, store.stages(task.id)[1])).rejects.toThrow("docking_engine_disabled")
  } finally { store.close() }
})
