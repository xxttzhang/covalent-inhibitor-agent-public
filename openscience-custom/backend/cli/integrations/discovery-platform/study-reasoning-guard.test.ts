import { studyFixture } from "../../test/fixture/study"
import { test, expect } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { createStudy } from "./study-store"
import { guardStudyReasoning } from "./study-reasoning-guard"

test("bound reasoning rejects missing/wrong study and disabled route without modifying arguments", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "reasoning-guard-"))
  try {
    const request = studyFixture()
    const plan = await createStudy(root, "test", request)
    const tool = "target-hypothesis_covalent_target_hypothesis"
    await writeFile(path.join(root, `.discovery/studies/${plan.id}/reasoning.json`), JSON.stringify({ study: plan.id,
      project: "test", session: "ses_guard", tool, createdAt: new Date().toISOString(), computeAllowed: false }))
    const input = { tool, sessionID: "ses_guard" }
    const args = { action: "finalize", evidence_id: "existing", hypothesis: { route: "library-first" } }
    const before = JSON.stringify(args)
    await expect(guardStudyReasoning(root, "test", input, args)).rejects.toThrow("hypothesis.study_id=")
    expect(JSON.stringify(args)).toBe(before)
    await expect(guardStudyReasoning(root, "test", input, { ...args, hypothesis: { ...args.hypothesis, study_id: "other" } })).rejects.toThrow("hypothesis.study_id=")
    await expect(guardStudyReasoning(root, "test", input, { ...args, hypothesis: { route: "de-novo", study_id: plan.id } })).rejects.toThrow("not selected")
    expect(await guardStudyReasoning(root, "test", input, { ...args, hypothesis: { ...args.hypothesis, study_id: plan.id } })).toBeUndefined()
    expect(await guardStudyReasoning(root, "test", input, { action: "read", evidence_id: "existing" })).toBeUndefined()
    expect(await guardStudyReasoning(root, "test", { ...input, sessionID: "ses_unbound" }, args)).toBeUndefined()
  } finally { await rm(root, { recursive: true, force: true }) }
})
