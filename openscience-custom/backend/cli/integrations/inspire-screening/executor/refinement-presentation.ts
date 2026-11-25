import type { refinementProposalSnapshot } from "./refinement-proposal"

// Presentation is derived from verified proposal fields, never from model prose.
// It does not change the frozen proposal, selection policy, or approval identity.
export function presentRefinement(snapshot: ReturnType<typeof refinementProposalSnapshot>) {
  if (!snapshot || snapshot.state !== "AVAILABLE") return snapshot
  const proposal = snapshot.proposal
  return { ...snapshot, interpretation: {
    rank_definition: "两种排名分位均越小越靠前，0 表示最佳排名端点、1 表示最末端；并列采用平均排名。排名分位不是原始得分，0 不表示零分或无活性。",
    candidates: proposal.selection.input.candidates.map((row) => ({
      input_sdf_index: row.input_sdf_index, compound_id: row.compound_id,
      selection_channel: row.selection_channel,
      relative_rank: row.carsidock_rank_fraction === row.rtmscore_rank_fraction ? "两引擎排名分位相同"
        : row.carsidock_rank_fraction < row.rtmscore_rank_fraction ? "CarsiDock 排名较靠前，RTMScore 排名较靠后"
          : "RTMScore 排名较靠前，CarsiDock 排名较靠后",
    })),
    failures: proposal.quality_policy.policy.version === "refinement-structural-qc-v2-provisional"
      ? "引擎执行失败保留 FAILED / NOT_EVALUATED；执行成功时独立报告 physical_validity、evidence_complete、review_eligible。缺失证据为 UNKNOWN，实测不通过才为 FAIL；历史准入字段 REJECTED 不等于物理失败，不得删除候选或解释为无活性。"
      : "引擎执行失败保留 FAILED / NOT_EVALUATED；执行成功但必要测量缺失或质检未通过保留 SUCCESS / REJECTED。不得混成成功结果或删除。",
    approval: proposal.continuation?.kind === "recovery"
      ? "此草案未获批准。保留已完成 AF3 的原计划与结果，仅为 CovDock 续跑申请新的原生审批，不重复提交 AF3。"
      : "此草案未获批准。条件具备后，一张两阶段原生审批卡统一确认候选、资源、时限及质检规则，不另设一次质检审批。",
  } }
}
