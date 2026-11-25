// Publish only the registered, complete, hash-verified Library-first batch.
import path from "node:path"
import { ArtifactStore } from "../src/artifact/store"

const root = path.resolve(import.meta.dir, "../../../..")
const folder = path.join(root, "reports/library-md-20261008-v1")
const output = path.join(folder, "analysis-v1")
const state = path.join(folder, "platform")
const receipt = path.join(state, "publication-receipt.json")
const hash = (bytes: Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
const manifest = await Bun.file(path.join(output, "manifest.json")).json()
if (manifest.schema !== "md-analysis-package-v1" || manifest.replicas !== 24 || manifest.frames !== 24000 || manifest.charts.length !== 57 || manifest.review !== "automatic-report-needs-review") throw Error("Complete audited Library-first report required")
for (const [name, expected] of Object.entries(manifest.files)) {
  if (path.basename(name) !== name || hash(new Uint8Array(await Bun.file(path.join(output, name)).arrayBuffer())) !== expected) throw Error("Result hash mismatch: " + name)
}
const source = await Bun.file(path.join(root, "reports/library-consistency-20261001/platform-receipt.json")).json()
const reservation = await Bun.file(path.join(state, "submission-reservation.json")).json()
const native = reservation.approval.mechanism === "native_remote_compute"
const project = native ? reservation.approval.project_id : source.project
const session = native ? reservation.approval.session_id : source.session
if (!project || !session) throw Error("Missing original platform destination")
const selected = ["report.md", "replica-means.csv", "provenance.json", "manifest.json", "analysis-bundle.zip", ...manifest.charts.map((chart: { file: string }) => chart.file)]
const artifacts = []
for (const filename of selected) {
  const content = Bun.file(path.join(output, filename))
  const extension = path.extname(filename)
  const mime = extension === ".md" ? "text/markdown" : extension === ".csv" ? "text/csv" : extension === ".png" ? "image/png" : extension === ".zip" ? "application/zip" : "application/json"
  artifacts.push(await ArtifactStore.save({ projectID: project, sessionID: session,
    sourcePath: path.join(output, filename), filename, content, mimeType: mime,
    kind: extension === ".png" ? "figure" : extension === ".md" ? "document" : extension === ".csv" ? "table" : "data",
    captureQuality: "declared", title: "Library-first MD · " + filename }))
  await Bun.write(path.join(state, "publication-partial.json"), JSON.stringify({ project, session, artifacts }, null, 2))
}
await Bun.write(receipt, JSON.stringify({ project, session, url: `http://127.0.0.1:4096/${project}/session/${session}`, artifacts,
  replicas: 24, production_ns: 240, scientific_review: "pending", affinity_proven: false }, null, 2))
console.log(JSON.stringify({ published: artifacts.length, replicas: 24, scientific_review: "pending" }))
