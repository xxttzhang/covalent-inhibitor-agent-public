# 复制边界与外部依赖

## 保留清单

1. 宿主 backend、frontend、tooling、assets、workspace 配置和 Bun 锁文件。
2. 不依赖未审计科研数据的自动化测试与评测程序。
3. 公共 `src/covscreen`、正式脚本、schemas、环境声明和测试。
4. InterMol、PDBFixer 中已登记的供应商源码和许可证。
5. 三条路线的 src、scripts、tests、配置、登记方案及 De novo runtime/evaluation。
6. 正式调用仍支持的 `historical-scripts` 暂保留，后续逐个审查；目录名不能作为删除依据。
7. 发布/构建/启动工具与正式入口依赖的 ops 文件。

## 排除清单

- 原工作区 reports、results、outputs、output、jobs、state、pilot、reproduction、旧 releases。
- 官方对照源码、历史 OpenScience 副本、演示文稿和实验图片。
- node_modules、.venv、.git、缓存、构建产物、浏览器截图与测试输出。
- 生产 openscience.json、provider 配置、批准密钥、SSH 凭证、许可证。
- 无再分发许可的 `drug-idea-generator` 源码。
- 项目科研 fixtures、结构文件、候选索引及 De novo 靶点资产；运行时由使用者从合法来源外部提供。
- 活动发布指针不成为新工程的默认执行配置；仅作为迁移证据另存。
- 日期化历史运维目录默认排除；若发现正式调用依赖，先明确归属再补入。
- 只有目标也在清单内的内部文件链接才保留，并改为新工程内相对路径。
  其他链接需要逐项审查；不允许新工程通过链接读取旧工程。

## 外部依赖

| 依赖 | 处置 |
|---|---|
| Bun 及 npm 包 | 使用工程锁文件独立安装；记录实际版本 |
| Python 和科学包 | 独立环境安装，验证版本兼容；不能依靠旧 editable install |
| CarsiDock、RTMScore、PocketXMol 等权重/引擎 | 外部资产登记和内容校验；不混入源码 |
| Schrödinger 等软件及许可证 | 外部登记环境；不复制许可证 |
| LLM、Inspire 与批准凭证 | 使用受控的运行配置，不写入代码或报告 |
| 化合物库、受体、候选和既有研究 | 独立数据目录；历史输入必须通过正式来源接口引入 |
| 网页验收日志/截图/产物 | 写到新工程之外的验收目录 |

## 复制与变更策略

逐文件记录原相对路径、目标路径、原始 SHA256 和复制后的 SHA256。
复制前后再次读取源文件；变化则拒绝把该次副本声明为稳定基线。
目录改名和路径适配另有变更记录，不能把修改后的文件冒称与旧发布逐字相同。
源码清单稳定只是可追溯性的证据，不代表构建或科研验收通过。

初步调查发现旧 `ops/release/components.json` 未完整纳入科学 schemas、
InterMol/PDBFixer 和所有测试样例，同时有历史报告路径依赖；本次扩充清单并单列依赖审计。
