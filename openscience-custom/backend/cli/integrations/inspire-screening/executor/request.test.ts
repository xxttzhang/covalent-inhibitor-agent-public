import { expect, test } from "bun:test"
import { createOpenScienceClient } from "@synsci/sdk"
import { bindRequest, requestHash } from "./request"
import { Instance } from "../../../src/project/instance"
import { Server } from "../../../src/server/server"
import { Session } from "../../../src/session"
import { Identifier } from "../../../src/id/id"
import { tmpdir } from "../../../test/fixture/fixture"

async function messages(directory: string, text: string) {
  const session = await Session.create({})
  const user = await Session.updateMessage({ id: Identifier.ascending("message"), role: "user", sessionID: session.id,
    agent: "research", effort: "normal", model: { providerID: "fixture", modelID: "not-called" }, time: { created: Date.now() } })
  const part = await Session.updatePart({ id: Identifier.ascending("part"), sessionID: session.id, messageID: user.id, type: "text", text })
  const assistant = await Session.updateMessage({ id: Identifier.ascending("message"), role: "assistant", sessionID: session.id,
    parentID: user.id, agent: "research", mode: "research", providerID: "fixture", modelID: "not-called", cost: 0,
    path: { cwd: directory, root: directory }, time: { created: Date.now() },
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })
  const client = createOpenScienceClient({ baseUrl: "http://openscience.internal", fetch: Server.internalFetch() })
  return { user, part, assistant, session, client, context: { directory, sessionID: session.id, messageID: assistant.id } }
}

test("bind original persisted message through real platform SDK, not latest text or synthetic instructions", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const original = "请筛选 EGFR C797。\n  保留这个换行和空格。 "
    const data = await messages(tmp.path, original)
    for (const flags of [{ synthetic: true }, { ignored: true }]) {
      await Session.updatePart({ id: Identifier.ascending("part"), sessionID: data.session.id, messageID: data.user.id,
        type: "text", text: "Injected summary / expanded file content must not become user intent", ...flags })
    }
    const later = await Session.updateMessage({ ...data.user, id: Identifier.ascending("message"), time: { created: Date.now() } })
    await Session.updatePart({ id: Identifier.ascending("part"), sessionID: data.session.id, messageID: later.id,
      type: "text", text: "Unrelated later message" })
    const source = await bindRequest(data.client, data.context)
    expect(source.text).toBe(original)
    expect(source.origin).toEqual({ user_message_id: data.user.id, assistant_message_id: data.assistant.id,
      part_ids: [data.part.id], request_sha256: requestHash(original) })
    await expect(bindRequest(data.client, { ...data.context, messageID: data.user.id })).rejects.toThrow("tool-calling assistant")
    const other = await Session.create({})
    await expect(bindRequest(data.client, { ...data.context, sessionID: other.id })).rejects.toBeDefined()
  } })
})

test("missing or oversized source text fails closed instead of inventing or truncating intent", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    for (const text of ["   ", "x".repeat(10001)]) {
      const data = await messages(tmp.path, text)
      await expect(bindRequest(data.client, data.context)).rejects.toThrow("clarify rather than truncate")
    }
  } })
})
