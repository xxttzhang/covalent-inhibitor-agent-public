---
description: Plan or execute an approved De novo registered generation job
agent: covalent-scientist
---

Use `covalent-job-execution`. Validate the approved De novo route, project-local pocket/protein
inputs, target residue, pocket definition, generator choice, seed, and resource budget. Compile
PocketXMol, REINVENT4, DiffLinker, or deterministic RDKit generation with `execution_mode: plan`
first. Show the registered command, input and checkpoint hashes, candidate/runtime/GPU limits,
outputs, stop conditions, and scientific scope. Execute only when `$ARGUMENTS` explicitly approves
that exact plan; then set `execution_mode: execute`, `human_approved: true`, and a meaningful
approval note. Poll status and return candidates, invalid/duplicate proposals, artifacts, logs,
and checksums. Never interpret generation confidence or likelihood as affinity or reactivity.
