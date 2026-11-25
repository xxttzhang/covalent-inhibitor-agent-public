import { createWorkflowProjection, declaredInterface, matchRouteIntent, workflowInterface, basename, type RecordTools } from "./route-workflow"
import { Storage } from "../storage/storage"
import type { ToolCallOptions } from "ai"
import { workflowHostActions } from "../../integrations/inspire-screening/executor/workflow-host"

export const libraryStorage = "library-workflow-bindings"
export const libraryEntry = "covalent_library_workflow"

export function libraryIntent(text: string) {
  return matchRouteIntent(text) === "library-first"
}
export type ScreeningBridge = (args: Record<string, unknown>, options: ToolCallOptions,
  execute: (args: Record<string, unknown>) => Promise<unknown>) => Promise<unknown>

/** This route has no pre-contract workflow server. A present entry MUST carry
 * the backend declaration; legacy raw library APIs remain compatible if absent. */
export async function libraryWorkflowTools(tools: RecordTools, scope: { project: string; session: string; intent: boolean; screening?: ScreeningBridge }) {
  const saved = await Storage.read<{ entry: string }>([libraryStorage, scope.project, scope.session]).catch(error => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  })
  const entries = Object.keys(tools).filter(key => basename(key) === libraryEntry)
  const selected = saved?.entry ?? entries[0]
  if (!selected && !saved) return tools
  const tool = tools[selected!]
  if (!tool) throw Error("Bound Library-first workflow server is unavailable")
  const schema = tool.inputSchema as { jsonSchema?: { "x-research-workflow"?: unknown } }
  const profile = workflowInterface.parse(schema.jsonSchema?.["x-research-workflow"])
  if (profile.route !== "library-first" || profile.entry !== libraryEntry) throw Error("Library workflow declaration changed route or entry")
  const adapted: RecordTools = Object.fromEntries(Object.entries(tools).map(([name, tool]) => {
    const execute = tool.execute
    if (basename(name) !== "covalent_library_research" || !execute) return [name, tool]
    return [name, { ...tool, execute: async (value: unknown, options: ToolCallOptions) => {
      const args = value as Record<string, unknown>
      if (!workflowHostActions.some(action => action === args?.action)) return execute(value, options)
      if (!scope.screening) throw Error("Authenticated screening host adapter is unavailable; no task created")
      return scope.screening(args, options, async parameters => await execute(parameters, options))
    } }]
  }))
  return createWorkflowProjection(declaredInterface(adapted, profile, saved?.entry),
    { key: libraryStorage, requestSuffix: "library" }).resolveTools(adapted, scope)
}
