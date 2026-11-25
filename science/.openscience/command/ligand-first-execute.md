---
description: Plan or execute the next approved stage of the formal Ligand-first chain
agent: ligand-first-agent
---

Pass the verbatim `$ARGUMENTS` to `covalent_ligand_first_intake`. If intake is not READY, return its
exact missing or conflicting fields and stop. If READY, call `covalent_ligand_first_chain` with the
resolved Campaign and `execution_mode: plan`. Report the current stage, compiled next stage,
registered command, input hashes, resource budget, quality gate, expected outputs, and stopping
conditions.

Execute only when `$ARGUMENTS` explicitly approves that exact compiled stage and budget. Then call
`covalent_ligand_first_chain` with `execution_mode: execute`, `human_approved: true`, and a
meaningful approval note. Poll the returned job, inspect its full-denominator candidate and failure
tables, and call the chain again to compile the following stage. Never skip directly to a later
stage or invoke the legacy Attachment Planner. Generation, docking, AF3 confidence, and Pareto
selection are computational evidence, not synthesis or experimental validation.
