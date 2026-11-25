// Software-only transport fixture. Implements read commands and no scheduler.
import { appendFileSync } from "node:fs"
const file = process.argv[1]
const data = await Bun.file(file + ".json").json()
const args = process.argv.slice(2)
appendFileSync(file + ".calls", JSON.stringify(args) + "\n")
if (JSON.stringify(args.slice(0, 11)) !== JSON.stringify(["--json", "--no-env-file", "--account", "fixture",
  "notebook", "exec", "fixture", "--workspace", "CPU资源空间", "--timeout", "90"])) throw Error("Wrong fixture transport")
const positional = [...args[11].matchAll(/'([^']*)'/g)].map(match => match[1]).slice(3)
const result = (() => {
  if (positional[0] === "verify") {
    if (positional[1] !== data.parent || positional[3] !== data.digest) throw Error("Wrong core binding")
    return data.core[positional[2]]
  }
  const encoded = args[11].match(/'([A-Za-z0-9+/=]+)'$/)?.[1]
  if (!encoded) throw Error("Missing bounded packet")
  const packet = JSON.parse(Buffer.from(encoded, "base64").toString())
  const stage = data.stages[packet.task]
  if (packet.parent !== data.parent || !stage || packet.digest !== stage.staging.digest) throw Error("Wrong refinement binding")
  if (packet.action === "read") return stage.staging
  if (packet.action === "outputs") {
    if (data.unavailable) throw Error("Software fixture transport unavailable")
    return data.changed ? { ...stage.readback, manifest_sha256: "0".repeat(64) } : stage.readback
  }
  throw Error("Report read must not stage, approve or create work")
})()
process.stdout.write(JSON.stringify({ success: true, data: { returncode: 0, output: JSON.stringify(result) } }))
