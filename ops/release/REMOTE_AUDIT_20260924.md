# 远端关键文件核对（2026-09-24）

使用 Inspire CLI 只读检查 `asgbie3`（CPU资源空间），状态 RUNNING。
范围仅为 `2_agent/ligand-parser-stage-rC9rUmAc/` 下五个关键文件，不声称全量远端目录一致。

| 远端相对路径 | SHA256 | 本地对应 |
| --- | --- | --- |
| `scripts/run_ligprep_job.py` | `4187324ec954b2d53fc5c2772b481d2fd1a70cd704fddcff3b317862edcbd096` | 科研目录同路径 |
| `scripts/run_covdock_job.py` | `542ff4991286944f2cf26e36be3a5896aa975dcfff089741ee59cf7000265e14` | 科研目录 `ops/run_covdock_job_isolated.py` |
| `scripts/run_covdock_lo_registered.py` | `f9c059eec4ac9786f173d5bcc1c7f34fa61413be5fe8e0c99a2d6ea8c941316d` | 科研目录 `ops/run_covdock_lo_registered_isolated.py` |
| `src/covscreen/tool_api.py` | `052d744f99ad8fb2bb5f5ebadee64594282c89e913a23237ad40c99413637fd0` | 科研目录同路径 |
| `src/covscreen/external_hpc.py` | `5d25f0218ac52fdbb346498d4548d1040baadafbd6a69495ed099ac2bdff1782` | 科研目录同路径 |

五个映射均匹配。两个 CovDock 脚本不等同于本地 `scripts/` 中普通版本，因此发布登记表显式保留隔离版映射；本轮未修改任何远端文件或运行进程。
