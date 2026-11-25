/** Offline native schema inventory plus real in-memory MCP tools/list.
 * No tool execution, credentials, network requests or scientific jobs.
 */
import z from "zod"
import path from "node:path"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js"
process.env.OPENSCIENCE_DISABLE_MODELS_FETCH = "1"
// Initialize shared application globals before the cyclic tool modules.
await import("../src/global")
const { DeNovoTool } = await import("../src/tool/denovo")
const { MolecularDynamicsTool } = await import("../src/tool/molecular-dynamics")
const { studyTool } = await import("../integrations/discovery-platform/study-tool")
const { contract, capability } = await import("../integrations/inspire-screening/executor/mcp")
const { mcp } = await import("../integrations/inspire-covindb/server")
const legacy = await (await import("../integrations/inspire-screening/plugin")).default()
const compatibility = await (await import("../integrations/inspire-screening/plugin-v2")).default()

const output = process.argv[2]
if (!output) throw Error("An output JSON path is required")
const forbidden = async (): Promise<never> => { throw Error("Inventory cannot execute research actions") }
const study = studyTool("/inventory-only", "inventory-only", forbidden, forbidden, forbidden)
const native = await Promise.all([DeNovoTool, MolecularDynamicsTool].map(async tool => {
  const value = await tool.init()
  return { name: tool.id, description: value.description, inputSchema: z.toJSONSchema(value.parameters),
    origin: "native", source: `src/tool/${tool.id === "denovo_generate" ? "denovo" : "molecular-dynamics"}.ts` }
}))
const server = mcp()
const client = new Client({ name: "offline-research-inventory", version: "1" })
const transports = InMemoryTransport.createLinkedPair()
try {
  await Promise.all([server.connect(transports[0]), client.connect(transports[1])])
  const database = await client.listTools()
  const entries = [...native,
    { name: "discovery_study", description: study.description, inputSchema: z.toJSONSchema(z.object(study.args)),
      origin: "native_plugin", source: "integrations/discovery-platform/study-tool.ts" },
    ...[contract, capability].map(entry => ({ name: entry.name, description: entry.description,
      inputSchema: z.toJSONSchema(entry.schema), origin: "in_process_mcp",
      source: "integrations/inspire-screening/executor/mcp.ts" })),
    ...database.tools.map(entry => ({ ...entry, origin: "independent_mcp",
      source: "integrations/inspire-covindb/server.ts" })),
    ...Object.entries(legacy.tool).map(([name, entry]) => ({ name, description: entry.description,
      inputSchema: z.toJSONSchema(z.object(entry.args)), origin: "legacy_plugin",
      source: "integrations/inspire-screening/plugin.ts" })),
    ...Object.entries(compatibility.tool).map(([name, entry]) => ({ name, description: entry.description,
      inputSchema: z.toJSONSchema(z.object(entry.args)), origin: "legacy_plugin",
      source: "integrations/inspire-screening/plugin-v2.ts" })),
  ].map(entry => {
    const bytes = JSON.stringify(entry.inputSchema)
    return { ...entry, input_schema_bytes: Buffer.byteLength(bytes),
      input_schema_sha256: new Bun.CryptoHasher("sha256").update(bytes).digest("hex") }
  })
  await Bun.write(path.resolve(output), JSON.stringify({ schema: "native-research-tool-inventory-v1",
    scope: "Local source schemas, not a production connection or model-visibility snapshot",
    compute_submitted: false, tools_executed: false, entries }, null, 2) + "\n")
  console.log(`Inventoried ${entries.length} native/independent tools without execution`)
} finally {
  await client.close()
  await server.close()
}
