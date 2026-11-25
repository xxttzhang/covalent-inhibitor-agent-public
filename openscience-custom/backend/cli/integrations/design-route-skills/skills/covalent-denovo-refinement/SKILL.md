---
name: covalent-denovo-refinement
description: Review covalent De novo candidates with independently prepared CovDock poses, matched control redocking and traceable MD shortlist selection. Use for the existing EGFR protocol or a new explicit downstream plan; no generic native refinement API is registered.
category: chemistry
tags: [covalent, denovo, covdock, selection]
---

# De novo 共价对接与候选筛选

此阶段使用独立 CovDock leadopt 复核，**当前 De novo 登记链没有 AF3 必经阶段**。执行和产物索引见 [已验证证据](../covalent-denovo-design/references/evidence.md)；已有脚本为特定批次方案，不是可直接消费任意新结果的原生工具。

1. 冻结原始 QC 通过集合及原子映射，保留每个分子的 raw/relaxed 差异。历史 006 raw 通过、relaxed clash 失败，因此保留并标记；不能因只读优化后 SDF 而漏掉它。
2. 用同一受体准备、质子化和 LigPrep 设置进行对照与候选对接，记录实际微观态和未覆盖的立体化学。登记协议重新准备配体构象，不以生成/晶体坐标作位置约束。
3. 阿法替尼同流程重对接校准：按评分预先选最优 pose，经蛋白骨架对齐后计算对称性修正配体重原子 RMSD，配体不独立拟合；本批预设 ≤2 Å。不能先挑 RMSD 最小姿势再声称评分校准通过。
4. 固定 C797 SG 和指定反应碳（本批 WBC）拓扑身份。存在多个亲电基团时明确反应位点，不能由自动匹配随意选择。核验化学图、键连接、键长/角度、碰撞、应变与多姿势一致性。
5. 对照通过后审查候选；保留亲和评分、local/global strain、hinge 氢键、pose 一致性和骨架多样性等独立证据。氢键几何注释不是轨迹占有率，strain 不是结合自由能。未预设的药物相似性描述符用作解释，不能事后新增淘汰线。

选样写清每个候选的晋级/保留/排除原因；不是简单按一个分数取前三。历史入选 004、001、007（007 探索性）的结论绑定原批次；后续 MD 研究优先级可变，必须保留原对接排序。

交付源 Maestro/复合物/配体 SDF、逐 pose 审计和候选表，校验原始 ID 与归档哈希。随后单独冻结共价参数、重复次数、对照、预算和分析计划；不能调用 Library-first 固定八分子的 `molecular_dynamics` 来替代。
