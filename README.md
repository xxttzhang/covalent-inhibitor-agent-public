# 共价药物发现智能体平台（公开源码快照）

这是从私有研发仓库已提交版本 `736b9fcec63e67e89221f7269fdb0691c70a1a49`
整理出的独立、代码优先公开副本。它使用全新的 Git 历史，不包含私有仓库的未提交修改、
运行状态、研究结果、凭据或历史提交。整理公开副本不会改变原仓库及现有验收环境。

本仓库当前是“可公开查看源码”，不是获得 OSI 开源许可的发行版。原创部分尚未授予复用许可；
第三方目录继续适用各自许可证。完整边界见 [公开分发说明](PUBLIC_DISTRIBUTION.md)
和 [第三方声明](THIRD_PARTY_NOTICES.md)。

本目录是从现有工作区整理的新工程，公共科研目录使用 `science/`。
Ligand-first 已于 2026-11-23 完成小批量至 CovDock-LO 报告的真实验收，并部署 `0.2.0-rc.1` 固定发布版。上线检查覆盖网页、真实科研工具连接、历史会话与 Results 保留，以及 Kimi K3 通信。具体源码身份和范围见发布状态；不代表全部路线完成验收。
现有会话、科研结果与认证保留在外部运行目录，旧冻结任务继续使用原科研发布。

- [当前发布状态与验收范围](docs/ligand-first-release-status.md)
- [架构与职责](docs/architecture.md)
- [复制边界与外部依赖](docs/source-boundary.md)
- [网页验收合同](docs/acceptance.md)
- [迁移状态与缺陷](docs/migration-status.md)

正式源码、必要测试和小型测试样例属于代码。科研结果、会话、任务账本、
测试日志、截图、权重、许可证及凭证属于外部状态。
源码来源与逐文件哈希由 `ops/source-baseline/snapshot.py` 记录。

Library-first 本轮内测的代码导航、实际完成范围与发布阻断项见 [Library-first 发布清单](routes/library-first/RELEASE.md)。

## 工程结构

| 目录 | 职责 |
|---|---|
| `openscience-custom/` | 原有 Bun 工作区：网页、Research 宿主、SDK、集成与测试 |
| `science/` | 公共 Python 包 `covscreen`、科学命令、schema 与供应商源码 |
| `routes/` | Library-first、Ligand-first、De novo 专属工作流与实现 |
| `ops/` | 部署、发布、源码清单及验证工具 |
| `configs/examples/` | 无密钥的配置模板；不保存活动发布或生产账户配置 |
| `evaluations/` | 用户验收场景与检查要求；执行证据保存到外部目录 |
| `docs/` | 当前架构、接口职责、开发部署及验收说明 |

开发命令见 [开发说明](docs/development.md)，部署配置见 [外部配置说明](configs/examples/README.md)。实际通过范围以 [Ligand-first 发布状态](docs/ligand-first-release-status.md) 为准。运行地址、进程与版本记录保存在部署方的外部运行目录；历史文档中的端口不作为当前入口。
