// Fixed, unweighted objectives; missing evidence is never replaced by zero.
export type ParetoRow = { input_sdf_index: number; compound_id: string; status: string;
  af3: { score: number | null }; covdock: { measurements: { strain_kcal_mol: number | null } } }

export function refinementPareto(rows: ParetoRow[]) {
  if (new Set(rows.map((r) => r.input_sdf_index)).size !== rows.length
    || new Set(rows.map((r) => r.compound_id)).size !== rows.length) throw new Error('Duplicate Pareto identity')
  const ranked = rows.map((r) => ({ input_sdf_index: r.input_sdf_index, compound_id: r.compound_id,
    af3_mpae: r.af3.score, strain_kcal_mol: r.covdock.measurements.strain_kcal_mol,
    eligible: r.status === 'BOTH_STRUCTURAL_QC_PASSED' && typeof r.af3.score === 'number' && Number.isFinite(r.af3.score)
      && typeof r.covdock.measurements.strain_kcal_mol === 'number' && Number.isFinite(r.covdock.measurements.strain_kcal_mol),
    source_status: r.status, pareto_front: null as number | null, display_order: null as number | null }))
  const remaining = new Set(ranked.filter((r) => r.eligible))
  for (const front of Array.from({ length: remaining.size }, (_, i) => i + 1)) {
    if (!remaining.size) break
    const members = [...remaining].filter((a) => ![...remaining].some((b) => b !== a
      && b.af3_mpae! <= a.af3_mpae! && b.strain_kcal_mol! <= a.strain_kcal_mol!
      && (b.af3_mpae! < a.af3_mpae! || b.strain_kcal_mol! < a.strain_kcal_mol!)))
    members.forEach((r) => { r.pareto_front = front; remaining.delete(r) })
  }
  ranked.sort((a, b) => (a.pareto_front ?? Infinity) - (b.pareto_front ?? Infinity) || a.input_sdf_index - b.input_sdf_index)
  ranked.filter((r) => r.eligible).forEach((r, i) => { r.display_order = i + 1 })
  return { policy: 'af3-strain-pareto-v1', objectives: { af3_mpae: 'min', strain_kcal_mol: 'min' },
    note: '仅对双引擎结构质检通过且两目标完整者分层；同层按输入索引稳定展示，不代表同层内优劣。未纳入化学风险、ADMET或实验活性；不等同于科研端完整多属性 Pareto。',
    candidates: ranked }
}
