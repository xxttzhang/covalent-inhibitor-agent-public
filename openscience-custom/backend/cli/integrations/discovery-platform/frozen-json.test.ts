import { expect, test } from "bun:test"
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { publishFrozenJSON } from "./frozen-json"

test("concurrent receipt publication preserves one complete immutable winner", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "frozen-plan-"))
  const filename = path.join(root, "plan.json")
  try {
    // A crash before publication can leave a partial temporary file.
    await writeFile(filename + ".interrupted.pending", '{"partial":')
    const plans = Array.from({ length: 12 }, (_, id) => ({ id, inputs: "x".repeat(10000) }))
    await Promise.all(plans.map(plan => publishFrozenJSON(filename, plan)))
    const first = await readFile(filename, "utf8")
    expect(plans).toContainEqual(JSON.parse(first))
    await publishFrozenJSON(filename, { replacement: true })
    expect(await readFile(filename, "utf8")).toBe(first)
    expect((await readdir(root)).sort()).toEqual(["plan.json", "plan.json.interrupted.pending"])
  } finally { await rm(root, { recursive: true, force: true }) }
})
