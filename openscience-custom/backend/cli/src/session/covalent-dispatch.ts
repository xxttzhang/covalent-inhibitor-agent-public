import path from "node:path"
import { lstat, mkdir, open, readFile, unlink, writeFile } from "node:fs/promises"
import z from "zod"

const profile = z.object({
  tool: z.string().regex(/^[a-zA-Z0-9_-]+_covalent_(ligand_design_step|library_research|submit_job)$/),
  python: z.string().startsWith("/"),
  binding: z.string().startsWith("/"),
  root: z.string().startsWith("/inspire/"),
  state: z.string().startsWith("/"),
  approval_key: z.string().startsWith("/inspire/"),
  runtime: z.string().startsWith("/").optional(),
  instance: z.literal("asgbie2").optional(),
  recover_run: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$/).optional(),
  recover_terminal: z.string().regex(/^[a-f0-9]{64}$/).optional(),
}).strict().superRefine((value, ctx) => {
  if (value.instance && !value.tool.endsWith("_covalent_library_research"))
    ctx.addIssue({ code: "custom", message: "asgbie2 dispatch is reserved for Library-first" })
})
const actions = new Set(["start_pose", "pose_status", "start_af3", "af3_status", "start_covdock", "covdock_status"])
const vs = new Set(["start_vs", "vs_status"])
const library = new Set(["start_vs_prescreen", "vs_prescreen_status", "start_vs", "vs_status", "start_lo", "lo_status"])
const equilibration = new Set(["start_md_equilibration", "md_equilibration_status"])
const response = z.object({
  isError: z.boolean().optional(),
  structuredContent: z.object({
    workflow: z.object({ workflow_id: z.string() }).passthrough(),
    result: z.object({ run_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$/),
      job_state: z.string() }).passthrough(),
  }).passthrough(),
}).passthrough()

async function privateFile(filename: string) {
  const info = await lstat(filename)
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077))
    throw Error("Dispatch configuration must be an owner-only regular file")
  return readFile(filename, "utf8")
}

/** Installation-owned adapter, never model-supplied commands or roots.
 * The existing dispatcher rechecks the signed job and immutable release before
 * reserve/create. A persisted receipt always selects status, never submit again.
 * This does not grant approval, replace failed schedulers, or retry science. */
export async function dispatchCovalent(key: string, args: Record<string, unknown>, output: unknown) {
  const native = z.object({ isError: z.boolean().optional(), structuredContent: z.object({
    job: z.object({ run_id: z.string(), state: z.string(), route: z.literal("de-novo"),
      project_id: z.string(), engine: z.string() }).passthrough(),
    spec: z.object({ run_id: z.string(), request_id: z.string(), project_id: z.string(),
      route: z.literal("de-novo"), engine: z.enum(["schrodinger-covdock-vs", "schrodinger-covdock-lo", "de-novo-prereaction-glide"]),
      execution_mode: z.literal("execute"), human_approved: z.literal(true),
      provenance: z.object({ worker_backend: z.literal("external-instance") }).passthrough(),
    }).passthrough(),
  }).passthrough() }).passthrough().safeParse(output)
  const denovo = (key.endsWith("_covalent_submit_job") || key.endsWith("_covalent_get_job")) && native.success
  if (!denovo && (key.endsWith("_covalent_cancel_job") || key.endsWith("_covalent_get_job")))
    return dispatchCancellation(key, args, output)
  const libraryAction = key.endsWith("_covalent_library_research") && library.has(String(args.action))
  const filename = denovo ? process.env.COVALENT_DENOVO_DISPATCH_PROFILE
    : libraryAction ? process.env.COVALENT_LIBRARY_DISPATCH_PROFILE : process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE
  if (!filename || (!denovo && !libraryAction && !actions.has(String(args.action)) && !equilibration.has(String(args.action)) && !vs.has(String(args.action)))) return output
  const config = profile.parse(JSON.parse(await privateFile(filename)))
  if (key !== (denovo && key.endsWith("_covalent_get_job") ? config.tool.replace(/_covalent_submit_job$/, "_covalent_get_job") : config.tool)) return output
  const original = denovo && native.success ? native.data.structuredContent : undefined
  if (original) {
    const { job, spec } = original
    if (job.run_id !== spec.run_id || job.project_id !== spec.project_id || job.engine !== spec.engine
      || (key.endsWith("_covalent_get_job") ? args.run_id !== spec.run_id
        : args.request_id !== spec.request_id || args.project_id !== spec.project_id || args.route !== spec.route || args.engine !== spec.engine))
      throw Error("De novo dispatch differs from the registered job identity")
  }
  const parsed = response.safeParse(original && native.success ? { ...native.data, structuredContent: { ...original,
    workflow: { workflow_id: original.spec.project_id },
    result: { run_id: original.job.run_id, job_state: original.job.state } } } : output)
  if (!parsed.success || parsed.data.isError) return output
  const data = parsed.data.structuredContent
  if (!denovo && data.workflow.workflow_id !== args.workflow_id) throw Error("Dispatch workflow identity mismatch")
  const binding = JSON.parse(await privateFile(config.binding))
  if (binding.data_root !== config.root) throw Error("Dispatch data root differs from release binding")
  await mkdir(config.state, { recursive: true, mode: 0o700 })
  const folder = await lstat(config.state)
  if (!folder.isDirectory() || folder.isSymbolicLink() || (folder.mode & 0o077))
    throw Error("Dispatch receipts require a private directory")
  const receipt = path.join(config.state, data.result.run_id + ".json")
  const locked = receipt + ".lock"
  const claim = await open(locked, "wx", 0o600).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "EEXIST") return undefined
    throw error
  })
  const annotate = (state: string, error?: string) => {
    const dispatch = { run_id: data.result.run_id, state,
      ...(error ? { error, execution_confirmed: false } : {}),
      scientific_acceptance: false, instruction: error
        ? "Dispatch failed or is uncertain. QUEUED is the stored registration state, not evidence of CPU queueing or execution. Inspect this original run; never repeat start."
        : "Keep the registered run; never repeat start or scheduler creation." }
    // Routing, continuation and the workbench consume the original sealed receipt.
    const { branches, external_dispatch: _previousDispatch, ...current } = data
    const value = { external_dispatch: dispatch, ...(original ?? current),
      ...(branches ? { history: "Historical branches remain stored; use inspect_branch to read them." } : {}) }
    return { ...parsed.data, ...(error ? { isError: true } : {}), structuredContent: value,
      content: [{ type: "text", text: JSON.stringify(value) }] }
  }
  if (!claim) return annotate("dispatch_in_progress_or_reconciliation_required")
  try {
    const saved = await lstat(receipt).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return undefined
      throw error
    })
    if (saved && (!saved.isFile() || saved.isSymbolicLink())) throw Error("Invalid dispatch receipt")
    if (!saved && data.result.job_state !== "QUEUED") return output
    const source = path.resolve(import.meta.dir, "../../../../..")
    const dispatcher = denovo ? "dispatch_ligand_vs.py" : libraryAction ? "dispatch_library_vs.py" : vs.has(String(args.action)) ? "dispatch_ligand_vs.py"
      : ["start_covdock", "covdock_status"].includes(String(args.action)) && typeof data.result.execution_instance === "string" && ["asgbie3", "asgbie4"].includes(data.result.execution_instance) ? "dispatch_ligand_lo.py"
      : equilibration.has(String(args.action)) ? "dispatch_ligand_openmm.py" : "dispatch_ligand_smoke.py"
    const script = path.join(source, libraryAction ? "routes/library-first/ops" : "routes/ligand-first/ops", dispatcher)
    const child = Bun.spawn([config.python, script, saved ? "status" : "submit", "--run-id", data.result.run_id,
      "--state", receipt, "--root", config.root, "--binding", config.binding,
      ...(libraryAction && config.instance ? ["--instance", config.instance] : [])], {
      stdin: "ignore", stdout: "pipe", stderr: "pipe",
      env: { ...process.env, COVALENT_REMOTE_APPROVAL_KEY_FILE: config.approval_key,
        ...(denovo ? { COVALENT_LIGAND_VS_INSTANCE: "asgbie3" } : {}),
        ...(config.runtime ? { COVALENT_SCIENCE_RUNTIME_PROFILE: config.runtime } : {}),
        ...(config.recover_run ? { COVALENT_INSTANCE_RECOVER_RUN: config.recover_run } : {}),
        ...(config.recover_terminal ? { COVALENT_INSTANCE_RECOVER_TERMINAL: config.recover_terminal } : {}),
        PYTHONPATH: path.join(source, "science/src"), PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" },
    })
    const timer = setTimeout(() => child.kill("SIGTERM"), denovo ? 720_000 : 240_000)
    try {
      const [stdout, stderr, code] = await Promise.all([
        new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
      ])
      await writeFile(receipt + ".observation.json", JSON.stringify({ dispatcher, action: saved ? "status" : "submit", code, stdout, stderr, at: new Date().toISOString() }), { mode: 0o600 })
      if (code !== 0) return annotate("dispatch_failed_or_uncertain_inspect_receipt",
        stderr.trim().slice(-8000) || `Dispatcher exited with code ${code}; execution is not confirmed.`)
      const observed = JSON.parse(stdout)
      if (observed.error) return annotate(String(observed.scheduler_state), String(observed.error))
      if (observed.submitted === true)
        return annotate(["asgbie2", "asgbie3"].includes(observed.name) ? "instance_launch_requested" : "scheduler_submitted")
      return annotate(String(observed.scheduler_state ?? "observed"))
    } finally { clearTimeout(timer) }
  } finally {
    await claim.close()
    await unlink(locked)
  }
}

const cancellation = z.object({
  isError: z.boolean().optional(),
  structuredContent: z.object({
    cancellation_requested: z.literal(true),
    cancellation_confirmed: z.boolean(),
    job: z.object({ run_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$/),
      route: z.literal("ligand-first") }).passthrough(),
    scheduler_cancellation: z.object({ protocol: z.literal("reserved-hpc-cancellation-v1"),
      scope: z.object({ kind: z.literal("cancel"), run_id: z.string(),
        spec_sha256: z.string().regex(/^[a-f0-9]{64}$/),
        scheduler: z.object({ backend: z.literal("external-hpc"), name: z.string().min(1) }) }),
    }).passthrough(),
  }).passthrough(),
}).passthrough()

export function cancellationObservation(data: z.infer<typeof cancellation>["structuredContent"], observation: unknown) {
  const confirmed = z.object({ run_id: z.string(), name: z.string(), cancellation_confirmed: z.literal(true),
    scheduler_state: z.enum(["STOPPED", "CANCELLED"]),
    termination_evidence: z.object({ scope: cancellation.shape.structuredContent.shape.scheduler_cancellation.shape.scope,
      confirmed_at: z.string(), cancellation_confirmed: z.literal(true),
      worker_never_started: z.literal(true), vendor_execution: z.literal("not_started") }) }).safeParse(observation)
  const scope = data.scheduler_cancellation.scope
  if (!confirmed.success) return { ...data, scheduler_stop: {
    state: "scheduler_observed_vendor_confirmation_pending", observation, cancellation_confirmed: false } }
  const value = confirmed.data
  const proof = value.termination_evidence.scope
  if (value.run_id !== scope.run_id || value.name !== scope.scheduler.name
      || proof.run_id !== scope.run_id || proof.spec_sha256 !== scope.spec_sha256
      || proof.scheduler.name !== scope.scheduler.name)
    throw Error("Cancellation confirmation identity mismatch")
  return { ...data, cancellation_confirmed: true,
    job: { ...data.job, state: "CANCELLED", finished_at: value.termination_evidence.confirmed_at,
      failure_reason: "authorized_scheduler_stop_before_worker_start" },
    scheduler_cancellation: { ...data.scheduler_cancellation, state: "confirmed_before_worker_start" },
    scheduler_stop: { state: "confirmed_before_worker_start", observation, cancellation_confirmed: true } }
}

async function dispatchCancellation(key: string, args: Record<string, unknown>, output: unknown) {
  const filename = process.env.COVALENT_EXTERNAL_DISPATCH_PROFILE
  if (!filename) return output
  const config = profile.parse(JSON.parse(await privateFile(filename)))
  const observing = key.endsWith("_covalent_get_job")
  if (key !== config.tool.replace(/_covalent_ligand_design_step$/, observing ? "_covalent_get_job" : "_covalent_cancel_job")) return output
  const parsed = cancellation.safeParse(output)
  if (!parsed.success || parsed.data.isError) return output
  const data = parsed.data.structuredContent
  if (!observing && data.cancellation_confirmed) return output
  const scope = data.scheduler_cancellation.scope
  if (data.job.run_id !== args.run_id || scope.run_id !== args.run_id)
    throw Error("Cancellation run identity mismatch")
  const binding = JSON.parse(await privateFile(config.binding))
  if (binding.data_root !== config.root) throw Error("Cancellation data root differs from release binding")
  const folder = await lstat(config.state)
  if (!folder.isDirectory() || folder.isSymbolicLink() || (folder.mode & 0o077))
    throw Error("Cancellation receipts require a private directory")
  const receipt = path.join(config.state, data.job.run_id + ".json")
  const saved = JSON.parse(await privateFile(receipt))
  if (saved.run_id !== scope.run_id || saved.spec_sha256 !== scope.spec_sha256
      || saved.name !== scope.scheduler.name || saved.dispatch_root !== config.root)
    throw Error("Cancellation scope differs from original dispatch receipt")
  const locked = receipt + ".lock"
  const claim = await open(locked, "wx", 0o600).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "EEXIST") return undefined
    throw error
  })
  const annotate = (state: string, observation?: unknown) => {
    const value = { ...data, scheduler_stop: { state, observation, cancellation_confirmed: false,
      instruction: "Keep this run; request acknowledgement is not worker/vendor termination evidence." } }
    return { ...parsed.data, structuredContent: value, content: [{ type: "text", text: JSON.stringify(value) }] }
  }
  if (!claim) return annotate("observation_in_progress_or_reconciliation_required")
  try {
    const source = path.resolve(import.meta.dir, "../../../../..")
    const script = path.join(source, "routes/ligand-first/ops/cancel_ligand_hpc.py")
    const child = Bun.spawn([config.python, script, "--run-id", scope.run_id, "--state", receipt,
      "--root", config.root, "--binding", config.binding, ...(observing ? ["--observe-only"] : [])], {
      stdin: "ignore", stdout: "pipe", stderr: "pipe",
      env: { ...process.env, COVALENT_REMOTE_APPROVAL_KEY_FILE: config.approval_key,
        PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" },
    })
    const timer = setTimeout(() => child.kill("SIGTERM"), 240_000)
    try {
      const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(),
        new Response(child.stderr).text(), child.exited])
      await writeFile(receipt + ".stop-observation.json", JSON.stringify({ code, stdout, stderr,
        action: observing ? "observe" : "stop", at: new Date().toISOString() }), { mode: 0o600 })
      if (code !== 0) return annotate("stop_failed_or_uncertain_inspect_receipt")
      const value = cancellationObservation(data, JSON.parse(stdout))
      return { ...parsed.data, structuredContent: value, content: [{ type: "text", text: JSON.stringify(value) }] }
    } finally { clearTimeout(timer) }
  } finally {
    await claim.close()
    await unlink(locked)
  }
}
