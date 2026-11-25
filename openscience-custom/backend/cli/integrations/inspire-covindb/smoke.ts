// Explicit live read-only acceptance test. No model API calls or compute jobs.
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import path from "node:path"

const root = path.resolve(import.meta.dir, "../../../../..")
const config = await Bun.file(path.join(root, "configs/inspire-covindb.json")).json()
const secret = (await Bun.file(path.join(root, ".cache/inspire-covindb/token")).text()).trim()
const client = new Client({ name: "covindb-acceptance", version: "1.0.0" })
const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${config.port}/mcp`), {
  requestInit: { headers: { Authorization: `Bearer ${secret}` } },
})
try {
  await client.connect(transport)
  const report: Record<string, unknown> = { tested_utc: new Date().toISOString() }
  for (const [name, args] of [
    ["covindb_catalog", {}],
    ["covindb_query", { target: "EGFR", site: "C797", limit: 3 }],
  ] as const) {
    const response = await client.callTool({ name, arguments: args }, undefined, { timeout: 100000 })
    if (response.isError) throw new Error(JSON.stringify(response.content))
    const content = response.content as { type: string; text?: string }[]
    report[name] = JSON.parse(content.find((item) => item.type === "text")!.text!)
  }
  const result = report.covindb_query as { total_matching_records: number; unique_inhibitors: number; records: { Inhibitor: string }[] }
  if (!result.total_matching_records || !result.records.every((row) => /^CI\d+$/.test(row.Inhibitor))) throw new Error("Expected real EGFR C797 database IDs.")
  const target = path.join(root, "reports/inspire-covindb-acceptance.json")
  await Bun.write(target, JSON.stringify(report, null, 2) + "\n")
  console.log(JSON.stringify({ report: target, total_matching_records: result.total_matching_records,
    unique_inhibitors: result.unique_inhibitors, sample_ids: result.records.map((row) => row.Inhibitor) }))
} finally {
  await client.close()
}
