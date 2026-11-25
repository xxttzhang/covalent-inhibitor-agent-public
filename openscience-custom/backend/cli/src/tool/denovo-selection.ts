import path from "node:path"
import z from "zod"
import { createHash } from "node:crypto"
import { lstat, realpath, readFile, writeFile } from "node:fs/promises"
import { isDeepStrictEqual } from "node:util"
import { publishFrozenJSON } from "../../integrations/discovery-platform/frozen-json"
import { registeredDeNovoSource } from "./denovo-source"
import { readDeNovoResults } from "./denovo-results"
import { deNovoTasks } from "./denovo-task"
import { dispatchCovalent } from "../session/covalent-dispatch"

export async function batchLOFile(id: string, kind: "input" | "plan") {
  const retry = path.join(deNovoTasks, id, "batch-lo-retry-input.json")
  return path.join(deNovoTasks, id, `batch-lo-${await Bun.file(retry).exists() ? "retry-" : ""}${kind}.json`)
}

export async function statusDeNovoBatch(id: string, directory: string, session: string,
  action: "status_vs" | "read_vs_products" | "prepare_lo" | "retry_lo" | "plan_lo" | "status_lo" | "evaluate_lo" | "prepare_prereaction_batch" | "plan_prereaction_batch" | "status_prereaction_batch" | "evaluate_prereaction_batch" | "report_batch" = "status_vs") {
  const results = await readDeNovoResults(deNovoTasks, id, { directory, session })
  if (results.state !== "RESULTS_READ") throw Error("Native generation results are not ready")
  const filename = path.join(deNovoTasks, id, "batch-vs-plan.json")
  if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
    throw Error("Missing regular frozen batch VS plan")
  const plan = z.object({ schema: z.literal("denovo-batch-vs-plan-v1"),
    manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/) }).passthrough().parse(await Bun.file(filename).json())
  const manifest = path.join(deNovoTasks, id, "batch-input/input.json")
  if (createHash("sha256").update(new Uint8Array(await Bun.file(manifest).arrayBuffer())).digest("hex") !== plan.manifest_sha256)
    throw Error("Batch status source differs from its frozen plan")
  const { MCP } = await import("../mcp")
  const tool = (await MCP.tools())["covalent-discovery_covalent_de_novo_batch"]
  if (!tool?.execute) throw Error("De novo batch connector unavailable")
  const stage = !["status_vs", "read_vs_products", "prepare_lo"].includes(action)
    ? z.object({ manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
        batch_manifest_sha256: z.literal(plan.manifest_sha256) }).passthrough()
      .parse(await Bun.file(await batchLOFile(id, "input")).json()) : undefined
  const prereaction = ["plan_prereaction_batch", "status_prereaction_batch", "evaluate_prereaction_batch", "report_batch"].includes(action)
    ? z.object({ source_sha256: z.string().regex(/^[a-f0-9]{64}$/),
        manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/) }).passthrough()
      .parse(await Bun.file(path.join(deNovoTasks, id, "batch-prereaction-input.json")).json()) : undefined
  const response = z.object({ isError: z.boolean().optional(), structuredContent: z.unknown() }).passthrough()
    .parse(await tool.execute({ action, manifest_sha256: plan.manifest_sha256,
      ...(stage ? { lo_manifest_sha256: stage.manifest_sha256 } : {}),
      ...(prereaction ? { prereaction_source_sha256: prereaction.source_sha256,
        prereaction_manifest_sha256: prereaction.manifest_sha256 } : {}) },
      { toolCallId: "denovo-batch-status-" + id, messages: [] }))
  if (response.isError) throw Error("Batch " + action + " failed; do not resubmit: " + JSON.stringify(response.structuredContent))
  // Continue only the already registered, signed instance job; never submit a new job.
  const dispatch = await (async () => {
    if (!["status_vs", "status_lo", "status_prereaction_batch"].includes(action)) return undefined
    const current = z.object({ execution: z.object({ run_id: z.string().optional() }).passthrough() }).parse(response.structuredContent)
    if (!current.execution.run_id) return undefined
    const key = "covalent-discovery_covalent_get_job"
    const observe = (await MCP.tools())[key]
    if (!observe?.execute) throw Error("Registered instance observation unavailable")
    const args = { run_id: current.execution.run_id }
    const observed = await observe.execute(args, { toolCallId: "denovo-instance-" + id, messages: [] })
    const delivered = z.object({ isError: z.boolean().optional(), structuredContent: z.record(z.string(), z.unknown()) })
      .passthrough().parse(await dispatchCovalent(key, args, observed))
    if (delivered.isError) throw Error("Original instance dispatch needs review: " + JSON.stringify(delivered.structuredContent.external_dispatch))
    return delivered.structuredContent.external_dispatch
  })()
  if (["status_vs", "status_lo", "evaluate_lo", "status_prereaction_batch", "evaluate_prereaction_batch"].includes(action)) {
    const current = z.object({ execution: z.object({ identity: z.object({ request_id: z.string() }),
      registered_job_observed: z.boolean().optional(), job_state: z.string().optional() }).passthrough() }).parse(response.structuredContent)
    if (current.execution.registered_job_observed && current.execution.job_state) {
      const { acknowledgeDeNovoCompute } = await import("../session/denovo-continuation-compute")
      await acknowledgeDeNovoCompute({ directory, project: (await import("../project/instance")).Instance.project.id, session },
        action === "status_vs" ? "vs" : action.includes("prereaction") ? "glide" : "lo", id,
        current.execution.identity.request_id, current.execution.job_state)
    }
  }
  if (action === "report_batch") {
    const saved = z.object({ manifest_sha256: z.string(), source_sha256: z.string() }).passthrough()
      .parse(await Bun.file(path.join(deNovoTasks, id, "batch-prereaction-input.json")).json())
    const value = z.object({ schema: z.literal("denovo-candidate-ranking-v1"), task_id: z.literal(id),
      manifest_sha256: z.literal(saved.manifest_sha256), source_sha256: z.literal(saved.source_sha256),
      compute_submitted: z.literal(false), scientific_acceptance: z.literal(false), md_ready: z.literal(false),
      candidates: z.array(z.record(z.string(), z.unknown())).min(1).max(12),
      report_artifact: z.object({ path: z.string(), sha256: z.string().regex(/^[a-f0-9]{64}$/) }),
    }).passthrough().parse(response.structuredContent)
    await publishFrozenJSON(path.join(deNovoTasks, id, "batch-report.json"), value)
    return value
  }
  if (action === "prepare_prereaction_batch") {
    const value = z.object({ schema: z.literal("denovo-batch-prereaction-input-v1"), task_id: z.literal(id),
      source_sha256: z.string().regex(/^[a-f0-9]{64}$/), manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
      compute_submitted: z.literal(false), docking_performed: z.literal(false),
      states: z.array(z.record(z.string(), z.unknown())).min(1).max(5),
    }).passthrough().parse(response.structuredContent)
    const receipt = path.join(deNovoTasks, id, "batch-prereaction-input.json")
    await publishFrozenJSON(receipt, value)
    if (!isDeepStrictEqual(await Bun.file(receipt).json(), value)) throw Error("Frozen prereaction inputs changed")
    return value
  }
  if (action === "status_lo" || action === "evaluate_lo" || action === "status_prereaction_batch" || action === "evaluate_prereaction_batch") {
    const glide = action === "status_prereaction_batch" || action === "evaluate_prereaction_batch"
    const saved = z.object({ manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/) }).passthrough()
      .parse(await Bun.file(glide ? path.join(deNovoTasks, id, "batch-prereaction-input.json") : await batchLOFile(id, "input")).json())
    return { ...z.object({ schema: z.literal(glide ? "denovo-batch-glide-status-v1" : "denovo-batch-lo-status-v1"), task_id: z.literal(id),
      manifest_sha256: z.literal(saved.manifest_sha256), compute_submitted: z.literal(false),
      scientific_acceptance: z.literal(false), execution: z.record(z.string(), z.unknown()),
    }).passthrough().parse(response.structuredContent), ...(dispatch ? { external_dispatch: dispatch } : {}) }
  }
  if (action === "plan_lo" || action === "plan_prereaction_batch") {
    const value = z.object({ schema: z.literal(action === "plan_lo" ? "denovo-batch-lo-plan-v1" : "denovo-batch-glide-plan-v1"), task_id: z.literal(id),
      manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/), native_approval_required: z.literal(true),
      compute_submitted: z.literal(false), submit_arguments: z.record(z.string(), z.unknown()),
    }).passthrough().parse(response.structuredContent)
    const receipt = action === "plan_lo" ? await batchLOFile(id, "plan") : path.join(deNovoTasks, id, "batch-glide-plan.json")
    await publishFrozenJSON(receipt, value)
    if (!isDeepStrictEqual(await Bun.file(receipt).json(), value)) throw Error("Frozen LO plan changed")
    return value
  }
  if (action === "prepare_lo" || action === "retry_lo") {
    const value = z.object({ schema: z.literal("denovo-batch-lo-input-v1"), task_id: z.literal(id),
      batch_manifest_sha256: z.literal(plan.manifest_sha256), manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/),
      candidate_count: z.number().int().min(1).max(12), compute_submitted: z.literal(false),
      generated_coordinates_reused: z.literal(false), vs_coordinates_reused: z.literal(false),
    }).passthrough().parse(response.structuredContent)
    const receipt = path.join(deNovoTasks, id, action === "retry_lo" ? "batch-lo-retry-input.json" : "batch-lo-input.json")
    if (action === "retry_lo") z.object({ retry_of: z.object({ manifest_sha256: z.string(),
      run_id: z.string(), attempt: z.literal(2) }) }).parse(value)
    await publishFrozenJSON(receipt, value)
    if (!isDeepStrictEqual(await Bun.file(receipt).json(), value)) throw Error("Frozen LO selection changed")
    return value
  }
  if (action === "read_vs_products") return z.object({ schema: z.literal("denovo-batch-vs-products-v1"), task_id: z.literal(id),
    manifest_sha256: z.literal(plan.manifest_sha256), compute_submitted: z.literal(false),
    scientific_acceptance: z.literal(false), candidate_count: z.number().int().nonnegative(),
    lo_selection: z.record(z.string(), z.unknown()),
    original_execution_state: z.enum(["COMPLETED", "FAILED"]), postprocessing_recovery: z.boolean(),
    reaction_site_rejections: z.array(z.record(z.string(), z.unknown())),
    evaluation_policy: z.string(), candidates: z.array(z.record(z.string(), z.unknown())) })
    .passthrough().parse(response.structuredContent)
  return { ...z.object({ schema: z.literal("denovo-batch-vs-status-v1"), task_id: z.literal(id),
    manifest_sha256: z.literal(plan.manifest_sha256), compute_submitted: z.literal(false),
    execution: z.record(z.string(), z.unknown()) }).passthrough().parse(response.structuredContent),
    ...(dispatch ? { external_dispatch: dispatch } : {}) }
}

export async function planDeNovoBatch(id: string, directory: string, session: string) {
  const registration = await registerDeNovoBatch(id, directory, session)
  const filename = path.join(deNovoTasks, id, "batch-vs-plan.json")
  const existing = await readFrozenVSPlan(filename, id, registration.manifest_sha256)
  if (existing) return existing
  const { MCP } = await import("../mcp")
  const tool = (await MCP.tools())["covalent-discovery_covalent_de_novo_batch"]
  if (!tool?.execute) throw Error("Publish and connect the De novo batch scientific tool before planning")
  const response = z.object({ isError: z.boolean().optional(), structuredContent: z.unknown(),
    content: z.unknown().optional() }).passthrough().parse(await tool.execute({ action: "plan_vs",
      manifest_sha256: registration.manifest_sha256, cpu_count: 55, max_runtime_minutes: 60 },
      { toolCallId: "denovo-batch-" + id, messages: [] }))
  if (response.isError) throw Error("Batch VS plan failed: " + JSON.stringify(response.structuredContent ?? response.content))
  const plan = z.object({ schema: z.literal("denovo-batch-vs-plan-v1"),
    manifest_sha256: z.literal(registration.manifest_sha256), compute_submitted: z.literal(false),
    native_approval_required: z.literal(true), submit_arguments: z.record(z.string(), z.unknown()),
  }).passthrough().parse(response.structuredContent)
  await publishFrozenJSON(filename, plan)
  if (!isDeepStrictEqual(await Bun.file(filename).json(), plan))
    throw Error("Batch VS plan changed; preserve the original frozen plan")
  return plan
}

export async function readFrozenVSPlan(filename: string, task: string, digest: string) {
  const entry = await lstat(filename).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return undefined
    throw error
  })
  if (!entry) return undefined
  if (!entry.isFile() || await realpath(filename) !== filename)
    throw Error("Frozen VS plan must be a regular file")
  const plan = z.object({ manifest_sha256: z.literal(digest) }).passthrough().parse(await Bun.file(filename).json())
  const { boundBatchSubmission } = await import("../session/denovo-batch-execution")
  boundBatchSubmission(plan, task, "vs")
  return plan
}

export async function registerDeNovoBatch(id: string, directory: string, session: string) {
  const input = await prepareDeNovoBatch(id, directory, session)
  const source = await registeredDeNovoSource()
  const profile = z.object({ local_python: z.string() }).passthrough()
    .parse(await Bun.file(path.join(source.package, "profile.json")).json())
  const script = path.resolve(source.package, "../../runtime/batch_transport.py")
  if (!await Bun.file(script).exists()) throw Error("Publish the batch transport adapter before registration")
  const manifest = new Uint8Array(await Bun.file(path.join(input.directory, "input.json")).arrayBuffer())
  const checksum = createHash("sha256").update(manifest).digest("hex")
  const child = Bun.spawn([profile.local_python, "-B", script, "--input", input.directory,
    "--manifest-sha256", checksum, "--workspace", path.resolve(import.meta.dir, "../../../../..")], {
    cwd: directory, env: { ...process.env, PYTHONNOUSERSITE: "1" }, stdout: "pipe", stderr: "pipe",
  })
  const timeout = setTimeout(() => child.kill(), 190_000)
  const [exit, stdout, stderr] = await Promise.all([child.exited, new Response(child.stdout).text(),
    new Response(child.stderr).text()]).finally(() => clearTimeout(timeout))
  if (exit !== 0) throw Error("Batch registration failed: " + stderr.slice(-2000))
  const result = z.object({ state: z.literal("BATCH_REGISTERED_NO_COMPUTE"), task_id: z.literal(id),
    manifest_sha256: z.literal(checksum), remote_directory: z.string(), compute_submitted: z.literal(false),
  }).strict().parse(JSON.parse(stdout))
  const receipt = path.join(input.directory, "registration.json")
  await publishFrozenJSON(receipt, result)
  if (!isDeepStrictEqual(await Bun.file(receipt).json(), result))
    throw Error("Batch registration destination changed; preserve the original receipt")
  return result
}

export async function prepareDeNovoBatch(id: string, directory: string, session: string) {
  const frozen = await selectDeNovoBatch(id, directory, session)
  const source = await registeredDeNovoSource()
  const profile = z.object({ local_python: z.string() }).passthrough()
    .parse(await Bun.file(path.join(source.package, "profile.json")).json())
  const output = path.join(deNovoTasks, id, "batch-input")
  const manifest = path.join(output, "input.json")
  const request = z.object({ profile: z.literal("egfr-c797-acrylamide-v1"),
    package_hashes: z.record(z.string(), z.string()) }).passthrough()
    .parse(await Bun.file(path.join(deNovoTasks, id, "request.json")).json())
  const receptor = path.join(deNovoTasks, id, "package/assets/protein_A.pdb")
  if (!(await lstat(receptor)).isFile() || await realpath(receptor) !== receptor)
    throw Error("Generation receptor must be a regular source file")
  const bytes = await readFile(receptor)
  const receptorHash = createHash("sha256").update(bytes).digest("hex")
  if (request.package_hashes["assets/protein_A.pdb"] !== receptorHash)
    throw Error("Generation receptor differs from its frozen request")
  async function read() {
    for (const filename of [manifest, path.join(output, "ligands.sdf"), path.join(output, "receptor.pdb")]) {
      if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
        throw Error("Batch input must contain regular files without symlinks")
    }
    const value = z.object({ schema: z.literal("pocketxmol-batch-input-v1"),
      task_id: z.literal(id), generation_provenance: z.record(z.string(), z.string()),
      ligands_sha256: z.string(), compute_submitted: z.literal(false),
    }).passthrough().parse(await Bun.file(manifest).json())
    const saved = z.object({ selection_sha256: z.literal(frozen.sha256), manifest_sha256: z.string(),
      receptor_sha256: z.literal(receptorHash), attachment_residue: z.literal("A:797") }).strict()
      .parse(await Bun.file(path.join(output, "binding.json")).json())
    const sha = async (name: string) => createHash("sha256").update(
      new Uint8Array(await Bun.file(name).arrayBuffer())).digest("hex")
    if (saved.manifest_sha256 !== await sha(manifest)
      || saved.receptor_sha256 !== await sha(path.join(output, "receptor.pdb"))
      || value.ligands_sha256 !== await sha(path.join(output, "ligands.sdf"))
      || !isDeepStrictEqual(value.generation_provenance, frozen.selection.provenance))
      throw Error("Frozen batch input changed")
    return { ...value, directory: output, selection_sha256: frozen.sha256,
      receptor_sha256: receptorHash, attachment_residue: "A:797" }
  }
  if (await Bun.file(manifest).exists()) return read()
  const child = Bun.spawn([profile.local_python, "-B", "-m", "covscreen_de_novo.batch_input", "--output", output], {
    cwd: directory, env: { ...process.env, PYTHONPATH: path.resolve(source.package, "../../src"), PYTHONNOUSERSITE: "1" },
    stdin: new TextEncoder().encode(JSON.stringify(frozen.selection)), stdout: "pipe", stderr: "pipe",
  })
  const timeout = setTimeout(() => child.kill(), 120_000)
  const [exit, , stderr] = await Promise.all([child.exited, new Response(child.stdout).text(),
    new Response(child.stderr).text()]).finally(() => clearTimeout(timeout))
  if (exit !== 0) throw Error("Batch input export failed: " + stderr.slice(-2000))
  await writeFile(path.join(output, "receptor.pdb"), bytes, { flag: "wx", mode: 0o600 })
  await publishFrozenJSON(path.join(output, "binding.json"), { selection_sha256: frozen.sha256,
    receptor_sha256: receptorHash, attachment_residue: "A:797",
    manifest_sha256: createHash("sha256").update(new Uint8Array(await Bun.file(manifest).arrayBuffer())).digest("hex") })
  return read()
}

export async function selectDeNovoBatch(id: string, directory: string, session: string) {
  const results = await readDeNovoResults(deNovoTasks, id, { directory, session })
  if (results.state !== "RESULTS_READ") throw Error("Native generation results are not ready")
  const filename = path.join(deNovoTasks, id, "batch-selection.json")
  const schema = z.object({ schema: z.literal("pocketxmol-batch-selection-v1"), task_id: z.literal(id),
    compute_submitted: z.literal(false), independent_preparation: z.literal("NOT_PERFORMED"),
    provenance: z.record(z.string(), z.string()),
  }).passthrough()
  const checksum = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex")
  async function read() {
    if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
      throw Error("Batch selection must be a regular file without symlinks")
    const saved = z.object({ sha256: z.string(), selection: schema }).strict()
      .parse(await Bun.file(filename).json())
    if (checksum(saved.selection) !== saved.sha256
      || !isDeepStrictEqual(saved.selection.provenance, results.provenance))
      throw Error("Frozen selection or generation provenance changed")
    return saved
  }
  if (await Bun.file(filename).exists()) return read()
  const source = await registeredDeNovoSource()
  const profile = z.object({ local_python: z.string(), allowed_directories: z.array(z.string()) })
    .passthrough().parse(await Bun.file(path.join(source.package, "profile.json")).json())
  if (!profile.allowed_directories.includes(directory)) throw Error("Project is not registered for De novo selection")
  const code = path.resolve(source.package, "../../src")
  if (!await Bun.file(path.join(code, "covscreen_de_novo/batch_selection.py")).exists())
    throw Error("Publish the PocketXMol batch selection module before use")
  const child = Bun.spawn([profile.local_python, "-B", "-m", "covscreen_de_novo.batch_selection", "-"], {
    cwd: directory, env: { ...process.env, PYTHONPATH: code, PYTHONNOUSERSITE: "1" },
    stdin: new TextEncoder().encode(JSON.stringify(results)), stdout: "pipe", stderr: "pipe",
  })
  const timeout = setTimeout(() => child.kill(), 120_000)
  const [exit, stdout, stderr] = await Promise.all([child.exited,
    new Response(child.stdout).text(), new Response(child.stderr).text()]).finally(() => clearTimeout(timeout))
  if (exit !== 0) throw Error("Batch chemical selection failed: " + stderr.slice(-2000))
  const selection = schema.parse(JSON.parse(stdout))
  if (!isDeepStrictEqual(selection.provenance, results.provenance))
    throw Error("Batch selector returned a different generation source")
  await publishFrozenJSON(filename, { sha256: checksum(selection), selection })
  return read()
}
