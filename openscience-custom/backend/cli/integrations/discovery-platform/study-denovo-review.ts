import path from "node:path"
import { createHash, randomUUID } from "node:crypto"
import { lstat, readFile, realpath, rename, readdir, writeFile } from "node:fs/promises"
import z from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { readDeNovoPlan } from "./study-denovo-plan"
import { publishFrozenJSON } from "./frozen-json"
import { deNovoTasks } from "../../src/tool/denovo-task"
import { readDeNovoResults } from "../../src/tool/denovo-results"
import { registeredDeNovoSource } from "../../src/tool/denovo-source"

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const sha = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex")
const sourceSchema = z.object({ schema: z.literal("native-denovo-independent-source-v1"),
  route: z.literal("de-novo"), study_id: z.string(), project_id: z.string(), task_id: z.string(),
  request_sha256: hash, hypothesis_sha256: hash, candidate_id: z.string(), source_candidate_sha256: hash,
  receptor_sha256: hash, candidate_count: z.literal(1), control_count: z.literal(1),
  input_contract: z.object({ input_count: z.literal(2), input_sha256: hash, identity_sha256: hash }).strict(),
  generated_coordinates_used_for_refinement: z.literal(false), ready_for_scientific_handoff: z.literal(false),
  compute_submitted: z.literal(false), independent_prereaction_evaluation: z.literal("NOT_PERFORMED"),
  independent_adduct_evaluation: z.literal("NOT_PERFORMED"), control_redocking: z.literal("NOT_PERFORMED"),
  files: z.record(z.string(), hash),
}).strict()
const receiptSchema = z.object({ schema: z.literal("study-denovo-independent-input-v1"),
  study_id: z.string(), project_id: z.string(), source_id: z.string().regex(/^denovo-review-[a-f0-9]{32}$/),
  source_manifest_sha256: hash, candidate_id: z.string(), generation_plan_digest: z.string(),
  generation_request_sha256: hash, evaluation_script_sha256: hash,
  source_directory: z.literal("denovo-independent-input"),
  compute_submitted: z.literal(false), remote_source_registered: z.literal(false),
}).strict()

async function studyFolder(directory: string, study: DiscoveryStudy) {
  const root = await realpath(directory)
  const folder = path.join(root, ".discovery/studies", study.id)
  if (await realpath(folder) !== folder) throw Error("Study source directory must not traverse symlinks")
  return folder
}

/** Hash validation reads only host-owned receipts, never caller-provided PASS labels. */
export async function readDeNovoIndependentInput(directory: string, study: DiscoveryStudy) {
  const folder = await studyFolder(directory, study)
  const file = path.join(folder, "denovo-independent-input.json")
  if (!await Bun.file(file).exists()) return null
  if (await realpath(file) !== file || !(await lstat(file)).isFile()) throw Error("Independent input receipt must be regular")
  const receipt = receiptSchema.parse(await Bun.file(file).json())
  const plan = await readDeNovoPlan(directory, study)
  if (!plan || receipt.study_id !== study.id || receipt.project_id !== study.project
    || receipt.generation_plan_digest !== plan.planDigest) throw Error("Independent input study/plan binding changed")
  const root = path.join(folder, receipt.source_directory)
  if (await realpath(root) !== root) throw Error("Independent source directory must not traverse symlinks")
  const manifest = path.join(root, "source.json")
  if (await realpath(manifest) !== manifest || !(await lstat(manifest)).isFile()) throw Error("Independent manifest must be regular")
  const bytes = await readFile(manifest)
  if (sha(bytes) !== receipt.source_manifest_sha256 || receipt.source_id !== "denovo-review-" + sha(bytes).slice(0, 32))
    throw Error("Independent input content identity changed")
  const source = sourceSchema.parse(JSON.parse(bytes.toString()))
  if (source.study_id !== study.id || source.project_id !== study.project || source.task_id !== plan.plan.request.id
    || source.candidate_id !== receipt.candidate_id || source.request_sha256 !== receipt.generation_request_sha256)
    throw Error("Independent input generation binding changed")
  const observed: Record<string, string> = {}
  async function inspect(current: string) {
    for (const item of await readdir(current, { withFileTypes: true })) {
      const filename = path.join(current, item.name)
      if (item.isSymbolicLink()) throw Error("Independent input contains a symlink")
      if (item.isDirectory()) { await inspect(filename); continue }
      if (!item.isFile()) throw Error("Independent input contains a nonregular artifact")
      const name = path.relative(root, filename)
      if (name !== "source.json") observed[name] = sha(await readFile(filename))
    }
  }
  await inspect(root)
  if (Object.keys(observed).length !== Object.keys(source.files).length
    || Object.entries(observed).some(([name, checksum]) => source.files[name] !== checksum))
    throw Error("Independent input source inventory changed")
  const latest = path.join(folder, "denovo-independent-latest.json")
  const review = await (async () => {
    if (!await Bun.file(latest).exists()) return null
    if (await realpath(latest) !== latest) throw Error("Independent observation pointer must be regular")
    const pointer = z.object({ sha256: hash }).strict().parse(await Bun.file(latest).json())
    const filename = path.join(folder, `denovo-independent-observation-${pointer.sha256}.json`)
    if (await realpath(filename) !== filename) throw Error("Independent observation must be regular")
    const raw = await Bun.file(filename).json()
    const value = z.object({ study_id: z.literal(study.id), source_id: z.literal(receipt.source_id),
      source_manifest_sha256: z.literal(receipt.source_manifest_sha256), compute_submitted: z.literal(false) })
      .passthrough().parse(raw)
    if (sha(new TextEncoder().encode(JSON.stringify(raw))) !== pointer.sha256) throw Error("Independent observation checksum changed")
    return value
  })()
  return { ...receipt, source, local_directory: root,
    latest_review: review,
    next: { tool: "covalent_de_novo_review", action: "plan", source_id: receipt.source_id,
      source_manifest_sha256: receipt.source_manifest_sha256,
      requires_host_transport_to: `state/de-novo-inputs/${receipt.source_id}` },
    state: "INPUTS_FROZEN_REMOTE_REGISTRATION_PENDING" as const,
    independent_prereaction_evaluation: "NOT_PERFORMED", independent_adduct_evaluation: "NOT_PERFORMED",
    md_handoff: "NOT_PERFORMED", ready_for_scientific_handoff: false }
}

export async function prepareDeNovoIndependentInput(directory: string, study: DiscoveryStudy, candidateID: string) {
  const saved = await readDeNovoIndependentInput(directory, study)
  if (saved) {
    if (saved.candidate_id !== candidateID) throw Error("This study already froze another candidate; preserve that selection")
    return saved
  }
  const plan = await readDeNovoPlan(directory, study)
  if (!plan) throw Error("Compile and complete this study's native generation first")
  const results = await readDeNovoResults(deNovoTasks, String(plan.plan.request.id),
    { directory, study: study.id, request: plan.plan.request })
  if (results.state !== "RESULTS_READ" || !results.candidates) throw Error("Native generation results are not ready")
  const candidate = results.candidates.find(row => row.candidate_id === candidateID)
  if (!candidate?.structural_qc_passed || !candidate.source_sha256) throw Error("Select one source-verified structurally passing candidate")
  const source = await registeredDeNovoSource()
  if (!await Bun.file(source.evaluation).exists()) throw Error("Active release lacks the registered independent-input adapter; publish the reviewed release first")
  const profile = z.object({ local_python: z.string(), allowed_directories: z.array(z.string()) }).passthrough()
    .parse(await Bun.file(path.join(source.package, "profile.json")).json())
  if (!profile.allowed_directories.includes(directory)) throw Error("Project is not registered for native De novo evaluation")
  const folder = await studyFolder(directory, study)
  const output = path.join(folder, "denovo-independent-input")
  // A reserved directory is never overwritten or silently rebuilt after a crash.
  if (await Bun.file(path.join(output, "source.json")).exists()) throw Error("Uncommitted independent source exists; inspect before recovery")
  const temporary = path.join(folder, ".denovo-independent-" + randomUUID())
  const python = Bun.spawn([profile.local_python, "-B", source.evaluation,
    path.join(deNovoTasks, String(plan.plan.request.id)), "--request-sha256", results.provenance.request_sha256,
    "--study-id", study.id, "--project-id", study.project, "--candidate-id", candidateID, "--output", temporary],
    { cwd: directory, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
      stdout: "pipe", stderr: "pipe" })
  const timer = setTimeout(() => python.kill(), 120_000)
  const [code, stdout, stderr] = await Promise.all([python.exited,
    new Response(python.stdout).text(), new Response(python.stderr).text()]).finally(() => clearTimeout(timer))
  if (code !== 0) {
    await publishFrozenJSON(path.join(folder, "denovo-independent-error-" + randomUUID() + ".json"),
      { candidate_id: candidateID, task_id: plan.plan.request.id, state: "INPUT_PREPARATION_FAILED",
        error: stderr.slice(-8000), compute_submitted: false, independent_evaluation: "NOT_PERFORMED" })
    throw Error("Independent input verification failed: " + stderr.slice(-2000))
  }
  const prepared = z.object({ source_id: z.string(), source_manifest_sha256: hash }).passthrough().parse(JSON.parse(stdout))
  const value = sourceSchema.parse(await Bun.file(path.join(temporary, "source.json")).json())
  if (value.source_candidate_sha256 !== candidate.source_sha256 || value.candidate_id !== candidateID)
    throw Error("Recomputed candidate differs from the native study result")
  await rename(temporary, output)
  const receipt = receiptSchema.parse({ schema: "study-denovo-independent-input-v1", study_id: study.id,
    project_id: study.project, source_id: prepared.source_id, source_manifest_sha256: prepared.source_manifest_sha256,
    candidate_id: candidateID, generation_plan_digest: plan.planDigest,
    generation_request_sha256: results.provenance.request_sha256,
    evaluation_script_sha256: sha(await readFile(source.evaluation)), source_directory: "denovo-independent-input",
    compute_submitted: false, remote_source_registered: false })
  await publishFrozenJSON(path.join(folder, "denovo-independent-input.json"), receipt)
  return readDeNovoIndependentInput(directory, study)
}

const transportSchema = z.object({ state: z.literal("SOURCE_REGISTERED_NO_COMPUTE"), source_id: z.string(),
  source_manifest_sha256: hash, study_id: z.string(), data_root: z.string(), remote_directory: z.string(),
  file_count: z.number().int().positive(), compute_submitted: z.literal(false), bridge_sha256: hash,
  transport_script_sha256: hash }).strict()

export async function registerDeNovoIndependentInput(directory: string, study: DiscoveryStudy) {
  const input = await readDeNovoIndependentInput(directory, study)
  if (!input) throw Error("Freeze one native-study independent input first")
  const source = await registeredDeNovoSource()
  if (!await Bun.file(source.reviewTransport).exists()) throw Error("Publish the registered source transport before use")
  const profile = z.object({ local_python: z.string() }).passthrough()
    .parse(await Bun.file(path.join(source.package, "profile.json")).json())
  const workspace = path.resolve(import.meta.dir, "../../../../..")
  const child = Bun.spawn([profile.local_python, "-B", source.reviewTransport,
    "--project-directory", directory, "--study-id", study.id, "--workspace", workspace],
    { cwd: directory, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" }, stdout: "pipe", stderr: "pipe" })
  const timer = setTimeout(() => child.kill(), 190_000)
  const [code, stdout, stderr] = await Promise.all([child.exited,
    new Response(child.stdout).text(), new Response(child.stderr).text()]).finally(() => clearTimeout(timer))
  if (code !== 0) throw Error("Independent source transport failed; inspect before retry: " + stderr.slice(-2000))
  const result = transportSchema.parse(JSON.parse(stdout))
  if (result.study_id !== study.id || result.source_id !== input.source_id
    || result.source_manifest_sha256 !== input.source_manifest_sha256
    || result.remote_directory !== `${result.data_root}/state/de-novo-inputs/${input.source_id}`)
    throw Error("Transport readback does not match this study")
  const folder = await studyFolder(directory, study)
  const filename = path.join(folder, "denovo-independent-transport.json")
  await publishFrozenJSON(filename, result)
  const published = transportSchema.parse(await Bun.file(filename).json())
  if (JSON.stringify(published) !== JSON.stringify(result)) throw Error("Registered transport binding changed; preserve the original study root")
  return { ...result, next: { tool: "denovo_generate", action: "plan_evaluation", study_id: study.id } }
}

/** These fixed actions cannot submit computation or pass an approval envelope. */
export async function syncDeNovoIndependentReview(directory: string, study: DiscoveryStudy,
  action: "plan" | "replan" | "cancel_plan" | "status" | "evaluate" | "prepare_md_handoff" | "plan_prereaction" | "prereaction_status" | "evaluate_prereaction") {
  const input = await readDeNovoIndependentInput(directory, study)
  if (!input) throw Error("Freeze one native-study independent input first")
  const folder = await studyFolder(directory, study)
  const filename = path.join(folder, "denovo-independent-transport.json")
  if (!await Bun.file(filename).exists()) throw Error("Register this exact source package before independent evaluation")
  if (await realpath(filename) !== filename) throw Error("Transport receipt must not be a symlink")
  const transport = transportSchema.parse(await Bun.file(filename).json())
  if (transport.study_id !== study.id || transport.source_id !== input.source_id
    || transport.source_manifest_sha256 !== input.source_manifest_sha256) throw Error("Transport source binding changed")
  const { MCP } = await import("../../src/mcp")
  const tools = await MCP.tools()
  const tool = tools["covalent-discovery_covalent_de_novo_review"]
  if (!tool?.execute) throw Error("Active shared release lacks covalent_de_novo_review; publish and reconnect before use")
  const response = z.object({ isError: z.boolean().optional(), structuredContent: z.record(z.string(), z.unknown()).optional(),
    content: z.array(z.object({ type: z.string(), text: z.string().optional() }).passthrough()).optional() }).passthrough().parse(
      await tool.execute({ action, source_id: input.source_id, source_manifest_sha256: input.source_manifest_sha256 },
        { toolCallId: "denovo-review-" + randomUUID(), messages: [] }))
  if (response.isError) throw Error("Independent review failed: " + JSON.stringify(response.structuredContent ?? response.content))
  const result = z.object({ study_id: z.literal(study.id), source_id: z.literal(input.source_id),
    source_manifest_sha256: z.literal(input.source_manifest_sha256), data_root: z.literal(transport.data_root),
    compute_submitted: z.literal(false) }).passthrough().parse(response.structuredContent)
  const digest = sha(new TextEncoder().encode(JSON.stringify(result)))
  await publishFrozenJSON(path.join(folder, `denovo-independent-observation-${digest}.json`), result)
  const temporary = path.join(folder, `.denovo-independent-latest-${randomUUID()}.pending`)
  await writeFile(temporary, JSON.stringify({ sha256: digest }), { flag: "wx", mode: 0o600 })
  await rename(temporary, path.join(folder, "denovo-independent-latest.json"))
  return result
}
