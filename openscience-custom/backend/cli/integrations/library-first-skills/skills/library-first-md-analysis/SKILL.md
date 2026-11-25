---
name: library-first-md-analysis
description: Inspect, plan or start the host-registered Library-first eight-candidate OpenMM MD and trajectory-analysis workflow, and interpret its plots and replica-level evidence without extending the fixed input profile.
category: chemistry
tags: [covalent, library-first, md]
---

# Library-first MD 与自动分析

使用 `molecular_dynamics`，只接受 `{"action":"plan"}`、`{"action":"status"}`、`{"action":"start"}`。plan/status 不提交计算；start 必须来自用户实际执行意图，由原生资源机制处理授权。当前批次已登记时 start 返回原状态，不重复提交。

## 已支持范围

固定候选 library_017/019/029/030/033/038/056/081；已准备的 OpenMM/OPLS_2005 共价加合物；每个候选三次独立 10 ns，资源上限 8 GPU、60 GPU·h。接口没有新候选、任意结构、时长或追加重复参数。查询其他批次先报告该限制，不能借此工具启动未登记体系。

阶段合同：prepare → parameters → short_qc → production → trajectory_audit → analysis → report。当前真实运行入口消费已完成准备/参数审计的固定体系，不等价于任意新结构的自动 prepare/parameters 都已实现。

运行核验包括 CPU/CUDA 力/能量检查、最小化/平衡/短程 QC、生产、全轨迹审计及结果发布。平台本地观察器依赖主机在线；远端任务独立运行不意味着主机休眠时仍能即时更新页面。

## 应展示的结果

读取真实 manifest、逐重复统计、图表和交付回执；该批已交付 RMSD、配体保留/相对位移、接触、氢键、RMSF、物理诊断、分块稳定性七类图及批次汇总。缺失指标明确列出，不能用一个 RMSD 终值代替整条轨迹分析。

先确认轨迹/拓扑原子一致、时间与帧数完整、PBC 处理、对齐对象、单位及重复编号，再解释均值与重复间差异；不能把相关帧当作独立重复，也不能仅凭短时曲线平稳断言收敛。

化学状态为 covalent_adduct：分析加合物构象与相互作用，不能把永久共价键约束解释成反应前 NAC 成功率；稳定性不证明亲和力、选择性或 kinact。自动分析完成与科学审阅、实验验证分开。

已完成 24 条轨迹/240 ns 的历史证据见 [已验证范围](../library-first-screening/references/validated-scope.md)。当前状态使用真实工具查询。源码合同：`src/tool/molecular-dynamics.ts`、`integrations/md-workflow/registered.ts`（相对 backend/cli）。
