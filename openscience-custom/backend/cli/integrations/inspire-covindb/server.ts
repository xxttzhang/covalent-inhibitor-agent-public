import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"
import { timingSafeEqual, randomBytes } from "node:crypto"
import { mkdir, readFile, writeFile, chmod } from "node:fs/promises"
import path from "node:path"
import z from "zod"

const root = path.resolve(import.meta.dir, "../../../../..")
const schema = z.object({
  cli: z.string().startsWith("/"),
  account: z.string().min(1),
  notebook: z.string().min(1),
  workspace: z.string().min(1),
  port: z.number().int().min(1024).max(65535),
  database_root: z.string().startsWith("/inspire/"),
}).strict()
export const querySchema = z.object({
  target: z.string().trim().min(1).max(160).optional().describe("Exact gene symbol or UniProt accession, e.g. EGFR or P00533; not the full protein name."),
  site: z.string().trim().min(1).max(20).optional().describe("Residue plus position, e.g. C797 / CYS-797. Not a mutation such as C797S."),
  inhibitor: z.string().trim().min(1).max(160).optional().describe("Exact CI database ID, or substring of an inhibitor name."),
  limit: z.number().int().min(1).max(50).default(20),
  offset: z.number().int().min(0).max(100000).default(0),
}).strict().refine((args) => !!(args.target || args.inhibitor), "Provide target or inhibitor.")
const annotations = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }
const quote = (value: string) => "'" + value.replaceAll("'", "'\"'\"'") + "'"
const settings = () => Bun.file(process.env.COVINDB_CONFIG ?? path.join(root, "configs/inspire-covindb.json")).json().then((value) => schema.parse(value))
const envelope = z.object({ success: z.boolean(), data: z.object({ returncode: z.number(), output: z.string() }).optional() })
let busy = false

async function token() {
  const directory = path.join(root, ".cache/inspire-covindb")
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const file = path.join(directory, "token")
  try {
    await writeFile(file, randomBytes(32).toString("hex"), { flag: "wx", mode: 0o600 })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
  }
  await chmod(file, 0o600)
  const value = (await readFile(file, "utf8")).trim()
  if (!/^[a-f0-9]{64}$/.test(value)) throw new Error("Invalid connector token file.")
  return value
}

async function remote(operation: "catalog" | "query", args: unknown) {
  if (busy) throw new Error("Another Inspire database query is running. Retry after it finishes.")
  busy = true
  try {
    const config = await settings()
    const source = await Bun.file(path.join(import.meta.dir, "query.py")).text()
    const command = "COVINDB_ROOT=" + quote(config.database_root) + " python3 -c " + quote(source) + " " + quote(JSON.stringify({ operation, arguments: args }))
    // No model-provided commands, filenames, stdin, credentials, or environment.
    const child = Bun.spawn([config.cli, "--json", "notebook", "exec", config.notebook,
      "--account", config.account, "--workspace", config.workspace, "--timeout", "45", command],
    { stdin: "ignore", stdout: "pipe", stderr: "pipe", env: process.env })
    const timer = setTimeout(() => child.kill("SIGKILL"), 85000)
    try {
      const [stdout, , exit] = await Promise.all([
        new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
      ])
      // Never forward raw CLI errors: they can contain session/proxy credentials.
      if (exit !== 0) throw new Error("Inspire read failed or timed out. Check the configured account login and running CPU Notebook; no database result was produced.")
      const parsed = envelope.safeParse(JSON.parse(stdout))
      if (!parsed.success || !parsed.data.success || parsed.data.data?.returncode !== 0) {
        throw new Error("Remote database read failed. Check Notebook connectivity, database path and schema. No fallback result was substituted.")
      }
      const result: unknown = JSON.parse(parsed.data.data.output)
      return result
    } finally {
      clearTimeout(timer)
    }
  } finally {
    busy = false
  }
}

export function mcp() {
  const server = new McpServer({ name: "inspire-covindb", version: "1.0.0" }, {
    instructions: "Query the user's Inspire CovInDB CSV snapshot with covindb_query before consulting the public CovalentInDB website. Do not substitute literature guesses for database records. This connector is read-only and never submits compute jobs.",
  })
  const call = async (operation: "catalog" | "query", args: unknown) => {
    try {
      const result = await remote(operation, args)
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }] }
    } catch (error) {
      const message = error instanceof Error && /^(Inspire read|Remote database|Another Inspire)/.test(error.message)
        ? error.message : "Database connector could not complete the request. Check local connector configuration; no result was fabricated."
      return { isError: true, content: [{ type: "text" as const, text: message }] }
    }
  }
  server.registerTool("covindb_catalog", {
    description: "Inspect the user's actual CovalentInDB / CovInDB database files on Inspire: paths, sizes, columns and query support. Use this instead of public-site web access. Read-only; no downloads or compute jobs.",
    inputSchema: z.object({}).strict(), annotations,
  }, () => call("catalog", {}))
  server.registerTool("covindb_query", {
    description: "Search the user's REMOTE CovalentInDB / CovInDB_All.csv on Inspire for known covalent inhibitors. For 'EGFR C797 共价抑制剂', use target='EGFR', site='C797'. Returns real CI IDs, source SHA256, total evidence rows/distinct inhibitor counts, original activity, references and SMILES, with pagination. Prefer this tool over public websites or literature guesses. Read-only; not virtual docking or full-library screening. A failed query is NOT an empty result.",
    inputSchema: querySchema, annotations,
  }, (args) => call("query", args))
  server.registerResource("database-guide", "covindb://guide", { mimeType: "text/plain", description: "Scope and interpretation of the remote database connector." }, async (uri) => ({
    contents: [{ uri: uri.href, text: "Use covindb_catalog to inspect remote CSV files. Use covindb_query with target=EGFR and site=C797 for real database evidence. All reads run through the configured Inspire account and existing CPU Notebook. Virtual/natural compound libraries are catalogued only; no target annotation is inferred. Report CI IDs, source path/hash and record counts. No-hit, connection failure, and literature inference must remain distinct." }],
  }))
  server.registerPrompt("query-covindb", { description: "Query the actual remote database, not the public website." }, () => ({
    messages: [{ role: "user", content: { type: "text", text: "请调用 covindb_query 查询 Inspire 上的 CovInDB：target=EGFR，site=C797，limit=10。报告真实 CI 编号、来源文件、匹配记录数及不同抑制剂数；保留活性单位和文献。不得用文献推测替代数据库结果。" } }],
  }))
  return server
}

export function authorize(request: Request, secret: string, port: number) {
  if (request.headers.get("host") !== `127.0.0.1:${port}` || request.headers.has("origin")) return false
  const actual = Buffer.from(request.headers.get("authorization") ?? "")
  const expected = Buffer.from(`Bearer ${secret}`)
  return actual.length === expected.length && timingSafeEqual(actual, expected)
}

export function start(secret: string, port: number) {
  const listener = Bun.serve({
    hostname: "127.0.0.1", port, idleTimeout: 120, maxRequestBodySize: 8192,
    async fetch(request) {
      const url = new URL(request.url)
      if (request.headers.get("host") !== `127.0.0.1:${listener.port}` || request.headers.has("origin")) return new Response("Forbidden", { status: 403 })
      if (url.pathname === "/health" && request.method === "GET") return Response.json({ service: "inspire-covindb", version: "1.0.0", healthy: true })
      if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 })
      if (!authorize(request, secret, listener.port!)) return new Response("Unauthorized", { status: 401 })
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: { Allow: "POST" } })
      const server = mcp()
      const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true })
      await server.connect(transport)
      try {
        return await transport.handleRequest(request)
      } finally {
        await server.close()
      }
    },
  })
  return listener
}

async function register(secret: string, port: number) {
  const base = "http://127.0.0.1:4096"
  const health = await fetch(`http://127.0.0.1:${port}/health`).then((r) => r.json())
  if (health.service !== "inspire-covindb" || !health.healthy) throw new Error("Connector is not healthy.")
  const url = `http://127.0.0.1:${port}/mcp`
  const current = await fetch(`${base}/global/config`).then((r) => r.json())
  const existing = current.mcp?.["inspire-covindb"]
  if (existing) {
    if (existing.type !== "remote" || existing.url !== url || existing.enabled === false) throw new Error("Existing connector configuration differs; refusing to overwrite it automatically.")
    const status = await fetch(`${base}/mcp`).then((r) => r.json())
    if (status["inspire-covindb"]?.status !== "connected") {
      const response = await fetch(`${base}/mcp/inspire-covindb/connect`, { method: "POST" })
      if (!response.ok) throw new Error("Could not reconnect database tools.")
    }
  } else {
    const status = await fetch(`${base}/session/status`).then((r) => r.json())
    if (Object.keys(status).length) throw new Error("Research sessions are active. Register after they finish to avoid interruption.")
    const response = await fetch(`${base}/mcp/inspire-covindb/config`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "global", config: {
        type: "remote", url, enabled: true, oauth: false, timeout: 100000,
        headers: { Authorization: `Bearer ${secret}` },
      } }),
    })
    if (!response.ok) throw new Error("Could not persist database connector configuration.")
  }
  const inspection = await fetch(`${base}/mcp/inspire-covindb`).then((r) => r.json())
  if (inspection.status?.status !== "connected" || !inspection.tools?.some((t: { name: string }) => t.name === "covindb_query")) throw new Error("Database tool discovery failed.")
  console.log("Inspire 数据库查询工具已连接（只读）。")
}

if (import.meta.main) {
  const config = await settings()
  const secret = await token()
  if (process.argv.includes("--register")) await register(secret, config.port)
  else {
    start(secret, config.port)
    console.log(`Inspire CovInDB read-only connector listening on 127.0.0.1:${config.port}`)
  }
}
