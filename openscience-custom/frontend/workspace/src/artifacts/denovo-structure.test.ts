import { expect, test } from 'bun:test'
import path from 'node:path'
import { gunzipSync } from 'node:zlib'
import { denovoGeometry } from './denovo-structure'
import { candidateTable } from './candidate-table'
import { maestroGeometry } from './maestro-preview'
import { pocketTarget, type PocketResidue } from '../science/renderers/molecular/pocket'
import { targetContext } from '@synsci/util/screening-target'

const study = path.resolve(import.meta.dir, '../../../../../reports/denovo-covdock-20260929')
test('all seven real best poses retain exact identity, coordinates and the recorded C797 bond', async () => {
  const comparison = await Bun.file(path.join(study, 'comparison.json')).json()
  const table = candidateTable(await Bun.file(path.join(study, 'platform-comparison-v1/denovo_comparison.csv')).text())!
  for (const row of comparison.records.filter((r: { id: string }) => r.id.startsWith('denovo_'))) {
    const text = gunzipSync(await Bun.file(path.join(study, row.id, `${row.id}-out.maegz`)).arrayBuffer()).toString()
    const geometry = denovoGeometry(text, row.pose_index_0based, row.id, { chain: "A", residue: "CYS", number: 797, atom: "SG", element: "S", ligandAtom: "WBC", neighborAtom: "WAC" })
    expect(geometry.atoms).toEqual(maestroGeometry(text, row.pose_index_0based).atoms)
    expect(geometry.atoms.length).toBeGreaterThan(5000)
    expect(geometry.cif).toContain('_struct_conn.conn_type_id')
    expect(geometry.cif).toContain('cov1 covale')
    expect(() => denovoGeometry(text, row.pose_index_0based, 'denovo_999', { chain: "A", residue: "CYS", number: 797, atom: "SG", element: "S", ligandAtom: "WBC", neighborAtom: "WAC" })).toThrow('identity')
    expect(() => denovoGeometry(text.replaceAll('i_m_order', 'missing_bond_order'), row.pose_index_0based, row.id, { chain: "A", residue: "CYS", number: 797, atom: "SG", element: "S", ligandAtom: "WBC", neighborAtom: "WAC" })).toThrow('bond table')
    expect(() => denovoGeometry(text, 100, row.id, { chain: "A", residue: "CYS", number: 797, atom: "SG", element: "S", ligandAtom: "WBC", neighborAtom: "WAC" })).toThrow('pose')
    const context = targetContext.parse(JSON.parse(table.rows.find(r => r.compound_id === row.id)!.covdock_target_context))
    const mapping = { ...geometry.site, artifact_sha256: context.artifact_sha256, receptor_sha256: context.parameters!.receptor_sha256! }
    const residue: PocketResidue = { key: 'target', label: 'A/CYS797', chain: geometry.site.chain, number: geometry.site.residue_number, component: geometry.site.residue, insertionCode: '', polymer: true, atoms: [] }
    expect(pocketTarget([residue], [], context).target).toBeUndefined()
    expect(pocketTarget([residue], [], context, mapping).target).toEqual(residue)
    expect(pocketTarget([residue], [], context, { ...mapping, artifact_sha256: '0'.repeat(64) }).target).toBeUndefined()
    expect(pocketTarget([residue], [], context, { ...mapping, receptor_sha256: '0'.repeat(64) }).target).toBeUndefined()
    expect(pocketTarget([residue], [], context, { ...mapping, residue_number: 798 }).target).toBeUndefined()
  }
})
