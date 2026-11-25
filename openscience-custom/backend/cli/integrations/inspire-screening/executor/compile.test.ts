import { expect, test } from "bun:test"
import { compileScreening } from "./compile"
import { requestHash } from "./request"
import { digest, planSchema, type ResearchInputs } from "./model"
import { createArgs, Inspire } from "./inspire"
import { chmod } from "node:fs/promises"
import { tmpdir as fixture } from "../../../test/fixture/fixture"
import { Store } from "./store"
import path from "node:path"
import { study } from "@synsci/util/discovery-study"
import { compileStudyLibrary } from "../../discovery-platform/study-library-compiler"

// Explicit synthetic infrastructure, never submitted. Tests call the same
// compiler used by the real create entry; there is no replacement planner.
function input(): Parameters<typeof compileScreening>[0] {
  const text = "Compiler test: EGFR C797"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: "test-project", group: "CPU",
    quota: "0,4,16", image: "test-slurm", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0.1 }
  return {
    intent: { target: "EGFR", site: "C797", library: "CovInDB/Virtual_Screening_Library.csv",
      screening_mode: "economy", max_points: 10,
      target_parameters: { target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
        insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" } },
    source: { text, origin: { user_message_id: "test-user", assistant_message_id: "test-assistant",
      part_ids: ["test-part"], request_sha256: requestHash(text) } },
    directory: "/test/workspace", session: "test-session",
    runtime: { root: "/inspire/test/outputs", project: "/inspire/test/science", worker: "/inspire/test/worker.py", activation: "/inspire/test/env.sh" },
    connection: { cli: "/nonexistent/test-inspire", account: "test", notebook: "test", workspace: "CPU资源空间" },
    assets: Object.fromEntries(["source", "target", "receptor", "engine", "worker"].map(key => ["/inspire/test/" + key, "a".repeat(64)])),
    cpu, gpu: { ...cpu, kind: "job", workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 1 },
  }
}

test("shared compiler produces independent plans and propagates declared runtime, budget and scale", () => {
  const first = input()
  const original = JSON.stringify(first)
  const a = compileScreening(first)
  const b = compileScreening(first)
  expect(a.id).not.toBe(b.id)
  expect(a.runtime).toEqual(first.runtime)
  expect(a.origin).toEqual(first.source.origin)
  expect(a.target_parameters).toEqual(first.intent.target_parameters)
  expect(a.shortlist).toBe(3)
  expect(a.steps.map(step => step.phase)).toEqual(["prepare", "carsidock", "rtmscore", "report"])
  expect(a.automation?.intent_digest).toBe(digest(first.intent))
  expect(JSON.stringify(first)).toBe(original)
  const changed = input()
  changed.intent.screening_mode = "deep"
  changed.intent.max_points = 20
  changed.runtime.root = "/inspire/test/another-output-root"
  const c = compileScreening(changed)
  expect(c.shortlist).toBe(20)
  expect(c.max_records).toBe(50000)
  expect(c.max_points).toBe(20)
  expect(c.runtime.root).toBe(changed.runtime.root)
  expect(c.automation?.intent_digest).not.toBe(a.automation?.intent_digest)
})

test("new target and missing or mismatched binding never fall back to EGFR", () => {
  const value = input()
  value.intent.target = "BTK"
  value.intent.site = "C481"
  value.intent.target_parameters = { ...value.intent.target_parameters!, target: "BTK", receptor: "5P9J", residue_number: 481 }
  expect(() => compileScreening(value)).toThrow("不会回退")
  expect(value.intent.target).toBe("BTK")
  const missing = input()
  delete missing.intent.target_parameters
  expect(() => compileScreening(missing)).toThrow("Explicit target")
  const changed = input()
  changed.intent.target_parameters!.chain = "B"
  expect(() => compileScreening(changed)).toThrow("不会回退")
})

test("compiler does not silently reduce scope or ignore budget/provenance errors", () => {
  const low = input()
  low.intent.max_points = 0
  expect(() => compileScreening(low)).toThrow("native confirmation")
  const wrong = input()
  wrong.source.text = "modified request"
  expect(() => compileScreening(wrong)).toThrow("Original request text differs")
})

test("research pilot is carried through compiler, schema and actual Inspire submission arguments", () => {
  const environment = input()
  environment.intent.screening_mode = "research_pilot_v1"
  const plan = compileScreening(environment)
  expect(plan).toMatchObject({ scope: "bounded", max_records: 20000, shortlist: 5,
    automation: { selected_mode: "research_pilot_v1", requested_mode: "research_pilot_v1", adjusted: false } })
  expect(plan.steps.map(step => step.max_candidates)).toEqual([5, 5, 5, 5])
  expect(plan.steps.map(step => step.minutes)).toEqual([15, 10, 5, 5])
  const limits = plan.steps.map((step, ordinal) => {
    const args = createArgs(plan, { task_id: plan.id, ordinal, phase: step.phase, name: `${plan.id}-${step.phase}`,
      state: "WAITING", handle: null, manifest: null, error: null, submitted: null }, true)
    expect(args).toContain("--dry-run")
    expect(args[args.indexOf("--quota") + 1]).toBe(step.resource.quota)
    return Number(args[args.indexOf("--max-time") + 1])
  })
  expect(limits[1] + limits[2]).toBe(0.25)
  expect(() => planSchema.parse({ ...plan, max_records: 10000 })).toThrow("versioned screening profile")
  expect(() => planSchema.parse({ ...plan, automation: { ...plan.automation!, requested_mode: "deep", adjusted: true } }))
    .toThrow("cannot be substituted")
  expect(() => planSchema.parse({ ...plan, steps: plan.steps.map(step => ({ ...step,
    minutes: step.phase === "carsidock" ? 11 : step.minutes })) })).toThrow("versioned screening profile")
  environment.intent.max_points = 0
  expect(() => compileScreening(environment)).toThrow("native confirmation")
})

function researchEnvironment() {
  const environment = input()
  environment.intent.screening_mode = "research_pilot_v1"
  const file = (name: string, digit: string) => ({ path: `/inspire/test/research/${name}`, sha256: digit.repeat(64) })
  const roles: ResearchInputs = { version: "screening-research-inputs-v1",
    library: { ...file("library-prefix.csv", "1"), records: 20000, selection: "frozen-file-order-prefix",
      source: file("original-library.csv", "2"), manifest: file("manifest.json", "3") },
    receptor: file("independently-prepared-1M17.pdb", "4"), science_config: file("research-target.yaml", "5"),
    reference: file("bound-reference.sdf", "6") }
  environment.research_inputs = roles
  for (const role of [roles.library, roles.library.manifest, roles.receptor, roles.science_config, roles.reference])
    environment.assets[role.path] = role.sha256
  return environment
}

for (const mode of ["legacy", "research", "changed-research-input"] as const) {
  test(`native preflight sends its frozen input roles through a real local CLI fixture before dry-run: ${mode}`, async () => {
    await using tmp = await fixture()
    const environment = mode === "legacy" ? input() : researchEnvironment()
    const cli = path.join(tmp.path, "inspire-fixture")
    const log = path.join(tmp.path, "argv.jsonl")
    environment.connection.cli = cli
    const plan = compileScreening(environment)
    const observed = { ...plan.assets }
    if (mode === "changed-research-input") observed[plan.research_inputs!.receptor.path] = "f".repeat(64)
    // The executable fixture implements only quota, read-only snapshot and
    // dry-run. It cannot contact Inspire or execute a scientific command.
    await Bun.write(cli, `#!${process.execPath}\nimport fs from "node:fs/promises";
const args=process.argv.slice(2); await fs.appendFile(${JSON.stringify(log)},JSON.stringify(args)+"\\n");
const send=data=>console.log(JSON.stringify({success:true,data}));
if(args[5]==="quota") send({items:[${JSON.stringify({ compute_group: environment.cpu.group, quota: environment.cpu.quota,
      points_per_hour: environment.cpu.points_per_hour, priority: "any" })}]});
else if(args[4]==="notebook"&&args[5]==="exec") {
 const match=args.at(-1).match(/'snapshot'(?: '([A-Za-z0-9+/=]+)')?$/);
 const roles=match?.[1]?JSON.parse(Buffer.from(match[1],"base64").toString()):null;
 if(!match||JSON.stringify(roles)!==JSON.stringify(${JSON.stringify(plan.research_inputs ?? null)})) {
   console.log(JSON.stringify({success:false,error:{code:"ResearchInputBindingMissing"}}));process.exit(2);
 }
 send({returncode:0,output:JSON.stringify({assets:${JSON.stringify(observed)}})});
} else if(args[5]==="create"&&args.includes("--dry-run")) send({dry_run:true});
else process.exit(3);
`)
    await chmod(cli, 0o700)
    const store = new Store(":memory:")
    try {
      const task = store.create(plan)
      const operation = new Inspire(plan.connection).preflight(task, store.stages(task.id)[0])
      if (mode === "changed-research-input") await expect(operation).rejects.toThrow("Frozen source or engine changed")
      else await operation
      const calls = (await Bun.file(log).text()).trim().split("\n").map(line => JSON.parse(line) as string[])
      expect(calls).toHaveLength(mode === "changed-research-input" ? 2 : 3)
      expect(calls.every(args => args[0] === "--json" && args[3] === plan.connection.account)).toBe(true)
      expect(calls.filter(args => args[5] === "create").every(args => args.includes("--dry-run"))).toBe(true)
      expect(store.get(task.id).state).toBe("DRAFT")
      expect(store.stages(task.id).every(stage => stage.submitted === null && stage.handle === null)).toBe(true)
    } finally { store.close() }
  })
}

test("research role bindings freeze a separate prepared receptor and actual prefix without changing legacy plans", () => {
  const environment = researchEnvironment()
  const frozen = JSON.stringify(environment)
  const plan = compileScreening(environment)
  expect(plan.research_inputs).toEqual(environment.research_inputs)
  expect(plan.research_inputs!.library.sha256).not.toBe(plan.research_inputs!.library.source.sha256)
  expect(plan.assets[plan.research_inputs!.library.source.path]).toBeUndefined()
  expect(plan.assets[plan.research_inputs!.receptor.path]).toBe(plan.research_inputs!.receptor.sha256)
  expect(JSON.stringify(environment)).toBe(frozen)
  expect(Object.hasOwn(compileScreening(input()), "research_inputs")).toBe(false)
  const changed = structuredClone(plan)
  changed.research_inputs!.reference.sha256 = "7".repeat(64)
  changed.assets[changed.research_inputs!.reference.path] = "7".repeat(64)
  expect(digest(planSchema.parse(changed))).not.toBe(digest(plan))
})

test("explicit research inputs reject missing assets, role substitution, path escapes and unsupported modes", () => {
  const environment = researchEnvironment()
  const plan = compileScreening(environment)
  for (const role of ["library", "receptor", "science_config", "reference"] as const) {
    const changed = structuredClone(plan)
    delete changed.assets[changed.research_inputs![role].path]
    expect(() => planSchema.parse(changed)).toThrow("frozen asset hash")
  }
  const missing = structuredClone(plan)
  delete missing.assets[missing.research_inputs!.library.manifest.path]
  expect(() => planSchema.parse(missing)).toThrow("frozen asset hash")
  const duplicate = structuredClone(plan)
  duplicate.research_inputs!.reference = duplicate.research_inputs!.receptor
  expect(() => planSchema.parse(duplicate)).toThrow("distinct")
  for (const filename of ["/tmp/receptor.pdb", "/inspire/../outside", "/inspire/double//file", "/inspire/./file", "/inspire/file\narg"]) {
    const changed = structuredClone(plan)
    changed.research_inputs!.receptor.path = filename
    changed.assets[filename] = changed.research_inputs!.receptor.sha256
    expect(() => planSchema.parse(changed)).toThrow()
  }
  environment.intent.screening_mode = "standard"
  expect(() => compileScreening(environment)).toThrow("restricted to the research pilot")
  expect(() => planSchema.parse({ ...plan, research_inputs: { ...plan.research_inputs!, version: "future-unreviewed" } })).toThrow()
  expect(() => planSchema.parse({ ...plan, research_inputs: { ...plan.research_inputs!, command: "injected command" } })).toThrow()
})

test("immutable study budget and upstream proposal reach the actual compiler", async () => {
  const root = path.resolve(import.meta.dir, "../../../../..")
  const saved = study.parse(await Bun.file(path.join(root,
    ".discovery/studies/study-e58f69ec-8a34-45dc-b8cc-900049265515/plan.json")).json())
  const original = await Bun.file(path.join(root,
    "results/target-hypotheses/hyp-8f4bc876d5e3430a85d8dd70/proposal-1b0ce2c8f9d3/hypothesis.json")).text()
  const environment = input()
  expect(() => compileStudyLibrary(saved, original, environment)).toThrow("new Library-first")
  saved.routes["library-first"] = { selected: true, mode: "new",
    inputs: { library: "CovInDB/Virtual_Screening_Library.csv", warhead: "acrylamide" },
    planning: { screeningMode: "economy", maxPoints: 10 } }
  const before = JSON.stringify(saved)
  const compiled = compileStudyLibrary(saved, original, environment)
  expect(compiled.compiled).toBe(true)
  if (!compiled.compiled) throw Error("Expected actual plan")
  expect(compiled.plan.shortlist).toBe(3)
  expect(compiled.plan.max_points).toBe(10)
  expect(compiled.plan.runtime).toEqual(environment.runtime)
  expect(compiled.submitted).toBe(false)
  expect(JSON.stringify(saved)).toBe(before)
  delete saved.routes["library-first"].planning
  expect(compileStudyLibrary(saved, original, environment).compiled).toBe(false)
  saved.id = "study-00000000-0000-0000-0000-000000000000"
  expect(() => compileStudyLibrary(saved, original, environment)).toThrow("does not belong")
})
