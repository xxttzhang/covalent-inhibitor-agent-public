import path from "node:path"
import { ArtifactStore } from "../openscience-custom/backend/cli/src/artifact/store"
import { candidateTable } from "../openscience-custom/frontend/workspace/src/artifacts/candidate-table"

const root = path.resolve(import.meta.dir, "..")
const folder = path.join(root, "reports/three-route-workbench-20261008")
const af3 = path.join(root, "reports/ligand-af3-recovery-20261008-v2")
const retained = path.join(root, "reports/ligand-covdock-serial-retry-clean-20261008/retained-066-capsule")
const digest = (value: Uint8Array) => new Bun.CryptoHasher("sha256").update(value).digest("hex")
const manifest = await Bun.file(path.join(af3, "platform-delivery/manifest.json")).json()
const auditFile = Bun.file(path.join(af3, "platform-delivery/AF3_RAW_OUTPUT_AUDIT.json"))
if (digest(new Uint8Array(await auditFile.arrayBuffer())) !== manifest.files["AF3_RAW_OUTPUT_AUDIT.json"]) throw Error("AF3 audit mismatch")
const audit = await auditFile.json()
const tableFile = Bun.file(path.join(af3, "verified-evidence/artifacts/stage4_top_candidates.csv"))
if (digest(new Uint8Array(await tableFile.arrayBuffer())) !== audit.source_files["artifacts/stage4_top_candidates.csv"]) throw Error("Candidate table mismatch")
const rows = candidateTable(await tableFile.text())?.rows
if (!rows || rows.length !== 3) throw Error("Expected three AF3 candidates")
const sources = rows.map(row => {
  const name = Object.keys(audit.source_files).find(name => name.endsWith("_model.cif") && name.includes(row.compound_id + "__"))
  if (!name) throw Error("Missing AF3 model")
  return { compound: row.compound_id, method: "af3", file: path.join(af3, "verified-evidence", name), sha256: audit.source_files[name] as string }
})
const dock = await Bun.file(path.join(retained, "covdock_lo_registered_manifest.json")).json()
sources.push({ compound: "fragment_000001_product_000066", method: "covdock", file: path.join(retained, "covdock_lo/library_first_af3_top_covdock_lo-out.maegz"), sha256: dock.pose_sha256 })
for (const source of sources) if (digest(new Uint8Array(await Bun.file(source.file).arrayBuffer())) !== source.sha256) throw Error("Structure mismatch: " + source.compound)
const receipt = path.join(folder, "structures.json")
if (await Bun.file(receipt).exists()) {
  const previous = await Bun.file(receipt).json() as { structures: { compound: string; method: string; artifact: { current: { sha256: string } } }[] }
  if (sources.every(source => previous.structures.some(ref => ref.compound === source.compound && ref.method === source.method && ref.artifact.current.sha256 === source.sha256))) {
    console.log(JSON.stringify({ reused: previous.structures.length })); process.exit(0)
  }
  throw Error("Existing structure registration differs; retain its provenance")
}
const structures = []
for (const source of sources) {
  const artifact = await ArtifactStore.save({ projectID: "prj_dbfd2a34da2e4c78977238fcf59d39ae", sessionID: "ses_f14d8c61dffePbcbRvvucbLg1I",
    sourcePath: source.file, filename: path.basename(source.file), content: Bun.file(source.file), kind: "file",
    mimeType: source.method === "af3" ? "chemical/x-mmcif" : "application/gzip", captureQuality: "declared",
    title: `Ligand-first · ${source.compound} · ${source.method === "af3" ? "本轮 AF3 原始结构（几何待审阅）" : "066 超时任务保留的 10 个原始姿势"}` })
  structures.push({ compound: source.compound, method: source.method, artifact })
  await Bun.write(receipt, JSON.stringify({ structures, newComputeSubmitted: false }, null, 2))
}
console.log(JSON.stringify({ registered: structures.length, newComputeSubmitted: false }))
