---
name: covalent-denovo-design
description: Route OpenScience fixed-warhead De novo requests to the registered PocketXMol workflow, inspect progress and plan downstream review. Use for covalent de novo generation; generic denovo-design is not this executor.
category: chemistry
tags: [covalent, denovo, pocketxmol, workflow]
---

# De novo 固定 warhead 设计

当前原生生成能力是 EGFR WT C797 / 4G5J A / 丙烯酰胺，PocketXMol maskfill。不能把该固定方案称为任意靶点自动化；其他靶点、突变体、warhead 或附加约束先形成新方案和适配需求，不能回退本方案。

- 新生成或解释生成 QC：加载 `covalent-denovo-generation`。
- 生成候选的独立共价对接与筛选：加载 `covalent-denovo-refinement`。
- 已有重复 MD 的分析/图表解释：加载 `covalent-denovo-md-analysis`。

执行接口见 [原生工具合同](references/tool-contracts.md)。自然语言生成请求先用 `discovery_study` 建立或恢复研究，沿 `denovo_generate.workflow` 返回的下一步完成科学推理、假说同步和 `plan_study` 编译。缺少科学输入时明确补齐，不自行填入默认靶点、种子或槽位。`create` 已停用，仅返回需要科学决策，不创建任务。冻结计划后，实际用户的“执行 de-novo 研究 study-<id>”才进入 `create_study`，并仍须原生算力审批。`status` 使用已有 task ID，绑定原会话/项目；跨会话使用项目内 `workflow` 读取原任务，不新建替代任务。

本路线：PocketXMol 生成 → 原始/优化后分别 QC → 独立 CovDock 和对照 → 多目标选样 → 共价加合物 MD → 轨迹分析/报告。**原生生成工具只自动覆盖生成、QC 和 Results 交付**；后续阶段已有项目专用脚本及历史结果，没有一条当前原生通用链自动承接新候选。通用 `denovo-design` skill 和 Library-first 执行工具都不是其替代品。

新任务每步保留 candidate ID、原始 slot、化学图、指定反应原子、输入与版本哈希、资源计划和失败账本。历史七候选及其 MD 结果只能解释所属批次，见 [已验证证据](references/evidence.md)。输出区分计算完成、结构门通过、采样是否充分及最终交付；固定 warhead 几何、共价拓扑、对接分数都不证明活性。
