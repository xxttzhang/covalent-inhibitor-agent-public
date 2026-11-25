// Local CLI contract simulator. No network, resources, molecular engines or science evidence.
import { appendFileSync } from "node:fs"
const file = process.argv[1]
const data = await Bun.file(file + ".json").json()
const args = process.argv.slice(2)
appendFileSync(file + ".calls", JSON.stringify(args) + "\n")
if (JSON.stringify(args.slice(0, 11)) !== JSON.stringify(["--json", "--no-env-file", "--account", "fixture",
  "notebook", "exec", "fixture", "--workspace", "CPU资源空间", "--timeout", "90"])) throw new Error("Wrong bound fixture transport")
const positional = [...args[11].matchAll(/'([^']*)'/g)].map((match) => match[1]).slice(3)
const [action, task, expected] = positional
if (task !== data.task || (action === "verify" ? positional[3] : expected) !== data.digest) throw new Error("Wrong immutable fixture task")
const result = await (async () => {
  if (action === "verify") {
    const stage = await Bun.file(file + "." + positional[2] + ".json").json()
    const bytes = await Bun.file(file + ".artifact").arrayBuffer()
    if (new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== stage.artifacts["fixture.txt"]) throw new Error("Changed fixture artifact")
    return stage
  }
  if (action === "select-refinement") {
    const policy = JSON.parse(Buffer.from(positional[3], "base64").toString())
    if (JSON.stringify(policy) !== JSON.stringify(data.proposal.policy)) throw new Error("Different selection policy")
    return data.proposal
  }
  if (action === "read-selection") {
    if (positional[3] !== data.proposal.selection_id || positional[4] !== data.proposal.selection_manifest_digest) throw new Error("Different readback witness")
    if (data.readback_error) throw new Error("Readback unavailable")
    return data.readback ?? data.proposal
  }
  throw new Error("No creation, approval or fallback command allowed")
})()
process.stdout.write(JSON.stringify({ success: true, data: { returncode: 0, output: JSON.stringify(result) } }))
