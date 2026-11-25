import { expect, test } from "bun:test"
import { Hono } from "hono"
import { HTTPException } from "hono/http-exception"
import { mkdtemp, rm } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { installStudyHTTP } from "./study-http"
import { studyRoutes } from "@synsci/util/discovery-study"

test("warm HTTP route creates/lists isolated studies while retaining host authorization", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "study-http-"))
  const authority = { trusted: true, project: "project-a" }
  const app = new Hono().use("*", async (c, next) => {
    if (c.req.header("x-test-deny")) return c.json({ error: "host denied" }, 403)
    await next()
  }).all("*", c => c.json({ error: "not_found" }, 404))
  await app.request("/warm")
  installStudyHTTP(app, () => ({ directory, project: authority.project, authorize: async () => {
    if (!authority.trusted) throw new HTTPException(403, { message: "Project not trusted" })
  } }))
  const post = (body: unknown, headers: Record<string, string> = {}) => app.request("http://localhost/api/discovery/study", {
    method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body),
  })
  try {
    expect((await post({ action: "list" }, { "x-test-deny": "yes" })).status).toBe(403)
    expect((await post({ action: "list" }, { origin: "https://foreign.example" })).status).toBe(403)
    expect((await post({ action: "list" }, { origin: "http://127.0.0.1:4449" })).status).toBe(200)
    expect((await post({ action: "list" }, { origin: "http://localhost:4449", "x-test-deny": "yes" })).status).toBe(403)
    expect((await app.request("/api/discovery/study")).status).toBe(405)
    expect((await post({ action: "submit" })).status).toBe(400)
    expect((await app.request("/unrelated")).status).toBe(404)
    authority.trusted = false
    expect((await post({ action: "list" })).status).toBe(403)
    authority.trusted = true
    const response = await post({ action: "create", request: { title: "HTTP test", target: { id: "other-c909", gene: "OTHER",
      taxon: 9606, uniprot: "TEST", site: { chain: "B", residue: "CYS", number: 909, atom: "SG", numbering: "canonical" },
      receptor: { reference: "test-input" } }, hypothesis: { state: "pending", rationale: "HTTP test only", evidence: [] },
      routes: Object.fromEntries(studyRoutes.map(route => [route, { selected: route === "library-first", mode: "new", inputs: {} }])) } })
    expect(response.status).toBe(200)
    const created = await response.json()
    expect(created.plan.target.site.number).toBe(909)
    const loaded = await (await post({ action: "get", study_id: created.plan.id })).json()
    expect(loaded.routes[0].state).toBe("missing-inputs")
    expect(loaded.routes[1].state).toBe("not-selected")
    expect(loaded.candidates).toEqual([])
    expect((await (await post({ action: "list" })).json()).studies).toHaveLength(1)
    expect((await (await post({ action: "compile-library", study_id: created.plan.id })).json()).error).toContain("Host remote-read authorization")
    // Updating an already-warm registration must use the host's new callback.
    installStudyHTTP(app, () => ({ directory, project: authority.project, authorize: async () => {},
      authorizeRemote: async () => { throw Error("remote authority denied by host") } }))
    const denied = await post({ action: "compile-library", study_id: created.plan.id })
    expect(denied.status).toBe(400)
    expect((await denied.json()).error).toBe("remote authority denied by host")
    authority.project = "project-b"
    expect((await post({ action: "get", study_id: created.plan.id })).status).toBe(400)
  } finally { await rm(directory, { recursive: true, force: true }) }
})
