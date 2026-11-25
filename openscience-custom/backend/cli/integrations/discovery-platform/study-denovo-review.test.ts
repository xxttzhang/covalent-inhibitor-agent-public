import { studyFixture } from "../../test/fixture/study"
import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, readdir, rm } from "node:fs/promises"
import { createStudy } from "./study-store"
import { prepareDeNovoIndependentInput, readDeNovoIndependentInput,
  registerDeNovoIndependentInput, syncDeNovoIndependentReview } from "./study-denovo-review"

test("native independent actions need the same frozen study and never invent generation or approval", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "denovo-independent-"))
  try {
    const request = studyFixture()
    request.routes["library-first"].selected = false
    request.routes["de-novo"] = { selected: true, mode: "new", inputs: { pocket: "explicit", warhead: "acrylamide" },
      generation: { slots: 1, seed: 12 } }
    const study = await createStudy(root, "test", request)
    expect(await readDeNovoIndependentInput(root, study)).toBeNull()
    await expect(prepareDeNovoIndependentInput(root, study, "denovo-" + "a".repeat(24) + ":0:raw"))
      .rejects.toThrow("native generation first")
    await expect(registerDeNovoIndependentInput(root, study)).rejects.toThrow("Freeze one native-study")
    await expect(syncDeNovoIndependentReview(root, study, "plan")).rejects.toThrow("Freeze one native-study")
    await expect(syncDeNovoIndependentReview(root, study, "prepare_md_handoff")).rejects.toThrow("Freeze one native-study")
    expect(await readdir(path.join(root, ".discovery/studies", study.id))).toEqual(["plan.json"])
  } finally { await rm(root, { recursive: true, force: true }) }
})
