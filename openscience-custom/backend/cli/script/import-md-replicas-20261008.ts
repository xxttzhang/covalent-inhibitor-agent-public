import path from "node:path"
import { open } from "node:fs/promises"
import { ArtifactStore } from "../src/artifact/store"

// Run after actual trajectory analysis and scientific/figure review. Default is read-only.
const root = path.resolve(import.meta.dir, "../../../..")
const argument = process.argv.indexOf("--folder")
if (argument < 0 || !process.argv[argument + 1]) throw Error("Pass --folder <completed report directory> [--apply]")
const folder = path.resolve(process.argv[argument + 1])
const digest = async (filename: string) => new Bun.CryptoHasher("sha256")
  .update(await Bun.file(filename).arrayBuffer()).digest("hex")
const manifest = await Bun.file(path.join(folder, "manifest.json")).json()
const summary = await Bun.file(path.join(folder, "sampling-summary.json")).json()
const review = await Bun.file(path.join(folder, "scientific-review.json")).json()
const names = ["denovo_004", "denovo_001", "denovo_007", "control_afatinib"]
if (manifest.status !== "COMPLETE_REPLICA_EVIDENCE_RENDERED_REQUIRES_REVIEW" ||
  manifest.equilibrium_proven !== false || manifest.affinity_rank_available !== false ||
  summary.status !== "REPEATED_MD_SAMPLING_ASSESSED" || summary.replicas.length !== 12 ||
  summary.global_affinity_rank !== null || Object.keys(summary.systems).length !== 4)
  throw Error("Complete 12-replica evidence and bounded scientific claims required")
for (const name of names) {
  const replicas = summary.replicas.filter((run: { candidate: string }) => run.candidate === name)
  if (replicas.map((run: { replica: number }) => run.replica).sort().join() !== "1,2,3" ||
    summary.systems[name]?.equilibrium_proven !== false) throw Error("Invalid replicas: " + name)
}
if (review.source_manifest_sha256 !== await digest(path.join(folder, "manifest.json")) ||
  review.scientific_review_completed !== true || review.all_figures_visually_reviewed !== true ||
  typeof review.interpretation !== "string" || !review.interpretation.trim())
  throw Error("Record the actual scientific and visual review before publishing")
const files = Object.entries(manifest.files) as [string, string][]
for (const filename of ["REPEATED_MD_REPORT.md", "sampling-summary.json", "REPLICA_ESTIMATES.csv",
  "REPLICA_BLOCK_DIAGNOSTICS.csv", "REPLICA_TRACKED_HBONDS.csv"])
  if (!manifest.files[filename]) throw Error("Unbound report component: " + filename)
for (const [filename, expected] of files) {
  if (path.basename(filename) !== filename || await digest(path.join(folder, filename)) !== expected)
    throw Error("Changed report input: " + filename)
}
const markdown = "REPEATED_MD_REPORT.md"
const source = await Bun.file(path.join(folder, markdown)).text()
const references = [...source.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)].map((match) => match[1])
if (references.length !== 3 || references.some((name) => !manifest.figures.includes(name) || !manifest.files[name]))
  throw Error("Expected three bound report figures")
if (!process.argv.includes("--apply")) {
  console.log(JSON.stringify({ status: "REVIEWED_REPORT_READY_FOR_PUBLICATION", wrote_platform: false }))
  process.exit(0)
}
const original = await Bun.file(path.join(root, "reports/md-replicas-launch-20261008-v1/platform-receipt.json")).json()
const reservation = await open(path.join(folder, "platform-publication-reservation.json"), "wx")
await reservation.writeFile(JSON.stringify({ started: new Date().toISOString(), project: original.project }))
await reservation.close()
// Preserve a receipt after each write. On any failure inspect it; do not replay publication.
const artifacts: Awaited<ReturnType<typeof ArtifactStore.save>>[] = []
const urls = new Map<string, string>()
for (const filename of [...files.map(([name]) => name).filter((name) => name !== markdown), "manifest.json", "scientific-review.json"]) {
  const mime = filename.endsWith(".png") ? "image/png" : filename.endsWith(".svg") ? "image/svg+xml" :
    filename.endsWith(".csv") ? "text/csv" : "application/json"
  const artifact = await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
    sourcePath: path.join(folder, filename), filename, content: Bun.file(path.join(folder, filename)),
    kind: mime.startsWith("image/") ? "image" : mime === "text/csv" ? "table" : "data",
    mimeType: mime, captureQuality: "declared", title: "重复 MD · " + filename })
  artifacts.push(artifact)
  const url = new URL(`/file/artifact-store/${artifact.id}/raw`, original.url)
  url.searchParams.set("project", original.project)
  url.searchParams.set("versionID", artifact.currentVersionID)
  urls.set(filename, url.href)
  await Bun.write(path.join(folder, "platform-publication-partial.json"), JSON.stringify({ artifacts }, null, 2))
}
const rendered = source.replace(/(!\[[^\]]*\]\()([^)]+)(\))/g, (_, before, filename, after) => {
  const url = urls.get(filename)
  if (!url) throw Error("Missing published figure: " + filename)
  return before + url + after
})
// Keep the portable source report unchanged; the platform copy pins immutable image versions.
const published = "REPEATED_MD_REPORT_PLATFORM.md"
await Bun.write(path.join(folder, published), rendered)
artifacts.push(await ArtifactStore.save({ projectID: original.project, sessionID: original.session,
  sourcePath: path.join(folder, published), filename: published, content: Bun.file(path.join(folder, published)),
  kind: "document", mimeType: "text/markdown", captureQuality: "declared",
  title: "重复 MD · 四体系三重复 · 采样评估与不确定性" }))
await Bun.write(path.join(folder, "platform-receipt.json"), JSON.stringify({ project: original.project,
  session: original.session, url: original.url, artifacts, verified_trajectory_count: 12,
  source_report_sha256: await digest(path.join(folder, markdown)),
  platform_report_sha256: await digest(path.join(folder, published)),
  browser_verified: false, goal_complete: false }, null, 2))
console.log(JSON.stringify({ status: "PUBLISHED_REQUIRES_DOWNLOAD_AND_BROWSER_VERIFICATION", artifacts: artifacts.length }))
