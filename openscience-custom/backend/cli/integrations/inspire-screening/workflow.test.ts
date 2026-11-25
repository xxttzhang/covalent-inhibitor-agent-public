import { expect, test } from "bun:test"
import { compile, schema, stages } from "./workflow"
import plugin from "./plugin-v2"

const report = { target_registered: true, source: { exists: true }, engine_assets_present: { carsidock: true, rtmscore: true, af3: true, covdock: true }, existing_project_route: "ligand-first", submitted: false as const, engine_runtime_verified: false as const }

test("registered tools separate formal workflow from explicitly requested historical test", async () => {
  const hooks = await plugin()
  expect(Object.keys(hooks.tool)).toEqual(["inspire_library_workflow", "inspire_screening_smoke"])
  expect(hooks.tool.inspire_screening_smoke.args.purpose.safeParse(undefined).success).toBe(false)
  expect(hooks.tool.inspire_screening_smoke.args.purpose.safeParse("historical_pilot_connection_test").success).toBe(true)
  const output = { system: [] as string[] }
  await hooks["experimental.chat.system.transform"]({}, output)
  expect(output.system.join()).toContain("do not route around it")
})

test("workflow follows the user's exact phase ordering, not the legacy pilot", () => {
  expect(stages.map((stage) => stage.id)).toEqual(["intake", "prepare", "carsidock", "rtmscore", "uncertainty", "af3", "covdock", "finalize"])
  const result = compile(schema.parse({ action: "draft", target: "EGFR", site: "C797" }), report)
  expect(result.scope).toBeNull()
  expect(result.limits.shortlist).toBeNull()
  expect(result.reuse_legacy_pilot).toBe(false)
  expect(result.status).toBe("DRAFT_NOT_EXECUTABLE")
  expect(result.blockers).toContain("Explicit scope/budget required: scope")
  expect(result.route_note).toContain("isolated")
})

test("budgets cannot silently truncate full scope or exceed promoted source", () => {
  for (const input of [
    { action: "draft", scope: "full", max_records: 10000 },
    { action: "draft", scope: "bounded" },
    { action: "draft", scope: "bounded", max_records: 10, shortlist: 20 },
    { action: "draft", shortlist: 10, af3_max: 11 },
    { action: "draft", af3_max: 101 }, { action: "draft", covdock_max: 51 },
    { action: "draft", source_library: "known_inhibitors" },
    { action: "draft", human_approved: true }, { action: "start", command: "anything" },
    { action: "draft", target: "../other" }, { action: "inspect", max_records: 130 },
  ]) expect(schema.safeParse(input).success).toBe(false)
})

test("complete numerical scope is not scientific evidence, runtime verification or approval", () => {
  const input = schema.parse({ action: "draft", target: "EGFR", site: "C797", scope: "full", shortlist: 20000,
    gpu_minutes: 60, cpu_minutes: 60, af3_max: 100, covdock_max: 50 })
  const result = compile(input, report)
  expect(result.blockers.some((item) => item.includes("Explicit scope/budget required"))).toBe(false)
  expect(result.dispatch_enabled).toBe(false)
  expect(result.approved).toBe(false)
  expect(result.submitted).toBe(false)
  expect(result.stages.every((stage) => !stage.runtime_verified)).toBe(true)
  expect(result.stages.find((stage) => stage.id === "af3")?.state).toBe("WAITING_FOR_PARENT")
  expect(result.blockers.some((item) => item.includes("Uncertainty"))).toBe(true)
})

test("missing inputs stay blocked, with downstream stages waiting", () => {
  const result = compile(schema.parse({ action: "draft" }), { ...report, target_registered: false, source: { exists: false } })
  expect(result.blockers).toContain("Target/site is not uniquely registered")
  expect(result.blockers).toContain("Requested virtual-screening library is unavailable")
})
