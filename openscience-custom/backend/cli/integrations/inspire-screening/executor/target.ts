import { targetParameters, targetSite, type TargetParameters } from "@synsci/util/screening-target"

// The installed worker still has one audited receptor/chemistry adapter.
// Parameterizing intent must NOT silently generalize that adapter to new targets.
export const registeredTarget = {
  target: "EGFR", receptor: "1M17", chain: "A", residue: "CYS", residue_number: 797,
  insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition",
} as const satisfies TargetParameters

export function targetAdmission(parameters: TargetParameters) {
  const p = targetParameters.parse(parameters)
  if (Object.entries(registeredTarget).every(([key, value]) => p[key as keyof TargetParameters] === value)
    && !p.receptor_sha256) return { supported: true as const }
  return { supported: false as const, reason: "任务参数已保存；该靶点/受体/编号/反应组合尚无已核验的执行适配器。需完成受体资产、位点映射及引擎验证；不会回退到 EGFR C797，也不会提交算力。" }
}

export function assertTargetBinding(target: string, site: string, parameters: TargetParameters) {
  if (target !== parameters.target || site !== targetSite(parameters)) throw new Error("任务靶点/位点与结构化参数不一致")
  const admission = targetAdmission(parameters)
  if (!admission.supported) throw new Error(admission.reason)
}
