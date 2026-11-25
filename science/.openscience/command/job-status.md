---
description: Show registered covalent job state, logs, artifacts, and candidates
agent: library-first-operator
---

Use the `covalent-job-execution` skill. Treat `$ARGUMENTS` as a `run_id`. Call
`covalent_get_job`, then show its state, route, engine, attempt, parent run, approved budget,
runtime, failure reason, and artifact paths. If the job is running or failed, inspect both log
streams with `covalent_get_job_log`. If a candidate table is registered, call
`covalent_get_candidates` and summarize success and failure rows without changing the denominator.
