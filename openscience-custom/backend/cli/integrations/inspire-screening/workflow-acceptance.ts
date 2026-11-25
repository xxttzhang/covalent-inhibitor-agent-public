// Real remote read-only acceptance: never call a model API or submit a job.
import path from "node:path"
import type { ToolContext } from "@synsci/plugin/tool"
import { execute } from "./workflow"

const context = { sessionID: "workflow-v2-acceptance", directory: path.resolve(import.meta.dir, "../../../../.."),
  abort: new AbortController().signal, metadata: () => {}, ask: async () => { throw new Error("Acceptance must not ask for compute") } } as unknown as ToolContext
const result = JSON.parse(await execute({ action: "draft" }, context))
if (result.source_library !== "virtual_screening" || result.submitted || result.reuse_legacy_pilot || result.status !== "DRAFT_NOT_EXECUTABLE") throw new Error("Workflow contract failed")
if (!result.inspection.source.exists || !result.inspection.target_registered) throw new Error("Expected source/target assets are unavailable")
const report = { tested_utc: new Date().toISOString(), draft_file: result.file, digest: result.digest,
  stages: result.stages.map((stage: { id: string }) => stage.id), inspection: result.inspection,
  missing: result.blockers, status: result.status, submitted: false, model_api_called: false }
const output = path.join(context.directory, "reports/inspire-library-workflow-acceptance.json")
await Bun.write(output, JSON.stringify(report, null, 2))
console.log(JSON.stringify({ ...report, report: output }))
