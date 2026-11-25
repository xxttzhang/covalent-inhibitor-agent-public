---
description: Legacy compatibility command for the retired Ligand-first Attachment Planner
agent: ligand-first-attachment-planner
---

This command is retained only to audit or reproduce legacy attachment-planning artifacts. It is
not part of the formal natural-language Ligand-first route. For a new Ligand-first request, use
`/ligand-first-start` or `/ligand-first-execute`, which invoke the registered intake and stage chain.

For an explicitly requested legacy audit, use `$ARGUMENTS` to identify a project-local
`LigandFirstRequest`. Validate the project, call
`covalent-discovery_covalent_plan_ligand_first`, and follow the stages: noncovalent pose → scaffold
modification map → attachment/vector/linker/warhead plan → controlled generation → CarsiDock-Cov
geometry screen → unified candidate pool. Use `covalent_plan_attachments` only as the lower-level
atom scorer. Return stage gates and evidence gaps, not synthesized molecules or experimental
claims. Missing pose, scaffold annotation, vector geometry, interaction retention, clash, or
synthetic-accessibility evidence must stop advancement at the matching NEEDS_* decision.
