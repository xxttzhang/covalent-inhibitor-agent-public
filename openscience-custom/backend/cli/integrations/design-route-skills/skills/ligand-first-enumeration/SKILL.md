---
name: ligand-first-enumeration
description: Prepare and audit Ligand-first parent structures and Covalentizer enumeration. Distinguish the RDKit branch-replacement subset from the upstream-native full-parent backend; preserve atom mapping and rejected candidates.
category: chemistry
tags: [covalent, ligand-first, rdkit, covalentizer]
---

# Ligand-first 输入与枚举

先加载 `ligand-first-covalentization` 的路线约束。根据实际 campaign 的 `enumeration_backend` 选择流程，不把不同后端的化学规则混用。具体命令、输出及依赖见 [接口清单](../ligand-first-covalentization/references/tool-contracts.md)。

当前网页通过阶段开放的 `prepare`、`generate` 和证据读取动作完成候选规划与生成；下列脚本仅解释历史底层实现，不授权网页智能体运行脚本。生成后读取完整批次、化学身份及立体化学暂缓原因，再由 `prepare_docking_input` 选择直接 LO 或 VS→LO，不手工截取前三个。

1. `prepare_ligand_first_inputs.py` 审计冻结结构、母体身份和源文件 SHA-256，导出母体 SDF。`--restore-missing` 仅在需要补齐来源文件时使用，验证指定 RCSB 文件哈希，不覆盖已有资产。
2. 对 `rdkit-branch`，`generate_real_ligand_first_candidates.py` 使用项目内 Covalentizer 规则子集；自动位点发现须同时给 `--auto-discover-sites --site-discovery-json`。保存出射距离/方向、parent atom map、替换支链、保护原子/键和不推进的出口。
3. 对 `upstream-native`，`run_upstream_ligand_campaign.py` 调用登记的上游 Covalentizer，实现完整母体图保留、手性敏感映射，最小母体保留比例 1.0。它不是 RDKit 支链替换，也不调用 Attachment Planner。

RDKit 子集为 acrylamide、chloroacetamide、vinyl sulfone、propiolamide 与 direct/methylene/ethylene；上游原生硫类是 **vinyl sulfonamide**，不能写成 vinyl sulfone，也不能强行套用固定 4×3 数量。读取对应规则版本和 manifest，保持生成来源、反应类别、亲电原子、原子映射、失败原因和预算门。`upstream-native` 已有显式验证配置，但不自动替换默认后端。

化学门检查 sanitization、价态、自由基/电荷、单组分、去重、保护结构和反应位点唯一性。匹配多个 warhead 时不能由模型猜反应原子；按后端登记规则处理。完整母体图或 MCS 保留不代表结合相互作用保持，需后续三维证据。

PAINS 命中和基于 cLogP/芳环数的聚集风险是标记/启发式；未运行毒性或反应性模型时不能报告其预测结果。预筛阈值和 hard/soft 角色以当前配置为准，不能把通用药物相似性规则自动当硬删除条件。

输出候选表、SDF、来源 lineage、位点审计与失败账本；表格索引必须与 SDF 原始序号一致。零候选、缺映射、输入审计失败或原生枚举超预算时停止晋级，不悄悄增加预算或切换后端。
