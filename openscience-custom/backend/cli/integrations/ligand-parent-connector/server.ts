import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { Server } from "@modelcontextprotocol/sdk/server/index.js"
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"
import { ListToolsRequestSchema, CallToolRequestSchema, ListResourcesRequestSchema, ListPromptsRequestSchema } from "@modelcontextprotocol/sdk/types.js"
import { authorized } from "../hypothesis-connector/server"
import { lstat } from "node:fs/promises"

// Operator-bound evidence and bounded local design connector. No model-supplied
// paths/commands, remote compute submission, release pointers or sandbox changes.
const filename = process.env.LIGAND_PARENT_CONNECTOR_KEY_FILE
const entry = process.env.LIGAND_PARENT_MCP_ENTRY
const python = process.env.LIGAND_PARENT_PYTHON
const project = process.env.COVSCREEN_PROJECT_ROOT
if (!filename || !entry || !python) throw Error("Explicit connector binding required")
const stat = await lstat(filename)
if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077)) throw Error("Owner-only key required")
const secret = (await Bun.file(filename).text()).trim()
if (secret.length < 32) throw Error("Connector key too short")
const port = Number(process.env.LIGAND_PARENT_PORT ?? 4102)
if (![4102, 4103].includes(port)) throw Error("Unregistered local connector port")
const listener = Bun.serve({ hostname: "127.0.0.1", port, idleTimeout: 255, maxRequestBodySize: 1_000_000,
  async fetch(request) {
    if (!authorized(request, secret, listener.port!)) return new Response("Unauthorized", { status: 401 })
    const pathname = new URL(request.url).pathname
    if (pathname === "/health") return Response.json({ healthy: true, service: "ligand-parent-evidence", remote_compute: false, local_bounded_graph_generation: true })
    if (pathname !== "/ligand-parent" || request.method !== "POST") return new Response("Not found", { status: 404 })
    const server = new Server({ name: "ligand-parent-evidence", version: "1.0.0" }, { capabilities: { tools: {}, resources: {}, prompts: {} } })
    const client = new Client({ name: "ligand-parent-evidence", version: "1.0.0" })
    const connected = { promise: undefined as Promise<void> | undefined }
    const ready = () => connected.promise ??= client.connect(new StdioClientTransport({ command: python, args: ["-B", entry], stderr: "inherit",
      env: project ? { COVSCREEN_PROJECT_ROOT: project } : undefined }))
    server.setRequestHandler(ListToolsRequestSchema, async () => { await ready(); return client.listTools() })
    server.setRequestHandler(CallToolRequestSchema, async message => {
      if (!["covalent_target_hypothesis", "covalent_ligand_design", "covalent_ligand_workflow",
        "covalent_ligand_evidence", "covalent_ligand_design_step"].includes(message.params.name)) throw Error("Unknown bounded design tool")
      await ready()
      return client.callTool(message.params, undefined, { timeout: 240000 })
    })
    server.setRequestHandler(ListResourcesRequestSchema, async () => ({ resources: [] }))
    server.setRequestHandler(ListPromptsRequestSchema, async () => ({ prompts: [] }))
    const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true })
    await server.connect(transport)
    try { return await transport.handleRequest(request) }
    finally { await client.close(); await server.close() }
  },
})
console.log(`Ligand parent/design connector listening on loopback:${port}`)
