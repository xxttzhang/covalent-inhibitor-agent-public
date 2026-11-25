import { Structure, StructureElement, StructureProperties as P, Unit } from "molstar/lib/mol-model/structure"
import { OrderedSet, SortedArray } from "molstar/lib/mol-data/int"
import type { PluginContext } from "molstar/lib/mol-plugin/context"
import { Color } from "molstar/lib/mol-util/color"
import { setSubtreeVisibility } from "molstar/lib/mol-plugin/behavior/static/state"
import type { TargetContext } from "@synsci/util/screening-target"

export type PocketAtom = { unit: number; index: number; name: string; element: string; xyz: number[] }
export type PocketResidue = { key: string; label: string; chain: string; number: number; insertionCode?: string; component: string; polymer: boolean; atoms: PocketAtom[] }
// Constructed only after validating the immutable result's residue and bond table.
export type StructureTargetMapping = { artifact_sha256: string; receptor_sha256: string; chain: string; residue: string; residue_number: number; insertion_code: string; targetAtom: string; ligandAtom: string }

export function pocketResidues(structure: Structure) {
  const residues = new Map<string, PocketResidue>()
  const location = StructureElement.Location.create(structure)
  for (const unit of structure.units) {
    if (!Unit.isAtomic(unit)) continue
    location.unit = unit
    for (let i = 0; i < unit.elements.length; i++) {
      location.element = unit.elements[i]
      const key = `${unit.id}:${P.residue.key(location)}`
      if (!residues.has(key)) {
        const chain = P.chain.auth_asym_id(location)
        const number = P.residue.auth_seq_id(location)
        const component = P.residue.label_comp_id(location)
        const insertionCode = P.residue.pdbx_PDB_ins_code(location) || ""
        residues.set(key, { key, chain, number, insertionCode, component, label: `${chain || "_"} / ${component} ${number}${insertionCode} [${unit.id}]`,
          polymer: P.entity.type(location) === "polymer", atoms: [] })
      }
      residues.get(key)!.atoms.push({ unit: unit.id, index: i, name: P.atom.label_atom_id(location), element: P.atom.type_symbol(location),
        xyz: [unit.conformation.x(location.element), unit.conformation.y(location.element), unit.conformation.z(location.element)] })
    }
  }
  return [...residues.values()]
}

const heavy = (atom: PocketAtom) => atom.element !== "H" && atom.element !== "D"
export function pocketLigands(residues: PocketResidue[]) {
  return residues.filter((r) => !r.polymer && !["HOH", "WAT", "DOD"].includes(r.component) && r.atoms.filter(heavy).length >= 3)
}

export function pocketNeighborhood(residues: PocketResidue[], ligand: PocketResidue, radius: number) {
  if (!Number.isFinite(radius) || radius < 2 || radius > 10) throw new Error("口袋半径须为 2–10 Å")
  const atoms = ligand.atoms.filter(heavy)
  const distance = (a: PocketAtom, b: PocketAtom) => Math.hypot(...a.xyz.map((v, i) => v - b.xyz[i]))
  const neighbors = residues.filter((r) => r.polymer && r.atoms.some((a) => heavy(a) && atoms.some((b) => distance(a, b) <= radius)))
  // Geometry-only candidates, never a canonical numbering assignment.
  const reactive = neighbors.filter((r) => r.component === "CYS" && r.atoms.some((a) => a.name === "SG" && atoms.some((b) => {
    const d = distance(a, b)
    return d >= 1.5 && d <= 2.2
  })))
  return { neighbors, reactive }
}

export function pocketTarget(residues: PocketResidue[], reactive: PocketResidue[], context?: TargetContext, mapping?: StructureTargetMapping) {
  const parameters = context?.parameters
  if (parameters) {
    const mapped = mapping && mapping.artifact_sha256 === context.artifact_sha256 && mapping.receptor_sha256 === parameters.receptor_sha256 &&
      mapping.chain === parameters.chain && mapping.residue === parameters.residue && mapping.residue_number === parameters.residue_number && mapping.insertion_code === parameters.insertion_code
    if (parameters.numbering !== "structure" || (parameters.receptor_sha256 !== context?.artifact_sha256 && !mapped))
      return { target: undefined, note: "任务位点尚未与此结果文件建立经核验的编号映射，请按结构标签手动选择；不自动套用受体编号。" }
    const matches = residues.filter((r) => r.polymer && r.chain === parameters.chain && r.component === parameters.residue &&
      r.number === parameters.residue_number && (r.insertionCode ?? "") === parameters.insertion_code)
    return { target: matches.length === 1 ? matches[0] : undefined,
      note: matches.length === 1 ? mapped ? "位点已按原始对接结构的残基与键表核验，保留来源受体校验值。" : "按任务参数与完全一致的结构文件匹配；不代表反应或结合活性验证。" : "任务位点在本结构中缺失或存在多个实例，请手动消歧。" }
  }
  return { target: reactive.length === 1 ? reactive[0] : undefined,
    note: "未提供完整任务位点映射；仅显示唯一的 CYS SG 邻近几何候选，不代表目标位点认定。" }
}

export async function drawPocket(p: PluginContext, ref: string, structure: Structure, ligand: PocketResidue, neighbors: PocketResidue[], target?: PocketResidue,
  link?: { targetAtom: string; ligandAtom: string }, recordedBond?: { targetAtom: string; ligandAtom: string }) {
  const refs: string[] = []
  const loci = (rows: PocketResidue[]) => {
    const units = new Map<number, number[]>()
    for (const row of rows) for (const atom of row.atoms) {
      if (!units.has(atom.unit)) units.set(atom.unit, [])
      units.get(atom.unit)!.push(atom.index)
    }
    return StructureElement.Loci(structure, [...units].map(([id, indices]) => ({ unit: structure.unitMap.get(id),
      indices: OrderedSet.ofSortedArray(SortedArray.ofSortedArray([...new Set(indices)].sort((a, b) => a - b) as StructureElement.UnitIndex[])) })))
  }
  try {
    for (const [key, rows, color] of [["neighbors", neighbors.filter((r) => r.key !== target?.key), 0x6eb8d0], ["ligand", [ligand], 0xe6b422], ["target", target ? [target] : [], 0xff5060]] as const) {
      if (!rows.length) continue
      const component = await p.builders.structure.tryCreateComponent(ref, {
        type: { name: "bundle", params: StructureElement.Bundle.fromLoci(loci([...rows])) }, nullIfEmpty: true, label: `Pocket ${key}`,
      }, `pocket-${key}`)
      if (!component) continue
      refs.push(component.ref)
      await p.builders.structure.representation.addRepresentation(component, { type: "ball-and-stick", color: "uniform", colorParams: { value: Color(color) } })
      if (key === "target") await p.builders.structure.representation.addRepresentation(component, {
        type: "label", typeParams: { level: "residue", background: true, backgroundColor: Color(0xffffff),
          backgroundOpacity: 0.85, sizeFactor: 1.4, offsetZ: 2, borderWidth: 0.1 },
        color: "uniform", colorParams: { value: Color(0xb31b32) },
      })
    }
    if (target && recordedBond) {
      const a = target.atoms.filter(atom => atom.name === recordedBond.targetAtom)
      const b = ligand.atoms.filter(atom => atom.name === recordedBond.ligandAtom)
      if (a.length !== 1 || b.length !== 1 || !structure.interUnitBonds.edges.some(edge =>
        edge.unitA === a[0].unit && edge.indexA === a[0].index && edge.unitB === b[0].unit && edge.indexB === b[0].index ||
        edge.unitB === a[0].unit && edge.indexB === a[0].index && edge.unitA === b[0].unit && edge.indexA === b[0].index))
        throw new Error('原始共价键未被三维查看器读取，拒绝用距离连线替代。')
      const component = await p.builders.structure.tryCreateComponent(ref, {
        type: { name: 'bundle', params: StructureElement.Bundle.fromLoci(loci([target, ligand])) }, nullIfEmpty: true, label: 'Recorded covalent bond',
      }, 'pocket-covalent-bond')
      if (!component) throw new Error('无法建立共价键显示组件。')
      refs.push(component.ref)
      await p.builders.structure.representation.addRepresentation(component, {
        type: 'ball-and-stick', typeParams: { visuals: ['inter-bond'] }, color: 'uniform', colorParams: { value: Color(0xff5060) },
      })
    }
    const root = p.managers.structure.hierarchy.current.structures.find((r) => r.cell.transform.ref === ref)
    for (const component of root?.components ?? []) {
      if (!refs.includes(component.cell.transform.ref)) setSubtreeVisibility(p.state.data, component.cell.transform.ref, true)
    }
    if (target && link) {
      const a = target.atoms.filter((atom) => atom.name === link.targetAtom)
      const b = ligand.atoms.filter((atom) => atom.name === link.ligandAtom)
      if (a.length !== 1 || b.length !== 1) throw new Error("反应原子不唯一，拒绝猜测连线端点。")
      const distance = Math.hypot(...a[0].xyz.map((x, i) => x - b[0].xyz[i]))
      const measurement = await p.managers.structure.measurement.addDistance(loci([{ ...target, atoms: a }]), loci([{ ...ligand, atoms: b }]), {
        customText: `SG ··· ${link.ligandAtom}  ${distance.toFixed(2)} Å (pre-reaction)`,
        visualParams: { linesColor: Color(0xd52b55), textColor: Color(0xb31b32), dashLength: 0.15, linesSize: 0.1,
          textSize: 0.65, background: true, backgroundColor: Color(0xffffff), backgroundOpacity: 0.9 },
      })
      if (measurement) refs.push(measurement.selection.ref)
    }
    p.managers.camera.focusLoci(loci([ligand, ...neighbors, ...(target ? [target] : [])]))
    return refs
  } catch (error) {
    const cleanup = p.state.data.build()
    for (const ref of refs) cleanup.delete(ref)
    await cleanup.commit()
    throw error
  }
}
