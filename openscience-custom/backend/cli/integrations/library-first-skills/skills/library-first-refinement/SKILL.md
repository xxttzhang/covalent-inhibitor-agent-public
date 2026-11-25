---
name: library-first-refinement
description: Select a traceable Library-first refinement subset and operate the registered AF3 and CovDock-LO workflow; explain rank disagreement, structural quality, score limits and verified refinement delivery.
category: chemistry
tags: [covalent, library-first, af3, covdock]
---

# Library-first 选样与精筛

所有动作使用 `inspire_screening_task` 的原 task_id。先 `report` 核验主筛，再依据实际候选提出明确 selection_policy。读取 [调用合同](../library-first-screening/references/tool-contracts.md)。

## 选样

`select_refinement` 接收七个字段：max_candidates（1–100）、max_fraction（>0 且 ≤0.25）、disagreement_threshold（>0 且 ≤1）、top_fraction（>0 且 ≤1）、uncertainty_count（1–100）、consensus_count/diversity_count（0–100）。三通道配额总和 ≤max_candidates。策略由当前研究目的与证据明确制定，不把历史阈值当所有靶点默认值。

算法按两个引擎记录的方向分别计算含并列处理的排名分位数，取绝对分位差；有效上限为 min(max_candidates, floor(eligible_count × max_fraction))。分歧阈值并非校准的不确定性概率。共识及多样性通道是对照/覆盖，不称为“不确定候选”。剩余槽位不自动补满；零候选返回 NO_CANDIDATES_MEET_POLICY，不改用全池。

返回 selection_id、父计划/manifest 哈希、候选身份、选择通道与理由。此动作持久化选样工件但不运行 AF3/CovDock，也不授予算力权限。

## 计划、执行与交付

环境问题用 `diagnose_refinement`；有精筛意图且已有子集时 `plan_refinement` 形成原会话绑定的执行草案。需要执行时 `approve_refinement` 走平台统一原生资源批准，后台推进所批 AF3/CovDock 阶段。已有有效授权按执行器处理，不要求重复聊天确认。

`refinement_status` 查询；`refinement_report` 重新核验综合报告及交付；文件交付故障用 `resume_refinement_delivery`，不重跑计算。取消用 `cancel_refinement` 并核实实际终止。环境、许可证或输入缺失均返回具体阻塞，不能换引擎后沿用原批准。

## 两个引擎的解释

- AF3：核验准备后的共价加合物、userCCD/原子名、bondedAtomPairs 与候选身份；不能只把反应前 SMILES 传入就声称模拟了共价连接。按实际报告中指标定义解释 mPAE/置信度，不能当作亲和力。
- CovDock-LO：少量候选独立准备、对接与精修；不是整个虚拟库的默认主筛。保留受体、LigPrep 身份、反应类型、姿势、评分、应变与失败。商业运行环境是否可用由真实预检决定。
- 比较姿势先按指定蛋白/口袋原子对齐；不能拟合配体本身后宣称姿势一致。检查化学图、手性、共价键几何、碰撞、应变和关键相互作用，分别呈现证据，不只给单一分数。
- 未计算的 AF3–CovDock 一致性写未评估；不要凭同为成功输出认定一致。历史 A/B1/B2 是特定质量门版本的标签，引用时注明来源，不能当活性排名或 MD 等级。

已完成八候选属于有界历史验证，范围及原始/后续复核关系见 [验收证据](../library-first-screening/references/validated-scope.md)。
