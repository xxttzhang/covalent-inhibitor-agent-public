import { expect, test } from "bun:test"
import { assertPlan, createArgs, hash, inputSchema, legacySharedRoot } from "./core"

test("actions require explicit bounds and never accept raw commands or approval booleans", () => {
  expect(inputSchema.safeParse({ action: "plan", candidates: 3, minutes: 10 }).success).toBe(true)
  for (const input of [
    { action: "plan" }, { action: "plan", candidates: 501, minutes: 10 },
    { action: "start", plan_id: "../other" }, { action: "start", human_approved: true },
    { action: "targets", command: "touch anything" },
    { action: "start", plan_id: "os-cov-" + "a".repeat(24), candidates: 100 },
  ]) expect(inputSchema.safeParse(input).success).toBe(false)
})

function fixture() {
  const config = { cli: "/bin/inspire", account: "test", cpu_notebook: "cpu", cpu_workspace: "CPU资源空间" as const,
    workspace: "分布式训练空间" as const, project: "project", group: "group", quota: "1,10,200", image: "image",
    priority: 4, max_candidates: 500, max_minutes: 240, shared_root: "/inspire/fixture-shared" }
  const body = { name: "os-cov-" + "a".repeat(24), campaign: "egfr-c797-covindb-pilot-v1" as const,
    session: "session", directory: "/project", created: Date.now(), candidates: 3, minutes: 10, config,
    assets: { campaign: "egfr-c797-covindb-pilot-v1" as const, prepared_candidates: 130, hashes: {}, reaction: "Michael Addition",
      canonical_site: "EGFR C797", structure_site: "A:CYS:773", torch: "2.4", cuda_build: "12.4", gpu_execution_verified: false as const, scope: "pilot" },
    points_per_hour: 1, maximum_compute_points: 1 / 6 }
  return { ...body, digest: hash(body) }
}

test("approvals bind session, inputs, resources and a short expiry", () => {
  const plan = fixture()
  expect(() => assertPlan(plan, "session", "/project")).not.toThrow()
  expect(() => assertPlan(plan, "different", "/project")).toThrow()
  expect(() => assertPlan(plan, "session", "/elsewhere")).toThrow()
  expect(() => assertPlan({ ...plan, candidates: 130 }, "session", "/project")).toThrow()
  expect(() => assertPlan(plan, "session", "/project", plan.created + 31 * 60000)).toThrow()
})

test("dry-run uses the real CLI surface, explicit scheduling, bounded runtime and no automatic retries", () => {
  const args = createArgs(fixture(), "a".repeat(64), true)
  for (const flag of ["--workspace", "--project", "--group", "--quota", "--image", "--max-time", "--dry-run", "--no-auto-fault-tolerance", "--no-enable-notification"]) expect(args).toContain(flag)
  expect(args[args.indexOf("--nodes") + 1]).toBe("1")
  expect(Number(args[args.indexOf("--max-time") + 1])).toBeCloseTo(1 / 6)
  expect(args.join(" ")).not.toContain("modal")
  expect(args.join(" ")).toContain("COVSCREEN_LEGACY_SHARED_ROOT='/inspire/fixture-shared'")
  expect(() => createArgs(fixture(), "'; touch /tmp/oops", false)).toThrow()
})

test("installation paths are explicit and reject traversal", () => {
  expect(legacySharedRoot({ shared_root: "/inspire/fixture" })).toBe("/inspire/fixture")
  for (const shared_root of ["relative", "/inspire/fixture/../other", "/inspire/fixture\n"])
    expect(() => legacySharedRoot({ shared_root })).toThrow()
  const saved = process.env.COVSCREEN_LEGACY_SHARED_ROOT
  delete process.env.COVSCREEN_LEGACY_SHARED_ROOT
  try { expect(() => legacySharedRoot({})).toThrow("COVSCREEN_LEGACY_SHARED_ROOT") }
  finally {
    if (saved !== undefined) process.env.COVSCREEN_LEGACY_SHARED_ROOT = saved
  }
})
