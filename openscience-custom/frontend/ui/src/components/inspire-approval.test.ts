import { expect, test } from "bun:test"
import { inspireDetails } from "./inspire-approval"

test('new refinement approval distinguishes full scientific Pareto from legacy and absent ranking', () => {
  const rows = Object.fromEntries(inspireDetails({ parent_task_id: 'new-task', delivery_policy: { final_ranking: 'library-first-pareto-v2.0' } }))
  expect(rows['最终排名与总览']).toContain('构象 RMSD↓')
  expect(rows['最终排名与总览']).toContain('失败/缺测保留')
  expect(Object.fromEntries(inspireDetails({ parent_task_id: 'old-task' }))['最终排名与总览']).toContain('不追加到旧审批')
})

test("multi-stage approval shows each rate, workspace and time without undefined values", () => {
  const rows = Object.fromEntries(inspireDetails({ project: "research", account: "approved-account", notebook: "cpu",
    candidates: 5, minutes: 60, maximum_compute_points: 0.5, digest: "frozen", stages: [
      { phase: "prepare", minutes: 30, resource: { workspace: "CPU资源空间", group: "CPU", quota: "0,4,16", image: "slurm", points_per_hour: 0 } },
      { phase: "carsidock", minutes: 20, resource: { workspace: "分布式训练空间", group: "H100", quota: "1,10,200", image: "pytorch", points_per_hour: 1 } },
    ] }))
  expect(rows["账号 / 文件入口"]).toBe("approved-account / cpu")
  expect(rows["实时计价"]).toContain("prepare: 0")
  expect(rows["实时计价"]).toContain("carsidock: 1")
  expect(rows["分阶段时限"]).toContain("carsidock: 20 分钟")
  expect(rows["资源空间"]).toContain("CPU资源空间")
  expect(JSON.stringify(rows)).not.toContain("undefined")
  expect(rows["批准点券上限"]).toStartWith("0.500")
})

test("legacy single-stage cards retain their fields and missing rates are not free", () => {
  const rows = Object.fromEntries(inspireDetails({ workspace: "分布式训练空间", minutes: 10, points_per_hour: 1 }))
  expect(rows["实时计价"]).toStartWith("1 点券")
  expect(rows["资源空间"]).toBe("分布式训练空间")
  expect(rows["批准点券上限"]).toStartWith("未提供")
  expect(Object.fromEntries(inspireDetails({}))["实时计价"]).toStartWith("未提供")
})

test("core approval separates source rows from shortlisted candidates and exposes the frozen science settings", () => {
  const plan = { task_id: "screen-fixture", target: "EGFR C797", library: "CovInDB/Virtual_Screening_Library.csv",
    source_scope: "bounded", max_records: 2000, candidates: 5, minutes: 30, seed: 20260725,
    quality_gates: { min_carsidock_success: 0.75, min_rtmscore_coverage: 0.95 },
    stages: [{ phase: "prepare", minutes: 10, resource: {} }] }
  const rows = Object.fromEntries(inspireDetails(plan))
  expect(rows["任务编号"]).toBe(plan.task_id)
  expect(rows["靶点 / 位点"]).toBe("EGFR C797")
  expect(rows["候选库"]).toBe(plan.library)
  expect(rows["源库读取范围"]).toBe("有界读取，最多 2000 条")
  expect(rows["候选数 / 总时限"]).toBe("5 个 / 30 分钟")
  expect(rows["准备阶段随机种子"]).toBe("20260725")
  expect(JSON.parse(rows["质检门槛"])).toEqual(plan.quality_gates)
  expect(Object.fromEntries(inspireDetails({ ...plan, source_scope: "full", max_records: null }))["源库读取范围"]).toBe("全库")
  for (const max_records of [undefined, null, -1, 1.5, Infinity]) {
    expect(Object.fromEntries(inspireDetails({ ...plan, max_records }))["源库读取范围"]).toContain("请核对")
  }
  expect(Object.fromEntries(inspireDetails({ ...plan, library: undefined }))["候选库"]).toBe("未提供")
  expect(rows["自动文件交付"]).toContain("不会追加到旧审批")
  const delivery_policy = { mode: "all-core-artifacts-v1", max_file_bytes: 134217728, max_total_bytes: 1073741824, max_files: 100000 }
  const updated = Object.fromEntries(inspireDetails({ ...plan, delivery_policy }))
  expect(updated["自动文件交付"]).toContain("1073741824")
  expect(updated["自动文件交付"]).toContain("超限暂停不截断")
  expect(updated["失败时的交付"]).toContain("未批准")
  const failure = Object.fromEntries(inspireDetails({ ...plan,
    delivery_policy: { ...delivery_policy, failure: "diagnostics-and-verified-prefix-v1" } }))
  expect(failure["失败时的交付"]).toContain("已验证的上游文件与白名单诊断文件")
  expect(failure["失败时的交付"]).toContain("不重跑计算")
})

test("refinement approval displays the complete bounded identity list and separate reasons and budget", () => {
  const input = { parent_task_id: "screen-fixture", maximum_compute_points: 0.2,
    input_sha256: "input-file-hash", identity_sha256: "identity-hash", selection_manifest_digest: "selection-hash",
    engine_parameters: { name: "af3", num_seeds: 1 }, runtime_assets: { name: "af3", source_data_json: "/inspire/protein.json" },
    code_release: { release_id: "release", manifest_sha256: "checksum" }, selected_candidates: [
      { compound_id: "one", input_sdf_index: 1, selection_channel: "rank_disagreement_review", rank_disagreement: 0.8 },
      { compound_id: "two", input_sdf_index: 2, selection_channel: "consensus_sentinel", rank_disagreement: 0.1 },
    ] }
  const rows = Object.fromEntries(inspireDetails(input))
  expect(rows["批准点券上限"]).toStartWith("0.200")
  expect(rows["冻结精筛名单 / 理由"]).toContain("one [1]：排名分歧复核")
  expect(rows["冻结精筛名单 / 理由"]).toContain("two [2]：一致性对照")
  expect(rows["候选身份校验"]).toBe("identity-hash")
  expect(rows["精筛引擎参数"]).toBe('{"name":"af3","num_seeds":1}')
  expect(rows["冻结科学质检规则"]).toContain("没有新版")
  const quality = Object.fromEntries(inspireDetails({ ...input, scientific_quality_policy: {
    version: "refinement-structural-qc-v1", min_covalent_distance_angstrom: 1.5,
    max_covalent_distance_angstrom: 2.2, covdock_max_strain_kcal_mol: 20 } }))
  expect(quality["冻结科学质检规则"]).toContain("2.2")
  expect(quality["冻结科学质检规则"]).toContain("不证明亲和力或活性")
  expect(quality["冻结科学质检规则"]).toContain("缺少必要测量不得通过")
  expect(rows["精筛运行资产"]).toContain("/inspire/protein.json")
  expect(rows["精筛代码版本"]).toContain("checksum")
  expect(rows["精筛审批说明"]).toContain("不继承")
  expect(rows["精筛验收计数口径"]).toContain("历史结果口径")
  const classified = Object.fromEntries(inspireDetails({ ...input, result_contract: "screening-refinement-outcome-v2" }))
  expect(classified["精筛验收计数口径"]).toContain("覆盖率按引擎成功计算")
  expect(classified["精筛验收计数口径"]).toContain("可以全部淘汰")
  expect(classified["精筛验收计数口径"]).toContain("通过不等于实验证实活性")
  expect(Object.fromEntries(inspireDetails({ ...input, result_contract: "unrecognized" }))["精筛验收计数口径"]).toContain("请勿批准")
  for (const selected_candidates of [[], [...input.selected_candidates, {}], Array(101).fill(input.selected_candidates[0])]) {
    expect(Object.fromEntries(inspireDetails({ ...input, selected_candidates }))["冻结精筛名单 / 理由"]).toContain("请勿批准")
  }
})
test("explicit uncapped test policy is distinguished from a missing budget", () => {
  expect(inspireDetails({ maximum_compute_points: null, budget_policy: "uncapped-test100-v1" })
    .find(([label]) => label === "批准点券上限")?.[1]).toContain("不设金额上限")
  expect(inspireDetails({ maximum_compute_points: null })
    .find(([label]) => label === "批准点券上限")?.[1]).toContain("未提供")
})

test("one workflow approval explains sequential independent evaluations and preserves both resource budgets", () => {
  const plan = { parent_task_id: "core-fixture", workflow_contract: "screening-refinement-workflow-v1",
    execution: "af3_then_covdock_same_subset", failure_policy: "stop_on_execution_failure",
    quality_policy: "retain_rejections_and_continue", result_contract: "screening-refinement-outcome-v2",
    maximum_compute_points: 1.2, engine_parameters: [{ name: "af3", num_seeds: 2 }, { name: "covdock", mode: "LO" }],
    runtime_assets: [{ name: "af3", cysteine_sequence_index: 105 }, { name: "covdock", attachment_residue: "A:797" }],
    stages: [{ phase: "af3", maximum_compute_points: 1, minutes: 30, resource: { quota: "1,10,200" } },
      { phase: "covdock", maximum_compute_points: 0.2, minutes: 40, resource: { quota: "0,4,16" } }] }
  const rows = Object.fromEntries(inspireDetails(plan))
  expect(rows["一次确认的执行顺序"]).toContain("不自动沿用 AF3 构象")
  expect(rows["失败与质量淘汰"]).toContain("执行失败停止后续提交")
  expect(rows["失败与质量淘汰"]).toContain("AF3 置信度不是亲和力")
  expect(rows["分阶段新增预算"]).toBe("af3: 1 点券；covdock: 0.2 点券")
  expect(rows["精筛引擎参数"]).toContain('"num_seeds":2')
  expect(rows["精筛引擎参数"]).toContain('"mode":"LO"')
  expect(rows["精筛运行资产"]).toContain("A:797")
  expect(rows["精筛验收计数口径"]).not.toContain("历史结果口径")
  expect(rows["批准点券上限"]).toStartWith("1.200")
  expect(rows["精筛自动文件交付"]).toContain("不会继承主筛")
  expect(rows["作业内运行检查"]).toBeUndefined()
  expect(Object.fromEntries(inspireDetails({ ...plan, runtime_admission: "cpu-before-approval-gpu-at-start-v1" }))["作业内运行检查"]).toContain("计入所列时限")
  const delivered = Object.fromEntries(inspireDetails({ ...plan, delivery_policy: { mode: "all-refinement-artifacts-v1",
    max_file_bytes: 134217728, max_total_bytes: 1073741824, max_files: 2000 } }))
  expect(delivered["精筛自动文件交付"]).toContain("完整精筛输出清单")
  expect(delivered["精筛自动文件交付"]).toContain("超限暂停")
  expect(delivered["精筛自动文件交付"]).toContain("1073741824")
  expect(Object.fromEntries(inspireDetails({ ...plan, execution: "parallel" }))["一次确认的执行顺序"]).toContain("请勿批准")
  expect(Object.fromEntries(inspireDetails({ ...plan, failure_policy: "retry" }))["失败与质量淘汰"]).toContain("请勿批准")
})
