// NON-SCIENTIFIC CLI simulator for transport tests. No network, GPU or engines.
import { appendFileSync } from "node:fs"
const file = process.argv[1] + ".json"
const data = await Bun.file(file).json()
const args = process.argv.slice(2)
appendFileSync(process.argv[1] + ".calls", JSON.stringify(args) + "\n")
if (JSON.stringify(args.slice(0, 4)) !== JSON.stringify(["--json", "--no-env-file", "--account", "fixture"])) {
  throw new Error("Fixture requires an explicitly bound account and disabled dotenv")
}
const [kind, action] = args.slice(4)
const value = (flag: string) => args[args.indexOf(flag) + 1]
const fail = (code: string) => { process.stdout.write(JSON.stringify({ success: false, error: { code } })); process.exit(2) }
if (action === "status" && data.error) fail(data.error)
const result = (() => {
  if (kind === "notebook" && action === "exec") {
    const encoded = args.at(-1)?.match(/'([A-Za-z0-9+/=]+)'$/)?.[1]
    if (!encoded) throw new Error("Fixture requires an encoded positional packet, not stdin")
    const packet = JSON.parse(Buffer.from(encoded, "base64").toString())
    if (packet.action === "outputs") {
      if (packet.evidence_sha256 && data.readback_error) fail(data.readback_error)
      return { returncode: data.readback_code ?? 0, output: JSON.stringify(data.readback) }
    }
    if (!["freeze", "read"].includes(packet.action)) throw new Error("Only file staging/readback is simulated")
    const receipt = packet.action === "read" ? data.staging_read ?? data.staging : data.staging
    return { returncode: data.staging_code ?? 0, output: data.staging_output ?? JSON.stringify(receipt) }
  }
  if (kind === "project" && action === "detail") return { name: data.spec.project, english_name: "fixture-namespace" }
  if (action === "quota") return { items: [{ compute_group: data.spec.group, quota: data.spec.quota,
    points_per_hour: data.price ?? data.spec.points_per_hour, priority: "any" }] }
  if (action === "create") {
    if (args.includes("--dry-run")) return { dry_run: true, name: value("--name") }
    if (data.lost) fail("ConnectionInterrupted") // Call was recorded but caller receives no successful reply.
    return { name: value("--name"), status: "QUEUING" }
  }
  if (action === "stop") return { name: args[6], status: "STOP_REQUESTED" } // Not a terminal observation.
  if (action === "status") {
    const [gpu, cpu, memory_gib] = data.spec.quota.split(",").map(Number)
    return { name: data.name, project: data.namespace ? "fixture-namespace" : data.spec.project,
      compute_group: data.spec.group, status: data.status ?? "RUNNING", steps: data.steps ?? "1/1",
      resource: { gpu, cpu, memory_gib, nodes: 1 }, ...data.override }
  }
  throw new Error("Unimplemented fixture CLI action: " + action)
})()
process.stdout.write(JSON.stringify({ success: true, data: result }))
