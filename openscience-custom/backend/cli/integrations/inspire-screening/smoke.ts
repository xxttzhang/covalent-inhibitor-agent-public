// Live no-dispatch acceptance. This script cannot approve the native start gate.
import { execute } from "./core"
import { mkdir } from "node:fs/promises"
import path from "node:path"

const root = path.resolve(import.meta.dir, "../../../../..")
const ctx = { sessionID: "inspire-connector-acceptance", messageID: "test", agent: "research",
  directory: path.join(root, "openscience-custom"), worktree: path.join(root, "openscience-custom"),
  abort: new AbortController().signal,
  metadata: () => {},
  ask: async (input: { permission: string; patterns: string[] }) => {
    if (input.permission !== "remote_compute" || !/^[a-f0-9]{64}$/.test(input.patterns[0])) throw new Error("Incorrect approval boundary")
    throw new Error("APPROVAL_PREVIEW_ONLY")
  },
}
const plan = JSON.parse(await execute({ action: "plan", candidates: 3, minutes: 10 }, ctx))
console.log(JSON.stringify({ phase: "dry_run_passed", plan_id: plan.plan_id }))
const blocked = await execute({ action: "start", plan_id: plan.plan_id }, ctx).then(() => false, (error) => error.message === "APPROVAL_PREVIEW_ONLY")
if (!blocked) throw new Error("Native approval boundary was not enforced")
const report = { tested_utc: new Date().toISOString(), plan, approval_boundary_verified: true,
  submitted: false, note: "Real InspireSkill preflight and job create --dry-run passed. Start was deliberately denied before preparation/submission. No model API was called." }
await mkdir(path.join(root, "reports"), { recursive: true })
await Bun.write(path.join(root, "reports/inspire-screening-acceptance.json"), JSON.stringify(report, null, 2) + "\n")
console.log(JSON.stringify(report))
