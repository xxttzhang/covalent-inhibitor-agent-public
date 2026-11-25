---
name: covalent-denovo-generation
description: Use the actual denovo_generate contract and interpret PocketXMol fixed-acrylamide raw and relaxed QC, provenance, budget and failure states. Applies only to the registered EGFR WT C797 profile.
category: chemistry
tags: [covalent, denovo, pocketxmol, qc]
---

# De novo 生成与结构验收

先核对 [工具参数与请求边界](../covalent-denovo-design/references/tool-contracts.md)。自然语言设计请求进入 study 规划；先恢复或建立研究，再通过 `workflow`、科学推理及假说同步、`plan_study` 得到冻结计划。`create` 不执行生成。实际用户确认“执行 de-novo 研究 study-<id>”后，调用 `{"action":"create_study","study_id":"实际研究ID"}`，原生审批绑定该冻结计划。模型不能编造用户指令、批准标志或修改冻结的 slots、seed、target、pocket。

PocketXMol maskfill 固定丙烯酰胺五个重原子，只从 N 生长；100 步、batch 2、一次采样。模型源码/权重/训练配置和执行包均固定哈希。最多 1 张 H100、15 分钟，实时单价上限 1 点券/小时，精确计划由现有原生算力审批执行；不能把 skill 当作额外授权或绕过原生审批。

验收逐请求槽位记录：化学合法性、连接位点、warhead 图和固定坐标、蛋白 clash、反应前几何、PoseBusters、SDF/采样日志身份。原始和约束优化后分别统计，分母均为请求槽位；优化是带约束 UFF/蛋白排斥，不是亲和力优化。上游 bad 重建或身份不一致不能被优化“救回”。本 profile 的几何判据、版本和非普适性见工具合同。

输出原始/优化后通过 SDF、逐分子 CSV、结构联系图、汇总 JSON 和报告；原始日志和失败槽位保留在任务目录。不要把两组通过数相加，也不要用优化结果替换原始主要终点。

状态查询 `{"action":"status","task_id":"denovo-<24位小写十六进制ID>"}`；使用实际返回 ID。拒绝审批不提交；提交响应不确定只核对预留作业名，失败不自动重提或补样。恢复沿原任务；本机休眠/关机影响控制与交付，当前没有重启自动恢复守护服务。`COMPLETED` 还需核对任务 summary 和实际 Results，不能拿旧任务文件填充。

结果是反应前结构提案；位点和 warhead 朝向由参考结构辅助固定，不是独立预测共价位点的成功证据。
