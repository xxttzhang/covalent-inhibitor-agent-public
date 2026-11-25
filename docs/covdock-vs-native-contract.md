# CovDock-VS 原生输入与执行计划契约

## 2026-11-05 RC8 网页复验与诊断解释修订

固定版 3fa75ee 已完成单后端交接，原研究保留。原任务诊断已由网页读取，
展开运行步骤内审批可访问；纠正版 Results v2 已重开并下载核对哈希。
这些验证没有使受体通过，也未执行新的科研计算。

网页模型曾将 involves_modeled_atom=false 解释反，并把参考边界当作硬阈值。
本次源码补充明确字段解释契约：false 指保留观测原子对；示例采用 entity_position；
参考距离须结合容差；诊断不证明碰撞成因。当前 v1 松弛将观测重原子质量设为零，
并检查其漂移，因此原样重试不能修复固定观测原子之间的碰撞。恢复需要有根据的
来源修订或另行实现、验证的准备策略，不能仅重提或降低门槛。

这一解释补丁尚未部署，运行中的 RC8 仍为 3fa75ee。完整 VS→LO→MD 网页验收未完成。
验收记录位于源码外 outputs/source-baseline-20261029/rc8-web-acceptance-20261105.md。

## 2026-11-04 RC7 受体失败诊断修复（历史源码检查点，随后已部署 RC8）

RC7 已通过原网页独立批准并执行 2ITY 受体准备；原任务
`20261104T111111-library-first-receptor-preparation-f6a3efc10f` 退出码为 1。
补建、松弛、身份与立体化学阶段完成，独立几何审计不通过：1 处内部距离违规、
7 处范德华碰撞。没有生成已准入受体或新 VS 输入，也没有自动重提。

本次修复让 `receptor_preparation_status` 返回原任务登记产物中的失败阶段、
几何检查计数与有界示例、来源哈希及当前工作流的 reconsider 修订路径。
未登记的文件不会被读取，产物改变会撤回诊断，原字节恢复后复用同一任务。
这是生产者失败证据的读取，不是重新执行科学审计、受体准入或分子失活判断；
不改门槛、不添加自动重试。普通 status/resume 会复核已保存的失败诊断。

展开运行步骤时不再对整个区域设置 `aria-hidden`，使其中的工具结果和审批
可被无障碍接口访问。此源码修复不代表已替换 RC7 或已完成网页复验；实际发布
身份以源码外验收记录为准。

## 2026-11-04 VS 前受体修订连接（历史源码检查点，随后已部署 RC7）

RC6 原网页在受体审阅中正确保留 `requires_revision`，但当前动作没有受体
修订入口：已有受体准备依赖精修候选，不能解决 VS 前的输入问题。
这次连接将当前 VS 原始输入作为独立的 `sealed-ligand-vs-receptor-v1` 来源，
保存于 `vs_receptor`，不创建或伪造 `refinement_input`、合格候选或旧姿态任务。

当前阶段可依次使用 `prepare_receptor_input`、`check_receptor_runtime`，然后
独立原生批准 `start_receptor_preparation`。复用已有 CPU 准备、原任务恢复、
质量/环境测量重读和报告能力。任务完成不等于核验通过；必须成功重读登记产物
后才开放 `use_prepared_receptor_for_vs`。

返回 VS 时重新核验准备任务和不可变质量证据，仅把受体的作者编号映射为原有
实体位置，坐标保持不变；原始沉积结构投影保留用于后续参考系比对。新的 VS
输入绑定准备任务、质量报告、原始来源、映射实现和产物哈希。旧 VS 输入、审阅
及计划归档；新输入必须重新审阅环境，并另行批准 VS。不会直接开放 LO 或 MD。
未解析残基已建模只表示准备产物的事实，不自动证明建模区域、生物组装或位点
适用性。新的上下文证据仍通过绑定文件读取器逐项读取。

软件验证分别覆盖实际 2ITY 的准备输入与原生审批/恢复，以及隔离登记的历史
1M17 坐标夹具的质量重放、输入替换、坐标不变、来源篡改拒绝和 VS 重新审阅。
历史夹具对上游构建/候选策略的隔离仅用于软件测试，不修改生产科学门槛，
不能当作当前网页研究已经完成受体准备。固定发布与原网页复验仍待完成。

下方各段保留历史检查点含义。RC6 以及后续实际运行身份和网页证据，以源码外
`outputs/source-baseline-20261029/acceptance-current.md` 和对应运行回执为准。

## 2026-11-04 PDB 原文与四个审阅入口（新源码，待固定版复验）

RC5 网页已验证 JSON 字段读取，但出现了另一处可复现的证据缺口：
2ITY 已绑定的原始 `structure.pdb` 包含 REMARK 350 组装声明，JSON-only
读取器却没有开放该来源。模型还曾把部分分页当成全量、用序列位置推断
未解析区域的空间关系；该审阅由开发者拒绝并纠正，不计为自主验收通过。

共享读取器现在为已绑定 PDB 提供确定性的 `pdb-header-lines-v1` 视图：
`/header_records` 按记录名分组，REMARK 按编号分组，每行保留原文和原始
一基行号。读取与引用都核验原始文件哈希，坐标/连接记录不作为头部内容返回。
这只是来源声明，不自动验证生物组装、构象适用性或 MD 准入。
JSON 的读取、分页和引用边界保持；分页明确给出本页覆盖区间及完整子值数，
最后一页不代表全文已读完。源文件没有某记录也不等于相关生物事实不存在。

相同原生入口已接到 Ligand `read_vs_input` / `read_covdock_environment` 和
Library `read_vs_input` / `read_lo_input`，只允许当前绑定清单中的 JSON/PDB。
字段读取参数不得用于执行或规划动作，私有配置及清单外路径仍不可读取。
审阅指导明确禁止以序列编号代替空间测量；证据无法支持结论时保留未知。
这项软件修复不代表模型推理问题已通过网页复验，也不解除当前案例的科学门槛。

下方为历史检查点。RC5 实际部署身份及网页结果以源码外
`outputs/source-baseline-20261029/acceptance-current.md` 和运行回执为准；
本节新修复尚未取代 RC5。

## 2026-11-04 固定版网页发现的证据读取缺陷

RC4 已完成统一部署；下方各“未部署”段落保留其当时的源码检查点含义，
实际部署身份以源码外 RC4 identity.json 为准。真实 Ligand 网页已完成新 VS
输入绑定，但环境审阅被两个接口问题阻塞：历史姿态评估使响应过大而截断，
`read_vs_input` 仅给出远端文件路径和哈希，未提供可供判断的 JSON 内容。

修复使 VS 规划响应只返回历史任务摘要，完整历史仍保存在原研究中。
`read_vs_input` 默认列出当前研究已绑定的 JSON 证据，可使用 `evidence_path`、
`evidence_locator`（RFC 6901 指针）和 `evidence_offset` 在原生工具内读取。
每次重新核验当前来源和文件哈希；不允许读取清单外文件或私有执行计划。
小字段返回完整值；大节点返回真实子指针和分页信息，明确标记不完整，不能
把摘要冒充完整证据。环境判断、引用核验、科学门槛与执行批准保持独立。

本修复的软件测试覆盖实际沉积 2ITY 输入的字段读取、可直接用于审阅的引用、
大历史记录下的响应大小，以及分页、指针转义、越界、未绑定文件和哈希变化。
修复后的新固定发布及网页复验仍需完成，尚未批准或提交新 VS 计算。

## 2026-11-04 VS → LO 连接修复（源码，未部署）

Ligand 新增 `prepare_vs_refinement(candidate_ids, reason)`，只接受当前原生 VS
评估中同一姿态通过全部门槛的候选。交接冻结明确子集与理由、原始输入顺序、
VS 任务/评估/原始产物哈希；沿用原设计的反应前分子，供 LO 独立重新对接。
不读取旧 CarsiDock 姿态计划，不把 VS 分数写成 CarsiDock/AF3 分数，也不继承批准。

`prepare_vs_refinement_target` 接入原有靶点/构建审计与受体准备。后续仍使用
独立的受体准备、LO 环境审阅、计划和原生批准，以及原有 LO 评估与 MD 准入。
LO 计划显式包含候选来源证据。VS 的状态重读不能覆盖后续任务的展示或阻止
其重新核验；暂时变化的来源保留原交接和任务身份，恢复原始字节后可再次验证。

软件测试中的正几何测量为受控测试前提，真实原始结构身份检查与原生签名/登记
仍执行；未启动厂商对接、受体修复或 MD，不计作网页验收。
Library 的 VS → 精修连接仍待完成；当前源码也尚未进行统一发布和网页复验。

实现：`science/src/covscreen/covdock_vs.py` 及 preparation、runner、Ligand/Library 适配器。2026-11-03 已接入规划、环境审阅、原生签名登记/恢复、专用 CPU 调度和原始产物评估代码。固定验收候选源码中 `native_vs_workflow_ready` 已开启，仍需配置运行环境及独立原生批准；开关不代表已部署或网页验收通过。用户已明确：VS 筛选后保留 LO 精修及既有 MD 准入路径，不建设 VS 结果直接进入 MD 的接口。

## 当前实现

- `Inputs` 明确使用 `schrodinger-covdock-vs` / `enrichment`，不接受 LO 或任意 mode。首版仅支持带 905/906 反应原子映射的 Michael Addition 候选。
- `Source` 绑定 Ligand 或 Library 原生研究 ID、版本和源码外的研究输入清单哈希；不要求或伪造上游 CarsiDock 任务。
- `inspect_inputs` 读取原始 SDF 与实际交给厂商的 Maestro 输入，逐项核对候选顺序、名称、数量、电荷、立体化学和连接关系。拒绝重复候选、隐式 LigPrep 扩展、失去反应原子映射、非有限坐标和路径越界。
- 受体使用现有 `inspect_receptor` 校验准备后 Maestro 文件中的唯一游离 Cys SG。坐标身份通过不等于受体科学适用性通过。
- CPU、内存、候选数和墙钟上限显式记录；GPU 必须为 0；本地子任务数不得超过 CPU 与实际候选数。编译命令固定 `-mode enrichment`、`-RETRIES 0`、`-WAIT`，不包含自动回退。
- `freeze_execution` / `validate_execution` 绑定输入证据、命令、预算、安装路径、厂商启动程序、许可证文件及本模块依赖源码哈希。许可证内容不写入计划；包含许可证路径的计划只保留在私有宿主，不能直接作为模型工具输出。

契约本身不授予原生批准，不提交计算，不宣称科学门禁通过或 MD 就绪。工作流适配器仍须从当前研究状态生成并验证 `Source`，不能信任模型自报的研究 ID 或输入清单。

## 仍需接入

1. 完成原始产物评估与工作流恢复的整体回归，并在固定发布的真实网页任务上复验。软件测试不能代替厂商真实输出的原生执行验收。
2. 保留 CovDock-LO 精修及其后的现有 MD 准入检查；VS 几何通过不是 LO 精修通过或 MD 准入，不将直接 VS→MD 作为发布条件。
3. Library 原生筛选/VS 迁移与提交参数过大修复已进入源码，仍需在原研究上从网页验证，不能用三候选 Ligand 试验替代 Library 的筛选范围。
4. 将代码、输入/配置登记和宿主调度一起发布为固定版本，再从网页复验审批、执行、恢复、报告。已有软件回归不能代替这一步。

## VS → LO 来源与审批（2026-11-04，源码连接）

- Ligand 的 `prepare_vs_refinement` 绑定当前注册 VS 任务的合格候选与选择理由，再通过 `prepare_vs_refinement_target` 进入现有靶点、受体准备和 LO 规划。原始反应前分子、VS 原始结果和评估均有哈希绑定，不复用旧构象任务或审批。
- Library 的同名动作冻结 1–5 个明确选择且共同通过 VS 检查的候选，保留原始输入索引、预筛来源和受体编号。`read_lo_input` 提供来源清单，`review_lo_environment` 要求独立的精修环境判断；VS 适用性结论不能代替 LO 结论。
- `check_lo_runtime` 冻结私有 LO 计划：0 GPU、4 CPU、16 GiB、180 分钟，候选上限为实际选中数量。配置可用不等于厂商许可证已签出。`start_lo` 需要新的原生批准，宿主使用 Library 专用调度配置；预筛和 VS 的旧批准不会转移。
- `lo_status` 与 `reconcile` 恢复原登记任务；`evaluate_lo` / `report_lo` 复用注册 LO 结构读取及现有质量检查，保存独立的 Library 评估与报告。来源或报告改变时撤回结果，恢复原字节后重新核验；仍在执行或结果不确定时不能重新选候选绕过旧任务。
- 这部分只连接至 LO 结果审阅。Library 新 LO 结果接入既有 MD 选择、所需 AF3 证据和其他准入检查仍需完成；不得伪造旧筛选排名、AF3 成功或放宽 MD 门槛。统一部署和真实网页验收仍未完成。

## 2026-11-03 原生连接进展（源码，未部署）

- `ligand_vs_input.py` 从当前设计和最新证据读取、复核候选图与来源，使用已有受体映射/构建审计生成 VS 准备输入。不存在或不引用 CarsiDock 上游任务；旧拒绝记录保留。真实 2ITY 软件测试核对原始 C797 到投影 A:102、27 个未解析残基、50 个排除原子。投影不补齐缺失残基，不是受体适用性证明。
- `prepare_vs_input`、`read_vs_input`、`review_vs_environment`、`check_vs_runtime` 暴露为当前 Ligand 研究动作，无任意输入路径。审阅覆盖组装、排除成分、构建/位点、反应范围，引用必须定位到绑定 JSON 的真实字段；`suitable_for_exploratory_vs` 不接受精修用途的判断代替。判断不能证明引文蕴涵或生物学适用性。
- 修改判断使旧计划失效并保留历史；计划绑定审阅文件哈希。完整运行计划包含私有运行路径，仅以权限 0600 保存，模型可见结果只包含计划哈希、预算和状态。
- `ligand_vs_execution.py` 接入 `start_vs`、`vs_status`、`reconcile`：独立引擎名、精确原生签名、一次登记、响应丢失后查回原任务。任务状态与科学验收分开；原生策略关闭时不能执行。审批范围在工作流中只保存摘要，签名记录由任务注册表保存。
- 宿主为 VS 选择 `dispatch_ligand_vs.py`，显式绑定固定科研发布与数据根；调用 CPU HPC 调度，使用 0 GPU / 4 CPU / 16 GiB / 60 分钟、最多三个候选的 Ligand 试验预算。预约前先校验资源，创建响应不确定时保留回执并禁止重发；不调用旧 GPU smoke 调度。
- 软件测试只登记临时测试任务或使用受控调度回复，没有启动厂商程序或提交真实科研计算。这些测试不是网页验收证据。

## 原始结果评估进展（源码，未部署）

- `covdock_vs_output.py` 直接读取注册任务的原始 Maestro 复合物，不执行嵌入的厂商 Python 数据。核对候选名称、序号、原子来源、Michael 产物图、电荷、氢数、原有立体化学及受体重原子连接，保留每个姿态自己的受体坐标。
- `covdock_vs_geometry.py` 以受体 Cα 对齐沉积坐标，不拟合配体；测量母核偏移、保护相互作用距离、局部松弛能量差、共价键几何及排除成键邻居后的蛋白碰撞。阈值仍为暂定 QC，不能用于宣称活性、反应性或 MD 就绪。
- `evaluate_vs` / `report_vs` 重读注册输出及哈希，输出不可变评估与报告。没有产物的候选仍保留在分母；测量缺证据和实际几何失败分开。后续 `vs_status` 复核已保存评估，结果改变会撤回摘要和当前报告；不会另建任务。
- 原生签名任务的软件集成测试用合成结构验证评估、报告、缺失候选、改变产物后的拒绝及恢复。20 项输出/几何/集成测试在数值警告作为错误的模式下通过。该检查不提交真实计算，不构成网页验收。

## 本轮验证

原始结果接入后的合并回归 **128 项通过（121.55 秒）**，启用 `-W error::RuntimeWarning`，覆盖 VS 输入、准备、产物读取、暂定几何门禁、环境审阅、原生登记/恢复、HPC 边界和停用策略。日志：源码外 `outputs/source-baseline-20261029/vs-readback-integration-20261103.log`。该记录为软件验证；尚未部署、尚未网页复验。先前将直接 VS→MD 列为剩余工作的要求已按用户澄清撤销。

普通研究 `status/resume` 已接入同一 VS 复核逻辑：原始结果或任务身份变化时撤回当前评估，原证据恢复后重新验证，不登记新任务；无变化的重复读取不增加工作流版本。缓存评估的身份核验不再写出新副本。原生登记与状态恢复回归 **7 项通过（105.53 秒）**，严格数值警告模式，日志为同目录 `vs-navigation-integration-20261103.log`。

2026-11-03：VS 输入、环境审阅、原生审批/登记/恢复、专用调度、共用 HPC 边界与停用策略合并回归 **112 项通过**；宿主审批/调度/策略 **20 项通过**，后端类型检查通过。整组 Python 日志位于源码外 `outputs/source-baseline-20261029/vs-native-integration-20261103.log`，宿主日志为同目录 `vs-host-integration-20261103.log`。随后补充旧拒绝分支切换 VS 规划时的当前状态展示断言，相关输入工作流测试单独复跑。所有调度测试均无真实计算提交。

`tests/test_covdock_vs.py` 与 `tests/test_covdock_receptor.py` 共 53 项通过。覆盖模式/身份/文件变化/越界/预算/运行计划变化的拒绝和有效输入的编译。

在源码外，以 2026-11-02 CovDock-VS 独立试验的真实准备文件验证了 3 个候选身份和受体输入，证据保存到 `outputs/source-baseline-20261029/vs-real-input-contract-check.json`。该验证只读取既有文件，不是新的计算或网页验收。


## 观测坐标松弛 v2：源码实现与发布边界

v1 默认仍固定观测重原子，历史审计格式保持不变。v2 必须显式选择，使用独立策略、回执、质量与几何审计版本；前两阶段固定观测重原子，最终阶段允许其在非周期 `0.5*k*r²` 位置约束下移动。`k=1000 kJ/mol/nm²` 是待验证的开发参数，不是针对 2ITY 已验证的科学准入标准。

实现参考：[OpenMM CustomExternalForce](https://docs.openmm.org/latest/api-python/generated/openmm.openmm.CustomExternalForce.html)。

已连接的源码路径：

- `select_restrained_receptor_policy` 仅接受当前 pre-VS 研究经重新核验的失败受体任务，要求理由，保留原任务及审批记录，恢复原规划阶段；该动作不提交计算。
- 新计划绑定输入与完整策略摘要，使用不同于 v1 的请求身份；编译器和执行器分别核验策略，仍需独立原生批准。
- 编排调用 v2 身份与几何审计。观测来源原子标为 `relaxed_observed_atom`，保存原坐标及逐原子位移，拒绝原子丢失、重复、身份变化和非有限坐标。几何阈值没有为当前失败而放宽。
- 几何通过后仅进入 `observed_motion_review_pending`。研究评估独立重放登记产物，报告可保存、重开；`receptor_prepared`、CovDock 和 MD 准入保持否。
- 位移明细包含链、原始残基编号、残基名称、实体位置、原子名称、来源坐标和新坐标；这些测量不自动证明结合位点保留或生物学适用。

尚未完成：位移及结合位点保留的审阅与准入合同、v2 准入产物与下游输入绑定、固定版部署以及真实网页计算。旧准入入口仍拒绝有最终位置约束的回执。不得用软件测试或历史坐标构造的 v2 元数据声称新受体计算通过。

软件测试与实际验收记录保存在源码目录之外。源码中的测试可通过外部 fixture 路径读取历史记录，不将测试产物复制回代码树。

### Receptor observations and explicit refresh

`receptor_preparation_status` is a read-only projection of the registered task.
It validates job identity, frozen inputs and registered diagnostics without
saving the workflow, increasing its revision, submitting work, or publishing
review/report artifacts. Navigation `status`/`resume` in receptor preparation
stages uses the same projection. The response can show a newer job state than
the stored workflow; its revision remains the stored revision.

`refresh_receptor_preparation` explicitly persists that observation. An unchanged
observation is a no-op, including revision and event history. A changed observation
increments revision once. Call it before a state-changing navigation that requires
the terminal task state to be saved. Existing revisions are never rolled back.

Reading an existing evaluation validates its immutable witness and dependencies;
it does not regenerate missing evidence. A failed explicit evaluation remains
visible until another explicit evaluation succeeds. `evaluate_receptor` and
`report_receptor` retain their explicit scientific replay/publication roles.

### Bounded local observed-atom repair (`v2-local`)

`select_local_receptor_policy` is distinct from global `v2` relaxation. It derives
an exact whole-residue mask from registered observed-only source collisions in a
failed v1 task, validates those distances against the original deposited atoms,
and preserves that task in preparation history. It does not execute computation.
A different source, incomplete collision inventory, ambiguous parent-ligand
instance, changed atom provenance, or a mask containing the reactive Cys rejects
selection. Planning re-derives the policy from the retained evidence each time.

The local extension retains the existing forcefield and staged preparation.
All observed atoms remain fixed in the first two stages. In the final stage,
only observed atoms explicitly named in the mask gain mobility. Remaining
observed heavy atoms have zero mass and must retain their source coordinates.
Newly reconstructed atoms and hydrogens remain computationally modeled atoms.

Harmonic restraints use `0.5*k*distance^2`, with distances in nm and k in
kJ/mol/nm². Selected backbone atoms and atoms of observed residues within 5 Å of
the deposited parent ligand use k=10000 and maximum displacement 0.2 Å; other
selected sidechain atoms use k=1000 and maximum displacement 0.5 Å. The reactive
Cys is always outside the mask. The independent audit requires all other
observed atoms to remain within 1e-10 Å of the original coordinates (numerical
tolerance, no structural alignment). These are conservative, predeclared trial
limits, not validated experimental uncertainty estimates. The existing chemical
identity, stereochemistry and geometry thresholds remain unchanged.

The frozen plan includes exact atom identities, reference coordinates, policy
hash and predecessor evidence. It allows one candidate, one CPU, no GPU, at most
30 minutes, and requires a new exact native approval. A task finishing is not
acceptance: independent full-atom displacement, geometry and source-bound
site/environment review remain mandatory before any VS handoff. Failure does
not authorize changing the mask, weakening restraints, relaxing audit thresholds
or automatically retrying. VS and LO/MD approvals remain separate.

### Bounded local preparation after a failed harmonic-local trial

`select_bounded_local_receptor_policy` is an explicit pre-VS recovery action,
available only after a registered failed `v2-local` task. It validates the signed
predecessor, independently reproduces its movable-atom displacement failure,
revalidates the original retained v1 collision witness and preserves the exact
source, atom mask, fixed Cys, harmonic objective and displacement thresholds.
It archives task and selection history and compiles a distinct `v2-local-bounded`
request. No fallback, retry or scientific calculation occurs during selection.

Only the final stage changes to a projected Euclidean-ball optimizer. Its frozen
iteration/evaluation budgets and numerical convergence receipt are checked;
nonconvergence fails without retry. A numerical convergence result does not grant
geometry, receptor or environmental admission. A fresh exact native approval is
mandatory. The old `v2-local` approval and implementation remain unchanged.


### Free-atom block recovery

`select_block_local_receptor_policy` explicitly replans a signed failed
`v2-local-bounded` task whose registered receipt proves iteration-limit
nonconvergence. It preserves source, original observed coordinates, local mask,
fixed reactive site, motion and geometry limits. It revalidates the unique original
harmonic-local/source-collision evidence at selection and compilation, archives the
predecessor and selection history, and creates a distinct `v2-local-block` request.
Selection and compilation do not submit computation; prior native authorization
cannot authorize the changed scope.

The block policy allocates up to100 accepted iterations to a free-atom warmup within
one total1000-iteration/3000-evaluation budget. Bounded observed atoms are held during
warmup; the full solve restores their original displacement balls. Warmup numerical
failure stops without fallback; its planned iteration cap can transition with the
remaining budget. Only a full-system projected residual at or below10 can establish
numerical convergence. All identity, motion, geometry and environment gates remain
required before VS; block convergence is not receptor or MD admission.

This is a working-tree contract. Deployment status and an exact execution scope are
recorded outside source; no new receptor computation is authorized by this document.
