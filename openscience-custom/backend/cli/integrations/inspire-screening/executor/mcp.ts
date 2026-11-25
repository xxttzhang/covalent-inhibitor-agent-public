import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js"
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { CallToolResultSchema, type CallToolResult, type ServerRequest, type ServerNotification } from "@modelcontextprotocol/sdk/types.js"
import type { RequestHandlerExtra } from "@modelcontextprotocol/sdk/shared/protocol.js"
import type { PluginInput } from "@synsci/plugin"
import type { ToolContext } from "@synsci/plugin/tool"
import z from "zod"
import { execute, schema } from "./tool"
import { bindRequest } from "./request"
import { registeredTarget } from "./target"
import { workflowLifecycle, workflowReference, readWorkflowExecution, type WorkflowReference } from "./workflow-execution"
import { open } from "./host"

/** The host supplies authority out of band. Tool arguments never carry a
 * session identity, approval callback, or model-authored original request. */
export const contract = {
  name: "inspire_screening_task",
  description: "Persistent experimental Library-first EXECUTOR via realZillionX/InspireSkill. For create, collect target_parameters (target, receptor, chain, residue, residue_number, insertion_code, numbering, reaction_type), virtual library, screening_mode (economy/standard/deep), and max_points. New targets are persisted but return TARGET_CONFIGURATION_REQUIRED until an adapter is verified; never substitute EGFR. Never ask the user to choose raw record caps, shortlist size, CPU/GPU, quotas, or stage minutes. The host binds the actual user request, persists an intake, reads live Inspire quota prices, and applies a versioned resource policy. If the requested mode exceeds budget, a native question explicitly asks whether to accept a lower mode or raise the cap; there is no silent downgrade. It then freezes the exact scope/resources/cost and AUTOMATICALLY presents the native resource approval card in the same call. Clarifying answers never approve compute. No compute is submitted before native approval. approve only reopens approval for an existing unapproved draft; it is NOT consent. A durable daemon then runs preparation → CarsiDock → RTMScore → report. status/list/wake recover without resubmitting. Currently audited target EGFR C797 with disclosed 1M17 mapping; no legacy pilot inputs. AF3/CovDock execution and delivery are implemented and verified for the recorded EGFR selected-eight batch; generalization to new targets is not validated. Do not create duplicate tasks for the same original request or claim real end-to-end validation from software tests.",
  schema,
} as const

export const capability = {
  name: "inspire_screening_capabilities",
  description: "Read the Library-first executor's implemented capabilities and outstanding preparation boundaries. No network, task creation, resource allocation, or claim of current engine readiness.",
  schema: z.object({}).strict(),
} as const

export function capabilities() {
  return {
    schema: "library-first-capabilities-v1",
    executor: contract.name,
    tools: [contract.name, capability.name],
    transport: "host-bound-mcp",
    runtime_verified: false,
    compute_submitted: false,
    core: { stages: ["prepare", "carsidock", "rtmscore", "report"],
      registered_targets: [registeredTarget], automatic_after_scope_approval: true },
    refinement: { implemented: true, requires_verified_core_artifacts: true },
    preparation: {
      target_name_only: false,
      arbitrary_receptor: false,
      generic_covalent_md: false,
      registered_receptor_repair: {
        backend: "pdbfixer-openmm",
        pdbfixer_version: "1.12.0",
        openmm_version_verified_during_plan: true,
        prime: { implemented: false, status: "capability_not_implemented" },
        multiple_backends_in_one_lineage_allowed: false,
      },
    },
    md: { generic_route_connected: false, historical_fixed_batch_is_not_generic: true },
    result_policy: "Task and artifact identities come from executor results; do not invent paths or successful states.",
  }
}

/** A real MCP server using the existing executor and its durable state. The
 * transport may change; host authority must never come from MCP arguments. */
export function server(host: PluginInput["client"], context: ToolContext, workflow?: WorkflowReference) {
  const reference = workflow ? workflowReference.parse(workflow) : undefined
  const mcp = new McpServer({ name: "library-first", version: "1.0.0" })
  mcp.registerTool(capability.name, { description: capability.description, inputSchema: capability.schema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true } }, async () => {
    context.abort.throwIfAborted()
    const result = capabilities()
    return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result }
  })
  // The original request is bound before execute() applies cross-field checks,
  // matching the existing native entry. The model need not repeat user text.
  mcp.registerTool(contract.name, { description: reference
      ? "Lifecycle operations on the workflow's exact frozen screening task. Task identity is host-bound. Approval remains a separate native decision."
      : contract.description,
    inputSchema: reference ? workflowLifecycle : z.object(contract.schema.shape).strict(),
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false } },
    async (input: Record<string, unknown>, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<CallToolResult> => {
    context.abort.throwIfAborted()
    const bound = { ...context, abort: AbortSignal.any([context.abort, extra.signal]) }
    const source = input.action === "create" || input.action === "plan_refinement"
      ? await bindRequest(host, bound) : undefined
    bound.abort.throwIfAborted()
    const args = input.action === "create" ? { ...input, request: source!.text } : input
    const text = await execute(args, bound, source, reference)
    const result: unknown = JSON.parse(text)
    const receipt = reference ? await (async () => {
      const store = await open()
      try { return readWorkflowExecution(store, reference, bound) } finally { store.close() }
    })() : undefined
    // Preserve the existing native text contract, including list's array,
    // while exposing a consistent object to MCP clients.
    return { content: [{ type: "text", text }], structuredContent: { result,
      ...(receipt ? { workflow_execution: receipt } : {}) } }
  })
  return mcp
}

/** In-process MCP keeps native permission/metadata callbacks in the host.
 * Compute is persisted by the executor and does not depend on this connection. */
export async function invoke(host: PluginInput["client"], context: ToolContext, name: string, args: Record<string, unknown>,
  workflow?: WorkflowReference) {
  context.abort.throwIfAborted()
  const mcp = server(host, context, workflow)
  const client = new Client({ name: "openscience-library-first", version: "1.0.0" })
  const [caller, callee] = InMemoryTransport.createLinkedPair()
  try {
    await mcp.connect(callee)
    await client.connect(caller)
    const result = CallToolResultSchema.parse(await client.callTool({ name, arguments: args }, undefined,
      { signal: context.abort, timeout: 24 * 60 * 60 * 1000 }))
    // Native permission can wait for the user. Abort is propagated; this
    // transport timeout is not permission to run an engine for 24 hours.
    const text = result.content.filter(block => block.type === "text").map(block => block.text).join("\n")
    if (result.isError) throw new Error(text)
    // Existing plugin callers retain their text contract. The workflow host
    // needs the ledger receipt as well as the executor's route-specific result.
    return workflow ? JSON.stringify(result.structuredContent) : text
  } finally {
    await client.close()
    await mcp.close()
  }
}
