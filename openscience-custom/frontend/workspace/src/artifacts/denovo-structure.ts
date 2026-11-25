import { maestroGeometry, maestroOffsets, maestroTokens } from './maestro-preview'

export type BondSelection = { chain: string; residue: string; number: number; atom: string; element: string; ligandAtom: string; neighborAtom: string }

// Read the designated bond from the original pose, never infer it from distance.
export function denovoGeometry(text: string, index: number, id: string, site: BondSelection) {
  const offsets = maestroOffsets(text)
  const geometry = maestroGeometry(text, index, offsets)
  if (!id || geometry.denovoId !== id) throw new Error('De novo pose identity mismatch')
  const block = text.slice(offsets[index], offsets[index + 1] ?? text.length)
  const table = /\bm_bond\[(\d+)\]\s*\{([\s\S]*?):::([\s\S]*?):::\s*\}/.exec(block)
  if (!table) throw new Error('Covalent pose has no bond table')
  const fields = maestroTokens(table[2])
  const tokens = maestroTokens(table[3])
  if (tokens.length !== Number(table[1]) * (fields.length + 1) || ['i_m_from', 'i_m_to', 'i_m_order'].some(k => !fields.includes(k))) throw new Error('Incomplete covalent bond table')
  const bonds = Array.from({ length: Number(table[1]) }, (_, i) => {
    const row = tokens.slice(i * (fields.length + 1), (i + 1) * (fields.length + 1))
    const value = (key: string) => Number(row[fields.indexOf(key) + 1])
    return { from: value('i_m_from'), to: value('i_m_to'), order: value('i_m_order') }
  })
  const single = (predicate: (a: typeof geometry.atoms[number]) => boolean) => {
    const atoms = geometry.atoms.filter(predicate)
    if (atoms.length !== 1) throw new Error('Covalent atom missing or ambiguous')
    return atoms[0]
  }
  const sg = single(a => a.chain === site.chain && a.component === site.residue && a.residue === site.number && a.name === site.atom && a.element === site.element && !a.ligand)
  const beta = single(a => a.ligand && a.name === site.ligandAtom && a.element === 'C')
  const alpha = single(a => a.ligand && a.name === site.neighborAtom && a.element === 'C')
  const order = (a: number, b: number) => bonds.filter(x => (x.from === a && x.to === b) || (x.from === b && x.to === a))
  for (const [a, b] of [[sg.index, beta.index], [alpha.index, beta.index]]) {
    const matches = order(a, b)
    if (matches.length !== 1 || matches[0].order !== 1) throw new Error('Designated adduct bond is not a single bond')
  }
  const chains = [...new Set(geometry.atoms.map(a => a.chain))]
  const asym = (a: typeof sg) => a.ligand ? `LC${chains.indexOf(a.chain) + 1}_${a.residue}` : `C${chains.indexOf(a.chain) + 1}`
  const link = ['loop_', '_struct_conn.id', '_struct_conn.conn_type_id', '_struct_conn.ptnr1_label_asym_id', '_struct_conn.ptnr1_label_comp_id', '_struct_conn.ptnr1_label_seq_id', '_struct_conn.ptnr1_label_atom_id', '_struct_conn.ptnr1_auth_asym_id', '_struct_conn.ptnr1_auth_seq_id', '_struct_conn.ptnr2_label_asym_id', '_struct_conn.ptnr2_label_comp_id', '_struct_conn.ptnr2_label_seq_id', '_struct_conn.ptnr2_label_atom_id', '_struct_conn.ptnr2_auth_asym_id', '_struct_conn.ptnr2_auth_seq_id', '_struct_conn.pdbx_value_order',
    `cov1 covale ${asym(sg)} ${sg.component} ${sg.residue} ${sg.name} ${sg.chain} ${sg.residue} ${asym(beta)} ${beta.component} . ${beta.name} ${beta.chain} ${beta.residue} sing`, '#']
  return { ...geometry, site: { chain: sg.chain, residue: sg.component, residue_number: sg.residue, insertion_code: '', targetAtom: sg.name, ligandAtom: beta.name }, cif: geometry.cif + link.join('\n') + '\n' }
}
