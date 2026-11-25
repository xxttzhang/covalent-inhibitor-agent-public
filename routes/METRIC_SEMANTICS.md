# 共价候选评价：指标语义合同 v1

日期：2026-10-20。范围：Ligand-first、Library-first、De novo。

状态：本次确定的科学与接口迁移合同；不是已部署的运行实现。本次不修改数值阈值、现有结果、审批或执行器。后续策略、报告和 MCP 适配以本合同为依据。实现尚未符合的条款必须标记缺口，不能将文档约定当成已执行验证。

## 1. 结论边界

每个指标仅支持它直接测量的结论。通用代码可执行不代表跨靶点、战头或化学态完成校准。候选排名、计算正常结束、结构质量合格和实验活性是不同结论。

三条路线共享身份、结构质量、证据状态和下游验证定义；各自保留候选来源及研究约束。不同路线不要求相同的指标集合，也不因缺少不适用的母体而惩罚 Library-first 或 De novo。

评价对象必须属于以下明确状态：

| 状态 | 含义 |
|---|---|
| free_ligand | 指定连接图、立体化学、质子化/互变异构态的游离分子 |
| prereaction_complex | 蛋白与配体未建立目标共价连接的反应前结构假说 |
| covalent_adduct | 已指定反应产物连接图、原子映射及位点的加合物结构假说 |
| simulation_system | 给定力场、共价拓扑、溶剂、边界与约束的模拟体系 |
| experimental_sample | 有样品身份、实验条件、对照和原始数据的实验对象 |

同一候选不同状态之间通过显式反应映射关联，不能当作相同拓扑直接比较。反应前距离/进攻方向与产物键长/键角不得互换。直接预测加合物的路线可以提交产物结构，但必须将缺少的反应前证据如实保留，不能伪造反应路径或将其自动计为失败。

## 2. 指标角色与证据状态

角色由冻结的项目策略指定，不能根据测量结果临时更改：

- identity_gate：身份、化学合法性、映射与所选反应的必要一致性检查。
- physical_validity_gate：在明确模型、环境、原子选择及可靠判定方法下的物理合理性检查。
- task_constraint：本次研究明确选择的约束，违反只说明不符合当前分支，不能推断无活性。
- risk_annotation：风险提示，不能单独作为已证实毒性、不可成药或非选择性的结论。
- ranking_signal：可比较集合内的排序依据，不能解释为通过/失败或实验效力。
- technical_qc：文件、算法表示、数值积分和运行质量检查。
- mechanistic_evidence：支持明确机制命题的计算或实验证据，必须保留证据类型与局限。

单项记录分开表达：

- measurement_status：available / not_run / unavailable / failed / not_applicable。
- decision：pass / fail / warn / undetermined / not_applicable；纯排序指标只返回值、方向与比较范围，不生成 pass。
- 未配置可解释的判据、未完成方法适用性核验或必要数据缺失时，不得给出 pass。可以提供探索性数值和 undetermined。
- 缺少可选指标不否定已有有效测量；缺少当前阶段必需指标则阻止该阶段作出通过结论。
- not_applicable 必须有适用性理由，不能用于隐藏失败。

保留现有 workflow_protocol 的 execution 与 assessment 外层状态，通过显式适配映射这些语义，不另建工作流状态机。旧报告继续保留原始标签，不能静默升级为新语义下的通过。

## 3. 指标定义与允许的结论

### 身份、性质与路线约束

| 稳定语义名 | 实际测量/核对对象 | 允许支持的结论 | 不能支持的结论 | 默认角色及失败解释 |
|---|---|---|---|---|
| chemical_identity | 原子、键级、立体化学、形式电荷、组分及变换前后映射 | 本记录对应预期化学实体；在指定规则下有效 | 已合成、稳定、具有活性 | identity_gate；错误阻止该资产交接；未知立体化学要显式声明 |
| target_site_identity | 物种、序列、构建体、突变、链、canonical/author/entity 位点映射 | 计算使用了指定靶点和位点 | 此 Cys 必然可反应或该结构代表生理状态 | identity_gate；映射不明为证据不足，不猜编号 |
| warhead_membership | SMARTS/反应模板匹配、具体反应原子与候选匹配数 | 分子属于策略允许的结构类别，存在明确反应位点假说 | 本征反应速率、选择性、可逆性或共价形成得到证明 | task_constraint + identity_gate；多匹配必须消歧或显式分支，不默认多战头无效 |
| molecular_weight | 指定游离分子/盐处理规则下的质量 | 分子大小及项目范围符合程度 | 活性、口服利用度 | task_constraint 或 risk_annotation；片段与大分子策略分开 |
| calculated_lipophilicity | 指定模型的 cLogP；logD 另附 pH、pKa 与模型 | 该模型下脂溶性估计 | 溶解度、通透性、暴露量得到确认 | risk_annotation / ranking_signal；不得混用 logP 与 logD |
| topological_polar_surface_area | 指定化学态的 TPSA | 拓扑极性描述符 | 实际暴露极性、细胞/血脑屏障通透性 | risk_annotation / ranking_signal |
| rotatable_bond_count | 明确可旋转键定义的计数 | 分子柔性的一种粗略描述 | 构象熵或结合自由能 | risk_annotation / ranking_signal |
| charge_state | 指定微观化学态的形式电荷；若预测分布则附 pH/方法 | 被计算实体的电荷状态 | 生理 pH 主导状态已确认 | identity_gate 对身份；risk_annotation 对性质 |
| protected_scaffold_displacement | 对应母体核心原子的空间位移，注明受体对齐与原子集合 | 是否保留当前分支要求的母体空间占位 | 姿势正确、结合能力或所有路线都必须保留母体 | task_constraint；不适用于无母体任务；失败可触发新设计分支 |
| protected_contact_retention | 预先指定的配体—蛋白原子接触满足距离规则的比例及逐项结果 | 指定几何接触被保留 | 已形成氢键/盐桥、作用能保留或总体亲和力不变 | task_constraint / ranking_signal；核心锚点单独列示，空集合不能伪造 0% 或 100% |
| generation_constraint_integrity | 固定原子、允许生长位点、战头/片段保留、来源 | 生成符合冻结任务约束 | 生成物可合成或有活性 | task_constraint / identity_gate；记录原始及优化后分母，不能合并通过数 |

### 几何、能量与预测一致性

| 稳定语义名 | 实际测量/核对对象 | 允许支持的结论 | 不能支持的结论 | 默认角色及前提 |
|---|---|---|---|---|
| prereaction_approach_geometry | 未成键复合物中核亲原子—亲电原子距离、按反应定义的方向/二面角及可达性 | 与某反应路径假说相容的空间预组织 | 反应能垒、反应速率、共价反应已发生 | mechanistic_evidence；按反应类型定义，不能借用产物指标 |
| product_bond_geometry | 明确产物拓扑下实际成键原子的键长、局部键角/手性 | 加合物局部结构与选定产物模型相容 | 反应前可达性、动力学或稳定产物已被实验确认 | physical_validity_gate；按元素、键型、反应和方法校准 |
| dummy_site_alignment | 对接虚拟核亲原子与实际目标原子的位置差 | 对接表示与受体锚点的一致性 | 化学反应能力或真实成键键长合格 | technical_qc；仍需直接测量真实目标原子与反应碳 |
| nonbonded_overlap | 选定环境的逐原子非键接触、距离/半径比、最严重冲突与参与原子 | 给定拓扑和环境下是否存在空间冲突 | 靶点选择性或结合能 | physical_validity_gate；按拓扑排除成键/近邻对，禁止整残基豁免；缺失环境写入范围 |
| ligand_local_relaxation_energy | 同一化学态在指定方法下，从输入坐标到收敛局部极小值的能量差 | 局部松弛可释放的模型内部能量 | 完整构象应变、结合自由能、反应能垒 | risk_annotation；当前 MMFF/UFF 单起点计算归此类，缺参数/不收敛为 undetermined |
| ligand_conformational_strain | 同一分子、化学态、能量方法下，合理处理的结合构象与明确参考构象集合的能量差 | 该模型下构象能量代价的估计 | 亲和力或共价形成代价 | risk_annotation；经适用性校准后才可设物理门槛；跨分子比较须注明局限 |
| parent_relative_strain_change | 方法与参考定义一致时，候选与母体各自应变估计的差 | 相对变化的辅助信息 | 候选绝对应变可接受 | ranking_signal / risk_annotation；禁止替代候选自身评价 |
| docking_or_rescoring_score | 明确引擎、模型版本、反应态和受体下的打分 | 同一已定义可比集合内的模型偏好 | 实测 Kd、IC50、kinact 或跨引擎绝对优劣 | ranking_signal；保留原始分数、方向与集合，不直接相加不同单位分数 |
| structure_prediction_confidence | AF3 等模型的具体置信度，附原子/界面选择与聚合方法 | 模型对结构或相对定位的置信度 | 相互作用能、亲和力、配体内部化学质量全面合格 | ranking_signal / risk_annotation；PAE、pLDDT 等不能合并称为“亲和力” |
| cross_method_pose_agreement | 明确受体对齐、化学态、原子映射、对称性处理后的姿势差异 | 两种预测在定义范围内是否接近 | 任何一种预测必然正确 | risk_annotation / ranking_signal；反应前后仅在声明的共同原子集合比较 |
| pareto_rank | 冻结候选集合、有效指标及方向下的非支配层级 | 在本批次指定目标下的相对优先级 | 活性概率、与另一批次的绝对可比性 | ranking_signal；报告分母、缺失指标、相关/重复信号及选择规则 |

### MD 与机制证据

| 稳定语义名 | 允许支持的结论 | 不能支持的结论 | 使用规则 |
|---|---|---|---|
| simulation_numerical_qc | 轨迹及积分在检查范围内数值可用，温度/密度等符合所选体系的运行要求 | 已达平衡、已收敛或分子稳定 | technical_qc；与科学结论分开 |
| trajectory_structural_behavior | 给定拓扑、约束、时间窗与采样下的位姿、接触、溶剂和局部几何变化 | 真实驻留时间、共价生成能力或高亲和力 | mechanistic_evidence；报告重复、约束、对齐、采样局限和不确定性 |
| covalent_bond_persistence_in_fixed_topology_md | 所指定共价拓扑下连接几何的数值行为 | 共价键自然形成或反应不可逆 | technical_qc；键由拓扑/约束维持时，持续存在不是独立成键证据 |
| intrinsic_electrophile_reactivity | 在明确模型硫醇或计算模型及条件下的反应性 | 对目标的选择性或蛋白组安全性 | mechanistic_evidence；GSH 等模型实验附浓度、pH、温度和动力学模型 |
| target_directed_covalent_mechanism | 与具体命题相匹配的时间依赖、加合物/位点、对照或反应能垒证据 | 单一读数即可证明完整抑制机制 | mechanistic_evidence；实验与计算分开，不用 docking 或固定拓扑 MD 补足 |
| inhibition_kinetics | 在适当模型与可辨识数据下的抑制动力学参数 | 任意体系均可用同一两步不可逆模型解释 | mechanistic_evidence；kinact、KI 及比值仅在模型适用时使用；可逆共价另定模型 |
| selectivity_and_developability | 测试范围内的选择性、暴露、溶解性等具体测量 | 未测试体系中的安全性或成药性 | 单项独立记录；缺数据不得称为低风险或安全 |

## 4. 当前实现必须保留的真实含义

1. `ligand_first_geometry._scaffold_rmsd` 是原坐标系下指定原子的位移 RMSD，没有对配体单独最优叠合；命名与报告必须说明坐标系。新增跨结构比较需先核对受体对齐，不能悄悄换公式。
2. `_interaction_measurements` 只检查指定原子对距离。新增氢键等类型时须有相应供受体、质子化、方向和几何定义；现有结果只能称为接触保留。
3. `attack_angle_s_cbeta_calpha_degrees` 在已连接虚拟硫的产物上计算，归入 product_bond_geometry，不能解释为 prereaction_approach_geometry。兼容旧键名时附语义名及说明，不改历史文件。
4. 原 `covalent_cs_bond_distance_angstrom` 来自虚拟硫，归入产物对接表示的局部几何检查。2026-10-20 本地新增 `actual_sg_cbeta_distance_angstrom` 与 `actual_sg_cbeta_calpha_angle_degrees`，直接使用已映射受体 SG；分别沿用冻结的产物距离/角度窗口检查。新增检查不构成反应前路径或反应发生证据；旧记录缺少这些字段不能补记为已通过。
5. 历史碰撞函数排除整个目标残基、忽略 HETATM，且允许一定冲突原子比例。2026-10-20 本地已改为 CYS SG—映射 β 碳连接图的跨界 1-2/1-3/1-4 对排除，目标残基其余原子参与检查；新增冻结策略 `max_severe_clash_pair_count` 默认 0。旧规则和结果原位保留；缺少新策略字段的计划不能在新运行时静默继承规则。此检查仍只覆盖沉积蛋白 ATOM 重原子，跨成键近邻及 HETATM 环境尚需单独评价，不能声称完整环境已验证。
6. 当前 `_strain_energy` 为单起点局部最小化能量差。2026-10-20 本地已加入 RDKit 收敛返回码和有限能量检查，未收敛不产生有效能量；新记录标记 `local_relaxation_convergence_checked_v1`，旧记录默认 `legacy_unchecked`。参考构象与加合物截断定义仍需完善。仅限制相对母体增量不能据此宣称候选绝对应变合理。
7. CovDock 的 local_strain_energy 必须追溯其实际输出字段、方法和参考态；未核对前不能与 RDKit 指标视为同一能量。
8. AF3/CovDock 的一致性只表示定义范围内的预测接近。当前 HIGH/LOW 标签不是结构正确性等级。

## 5. 策略、MCP 与结果合同

语义目录只有这一份；数值策略由后端维护。提示词和宿主不得复制另一套阈值或科研状态机。

后续每项机器可读定义至少包含：

`metric_id, semantic_version, entity_state, applicability, definition, atom_selection,
units, method_version, role, supported_claim, unsupported_claims, missing_behavior`

每次实际评价至少绑定：

`policy_id/version/hash, calibration_status, input_asset_refs/hashes,
target/construct/site, candidate/chemical_state/reaction_mapping,
metric_id, measurement_status, value/units, criterion, decision,
reason/evidence_refs, execution_run_id`

其中 criterion 是明确阈值/类别规则，或排序方向；此文不指定新数值。校准状态区分 exploratory_default、retrospectively_calibrated、externally_validated，并记录对应数据集与范围；测试通过不提升科学校准等级。

已有工作流阶段工具应返回当前生效策略摘要、逐项结论及允许的下一步，不新增每指标独立 MCP。现有调用来源：

- Library 初筛：`covalent_submit_job(engine=covindb-prescreen)` 及现有路线编排。
- Ligand 几何：`covalent_ligand_design_step(action=evaluate_pose/prepare_refinement)`；历史 `covalent_evaluate_ligand_first_candidate` 保留兼容。
- CovDock：注册 `schrodinger-covdock-lo` 执行及其评价模块。
- De novo：现有生成器专用验收器；只有实际测量到的项目可适配。

变更语义定义、策略或输入时，保留旧结果，创建新版本并追踪依赖。纯展示更名不触发重算；改变原子集合/拓扑/测量公式需重测；改变阈值可在原始有效测量上重新判定，但若候选已被早期截断，必须恢复全分母或重跑受影响筛选，不能只对幸存者改标签。

## 6. 阶段交接与验收

允许的报告措辞包括“化学身份核验通过”“满足当前母体保留约束”“在指定环境下的产物结构检查通过”“已生成模拟可用输入”。每项必须附范围。不得从这些结果自动得出“共价抑制剂已验证”。

进入 MD 需要候选的明确选择、可核验的受体及产物身份、参数化/体系准备证据和单独运行授权。排名领先、几何通过或已有受体都不单独授予 MD 准入。

迁移验收实例：

| 输入情形 | 必须得到的解释 |
|---|---|
| 丙烯酰胺匹配，但未测反应性 | 战头类别符合；反应性未验证 |
| 已成键产物角度合理，但无反应前结构 | 产物局部几何相容；进攻几何未评价 |
| 与母体应变差小，但候选自身异常 | 相对变化不能覆盖自身风险 |
| 接触列表为空 | 接触保留不可评价或有理由的不适用；不是零分活性或全部通过 |
| 两模型姿势接近，但化学拓扑错误 | 一致性不能覆盖身份门槛失败 |
| 同一共价键在固定拓扑 MD 中持续存在 | 模型连接保持；不构成真实反应发生证据 |
| 高排名候选缺少必需位点映射 | 保留原排名事实，阻止依赖该映射的交接 |
| 已完成计算，但指标方法不适用于该战头 | 执行完成；相应科学评价 unsupported/undetermined |
| 修改阈值后只剩旧版筛选幸存者 | 不得声称对原候选全集完成新策略筛选 |

## 7. 依据与校准边界

本合同中的角色分配和工程处理规则是本项目的设计决策；以下来源支持其科学边界，不提供本项目全部数值门槛的直接验证。

- [Veber 等，2002，理化性质与大鼠口服利用度原始研究](https://citeseerx.ist.psu.edu/document?doi=8493576a7927322ebb79c353bc1c1100a186eb50&repid=rep1&type=pdf)：性质经验规则不等于具体候选活性或人体暴露的验证。
- [PoseBusters 原始研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC10901501/)：化学、几何及分子间合理性需分别检查。共价体系的拓扑排除需另行适配。
- [Jain 等，2023，构象应变研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC9923749/)：应变估计及其分布与结构处理、分子大小和构象复杂性有关。
- [EMBL-EBI，AlphaFold 3 质量评价](https://www.ebi.ac.uk/training/online/courses/alphafold/alphafold-3-and-alphafold-server/how-to-assess-the-quality-of-alphafold-3-predictions/)：置信度描述结构预测；配体 pLDDT 不覆盖所有内部配体几何。
- [GROMACS 拓扑说明](https://manual.gromacs.org/documentation/2025.2/reference-manual/topologies/topologies.html)：相互作用由指定拓扑定义；本合同据此限制固定拓扑轨迹的成键机制解释，非限定平台使用该引擎。
- [Craven 等，2018，靶点引导共价配体动力学筛选](https://onlinelibrary.wiley.com/doi/full/10.1002/anie.201711825)：区分本征反应性与靶点引导作用需要相应动力学和对照证据。
