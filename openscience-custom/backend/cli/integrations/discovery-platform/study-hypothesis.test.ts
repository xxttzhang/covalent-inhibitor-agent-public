import { test, expect } from "bun:test"
import { mkdtemp, mkdir, rm, unlink, rename } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { studyActionResult } from "./study-actions"
import { createStudy } from "./study-store"
import { attachHypothesis } from "./study-hypothesis"
import { studyReport } from "@synsci/util/discovery-study-report"

test("real unsupported proposal uses formal ingress, freezes provenance, rejects other studies and targets", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "study-hypothesis-"))
  const platform = path.resolve(import.meta.dir, "../../../..")
  const filename = "results/target-hypotheses/hyp-8f4bc876d5e3430a85d8dd70/proposal-ef57729be979/hypothesis.json"
  try {
    const old = await Bun.file(path.join(platform, ".discovery/studies/study-e58f69ec-8a34-45dc-b8cc-900049265515/plan.json")).json()
    const { schema, id, project, createdAt, ...request } = old
    const plan = await createStudy(root, "test", request)
    const original = await Bun.file(path.join(platform, filename)).json()
    original.hypothesis.study_id = plan.id
    await mkdir(path.dirname(path.join(root, filename)), { recursive: true })
    await Bun.write(path.join(root, filename), JSON.stringify(original))
    const before = await studyActionResult(root, "test", { action: "get", study_id: plan.id })
    expect(before && "hypothesisSources" in before && before.hypothesisSources.map(p => p.source)).toEqual([filename])
    const input = { action: "attach-hypothesis", study_id: plan.id, source: filename }
    const result = await studyActionResult(root, "test", input) as NonNullable<Awaited<ReturnType<typeof attachHypothesis>>>
    expect(result.value.handoff.state).toBe("UNSUPPORTED")
    expect(result.value.handoff.compatibility_blockers).toEqual(original.handoff.compatibility_blockers)
    expect(result.newComputeSubmitted).toBe(false)
    expect(result.executionBinding).toContain("pending")
    expect(JSON.parse(result.original)).toEqual(original)
    const second = await createStudy(root, "test", request)
    const other = await studyActionResult(root, "test", { action: "get", study_id: second.id })
    expect(other && "hypothesisSources" in other && other.hypothesisSources).toEqual([])
    await expect(studyActionResult(root, "test", { ...input, study_id: second.id })).rejects.toThrow("study mismatch")
    original.hypothesis.study_id = second.id
    original.target.symbol = "JAK3"
    await Bun.write(path.join(root, filename), JSON.stringify(original))
    await expect(studyActionResult(root, "test", { ...input, study_id: second.id })).rejects.toThrow("target/site/species mismatch")
    await unlink(path.join(root, filename))
    expect(await studyActionResult(root, "test", input)).toEqual(result)
    const overview = await studyActionResult(root, "test", { action: "get", study_id: plan.id })
    expect(overview && "hypothesisAttachments" in overview && overview.hypothesisAttachments[0]?.sha256).toBe(result.sha256)
    if (!overview || !("hypothesisAttachment" in overview)) throw Error("Missing overview")
    expect(studyReport(overview)).toContain("UNSUPPORTED")
    expect(studyReport(overview)).toContain(result.sha256)
    const receiptPath = path.join(root, `.discovery/studies/${plan.id}/de-novo.hypothesis.import.json`)
    // Migrate the fixture to the pre-multi-route layout; production legacy files must remain readable.
    const legacy = path.join(root, `.discovery/studies/${plan.id}/hypothesis.import.json`)
    await rename(receiptPath, legacy)
    for (const [index, route] of ["library-first", "ligand-first"].entries()) {
      const variant = JSON.parse(result.original)
      variant.hypothesis.route = route
      variant.handoff.state = "NEEDS_INPUTS"
      variant.hypothesis.rationale = "Synthetic interface fixture; not a real route computation"
      const next = filename.replace("ef57729be979", String(index + 1).repeat(12))
      await mkdir(path.dirname(path.join(root, next)), { recursive: true })
      await Bun.write(path.join(root, next), JSON.stringify(variant))
      await studyActionResult(root, "test", { ...input, source: next })
    }
    const multi = await studyActionResult(root, "test", { action: "get", study_id: plan.id })
    if (!multi || !("hypothesisAttachments" in multi)) throw Error("Missing multi-route overview")
    expect(multi.hypothesisAttachments.map(item => item.value.hypothesis.route)).toEqual(["library-first", "ligand-first", "de-novo"])
    expect(multi.hypothesisAttachments[2].sha256).toBe(result.sha256)
    expect(studyReport(multi)).toContain("路线：ligand-first；原始状态：NEEDS_INPUTS")
    expect(studyReport(multi)).toContain("路线：de-novo；原始状态：UNSUPPORTED")
    const receipt = await Bun.file(legacy).json()
    receipt.original += " "
    await Bun.write(legacy, JSON.stringify(receipt))
    await expect(studyActionResult(root, "test", { action: "get", study_id: plan.id })).rejects.toThrow("hash mismatch")
  } finally { await rm(root, { recursive: true, force: true }) }
})
