# 当前工具合同

范围：本地 OpenScience Library-first 集成，2026-10-14 源码整理。运行时 schema 与真实任务证据优先。此前会话已核实 covalent-discovery 服务连接并暴露 30 个工具；这些工具不是正式主筛插件的同义入口。源码位置相对 `openscience-custom/backend/cli/`，新源码发布状态须单独核验。

源码 1.22.0 共 47 个工具；Library-first 已新增候选冻结、单候选登记、Schrodinger 原生图 QC/加合物准入和只读 MD 续跑编排，并延伸到固定策略的候选 MD 报告。生产、审计、分析与报告接口完成合同、父链和 JobEngine 软件回归，真实历史体系只做了 0.02 ps 引擎及 DCD 读取诊断，明确不是科学轨迹；正式 500 ps 平衡、三条 10 ns 新代码轨迹、真实跨重复分析和新候选原生运行仍未完成，且未重新部署远端服务。

| 能力 | 当前入口 | 实际执行层 | 边界 |
|---|---|---|---|
| 数据库证据 | MCP `inspire-covindb` 的 `covindb_catalog` / `covindb_query` | `integrations/inspire-covindb/server.ts`、`query.py` | 只读；不是候选虚拟库筛选 |
| 靶点身份及序列 | `covalent_library_first_resolve_target`，若已暴露 | 共享科研包 `covscreen.target_identity` | gene + taxon；plan 离线，search 查询并保存证据；不选择反应位点或准备受体 |
| 无母体的结构发现 | `covalent_library_first_discover_structures`，若已暴露 | 共享 `covscreen.structure_discovery` 与身份产物校验 | target_artifact_id + target_manifest_sha256；有界候选页，不是可直接对接的受体 |
| 坐标与 Cys 核验 | `covalent_library_first_acquire_structure`，若已暴露 | 共享 acquisition、rcsb_mapping、pdb_coordinates；target_site_audit | 消费结构产物 ID/哈希；明确 entry/Cys，核验坐标，不修复或放行受体 |
| 注册受体制备 | `covalent_library_first_prepare_receptor`，若已暴露 | JobManager；共享 repair/relaxation/quality/geometry/admission | 消费 `receptor-input-*`；plan/start/status；当前唯一后端为 PDBFixer 1.12.0 + OpenMM 8.6.1；Prime 尚未实现且不能与 PDBFixer 串联；仅放行隔离单链蛋白范围，保持 campaign_ready=false |
| 位点环境测量 | `covalent_library_first_assess_receptor_context`，若已暴露 | 共享 `receptor_site_context.py` | 绑定 acquisition + completed preparation；SASA/局部壳层/沉积环境近邻只作描述，不放行反应性或 campaign |
| 主筛候选冻结 | `covalent_library_first_deliver_md_candidates`，若已暴露 | JobManager；`md_candidate_delivery.py` | 只接受 Pareto run ID + hash；自动追溯 CovDock 父任务并冻结候选和结构资产；不选择候选、不做图 QC |
| 候选 MD pose 登记 | `covalent_library_first_select_md_candidate`，若已暴露 | `md_candidate_pose.py` | 只接受交付/受体/环境的任务或工件 ID + hash 以及 compound/index；产生 native-graph-QC-pending 工件 |
| 原生图 QC 与加合物准入 | `covalent_library_first_admit_md_candidate`，若已暴露 | JobManager；注册 Schrodinger 检查器；`md_adduct_input.py` | 只消费候选 pose artifact ID/hash；输出 admission run 与 parameterization-pending 加合物 |
| 单候选加合物赋参 | `covalent_library_first_parameterize_adduct`，若已暴露 | `md_adduct_input.py`、JobManager、`md_parameterization.py` | 只消费 `covalent-adduct-input-*`；plan/start/status；原生图与 OPLS 审计后仍不是 MD-ready 系统 |
| OpenMM 体系准备 | `covalent_library_first_prepare_openmm_system`，若已暴露 | JobManager；`md_system_conversion.py`、`md_system_audit.py`、`md_system_minimization.py` | 只消费完成的赋参 run ID + audit hash；转换、独立力场核对、有界 CPU 最小化；成功后仍为 short-QC pending |
| OpenMM short-QC | `covalent_library_first_run_openmm_short_qc`，若已暴露 | JobManager；`md_short_qc.py` | 只消费完成的体系准备 run ID + manifest hash；固定种子无约束 CPU 积分稳定性；成功后仍为 equilibration pending |
| OpenMM 平衡 | `covalent_library_first_equilibrate_openmm_system`，若已暴露 | JobManager；`md_equilibration.py` | 只消费完成 short-QC run ID + manifest hash；固定 500 ps NPT，平台由部署登记；完成后仍需生产评审且不声称收敛 |
| OpenMM 生产 | `covalent_library_first_run_openmm_production`，若已暴露 | JobManager；`md_production.py` | 只消费准入平衡 run ID + hash；replica 1–3 各 10 ns；输出 DCD/checkpoint，完成后仍待轨迹审计 |
| 轨迹完整性审计 | `covalent_library_first_audit_openmm_trajectory`，若已暴露 | JobManager；`md_trajectory_audit.py` | 只消费完成生产 run ID + hash；校验 DCD/帧/哈希/共价距离；通过后仍待结构分析与三重复汇总 |
| 三重复结构分析 | `covalent_library_first_analyze_openmm_replicas`，若已暴露 | JobManager；`md_structural_analysis.py` | 强制同一平衡父链的 replica 1–3；输出逐帧及逐重复描述指标；不作亲和力、收敛或筛选结论 |
| 候选 MD 报告 | `covalent_library_first_build_md_report`，若已暴露 | JobManager；`md_candidate_report.py` | 只消费结构分析 run ID + hash；固定 v1 技术门槛和三重复共识；所有分类都要求跨证据专家复核 |
| MD 续跑编排 | `covalent_library_first_next_md_action`，若已暴露 | 复用上述工具各自的 `plan` | 验证当前证据后返回下一正式调用；不提交计算；强制三生产/三审计/专家复核 |
| 主筛能力发现 | `inspire_screening_capabilities`，若当前版本已暴露 | `integrations/inspire-screening/executor/mcp.ts` | 只读源码能力与缺口；不证明实时环境可用 |
| RDKit 准备、CarsiDock-Cov、RTMScore、报告 | `inspire_screening_task` | `integrations/inspire-screening/executor/tool.ts`、`worker.py` | prepare/carsidock/rtmscore/report 由后台按父链推进 |
| 精筛选样、AF3、CovDock-LO | 同一 `inspire_screening_task` | selection-proposal/refinement-workflow/refinement-execution/refinement-report | 独立选样及资源计划；不重跑主筛 |
| 固定批次 MD 和分析 | `molecular_dynamics` | `src/tool/molecular-dynamics.ts`、`integrations/md-workflow/registered.ts` | 已准备的八候选、三次 10 ns；不是任意输入 MD API |

MCP 工具在模型侧可能被附加服务名前缀；使用会话当前实际列出的名字，不猜测前缀。`inspire_screening_smoke` 是历史连接测试，不能拿来启动正式筛选。DeepCoSI、De novo 和 Ligand-first 不在本包执行范围。

正式插件经宿主绑定的进程内 MCP 调用原执行器，保留原始消息、资源卡和持久任务；不新增独立远程连接。新源码默认不再合并 `inspire_library_workflow` / `inspire_screening_smoke`；这些入口仍保留于历史插件，运行中的旧版本可能仍显示它们。MCP 结构化主筛返回为 `{result: 原执行器结果}`，文本保留原格式。

## inspire_screening_task

- `create`：要求 `request`；原文由主机重新绑定当前用户消息。可接收明确的 `target_parameters`、`target`、`site`、`library`、`screening_mode`、`max_points`。不能指定 task_id、任意命令、原始记录上限或 GPU 参数。缺条件进入平台 intake；已有授权范围沿用，具体计算计划仍由原生资源机制核对。
- `target_parameters`：target/receptor/chain/residue/residue_number/insertion_code/numbering/reaction_type；receptor_sha256 可选但是否准入由适配器决定。不能把结构编号与 canonical 编号混用。
- `library`：当前仅 `CovInDB/Virtual_Screening_Library.csv`。
- `screening_mode`：economy/standard/deep/test100。`max_points` 为非负有限数；null 仅用于用户明确提出 test100 不设点券上限的情形。省略不代表授权。
- `list`：只传 action；用返回的身份与请求选择任务，不默认使用“最新一条”。
- `status` / `report` / `approve` / `cancel` / `wake` / `resume_delivery` / `diagnose_preparation`：action + task_id。
- `select_refinement`：action + task_id + selection_policy，具体见精筛 skill。
- `diagnose_refinement` / `plan_refinement` / `approve_refinement` / `cancel_refinement` / `refinement_status` / `refinement_report` / `resume_refinement_delivery`：action + task_id；需使用原会话的动作按主机限制执行，不复制任务绕过绑定。
- `download`：action + task_id + artifact；artifact 必须来自本任务已核验清单，严格遵循当前 schema 的 phase/name，不能传任意路径。

任务 ID 来自平台，格式 `screen-` 加 24 位十六进制。生命周期动作不允许修改冻结的任务参数。`approve` 只是重新打开既有草案的原生审批，不代表模型已批准。`wake` 只恢复观察器，不重提交；`resume_delivery` 只恢复文件交付。取消已提交任务时，等待实际停止回读，不能把请求已发送当作已停止。

## 规模策略（不是吞吐保证）

| 模式 | 源记录上限 | 入对接候选数 | prepare / CarsiDock / RTMScore / report 时限（分钟） |
|---|---:|---:|---|
| economy | 2,000 | 3 | 15 / 10 / 5 / 5 |
| standard | 10,000 | 5 | 30 / 20 / 10 / 5 |
| deep | 50,000 | 20 | 60 / 60 / 30 / 10 |
| test100 | 100,000 | 100 | 60 / 120 / 60 / 10 |

版本真源：`integrations/inspire-screening/executor/resource-planner.ts`。主机根据实时费率核验预算；不能静默降规模。源记录按文件顺序有界读取，不称为全库、随机或代表性抽样。

## 非执行调用示例

```json
{"action":"list"}
```

取得确切 task_id 后调用 `{"action":"status","task_id":"实际任务ID"}` 或 `report`。MD：`{"action":"plan"}` / `{"action":"status"}`。示例里的文字不是合法 task_id；不得原样提交。

机器可校验的参数示例在本技能包源码的 `examples.json`，由验收程序读取真实 Zod schema 检查；示例不授予启动新计算的权限。
