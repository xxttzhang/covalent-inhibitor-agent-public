import { beforeEach, afterEach, expect, test } from "bun:test"
const legacySharedRootBeforeTest = process.env.COVSCREEN_LEGACY_SHARED_ROOT
beforeEach(() => { process.env.COVSCREEN_LEGACY_SHARED_ROOT = "/inspire/fixture-shared" })
afterEach(() => {
  if (legacySharedRootBeforeTest === undefined) delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  else process.env.COVSCREEN_LEGACY_SHARED_ROOT = legacySharedRootBeforeTest
})
import path from "node:path"
import { createHash } from "node:crypto"
import { cp, mkdir, readFile } from "node:fs/promises"
import { tmpdir } from "../../test/fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { Identifier } from "../../src/id/id"
import { PermissionNext } from "../../src/permission/next"
import { Store } from "../inspire-screening/executor/store"
import { requestHash } from "../inspire-screening/executor/request"
import { ResearchLoopStore } from "./research-loop-store"
import { researchDigest, type ResearchContract } from "./research-loop-contract"
import type { ResearchExecutionEnvironment } from "./research-execution"
import { writeSyntheticResearchProofs } from "./research-execution-test-fixture"
import { researchSnapshot, commitResearchDecision } from "./research-loop"
import { nativeResearchCapability, readResearchNativeAction } from "./research-native-controller"
import { createResearchHost, researchHostSpec, researchHostCatalog, loadResearchHostCatalog, researchHostAction } from "./research-host"
import { researchTool } from "./research-tool"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"
import { prepareResearchPriorUsage } from "./research-prior-usage"

// SYNTHETIC infrastructure tests: actual platform Session/Message stores and
// SQLite/native permission implementation, no provider, chemistry or remote job.
const hash = (value: string) => createHash("sha256").update(value).digest("hex")
async function write(filename: string, value: unknown) {
  const bytes = JSON.stringify(value)
  await Bun.write(filename, bytes)
  return { path: filename, sha256: hash(bytes) }
}
async function setup(directory: string, actualV4 = false) {
  const session = await Session.create({ title: "SYNTHETIC research host test" })
  const user = MessageV2.User.parse({ id: Identifier.ascending("message"), sessionID: session.id, role: "user",
    time: { created: Date.now() }, agent: "research", model: { providerID: "fixture", modelID: "fixture-model" } })
  await Session.updateMessage(user)
  const request = "SYNTHETIC existing research request for the bounded EGFR pilot; test only"
  const part = { id: Identifier.ascending("part"), messageID: user.id, sessionID: session.id, type: "text" as const, text: request }
  await Session.updatePart(part)
  const assistant = MessageV2.Assistant.parse({ id: Identifier.ascending("message"), sessionID: session.id, role: "assistant",
    time: { created: Date.now() }, parentID: user.id, modelID: "fixture-model", providerID: "fixture", agent: "research", mode: "primary",
    path: { cwd: directory, root: directory }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
  await Session.updateMessage(assistant)
  const callID = "synthetic-call-" + crypto.randomUUID()
  const toolPart = { id: Identifier.ascending("part"), sessionID: session.id, messageID: assistant.id, type: "tool" as const,
    callID, tool: "discovery_research", state: { status: "running" as const, input: { action: "open" }, time: { start: Date.now() } } }
  await Session.updatePart(toolPart)
  const fixture = await nativeFixture(directory, session.id)
  if (actualV4) await useActualV4(fixture)
  const evidence = await write(path.join(directory, "policy-evidence.json"), { scope: "SYNTHETIC endpoint and pricing; not a real tariff" })
  const policy = { schema: "research-model-budget-policy-v1", provider_id: "fixture", model_id: "fixture-model", api_model_id: "fixture-model",
    endpoint_origin: "https://example.invalid", endpoint_base_url: "https://example.invalid/v1", response_model_ids: ["fixture-model"],
    input_token_cap: 100000, output_token_cap: 1024, input_cap_basis: "provider-enforced-context-window",
    output_cap_basis: "provider-enforced-including-reasoning", usage_semantics: "inclusive-input-output",
    pricing: { currency: "USD", basis: "conservative-price-ceiling", input_per_million: 10, output_per_million: 20,
      valid_until: new Date(Date.now() + 86400000).toISOString() }, evidence_refs: ["synthetic-policy"] }
  const verification = await write(path.join(directory, "policy-review.json"), { schema: "research-host-policy-verification-v1",
    policy_digest: researchDigest(policy), status: "verified", evidence_refs: policy.evidence_refs,
    reviewed_at: new Date().toISOString(), review_basis: "SYNTHETIC review, no live inference" })
  const { source: _, ...execution } = fixture.environment
  const spec = researchHostSpec.parse({ id: "synthetic-pilot", label: "SYNTHETIC bounded pilot", workspace_project: Instance.project.id,
    contract: fixture.contract, request: { session_id: session.id, user_message_id: user.id, sha256: hash(request) }, execution,
    binding: fixture.binding, policy, policy_verification: verification, policy_evidence: [{ ...evidence, ref: "synthetic-policy" }],
    limits: { max_output_tokens: 100, timeout_ms: 1000 }, valid_until: new Date(Date.now() + 3600000).toISOString() })
  const catalog = researchHostCatalog.parse({ schema: "drug-design-research-host-catalog-v1", specs: [spec] })
  const catalogFile = await write(path.join(directory, "catalog.json"), catalog)
  const research = new ResearchLoopStore(path.join(directory, "research.sqlite")), native = new Store(path.join(directory, "native.sqlite"))
  const counters = { permissions: 0, remote: 0, evaluation: 0 }
  const context = { sessionID: session.id, messageID: assistant.id, agent: assistant.agent, directory, worktree: directory,
    callID, abort: new AbortController().signal, metadata: () => {}, ask: async () => {
      counters.permissions++; throw new PermissionNext.RejectedError()
    } }
  const options = { directory, project: Instance.project.id, research, native, catalog, catalog_digest: catalogFile.sha256,
    authorizeRemote: async () => { counters.remote++ }, authorizeEvaluation: async () => { counters.evaluation++ } }
  const host = createResearchHost(options)
  const open = async () => {
    const result = await host.execute({ action: "open", spec_id: spec.id }, context)
    if (!("loop_id" in result)) throw Error("Expected opened loop")
    return result.loop_id
  }
  return { ...fixture, research, native, user, part, toolPart, spec, catalog, catalogFile, context, options, counters, host, open,
    close() { research.close(); native.close() } }
}
async function run(fn: (data: Awaited<ReturnType<typeof setup>>) => Promise<void>, actualV4 = false) {
  await using temporary = await tmpdir({ git: true })
  await Instance.provide({ directory: temporary.path, fn: async () => {
    const data = await setup(temporary.path, actualV4)
    try { await fn(data) } finally { data.close(); await Instance.dispose() }
  } })
}
async function select(data: Awaited<ReturnType<typeof setup>>, id: string) {
  const offered = data.research.evidence(id, "host").find(row => row.key === "native-plan:round-1")!
  const prepared = (offered.payload as { prepared: { plan: { id: string } } }).prepared
  data.research.record(id, "synthetic-receipt", { schema: "SYNTHETIC-model-boundary-only", model_called: false })
  const snapshot = researchSnapshot(data.research, id)
  return commitResearchDecision({ store: data.research, id, context: snapshot.context, expected_revision: snapshot.loop.revision,
    provenance: { kind: "model_response", receipt_ref: "synthetic-receipt", request_digest: hash("SYNTHETIC request") },
    value: { schema: "drug-design-decision-v1", loop_id: id, state_revision: snapshot.loop.revision, action: "execute_prepared_action",
      capability_id: nativeResearchCapability, plan_id: prepared.plan.id, arguments: {}, evidence_refs: [offered.key],
      rationale: "SYNTHETIC native control path", alternatives: [], expected_observation: "Native permission outcome", outcome: null, missing_inputs: [] } })
}

test("real platform call opens one frozen plan with no model, compute, approval or budget reservation", () => run(async data => {
  const list = await data.host.execute({ action: "list" }, data.context)
  expect(list).toMatchObject({ configured: true, specifications: [{ id: data.spec.id }] })
  const ids = await Promise.all([data.open(), data.open()])
  expect(ids[0]).toBe(ids[1])
  expect(data.research.list(data.directory)).toHaveLength(1)
  expect(data.research.reservations(ids[0]!)).toEqual([])
  expect(data.native.list()).toEqual([])
  expect(data.counters).toEqual({ permissions: 0, remote: 0, evaluation: 0 })
  expect(data.research.evidence(ids[0]!, "host").filter(row => row.key === "native-plan:round-1")).toHaveLength(1)
  expect(await data.host.execute({ action: "read", loop_id: ids[0] }, data.context)).toMatchObject({ state: "READY", native: null,
    scope: { records: 20000, candidates_per_round: 5, max_rounds: 2, second_batch_available: false } })
}))

test("actual v4 CPU is carried once by host open and survives independent SQLite reopen without taking round slots", () => run(async data => {
  const ids = await Promise.all([data.open(), data.open()])
  expect(ids[0]).toBe(ids[1])
  const id = ids[0]!, loop = data.research.get(id)
  expect(loop.ledger.used.cpu_core_hours).toBeCloseTo(0.060905516377777774, 14)
  expect(loop.ledger.unsettled).toEqual([])
  expect(data.research.reservations(id)).toMatchObject([{ state: "SETTLED", task: null,
    payload: { kind: "evaluation", round: 0, candidate_slots: 0 } }])
  const restored = new ResearchLoopStore(path.join(data.directory, "research.sqlite"))
  try {
    const host = createResearchHost({ ...data.options, research: restored })
    expect(await host.execute({ action: "open", spec_id: data.spec.id }, data.context)).toMatchObject({ resumed: true, loop_id: id })
    expect(restored.get(id)).toEqual(loop)
    const usage = await prepareResearchPriorUsage(data.spec.contract)
    usage!.receipt.resources.cpu_core_hours = 0 // detached view is not an accounting input
    usage!.record(restored, id)
    expect(restored.get(id)).toEqual(loop)
  } finally { restored.close() }
  await select(data, id)
  expect(data.research.reservations(id).filter(row => row.payload.kind === "compute")).toMatchObject([{ payload: { round: 1, candidate_slots: 5 } }])
  expect(data.research.get(id).ledger.used.cpu_core_hours).toBe(loop.ledger.used.cpu_core_hours)
  expect(data.native.list()).toEqual([])
  expect(data.counters).toEqual({ permissions: 0, remote: 0, evaluation: 0 })
}, true))

test("host rejects a contract that funds the first native plan but omits its prior CPU", () => run(async data => {
  // 4 CPU * 20 minutes + 10 CPU * 15 minutes = 3.8333, before prior preparation.
  const catalog = structuredClone(data.catalog)
  catalog.specs[0]!.contract.limits.resources.cpu_core_hours = 3.85
  const host = createResearchHost({ ...data.options, catalog, catalog_digest: researchDigest(catalog) })
  await expect(host.execute({ action: "open", spec_id: data.spec.id }, data.context)).rejects.toThrow("prior preparation")
  expect(data.research.list(data.directory)).toEqual([])
  expect(data.research.db.query("SELECT * FROM research_host_bindings").all()).toEqual([])
  expect(data.native.list()).toEqual([])
}, true))

test("reopening or resuming cannot silently recreate a missing prior-usage settlement", () => run(async data => {
  const id = await data.open()
  data.research.db.query("DELETE FROM research_reservations WHERE loop=?").run(id)
  await expect(data.open()).rejects.toThrow("not been carried")
  expect((await data.host.resume(data.context.abort))[0]).toMatchObject({ step: "needs_attention" })
  expect(data.research.reservations(id)).toEqual([])
  expect(data.native.list()).toEqual([])
  expect(data.counters).toEqual({ permissions: 0, remote: 0, evaluation: 0 })
}, true))

test("failed prior CPU reservation rolls back loop creation and rejects an uncharged native offer", () => run(async data => {
  const contract = structuredClone(data.spec.contract)
  contract.limits.resources.cpu_core_hours = 0.01
  const usage = await prepareResearchPriorUsage(contract)
  expect(() => data.research.db.transaction(() => {
    const loop = data.research.create(contract)
    usage!.record(data.research, loop.id)
  }).immediate()).toThrow("Cumulative research budget")
  expect(data.research.list(data.directory)).toEqual([])
  const id = await data.open()
  const saved = data.research.evidence(id, "host").find(row => row.key === "native-plan:round-1")!
  const prepared = (saved.payload as { prepared: import("./research-execution").PreparedResearchExecution }).prepared
  const other = data.research.create(data.spec.contract)
  const { registerResearchNativePlan } = await import("./research-native-controller")
  await expect(registerResearchNativePlan({ research: data.research, id: other.id, key: "round-1", round: 1, prepared }))
    .rejects.toThrow("not been carried")
  expect(data.research.evidence(other.id)).toEqual([])
}, true))

// Real frozen evidence, copied byte-for-byte into an isolated test project.
// Platform identity, runtime and reference ligand remain SYNTHETIC; no job runs.
async function useActualV4(data: Awaited<ReturnType<typeof nativeFixture>>) {
  const root = process.env.COVSCREEN_ACCEPTANCE_ROOT ?? path.resolve(import.meta.dir, "../../../../..")
  const manifest = JSON.parse(await readFile(path.join(root, preparedReceptorV4.manifest.path), "utf8"))
  const proof = JSON.parse(await readFile(path.join(root, preparedReceptorV4.binding.path), "utf8"))
  const receipt = JSON.parse(await readFile(path.join(root, proof.references.preparation_receipt.path), "utf8"))
  const subset = JSON.parse(await readFile(path.join(root, manifest.scope.dataset_manifest_path), "utf8"))
  for (const version of ["v2", "v3", "v4"]) {
    const relative = `reports/research-loop-20261013/receptor-preparation-${version}`
    await cp(path.join(root, relative), path.join(data.directory, relative), { recursive: true })
  }
  const files = [preparedReceptorV4.manifest.path, manifest.scope.dataset_manifest_path,
    ...Object.values(proof.references as Record<string, { path: string }>).map(ref => ref.path),
    ...Object.values(receipt.initial_inputs as Record<string, { path: string }>).map(ref => ref.path)]
  for (const relative of new Set(files)) {
    const target = path.join(data.directory, relative)
    await mkdir(path.dirname(target), { recursive: true })
    await cp(path.join(root, relative), target)
  }
  data.contract.allowed_adaptations = ["inspect", "evaluate", "stop"]
  data.contract.target.receptor_sha256 = manifest.target.receptor_sha256
  data.contract.target.mapping_sha256 = preparedReceptorV4.binding.sha256
  data.contract.inputs.library_sha256 = subset.source.sha256
  data.contract.inputs.subset_sha256 = manifest.scope.dataset_manifest_sha256
  data.contract.inputs.science_config_sha256 = manifest.recipe.sha256
  data.contract.quality = { manifest_sha256: preparedReceptorV4.manifest.sha256, required_checks: manifest.required_scientific_checks,
    minimum_candidates: manifest.scope.minimum_accepted_candidates, conclusion: "computational-shortlist-only" }
  const binding = data.binding as import("./research-execution").ResearchExecutionBinding
  Object.assign(binding, { library: "/inspire/SYNTHETIC/prior-budget/prefix.csv", receptor: "/inspire/SYNTHETIC/prior-budget/receptor.pdb",
    reference: "/inspire/SYNTHETIC/prior-budget/reference.sdf", subset_remote_manifest: "/inspire/SYNTHETIC/prior-budget/manifest.json",
    mapping: path.join(data.directory, preparedReceptorV4.binding.path), quality_manifest: path.join(data.directory, preparedReceptorV4.manifest.path),
    subset_manifest: path.join(data.directory, manifest.scope.dataset_manifest_path) })
  data.environment.assets = { ...data.environment.assets, [binding.library]: subset.subset.sha256,
    [binding.receptor]: manifest.target.receptor_sha256, [binding.reference!]: hash("SYNTHETIC reference; never submitted"),
    [binding.science_config]: manifest.recipe.sha256, [binding.subset_remote_manifest!]: manifest.scope.dataset_manifest_sha256 }
  data.environment.research_inputs = { version: "screening-research-inputs-v1",
    library: { path: binding.library, sha256: subset.subset.sha256, records: 20000, selection: "frozen-file-order-prefix",
      source: { path: subset.source.path, sha256: subset.source.sha256 },
      manifest: { path: binding.subset_remote_manifest!, sha256: manifest.scope.dataset_manifest_sha256 } },
    receptor: { path: binding.receptor, sha256: manifest.target.receptor_sha256 },
    science_config: { path: binding.science_config, sha256: manifest.recipe.sha256 },
    reference: { path: binding.reference!, sha256: data.environment.assets[binding.reference!]! } }
}

test("installation catalog hash is mandatory and tool arguments cannot self-certify paths or policy", () => run(async data => {
  expect(await loadResearchHostCatalog(data.catalogFile.path, undefined)).toBeNull()
  expect(await loadResearchHostCatalog(data.catalogFile.path, data.catalogFile.sha256)).toEqual(data.catalog)
  await expect(loadResearchHostCatalog(data.catalogFile.path, "0".repeat(64))).rejects.toThrow("hash")
  expect(() => researchHostAction.parse({ action: "open", spec_id: data.spec.id, policy_verified: true })).toThrow()
  expect(() => researchHostAction.parse({ action: "tick", loop_id: "x", command: "arbitrary" })).toThrow()
  const tool = researchTool(async () => data.host, async () => {})
  expect(Object.keys(tool.args).sort()).toEqual(["action", "loop_id", "spec_id"])
  expect(JSON.parse(await tool.execute({ action: "open", spec_id: data.spec.id }, data.context)).step).toBe("opened")
}))

test("missing call identity, synthetic parent and mismatched original model cannot authorize opening", () => run(async data => {
  await expect(data.host.execute({ action: "open", spec_id: data.spec.id }, { ...data.context, callID: undefined })).rejects.toThrow("actual platform")
  await Session.updatePart({ ...data.part, synthetic: true })
  await expect(data.open()).rejects.toThrow("nonsynthetic")
  await Session.updatePart(data.part)
  await Session.updateMessage({ ...data.user, model: { providerID: "other", modelID: "other" } })
  await expect(data.open()).rejects.toThrow("model differs")
  expect(data.research.list(data.directory)).toEqual([])
}))

test("restore reuses original task/slot; background cannot ask or submit and real refusal preserves unknown usage", () => run(async data => {
  const id = await data.open()
  await select(data, id)
  const restore = createResearchHost(data.options)
  expect((await restore.resume(data.context.abort))[0]).toMatchObject({ step: "needs_attention", native: { status: "not_prepared" } })
  expect(data.counters.permissions).toBe(0)
  expect(data.native.list()).toHaveLength(0)
  const approval = await data.host.execute({ action: "tick", loop_id: id }, data.context)
  expect(approval).toMatchObject({ step: "native_permission" })
  expect(data.counters.permissions).toBe(1)
  expect(readResearchNativeAction({ research: data.research, native: data.native, id })).toMatchObject({ status: "CANCELLED", cancellation: "approval_declined" })
  expect((await restore.resume(data.context.abort))[0]).toMatchObject({ step: "observed", observations: [{ classification: "execution_failure" }] })
  expect(data.research.get(id).ledger.unsettled).toEqual(["round-1"])
  // Deliberately insert an interrupted durable slot: recovery must not resolve
  // a provider or reissue a charge even with no attempt receipt.
  data.research.db.query("INSERT INTO research_host_model_slots VALUES(?,?,?,?,?)").run(id, 1, "host-model-1", data.context.messageID, data.context.callID)
  expect((await restore.resume(data.context.abort))[0]).toMatchObject({ step: "needs_reconciliation" })
  expect(data.native.list()).toHaveLength(1)
  expect(data.research.reservations(id)).toHaveLength(1)
  expect(data.counters.permissions).toBe(1)
}))

test("changed original request, frozen prepared plan or policy evidence fail closed", () => run(async data => {
  const id = await data.open()
  await Session.updatePart({ ...data.part, text: "changed request" })
  await expect(data.host.execute({ action: "read", loop_id: id }, data.context)).rejects.toThrow("Original")
  await Session.updatePart(data.part)
  await Bun.write(data.spec.policy_evidence[0]!.path, "{}")
  await expect(data.host.execute({ action: "tick", loop_id: id }, data.context)).rejects.toThrow("hash")
  expect(data.research.db.query("SELECT * FROM research_host_model_slots").all()).toEqual([])
  data.research.db.query("UPDATE research_host_bindings SET prepared_digest=? WHERE loop=?").run("0".repeat(64), id)
  expect((await createResearchHost(data.options).resume(data.context.abort))[0]).toMatchObject({ step: "needs_attention" })
  expect(data.research.reservations(id)).toEqual([])
}))

test("catalog absence or another project cannot automatically resume existing research", () => run(async data => {
  const id = await data.open()
  const disabled = createResearchHost({ ...data.options, catalog: null, catalog_digest: null })
  expect(await disabled.resume(data.context.abort)).toEqual([])
  expect(await disabled.execute({ action: "tick", loop_id: id }, data.context)).toMatchObject({ step: "needs_attention" })
  expect(await createResearchHost({ ...data.options, project: "another-project" }).resume(data.context.abort)).toEqual([])
  expect(data.research.reservations(id)).toEqual([])
}))

test("terminal research takes precedence over an unresolved model slot without clearing uncertainty", () => run(async data => {
  const id = await data.open()
  data.research.db.query("INSERT INTO research_host_model_slots VALUES(?,?,?,?,?)").run(id, 1, "host-model-1", data.context.messageID, data.context.callID)
  data.research.finish(id, data.research.get(id).revision, "insufficient_evidence", [])
  const restored = createResearchHost(data.options)
  expect((await restored.resume(data.context.abort))[0]).toMatchObject({ step: "stopped", state: "FINISHED",
    outcome: "insufficient_evidence", unresolved_model_slots: ["host-model-1"] })
  expect(await restored.execute({ action: "recover", loop_id: id }, data.context)).toMatchObject({ step: "stopped" })
  expect(data.research.reservations(id)).toEqual([])
  expect(data.counters).toEqual({ permissions: 0, remote: 0, evaluation: 0 })
}))

test("appended real user text and a completed or foreign tool invocation cannot reuse request authorization", () => run(async data => {
  const id = await data.open()
  await Session.updatePart({ ...data.part, id: Identifier.ascending("part"), text: "SYNTHETIC changed scope" })
  expect((await data.host.resume(data.context.abort))[0]).toMatchObject({ step: "needs_attention", reason: "Original research user message changed" })
  await Session.updatePart({ ...data.toolPart, tool: "another-tool" })
  await expect(data.host.execute({ action: "read", loop_id: id }, data.context)).rejects.toThrow("pending tool")
  expect(data.research.reservations(id)).toEqual([])
}))

async function nativeFixture(directory: string, session: string) {
  const shared = "/inspire/fixture-shared"
  const binding = { library: `${shared}/1_zhang/CovInDB/Virtual_Screening_Library.csv`,
    receptor: `${shared}/2_agent/science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb`,
    science_config: `/inspire/fixture/releases/${"3".repeat(64)}/science/configs/covindb/EGFR_C797_mvp_v1.yaml`, mapping: path.join(directory, "mapping.json"),
    subset_manifest: path.join(directory, "subset.json"), quality_manifest: path.join(directory, "quality.json") }
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "fixture-project", session,
    objective: "Test native plan binding, not chemistry", mode: "native-compute", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A",
      author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: "2".repeat(64), mapping_sha256: hash("synthetic-test-proof") },
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", library_sha256: "1".repeat(64), subset_sha256: hash("synthetic-test-proof"),
      source_release: "3".repeat(64), science_config_sha256: "4".repeat(64) },
    quality: { manifest_sha256: hash("synthetic-test-proof"), required_checks: ["synthetic_gate"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { gpu_hours: 0.5, cpu_core_hours: 8, compute_points: 2, model_tokens: 300000, model_cost: 10 },
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
