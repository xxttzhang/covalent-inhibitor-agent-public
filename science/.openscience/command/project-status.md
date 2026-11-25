---
description: Validate a target-independent covalent research project and summarize its decision
---

Validate `projects/$ARGUMENTS` with:

`python scripts/covalent_project.py validate --project-dir projects/$ARGUMENTS`

If validation passes, read `project.json`, `target_passport.json`,
`assessment/covalent_assessment.json`, `hypotheses/hypothesis-001.json`, and
`plans/route_plan.json`. Report the molecular context, cited evidence, unknowns,
GO/CONDITIONAL_GO/STOP decision, selected route, stop conditions, and the single next
human decision. Do not launch computation or modify project files.
