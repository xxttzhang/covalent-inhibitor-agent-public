---
name: covalent-denovo-md-analysis
description: Interpret existing De novo covalent-adduct repeated MD and its visual analysis, including trajectory identity, alignment, contacts, hydrogen bonds and sampling limits. Use for reports or planning a new MD protocol, not the Library-first MD executor.
category: chemistry
tags: [covalent, denovo, molecular-dynamics, analysis]
---

# De novo 重复 MD 与可视化分析

本路线已验收示例是 001/004/007 与阿法替尼四体系，各 3×10 ns，共 12 轨迹/120 ns；使用 OpenMM、OPLS_2005/TIP3P 共价加合物。Amber ff19SB/GAFF2 准备是另一项工作，不能标成该生产轨迹的力场。源报告和脚本见 [证据索引](../covalent-denovo-design/references/evidence.md)。

这些产物可分析，但当前 `molecular_dynamics` 原生工具绑定 Library-first 八候选，不适用于本路线新分子或追加采样。新 MD 需单独通过拓扑/参数、输入姿势、重复种子、资源与分析方案验收。

读数前核对拓扑—轨迹原子身份、DCD 完整性、时间/帧数、输入哈希、重复初速度和指标文件。区分预热、平衡与生产；按协议处理周期边界及坐标对齐，报告单位和选择集合，不能直接给一个无定义的 RMSD。

该历史协议：配体/预先固定环系 RMSD 用 15 Å 口袋蛋白 CA 拟合，不对配体自拟合；蛋白整体 RMSD 另用全蛋白 CA 拟合。接触保持为 4.5 Å 内初始残基接触，排除 C797 及跨共价连接三键内的原子对。不同配体初始接触集合不同，不用于亲和力横向排名。

展示完整未平滑 RMSD/接触时间曲线、每重复均值与重复间 SD、原有氢键占有率、分块诊断，以及温度/密度/势能。历史氢键只追踪预定义原子对，报告 3.5 Å/150° 和 3.0 Å/150° 两种阈值；不是全部新氢键或水桥。RMSF 可作为新分析扩展，但本批已有图表不能冒称包含未计算的 RMSF。

以独立重复为统计单位，不能将相关帧充当独立样本。首末 2 ns 块和重复间差异用于登记的追加采样提示；有效样本数依赖平稳性假设，常量观测不等于采样充分。图表保留失败/分歧重复，不挑有利片段。

四体系当前报告均触发追加采样提示；001 优先下一步验证、004 复核构象变化、007 解释重复分歧仅是研究安排。固定共价键保持不证明真实反应，配体位移不等于解离；10 ns 或更长检查点都不能自动证明收敛。报告结论、限制、源 CSV/JSON、图表及版本哈希，追加运行沿真实授权流程。
