import { studyFixture } from "../../test/fixture/study"
import { test, expect } from "bun:test"
import path from "node:path"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import os from "node:os"
import { reasoningPermissions, reasoningPrompt, reasoningTool, startReasoning, reasoningStatus, resumeReasoning } from "./study-reasoning"
import { createStudy } from "./study-store"
import { PermissionNext } from "../../src/permission/next"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { SessionStatus } from "../../src/session/status"
import { tmpdir } from "../../test/fixture/fixture"

test("consolidated and dedicated hypothesis tools preserve the artifact delivery route", () => {
  const delivery = "target-hypothesis_covalent_target_hypothesis"
  expect(reasoningTool(["covalent-discovery_covalent_target_hypothesis", delivery])).toBe(delivery)
  expect(reasoningTool(["covalent-discovery_covalent_target_hypothesis"])).toBe("covalent-discovery_covalent_target_hypothesis")
  expect(() => reasoningTool([])).toThrow("Exactly one")
  expect(() => reasoningTool(["unknown_covalent_target_hypothesis", "other_covalent_target_hypothesis"])).toThrow("Exactly one")
})

test("reasoning liveness comes from the original platform session, not its receipt", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const request = studyFixture()
    const plan = await createStudy(tmp.path, Instance.project.id, request)
    expect((await reasoningStatus(tmp.path, plan)).state).toBe("not_started")
    await expect(resumeReasoning(tmp.path, plan)).rejects.toThrow("Start this study")
    const session = await Session.create({})
    await writeFile(path.join(tmp.path, `.discovery/studies/${plan.id}/reasoning.json`),
      JSON.stringify({ study: plan.id, project: plan.project, session: session.id, tool: "fixture",
        createdAt: new Date().toISOString(), computeAllowed: false }))
    SessionStatus.set(session.id, { type: "busy" })
    expect(await reasoningStatus(tmp.path, plan)).toMatchObject({ state: "busy", live: true, session: session.id })
    expect(await resumeReasoning(tmp.path, plan)).toMatchObject({
      state: "busy", session: session.id, resumed: false, computeSubmitted: false })
    SessionStatus.set(session.id, { type: "idle" })
    expect(await reasoningStatus(tmp.path, plan)).toMatchObject({
      state: "interrupted_or_not_dispatched", live: false,
      next: { action: "resume-reasoning", study_id: plan.id }, computeSubmitted: false })
    expect((await startReasoning(tmp.path, plan)).session).toBe(session.id)
    await expect(resumeReasoning(tmp.path, plan)).rejects.toThrow("Original target-hypothesis tool is unavailable")
    expect((await reasoningStatus(tmp.path, plan)).session).toBe(session.id)
    expect(await Session.messages({ sessionID: session.id })).toHaveLength(0)
    await Session.remove(session.id)
  } })
})

test("reasoning scope denies compute and preserves alternate target, parent and pocket", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "study-reasoning-"))
  try {
    const request = studyFixture()
    request.target.gene = "BTK"
    request.target.uniprot = "Q06187"
    request.target.site.number = 481
    request.target.receptor.reference = "alternate.pdb"
    request.routes["ligand-first"].inputs = { parent: "explicit-parent.sdf", parentBinding: "noncovalent", warhead: "acrylamide" }
    request.routes["de-novo"].inputs = { pocket: "explicit-pocket.json", warhead: "acrylamide" }
    const plan = await createStudy(root, "test", request)
    const tool = "target_hypothesis_covalent_target_hypothesis"
    const text = reasoningPrompt(plan, tool)
    expect(text).toContain(JSON.stringify(plan, null, 2))
    expect(text).toContain(plan.id)
    expect(text).toContain("顶层传 evidence_id")
    expect(text).toContain("source_index 的键")
    expect(text).toContain("不得重复 collect")
    const permissions = reasoningPermissions(tool)
    expect(PermissionNext.evaluate("mcp", tool, permissions).action).toBe("allow")
    for (const key of ["bash", "task", "remote_compute", "inspire_screening_task", "discovery_study"])
      expect(PermissionNext.evaluate(key, "*", permissions).action).toBe("deny")
    expect(PermissionNext.evaluate("mcp", "covalent_discovery_submit", permissions).action).toBe("deny")
    const record = { study: plan.id, project: "test", session: "ses_test", tool, createdAt: new Date().toISOString(), computeAllowed: false as const }
    await writeFile(path.join(root, `.discovery/studies/${plan.id}/reasoning.json`), JSON.stringify(record))
    expect(await startReasoning(root, plan)).toEqual(record) // no MCP or model replay
  } finally { await rm(root, { recursive: true, force: true }) }
})
