---
description: Validate and display a De novo Generation route plan
agent: de-novo-generation-planner
---

Read the supplied project-local De novo request and call `covalent_plan_de_novo`. Report the
decision, missing assets, stage statuses, stop conditions, and exact next human decision. Do not
launch a generator or docking engine. Default example:
`projects/egfr-c797/plans/de_novo_request.example.json`.
