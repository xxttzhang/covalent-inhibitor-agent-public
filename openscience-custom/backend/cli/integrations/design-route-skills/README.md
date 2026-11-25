# Ligand-first / De novo OpenScience 技能包

2026-10-09 按本地 OpenScience 的 SKILL.md frontmatter、按需参考资料、原生 `/skill` 安装与 SkillTool 加载约定整理。源码及真实接口优先于旧说明；此包维护工具使用知识，不提交科学计算。

| 路线 | 入口 | 阶段技能 |
|---|---|---|
| Ligand-first | `ligand-first-covalentization` | `ligand-first-enumeration`、`ligand-first-refinement`、`ligand-first-evaluation` |
| De novo | `covalent-denovo-design` | `covalent-denovo-generation`、`covalent-denovo-refinement`、`covalent-denovo-md-analysis` |

每项覆盖适用时机、真实输入/输出、来源与编号、筛选/排序、错误停止和结果解释。两个入口的 references/ 维护接口合同与历史证据索引。其他路线已有 Library-first 五项技能，三路线共计十三项专用技能。

关键边界：Ligand-first 仓库有父链及阶段脚本，当前项目尚未暴露其原生聊天执行接口；De novo 原生生成只接受登记的 EGFR WT C797/丙烯酰胺方案，后续 CovDock/MD 为已有特定项目流程。技能就绪不等于任意靶点闭环就绪。

Ligand-first 分清 `rdkit-branch` 与 `upstream-native`；后者完整母体保留，原生硫类为 vinyl sulfonamide。De novo 原始/优化后验收分开，不把历史对接/MD 结果当成新任务结果。

从 backend/cli 运行：

```sh
bun integrations/design-route-skills/validate.ts
bun integrations/design-route-skills/install.ts
bun integrations/design-route-skills/verify-loading.ts
```

Ligand schema 验证（从工作区根运行；轻量隔离环境只含 Pydantic/PyYAML，不提供完整药物设计运行依赖）：

```sh
.tooling/design-route-validation/bin/python openscience-custom/backend/cli/integrations/design-route-skills/validate-ligand.py
```

安装只创建/更新此包八个 user-skills 并复制 references，调用平台原生失效刷新，不重启服务、不修改计算执行器。验证直接使用 OpenScience frontmatter/schema/review、真实 DeNovo 参数和自然语言解析器；本地 Ligand 配置样例用实际 Pydantic schema 验证。安装后经真实 SkillTool 逐项加载，逐文件比对内容，不冒称进行了 LLM 行为评估或远端科学运行。

交付与验收记录：工作区 `reports/design-route-skills-20261009/DELIVERY.md`。
