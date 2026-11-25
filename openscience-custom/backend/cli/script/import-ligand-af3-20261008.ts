import path from "node:path"
import { open } from "node:fs/promises"
import { ArtifactStore } from "../src/artifact/store"

const root = path.resolve(import.meta.dir, "../../../..")
const folder = path.join(root, "reports/ligand-af3-recovery-20261008-v2/platform-delivery")
const original = await Bun.file(path.join(root, "reports/md-replicas-launch-20261008-v1/platform-receipt.json")).json()
const manifest = await Bun.file(path.join(folder, "manifest.json")).json()
const audit = await Bun.file(path.join(folder, "AF3_RAW_OUTPUT_AUDIT.json")).json()
if (manifest.verified_candidates !== 3 || manifest.actual_samples_per_candidate !== 1 ||
  manifest.final_priority_available !== false || audit.records.length !== 3 ||
  audit.status !== "THREE_ORIGINAL_AF3_PREDICTIONS_READ_AND_VERIFIED") throw Error("AF3 evidence required")
for (const [filename, digest] of Object.entries(manifest.files)) {
  const actual = new Bun.CryptoHasher("sha256").update(await Bun.file(path.join(folder, filename)).arrayBuffer()).digest("hex")
  if (actual !== digest) throw Error("Changed file: " + filename)
}
if (process.argv.includes("--apply")) {
  const reservation = await open(path.join(folder, "publication-reservation.json"), "wx")
  await reservation.writeFile(JSON.stringify({ created: new Date().toISOString(), run_id: manifest.run_id }))
  await reservation.close()
  const artifacts = []
  for (const filename of [...Object.keys(manifest.files), "manifest.json"]) {
    const mime = filename.endsWith(".md") ? "text/markdown" : filename.endsWith(".csv") ? "text/csv" :
      filename.endsWith(".gz") ? "application/gzip" : "application/json"
    artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
      sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
      kind: mime === "text/markdown" ? "document" : mime === "text/csv" ? "table" : "data",
      mimeType: mime, captureQuality: "declared",
      title: filename.endsWith(".md") ? "Ligand-first · 原三候选 AF3 新推理 · 3/3 完成" : "Ligand-first AF3 · " + filename }))
    await Bun.write(path.join(folder, "publication-partial.json"), JSON.stringify({ artifacts }, null, 2))
  }
  await Bun.write(path.join(folder, "platform-receipt.json"), JSON.stringify({ project: original.project,
    session: original.session, url: original.url, artifacts, run_id: manifest.run_id,
    verified_candidates: 3, browser_verified: false, goal_complete: false }, null, 2))
}
console.log(JSON.stringify({ verified_candidates: 3, wrote_platform: process.argv.includes("--apply") }))
