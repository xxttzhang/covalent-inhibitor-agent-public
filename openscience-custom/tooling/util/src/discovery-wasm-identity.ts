import { z } from "zod"

type Molecule = { is_valid(): boolean; get_smiles(): string; get_json(): string; get_stereo_tags(): string; delete(): void }
type Chemistry = { get_mol(input: string, options?: string): Molecule | null }
const graph = z.object({ molecules: z.array(z.object({
  atoms: z.array(z.object({ z: z.number().default(6), impHs: z.number().default(0), nRad: z.number().default(0) })),
  bonds: z.array(z.object({ atoms: z.tuple([z.number(), z.number()]), bo: z.number().default(1), stereo: z.string().default("unspecified") })),
})) })
const tags = z.object({ CIP_atoms: z.array(z.tuple([z.number(), z.string()])), CIP_bonds: z.array(z.tuple([z.number(), z.number(), z.string()])) })

/** Runs inside a bounded worker. Conservative molecular identity, not chemical standardization. */
export function wasmIdentity(rdkit: Chemistry, text: string) {
  const invalid = { state: "invalid" as const, canonical: null, reason: "Unsupported or invalid SMILES" }
  if (!text || text.length > 20000 || /\s|\||>/.test(text)) return invalid
  const mol = rdkit.get_mol(text, JSON.stringify({ sanitize: true, removeHs: true }))
  if (!mol) return invalid
  try {
    if (!mol.is_valid()) return invalid
    // Atom maps are bookkeeping labels, not molecular identity. Reparse after removing them.
    const clean = rdkit.get_mol(mol.get_smiles().replace(/:\d+\]/g, "]"))
    if (!clean) return invalid
    try {
      const canonical = clean.get_smiles()
      const data = graph.parse(JSON.parse(clean.get_json()))
      if (!data.molecules.length || data.molecules.some(m => !m.atoms.length)) return invalid
      const unresolved = (reason: string) => ({ state: "unresolved" as const, canonical, reason })
      if (data.molecules.some(m => m.atoms.some(a => a.z === 0 || a.nRad !== 0))) return unresolved("Wildcard/radical identity kept separate")
      const stereo = tags.parse(JSON.parse(clean.get_stereo_tags()))
      if (stereo.CIP_atoms.some(item => item[1].includes("?"))) return unresolved("Unspecified atom stereochemistry: kept separate")
      // MinimalLib's CIP tags omit unspecified E/Z. Conservatively reject any unassigned
      // double bond with two substituents at both ends; symmetrical/ring cases may over-reject.
      for (const molecule of data.molecules) {
        const degrees = molecule.atoms.map(() => 0)
        for (const bond of molecule.bonds) for (const index of bond.atoms) degrees[index]++
        for (const bond of molecule.bonds) {
          if (bond.bo !== 2 || !["unspecified", "either"].includes(bond.stereo)) continue
          if (bond.atoms.every(index => {
            const atom = molecule.atoms[index]
            return atom.impHs < 2 && degrees[index] - 1 + atom.impHs >= 2
          })) return unresolved("Potential unspecified double-bond stereochemistry: kept separate")
        }
      }
      return { state: "validated" as const, canonical,
        reason: "RDKit exact isomeric graph; no salt, charge, isotope or tautomer normalization" }
    } finally { clean.delete() }
  } catch { return { state: "unresolved" as const, canonical: null, reason: "Chemistry validation failed; kept separate" } }
  finally { mol.delete() }
}
