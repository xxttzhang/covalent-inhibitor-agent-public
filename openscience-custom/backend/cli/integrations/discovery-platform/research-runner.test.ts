import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import path from "node:path"
import { mkdir, readFile } from "node:fs/promises"
import { tmpdir } from "../../test/fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { emptyResources, researchDigest, type ResearchContract } from "./research-loop-contract"
import { captureResearchArtifact, createResearchObservation } from "./research-observations"
import { prepareResearchRunner, readResearchRunner, recoverResearchRunner, invokeResearchRunner, researchRunnerMain,
  type ResearchRunnerSpec } from "./research-runner"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"

// TEST ONLY: temporary synthetic evidence and endpoint policy. These tests
// never call an inference provider and do not claim scientific acceptance.
const hash = (value: string) => createHash("sha256").update(value).digest("hex")
async function write(filename: string, value: unknown) {
  const content = JSON.stringify(value)
  await Bun.write(filename, content)
  return { path: filename, sha256: hash(content) }
}

async function fixture(directory: string) {
  const receptor = hash("SYNTHETIC receptor identity; no PDB supplied")
  const contract: ResearchContract = { schema: "drug-design-research-contract-v1", directory, project: "synthetic-offline-test",
    session: "synthetic-historical-session", objective: "SYNTHETIC host preparation test; no scientific evidence or inference",
    mode: "offline-replay", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A", author_residue: 773,
      reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: receptor, mapping_sha256: null },
    inputs: { library: "SYNTHETIC library", library_sha256: hash("synthetic library"), subset_sha256: null,
      science_config_sha256: hash("synthetic config"), source_release: hash("synthetic source") },
    quality: { manifest_sha256: null, required_checks: ["identity"], minimum_candidates: 1, conclusion: "computational-shortlist-only" },
    limits: { resources: { ...emptyResources(), model_tokens: 300000, model_cost: 10 }, max_library_records: 20000, max_rounds: 2,
      max_candidates_per_round: 5, max_total_candidates: 10, max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25,
      max_decisions: 2, max_elapsed_ms: 60000 }, allowed_adaptations: ["inspect", "stop"] }
  const artifact = await write(path.join(directory, "historical-artifact.json"), { scope: "SYNTHETIC historical artifact fixture" })
  const observation = createResearchObservation({ origin: "historical_import", run: { id: "SYNTHETIC-history", task_id: null,
    route: "library-first", release_id: contract.inputs.source_release, input_identity_digest: hash("synthetic input"),
    started_at: null, status: "succeeded", phase: "SYNTHETIC history" },
    counts: { input_records: 20000, selected: 0, attempted: 0, execution_succeeded: 0, scientific_passed: null },
    candidates: [], artifacts: [await captureResearchArtifact({ path: artifact.path, ref: "synthetic-artifact", expectedSha256: artifact.sha256 })],
    identity: { status: "unknown", evidence_refs: [], detail: "No actual scientific evaluation" },
    execution_checks: [], scientific_checks: [], required_scientific_checks: ["identity"], failure: null, integrity_errors: [],
    limitations: ["SYNTHETIC test only; not a historical scientific result"] })
  const historical = await write(path.join(directory, "history.json"), { schema: "drug-design-historical-replay-import-v1", contract, observation })
  const audit = await write(path.join(directory, "receptor-audit.json"), { schema: "drug-design-receptor-audit-v1", evaluator: "SYNTHETIC-evaluator",
    evaluator_sha256: hash("SYNTHETIC evaluator"), structure_sha256: receptor, entry_id: "1M17", uniprot: "P00533", uniprot_residue: 797,
    auth_asym_id: "A", author_residue_id: "773", reactive_atom: "SG", taxon: 9606, mapping_status: "pass", status: "fail",
    checks: [{ id: "synthetic_coordinate_check", status: "fail", detail: "SYNTHETIC failure; not a molecular audit" }], new_compute: false })
  const source = await write(path.join(directory, "policy-evidence.json"), { scope: "SYNTHETIC endpoint, limits and tariff; not a real price guarantee" })
  const policy: ResearchRunnerSpec["policy"] = { schema: "research-model-budget-policy-v1", provider_id: "fixture", model_id: "fixture-model",
    api_model_id: "fixture-model", endpoint_origin: "https://example.invalid", endpoint_base_url: "https://example.invalid/v1",
    response_model_ids: ["fixture-model"], input_token_cap: 100000, output_token_cap: 1024,
    input_cap_basis: "provider-enforced-context-window", output_cap_basis: "provider-enforced-including-reasoning",
    usage_semantics: "inclusive-input-output", pricing: { currency: "USD", basis: "conservative-price-ceiling",
      input_per_million: 10, output_per_million: 20, valid_until: new Date(Date.now() + 86400000).toISOString() },
    evidence_refs: ["synthetic-policy-source"] }
  const verification = await write(path.join(directory, "policy-verification.json"), { schema: "research-host-policy-verification-v1",
    policy_digest: researchDigest(policy), status: "verified", evidence_refs: policy.evidence_refs,
    reviewed_at: new Date().toISOString(), review_basis: "SYNTHETIC test verification; no actual model or account audited" })
  const spec: ResearchRunnerSpec = { schema: "research-host-runner-spec-v1", directory, contract,
    model: { provider_id: "fixture", model_id: "fixture-model", agent: "research" }, policy, policy_verification: verification,
    policy_evidence: [{ ...source, ref: "synthetic-policy-source" }], historical, receptor_audit: audit,
    limits: { max_output_tokens: 100, timeout_ms: 1000 } }
  return { spec, run: path.join(directory, "runner"), observation, artifact }
}

test("prepare creates a real local session and frozen evidence, preserving audit failure without model work", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const prepared = await prepareResearchRunner(item)
  expect(prepared).toMatchObject({ resumed: false, model_called: false, new_remote_compute: false })
  expect(prepared.manifest.session_id).toMatch(/^ses_/)
  expect(prepared.manifest.session_id).not.toBe(item.spec.contract.session)
  const evidence = await readResearchRunner(item)
  expect(evidence.loop.contract).toEqual({ ...item.spec.contract, session: prepared.manifest.session_id })
  expect(evidence.observations).toEqual([item.observation])
  expect(evidence.host_evidence.find(row => row.key === "runner-receptor-audit")?.payload)
    .toMatchObject({ audit: { status: "fail", mapping_status: "pass" }, status_preserved: true, new_compute: false })
  expect(evidence.model_calls).toEqual([])
  expect(evidence.decisions).toEqual([])
  expect(evidence.loop.ledger.total).toEqual(emptyResources())
  await Instance.provide({ directory: temporary.path, fn: async () => {
    try {
      const session = await Session.get(prepared.manifest.session_id)
      expect(session.directory).toBe(temporary.path)
      expect(session.permission).toEqual([{ permission: "*", pattern: "*", action: "deny" }])
      const message = await MessageV2.get({ sessionID: session.id, messageID: prepared.manifest.user_message_id })
      expect(message.info).toMatchObject({ role: "user", agent: "research", model: { providerID: "fixture", modelID: "fixture-model" } })
      expect(message.parts[0]).toMatchObject({ type: "text", synthetic: true })
      expect(JSON.stringify(message.parts)).toContain("Host-initiated actual research invocation")
    } finally { await Instance.dispose() }
  } })
})

test("prepare replay keeps one loop/session and recovery creates neither a provider receipt nor another request", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const original = await prepareResearchRunner(item)
  expect(await prepareResearchRunner(item)).toEqual({ ...original, resumed: true })
  const before = await readResearchRunner(item)
  const recovered = await recoverResearchRunner(item)
  expect(recovered.recovered).toMatchObject({ status: "no_attempt", invoked: false })
  expect(recovered.evidence).toEqual(before)
  expect(recovered.evidence.model_calls).toEqual([])
  expect(await Bun.file(path.join(item.run, "latest-decision-receipt.json")).exists()).toBe(false)
})

test.each(["compute", "adaptation", "tokens", "cost", "policy_verification", "evidence_hash", "historical_artifact", "audit_binding"] as const)(
  "prepare rejects %s before creating any session/run", async kind => {
    await using temporary = await tmpdir({ git: true })
    const item = await fixture(temporary.path)
    if (kind === "compute") item.spec.contract.limits.resources.compute_points = 1
    if (kind === "adaptation") item.spec.contract.allowed_adaptations = ["evaluate"]
    if (kind === "tokens") item.spec.contract.limits.resources.model_tokens = 100
    if (kind === "cost") item.spec.contract.limits.resources.model_cost = 0.1
    if (kind === "policy_verification") item.spec.policy_verification = await write(item.spec.policy_verification.path,
      { schema: "research-public-gateway-pricing-evidence-v1", account_pricing_verified: false })
    if (kind === "evidence_hash") item.spec.policy_evidence[0]!.sha256 = "0".repeat(64)
    if (kind === "historical_artifact") await Bun.write(item.artifact.path, "modified bytes")
    if (kind === "audit_binding") item.spec.contract.target.receptor_sha256 = "0".repeat(64)
    await expect(prepareResearchRunner(item)).rejects.toThrow()
    expect(await Bun.file(path.join(item.run, "runner.json")).exists()).toBe(false)
    expect(await Bun.file(path.join(item.run, "session-created.json")).exists()).toBe(false)
  })

test("interrupted preparation cannot silently create another session", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  await mkdir(item.run)
  await Bun.write(path.join(item.run, "prepare-claim.json"), "SYNTHETIC crashed preparation")
  await expect(prepareResearchRunner(item)).rejects.toThrow()
  expect(await Bun.file(path.join(item.run, "session-created.json")).exists()).toBe(false)
})

test("changed session request cannot be used as a recovered model context", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const prepared = await prepareResearchRunner(item)
  await Instance.provide({ directory: temporary.path, fn: async () => {
    try {
      const message = await MessageV2.get({ sessionID: prepared.manifest.session_id, messageID: prepared.manifest.user_message_id })
      const original = MessageV2.User.parse(message.info)
      await Session.updateMessage({ ...original, model: { providerID: "another-provider", modelID: "another-model" } })
    } finally { await Instance.dispose() }
  } })
  await expect(recoverResearchRunner(item)).rejects.toThrow("saved context")
  expect((await readResearchRunner(item)).model_calls).toEqual([])
})

test("editing both public manifest and spec cannot change the persisted host policy binding", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const prepared = await prepareResearchRunner(item)
  const changed = { ...item.spec, limits: { ...item.spec.limits, max_output_tokens: 1 } }
  await write(path.join(item.run, "spec.json"), changed)
  await write(path.join(item.run, "runner.json"), { ...prepared.manifest, spec_digest: researchDigest(changed) })
  await expect(readResearchRunner(item)).rejects.toThrow("immutable host origin")
})

test("CLI requires an explicit command, and invocation requires the existing authorization record", async () => {
  await expect(researchRunnerMain([])).rejects.toThrow("no model call is the default")
  await expect(researchRunnerMain(["invoke", "--run", "/unused"])).rejects.toThrow("--authorization")
  await expect(researchRunnerMain(["prepare", "--run", "/unused", "--authorization", "/unused"])).rejects.toThrow("invocation flags")
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const spec = await write(path.join(temporary.path, "runner-spec.json"), item.spec)
  await researchRunnerMain(["prepare", "--run", item.run, "--spec", spec.path])
  const recovered = await researchRunnerMain(["recover", "--run", item.run])
  expect(recovered).toMatchObject({ recovered: { status: "no_attempt", invoked: false } })
})

test("wrong goal scope and insufficient authorization never resolve a provider or reserve usage", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await fixture(temporary.path)
  const prepared = await prepareResearchRunner(item)
  const approval = { schema: "research-host-existing-goal-authorization-v1", origin: "existing-user-goal",
    scope: "offline-replay-model-decisions", loop_id: prepared.manifest.loop_id, contract_digest: prepared.manifest.contract_digest,
    policy_digest: researchDigest(item.spec.policy), source_ref: "SYNTHETIC testing authorization", statement: "SYNTHETIC; never invoke provider",
    authorized_at: new Date().toISOString(), max_calls: 1, max_tokens: 1, max_cost_usd: 1 }
  const filename = path.join(temporary.path, "authorization.json")
  await write(filename, { ...approval, scope: "native-compute" })
  await expect(invokeResearchRunner({ ...item, authorization: filename, abort: new AbortController().signal })).rejects.toThrow()
  await write(filename, approval)
  await expect(invokeResearchRunner({ ...item, authorization: filename, abort: new AbortController().signal })).rejects.toThrow("authorized cumulative budget")
  expect((await readResearchRunner(item)).model_calls).toEqual([])
  expect(JSON.parse(await readFile(path.join(item.run, "runner.json"), "utf8"))).toEqual(prepared.manifest)
})

// Real failed preparation/proof; only history scaffolding and never-contacted
// provider pricing remain explicitly synthetic. No scientific execution mock.
async function scientificFixture(directory: string) {
  const item = await fixture(directory)
  const root = path.resolve(import.meta.dir, "../../../../..")
  const manifestPath = "openscience-custom/backend/cli/integrations/discovery-platform/research-scientific-manifest-v2.json"
  const bindingPath = "reports/research-loop-20261013/receptor-preparation-v2/receptor-binding-v2.json"
  const diagnosticPath = "reports/research-loop-20261013/receptor-preparation-v2/failure-diagnostic-v2.1.json"
  async function copy(relative: string) {
    const bytes = await readFile(path.join(root, relative))
    const target = path.join(directory, relative)
    await mkdir(path.dirname(target), { recursive: true })
    await Bun.write(target, bytes)
    return { path: target, sha256: createHash("sha256").update(bytes).digest("hex") }
  }
  const binding = JSON.parse(await readFile(path.join(root, bindingPath), "utf8")) as {
    references: Record<string, { path: string; sha256: string }>
  }
  for (const ref of Object.values(binding.references)) await copy(ref.path)
  const receiptRef = binding.references.preparation_receipt!
  const receipt = JSON.parse(await readFile(path.join(root, receiptRef.path), "utf8")) as { artifacts: Record<string, string> }
  for (const name of Object.keys(receipt.artifacts)) await copy(path.join(path.dirname(receiptRef.path), name))
  const auditPolicy = JSON.parse(await readFile(path.join(root, binding.references.independent_audit_policy!.path), "utf8")) as { initial_audit: { path: string } }
  await copy(auditPolicy.initial_audit.path)
  item.spec.scientific_evidence = { manifest: await copy(manifestPath), binding: await copy(bindingPath), diagnostic: await copy(diagnosticPath) }
  item.spec.contract.target.receptor_sha256 = binding.references.source_receptor!.sha256
  item.spec.contract.target.mapping_sha256 = binding.references.source_mapping_audit!.sha256
  item.spec.receptor_audit = { path: path.join(directory, binding.references.source_mapping_audit!.path), sha256: binding.references.source_mapping_audit!.sha256 }
  const historical = JSON.parse(await readFile(item.spec.historical.path, "utf8")) as { contract: ResearchContract }
  historical.contract = item.spec.contract
  item.spec.historical = await write(item.spec.historical.path, historical)
  return { ...item, binding }
}

test("actual failed v2.1 proof enters model snapshot while preserving history and contract", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await scientificFixture(temporary.path)
  const original = structuredClone(item.spec.contract)
  const prepared = await prepareResearchRunner(item)
  const evidence = await readResearchRunner(item)
  expect(evidence.loop.contract).toEqual({ ...original, session: prepared.manifest.session_id })
  expect(evidence.observations).toEqual([item.observation])
  expect(evidence.host_evidence.find(row => row.key === "runner-receptor-audit")?.payload).toMatchObject({ audit: { status: "fail", mapping_status: "pass" } })
  const science = evidence.host_evidence.find(row => row.key === "runner-prepared-receptor-v2.1")?.payload
  expect(science).toMatchObject({ status: "fail", scientific_passed: false, native_admissible: false, audit_input_binding_complete: false, origin: "local_cpu_preparation_and_independent_audit",
    modeled_coordinates_are_experimental: false, native_execution: false, new_remote_compute: false,
    limitations: [expect.stringContaining("Auditor 2.1 did not bind")],
    source_sha256: original.target.receptor_sha256, prepared_sha256: "e98fbae80cb196ba885a4c202ae7f2107a668245f6638c4c846c02361cfd8a6a" })
  const diagnostic = evidence.host_evidence.find(row => row.key === "runner-prepared-receptor-failures-v2.1")?.payload
  expect(diagnostic).toMatchObject({ diagnostic_only: true, classes: [
    { id: "standard_amino_acid_stereochemistry", violation_count: 16, modeled_centers: 13, retained_centers: 3, entirely_retained_local_environments: 0 },
    { id: "intramolecular_distance_angles", violation_count: 19, modeled_pair_violations: 19, retained_observed_pair_violations: 0 },
    { id: "intramolecular_distance_internal", violation_count: 18, modeled_pair_violations: 18, retained_observed_pair_violations: 0 },
  ] })
  const { ResearchLoopStore } = await import("./research-loop-store")
  const { researchSnapshot } = await import("./research-loop")
  const store = new ResearchLoopStore(path.join(item.run, "research.sqlite"))
  try {
    const snapshot = researchSnapshot(store, prepared.manifest.loop_id)
    expect(snapshot.context.evidence_refs).toContain("runner-prepared-receptor-v2.1")
    expect(snapshot.context.evidence_refs).toContain("runner-prepared-receptor-failures-v2.1")
    expect(snapshot.context.prepared_plans).toEqual([])
    const sent = JSON.parse(snapshot.prompt.user)
    expect(sent.research_state.host_evidence.find((row: { ref: string }) => row.ref === "runner-prepared-receptor-v2.1").content).toEqual(science)
    expect(sent.research_state.host_evidence.find((row: { ref: string }) => row.ref === "runner-prepared-receptor-failures-v2.1").content).toEqual(diagnostic)
  } finally { store.close() }
  expect(evidence.model_calls).toEqual([])
  expect(evidence.loop.ledger.total).toEqual(emptyResources())
  expect((await prepareResearchRunner(item)).resumed).toBe(true)
})

test("actual passing v4 receptor enters replay as prospective evidence, retaining old failures and zero model calls", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await scientificFixture(temporary.path)
  const root = path.resolve(import.meta.dir, "../../../../..")
  const copy = async (relative: string) => {
    const bytes = await readFile(path.join(root, relative)), filename = path.join(temporary.path, relative)
    await mkdir(path.dirname(filename), { recursive: true }); await Bun.write(filename, bytes)
    return { path: filename, sha256: createHash("sha256").update(bytes).digest("hex") }
  }
  const proof = JSON.parse(await readFile(path.join(root, preparedReceptorV4.binding.path), "utf8")) as {
    references: Record<string, { path: string; sha256: string }> }
  for (const ref of Object.values(proof.references)) await copy(ref.path)
  const receipt = JSON.parse(await readFile(path.join(root, proof.references.preparation_receipt!.path), "utf8")) as {
    artifacts: Record<string, string>; initial_inputs: Record<string, { path: string; sha256: string }>; initial_artifacts: Record<string, string>; parent_v3_artifacts: Record<string, string> }
  for (const name of Object.keys(receipt.artifacts)) await copy(path.join(path.dirname(proof.references.preparation_receipt!.path), name))
  for (const ref of Object.values(receipt.initial_inputs)) await copy(ref.path)
  for (const [role, artifacts] of [["receipt", receipt.initial_artifacts], ["parent_v3_receipt", receipt.parent_v3_artifacts]] as const)
    for (const name of Object.keys(artifacts)) await copy(path.join(path.dirname(receipt.initial_inputs[role]!.path), name))
  item.spec.prepared_receptor_evidence = { manifest: await copy(preparedReceptorV4.manifest.path), binding: await copy(preparedReceptorV4.binding.path) }
  const contract = structuredClone(item.spec.contract)
  const created = await prepareResearchRunner(item)
  const evidence = await readResearchRunner(item)
  expect(evidence.loop.contract).toEqual({ ...contract, session: created.manifest.session_id })
  expect(evidence.observations).toEqual([item.observation])
  expect(evidence.host_evidence.find(row => row.key === "runner-prepared-receptor-v2.1")?.payload).toMatchObject({ status: "fail" })
  const current = evidence.host_evidence.find(row => row.key === "runner-prepared-receptor-v4.0")?.payload
  expect(current).toMatchObject({ status: "pass", scientific_passed: true, audit_input_binding_complete: true,
    prospective_scientific_input_admissible: true, native_execution_authorized: false,
    historical_candidates_re_evaluated: false, immutable_historical_contract_preserved: true })
  expect(evidence.model_calls).toEqual([])
  expect(evidence.loop.ledger.total).toEqual(emptyResources())
  expect((await prepareResearchRunner(item)).resumed).toBe(true)
  expect((await recoverResearchRunner(item)).recovered).toMatchObject({ status: "no_attempt", invoked: false })
  await Bun.write(path.join(temporary.path, proof.references.independent_audit!.path), "{}")
  await expect(prepareResearchRunner(item)).rejects.toThrow("V4 receptor evidence bytes changed")
})

test.each(["binding_hash", "diagnostic_counts", "diagnostic_audit", "source_target", "receipt_artifact"] as const)(
  "actual scientific context rejects %s before creating a session", async kind => {
    await using temporary = await tmpdir({ git: true })
    const item = await scientificFixture(temporary.path)
    const science = item.spec.scientific_evidence!
    if (kind === "binding_hash") science.binding.sha256 = "0".repeat(64)
    if (kind === "diagnostic_counts" || kind === "diagnostic_audit") {
      const value = JSON.parse(await readFile(science.diagnostic.path, "utf8"))
      if (kind === "diagnostic_counts") value.classes[0].modeled_centers = 0
      else value.audit_sha256 = "0".repeat(64)
      science.diagnostic = await write(science.diagnostic.path, value)
    }
    if (kind === "source_target") item.spec.contract.target.receptor_sha256 = "0".repeat(64)
    if (kind === "receipt_artifact") await Bun.write(path.join(temporary.path,
      path.dirname(item.binding.references.preparation_receipt!.path), "topology.json"), "{}")
    await expect(prepareResearchRunner(item)).rejects.toThrow()
    expect(await Bun.file(path.join(item.run, "session-created.json")).exists()).toBe(false)
  })

test("invoke and repeated prepare rehash every prepared artifact before provider resolution", async () => {
  await using temporary = await tmpdir({ git: true })
  const item = await scientificFixture(temporary.path)
  const prepared = await prepareResearchRunner(item)
  await Bun.write(path.join(temporary.path, item.binding.references.prepared_receptor!.path), "changed prepared receptor")
  await expect(prepareResearchRunner(item)).rejects.toThrow("evidence bytes changed")
  const approval = await write(path.join(temporary.path, "authorization.json"), { schema: "research-host-existing-goal-authorization-v1",
    origin: "existing-user-goal", scope: "offline-replay-model-decisions", loop_id: prepared.manifest.loop_id,
    contract_digest: prepared.manifest.contract_digest, policy_digest: researchDigest(item.spec.policy), source_ref: "SYNTHETIC-test-only",
    statement: "SYNTHETIC authorization; changed proof must fail before provider resolution", authorized_at: new Date().toISOString(),
    max_calls: 1, max_tokens: 200000, max_cost_usd: 10 })
  await expect(invokeResearchRunner({ ...item, authorization: approval.path, abort: new AbortController().signal })).rejects.toThrow("evidence bytes changed")
  expect((await readResearchRunner(item)).model_calls).toEqual([])
})
