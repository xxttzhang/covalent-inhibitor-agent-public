import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm } from "node:fs/promises"
import { createStudy } from "./study-store"
import { bindStudySession, sessionStudy, createSessionStudy } from "./study-session"
import { Storage } from "../../src/storage/storage"

test("session-bound study survives reads, isolates scope and never repairs explicit IDs", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "study-session-"))
  const scope = { directory, project: "binding-test", session: "ses_" + crypto.randomUUID().replaceAll("-", "") }
  try {
    const plan = await createStudy(directory, scope.project, {
      title: "Binding test", target: { id: "test", gene: "TEST", uniprot: "TEST", taxon: 9606,
        site: { chain: "A", number: 1, residue: "CYS", atom: "SG", numbering: "canonical" },
        receptor: { reference: "fixture" } }, hypothesis: { state: "pending", rationale: "", evidence: [] },
      routes: { "de-novo": { selected: true, mode: "new", inputs: {} } },
    })
    await expect(sessionStudy(scope)).rejects.toThrow("No study bound")
    await bindStudySession(scope, plan.id)
    expect(await sessionStudy(scope)).toBe(plan.id)
    const request = { title: plan.title, target: plan.target, hypothesis: plan.hypothesis, routes: plan.routes }
    expect(await createSessionStudy(scope, request)).toMatchObject({ reused: true, plan: { id: plan.id } })
    await expect(createSessionStudy(scope, { ...request, title: "replacement" })).rejects.toThrow("already has study")
    const fresh = { ...scope, session: scope.session + "fresh" }
    try {
      const concurrent = await Promise.all([createSessionStudy(fresh, request), createSessionStudy(fresh, request)])
      expect(concurrent[0].plan.id).toBe(concurrent[1].plan.id)
      expect(concurrent.filter(item => !item.reused)).toHaveLength(1)
    } finally {
      await Storage.remove(["study-session-binding", fresh.project, fresh.session])
      await Storage.remove(["study-session-creation", fresh.project, fresh.session])
    }
    expect(await sessionStudy({ ...scope })).toBe(plan.id)
    await expect(sessionStudy(scope, "study-typo")).rejects.toThrow()
    await expect(sessionStudy({ ...scope, session: scope.session + "other" })).rejects.toThrow("No study bound")
    await expect(sessionStudy({ ...scope, directory: directory + "/other" })).rejects.toThrow("scope mismatch")
    expect(await sessionStudy(scope)).toBe(plan.id)
    await rm(path.join(directory, ".discovery/studies", plan.id), { recursive: true })
    await expect(sessionStudy(scope)).rejects.toThrow()
  } finally {
    await Storage.remove(["study-session-binding", scope.project, scope.session])
    await rm(directory, { recursive: true, force: true })
  }
})
