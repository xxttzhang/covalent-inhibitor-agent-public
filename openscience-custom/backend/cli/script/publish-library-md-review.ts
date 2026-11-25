// Append the Codex review without replacing the frozen automatic report.
import path from "node:path"
import { ArtifactStore } from "../src/artifact/store"

const root = path.resolve(import.meta.dir, "../../../..")
const batch = path.join(root, "reports/library-md-20261008-v1")
const review = path.join(batch, "review-v1")
const receipt = path.join(review, "publication-receipt.json")
if (await Bun.file(receipt).exists()) throw Error("Review already published; inspect its immutable versions")
const audit = await Bun.file(path.join(review, "verification.json")).json()
if (audit.status !== "COMPUTATION_ANALYSIS_DELIVERY_VERIFIED" || audit.replicas !== 24 || audit.platform_artifacts_read_back !== 62) throw Error("Full batch verification required")
const source = await Bun.file(path.join(batch, "platform/publication-receipt.json")).json()
const artifacts = []
for (const filename of ["SCIENTIFIC_REVIEW.md", "system-summary.csv", "verification.json"]) {
  const extension = path.extname(filename)
  artifacts.push(await ArtifactStore.save({
    projectID: source.project,
    sessionID: source.session,
    sourcePath: path.join(review, filename),
    filename,
    title: "Library-first MD · Codex复核 · " + filename,
    content: Bun.file(path.join(review, filename)),
    mimeType: extension === ".md" ? "text/markdown" : extension === ".csv" ? "text/csv" : "application/json",
    kind: extension === ".md" ? "document" : extension === ".csv" ? "table" : "data",
    captureQuality: "declared",
  }))
}
await Bun.write(receipt, JSON.stringify({
  project: source.project, session: source.session, artifacts,
  review: "Codex evidence review; independent human expert review not claimed",
  preservesAutomaticPackage: true, convergenceEstablished: false,
}, null, 2))
console.log(JSON.stringify({ publishedReviewArtifacts: artifacts.length }))
