import { test, expect } from "bun:test"
import { mkdtemp, mkdir, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createStudy } from "./study-store"
import { attachHypothesis, readHypotheses, syncHypotheses } from "./study-hypothesis"
import { listReusableHypotheses, readHypothesisReuses, reuseHypothesis } from "./study-hypothesis-reuse"

test("explicit reuse freezes original evidence and never creates executable native attachment", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "study-reuse-"))
  try {
    const root = path.resolve(import.meta.dir, "../../../..")
    const old = await Bun.file(path.join(root, ".discovery/studies/study-c92778ff-f0a4-4743-bf95-ea403b8966ac/plan.json")).json()
    const { schema, id, project, createdAt, ...request } = old
    const source = await createStudy(directory, "reuse-test", request)
    const destination = await createStudy(directory, "reuse-test", { ...request, target: { ...request.target, receptor: { reference: "PDB:4G5J" } } })
    const original = await Bun.file(path.join(root, `.discovery/studies/${id}/ligand-first.hypothesis.import.json`)).json()
    const value = JSON.parse(original.original)
    value.hypothesis.study_id = source.id
    await mkdir(path.dirname(path.join(directory, original.source)), { recursive: true })
    await Bun.write(path.join(directory, original.source), JSON.stringify(value))
    const attached = await attachHypothesis(directory, source, original.source)
    if (!attached) throw Error("Missing attachment")
    expect(await readHypothesisReuses(directory, destination)).toEqual([])
    expect((await listReusableHypotheses(directory, destination)).map(item => item.sourceStudy)).toEqual([source.id])
    await expect(reuseHypothesis(directory, destination, source.id, "ligand-first", "0".repeat(64))).rejects.toThrow("changed")
    const saved = await reuseHypothesis(directory, destination, source.id, "ligand-first", attached.sha256)
    expect(saved.original).toBe(attached.original)
    expect(saved.sourcePlan.target.receptor.reference).toBe(source.target.receptor.reference)
    expect(saved.executionEligible).toBe(false)
    expect(JSON.parse(saved.original).hypothesis.study_id).toBe(source.id)
    expect(await readHypotheses(directory, destination)).toEqual([])
    expect(await syncHypotheses(directory, destination)).toEqual([])
    expect(await reuseHypothesis(directory, destination, source.id, "ligand-first", attached.sha256)).toEqual(saved)
    for (const target of [
      { ...destination.target, taxon: 10090 },
      { ...destination.target, gene: "BTK" },
      { ...destination.target, site: { ...destination.target.site, number: 481 } },
    ]) await expect(reuseHypothesis(directory, { ...destination, target }, source.id, "ligand-first", attached.sha256)).rejects.toThrow("mismatch")
    const next = await createStudy(directory, "reuse-test", request)
    value.hypothesis.study_id = next.id
    const second = original.source.replace(/proposal-[a-f0-9]{12}/, "proposal-111111111111")
    await mkdir(path.dirname(path.join(directory, second)), { recursive: true })
    await Bun.write(path.join(directory, second), JSON.stringify(value))
    expect((await syncHypotheses(directory, next)).map(item => item.source)).toEqual([second])
    expect((await syncHypotheses(directory, next)).length).toBe(1)
    const ambiguous = await createStudy(directory, "reuse-test", request)
    value.hypothesis.study_id = ambiguous.id
    for (const token of ["222222222222", "333333333333"]) {
      const candidate = original.source.replace(/proposal-[a-f0-9]{12}/, `proposal-${token}`)
      await mkdir(path.dirname(path.join(directory, candidate)), { recursive: true })
      await Bun.write(path.join(directory, candidate), JSON.stringify(value))
    }
    expect(await syncHypotheses(directory, ambiguous)).toEqual([])
    const filename = path.join(directory, `.discovery/studies/${destination.id}/ligand-first.hypothesis.reuse.json`)
    await Bun.write(filename, JSON.stringify({ ...saved, original: saved.original + " " }))
    await expect(readHypothesisReuses(directory, destination)).rejects.toThrow("hash mismatch")
  } finally { await rm(directory, { recursive: true, force: true }) }
})
