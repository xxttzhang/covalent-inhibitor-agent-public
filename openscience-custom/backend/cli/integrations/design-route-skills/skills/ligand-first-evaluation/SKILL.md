---
name: ligand-first-evaluation
description: Re-evaluate Ligand-first chemistry and physical evidence independently, produce Pareto candidates and explain completion criteria. Use for candidate selection and traceable final reports, not execution of generic MD.
category: chemistry
tags: [covalent, ligand-first, pareto, evaluation]
---
## 当前 LO 评估策略（2026-11-13）

Ligand-first 的 AF3/CovDock 跨方法一致性是可选补充证据，不能因 NA 单独阻断候选。新 v4 策略仍要求执行成功、完整物理检查及 PASS/WARN，保留共价连接、键长、碰撞、局部应变门禁。不得填造 AF3/HIGH，不自动启动 AF3。旧冻结 v3 判断先按原规则核验，再明确记录 v4 再评估，不覆盖旧产物。退出码 4 可表示批次未满足旧门禁，不能仅凭它声称没有姿态或要求重算；有登记产物时先按工作流开放动作 evaluate_covdock，再 report_covdock，然后 export_covdock_report 将原样报告交付 Results；export_design_report 是候选生成报告，不能替代 LO 报告导出。候选入选不等于 MD 或活性验证。此节优先于下文历史父链。


# Ligand-first 独立评价与交付

当前网页按工作流开放的 LO 状态、独立评估及报告动作读取同一批准任务的原始产物。直接 LO 不需要历史 VS 或 AF3 记录；完整候选来源、独立准备产物、批准范围、实际执行与原始姿态核验必须可追溯。计算完成不等于姿态通过，报告如实给出各项失败或未评估；没有通过姿态也可交付有界阴性报告。本轮不运行 MD。

以下是历史父链评价器的解释，不是网页调用脚本或要求新路线跑八阶段的指令。

使用 `evaluate_ligand_first_batch.py` 从源表、SDF/PDB/JSON 重新读取 SMILES、warhead、反应原子、母体 MCS 和物理字段。不能用 Candidate Passport 已有结论证明自身正确。命令和限制见 [接口清单](../ligand-first-covalentization/references/tool-contracts.md)。

默认 `--min-parent-mcs-fraction=0.60` 是该评价器的阈值，**不是**原生枚举完整母体保留 1.0 的替代条件。需要源坐标复核时同时提供 `--schrodinger-root`、`--attachment-chain`、`--attachment-residue`；缺少源坐标或工具时报告复核缺失，不能把复用字段当独立坐标证据。

物理或独立评价 FAIL/未知先排除，只有两门均为 PASS/WARN 才可晋级；使用共享脚本 `select_library_first_pareto.py --profile ligand-first`（由父链编译完整追溯参数）对其余候选作非支配排序，分别保存 AF3–mPAE、property fit、反应性风险、局部 strain、母体 MCS 等已实际得到的目标。未知指标保留缺失，不能填零或“通过”；不可比原始分数不加权求和。若缺下游阶段，最多给待复核名单。

历史父链完成标准（不适用于新的直接 LO 网页路线）：本次父链所要求各阶段产物、质量门、候选身份、父 run 引用、哈希和追溯报告一致；完整路线需八阶段闭环，合法续接也需核验上游来源。编排配置允许以 pareto 结束的连续后缀，不允许随意跳阶段来宣称完整链完成。

交付候选 CSV/Parquet/SDF、带真实二维结构的候选卡、Pareto manifest 和 JSON/Markdown 追溯报告。报告分别给出当前阶段、候选数、失败原因、证据时间及下载入口。网页能显示历史数据不说明任务可从聊天启动；只有实际发布并回读的文件才可称平台已交付。

MD、QM、ADMET、合成和实验按实际状态单列。Library-first 的固定八候选 `molecular_dynamics` 不能接收本路线分子。研究优先级不等同于活性、选择性或可合成性结论。
