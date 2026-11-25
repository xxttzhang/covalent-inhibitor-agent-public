import type { ToolCallOptions } from "ai"
import type { RecordTools } from "./route-workflow"
import { covalentExecution } from "./covalent-approval"
import { dispatchCovalent } from "./covalent-dispatch"
import { requireDockingTool } from "./docking-policy"

/** Install before route projection: the workflow must bind identity/revision
 * before native approval compiles the exact execution scope. */
export function guardCovalentExecution(tools: RecordTools,
  approve: (key: string, args: Record<string, unknown>, options: ToolCallOptions) => Promise<Record<string, unknown>>) {
  return Object.fromEntries(Object.entries(tools).map(([key, item]) => {
    const execute = item.execute
    return [key, !execute ? item : { ...item, execute: async (args: unknown, options: ToolCallOptions) => {
      const parameters = args as Record<string, unknown>
      requireDockingTool(key, parameters)
      const output = await execute(covalentExecution(key, parameters) ? await approve(key, parameters, options) : args, options)
      return dispatchCovalent(key, parameters, output)
    } }]
  }))
}
