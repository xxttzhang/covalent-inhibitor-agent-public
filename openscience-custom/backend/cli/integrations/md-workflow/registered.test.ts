import { describe, expect, test } from "bun:test"
import { base, profile, directRequest, registered } from "./registered"

describe("registered MD request boundary", () => {
  test("installation roots remain exact and remote configuration is required", () => {
    expect(profile.shape.directory.safeParse(base).success).toBe(true)
    expect(profile.shape.directory.safeParse(base + "/other").success).toBe(false)
    const prior = process.env.COVSCREEN_MD_REMOTE_PREFIX
    try {
      delete process.env.COVSCREEN_MD_REMOTE_PREFIX
      expect(profile.shape.remote_root.safeParse("/inspire/fixture/run").success).toBe(false)
      process.env.COVSCREEN_MD_REMOTE_PREFIX = "/inspire/fixture/"
      expect(profile.shape.remote_root.safeParse("/inspire/fixture/run").success).toBe(true)
      for (const value of ["/inspire/fixture-other/run", "/inspire/fixture/../other", "/tmp/run"])
        expect(profile.shape.remote_root.safeParse(value).success).toBe(false)
    } finally {
      if (prior === undefined) delete process.env.COVSCREEN_MD_REMOTE_PREFIX
      else process.env.COVSCREEN_MD_REMOTE_PREFIX = prior
    }
  })
  test("accepts explicit scoped execution", () => {
    expect(directRequest("开始 Library-first 的 MD 和自动分析")).toBe(true)
    expect(directRequest("Run Library-first MD and analysis")).toBe(true)
  })
  test("questions, cancellation and unrelated routes cannot start", () => {
    for (const text of ["Library-first MD 是什么？", "如何运行 Library-first MD", "不要运行 Library-first MD", "开始 ligand-first MD", "Library-first 的 MD 参数", "停止 Library-first MD", "Do not run Library-first MD", "Don't start Library-first MD", "How to run Library-first MD", "Cancel Library-first MD run"]) expect(directRequest(text)).toBe(false)
  })
  test("unregistered project or missing frozen profile fails closed", async () => {
    await expect(registered("/tmp/not-the-registered-md-project")).rejects.toThrow()
  })
})
