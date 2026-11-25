---
name: ligand-first-refinement
description: Apply Ligand-first batch routing: small batches directly to CovDock-LO, larger batches through VS then LO, with independent preparation, native approval and traceable pose assessment. CarsiDock-Cov is suspended.
category: chemistry
tags: [covalent, ligand-first, docking, alphafold3]
---
## 当前 LO 评估策略（2026-11-13）

Ligand-first 的 AF3/CovDock 跨方法一致性是可选补充证据，不能因 NA 单独阻断候选。新 v4 策略仍要求执行成功、完整物理检查及 PASS/WARN，保留共价连接、键长、碰撞、局部应变门禁。不得填造 AF3/HIGH，不自动启动 AF3。旧冻结 v3 判断先按原规则核验，再明确记录 v4 再评估，不覆盖旧产物。退出码 4 可表示批次未满足旧门禁，不能仅凭它声称没有姿态或要求重算；有登记产物时先按工作流开放动作 evaluate_covdock，再 report_covdock。候选入选不等于 MD 或活性验证。此节优先于下文历史父链。


# Ligand-first 对接与结构精筛

## 当前批量路由策略（2026-11-11 用户决定）

Ligand-first 从核验后的完整候选批次计数：明确化学身份和立体化学的 1–6 个候选默认直接 CovDock-LO，超过 6 个采用 VS→LO；零候选不提交。六个是产品策略，不是科学精度分界。少量候选可显式选择 VS，但须记录理由；不能先截断批次再宣称小批量。歧义候选保留暂缓原因，不替其选择构型。

生成后优先按阶段工具提供的 `prepare_docking_input` 自动分流，以工作流回执中的版本、分母、路径和可用动作作为依据。直接 LO 的来源是冻结候选图，不要求 VS、AF3 或对接前几何 PASS；不得伪造这些前置记录。直接 LO 仍必须完成独立受体／配体准备、构建体与环境审阅、输入身份绑定、网页原生资源审批，再在指定实例执行。

CPU 计算使用本次计划绑定的 asgbie3；只用原生科研工具发起审批和调度，不调用通用 shell、compute_job 或脚本替代。准备、执行成功、产物身份核验、姿态门禁和报告交付分别记录；按原 run 查询状态，不重复 start。失败时保留具体证据，经工作流开放的重规划入口和新审批处理，不能降低门槛或复用旧批准。

CarsiDock-Cov 保持停用，不新提交、重试或自动回退。VS 不能直接交给 MD，LO 也不等于 MD 就绪；本次验收止于 LO 姿态评估与报告。历史 CarsiDock／AF3／八阶段记录只供追溯，不能作为新直接 LO 路线的强制依赖。本节优先于下文及引用文件中的历史说明；实际可用动作仍以当前工具目录和阶段回执为准。

输入必须绑定已验收枚举批次的表/SDF/原子映射、受体哈希和位点映射。工具是仓库的阶段执行器；当前平台连接状态见 [接口清单](../ligand-first-covalentization/references/tool-contracts.md)。

## 历史父链证据解释（只读）

- **CarsiDock-Cov**：按 `carsidock_reaction_type` 路由，检查反应支持、姿势及候选身份。原始 CarsiScore、母体核心 RMSD、相互作用保持、反应距离/角度、clash、strain 和多姿势一致性分别记录。评分成功不等于全部物理门通过。
- **召回池**：当前父链按成功候选数封顶 `carsidock_top_n`，`round(top_n × 0.80)` 为 score union，余量为多样性，`rescue_n=0`。小批次采用实际舍入结果，不照搬大库名额。不要加入 Library-first 专属的 RTMScore 共识阶段；当前此链没有该阶段。
- **AF3–mPAE**：绑定 candidate ID、adduct 化学图、共价原子和蛋白片段映射。读实际采样日志和源 CIF；登记默认是 1 seed、1 sample、3 recycles，记录任何差异。当前 mPAE 为 ligand→protein `chain_pair_pae_min`，不是全界面 PAE 均值或亲和力。低 mPAE 不能跳过物理检查。
- **CovDock-LO**：按对应反应类型分组，核验指定残基/反应原子的真实拓扑键、键长/角度、局部 clash/strain。局部蛋白对齐后比较 AF3/CovDock 配体构象；记录原子映射和对齐选择，不能对配体自拟合掩盖位移。Schrödinger 安装、许可证和反应支持必须可用。

AF3 预设键与 CovDock 拓扑连接不是实验成键证据。不同 warhead、评分引擎和不同受体的分数不能直接加权混排；CovDock 用于物理复核，不覆盖 AF3 的原始读数。

交接逐候选保留输入/成功/失败数、引擎日志、pose ensemble、源文件哈希及排除原因。作业退出码为 0、部分分子输出或一份评分 CSV 不足以标记阶段通过。缺失/超预算/不支持反应/质量门失败保持具体状态，不能自动补算或复用历史姿势替代。之后加载 `ligand-first-evaluation`。
