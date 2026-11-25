import { expect, test } from "bun:test"
import { chmod, readdir } from "node:fs/promises"
import path from "node:path"
import { Bus } from "../../src/bus"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { SessionTraceStore } from "../../src/session/trace-store"
import { PermissionNext } from "../../src/permission/next"
import { PermissionRoutes } from "../../src/server/routes/permission"
import { approveCovalent } from "../../src/session/covalent-approval"
import { tmpdir } from "../fixture/fixture"

test("HTTP approval route exposes a real frozen plan; refusal survives reconnect without signing", async () => {
  await using tmp = await tmpdir({ git: true })
  const previous = process.env.COVALENT_APPROVAL_KEY_FILE
  const key = path.join(tmp.path, "isolated-http-approval.key")
  await Bun.write(key, "isolated-http-refusal-test-key-1234567890")
  await chmod(key, 0o600)
  process.env.COVALENT_APPROVAL_KEY_FILE = key
  try {
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const session = await Session.create({})
      const app = PermissionRoutes()
      const server = Bun.serve({ hostname: "127.0.0.1", port: 0,
        fetch: async request => await Instance.provide({ directory: tmp.path, fn: () => app.fetch(request) }) })
      const request = (url: string, options?: RequestInit) => fetch(new URL(url, server.url), options)
      const pending = Promise.withResolvers<PermissionNext.Request>()
      const unsubscribe = Bus.subscribe(PermissionNext.Event.Asked, (event) => {
        if (event.properties.sessionID === session.id) pending.resolve(event.properties)
      })
      const workspace = path.resolve(import.meta.dir, "../../../../..")
      const args = {
        request_id: "http-refusal-request", project_id: "synthetic-http-project", route: "ligand-first",
        engine: "rdkit-warhead-enumeration", parameters: { scaffold_smiles: "CC", allowed_warheads: ["acrylamide"] },
        execution_mode: "execute", resource_budget: { max_candidates: 2, max_runtime_minutes: 1 },
      }
      const result = approveCovalent("covalent-discovery_covalent_submit_job", args, session.id,
        "msg_http_refusal", async (metadata) => {
          const id = "permission_http_refusal"
          await PermissionNext.ask({ id, sessionID: session.id, permission: "remote_compute",
            patterns: [`covalent_submit_job:${metadata.execution_plan_sha256}`], always: [],
            metadata: { ...metadata, user_message_id: "msg_user_http_refusal" }, ruleset: [] })
          const audit = (await SessionTraceStore.readForApproval(session.id)).approvals[id]
          if (audit.reply !== "once" || !audit.repliedAt) throw new Error("Missing persisted exact approval")
          return id
        }, async (values) => {
          // Real Python plan preparation, with no engine execution and a
          // disposable key/root. No production approval endpoint is touched.
          const child = Bun.spawn([(process.env.COVSCREEN_TEST_PYTHON ?? "python3"),
            "-c", "import json,sys; from pathlib import Path; from covscreen.approval_scope import prepare; "
            + "v=json.load(sys.stdin); print(json.dumps({'structuredContent':prepare(Path(sys.argv[1]),v['tool'],v['arguments'])}))",
            tmp.path], {
            env: { ...process.env, PYTHONPATH: path.join(workspace, "science/src") },
            stdin: new Blob([JSON.stringify(values)]), stdout: "pipe", stderr: "pipe",
          })
          expect(await child.exited).toBe(0)
          return JSON.parse(await new Response(child.stdout).text())
        }).then(value => ({ value, error: undefined }), (error: unknown) => ({ value: undefined, error }))
      try {
        const card = await Promise.race([pending.promise, result.then(() => {
          throw new Error("Approval finished before publishing its native card")
        })])
        const listed = await request("/")
        expect(listed.status).toBe(200)
        const cards = await listed.json() as PermissionNext.Request[]
        expect(cards.map(item => item.id)).toContain(card.id)
        expect(card.metadata.execution_plan_sha256).toMatch(/^[a-f0-9]{64}$/)
        expect(card.metadata.execution_plan).toMatchObject({ kind: "job", spec: {
          resource_budget: { max_candidates: 2, max_runtime_minutes: 1 },
          parameters: { scaffold_smiles: "CC" },
        } })
        expect((await SessionTraceStore.readForApproval(session.id)).approvals[card.id]?.reply).toBeUndefined()
        const invalid = await request(`/${card.id}/reply`, { method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ reply: "approve", human_approved: true }) })
        expect(invalid.status).toBe(400)
        expect((await PermissionNext.list()).map(item => item.id)).toContain(card.id)
        const response = await request(`/${card.id}/reply`, { method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ reply: "reject", message: "Synthetic HTTP refusal; do not execute." }) })
        expect(response.status).toBe(200)
        const outcome = await result
        expect(outcome.value).toBeUndefined()
        expect(outcome.error).toBeInstanceOf(PermissionNext.PlanRejectedError)
        const audit = (await SessionTraceStore.readForApproval(session.id)).approvals[card.id]
        expect(audit.reply).toBe("reject")
        expect(audit.plan?.userMessageID).toBe("msg_user_http_refusal")
        expect(audit.plan?.identity).toBe(`covalent_submit_job:${card.metadata.execution_plan_sha256}`)
        expect(await (await request("/")).json()).toEqual([])
        const jobs = await readdir(path.join(tmp.path, "state/jobs")).catch((error: NodeJS.ErrnoException) => {
          if (error.code === "ENOENT") return []
          throw error
        })
        expect(jobs).toEqual([])
        await Instance.dispose()
        await Instance.provide({ directory: tmp.path, fn: async () => {
          await expect(PermissionNext.ask({ ...card, id: "permission_http_reconnect", ruleset: [] }))
            .rejects.toBeInstanceOf(PermissionNext.PlanRejectedError)
          expect(await (await request("/")).json()).toEqual([])
          expect((await SessionTraceStore.readForApproval(session.id)).approvals[card.id].reply).toBe("reject")
          await Session.remove(session.id)
        } })
      } finally {
        unsubscribe()
        await server.stop(true)
      }
    } })
  } finally {
    if (previous === undefined) delete process.env.COVALENT_APPROVAL_KEY_FILE
    else process.env.COVALENT_APPROVAL_KEY_FILE = previous
  }
}, 30_000)
