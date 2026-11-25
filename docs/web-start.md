# 从新目录开始网页验收

> 历史部署记录，非当前安装或服务入口。当前 ligand-first 状态见 [发布状态](ligand-first-release-status.md)；以下路径已匿名化，不能直接执行。

新源码：`/path/to/workspace/covalent-agent-platform`

**2026-11-03 当前入口校正：现有验收服务为后端 4311 / 网页 4451，运行源码提交 `4661089`，科研发布 `f0394dea`。** 当前地址为 <http://127.0.0.1:4451>；下文 4447 等入口和 RC2 状态为此前排障记录，不作为当前版本依据。新的 VS 固定候选正在准备，尚未替换现有服务。后续保持原研究及任务历史，沿用用户授权的单后端交接；不并行启动共享可写研究状态的两个后端。VS 仅用于筛选评估，MD 保留 LO 精修后的准入路径。

智能体与执行入口的停用策略已补充到源码；发布状态及 CovDock-VS 接入剩余项见 [构象引擎迁移说明](docking-engine-migration.md)。新的代码保护尚未部署至现有网页运行版本。

**2026-11-02 用户决定：后续构象暂不使用 CarsiDock-Cov，优先使用 CovDock-VS。** 不得自动回退或通过旧研究重新提交 CarsiDock-Cov 计算；历史记录保留。源码与验收项目的 `AGENTS.md` 已记录该约定，但网页原生流程的引擎替换尚未完成，不能将此约定当作已经部署的技术禁用。CovDock-VS 三候选独立试验见 [试验报告](/path/to/workspace/outputs/covdock-vs-20261102/REPORT.md)；碰撞与连接核查通过，完整科学门禁及 MD 准入尚未验证。

当前官方通道候选入口：<http://127.0.0.1:4447>。4445 为首轮排障，4446 为 RC1；历史会话保留，不混用各版本的验收证据。

独立项目：`/path/to/workspace/covalent-agent-runtime/rc2/projects/acceptance`

源码阅读顺序：`docs/architecture.md` → 宿主 `src/session/` → 公共 `science/src/covscreen/mcp/` → `science/src/covscreen/ligand_workflow.py` → `routes/ligand-first/` → 宿主 `integrations/inspire-screening/executor/` → 前端 `frontend/workspace/`。

## 当前可以做什么

打开当前候选网页、选择 acceptance 项目。官方模型通信及公开网页工具调用闭环已通过；Ligand、Library 正在续接本次验收已创建的研究。先查看已有会话，避免重复创建或提交。

**目前尚未通过 Ligand→MD 全流程验收。** 新远端执行配置、独立科研发布及数据根已绑定；还须逐阶段验证真实执行、结果展示与恢复，并在最终固定候选版本上复验。当前宿主提交为 `41a36c8`，生产前端构建与科研发布身份见外部 `rc2-runtime-identity.json`；用户要求节省费用后，官方通道已改用 `deepseek-flash`（默认及辅助模型），无响应超时仍为 300 秒。详见 `migration-status.md`。

## 正式验收顺序

1. 先核对并记录 Git 提交、源码归档、实际运行构建、科研发布和配置身份。
2. 以 Ligand 为首条主流程，从网页创建全新研究；原生批准、查看状态、结果和恢复全部走网页。
3. 每次必须进入终端、补文件、改数据库或找开发者才能继续，记为上线待办，并保留截图和步骤。
4. 每个阶段分别记录通过、失败、未测和外部条件缺失；Ligand 完成后再验收 Library。

运行数据在同级 `covalent-agent-runtime/`，迁移测试证据在同级 `outputs/source-baseline-20261029/`。不要把这些目录复制进源码，也不要通过旧研究的完成记录判断新副本成功。

查看服务状态：

```sh
cd /path/to/workspace/covalent-agent-platform
python3 ops/source-baseline/web.py status --runtime ../covalent-agent-runtime/rc2
```

状态命令同时列出缺少的执行配置；HTTP 200 只证明服务连通。

Library 筛选另需在科研数据根准备 `configs/library_docking_references.json`，
使用 `library-docking-references-v1` 格式登记公开实验结构和同坐标配体。
每个 reference 必须包含绝对路径 `pdb`、`reference_ligand`，对应 SHA256
`pdb_sha256`、`reference_sha256`，以及 `component_id`、`component_chain`、
`component_residue_id`。路径应指向本次已获取或显式部署的参考资产，不能借用旧研究结果。
部署完成后，用科研宿主解释器执行只读预检（以下是 1M17 的明确映射，不是通用默认值）：

```sh
python ops/source-baseline/check_docking_references.py --data-root /absolute/acceptance/data \
  --model 1 --author-chain A --author-residue 773 --canonical-position 797
```

缺少登记、文件变更或坐标不匹配时，先修复部署再开始网页验收。该检查不创建工作流、
不批准计算，也不代表受体科学适用性。本轮曾遗漏此登记，导致筛选输入准备失败，
补配置的开发者介入必须保留在验收缺陷记录中。

## 本轮仍需处理的问题

运行数据目录位于上层 Git 仓库中时，网页“New project”曾错误采用上层项目身份，随后无法创建会话。修复已保存为 `cea3559`，并在独立入口 4448 从网页直接新建项目、创建会话和收到回复；另核对了项目根及会话权限边界。当前 4447 尚未切换该修复，独立复验不代表整条科研流程通过。

聊天研究状态展示修复通过独立入口 4449 的记录副本复验：路线工作台和统一研究页能够显示原工作流、最后阶段及对应会话链接。它只读取回执，不查询或提交远端计算；没有结果图表不能推断候选数为零。该展示入口未配置模型或科研连接器，不用于推进研究。统一研究接口另发现前后端不同端口被重复来源检查拒绝，修复使用宿主已有来源策略，外部来源和宿主权限检查保留；回归通过，固定 f6bf077 的独立网页 4450 已完成读取复验；当前科研入口仍待最终候选整合复验。

源码中已合入确认过的 De novo 恢复与预算限制修复；科研连接器仍固定在 `f0394dea`，远端激活须另行发布与验证。
