// Non-scientific test child. Never contact Inspire or a molecular engine.
import path from "node:path"

const [directory, payload, mode, delay] = process.argv.slice(2)
await Bun.sleep(Number(delay))
if (mode === "failed") process.exit(2)
const result = JSON.parse(payload)
for (const row of result.candidates) {
  const contents = `NON-SCIENTIFIC TEST STRUCTURE ${row.input_sdf_index}\n`
  await Bun.write(path.join(directory, row.structure), contents)
  result.artifacts[row.structure] = new Bun.CryptoHasher("sha256").update(contents).digest("hex")
}
if (mode === "wrong_identity") result.candidates[0].compound_id = "incorrect-test-identity"
await Bun.write(path.join(directory, "result.json"), JSON.stringify(result))
