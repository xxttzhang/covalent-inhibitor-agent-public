# 平台按路线代码入口

_2026-10-12 · 本地浏览与维护入口，不是新部署版本_

---

## 📋 从哪里开始

| 目录 | 内容 |
| --- | --- |
| [library-first](library-first/README.md) | 化合物库筛选、路线编译、筛选评价及该路线 MD 脚本 |
| [ligand-first](ligand-first/README.md) | 母体输入、共价化设计、候选评价及该路线 MD 脚本 |
| [de-novo](de-novo/README.md) | 生成模型适配、从头生成与历史验证脚本 |
| [upstream](upstream/README.md) | 科学推理、靶点假设与路线交接 |
| [shared](shared/README.md) | 公共对接引擎、MD、审批、网页及执行器 |

Library-first 的 `src/`、`scripts/`、`configs/`、`covindb-configs/`、`tests/` 和 `historical-ops/` 已成为真实文件目录。Ligand-first 的专属源码、脚本、配置、测试和历史操作也已完成物理归位，远端新任务代码入口已切换并验证。De novo 专属实现也已物理迁入 routes/de-novo；upstream 及 shared 仍主要是相对符号链接导航。编辑链接会修改对应真源。

物理迁移记录在 `../reports/layout-migration-20261012/physical-moves.json`；本目录的 `catalog.json` 只登记仍保留的导航链接。不要把整个 routes 当作独立可运行包。

## 📚 子目录约定

| 子目录 | 用途 |
| --- | --- |
| `science/` | 路线专用 Python 模块 |
| `scripts/` | 科研命令脚本；包含不同年代版本，不表示全部为当前生产入口 |
| `tests/` | 相关单元测试 |
| `historical-ops/` | 原顶层 ops 的日期化实验、审批、修复及部署脚本；禁止批量执行 |
| `science-ops/` | 科研目录内维护脚本 |
| `shared/` | 公共模块导航；三个路线不各复制一套引擎 |

[catalog.json](catalog.json) 记录每个入口的真实路径及整理时文件哈希。文件名分类用于导航，不代表完整依赖分析；无法明确归入路线的文件保留在 shared，避免误称为路线专属。

## 执行与数据边界

Library-first 正式命令在新目录执行，通过 `_bootstrap.py` 选择同一源码版本中的公共包。Ligand-first 正式命令也已使用同样的发布内公共包定位规则，其生产代码入口已经切换；De novo 从不可变发布选择其专属入口，旧路径仅保留兼容。日期化脚本保留自己的实验输入合同，不属于正式筛选默认入口。

新旧部署、运行和历史结果状态见 `../reports/layout-migration-20261012/README.md`。Ligand-first 第二阶段证据见 `../reports/ligand-layout-migration-20261012/README.md`。De novo 迁移证据见 `../reports/denovo-layout-migration-20261012/README.md`。历史数据、冻结任务和软件环境仍原位保留。

## 🔧 维护与验证

在工作区根目录执行：

```sh
python3 ops/code-layout/organize.py
python3 ops/code-layout/organize.py --apply
python3 ops/code-layout/organize.py --check
python3 -m unittest discover -s ops/code-layout -p 'test_*.py'
```

默认只预览；`--apply` 只创建缺少的链接并刷新清单，遇到已有不同文件或链接就拒绝覆盖。`--check` 不运行科研计算。新增跨路线模块归入 shared；需要调整归属时审查整理脚本中的规则。正式发布仍见 [发布说明](../ops/release/README.md)。
