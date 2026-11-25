# 架构与源码归属

本次改变目录与可移植性，不改变科学门槛、已批准任务、原候选身份或历史版本。

| 层 | 唯一职责 | 主要源码入口 |
|---|---|---|
| 交互 | 输入、会话、批准卡、研究结果和结构展示 | `openscience-custom/frontend/workspace/` |
| 智能体运行 | 上下文、模型调用、工具循环、停止与重试边界 | `openscience-custom/backend/cli/src/session/`、`agent/` |
| 研究状态 | 研究身份、假说、证据、revision、候选与分支 | 宿主 `integrations/discovery-platform/`，路线工作流与 `science/src/covscreen/` |
| 科研路线 | 阶段条件、允许动作和来源适配 | `routes/library-first/`、`routes/ligand-first/`、`routes/de-novo/` |
| 工具和执行 | MCP 合同、预算、批准、任务登记、派发与恢复 | `science/src/covscreen/mcp/`、公共执行模块、宿主 `integrations/inspire-screening/executor/` |
| 科学能力 | 生成、化学图/结构检查、对接、MD、分析 | 公共 `covscreen` 与路线专属实现，登记的外部科学引擎 |

调用关系：用户目标 → 会话运行时 → 读取研究与证据 → 模型选择动作 →
后端验证阶段/输入 → 编译精确计划 → 原生批准与预算检查 → 实际执行 →
独立读取产物 → 更新研究状态 → 模型解释结果、继续或停止。

研究状态、任务执行状态和证据评价是三种不同事实。
模型文字不授予计算权限，也不替代科学产物核验。
未知提交结果必须对账原任务；失败恢复不能复用旧批准生成另一作业。

## 本次保持的物理边界

- `covalent-library-first/` 改为 `science/`，Python import 名仍为 `covscreen`。
- 当前仍位于 Library 路线的通用 MD 引擎暂保留真源位置，不复制成第二份。
- 宿主内部 Bun workspaces、SDK 和前端组件关系保持；不为目录美观拆成新服务。
- 运行目录、代码目录、模型资产目录分别配置，不能自动搜索旧工作区补缺文件。
- De novo 按经验证的源版本纳入；完整 De novo 科学验收独立于本次两路线验收。
