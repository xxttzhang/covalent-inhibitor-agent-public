// The native raw endpoint has no extension. Infer format only from the saved
// version's filename, never the title or a model-provided URL.
export function molecularPreview(filename: string, size: number) {
  if (!Number.isSafeInteger(size) || size <= 0 || size > 8 * 1024 * 1024) return
  const extension = filename.split(".").at(-1)?.toLowerCase()
  if (extension === "cif" || extension === "mmcif") return { kind: "protein-structure", format: "mmcif" }
  if (extension === "pdb" || extension === "ent") return { kind: "protein-structure", format: "pdb" }
  if (extension === "sdf" || extension === "mol" || extension === "mol2" || extension === "xyz") return { kind: "chem-3d", format: extension }
}
