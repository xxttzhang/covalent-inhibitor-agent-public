---
name: library-first-docking
description: Inspect historical Library-first docking and scoring evidence. New CarsiDock-Cov calculations are suspended; prefer CovDock-VS once its native workflow is integrated.
category: chemistry
tags: [covalent, library-first, docking]
---

# Library-first 主筛与重评分

## 当前引擎策略（2026-11-02 用户决定）

后续构象暂停使用 **CarsiDock-Cov**；不得新提交、重试、重新调度或自动回退到它。优先采用 **Schrödinger CovDock-VS**。本节优先于本文及引用文件中的历史阶段说明。

原生 CovDock 接口目前为 LO 精修，VS 初始构象工作流尚未接入；不得仅改引擎名称、借用 LO 身份或用独立试验结果伪造原生通过记录。遇到仍依赖 CarsiDock 的步骤，报告 `docking_engine_disabled` / VS 接入缺口，继续可独立进行的输入核验和历史结果审阅。不要重新询问用户是否停用该引擎。

历史记录、结构和评分保留供只读追溯。CovDock-VS 的无严重碰撞与正确共价连接不等于完整科学门禁通过；应变、关键相互作用及 MD 准入仍须核查。

执行入口是 `inspire_screening_task`，不是独立的 `carsidock` 或 `rtmscore` 平台工具。先读取 [接口与规模合同](../library-first-screening/references/tool-contracts.md)。阶段由已授权后台任务推进，模型无需逐步提交 GPU 作业。

## 输入与执行

新任务继承 `library-first-screening` 核验过的靶点参数、库、模式与预算。`create` 绑定实际用户执行请求并触发预检/原生资源审批；不要用于仅询问“能否筛选”的对话。已存在任务使用 list/status，不重复创建。原生批准的计划确定计算范围；加载 skill 不批准新计算。

准备阶段交付 `stage2_candidates.sdf`、`stage2_candidates.parquet`、选择清单和原子映射；CarsiDock-Cov 消费匹配的受体、参考配体、候选及反应类型。当前 worker 使用 1M17 A:CYS773，对应 EGFR canonical C797。CarsiDock、AF3、Schrödinger 环境不混装。

RTMScore 只消费本任务成功的 CarsiDock 姿势，不为了重评分重新对接。保留全部原始候选行，上游无姿势行继续标记失败。按 artifact 内 score_direction 解释分数；当前 RTMScore higher_is_better，不能误作结合自由能。不同评分不能直接做原始值平均或加权和。

当前主筛成功率门槛为 CarsiDock ≥0.75、成功姿势的 RTMScore 覆盖率 ≥0.95，来源为冻结计划 gates。它们是流水线可用性检查，不是活性标准；未达到时保留失败和诊断，不自动放宽。

## 状态、交付和恢复

`status` 读取阶段快照；`report` 重新核验源 manifest/工件，只有可用报告的验证通过才称报告可交付。计算 SUCCEEDED 与 `delivery.state=COMPLETE` 分别报告；后者须有完整清单及原生 Results 回读。远端路径不能当本机下载链接。

`wake` 恢复观察器；`resume_delivery` 恢复文件交付；均不得称为重算。提交响应不确定时按同一任务对账，不重提。原会话限制不通过复制任务规避。

技术真源（相对 backend/cli）：`integrations/inspire-screening/executor/{tool,worker,runner,report,delivery-queue}`；已有固定批验收见 [验证范围](../library-first-screening/references/validated-scope.md)。更换靶点/结构/warhead 需要登记并验证执行适配，不由 skill 自动扩展。
