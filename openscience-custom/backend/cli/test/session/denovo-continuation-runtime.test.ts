import { expect, test } from "bun:test"
import path from "node:path"
import { createHash } from "node:crypto"
import { digest } from "../../integrations/inspire-screening/executor/model"
import { writeFile } from "node:fs/promises"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { SessionStatus } from "../../src/session/status"
import { Identifier } from "../../src/id/id"
import { Storage } from "../../src/storage/storage"
import { createStudy } from "../../integrations/discovery-platform/study-store"
import { bindStudySession } from "../../integrations/discovery-platform/study-session"
import { consumeDeNovoReasoning, acknowledgeDeNovoReasoning, registerDeNovoReasoning, observeDeNovoContinuation, queueDeNovoNotification, resumeDeNovoFromUser } from "../../src/session/denovo-continuation-runtime"
import { pauseDeNovoContinuation, readDeNovoContinuation, tickDeNovoContinuation } from "../../src/session/denovo-continuation"
import { tmpdir } from "../fixture/fixture"

test("original reasoning receipt and live session drive observation; a foreign child cannot replace it", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parent = await Session.create({})
    const child = await Session.create({})
    const scope = { directory: tmp.path, project: Instance.project.id, session: parent.id }
    try {
      const study = await createStudy(tmp.path, scope.project, {
        title: "Continuation fixture", target: { id: "fixture", gene: "TEST", uniprot: "TEST", taxon: 9606,
          site: { chain: "A", number: 1, residue: "CYS", atom: "SG", numbering: "canonical" },
          receptor: { reference: "fixture" } }, hypothesis: { state: "pending", rationale: "", evidence: [] },
        routes: { "de-novo": { selected: true, mode: "new", inputs: {} } },
      })
      await bindStudySession(scope, study.id)
      await writeFile(path.join(tmp.path, ".discovery/studies", study.id, "reasoning.json"), JSON.stringify({
        study: study.id, project: scope.project, session: child.id, createdAt: new Date().toISOString(),
        tool: "fixture", computeAllowed: false,
      }))
      const saved = await registerDeNovoReasoning(scope, study.id, child.id)
      SessionStatus.set(child.id, { type: "busy" })
      expect(await observeDeNovoContinuation(scope, saved)).toMatchObject({ task: child.id, state: "RUNNING" })
      // V24: the validated proposal is consumed before the child finishes its summary.
      await expect(consumeDeNovoReasoning(scope, study.id, "generation-test")).rejects.toThrow("frozen generation plan")
      const folder = path.join(tmp.path, ".discovery/studies", study.id)
      const original = JSON.stringify({ schema_version: "target-hypothesis-v1",
        target: { symbol: "TEST", site: "C1", taxon: 9606, uniprot_id: "TEST" },
        hypothesis: { study_id: study.id, route: "de-novo" }, handoff: { state: "HOST_PLAN_REQUIRED", compute_submitted: false } })
      const hash = createHash("sha256").update(original).digest("hex")
      await Bun.write(path.join(folder, "de-novo.hypothesis.import.json"), JSON.stringify({
        schema: "study-hypothesis-import-v1", study: study.id, project: scope.project,
        source: "results/target-hypotheses/hyp-" + "a".repeat(24) + "/proposal-" + "b".repeat(12) + "/hypothesis.json",
        sha256: hash, original, importedAt: new Date().toISOString() }))
      const plan = { state: "PLAN_COMPILED_NOT_APPROVED", compiled: true, approved: false, submitted: false,
        study: study.id, sourceKind: "immutable-study-not-native-compute-request",
        request: { id: "generation-test", session_id: child.id }, inputHashes: { receptor: "fixture", pocket: "fixture" }, pending: [] }
      const receipt = { schema: "study-denovo-plan-v1", study: study.id, project: scope.project,
        createdAt: new Date().toISOString(), studyDigest: digest(study), hypothesisDigest: hash, planDigest: digest(plan), plan }
      await Bun.write(path.join(folder, "denovo-execution-plan.json"), JSON.stringify(receipt))
      await expect(consumeDeNovoReasoning(scope, study.id, "foreign-task")).rejects.toThrow("differs")
      expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
      const foreign = { ...plan, request: { ...plan.request, session_id: parent.id } }
      await Bun.write(path.join(folder, "denovo-execution-plan.json"), JSON.stringify({ ...receipt, plan: foreign, planDigest: digest(foreign) }))
      await expect(consumeDeNovoReasoning(scope, study.id, "generation-test")).rejects.toThrow("differs")
      await Bun.write(path.join(folder, "denovo-execution-plan.json"), JSON.stringify({ ...receipt, hypothesisDigest: "tampered" }))
      await expect(consumeDeNovoReasoning(scope, study.id, "generation-test")).rejects.toThrow("identity or evidence changed")
      expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
      await Bun.write(path.join(folder, "denovo-execution-plan.json"), JSON.stringify(receipt))
      await consumeDeNovoReasoning(scope, study.id, "generation-test")
      expect(SessionStatus.get(child.id).type).toBe("busy")
      expect((await readDeNovoContinuation(scope))?.state).toBe("observed")
      await registerDeNovoReasoning(scope, study.id, child.id, true)
      await pauseDeNovoContinuation(scope)
      await consumeDeNovoReasoning(scope, study.id, "generation-test")
      expect((await readDeNovoContinuation(scope))?.state).toBe("paused")
      await resumeDeNovoFromUser(parent.id, [{ type: "text", text: "continue" }])
      SessionStatus.set(child.id, { type: "idle" })
      expect(await observeDeNovoContinuation(scope, saved)).toMatchObject({ task: child.id, state: "INTERRUPTED" })
      await expect(registerDeNovoReasoning(scope, study.id, parent.id)).rejects.toThrow("differs")
      await expect(observeDeNovoContinuation(scope, { ...saved, study: "study-other" })).rejects.toThrow("no longer bound")
      await acknowledgeDeNovoReasoning(scope, { session: parent.id, state: "finished", live: false })
      expect((await readDeNovoContinuation(scope))?.state).toBe("waiting")
      await acknowledgeDeNovoReasoning(scope, { session: child.id, state: "interrupted_or_not_dispatched", live: false })
      expect((await readDeNovoContinuation(scope))?.state).toBe("observed")
      await registerDeNovoReasoning(scope, study.id, child.id, true)
      await Session.updateMessage({ role: "user", id: Identifier.ascending("message"), sessionID: parent.id,
        time: { created: Date.now() }, model: { providerID: "test", modelID: "test" }, agent: "build", effort: "normal" })
      await Session.updateMessage({ role: "assistant", id: Identifier.ascending("message"), sessionID: child.id,
        time: { created: Date.now(), completed: Date.now() }, parentID: Identifier.ascending("message"),
        modelID: "test", providerID: "test", mode: "build", agent: "build", path: { cwd: tmp.path, root: tmp.path },
        cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, finish: "stop" })
      await tickDeNovoContinuation(scope, { active: async () => true, idle: () => true,
        observe: saved => observeDeNovoContinuation(scope, saved),
        wake: async saved => { await queueDeNovoNotification(parent.id, saved) } })
      expect((await readDeNovoContinuation(scope))?.state).toBe("delivered")
      expect((await Session.messages({ sessionID: parent.id })).at(-1)?.parts[0]).toMatchObject({ synthetic: true })
      await pauseDeNovoContinuation(scope)
      await resumeDeNovoFromUser(parent.id, [{ type: "text", text: "continue", synthetic: true }])
      expect((await readDeNovoContinuation(scope))?.state).toBe("paused")
      await resumeDeNovoFromUser(parent.id, [{ type: "text", text: "continue" }])
      expect((await readDeNovoContinuation(scope))?.state).toBe("observed")
    } finally {
      SessionStatus.set(child.id, { type: "idle" })
      await Storage.remove(["denovo-continuations", scope.project, scope.session])
      await Storage.remove(["denovo-continuation-stops", scope.project, scope.session])
      await Storage.remove(["study-session-binding", scope.project, scope.session])
      await Session.remove(child.id)
      await Session.remove(parent.id)
    }
  } })
})

test("notification recovers partial message persistence without duplicating or granting compute", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const session = await Session.create({})
    try {
      const user = { role: "user" as const, id: Identifier.ascending("message"), sessionID: session.id,
        time: { created: Date.now() }, model: { providerID: "test", modelID: "test" },
        agent: "build", effort: "normal" as const, tools: { bash: false } }
      await Session.updateMessage(user)
      const saved = { study: "study-fixture", stage: "reasoning" as const, task: "ses-fixture", request: "study-fixture",
        state: "ready" as const, messageID: Identifier.ascending("message"), observed: "COMPLETED", next: 0, expires: Date.now() + 60_000 }
      await Session.updateMessage({ ...user, id: saved.messageID })
      expect(await queueDeNovoNotification(session.id, saved)).toBe(true)
      expect(await queueDeNovoNotification(session.id, saved)).toBe(true)
      const messages = await Session.messages({ sessionID: session.id })
      expect(messages).toHaveLength(2)
      const notice = messages.find(item => item.info.id === saved.messageID)!
      expect(notice.info).toMatchObject({ tools: { bash: false }, model: user.model })
      expect(notice.parts).toHaveLength(1)
      expect(notice.parts[0]).toMatchObject({ type: "text", synthetic: true })
      expect(JSON.stringify(notice.parts)).toContain("do not recreate the study, resubmit compute")
    } finally { await Session.remove(session.id) }
  } })
})
