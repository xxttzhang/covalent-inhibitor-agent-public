---
description: Inspect or advance the formal CovalentInDB Library-first parent-linked campaign
agent: library-first-agent
---

Use `$ARGUMENTS` as a project-relative campaign YAML; default to
`configs/library_first/EGFR_C797_covindb_pilot_v1.yaml` when omitted. Call
`covalent-discovery_covalent_library_first_chain` with `submit: false` first. Report every completed
stage, real run ID, quality-gate result, the sole next stage, exact registered inputs and resource
budget. Do not substitute an orphan or unrelated run.

Only if `$ARGUMENTS` explicitly approves that exact compiled stage and budget, call the same tool
with `submit: true`, `execution_mode: execute`, `human_approved: true`, and a meaningful approval
note. Track the returned run ID through `covalent_get_job` and logs. Never advance through a failed
quality gate, invent missing artifacts, or treat AF3 mPAE/CovDock score as binding affinity.
