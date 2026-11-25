import { jsonSchema, type Tool, type JSONSchema7 } from "ai"
import z from "zod"
import { Storage } from "../storage/storage"
import { Lock } from "../util/lock"
import { publishParentDelivery } from "./parent-delivery"
import { registerLigandContinuation } from "./ligand-continuation-runtime"
import { Log } from "../util/log"

export type RecordTools = Record<string, Tool>
const toolname = z.string().regex(/^covalent_[a-z0-9_]+$/)
export const workflowInterface = z.object({
  schema: z.literal("research-workflow-interface-v1"),
  route: z.enum(["ligand-first", "library-first", "de-novo"]),
  protocol: z.string().min(1), id_pattern: z.string().min(1),
  entry: toolname, tools: z.array(toolname).min(1),
  start_fields: z.array(z.string()).min(1),
  legacy_names: z.array(toolname), legacy_prefixes: z.array(toolname),
  refreshable: z.record(toolname, z.array(z.string())),
}).strict().superRefine((value, ctx) => {
  if (!value.tools.includes(value.entry) || new Set(value.tools).size !== value.tools.length
      || Object.keys(value.refreshable).some((name) => !value.tools.includes(name))
      || !value.start_fields.includes("action"))
    ctx.addIssue({ code: "custom", message: "Invalid workflow interface tool membership" })
})
export type WorkflowInterface = z.infer<typeof workflowInterface>
export const basename = (key: string) => key.slice(key.lastIndexOf("covalent_"))
const prefix = (key: string) => key.slice(0, key.lastIndexOf("covalent_"))

/** Backend declaration is authoritative; fallback is only for pre-contract servers. */
export function declaredInterface(tools: RecordTools, fallback: WorkflowInterface, selected?: string) {
  const entries = Object.entries(tools).filter(([key]) => selected ? key === selected : basename(key) === fallback.entry)
  const profiles = entries.map(([, tool]) => {
    const schema = tool.inputSchema as { jsonSchema?: Record<string, unknown> }
    const raw = schema.jsonSchema?.["x-research-workflow"]
    const value = raw === undefined ? fallback : workflowInterface.parse(raw)
    if (value.entry !== fallback.entry || value.route !== fallback.route) throw Error("Workflow interface changed route or entry")
    return value
  })
  if (profiles.some((value) => JSON.stringify(value) !== JSON.stringify(profiles[0])))
    throw Error("Configured workflow servers advertise inconsistent interface contracts")
  return profiles[0] ?? fallback
}

/** No route phases live here. Backend views determine permitted tools and actions. */
export function createWorkflowProjection(input: WorkflowInterface,
  storage: { key: string; requestSuffix: string }) {
  const profile = workflowInterface.parse(input)
  const entry = profile.entry
  const names = new Set(profile.tools)
  const legacy = new Set(profile.legacy_names)
  const label = profile.route[0].toUpperCase() + profile.route.slice(1)
  const bindingSchema = z.object({ entry: z.string(), workflow_id: z.string().regex(new RegExp(profile.id_pattern)),
    active: z.boolean().optional() }).strict()
  const viewSchema = z.object({ protocol: z.literal(profile.protocol),
    workflow_id: z.string().regex(new RegExp(profile.id_pattern)), revision: z.number().int().positive(),
    stage: z.string(), tools: z.record(z.string(), z.object({
      actions: z.array(z.string()).min(1), fields: z.array(z.string()),
      field_constraints: z.record(z.string(), z.record(z.string(), z.unknown())).optional(),
    }).strict().refine(value => Object.keys(value.field_constraints ?? {}).every(name => value.fields.includes(name)
      && !["action", "workflow_id", "expected_revision", "request_id"].includes(name)))).refine((value) => Object.keys(value).every((key) => names.has(key))),
  }).strict()
  type View = z.infer<typeof viewSchema>
  type Binding = z.infer<typeof bindingSchema>

  function workflowView(result: unknown): View | undefined {
    if (!result || typeof result !== "object") return
    const value = result as { isError?: boolean; structuredContent?: unknown; content?: { type: string; text?: string }[] }
    if (value.isError) return
    const structured = value.structuredContent ?? (() => {
      const text = value.content?.find((part) => part.type === "text")?.text
      if (!text) return
      try { return JSON.parse(text) as unknown } catch { return }
    })()
    if (!structured || typeof structured !== "object" || !("workflow" in structured)) return
    if ("route" in structured && structured.route !== profile.route) return
    const parsed = viewSchema.safeParse(structured.workflow)
    return parsed.success ? parsed.data : undefined
  }

  function stageDescription(raw: string | undefined, actions: string[], bound: boolean) {
    const text = (raw ?? "").trim()
    if (text.length <= 800) return text
    const sentences = text.split(/(?<=\.)\s+/).filter(Boolean)
    const head = sentences.slice(0, 2)
    if (!bound) return head.join(" ")
    const selected = [...head]
    const seen = new Set(head)
    for (const sentence of sentences.slice(2)) {
      if (actions.some((action) => new RegExp(`\\b${action}\\b`).test(sentence)) && !seen.has(sentence)) {
        seen.add(sentence)
        selected.push(sentence)
      }
      if (selected.join(" ").length >= 1400) break
    }
    return selected.join(" ")
  }

  function project(tool: Tool, actions: string[], fields: string[], bound: boolean,
    constraints: Record<string, Record<string, unknown>> = {}) {
    const raw = tool.inputSchema as { jsonSchema?: JSONSchema7 }
    if (!raw.jsonSchema || raw.jsonSchema.type !== "object") throw Error("Workflow tool lacks an object schema")
    const schema = structuredClone(raw.jsonSchema)
    const allowed = new Set(fields)
    if (bound) { allowed.delete("workflow_id"); allowed.delete("expected_revision") }
    else allowed.delete("request_id") // Stable request identity is host/session owned.
    schema.properties = Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([name]) => allowed.has(name)))
    for (const [name, constraint] of Object.entries(constraints)) {
      if (!allowed.has(name) || !(name in schema.properties)
          || ["action", "workflow_id", "expected_revision", "request_id"].includes(name))
        throw Error("Stage constraint references an unavailable or host-owned field")
      // Intersection cannot loosen the server's published input contract.
      schema.properties[name] = { allOf: [schema.properties[name]!, constraint as JSONSchema7] }
    }
    schema.properties.action = { type: "string", enum: actions }
    schema.required = (schema.required ?? []).filter((name) => allowed.has(name))
    schema.additionalProperties = false
    delete (schema as JSONSchema7 & { "x-research-workflow"?: unknown })["x-research-workflow"]
    if (!JSON.stringify(schema.properties).includes('"$ref"')) delete (schema as JSONSchema7 & { $defs?: unknown }).$defs
    const summary = stageDescription(tool.description, actions, bound)
    return { ...tool, inputSchema: jsonSchema(schema),
      description: `${summary}\nCurrent permitted actions: ${actions.join(", ")}. Required model-supplied fields: ${(schema.required ?? []).join(", ") || "none"}. Other advertised fields are optional unless the chosen action explicitly requires them. ${bound ? "Workflow identity and revision are bound by the host; do not supply them." : "Start through this entry; request identity is bound by the host."}` }
  }

  /** Pure projection used by the real session resolver and integration tests. */
  function projectTools(tools: RecordTools, selected?: string, view?: View): RecordTools {
    const entries = Object.keys(tools).filter((key) => basename(key) === entry)
    if (!entries.length && !selected) return tools // Older servers remain compatible.
    const output: RecordTools = {}
    for (const [key, tool] of Object.entries(tools)) {
      if (!key.includes("covalent_")) { output[key] = tool; continue }
      const name = basename(key)
      if (selected) {
        if (prefix(key) !== prefix(selected)) continue
        if (!view) {
          if (name === entry) output[key] = project(tool, ["start", "status"],
            profile.start_fields, false)
          continue
        }
        const permitted = view.tools[name as keyof View["tools"]]
        if (permitted && names.has(name)) output[key] = project(tool, permitted.actions, permitted.fields, true, permitted.field_constraints)
        continue
      }
      // A separately configured old connector must not become a second research
      // entry merely because its server prefix differs from the upgraded server.
      if (profile.legacy_prefixes.some((value) => name.startsWith(value)) || legacy.has(name)
          || (names.has(name) && name !== entry)) continue
      if (name === entry && entries.length > 1) continue
      if (name === entry) output[key] = project(tool, ["start", "status"],
        profile.start_fields, false)
      else output[key] = tool
    }
    return output
  }

  function bindArguments(name: string, input: unknown, session: string, view?: View) {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw Error("Expected workflow arguments")
    const args = input as Record<string, unknown>
    if (typeof args.action !== "string") throw Error("Workflow action is required")
    if (view) {
      const allowed = view.tools[name as keyof View["tools"]]
      if (!allowed || !allowed.actions.includes(args.action)) throw Error("Action unavailable at the current workflow stage")
      if (Object.keys(args).some((key) => !allowed.fields.includes(key) || ["workflow_id", "expected_revision"].includes(key)))
        throw Error("Workflow identity/revision is host bound; unexpected arguments")
      return { ...args, workflow_id: view.workflow_id,
        ...(name === entry && args.action === "status" ? {} : { expected_revision: view.revision }) }
    }
    if (name !== entry || !["start", "status"].includes(args.action)) throw Error("Start or resume through the workflow entry")
    if ("request_id" in args || "expected_revision" in args) throw Error("Request identity is host bound")
    return args.action === "start" ? { ...args, request_id: session + "-" + storage.requestSuffix } : args
  }

  /** Persist only a successful response from one of the registered scoped tools.
   * Status is refreshed each model step, so reconnect/compaction cannot restore
   * stale visibility. A failed refresh never falls back to the full tool set.
   */
  async function resolveTools(tools: RecordTools, scope: { project: string; session: string; intent: boolean }) {
    const key = [storage.key, scope.project, scope.session]
    const saved = await Storage.read<Binding>(key).catch((error) => {
      if (error instanceof Storage.NotFoundError) return undefined
      throw error
    })
    const binding = saved ? bindingSchema.parse(saved) : undefined
    const entries = Object.keys(tools).filter((name) => basename(name) === entry)
    if (!binding && scope.intent && entries.length > 1)
      throw Error(`Multiple ${label} workflow servers are configured; enable exactly one primary workflow server for this project`)
    const active = binding && binding.active !== false
    const selected = binding ? (active ? binding.entry : undefined) : (scope.intent && entries.length === 1 ? entries[0] : undefined)
    const current = active ? await (async () => {
      const execute = tools[binding.entry]?.execute
      if (!execute) throw Error(`Bound ${label} workflow server is unavailable; reconnect it before continuing`)
      const result = await execute({ action: "status", workflow_id: binding.workflow_id },
        { toolCallId: profile.route + "-workflow-visibility", messages: [] })
      const view = workflowView(result)
      if (!view || view.workflow_id !== binding.workflow_id) throw Error("Cannot verify workflow state; tools remain closed")
      return view
    })() : undefined
    const projected = projectTools(tools, selected, current)
    if (binding && !active && tools[binding.entry]) {
      projected[binding.entry] = project(tools[binding.entry], ["resume", "status"], ["action"], true)
      projected[binding.entry].description += " This session suspended the workflow. resume restores it; status only inspects it. Other routes remain available until resume."
    }
    for (const [name, item] of Object.entries(projected)) {
      const execute = item.execute
      if (!execute) continue
      const base = basename(name)
      if (!names.has(base)) {
        if (base === "covalent_target_hypothesis" && entries.length > 0)
          projected[name] = { ...item, execute: async (args, options) => {
            const value = args as { hypothesis?: { route?: string } }
            if (value.hypothesis?.route === profile.route) throw Error(`Use ${entry} for ${profile.route} research decisions`)
            return execute(args, options)
          } }
        continue
      }
      projected[name] = { ...item, execute: async (args, options) => {
        using _ = await Lock.write(key.join("/"))
        if (options.abortSignal?.aborted) throw Error("Workflow call cancelled before dispatch")
        const latest = await Storage.read<Binding>(key).catch(error => {
          if (error instanceof Storage.NotFoundError) return undefined
          throw error
        })
        if (latest?.entry !== binding?.entry || latest?.workflow_id !== binding?.workflow_id
            || (latest && latest.active !== false) !== (binding && binding.active !== false))
          throw Error("Workflow session binding changed; refresh tools before continuing")
        const input = args as Record<string, unknown>
        // The model may emit several additive/observational calls in one step.
        // Queue them and refresh only those operations. Scientific finalization
        // and design must keep the revision on which the model formed its input.
        const observed = current && profile.refreshable[base]?.includes(String(input?.action)) ? await (async () => {
          bindArguments(base, args, scope.session, current) // Must have been offered in the original view.
          const status = await tools[binding!.entry].execute!({ action: "status", workflow_id: current.workflow_id }, options)
          const updated = workflowView(status)
          if (!updated || updated.workflow_id !== current.workflow_id) throw Error("Cannot verify workflow state before queued evidence call")
          return updated
        })() : current
        const parameters = (() => {
          if (!binding || active || name !== binding.entry) return bindArguments(base, args, scope.session, observed)
          if (!input || Object.keys(input).length !== 1 || !["resume", "status"].includes(String(input.action)))
            throw Error("Suspended workflow accepts only resume/status; identity is host bound")
          return { action: input.action, workflow_id: binding.workflow_id }
        })()
        const result = await execute(parameters, options)
        const next = workflowView(result)
        if (next) {
          if (binding && next.workflow_id !== binding.workflow_id) throw Error("Workflow response changed bound identity")
          await Storage.write(key, { entry: prefix(name) + entry, workflow_id: next.workflow_id,
            active: input.action === "suspend" ? false : input.action === "resume" ? true : binding?.active ?? true })
          if (profile.route === "ligand-first")
            await registerLigandContinuation(scope, prefix(name) + entry, String(input.action), result)
              .catch(error => Log.Default.error("could not register task continuation", { session: scope.session, error }))
        }
        if (profile.route === "ligand-first" && base === "covalent_ligand_evidence"
            && ["export_parent_report", "export_design_report", "export_covdock_report"].includes(String(input.action)) && current)
          return publishParentDelivery(result, scope, current.workflow_id)
        return result
      } }
    }
    return projected
  }

  return { workflowView, projectTools, bindArguments, resolveTools }
}

export { matchRouteIntent } from "./route-intent"
