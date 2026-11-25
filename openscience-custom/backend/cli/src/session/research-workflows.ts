import { dockingRoutingSummary } from "./docking-policy"
import { Storage } from "../storage/storage"
import { jsonSchema, type JSONSchema7, type ToolCallOptions } from "ai"
import { Lock } from "../util/lock"
import { ligandIntent, ligandWorkflowTools } from "./ligand-workflow"
import { libraryEntry, libraryIntent, libraryStorage, libraryWorkflowTools, type ScreeningBridge } from "./library-workflow"
import { denovoIntent } from "./denovo-entry"
import { basename, type RecordTools } from "./route-workflow"
import { boundSessionStudy } from "../../integrations/discovery-platform/study-session"
import { deNovoSubmissionTool, deNovoCancellationTool } from "./denovo-execution"
import { deNovoBatchSubmissionTool } from "./denovo-batch-execution"
import { ligandCancellationTools } from "./ligand-cancellation"
import { readLigandContinuation, pauseLigandContinuation, resumeLigandContinuation } from "./ligand-continuation"

type Scope = { project: string; session: string }
const routes = [
  { route: "ligand-first", key: "ligand-workflow-bindings", entry: "covalent_ligand_workflow",
    intent: ligandIntent, resolve: ligandWorkflowTools },
  { route: "library-first", key: libraryStorage, entry: libraryEntry,
    intent: libraryIntent,
    resolve: libraryWorkflowTools },
] as const

async function activeRoutes(scope: Scope) {
  const saved = await Promise.all(routes.map(async route => {
    const binding = await Storage.read<{ active?: boolean }>([route.key, scope.project, scope.session]).catch(error => {
      if (error instanceof Storage.NotFoundError) return undefined
      throw error
    })
    return binding && binding.active !== false ? route : undefined
  }))
  return saved.filter(route => route !== undefined)
}

/** Session routing only. Scientific stages and allowable actions stay in the
 * backend. Suspend is explicit; mentioning a new route never discards a study. */
export async function researchWorkflowTools(discovered: RecordTools, scope: Scope & { text: string; screening?: ScreeningBridge;
  denovo?: { directory: string } }) {
  const active = await activeRoutes(scope)
  if (active.length > 1) throw Error("Multiple active research routes require binding reconciliation; no ambiguous execution")
  const intended = routes.find(route => route.intent(scope.text)
    && Object.keys(discovered).some(key => basename(key) === route.entry))
  const saved = intended ? await Storage.read<{ active?: boolean }>([intended.key, scope.project, scope.session]).catch(error => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  }) : undefined
  const selected = active.at(0) ?? (saved?.active === false ? undefined : intended)
  const study = !selected && scope.denovo ? await boundSessionStudy({ ...scope, directory: scope.denovo.directory }) : undefined
  const denovo = !selected && !!scope.denovo && (study
    ? study.routes["de-novo"].selected && study.routes["de-novo"].mode === "new"
      && !study.routes["library-first"].selected && !study.routes["ligand-first"].selected
    : denovoIntent(scope.text))
  // Project each unbound entry exactly once. Re-projecting an already wrapped
  // entry would bind request identity twice; raw foreign stage tools must also
  // never leak through a suspended route's discovery view.
  const projected = active[0] ? await active[0].resolve(discovered, { ...scope, intent: true }) :
    await libraryWorkflowTools(await ligandWorkflowTools(discovered, { ...scope, intent: false }), { ...scope, intent: false })
  const modern = routes.every(route => Object.keys(discovered).some(key => basename(key) === route.entry))
  const output: RecordTools = {}
  for (const [key, tool] of Object.entries(projected)) {
    const base = basename(key)
    // Retain explicit route navigation and the scientific hypothesis service.
    // Raw jobs/design aliases cannot compete with the native frozen-study path.
    if ((denovo || (!selected && modern)) && key.includes("covalent_") && ![
      "covalent_ligand_workflow", "covalent_library_workflow", "covalent_target_hypothesis", "covalent_capabilities",
    ].includes(base)) continue
    if (selected && !active.length && key.includes("covalent_") && base !== selected.entry) continue
    const route = routes.find(route => base === route.entry ||
      (route.route === "ligand-first" ? ["covalent_ligand_evidence", "covalent_ligand_design_step"] :
        ["covalent_library_research", "covalent_library_receptor"]).includes(base))
    if (!key.includes("covalent_") || !tool.execute) { output[key] = tool; continue }
    const execute = tool.execute
    output[key] = { ...tool, execute: async (args, options) => {
      using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
      const current = await activeRoutes(scope)
      if (current.some(value => value.route !== route?.route))
        throw Error("Another research route is active; suspend it before switching")
      // Await the complete dispatch and durable binding before disposing the
      // route lock; returning the promise would release it before completion.
      return await execute(args, options)
    } }
  }
  if (selected?.route === "ligand-first" && active.length === 1)
    Object.assign(output, await ligandCancellationTools(discovered, scope, async () => {
      const current = await activeRoutes(scope)
      return current.length === 1 && current[0].route === "ligand-first"
    }))
  if (selected?.route === "ligand-first" && active.length === 1 && await readLigandContinuation(scope))
    output.ligand_continuation = {
      description: "Read, pause or resume automatic follow-up of the existing submitted Ligand task. Never submits compute. Pausing follow-up does not cancel the remote job. Resume only when the user wants the original task followed to its report.",
      inputSchema: jsonSchema({ type: "object", properties: { action: { type: "string", enum: ["status", "pause", "resume"] } },
        required: ["action"], additionalProperties: false }),
      execute: async args => {
        const value = args as { action?: string }
        if (!value || Object.keys(value).length !== 1 || !["status", "pause", "resume"].includes(value.action ?? ""))
          throw Error("Expected continuation status, pause or resume")
        const routes = await activeRoutes(scope)
        if (routes.length !== 1 || routes[0].route !== "ligand-first") throw Error("Ligand workflow no longer active")
        const saved = await readLigandContinuation(scope)
        const binding = await Storage.read<{ entry: string; workflow_id: string }>(["ligand-workflow-bindings", scope.project, scope.session])
        if (!saved || saved.entry !== binding.entry || saved.workflow_id !== binding.workflow_id)
          throw Error("Continuation belongs to a different workflow")
        if (value.action === "pause") await pauseLigandContinuation(scope)
        if (value.action === "resume") await resumeLigandContinuation(scope)
        const result = { continuation: await readLigandContinuation(scope), compute_submitted: false }
        return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result }
      },
    }
  if (denovo && study && scope.denovo) {
    const batch = deNovoBatchSubmissionTool(discovered, { ...scope, directory: scope.denovo.directory }, study.id,
      async () => (await activeRoutes(scope)).length === 0)
    if (batch) output.denovo_submit_batch_vs = batch
    const batchLO = deNovoBatchSubmissionTool(discovered, { ...scope, directory: scope.denovo.directory }, study.id,
      async () => (await activeRoutes(scope)).length === 0, "lo")
    if (batchLO) output.denovo_submit_batch_lo = batchLO
    const glide = deNovoBatchSubmissionTool(discovered, { ...scope, directory: scope.denovo.directory }, study.id,
      async () => (await activeRoutes(scope)).length === 0, "glide")
    if (glide) output.denovo_submit_batch_prereaction = glide
    const submit = deNovoSubmissionTool(discovered, { ...scope, directory: scope.denovo.directory }, study.id,
      async () => (await activeRoutes(scope)).length === 0)
    if (submit) output.denovo_submit_evaluation = submit
    const cancel = deNovoCancellationTool(discovered, { ...scope, directory: scope.denovo.directory }, study.id,
      async () => (await activeRoutes(scope)).length === 0)
    if (cancel) output.denovo_cancel_evaluation = cancel
  }
  return { tools: output, activeRoute: selected?.route ?? (denovo ? "de-novo" : undefined) }
}

/** Native execution tools are used internally through bound adapters while a
 * workflow is selected; they cannot become a second model-visible entry. */
const nativeExecution = new Set(["inspire_screening_task", "inspire_screening_capabilities", "denovo_generate",
  "molecular_dynamics", "inspire_library_workflow", "inspire_screening_smoke", "inspire_screening"])
const studyEntries = new Set(["discovery_study", "discovery_research"])

export const researchRoutingInstructions = dockingRoutingSummary + `
Route ownership and workflow discipline:
- Use only the active route workflow entry and its stage-permitted actions, bound identity, and revision. Never bypass a missing capability or user-requested stopping stage by calling raw execution tools or creating a duplicate study.
- Library-first owns library/receptor screening; Ligand-first owns parent evidence and constrained candidate design; De novo uses discovery_study and denovo_generate with a frozen scientific plan. Do not create discovery_study/discovery_research records for Library-first or Ligand-first.
- If the request does not clearly identify a route (existing lead to covalentize → Ligand-first; screen a compound library → Library-first; new scaffolds → De novo), ask one short clarifying question before starting any workflow. Never pick a route for the user, and never start a workflow from a question about a route.
- Use explicit suspend/resume to switch between active Library/Ligand routes. Never invent unresolved target, residue, receptor, parent, warhead, library, queue position, or citation IDs.

Async execution and evidence boundaries:
- Never use sleep or a busy loop to wait on a long-running scientific task. Query the workflow-permitted status tool once (or check ligand_continuation: waiting/ready confirms background monitoring) and end the turn while waiting.
- A system task notification is an observation, not new user approval: refresh the bound task and continue permitted evaluation/reporting without duplicating compute submissions. If a connector read fails, report state unknown and observe the original run without resubmitting.
- Distinguish local registration/QUEUED from confirmed remote GPU running, and distinguish execution success or structural QC pass from experimental activity, kinetics, or selectivity. Treat missing/null scores as unmeasured, never zero.`

const ligandHostTools = new Set(["bash", "read", "grep", "glob", "list", "edit", "write", "apply_patch", "task"])

export function projectResearchNativeTools(tools: RecordTools, activeRoute?: string) {
  if (activeRoute === "de-novo")
    return projectResearchNativeTools(Object.fromEntries(Object.entries(tools)
      .filter(([name]) => name === "denovo_generate" || !nativeExecution.has(name))))
  if (activeRoute)
    return Object.fromEntries(Object.entries(tools).filter(([name]) => !nativeExecution.has(name) && !studyEntries.has(name)
      && (activeRoute !== "ligand-first" || !ligandHostTools.has(name))))
  const tool = tools.denovo_generate
  const raw = tool?.inputSchema as { jsonSchema?: JSONSchema7 } | undefined
  if (!raw?.jsonSchema) return tools
  const schema = structuredClone(raw.jsonSchema)
  // Keep the compatibility dispatcher, but stop advertising a dead-end create
  // action. Frozen study execution and historical status/results stay intact.
  const remove = (branches: JSONSchema7["anyOf"]) => branches?.filter(branch => {
    if (typeof branch === "boolean") return true
    const action = branch.properties?.action
    return typeof action !== "object" || (action.const !== "create"
      && !(action.enum?.length === 1 && action.enum[0] === "create"))
  })
  if (schema.anyOf) schema.anyOf = remove(schema.anyOf)
  if (schema.oneOf) schema.oneOf = remove(schema.oneOf)
  if (JSON.stringify(schema) === JSON.stringify(raw.jsonSchema)) return tools
  return { ...tools, denovo_generate: { ...tool, inputSchema: jsonSchema(schema),
    description: tool.description?.replace("Legacy create returns NEEDS_SCIENTIFIC_DECISION without creating a task. ", "") } }
}

/** A tool offered before workflow activation may still be queued by the model.
 * Recheck the durable route under the same lock immediately before dispatch. */
export function guardResearchNativeTools(tools: RecordTools, scope: Scope): RecordTools {
  return Object.fromEntries(Object.entries(tools).map(([name, tool]) => {
    const execute = tool.execute
    const host = ligandHostTools.has(name)
    if ((!host && !nativeExecution.has(name) && !studyEntries.has(name)) || !execute) return [name, tool]
    return [name, { ...tool, execute: async (args: unknown, options: ToolCallOptions) => {
      using _ = await Lock.write(["research-route", scope.project, scope.session].join("/"))
      if ((await activeRoutes(scope)).some(route => !host || route.route === "ligand-first"))
        throw Error("A research workflow is active; use its bound execution tools")
      return await execute(args, options)
    } }]
  }))
}
