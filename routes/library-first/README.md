# Library-first 代码与上线入口

当前内测范围、代码导航、阻断项和发布顺序统一见 [发布清单](RELEASE.md)。默认目标为 CarsiDock/RTMScore → VS → LO → 报告，AF3可选，本阶段不含MD。**目前尚未完成内测上线。**

维护真源为本工程的 `routes/library-first/` 和公共 `science/src/covscreen/`；工作区外层旧同名目录不是本轮维护目标。保留现有模块位置及兼容入口，不复制一套新业务实现。

下文为既有目录迁移与历史接口记录；其中MD相关验收不代表当前Library筛选主链已通过。

---

# Library-first：正式源码与实验脚本

本目录是 Library-first 专属代码的维护真源，已完成物理归位。`scripts/select_library_first_pareto.py` 仅为路线命令入口；同时服务 Ligand-first 的实现位于公共包 `../../science/src/covscreen/pareto_cli.py`。公共能力仍由 `../../science/src/covscreen/` 提供；平台接口仍由 `../../openscience-custom/` 提供。

当前 MCP、正式主筛入口、各阶段输入输出与未实现能力见 [接口合同与维护边界](CONTRACT.md)。维护接口时先看此文档，再修改对应真源。

2026-10-29：显式历史来源入口已上线。空研究核定 target/site 后可用 `attach_historical_md_source`、`read_historical_md_source`、`prepare_historical_md_candidate` 接入经过冻结核验的 Library 原生来源，再独立规划图准入。入口只复用输入，不能继承旧作业、许可或 MD 完成。017 已完成新原生图检查与真实软件失败恢复验收，12 个产物核验通过；参数化和 MD 尚未执行。接入证据及验收边界见 [当前记录](../../reports/library-native-source-20261029/STATUS.md)。

| 目录 | 责任 |
| --- | --- |
| `src/covscreen_library_first/` | 库预筛、路线编译、输入解析和候选召回 |
| `scripts/` | 正式命令入口；支持从任意工作目录启动，输入输出请显式指定 |
| `configs/`、`covindb-configs/` | 路线与靶点配置真源 |
| `tests/` | 路线测试 |
| 日期化历史脚本 | 具体位置按历史回执查找，不作为本轮默认执行入口 |
| `science/` | 兼容导航，编辑前确认实际目标；不是独立维护真源 |

旧 `covscreen.library_chain` 等导入转发到本目录的真实模块；旧 scripts 命令为兼容启动器；旧配置和日期化 ops 路径为兼容链接。只编辑本目录的实现文件，不在旧入口新增业务逻辑。冻结远端版本不随本地源码变化。

从本工程根目录执行路线测试（使用已配置依赖的Python环境）：

```sh
python -m pytest routes/library-first/tests
```

完整发布操作见 [发布清单](RELEASE.md)；构建配置必须显式提供，测试通过不代表远端部署或科学验收完成。

小型正式预筛入口为 `scripts/run_covindb_prescreen.py`，要求显式 `--input`、`--target-config`、`--output-dir`。不要因入口可执行就继承旧任务的批准范围或自动覆盖输出。

移交与剩余事项见 `../../reports/layout-migration-20261012/README.md`；逐文件记录见同目录的 `physical-moves.json`。
