# Inspire GPU 筛选接入

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

> 2026-10-09 文档状态更新：当前 Library-first 工具与 skill 的统一入口是 [Library-first 技能包](../library-first-skills/README.md)。EGFR test100 核心筛选、所选八候选 AF3/CovDock 及固定八体系 MD/分析均已有实际批次验收。下文早期“未完成/待验”叙述保留为历史背景，不能代替当前任务状态；新靶点和任意新候选的全链自动化仍未验收。技能包中的阶段说明及证据索引为当前使用依据。

## 当前默认入口：持久化 Library-first 执行器

`inspire_screening_task` 已注册，代码与操作边界见
[执行器说明](executor/README.md)。
`create` 冻结源库/引擎、候选范围、账号和资源计划并执行 dry-run；`approve` 经原生精确审批后才启动持久化四阶段任务。
`select_refinement` 仅对本任务已完成的结果形成小量候选提案，不启动 AF3/CovDock。
目前仍未完成真实核心筛选与精筛验收，不能称为完整自动科研闭环。
最新部署与验证进度见 `/path/to/workspace/reports/inspire-executor-progress-20260911.md`。
`register.ts` 指向该执行器；旧 `register-v2.ts` 仅用于旧规划版本，不应覆盖当前注册。

## 保留的只读规划入口（v2，以下是该入口的能力边界）

用户已确认候选来源为 **CovInDB 虚拟筛选库**，路线为：确定靶点与候选库 → 准备与质量检查 → CarsiDock 主筛 → RTMScore 重评分 → 按不确定性选少量候选进入 AF3 / CovDock 精筛 → 汇总复核。

- 只读工具 `inspire_library_workflow` 提供 `describe`、`inspect`、`draft`。`inspect` 通过 InspireSkill 读取真实远端资产；`draft` 生成绑定当前对话的本地 **不可执行草案**，如实列出未确定范围、预算与执行适配缺口。它不是新的持久化执行器。
- 不再默认截取历史 130 个分子；不把已知抑制剂证据表当作虚拟筛选候选库。
- 正式规模、候选晋级上限和 CPU/GPU 预算未冻结；源库目前只检查存在性/表头/文件大小，没有声称完成全库解析或 SHA256 冻结。
- 检出的旧项目路线是 Ligand-first，现有 Library-first Campaign 引用了历史 pilot。新的正式任务必须独立建档、核验结构/位点，不覆盖旧项目路线、不自动继承 seed run。
- 全链逐阶段原生审批提交适配 **尚未完成验证**；AF3 不确定性选择阈值、显式候选名单与理由需从本轮重评分证据产生。现阶段不能把规划工具描述为完整筛选执行器。
- 历史测试工具改名为 `inspire_screening_smoke`，要求 `purpose=historical_pilot_connection_test`，只用于用户另行明确要求的旧 pilot 连接测试。原生资源审批继续保留。

平台可输入：“按已经确认的 Library-first 流程，检查 EGFR C797 与 CovInDB 虚拟筛选库，展示各阶段及缺失条件，不复用旧 pilot，不提交计算。”

当前入口代码：`plugin-v2.ts`、`workflow.ts`、`workflow_inspect.py`；注册脚本 `register-v2.ts` 在没有活跃研究会话时替换旧插件，不重启平台。配置位于 `/path/to/workspace/configs/inspire-library-workflow.json`。真实只读验收见 `/path/to/workspace/reports/inspire-library-workflow-acceptance.json`。

## 历史 pilot 连接测试说明（不是正式策略）

通过用户指定的 [realZillionX/InspireSkill](https://github.com/realZillionX/InspireSkill) 和已安装的 `inspire` CLI 操作启智平台；不走 Modal，不使用自建的未公开平台 API。平台原有 sandbox 和远程计算审批保持启用。

## 在平台中使用

打开现有科研平台，在新对话中输入：

> 仅做历史 pilot 连接测试：使用 InspireSkill，为旧 EGFR C797 已准备候选生成 3 个分子、10 分钟的 GPU 测试计划。不要当作正式筛选，提交前让我审批。

生成计划只做预检、实时资源查询及 CLI `job create --dry-run`，不申请 GPU。随后提交动作会显示原生审批卡，列明项目、资源组、GPU 配额、候选数、时限、点券估算和计划摘要。只有批准该具体计划后才准备独立输入并提交。聊天中的“同意”或模型填写的布尔值不能替代原生审批。

当前注册范围是 **EGFR C797 的既有 CarsiDock-Cov pilot**。接入验收时有 130 个已准备候选，每次运行重新核验；这不是数据库中全部已知抑制剂，也不是全库筛选。子集按已准备候选表顺序选取，保留原始分子 ID，不声称重新排序得到最优候选。其他靶点、全库预处理、AF3 和下游评分不在本次接入范围。

## 工具及边界

原生工具名（v2）：`inspire_screening_smoke`，必须显式标记历史 pilot 连接测试用途。旧名称 `inspire_screening` 已从平台移除。

| 动作 | 行为 |
| --- | --- |
| `targets` / `preflight` | 查询已注册范围和资源，校验远端文件、分子身份、质量门槛与环境；不申请 GPU。 |
| `plan` | 必须指定 `candidates`、`minutes`；冻结文件哈希，验证资源和镜像，生成本地计划。 |
| `start` | 使用 `plan_id` 触发原生审批；批准后准备独立输入并提交一次 GPU 作业。 |
| `status` / `logs` / `artifacts` | 使用原对话的 `plan_id` 获取实际作业状态、有界日志和结果摘要。 |
| `cancel` | 对确切作业再次申请审批，批准后停止作业。 |

- 计划绑定对话和项目目录，有效期 30 分钟。文件、资源设置或价格改变后须重新生成计划并审批。
- 单节点、单 GPU；候选数不能超过实际已准备数量或配置上限。达到批准时限终止，不自动重试、不扩展全库。
- 点券估算来自实时配额费率乘以最长运行时间，不是美元报价，也不是账户级消费硬上限或最终账单保证。
- 无 GPU、输入哈希不一致或质量检查失败时终止，不退回 CPU 假装完成 GPU 筛选。
- 提交使用本地独占锁。若网络异常导致提交结果不确定，先按原作业名查询，禁止自动重发或新建作业。
- 提交成功不等于对接成功；结果区分 `SUCCEEDED`、`PARTIAL`、`FAILED` 并返回实际成功数量与产物路径。

## 代码与配置

- `plugin.ts`：工具注册与模型使用约束；`core.ts`：CLI 调用、计划、审批和作业生命周期。
- `worker.py`：CPU 预检、隔离候选准备、GPU 执行及结果校验。
- 本地配置：`/path/to/workspace/configs/inspire-screening.json`，不保存 API Key。
- 本地计划：`/path/to/workspace/.cache/inspire-screening/`。
- 远端独立工作根：`/inspire/hdd/global_user/<account>/2_agent/openscience_inspire_screening_v1/`。
- 每个批准作业写入该根目录的 `runs/<plan_id>/`，保留计划、候选、`engine.log` 和结果；不覆盖数据库或原科研运行目录。
- `register.ts`：幂等注册全局插件和 Inspire skill 路径。遇到活跃对话会暂停注册；不重启平台、不修改模型凭据。

接入已注册到现有平台配置。继续使用现有桌面启动入口即可，无须每次手工运行接入脚本。

## 验收记录与未验证部分

2026-09-10 已通过远端真实预检、3 个候选 / 10 分钟的真实 CLI dry-run，以及原生审批前停止的检查；没有提交 GPU 作业，也没有调用外部模型 API。记录：`/path/to/workspace/reports/inspire-screening-acceptance.json`。

自动测试覆盖参数、计划完整性、会话隔离、时限、CLI 限额参数和审批策略，前端类型检查通过。**实际候选准备与 GPU 对接仍需在用户批准具体小规模计划后验收，不能据 dry-run 宣称已跑通 GPU。**
