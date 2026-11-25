import { expect, test } from "bun:test"
import path from "node:path"
import { tmpdir } from "../../test/fixture/fixture"
import { runtime } from "./runtime"

test("existing observer calls one optional host step even without a study dashboard configuration", async () => {
  await using directory = await tmpdir()
  const steps: string[] = []
  const service = runtime(directory.path, "SYNTHETIC", undefined, async () => { steps.push("host-step") })
  await service.tick()
  expect(steps).toEqual(["host-step"])
  expect(await Bun.file(path.join(directory.path, ".discovery/runtime-status.json")).exists()).toBe(false)
  await service.stop()
  await service.tick()
  expect(steps).toEqual(["host-step"])
})

test("existing observer serializes background steps and stop waits for the in-flight host", async () => {
  await using directory = await tmpdir()
  const started = Promise.withResolvers<void>(), complete = Promise.withResolvers<void>()
  const calls: string[] = []
  const service = runtime(directory.path, "SYNTHETIC", undefined, async () => {
    calls.push("entered"); started.resolve(); await complete.promise; calls.push("completed")
  })
  const first = service.tick()
  await started.promise
  await service.tick()
  expect(calls).toEqual(["entered"])
  const stop = service.stop().then(() => { calls.push("stopped") })
  complete.resolve()
  await Promise.all([first, stop])
  expect(calls).toEqual(["entered", "completed", "stopped"])
})

test("host failure does not disable later observer ticks; absent host remains inert", async () => {
  await using directory = await tmpdir()
  const errors: number[] = []
  const service = runtime(directory.path, "SYNTHETIC", undefined, async () => {
    errors.push(1); throw Error("SYNTHETIC unavailable evidence; no job or model invoked")
  })
  await service.tick(); await service.tick(); await service.stop()
  expect(errors).toHaveLength(2)
  await runtime(directory.path, "SYNTHETIC").tick()
  expect(await Bun.file(path.join(directory.path, ".discovery/runtime-status.json")).exists()).toBe(false)
})
