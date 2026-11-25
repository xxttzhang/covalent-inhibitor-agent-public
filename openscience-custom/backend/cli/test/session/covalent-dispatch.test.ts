import { afterEach, beforeEach, expect, test } from "bun:test"
import { chmod, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { dispatchCovalent, cancellationObservation } from "../../src/session/covalent-dispatch"

const key = "covalent-discovery_covalent_ligand_design_step"
const args = { action: "pose_status", workflow_id: "ligand-flow-example" }
const output = { structuredContent: { workflow: { workflow_id: args.workflow_id },
  result: { run_id: "20261030-carsidock-example", job_state: "QUEUED" } }, content: [] }
const saved = process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE
const savedLibrary = process.env.COVALENT_LIBRARY_DISPATCH_PROFILE
const savedDeNovo = process.env.COVALENT_DENOVO_DISPATCH_PROFILE
const folders: string[] = []
beforeEach(() => { delete process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE; delete process.env.COVALENT_LIBRARY_DISPATCH_PROFILE; delete process.env.COVALENT_DENOVO_DISPATCH_PROFILE })
afterEach(async () => {
  if (saved) process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE = saved
  else delete process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE
  if (savedLibrary) process.env.COVALENT_LIBRARY_DISPATCH_PROFILE = savedLibrary
  else delete process.env.COVALENT_LIBRARY_DISPATCH_PROFILE
  if (savedDeNovo) process.env.COVALENT_DENOVO_DISPATCH_PROFILE = savedDeNovo
  else delete process.env.COVALENT_DENOVO_DISPATCH_PROFILE
  await Promise.all(folders.splice(0).map(folder => rm(folder, { recursive: true, force: true })))
})
async function configured() {
  const folder = await mkdtemp(path.join(os.tmpdir(), "dispatch-host-"))
  folders.push(folder)
  const state = path.join(folder, "receipts")
  await mkdir(state, { mode: 0o700 })
  const binding = path.join(folder, "binding.json")
  await writeFile(binding, JSON.stringify({ data_root: "/inspire/acceptance/data" }), { mode: 0o600 })
  const filename = path.join(folder, "profile.json")
  // Real failing executable exercises the process/error path without a network or compute mock.
  await writeFile(filename, JSON.stringify({ tool: key, python: "/usr/bin/false", binding,
    root: "/inspire/acceptance/data", state, approval_key: "/inspire/acceptance/data/.secrets/key" }), { mode: 0o600 })
  process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE = filename
  return { filename, receipt: path.join(state, output.structuredContent.result.run_id + ".json") }
}
test.each(["schrodinger-covdock-vs", "schrodinger-covdock-lo", "de-novo-prereaction-glide"])("De novo %s dispatch preserves its signed job and observes the same receipt", async (engine) => {
  const config = await configured()
  const tool = "covalent-discovery_covalent_submit_job"
  const settings = JSON.parse(await readFile(config.filename, "utf8"))
  await writeFile(config.filename, JSON.stringify({ ...settings, tool }), { mode: 0o600 })
  const job = { run_id: output.structuredContent.result.run_id, state: "QUEUED", route: "de-novo", project_id: "de-flow-test", engine }
  const spec = { ...job, request_id: "exact-request", execution_mode: "execute", human_approved: true,
    provenance: { worker_backend: "external-instance" } }
  const value = { structuredContent: { status: "PASS", job, spec }, content: [] }
  const request = { request_id: spec.request_id, project_id: job.project_id, route: job.route, engine }
  expect(await dispatchCovalent(tool, request, value)).toBe(value)
  process.env.COVALENT_DENOVO_DISPATCH_PROFILE = config.filename
  const result = await dispatchCovalent(tool, request, value) as { structuredContent: typeof value.structuredContent & { external_dispatch: unknown }; isError: boolean }
  expect(result.structuredContent).toMatchObject(value.structuredContent)
  expect(result.isError).toBe(true)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({ dispatcher: "dispatch_ligand_vs.py", action: "submit", code: 1 })
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  const observe = tool.replace("submit_job", "get_job")
  await dispatchCovalent(observe, { run_id: job.run_id }, value)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({ action: "status" })
  await expect(dispatchCovalent(observe, { run_id: "wrong-run" }, value)).rejects.toThrow("registered job identity")
  await expect(dispatchCovalent(tool, { ...request, request_id: "other" }, value)).rejects.toThrow("registered job identity")
})
test("absent adapter and non-dispatch actions preserve backend results", async () => {
  expect(await dispatchCovalent(key, args, output)).toBe(output)
  await configured()
  expect(await dispatchCovalent(key, { ...args, action: "generate" }, output)).toBe(output)
  expect(await dispatchCovalent("foreign_" + key, args, output)).toBe(output)
})

test("dispatch annotation preserves workflow protocol, route, batch and task identity", async () => {
  const config = await configured()
  // Existing claim exercises annotation without starting a process or compute.
  await writeFile(config.receipt + ".lock", "", { mode: 0o600 })
  const data = { ...output.structuredContent, schema_version: "research-workflow-result-v1",
    route: "ligand-first", workflow: { ...output.structuredContent.workflow, protocol: "ligand-workflow-v1",
      revision: 2, stage: "COVDOCK_EXECUTION_RUNNING", tools: {} },
    covdock_execution: { run_id: "registered-lo-run", request_id: "exact-request", job_state: "QUEUED" },
    refinement_input: { generated_candidate_count: 4, input_count: 2, candidate_deferrals: [] },
    execution: { state: "submitted" }, artifacts: [{ kind: "sealed_input", id: "input-1" }] }
  const result = await dispatchCovalent(key, args, { ...output, structuredContent: data }) as {
    structuredContent: typeof data & { external_dispatch: unknown }; content: { text: string }[] }
  expect(result.structuredContent).toMatchObject(data)
  expect(JSON.parse(result.content[0].text)).toEqual(result.structuredContent)
  expect(result.structuredContent.external_dispatch).toMatchObject({ state: "dispatch_in_progress_or_reconciliation_required" })
})

test.each([["start_vs_prescreen", "vs_prescreen_status"], ["start_vs", "vs_status"], ["start_lo", "lo_status"]])("Library %s requires its own profile and preserves the same receipt", async (start, status) => {
  const config = await configured()
  const tool = "covalent-discovery_covalent_library_research"
  const request = { action: start, workflow_id: "library-flow-fixture" }
  const value = { ...output, structuredContent: { ...output.structuredContent, workflow: { workflow_id: request.workflow_id } } }
  expect(await dispatchCovalent(tool, request, value)).toBe(value)
  const settings = JSON.parse(await readFile(config.filename, "utf8"))
  await writeFile(config.filename, JSON.stringify({ ...settings, tool }), { mode: 0o600 })
  delete process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE
  process.env.COVALENT_LIBRARY_DISPATCH_PROFILE = config.filename
  expect(await dispatchCovalent(key, request, value)).toBe(value)
  await dispatchCovalent(tool, request, value)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_library_vs.py", action: "submit", code: 1,
  })
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  await dispatchCovalent(tool, { ...request, action: status }, value)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_library_vs.py", action: "status", code: 1,
  })
})
test("unsafe configuration and cross-workflow output fail before a process starts", async () => {
  const config = await configured()
  await chmod(config.filename, 0o644)
  await expect(dispatchCovalent(key, args, output)).rejects.toThrow("owner-only")
  await chmod(config.filename, 0o600)
  await expect(dispatchCovalent(key, { ...args, workflow_id: "other" }, output)).rejects.toThrow("identity mismatch")
})
test("asgbie2 profile cannot redirect another route", async () => {
  const config = await configured()
  const settings = JSON.parse(await readFile(config.filename, "utf8"))
  await writeFile(config.filename, JSON.stringify({ ...settings, instance: "asgbie2" }), { mode: 0o600 })
  await expect(dispatchCovalent(key, args, output)).rejects.toThrow("reserved for Library-first")
  expect(await Bun.file(config.receipt + ".observation.json").exists()).toBe(false)
})
test("an existing in-flight claim cannot dispatch a second process", async () => {
  const config = await configured()
  await writeFile(config.receipt + ".lock", "", { mode: 0o600 })
  const result = await dispatchCovalent(key, args, output) as typeof output & { structuredContent: { external_dispatch: { state: string } } }
  expect(result.structuredContent.external_dispatch.state).toContain("reconciliation_required")
  await expect(Bun.file(config.receipt + ".observation.json").exists()).resolves.toBe(false)
})
test("process failure preserves the registered job and is not scientific failure", async () => {
  const config = await configured()
  const result = await dispatchCovalent(key, args, output) as typeof output & { structuredContent: { external_dispatch: { state: string } } }
  expect(result.structuredContent.result).toEqual(output.structuredContent.result)
  expect(result.structuredContent.external_dispatch.state).toBe("dispatch_failed_or_uncertain_inspect_receipt")
  expect(result).toMatchObject({ isError: true, structuredContent: { external_dispatch: {
    execution_confirmed: false, error: "Dispatcher exited with code 1; execution is not confirmed.",
  } } })
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({ action: "submit", code: 1 })
})

test("large historical branches cannot hide dispatch failure from the model", async () => {
  await configured()
  const result = await dispatchCovalent(key, args, { ...output, structuredContent: {
    ...output.structuredContent, branches: [{ content: "old evidence".repeat(20000) }],
  } }) as { content: { text: string }[] }
  expect(result.content[0].text.length).toBeLessThan(2000)
  expect(result.content[0].text.indexOf("external_dispatch")).toBeLessThan(10)
  expect(result.content[0].text).toContain("Dispatcher exited with code 1")
})
test("a persisted receipt only selects observation, never repeats creation", async () => {
  const config = await configured()
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  await dispatchCovalent(key, args, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({ action: "status", code: 1 })
  await rm(config.receipt)
  await symlink(config.filename, config.receipt)
  await expect(dispatchCovalent(key, args, output)).rejects.toThrow("Invalid dispatch receipt")
})
test("equilibration selects its signed dispatcher and preserves receipt-only recovery", async () => {
  const config = await configured()
  const request = { ...args, action: "md_equilibration_status" }
  await dispatchCovalent(key, request, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_openmm.py", action: "submit", code: 1,
  })
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  await dispatchCovalent(key, request, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_openmm.py", action: "status", code: 1,
  })
  expect(await dispatchCovalent(key, { ...args, action: "start_md_short_qc" }, output)).toBe(output)
})

test("VS uses its CPU dispatcher and never replays a saved reservation", async () => {
  const config = await configured()
  await dispatchCovalent(key, { ...args, action: "start_vs" }, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_vs.py", action: "submit", code: 1,
  })
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  await dispatchCovalent(key, { ...args, action: "vs_status" }, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_vs.py", action: "status", code: 1,
  })
})

const cancelkey = "covalent-discovery_covalent_cancel_job"
const cancelrun = output.structuredContent.result.run_id
const cancelscope = { kind: "cancel", run_id: cancelrun, spec_sha256: "a".repeat(64),
  scheduler: { backend: "external-hpc", name: "vs-one-fixture" } } as const
const canceloutput = { structuredContent: { cancellation_requested: true, cancellation_confirmed: false,
  job: { run_id: cancelrun, route: "ligand-first", state: "QUEUED" },
  scheduler_cancellation: { protocol: "reserved-hpc-cancellation-v1", scope: cancelscope } }, content: [] } as const

async function cancelconfigured() {
  const config = await configured()
  await writeFile(config.receipt, JSON.stringify({ run_id: cancelrun, spec_sha256: cancelscope.spec_sha256,
    name: cancelscope.scheduler.name, dispatch_root: "/inspire/acceptance/data" }), { mode: 0o600 })
  return config
}

test("cancel adapter preserves uncertain failure and never claims termination", async () => {
  const config = await cancelconfigured()
  const result = await dispatchCovalent(cancelkey, { run_id: cancelrun }, canceloutput)
  expect(result).toMatchObject({ structuredContent: { cancellation_confirmed: false,
    job: { state: "QUEUED" }, scheduler_stop: { state: "stop_failed_or_uncertain_inspect_receipt" } } })
  expect(JSON.parse(await readFile(config.receipt + ".stop-observation.json", "utf8"))).toMatchObject({ code: 1 })
  expect(await Bun.file(config.receipt + ".observation.json").exists()).toBe(false)
})

test("cancel rejects cross-run and changed scheduler receipt before spawn", async () => {
  const config = await cancelconfigured()
  await expect(dispatchCovalent(cancelkey, { run_id: "another-run" }, canceloutput)).rejects.toThrow("identity mismatch")
  const receipt = JSON.parse(await readFile(config.receipt, "utf8"))
  await writeFile(config.receipt, JSON.stringify({ ...receipt, name: "other-job" }), { mode: 0o600 })
  await expect(dispatchCovalent(cancelkey, { run_id: cancelrun }, canceloutput)).rejects.toThrow("scope differs")
  expect(await Bun.file(config.receipt + ".stop-observation.json").exists()).toBe(false)
})

test("cancel respects existing dispatch lock and foreign tool namespace", async () => {
  const config = await cancelconfigured()
  expect(await dispatchCovalent("foreign_" + cancelkey, { run_id: cancelrun }, canceloutput)).toBe(canceloutput)
  await writeFile(config.receipt + ".lock", "", { mode: 0o600 })
  expect(await dispatchCovalent(cancelkey, { run_id: cancelrun }, canceloutput)).toMatchObject({
    structuredContent: { scheduler_stop: { state: "observation_in_progress_or_reconciliation_required" } },
  })
  expect(await Bun.file(config.receipt + ".stop-observation.json").exists()).toBe(false)
})

test("get_job recovery uses observation and ignores jobs without signed intent metadata", async () => {
  const config = await cancelconfigured()
  const tool = "covalent-discovery_covalent_get_job"
  expect(await dispatchCovalent(tool, { run_id: cancelrun }, output)).toBe(output)
  expect(await Bun.file(config.receipt + ".stop-observation.json").exists()).toBe(false)
  await dispatchCovalent(tool, { run_id: cancelrun }, canceloutput)
  expect(JSON.parse(await readFile(config.receipt + ".stop-observation.json", "utf8"))).toMatchObject({
    action: "observe", code: 1,
  })
})

test("confirmed never-started cancellation refreshes the returned job, not just a nested label", () => {
  const observation = { run_id: cancelrun, name: cancelscope.scheduler.name, cancellation_confirmed: true,
    scheduler_state: "STOPPED", termination_evidence: { scope: cancelscope,
      confirmed_at: "2026-11-09T13:00:00Z", cancellation_confirmed: true,
      worker_never_started: true, vendor_execution: "not_started" } }
  expect(cancellationObservation(canceloutput.structuredContent, observation)).toMatchObject({
    cancellation_confirmed: true, job: { state: "CANCELLED", finished_at: "2026-11-09T13:00:00Z" },
  })
  expect(() => cancellationObservation(canceloutput.structuredContent, { ...observation, run_id: "wrong-run" }))
    .toThrow("identity mismatch")
  expect(cancellationObservation(canceloutput.structuredContent, { scheduler_state: "STOPPED" }))
    .toMatchObject({ cancellation_confirmed: false, job: { state: "QUEUED" } })
})

test("confirmed cancellation permits readback but cannot dispatch another stop", async () => {
  const config = await cancelconfigured()
  const confirmed = { ...canceloutput, structuredContent: { ...canceloutput.structuredContent,
    cancellation_confirmed: true, job: { ...canceloutput.structuredContent.job, state: "CANCELLED" } } }
  const receipt = config.receipt + ".stop-observation.json"
  const args = { run_id: cancelrun }
  expect(await dispatchCovalent("covalent-discovery_covalent_cancel_job", args, confirmed)).toBe(confirmed)
  expect(await Bun.file(receipt).exists()).toBe(false)
  await dispatchCovalent("covalent-discovery_covalent_get_job", args, confirmed)
  expect(JSON.parse(await readFile(receipt, "utf8"))).toMatchObject({ action: "observe", code: 1 })
})

test.each(["asgbie3", "asgbie4"])("instance %s LO selects its dispatcher while historical LO keeps its executor", async (instance) => {
  const config = await configured()
  const value = { ...output, structuredContent: { ...output.structuredContent,
    result: { ...output.structuredContent.result, execution_instance: instance } } }
  await dispatchCovalent(key, { ...args, action: "start_covdock" }, value)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_lo.py", action: "submit", code: 1,
  })
  await writeFile(config.receipt, "{}", { mode: 0o600 })
  await dispatchCovalent(key, { ...args, action: "covdock_status" }, value)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_lo.py", action: "status", code: 1,
  })
  await rm(config.receipt)
  await dispatchCovalent(key, { ...args, action: "covdock_status" }, output)
  expect(JSON.parse(await readFile(config.receipt + ".observation.json", "utf8"))).toMatchObject({
    dispatcher: "dispatch_ligand_smoke.py", action: "submit", code: 1,
  })
})

test("Library asgbie2 launch is reported as an instance", async () => {
  const config = await configured()
  const settings = JSON.parse(await readFile(config.filename, "utf8"))
  const executable = path.join(path.dirname(config.filename), "dispatch-result")
  await writeFile(executable, '#!/bin/sh\nprintf \'%s\\n\' \'{"submitted":true,"name":"asgbie2"}\'\n', { mode: 0o700 })
  const tool = "covalent-discovery_covalent_library_research"
  await writeFile(config.filename, JSON.stringify({ ...settings, tool, python: executable, instance: "asgbie2" }), { mode: 0o600 })
  process.env.COVALENT_LIBRARY_DISPATCH_PROFILE = config.filename
  const request = { action: "start_vs", workflow_id: "library-flow-fixture" }
  const value = { ...output, structuredContent: { ...output.structuredContent, workflow: { workflow_id: request.workflow_id } } }
  const result = await dispatchCovalent(tool, request, value) as { structuredContent: { external_dispatch: { state: string } } }
  expect(result.structuredContent.external_dispatch.state).toBe("instance_launch_requested")
})
