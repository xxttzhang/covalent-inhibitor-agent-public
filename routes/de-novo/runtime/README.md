# De novo 原生研究与运行边界

2026-10-29：以下独立评价适配和停止恢复修复已联合发布；新研究尚未完成真实生成、对接或 MD 验收。

固定登记方案为 EGFR WT / PDB 4G5J 链 A / C797 / 丙烯酰胺。先在 `discovery_study` 建立研究，保存该研究的位点—战头假说及来源证据，声明样本槽位和随机种子，再用 `denovo_generate.plan_study` 冻结生成计划。`create_study` 可为当前会话绑定的独立 De novo 研究发起精确计划的原生算力审批，无需用户再次输入执行口令；系统续跑通知不构成批准，原始用户消息仍须保留，只有原生批准回执才能启动计算。其他研究保留明确执行口令入口。旧 `create` 入口现在返回 `NEEDS_SCIENTIFIC_DECISION`，不会从一句自由文本直接启动生成。

生成最多 96 槽位、1 张 H100、15 分钟、单次采样，不自动补样。固定丙烯酰胺五个重原子，仅从 N 生长。源码、权重、输入、完整请求及原生批准均按任务保存。逐槽位记录原始与约束优化后结构，检查图、来源、碰撞、反应前几何和 PoseBusters；两个表示分别统计，不能相加。上游重建或身份失败不能靠优化消除。结构通过只表示生成阶段结构检查通过，不证明抑制活性、反应性或选择性。

## 独立评价的新接口

1. `prepare_evaluation`：在同一研究选择一个已完成原生生成的明确 `candidate_id`。已登记验证器重新核验生成来源与分子，再冻结原始证据、去除生成坐标的 2D/SMILES 输入，以及由原始 CCD/PDB 重建的 afatinib 对照。旧的未绑定研究候选不能冒充新研究输出。
2. `register_evaluation`：只将这份不可变数据包传到当前共享数据根的 `state/de-novo-inputs/<source_id>`。固定主机桥确定远端根，接收器逐文件核对哈希、拒绝链接与路径逃逸，已有目标只允许逐字节相同。该动作不运行对接、生成或 MD。
3. `plan_evaluation`：调用共享 `covalent_de_novo_review.plan`，编译“1 个候选 + 1 个 afatinib 对照”的既有 CovDock 计划，限定 1 核 CPU、8 GB、30 分钟、0 GPU。源码、环境、受体和精确候选表冻结后才返回 `covalent_submit_job` 参数。真实启动仍需要该新计划单独获得原生审批。
4. `evaluation_status` / `evaluate`：只观察同一不可变请求的既有作业。评价重新打开注册的厂商结构、核对原生批准和产物，并重放冻结物理门槛。失败、未知与缺少证据不会变成通过；结果回写原研究的不可变观察文件。`workflow` 可读回最后观察，状态回读不会伪称重新做过科学评价。
5. `plan_prereaction` / `prereaction_status` / `evaluate_prereaction`：为同一来源冻结独立 Vina 计划，单独获取原生审批；从二维身份重新生成起始构象，按预先固定的评分规则选姿，并从注册的原始输出重新测量候选及对照，不复用生成坐标。
6. `prepare_md_handoff`：当前返回 `MD_HANDOFF_BLOCKED`。受体对齐的对照 RMSD、生成候选的完整配体反应图与立体化学检查已接入本地 `evaluate`，结果分别持久化；实现与限制见 `reports/denovo-readiness-20261028/CONTROL_REVIEW.md` 与 `CHEMICAL_REVIEW.md`。尚未取得本次新研究的真实通过结果。独立反应前姿势评价已有单独计划、观察与原始输出重测入口；任何已通过的单项不能清除剩余要求，也不会伪造 shared MD candidate delivery。

`evaluation` 只读本地冻结来源；远端状态通过 `evaluation_status` 显式同步。新接口没有新的作业控制器、审批密钥、自动重试或调用者提供的 PASS 开关。

## 原始任务与恢复

本地任务位于工作区 `.cache/denovo/tasks/denovo-…/`；Results 发布到研究项目 `denovo-results/denovo-…/`。旧任务继续使用其原批准包，不能原地替换 worker。后台进程独立于对话轮次，OS 文件锁避免重复执行；自动恢复守护服务尚未实现。

本地停止恢复修复保留原始提交时间，恢复不延长原观察期限。停止请求先保存相同任务名和请求哈希，再观察同一调度作业确认终态。接口成功但作业仍活跃，或响应未知时显示 `NEEDS_REVIEW`，不提前标记取消。只有 `PENDING`、`QUEUING` 显示排队；未知状态保留原值，不自动停止或重提。完成与停止竞争时继续实际产物核验。

新功能及 worker 修复必须一起纳入新内容寻址发布。原生 De novo pointer 和共享 MCP 科学 release 必须都包含对应适配器，主机 TypeScript 入口也须更新。发布和新研究建立已验证，真实候选及独立计算尚未验收；当前版本、证据交付和限制见 `reports/denovo-closed-loop-20261029/STATUS.md`。

独立评价提交由研究绑定的 `denovo_submit_evaluation` 入口处理。模型只提供 `stage=prereaction|adduct`；主机复核当前研究和输入、读取冻结计划，并调用同一个受原生审批保护的共享执行器。通用 raw submit 工具不暴露给 De novo 会话。通信超时必须先核对已有请求或远端计划，不代表作业失败，也不触发自动重新提交。
