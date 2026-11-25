// Geometry-only display conversion. Original Maestro bytes remain the download
// source; no energies, topology, stereochemistry or scientific QC are inferred.
const elements = "? H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split(" ")

export function maestroTokens(text: string) {
  const values: string[] = []
  for (let i = 0; i < text.length;) {
    if (/\s/.test(text[i])) { i++; continue }
    if (text[i] === "#") { while (i < text.length && text[i] !== "\n") i++; continue }
    const quoted = text[i] === '"'
    if (quoted) i++
    let value = ""
    let closed = !quoted
    while (i < text.length) {
      const char = text[i++]
      if (char === "\\") {
        if (i === text.length) throw new Error("Truncated Maestro escape")
        value += text[i++]; continue
      }
      if (quoted && char === '"') { closed = true; break }
      if (!quoted && /\s/.test(char)) break
      value += char
    }
    if (!closed) throw new Error("Unterminated Maestro string")
    values.push(value)
  }
  return values
}

export function maestroOffsets(text: string) {
  if (text.length > 192 * 1024 * 1024) throw new Error("Maestro preview exceeds decompressed limit")
  const offsets = [...text.matchAll(/^f_m_ct\s*\{/gm)].map((match) => match.index!)
  if (!offsets.length || offsets.length > 1000) throw new Error("Unsupported Maestro pose count")
  return offsets
}

const cif = (value: string) => {
  if (/[\r\n'"]/.test(value)) throw new Error("Unsupported atom label in display conversion")
  return `'${value}'`
}

export function maestroGeometry(text: string, index: number, offsets = maestroOffsets(text)) {
  if (!Number.isInteger(index) || index < 0 || index >= offsets.length) throw new Error("Unknown Maestro pose")
  const block = text.slice(offsets[index], offsets[index + 1] ?? text.length)
  const table = /\bm_atom\[(\d+)\]\s*\{([\s\S]*?):::([\s\S]*?):::\s*\}/.exec(block)
  if (!table) throw new Error("Maestro atom table missing")
  const count = Number(table[1])
  if (count < 1 || count > 100000) throw new Error("Unsupported atom count")
  const fields = maestroTokens(table[2])
  if (new Set(fields).size !== fields.length) throw new Error("Duplicate Maestro atom column")
  const tokens = maestroTokens(table[3])
  if (tokens.length !== count * (fields.length + 1)) throw new Error("Maestro atom table is incomplete")
  const required = ["r_m_x_coord", "r_m_y_coord", "r_m_z_coord", "i_m_atomic_number", "i_m_residue_number", "s_m_pdb_atom_name", "s_m_pdb_residue_name", "s_m_chain_name", "i_cdock_ligand_atom"]
  if (required.some((name) => !fields.includes(name))) throw new Error("Unsupported Maestro geometry columns")
  const header = block.slice(block.indexOf("{") + 1, table.index)
  const separator = header.indexOf(":::")
  const names = maestroTokens(header.slice(0, separator))
  // CT properties may be followed by dependency tables before m_atom.
  // Those nested table tokens are not scalar CT property values.
  const values = header.slice(separator + 3)
  const nested = /^\s*\w+\[\d+\]\s*\{/m.exec(values)
  const properties = maestroTokens(values.slice(0, nested?.index ?? values.length))
  if (separator < 0 || names.length !== properties.length) throw new Error("Maestro pose metadata is incomplete")
  const property = (name: string) => properties[names.indexOf(name)] ?? null
  const atoms = Array.from({ length: count }, (_, atom) => {
    const row = tokens.slice(atom * (fields.length + 1), (atom + 1) * (fields.length + 1))
    const value = (key: string) => row[fields.indexOf(key) + 1]
    if (Number(row[0]) !== atom + 1) throw new Error("Maestro atom order changed")
    const xyz = ["r_m_x_coord", "r_m_y_coord", "r_m_z_coord"].map((key) => Number(value(key)))
    const number = Number(value("i_m_atomic_number"))
    const residue = Number(value("i_m_residue_number"))
    if (xyz.some((n) => !Number.isFinite(n)) || !Number.isInteger(number) || !elements[number] || number < 1 || !Number.isInteger(residue)) throw new Error("Invalid Maestro atom geometry")
    const ligand = value("i_cdock_ligand_atom") === "1"
    const chain = value("s_m_chain_name").trim()
    return { index: atom + 1, xyz, element: elements[number], residue,
      name: value("s_m_pdb_atom_name").trim(), component: value("s_m_pdb_residue_name").trim(), chain, ligand }
  })
  const chains = [...new Set(atoms.map((atom) => atom.chain))]
  // Ligand membership flags may omit ligand hydrogens. Use the residue group
  // for display entities, while preserving every original coordinate/index.
  const residueKey = (atom: typeof atoms[number]) => JSON.stringify([atom.chain, atom.residue, atom.component])
  const ligandResidues = new Set(atoms.filter((atom) => atom.ligand).map(residueKey))
  const entity = (atom: typeof atoms[number]) => {
    const chain = `C${chains.indexOf(atom.chain) + 1}`
    return ligandResidues.has(residueKey(atom)) ? `L${chain}_${atom.residue}` : chain
  }
  const entities = new Map(atoms.map((atom) => [entity(atom), ligandResidues.has(residueKey(atom)) ? "non-polymer" : "polymer"]))
  const lines = ["data_maestro_geometry", "#", "loop_", "_entity.id", "_entity.type",
    ...[...entities].map(([id, type]) => `${id} ${type}`), "#", "loop_", ...[
    "group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id", "label_comp_id", "label_asym_id", "label_entity_id", "label_seq_id", "pdbx_PDB_ins_code",
    "Cartn_x", "Cartn_y", "Cartn_z", "occupancy", "B_iso_or_equiv", "auth_seq_id", "auth_comp_id", "auth_asym_id", "auth_atom_id", "pdbx_PDB_model_num",
  ].map((name) => `_atom_site.${name}`)]
  for (const atom of atoms) {
    const chain = `C${chains.indexOf(atom.chain) + 1}`
    const ligand = ligandResidues.has(residueKey(atom))
    lines.push([ligand ? "HETATM" : "ATOM", atom.index, atom.element, cif(atom.name), ".", cif(atom.component), ligand ? entity(atom) : chain,
      entity(atom), ligand ? "." : atom.residue, "?", ...atom.xyz, 1, 0,
      atom.residue, cif(atom.component), cif(atom.chain || "_"), cif(atom.name), 1].join(" "))
  }
  return { cif: lines.join("\n") + "\n#\n", atoms, pose: index, poseCount: offsets.length,
    inputIndex: property("i_sd_INPUT_SDF_INDEX"), compound: property("s_sd_COMPOUND_ID"), denovoId: property("s_m_denovo_id") }
}
