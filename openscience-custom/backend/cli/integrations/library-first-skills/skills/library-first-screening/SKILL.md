---
name: library-first-screening
description: Plan, inspect and advance the configured OpenScience Library-first covalent screening route with the CovDock-VS-first policy, historical results and registered refinement/MD. New CarsiDock-Cov execution is suspended. Use for library screening requests, route progress or selection rationale.
category: chemistry
tags: [covalent, library-first, screening]
---

# Library-first 共价筛选

## 当前引擎策略（2026-11-02 用户决定）

后续构象暂停使用 **CarsiDock-Cov**；不得新提交、重试、重新调度或自动回退到它。优先采用 **Schrödinger CovDock-VS**。本节优先于本文及引用文件中的历史阶段说明。

原生 CovDock 接口目前为 LO 精修，VS 初始构象工作流尚未接入；不得仅改引擎名称、借用 LO 身份或用独立试验结果伪造原生通过记录。遇到仍依赖 CarsiDock 的步骤，报告 `docking_engine_disabled` / VS 接入缺口，继续可独立进行的输入核验和历史结果审阅。不要重新询问用户是否停用该引擎。

历史记录、结构和评分保留供只读追溯。CovDock-VS 的无严重碰撞与正确共价连接不等于完整科学门禁通过；应变、关键相互作用及 MD 准入仍须核查。

以当前项目实际暴露的工具及其参数 schema 为执行接口。此 skill 描述已配置平台，不把工具安装成功或历史单批完成当作任意靶点已可运行。

若已暴露 `inspire_screening_capabilities`，先用它核对主筛支持范围与新候选 MD 准备能力。返回的 `runtime_verified=false` 表示没有检查实时资源；工具清单或 capability 不是科学验收。该工具未暴露时依据当前实际 schema 和已验证范围，不猜测工具名或声称升级已上线。

## 先判断用户意图

- 查询进度：`inspire_screening_task` 的 `list` → 选择与当前项目/请求匹配的原任务 → `status`。不能为查询状态创建新任务。
- 解释结果：核心阶段用 `report`，精筛用 `refinement_report`；检查核验状态与交付状态后再报告。MD 用 `molecular_dynamics` 的 `status`。已有文件仅按证据所属批次解释。
- 设计新筛选：核对靶点、物种/构建体、结构、链、位点编号映射、反应类型、虚拟库、规模和预算。用户已经给定的信息沿用，不反复询问。关键未知保持未知。
- 执行新筛选：仅在用户有执行意图时调用 `create`。该动作会运行预检并在同一次调用中展示原生资源审批；它不是纯只读规划。审批通过后后台执行主筛四阶段，不必逐阶段再询问用户。

若实际工具清单包含 `covalent_library_first_resolve_target`，新靶点可先用基因名和明确物种解析身份；无需先提供母体配体或猜测 Cys。`plan` 不联网，`search` 保存有哈希的 UniProt 原始响应及 canonical FASTA。只有完整查询中的唯一核验身份可用于后续准备；Cys 位置列表只是序列观察，必须继续取得结构、编号映射和位点证据。工具未暴露时不声称已上线，不用既有 Ligand-first 母体默认值填补缺口。

若已暴露 `covalent_library_first_discover_structures`，直接传入身份工具返回的 `artifact_id` 和 `manifest_sha256`，不要手填 UniProt 或拼路径。`search` 只返回有界的结构候选页；读取 `complete_search`，不能把第一条当成最佳受体。结构覆盖区域、链、突变、缺失位点及反应条件仍需核验，发现条目不代表受体已准备好。

若已暴露 `covalent_library_first_acquire_structure`，传入结构发现产物 ID/哈希，从该页选择有依据的 entry 和 canonical Cys，获取真实坐标与编号映射。位点未覆盖、缺原子、多链/模型/构象歧义必须处理后才能继续。返回 `receptor-input-*` 仍是待准备输入；即使坐标检查通过，也不能跳过物种、突变、补环、质子化和反应性判断，不能直接提交主筛。

若已暴露 `covalent_library_first_prepare_receptor`，先用 `plan` 消费上一步返回的 `receptor-input-*` 和 manifest 哈希。整条目标链出现替代构象时，必须为每个歧义 author residue 显式给出 `altlocs`；不猜测构象。核对冻结父证据、单 CPU 预算、固定 seed 和注册运行时后，只有用户已批准实际计算时才用 `start`，再用同一工具的 `status` 或通用任务状态接口读取结果。`prepared_isolated_protein_scope` 只证明所选单链蛋白通过补原子、质子化、松弛、身份/立体和几何审计；继续检查生物组装体、辅因子、口袋适用性、位点可及性和支持的共价反应。只要 `campaign_ready=false`，就不能提交主筛。

当前 1.22.0 的注册受体修复后端是 PDBFixer 1.12.0，松弛后端是 OpenMM 8.6.1。Schrödinger Protein Preparation Wizard/Prime 虽能补侧链和环区，但此版本尚未把 Prime 实现为该 MCP 的后端；Schrodinger/CovDock 可用、历史 Prime 脚本存在或模型知道 Prime 的功能，都不能视为已经接入。每条受体谱系只运行一个修复后端，不把 PDBFixer 与 Prime 串联。缺失环靠近口袋或目标 Cys 时，报告 Prime/专家建模需求和当前 `capability_not_implemented`，不得让 PDBFixer 的自动修复替代该判断。未来 Prime 后端只有在固定目标 Cys 化学状态、保留观测重原子并通过同一原子图、手性和几何审计后，才能交付同一 prepared-receptor 合同。

部署时 PDBFixer 和 InterMol 必须来自当前不可变源码发布包；可写 campaign/data root 只保存配置、任务账本和产物，不复制 vendor 或脚本。工具返回的 source-release ID 与任务 provenance 不一致时停止恢复，不能从历史研究目录寻找同名程序继续执行。

若已暴露 `covalent_library_first_assess_receptor_context`，把同一 `receptor-input-*`/manifest、已完成 preparation run ID 及 prepared manifest 哈希原样传入。读取 SG/整残基 SASA、局部蛋白壳层和原始沉积环境近邻；不得把 4.5/6/8 Å 报告半径或两个真实回归值当作准入阈值。对邻近配体、水、离子、辅因子和其他链逐项形成保留/移除依据，并另行补充支持的反应类型与 Cys 反应性证据。该工具返回 PASS 只表示描述性工件生成成功，不表示 `campaign_ready`。

当前主筛适配器只接受已核验组合：EGFR、1M17、A 链、canonical CYS797、空插入码、Michael Addition；实际受体映射为 1M17 A:CYS773 → EGFR C797。不得从基因名猜测此映射；新靶点需要新适配器，不能回退 EGFR。`receptor_sha256` 字段虽存在，当前准入实现不接受自行添加该字段的组合。

历史核心执行顺序（新对接已暂停）：虚拟库准备/QC → CarsiDock-Cov → 已有姿势 RTMScore → 核验报告；之后按真实结果提出精筛子集，另行形成 AF3/CovDock 计划。源码已能把完成的 Pareto/CovDock 结果依次冻结、选择、执行原生图 QC/加合物准入，并接续赋参、OpenMM 和报告任务；只读 `covalent_library_first_next_md_action` 已统一编译合法下一步。当前仍缺真实新候选完整运行验收，因此不能声称任意新精筛分子已经自动跑通 MD。

## 按阶段加载

| 任务 | 加载 skill |
|---|---|
| 查询已知抑制剂证据，解释库准备/化学筛选 | `library-first-prescreen` |
| 主筛创建、CarsiDock/RTMScore、身份和失败检查 | `library-first-docking` |
| 分歧/共识/多样性选样，AF3/CovDock 与精筛交付 | `library-first-refinement` |
| 固定批次 MD、轨迹分析和图表解释 | `library-first-md-analysis` |

完整接口索引见 [工具合同](references/tool-contracts.md)；首次解释验收范围或遇到旧文档矛盾时读 [已验证范围](references/validated-scope.md)。其他 skills 为本包兄弟目录；不可用时读取对应文件并报告发现问题，不改用旧 MCP 工具名。

## 决策与呈现

方案分别写明：输入证据、化学/结构筛选门槛、排序依据、晋级数量、计算预算、继续/停止条件及尚缺的输入。每一步保留输入数、成功数、失败数、排除原因、原始候选 ID、源文件及哈希。模型不填写 `human_approved` 或伪造审批回执；资源权限由实际工具执行，不新增无必要的聊天确认。

用户要求“筛选后自动 MD 和分析”时，在完整方案中保留这一阶段和候选晋级条件。源码中的通用新候选准备与续跑合同已经接通，但运行时未登记、当前工件不合格或真实验收尚未完成时仍须明确报告缺口；不能用固定八体系的历史结果替代新候选，也不能将仅主筛完成报告为完整目标完成。

若工具清单包含 `covalent_library_first_next_md_action`，优先用它从当前正式阶段继续。传入 stage 与该阶段工具返回的 run/artifact ID 和 manifest SHA256；它只编译下一工具的计划，不提交任务。候选选择可按返回的 `select` 调用登记；计算阶段仍须通过对应工具的原生批准执行 `start`。不得跳过工具报告的 blocker，也不得删减平衡后生成的三条生产计划或三条轨迹审计。

若工具清单包含 `covalent_library_first_deliver_md_candidates`，只传完成的 Library-first Pareto run ID 与 `stage6_pareto_manifest.json` 哈希，先 `plan`。工具沿任务父链取得 CovDock manifest，核对 Pareto/CovDock 质量门、候选表和每个 AF3/CovDock 结构哈希；模型不得传表格或结构路径。获批后 `start`，再以 `status` 读取 `candidate-results.json` 及其哈希。该产物只表示候选和结构资产已冻结，`candidate_selected_for_md=false`，不能直接进入赋参。

若工具清单包含 `covalent_library_first_select_md_candidate`，从冻结交付中按 `compound_id + input_sdf_index` 显式选定单行，并传入正式 preparation run/hash 与 receptor-context artifact/hash。先用 `plan` 核对候选化学、目标位点和全部父证据；确认选择后用 `select` 取得 `candidate-pose-input-*` ID 与 manifest 哈希。模型不得传任意 MAEGZ、候选表或受体路径。该工件仍待原生图 QC，不能直接送入赋参。

若工具清单包含 `covalent_library_first_admit_md_candidate`，只传上述 candidate-pose artifact ID 与 manifest 哈希，先 `plan` 检查父证据、单候选 CPU 预算和登记的 Schrodinger runtime。获批后 `start`，再以 `status` 读取 admission 与 adduct-input manifest/hash。只有状态为 `ADDUCT_ADMITTED_PARAMETERIZATION_PENDING` 且 `native_graph_qc_completed=true`，才能进入赋参；这仍不代表参数或 MD 系统已准备好。

若工具清单包含 `covalent_library_first_parameterize_adduct`，优先传完成的 admission run ID 与 status 返回的 adduct-input manifest 哈希。先 `plan` 检查父证据、单候选 CPU 预算与 Schrodinger runtime；运行时未登记或工件缺失时报告明确 blocker，不允许模型传任意 MAE/QC 路径或借用固定八体系目录。用户已授权真实赋参后才 `start`，再用 `status` 检查 `parameterization-audit` 与 `finished`。即使状态为 `COVALENT_PARAMETERIZATION_AUDITED`，也仍需系统转换、最小化、短程能量/力一致性、无约束平衡和轨迹审计，不能直接称为 MD ready。

若工具清单包含 `covalent_library_first_prepare_openmm_system`，把已完成 parameterization run ID 和该任务返回的 `parameterization-audit` SHA256 原样传入，先 `plan`。该工具不接受 CMS 文件路径；它重放父任务及审计链，使用登记的 OpenMM/InterMol 环境依次转换、独立核对力场项并做有界 CPU 最小化。用户已授权实际计算后才 `start`，再以同一工具 `status` 读取 conversion、forcefield audit 和 minimization。即使返回 `OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING`，也不得启动生产 MD；下一门槛仍是独立 short-QC。

若工具清单包含 `covalent_library_first_run_openmm_short_qc`，只传已完成 system-preparation run ID 及其 `openmm-preparation.json` 哈希。先 `plan` 核对父链、固定种子、CPU 预算、步数与采样间隔；`steps` 必须能被 `report_interval` 整除。获批后 `start`，再用 `status` 读取逐帧积分证据。`UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING` 只放行到平衡阶段，不能直接进入生产或分析。

若工具清单包含 `covalent_library_first_equilibrate_openmm_system`，只传完成 short-QC 的 run ID、manifest 哈希和两个不同随机种子。`plan` 返回已登记的 CPU/CUDA 平台及固定 500 ps NPT 协议；资源预算的 `gpu_count` 必须与登记平台一致。获批后 `start`，长任务可用同一工具 `status` 或通用 job/status/log/cancel/retry 接口管理。只有 `NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING` 才能提出生产准入；诊断状态和协议完成均不能表述为收敛。

若工具清单包含 `covalent_library_first_run_openmm_production`，对同一正式平衡工件分别为 replica 1、2、3 调用 `plan/start/status`。不要自行指定速度文件；工具从 seed base 和 replica 推导三类独立种子。每个任务固定 10 ns，只有 `PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING` 才可交给轨迹审计。三个任务完成不等于分析完成，也不能只选最好的一条重复。

若工具清单包含 `covalent_library_first_audit_openmm_trajectory`，每条生产重复分别传入其 run ID 和 `production.json` 哈希，按 `plan/start/status` 执行。工具只验证 DCD 结构、帧序列、文件哈希、有限坐标和共价键距离；`TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING` 只允许进入后续分析。三条重复必须全部审计，不得因某一条指标较差而省略；完整性审计不能表述为构象稳定、亲和力或动力学结论。

若工具清单包含 `covalent_library_first_analyze_openmm_replicas`，一次传入三条正式审计任务的 run ID 与 manifest 哈希。工具会拒绝缺少 replica、重复编号或混用平衡父链；读取全部帧并返回描述性结构指标。`THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING` 仍需依据预先声明的 Library-first 判断标准生成候选级结论与报告，不得由模型挑选最好的一条重复，也不得从 RMSD 单项推断亲和力。

若工具清单包含 `covalent_library_first_build_md_report`，只传完成的 structural-analysis run ID 与 manifest 哈希。该工具使用固定的 `library-first-covalent-md-structural-triage-v1`，模型不得临时调整阈值以改变候选分类。读取 JSON 与 Markdown 中的技术门槛、逐重复失败项、三重复共识和限制；即使分类为 `STRUCTURALLY_SUPPORTIVE_FOR_EXPERT_REVIEW`，也只能进入对接、化学、选择性和实验信息的跨证据专家复核，不能报告为活性或亲和力已证实。

不同引擎分数保留各自定义和方向，不直接加权求和；排序分歧是复核线索，AF3 置信度、对接分数和 MD 稳定性均不是实验活性。未评估的项目写“未评估”，不能推断为通过。

界面汇报区分：计算状态、科学核验、结果交付、科学解释。给用户当前阶段、数量、阻塞原因和可访问结果；远端 `/inspire/...` 路径本身不是浏览器下载地址。
