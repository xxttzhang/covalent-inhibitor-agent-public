import { maestroGeometry } from "../src/artifacts/maestro-preview"
import { gzipPreview } from "../src/artifacts/gzip-preview"
import { verifyStructure } from "../src/artifacts/candidate-structure"

const manifest = await Bun.file("../../../science/reports/ligand_viewer_20260924/display_manifest.json").json()
for (const row of manifest.candidates) {
  const asset = manifest.assets[row.covdock_asset]
  const bytes = new Uint8Array(await Bun.file(asset.path).arrayBuffer())
  await verifyStructure(bytes, asset.sha256)
  const geometry = maestroGeometry(new TextDecoder().decode(await gzipPreview(bytes)), Number(row.covdock_structure_pose))
  if (geometry.compound !== row.compound_id || geometry.inputIndex !== row.input_sdf_index)
    throw new Error(`Candidate mismatch ${row.compound_id}: ${geometry.compound} / ${geometry.inputIndex}`)
  if (!geometry.atoms.some((atom) => atom.ligand) || !geometry.atoms.some((atom) => !atom.ligand)) throw new Error("Complex incomplete")
  if (row.af3_asset) {
    const af3 = manifest.assets[row.af3_asset]
    await verifyStructure(new Uint8Array(await Bun.file(af3.path).arrayBuffer()), af3.sha256)
  }
  console.log(JSON.stringify({ compound: row.compound_id, index: geometry.inputIndex, pose: geometry.pose, atoms: geometry.atoms.length,
    gate: row.covdock_gate_detail, af3: Boolean(row.af3_asset), verified: true }))
}
