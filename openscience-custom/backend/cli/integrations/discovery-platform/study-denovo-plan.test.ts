import { studyFixture } from "../../test/fixture/study"
import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm, readdir } from "node:fs/promises"
import { createStudy } from "./study-store"
import { studyActionResult } from "./study-actions"

test("formal De novo action requires a bound hypothesis and never creates a receipt on rejection", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "denovo-study-action-"))
  try {
    const request = studyFixture()
    request.routes["library-first"].selected = false
    request.routes["de-novo"] = { selected: true, mode: "new", inputs: { pocket: "explicit", warhead: "acrylamide" }, generation: { slots: 1, seed: 12 } }
    const plan = await createStudy(root, "test", request)
    await expect(studyActionResult(root, "test", { action: "compile-denovo", study_id: plan.id })).rejects.toThrow("Attach this study")
    await expect(studyActionResult(root, "test", { action: "bind-ligand-plan", study_id: plan.id })).rejects.toThrow("Attach this study")
    expect(await readdir(path.join(root, ".discovery/studies", plan.id))).toEqual(["plan.json"])
    const result = await studyActionResult(root, "test", { action: "get", study_id: plan.id })
    expect(result).toHaveProperty("denovoPlan", null)
    expect(result).toHaveProperty("candidates", [])
  } finally { await rm(root, { recursive: true, force: true }) }
})
