---
description: Resolve a natural-language Ligand-first request and show its next registered stage
agent: ligand-first-agent
---

Use `$ARGUMENTS` verbatim with
`covalent-discovery_covalent_ligand_first_intake`. If READY, call
`covalent-discovery_covalent_ligand_first_chain` in plan mode with the returned campaign
configuration. Show the normalized target, cysteine, parent ligand, next stage and bounded
resources. Show recoverable structure assets and deferred downstream dependencies separately;
do not treat an AF3/CovDock dependency as a blocker for an earlier structure or enumeration
stage. Never submit a calculation until the user explicitly approves that exact plan.
