---
description: Plan or execute an approved Library-first registered job
agent: library-first-operator
---

Use `library-first-benchmark` when the request is BTK-mini and always use
`covalent-job-execution`. Validate the project and call `covalent_platform_health`. Compile the
requested CovalentInDB prescreen, RDKit, CarsiDock-Cov, or RTMScore job with `execution_mode: plan` first. Display exact
inputs, registered command, outputs, candidate count, runtime, CPU, memory, GPU budget, stop
conditions, and scientific limitations. Execute only when `$ARGUMENTS` contains explicit user
approval for that exact plan; then submit a new request with `execution_mode: execute`,
`human_approved: true`, and a meaningful approval note. Poll the job and return logs, the
identity-mapped candidate table, failures, artifacts, and checksums.
