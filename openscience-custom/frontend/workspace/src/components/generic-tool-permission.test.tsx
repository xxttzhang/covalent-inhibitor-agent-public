import { afterAll, afterEach, describe, expect, test } from "bun:test"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"
import solid from "vite-plugin-solid"

const cleanups: Array<() => void> = []
const server = await createServer({
  root: fileURLToPath(new URL("../..", import.meta.url)),
  mode: "production",
  logLevel: "silent",
  plugins: [solid({ ssr: false, dev: false })],
  server: { middlewareMode: true },
  appType: "custom",
  resolve: { conditions: ["browser", "production"], dedupe: ["solid-js", "solid-js/web"] },
  ssr: {
    noExternal: true,
    resolve: { conditions: ["browser", "production"] },
  },
})
const [subject, web] = await Promise.all([
  server.ssrLoadModule("@synsci/ui/basic-tool") as Promise<typeof import("@synsci/ui/basic-tool")>,
  server.ssrLoadModule("solid-js/web") as Promise<typeof import("solid-js/web")>,
])

afterAll(() => server.close())

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  document.body.replaceChildren()
})

test("pending generic MCP permission shows exact arguments before any output, even in compact mode", () => {
  const host = document.createElement("div")
  document.body.append(host)
  const input = { action: "collect", target: "EGFR", scope: { taxon: 9606, site: "C797" } }
  cleanups.push(web.render(() => subject.GenericTool({
    tool: "covalent-discovery_covalent_ligand_evidence", input,
    status: "running", locked: true, forceOpen: true, hideDetails: true,
  }), host))
  const parameters = host.querySelector('[aria-label="Tool request parameters"]')
  expect(parameters).not.toBeNull()
  expect(JSON.parse(parameters!.textContent!)).toEqual(input)
  expect(host.querySelector('[data-slot="basic-tool-tool-subtitle"]')?.textContent).toBe("collect")
})
