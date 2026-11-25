import path from "node:path"
import { realpath } from "node:fs/promises"
import { createHash } from "node:crypto"
import { z } from "zod"

const sha = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex")

/** Host-selected evidence root. Validate source files before freezing the two
 * snapshots needed to recheck evidence identity inside the worker request. */
export async function verifyDeNovoEvidence(root: string, id: string, expected: string,
  target: { symbol: string; site: string; taxon: number; uniprot_id: string }, references: string[]) {
  if (!/^hyp-[a-f0-9]{24}$/.test(id)) throw Error("Invalid evidence identity")
  const directory = await realpath(root)
  const base = path.join(directory, "results/target-hypotheses", id)
  if (await realpath(base) !== base) throw Error("Evidence folder must not traverse symlinks")
  async function read(name: string) {
    const file = path.resolve(base, name)
    if (!file.startsWith(base + path.sep) || await realpath(file) !== file) throw Error("Unsafe evidence path")
    return new Uint8Array(await Bun.file(file).arrayBuffer())
  }
  const bytes = await read("manifest.json")
  if (sha(bytes) !== expected) throw Error("Evidence manifest changed")
  const manifest = z.object({ files: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)) }).passthrough().parse(JSON.parse(new TextDecoder().decode(bytes)))
  if (!manifest.files["evidence.json"]) throw Error("Manifest does not seal evidence.json")
  for (const [name, digest] of Object.entries(manifest.files)) {
    if (sha(await read(name)) !== digest) throw Error("Sealed evidence file changed: " + name)
  }
  const original = new TextDecoder().decode(await read("evidence.json"))
  const snapshots = { manifest_json: new TextDecoder().decode(bytes), evidence_json: original }
  verifyDeNovoEvidenceSnapshot(snapshots, expected, target, references)
  return snapshots
}

/** Validate frozen content before requesting compute, as well as during compilation. */
export function verifyDeNovoEvidenceSnapshot(snapshot: { manifest_json: string; evidence_json: string }, expected: string,
  target: { symbol: string; site: string; taxon: number; uniprot_id: string }, references: string[]) {
  if (sha(snapshot.manifest_json) !== expected) throw Error("Evidence manifest changed")
  const manifest = z.object({ files: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)) })
    .parse(JSON.parse(snapshot.manifest_json))
  if (sha(snapshot.evidence_json) !== manifest.files["evidence.json"]) throw Error("Sealed evidence content changed")
  const evidence = z.object({ target: z.string(), site: z.string(), taxon: z.number(),
    identity: z.object({ uniprot_id: z.string(), canonical_cys_verified: z.literal(true) }).passthrough(),
    literature: z.array(z.object({ id: z.string().optional() }).passthrough()).default([]),
    structures: z.array(z.object({ pdb_id: z.string().optional() }).passthrough()).default([]),
    existing_therapies: z.array(z.object({ molecule_chembl_id: z.string().optional() }).passthrough()).default([]),
    supplemental_sources: z.record(z.string(), z.unknown()).default({}),
  }).passthrough().parse(JSON.parse(snapshot.evidence_json))
  if (evidence.target !== target.symbol || evidence.site !== target.site || evidence.taxon !== target.taxon
    || evidence.identity.uniprot_id !== target.uniprot_id) throw Error("Evidence target differs from hypothesis")
  const ids = new Set([...evidence.literature.flatMap(item => item.id ? [item.id] : []),
    ...evidence.structures.flatMap(item => item.pdb_id ? ["PDB:" + item.pdb_id.toUpperCase()] : []),
    ...evidence.existing_therapies.flatMap(item => item.molecule_chembl_id ? ["ChEMBL:" + item.molecule_chembl_id] : []),
    ...Object.keys(evidence.supplemental_sources)])
  if (references.some(id => !ids.has(id))) throw Error("Proposal reference is absent from sealed evidence")
  return evidence
}
