// TEST ONLY: a real subprocess/filesystem transport fixture, NOT a scientific
// engine, benchmark or substitute for the required Inspire GPU acceptance.
import { createHash } from "node:crypto"
import { mkdir } from "node:fs/promises"
import path from "node:path"
import { digest, type Task, type Stage } from "./model"

const input = await Bun.file(process.argv[2]).json() as { task: Task; stage: Stage; root: string; fail?: boolean; extraReports?: number }
const folder = path.join(input.root, input.stage.name)
await mkdir(folder, { recursive: true })
if (input.fail) {
  await Bun.write(path.join(folder, "state.json"), JSON.stringify({ state: "failed", handle: input.stage.name }))
  process.exit(1)
}
const payload = "orchestration-fixture-not-scientific-output"
await Bun.write(path.join(folder, "artifact.txt"), payload)
const artifacts: Record<string, string> = { "artifact.txt": createHash("sha256").update(payload).digest("hex") }
if (input.stage.phase === "report") {
  for (let index = 0; index < (input.extraReports ?? 0); index++) {
    const name = `fixture-${index}.txt`
    await Bun.write(path.join(folder, name), payload)
    artifacts[name] = createHash("sha256").update(payload).digest("hex")
  }
}
const parent = input.stage.ordinal ? await Bun.file(path.join(input.root, `${input.task.id}-${input.task.plan.steps[input.stage.ordinal - 1].phase}`, "manifest.json")).json() : null
const result = { task_id: input.task.id, phase: input.stage.phase, plan_digest: input.task.digest, status: "SUCCEEDED",
  parent_digest: parent ? digest(parent) : null, input_count: 3, success_count: 3, failed_count: 0,
  identity_digest: "a".repeat(64), gates: { fixture_identity: true }, runtime_seconds: 1,
  artifacts, test_only: true }
await Bun.write(path.join(folder, "manifest.json"), JSON.stringify(result))
await Bun.write(path.join(folder, "state.json"), JSON.stringify({ state: "succeeded", handle: input.stage.name }))
