---
name: library-first-prescreen
description: Inspect CovInDB evidence and explain or verify Library-first virtual-library preparation, RDKit warhead mapping, property gates and three-channel candidate selection in the configured OpenScience workflow.
category: chemistry
tags: [covalent, library-first, rdkit]
---

# Library-first 数据来源与预筛

用于库来源、化学筛选和候选入选原因。新任务由 `library-first-screening` 路由，经 `inspire_screening_task` 启动准备阶段；本 skill 不另外发起一套手工 RDKit 筛选。

## 数据库角色

已知抑制剂证据：通过当前已连接 MCP `inspire-covindb` 的 `covindb_query` 查询 `CovInDB_All.csv`；它与候选源库 `Virtual_Screening_Library.csv` 分开。已知药物可作为参考和对照，不能代替虚拟候选库。

`covindb_catalog` 无参数，只读检查文件及表头。`covindb_query` 参数：target（精确基因符号或 UniProt）或 inhibitor（至少一个）、可选 site、limit（1–50，默认20）、offset（0–100000，默认0）；多个筛选条件取交集。示例参数：`{"target":"EGFR","site":"C797","limit":10,"offset":0}`。使用模型会话实际暴露的 MCP 工具名。

返回来源哈希、CI ID、原始活性/单位/文献/SMILES。记录数与独立抑制剂数分别报告，不跨不同 assay 直接比较活性。零命中、连接失败和未查询三者分开。

## 准备与筛选规则

读入当前冻结计划的源记录范围 → 分子解析/身份与去重 → SMARTS 匹配与亲电原子唯一性 → 理化硬门槛 → 警示项与优先级 → exploitation/diversity/exploration 选样 → 一致的 SDF/Parquet/身份映射。

当前 EGFR Michael Addition 配置采用丙烯酰胺。已用门槛：MW 180–650 Da、cLogP −1 至 6、TPSA ≤160 Å²、可旋转键 ≤15、绝对形式电荷 ≤2；真实冻结配置若不同应逐项披露。SMARTS 命中不证明实际反应性。多重亲电原子歧义不得由模型任选。

PAINS、过度反应性和聚集相关警示参与记录/惩罚，不一概描述成全部硬剔除；这也不是完整 ADMET 预测。原始失败/拒绝记录必须保留。

候选分配：50% exploitation，30% diversity，余量 exploration（整数按实现取整）；不足时实现可能 backfill，实际来源以 `stage2_selection_manifest.json` 和逐分子标签为准。与后续精筛“不自动补足配额”规则区分。

## 验收与失败

核对源文件哈希、读取范围、总数/独立数/硬门槛通过数/入选数、warhead 原子映射、SDF 与表中身份。输入索引不可因过滤重编号后丢失原始来源。上游准备失败先用 `diagnose_preparation`，不得放宽门槛、换库或重建任务以隐藏失败。

脚本：`routes/library-first/scripts/run_covindb_prescreen.py`；规则：`routes/library-first/src/covscreen_library_first/covindb.py`（相对工作区根）。平台工具合同见 [共享接口](../library-first-screening/references/tool-contracts.md)。
