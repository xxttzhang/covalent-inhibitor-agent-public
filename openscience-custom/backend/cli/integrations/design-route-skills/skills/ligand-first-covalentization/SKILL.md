---
name: ligand-first-covalentization
description: Plan and inspect the OpenScience Ligand-first covalentization route from an existing noncovalent ligand. Use for parent-pose intake, stage routing, progress and reuse on another target; check executor availability before proposing execution.
category: chemistry
tags: [covalent, ligand-first, workflow]
---

# Ligand-first 共价化路线

## 母体与 Cys 配对（2026-11-21）

先核验靶点，再用 `search_parent_index` 优先检索冻结 CovPDB 来源；支持 offset/limit 分页，保留 total_matches，不将第一页当成全部。该工具只接受当前工作流绑定的靶点，不接受模型另传 accession。没有命中不表示没有可用母体，继续用 PDB、ChEMBL 和文献补充。用户指定母体或位点时保留约束，不能因索引命中另一个组合而静默替换。

索引里的 chain_id/residue_number 是待核验的结构编号，不能直接传给 canonical site。物种、构建体、插入码、替代构象、配体实例和完整化学身份必须独立核验。ABPP unavailable/ambiguous 不等于阴性；预测 pKa、SASA、probcov 和 ligandability 仅作辅助证据，不自动决定成键能力或活性。

`measure_parent` 提供 parent_cys_assessment。默认最近 SG 距离小于6 Å只支持进入枚举可行性评审；小于等于2.1 Å的接触、显式 Cys 连接或证据缺失须核验，超出窗口表示超出当前自动方案范围，不是分子无效。`prepare` 会重读冻结结构和 CCD，未通过时返回 NEEDS_PARENT_PAIR_REVIEW；补证或 reconsider，不重复相同准备。

可改造位置、保留的母体核心与相互作用继续使用原有保护和官方枚举几何模块。距离通过不意味着存在合适出口方向，生成零候选也不证明靶点不可共价化。报告分别呈现配对预审、枚举/化学准入、执行结果和姿态证据。
报告中的规范 Cys 数量使用靶点回执的 `canonical_cysteine_count`；旧回执缺少该字段时，须用代码计算完整 `canonical_cysteines` 列表长度或省略数量，不凭阅读长列表估算。
序列中的 Cys 数量与结构中可测 SG 的数量分别报告。距离结论仅覆盖所选模型、链和占据率有效的实际 SG 坐标；缺失坐标记为未测量，不能推断远离配体。IC50 保留测定类型、单位和条件，不能改称结合亲和力（Kd/Ki）。未执行枚举或化学准入时写“未执行／未评价”，不能将其写成测得的零生成或零准入。
保存 Results 前，用代码核对报告引用的序列片段与原始序列切片逐字一致（去除 Markdown 强调后，长度须等于末位减首位加一）；加粗残基应替换原字符的显示格式，不能另插一个字符。不需要序列片段支撑结论时可以省略。文献结论限于实际检索到的化合物、研究和机制；几个可逆抑制剂的例子不能推出“某代抑制剂全部可逆”或其他未经穷尽核验的全称结论。

## 当前批量路由策略（2026-11-11 用户决定）

Ligand-first 从核验后的完整候选批次计数：明确化学身份和立体化学的 1–6 个候选默认直接 CovDock-LO，超过 6 个采用 VS→LO；零候选不提交。六个是产品策略，不是科学精度分界。少量候选可显式选择 VS，但须记录理由；不能先截断批次再宣称小批量。歧义候选保留暂缓原因，不替其选择构型。

生成后优先按阶段工具提供的 `prepare_docking_input` 自动分流，以工作流回执中的版本、分母、路径和可用动作作为依据。直接 LO 的来源是冻结候选图，不要求 VS、AF3 或对接前几何 PASS；不得伪造这些前置记录。直接 LO 仍必须完成独立受体／配体准备、构建体与环境审阅、输入身份绑定、网页原生资源审批，再在指定实例执行。

新 CPU 计算排除 asgbie2、asgbie3（2026-11-21 用户约束）；先检查其他实例的配额、占用和许可证，再绑定到本次原生计划；只用原生科研工具发起审批和调度，不调用通用 shell、compute_job 或脚本替代。准备、执行成功、产物身份核验、姿态门禁和报告交付分别记录；按原 run 查询状态，不重复 start。失败时保留具体证据，经工作流开放的重规划入口和新审批处理，不能降低门槛或复用旧批准。

CarsiDock-Cov 保持停用，不新提交、重试或自动回退。VS 不能直接交给 MD，LO 也不等于 MD 就绪；本次验收止于 LO 姿态评估与报告。历史 CarsiDock／AF3／八阶段记录只供追溯，不能作为新直接 LO 路线的强制依赖。本节优先于下文及引用文件中的历史说明；实际可用动作仍以当前工具目录和阶段回执为准。

将已知非共价配体变成可追溯的共价设计候选。先区分输入配置、工具可用性和科学结果；技能加载成功并不代表远端执行器已接入。

## 科研主入口与返回路径

同一工作流的阶段调用按顺序执行；`read_covdock_environment` 也会记录事件并更新 revision，不能并行发出多个依赖同一版本的请求。原生宿主会自动绑定工作流身份和 revision；当前工具 schema 未开放 `workflow_id`、`expected_revision` 时不要自行添加这两个参数。仅在接口明确要求时才使用上一回执的最新 revision。遇到版本冲突先用 `status` 核验当前阶段与回执，再继续尚未完成的动作；不能因此重复提交已启动的计算。

开放式设计请求中的科学选择由智能体负责：用户未指定位点时，自主提出一个有理由、范围有限的探索性位点假说，然后通过真实检索与测量核验。出现多个 Cys 或母体并不是要求用户代选的理由。已有知识可用于提出假说，但不能写成已核验事实；不足或失败时补证、重选或如实报告不能判断。只有用户意图／靶点身份确实无法消歧，或缺少属于用户的约束／执行授权时才询问。用户明确要求比较或自行选择位点时遵从其要求。

当工具目录提供 `covalent_ligand_workflow` 时，所有新的 Ligand-first 研究从该入口 `start` 开始，检查已有研究用 `status`。明确靶点与物种；位点尚未确定时省略 site，进入 TARGET_REVIEW，用 evidence 的 resolve_target 核验 UniProt 身份和 canonical Cys 位置，再用入口 select_site 记录研究位点与理由。序列中的 Cys 只支持残基身份，不证明反应性；选择保持 exploratory_hypothesis，后续仍须检索和几何评估。拼写或物种需要更正时，在采集前用 revise_target 记录原因，不用另一靶点的证据顶替。不得把模型记忆写成已核验来源。

OpenScience 宿主绑定 request_id、工作流 ID 和当前版本，按阶段提供 `covalent_ligand_evidence` 与 `covalent_ligand_design_step` 的可用动作；不要另选旧 intake/chain/campaign、底层提交器或脚本绕过阶段门槛。需要在同一会话比较其他路线时，用 suspend 记录理由；暂停只释放会话的路线工具选择，不删除工作流、不取消后台任务。用 resume 恢复该工作流，单独 status 不会重新激活它。

母体比较通过才开放 prepare，准备通过才开放 generate。生成失败或零候选时，可以用 `reconsider` 记录原因并重开母体比较；不能降低验收门槛来追求阳性结果。后续阶段仍可补充证据，新增证据会使旧母体提案和当前设计交接失效，旧分支保留供 `inspect_branch` 检查。重新 finalize 后才允许新分支设计。报告交付不是不可返回的终态，也不是计算通过。

`plan_pose` 只导出计算输入；运行时未登记、真实三维评价未运行时明确报告缺口。现有底层能力仍供维护和登记执行层使用。下面的八阶段与脚本说明是这些底层能力的参考，不能视为当前科研工具已获得的调用权限。未接入统一入口的旧部署应报告版本情况，不声称阶段控制已生效。

## 输入与路线

核对靶点/物种/构建体、结构与哈希、链/插入码/altloc、author→UniProt 位点映射、母体化学身份、非共价 pose 来源、保留核心/药效团和可修饰位点。已知资料沿用；没有三维母体 pose 或位点映射时不能猜测。没有 LINK 记录不能单独证明母体非共价。

历史八阶段（已暂停的新计算路径）：`structure_intake → covalentize → carsidock → carsidock_selection → af3 → covdock_lo → independent_evaluation → pareto`。新目标可复用此流程结构，但需独立准备和验收输入，不能自动继承 EGFR 残基号、反应类型、阈值或算力计划。

- 输入接入与枚举：加载 `ligand-first-enumeration`。
- 当前批量路由、独立准备、LO 与历史精修记录：加载 `ligand-first-refinement`。
- 独立评价、Pareto、结果交付：加载 `ligand-first-evaluation`。

## 实际调用边界

首次使用读 [接口和能力清单](references/tool-contracts.md)。以当次工具目录、工作流阶段与 MCP 连接为准；仓库实现和网页路线卡不等于当前可调用接口。执行新计算需要实际接入的执行器及其资源许可，不能虚构工具名，也不能借用 Library-first 的执行接口。

查询进度沿原 campaign/run ID 检查父链、实际作业、质量门和交付记录，禁止为状态查询提交新任务。本地副本需带采集时间；不同批次、枚举后端或受体不能拼成一个“完成”父链。按 [证据索引](references/evidence.md) 查验历史批次；这些不是实时调度器状态。

输出方案写清已知输入、枚举后端、每阶段筛选/排序规则、数量上限、依赖、预算和停止原因；状态分别报告计算、核验和交付。MD/QM/ADMET/合成若未做，保留“未评估”。
