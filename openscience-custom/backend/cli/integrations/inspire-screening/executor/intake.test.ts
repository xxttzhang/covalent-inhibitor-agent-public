import { expect, test } from "bun:test"
import path from "node:path"
import { unlink } from "node:fs/promises"
import { createOpenScienceClient } from "@synsci/sdk"
import { clarify, confirmPlanning, explicitConditions, conditions } from "./intake"
import { bindRequest } from "./request"
import { Store } from "./store"
import { propose } from "./approval"
import { digest, planSchema } from "./model"
import { Instance } from "../../../src/project/instance"
import { Server } from "../../../src/server/server"
import { Session } from "../../../src/session"
import { Question } from "../../../src/question"
import { PermissionNext } from "../../../src/permission/next"
import { Identifier } from "../../../src/id/id"
import { Bus } from "../../../src/bus"
import { tmpdir } from "../../../test/fixture/fixture"
import { choose, minutes, plannerVersion, policy } from "./resource-planner"
import { targetCommand, targetParameters } from "@synsci/util/screening-target"

const explicit = "EGFR C797；CovInDB 虚拟筛选库；标准筛选；预算不超过0.5点券"
const expected = { target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv",
  screening_mode: "standard", max_points: 0.5 } as const

test("new-target parameters survive native intake persistence; do not create or approve compute", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const parameters = targetParameters.parse({ target: "BTK", receptor: "BTK.cif", chain: "B", residue: "CYS",
      residue_number: 481, insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" })
    const data = await setup(tmp.path, targetCommand(parameters, "standard", 50))
    try {
      const intake = await clarify(data.store, data.source, { target: "EGFR", site: "C797" }, data.context, true)
      expect(intake.values.target_parameters).toEqual(parameters)
      expect(intake.values.target).toBe("BTK")
      expect(data.store.list()).toEqual([])
      expect(await PermissionNext.list()).toEqual([])
      expect(await Question.list()).toEqual([])
      const row = data.store.db.query("SELECT resolved FROM intakes WHERE id=?").get(intake.id) as { resolved: string }
      expect(JSON.parse(row.resolved).target_parameters).toEqual(parameters)
    } finally { data.store.close() }
  } })
})

test("new create requests cannot silently inherit the old EGFR receptor and reaction", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, explicit)
    const asked = Promise.withResolvers<Question.Request>()
    const unsubscribe = Bus.subscribe(Question.Event.Asked, (event) => asked.resolve(event.properties))
    try {
      const pending = clarify(data.store, data.source, {}, data.context, true)
      const result = pending.then(() => "resolved", () => "rejected")
      const question = await asked.promise
      expect(question.questions).toHaveLength(1)
      expect(question.questions[0].header).toBe("受体与位点参数")
      await Question.reject(question.id)
      expect(await result).toBe("rejected")
      expect(data.store.list()).toEqual([])
    } finally { unsubscribe(); data.store.close() }
  } })
})

test("research pilot scale requires its explicit versioned mode and finite budget", () => {
  expect(conditions.parse(explicitConditions("EGFR C797；CovInDB 虚拟筛选库；screening_mode=research_pilot_v1；预算1点券")))
    .toEqual({ ...expected, screening_mode: "research_pilot_v1", max_points: 1 })
  expect(explicitConditions("screening_mode=research_pilot_v1；点券不设上限").max_points).toBeUndefined()
  expect(explicitConditions("screening_mode=research_pilot_v1；标准筛选").screening_mode).toBeUndefined()
})

test("no points cap must be explicit, unambiguous and tied to test100", () => {
  const text = "EGFR C797；CovInDB 虚拟筛选库；100候选测试；点券不设上限"
  expect(conditions.parse(explicitConditions(text))).toEqual({ ...expected, screening_mode: "test100", max_points: null })
  expect(explicitConditions(text.replace("100候选测试", "标准筛选")).max_points).toBeUndefined()
  expect(explicitConditions(text.replace("点券不设上限", "例如点券不设上限")).max_points).toBeUndefined()
  expect(explicitConditions(text + "；预算50点券").max_points).toBeUndefined()
  expect(explicitConditions(text.replace("点券不设上限", "不要取消点券上限")).max_points).toBeUndefined()
  expect(explicitConditions(text.replace("点券不设上限", "")).max_points).toBeUndefined()
})

async function setup(directory: string, text: string) {
  const session = await Session.create({})
  const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
    agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
  await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: user.id, type: "text", text })
  const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
    parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
    path: { cwd: directory, root: directory }, time: { created: Date.now() },
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
  const client = createOpenScienceClient({ baseUrl: "http://openscience.internal", fetch: Server.internalFetch() })
  const controller = new AbortController()
  const metadata: unknown[] = []
  const context = { sessionID: session.id, messageID: assistant.id, directory, callID: "call-" + crypto.randomUUID(),
    abort: controller.signal, metadata: (value: unknown) => { metadata.push(value) } }
  const source = await bindRequest(client, context)
  const file = path.join(directory, "intake.sqlite")
  return { session, source, context, controller, metadata, file, store: new Store(file), client }
}

test("explicit user conditions beat model suggestions; ambiguous, negated and conflicting text is not guessed", () => {
  expect(conditions.parse(explicitConditions(explicit))).toEqual(expected)
  expect(explicitConditions("不要沿用旧的130个；例如预算1点券")).toEqual({})
  expect(explicitConditions("预算0.5点券；预算1点券").max_points).toBeUndefined()
  expect(explicitConditions("预算$10").max_points).toBeUndefined()
  expect(explicitConditions("经济筛选；深度筛选").screening_mode).toBeUndefined()
  expect(explicitConditions("筛选5000个；GPU 100分钟")).toEqual({})
  expect(explicitConditions("EGFR C797S").site).toBeUndefined()
  expect(explicitConditions("EGFR C797 或者 BTK C481；CovInDB 虚拟筛选库")).toEqual({ library: expected.library })
  expect(explicitConditions("CovInDB_All.csv 是已知抑制剂，不用虚拟筛选库")).toEqual({})
  expect(conditions.parse(explicitConditions(`${explicit}；不要使用旧任务或旧候选；不要运行 AF3/CovDock`))).toEqual(expected)
})

test("a complete actual user request needs no extra question and its persisted conditions cannot be replaced by model arguments", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, explicit)
    const result = await clarify(data.store, data.source, { ...expected, screening_mode: "deep", max_points: 10 }, data.context)
    expect(result.values).toEqual(expected)
    expect(await Question.list()).toHaveLength(0)
    expect(await PermissionNext.list()).toHaveLength(0)
    expect(data.store.list()).toHaveLength(0)
    data.store.close()
    const reopened = new Store(data.file)
    expect(await clarify(reopened, data.source, { ...expected, screening_mode: "economy" }, data.context)).toEqual(result)
    await expect(clarify(reopened, { ...data.source, text: "changed" }, expected, data.context)).rejects.toThrow("source differs")
    reopened.close()
  } })
})

test("a live-price budget mismatch requires a second native confirmation and still does not approve compute", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, "EGFR C797；CovInDB 虚拟筛选库；深度筛选；预算不超过0.5点券")
    const intake = await clarify(data.store, data.source, {}, data.context)
    const planned = choose({ screening_mode: intake.values.screening_mode, max_points: intake.values.max_points }, { cpu: 0, gpu: 1 })
    expect(planned).toMatchObject({ requested: "deep", selected: "standard", requires_confirmation: true })
    const asked = Promise.withResolvers<Question.Request>()
    const unsubscribe = Bus.subscribe(Question.Event.Asked, (event) => asked.resolve(event.properties))
    try {
      const pending = confirmPlanning(data.store, intake.id, planned, data.context)
      const card = await asked.promise
      expect(card.questions).toHaveLength(1)
      expect(card.questions[0].question).toContain("不会静默缩小范围")
      expect(data.store.list()).toHaveLength(0)
      expect(await PermissionNext.list()).toHaveLength(0)
      await Question.reply({ requestID: card.id, answers: [["采用标准方案"]] })
      await expect(pending).resolves.toEqual({ ...expected, requested_mode: "deep" })
      expect(data.store.db.query("SELECT state FROM intakes").all()).toEqual([{ state: "CONFIRMED" }])
      expect(data.store.list()).toHaveLength(0)
      expect(await PermissionNext.list()).toHaveLength(0)
    } finally { unsubscribe(); data.store.close() }
  } })
})

for (const mode of ["source", "bundle", "accept", "empty", "reject", "abort", "abort_other_project", "invalid"] as const) {
  test(`missing conditions are answered on the actual native question route before any task or resource approval: ${mode}`, async () => {
    await using tmp = await tmpdir({ git: true })
    const filename = path.join(import.meta.dir, `.intake-test-${crypto.randomUUID()}.mjs`)
    const run = await (async () => {
      if (mode !== "bundle") return clarify
      const build = await Bun.build({ entrypoints: [path.join(import.meta.dir, "intake.ts")], target: "bun", packages: "external" })
      if (!build.success) throw new AggregateError(build.logs)
      await Bun.write(filename, build.outputs[0])
      return (await import(filename)).clarify as typeof clarify
    })()
    try {
      await Instance.provide({ directory: tmp.path, fn: async () => {
        const data = await setup(tmp.path, "从 CovInDB 虚拟筛选库为 EGFR C797 寻找候选")
        const asked = Promise.withResolvers<Question.Request>()
        const unsubscribe = Bus.subscribe(Question.Event.Asked, (event) => {
          if (event.properties.sessionID === data.session.id) asked.resolve(event.properties)
        })
        const suggestion: Parameters<typeof clarify>[2] = mode === "empty" ? {} :
          { ...expected, screening_mode: mode === "accept" ? "economy" : "deep" }
        const pending = run(data.store, data.source, suggestion, data.context)
          .then((result) => ({ result }), (error: unknown) => ({ error }))
        try {
          const card = await asked.promise
          expect(card.questions).toHaveLength(1)
          if (mode === "empty") expect(card.questions[0].options).toHaveLength(0)
          if (mode !== "empty") expect(card.questions[0].question).toContain(String(suggestion.screening_mode)) // Suggestion only; not silently used.
          expect(card.tool).toEqual({ messageID: data.context.messageID, callID: data.context.callID })
          expect(data.store.list()).toHaveLength(0)
          expect(await PermissionNext.list()).toHaveLength(0)
          const actual = Server.internalFetch()
          const refreshed = await actual(new Request(`http://openscience.internal/question?directory=${encodeURIComponent(tmp.path)}`))
          expect((await refreshed.json() as Question.Request[]).find((item) => item.id === card.id)).toEqual(card)
          const url = `http://openscience.internal/question/${card.id}/${mode === "reject" ? "reject" : "reply"}?directory=${encodeURIComponent(tmp.path)}`
          if (mode === "abort") data.controller.abort()
          else if (mode === "abort_other_project") {
            await using other = await tmpdir({ git: true })
            await Instance.provide({ directory: other.path, fn: async () => { data.controller.abort() } })
          }
          else {
            const response = await actual(new Request(url, { method: "POST", headers: { "Content-Type": "application/json" },
              body: mode === "reject" ? undefined : JSON.stringify({ answers: mode === "invalid" ? [["随便"]] :
                mode === "accept" ? [["采用上述建议"]] : [["标准筛选；预算不超过0.5点券"]] }) }))
            expect(response.ok).toBe(true)
          }
          const result = await pending
          expect(await Question.list()).toHaveLength(0)
          expect(await PermissionNext.list()).toHaveLength(0)
          expect(data.store.list()).toHaveLength(0)
          if (["source", "bundle", "accept", "empty"].includes(mode)) {
            if (!("result" in result)) throw result.error
            expect(result.result.values).toEqual({ ...expected, screening_mode: mode === "accept" ? "economy" : "standard" })
            expect(data.store.db.query("SELECT state FROM intakes").all()).toEqual([{ state: "CONFIRMED" }])
          } else {
            expect("error" in result).toBe(true)
            expect(data.store.db.query("SELECT state FROM intakes").all()).toEqual([{ state: mode.startsWith("abort") ? "ABORTED" : mode === "reject" ? "REJECTED" : "INVALID" }])
            await expect(run(data.store, data.source, expected, { ...data.context, abort: new AbortController().signal })).rejects.toThrow("Prior clarification")
          }
        } finally { unsubscribe(); data.store.close() }
      } })
    } finally { if (mode === "bundle") await unlink(filename) }
  }, 10000)
}

test("confirmed intake binds exact parameters atomically to the task; confirmation cannot substitute for native compute approval", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const data = await setup(tmp.path, explicit)
    const intake = await clarify(data.store, data.source, {}, data.context)
    const concrete = policy("standard")
    const stageMinutes = minutes(concrete)
    const resource = { kind: "hpc", workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
      image: "fixture", image_type: "SOURCE_OFFICIAL", priority: 4, points_per_hour: 0 }
    const plan = planSchema.parse({ version: "screening-executor-v1", id: "screen-" + "f".repeat(24),
      request: data.source.text, origin: data.source.origin, directory: tmp.path, session: data.session.id,
      target: expected.target, site: expected.site, library: expected.library, scope: concrete.scope, max_records: concrete.max_records,
      shortlist: concrete.shortlist, seed: 1, max_points: expected.max_points,
      connection: { cli: "/never-called", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" },
      assets: Object.fromEntries(["a", "b", "c", "d", "e"].map((name) => ["/inspire/" + name, "a".repeat(64)])),
      runtime: { project: "/inspire/project", root: "/inspire/root", worker: "/inspire/worker", activation: "/inspire/activation" },
      automation: { version: plannerVersion, requested_mode: "standard", selected_mode: "standard", adjusted: false,
        intent_digest: digest(expected), estimated_points: 0.5, budget_headroom: 0,
        basis: "versioned-profile-plus-live-quota-rates" },
      steps: ["prepare", "carsidock", "rtmscore", "report"].map((phase) => ({ phase,
        minutes: stageMinutes[phase as keyof typeof stageMinutes], max_candidates: concrete.shortlist,
        resource: ["carsidock", "rtmscore"].includes(phase) ? { ...resource, kind: "job", workspace: "分布式训练空间", quota: "1,10,200", points_per_hour: 1 } : resource })),
      gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 }, created: Date.now(),
      approve_before: Date.now() + 60000, finish_before: Date.now() + 120000 })
    const context = { ...data.context, ask: (input: Parameters<typeof PermissionNext.ask>[0]) => PermissionNext.ask({ ...input,
      sessionID: data.session.id, ruleset: [{ permission: "*", pattern: "*", action: "allow" }] }) }
    await expect(propose(data.store, { ...plan, max_points: 1 }, context, intake.id)).rejects.toThrow("conditions differ")
    expect(data.store.list()).toHaveLength(0)
    const asked = Promise.withResolvers<PermissionNext.Request>()
    const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => asked.resolve(event.properties))
    try {
      const pending = propose(data.store, plan, context, intake.id)
      const card = await asked.promise
      expect(card.patterns).toEqual([digest(plan)])
      expect(data.store.get(plan.id).state).toBe("DRAFT")
      expect(data.store.get(plan.id).approval).toBeNull()
      expect(data.store.db.query("SELECT task_id FROM intakes").all()).toEqual([{ task_id: plan.id }])
      await PermissionNext.reply({ requestID: card.id, reply: "once" })
      expect((await pending).state).toBe("APPROVED")
      expect(data.store.stages(plan.id).every((stage) => stage.submitted === null)).toBe(true)
      await expect(clarify(data.store, data.source, {}, data.context)).rejects.toThrow("already created")
      expect(data.store.list()).toHaveLength(1)
    } finally { unsubscribe(); data.store.close() }
  } })
})
