---
description: Resolve a natural-language target request and start the registered Library-first workflow
agent: library-first-agent
---

Use `$ARGUMENTS` as the user's verbatim screening request. Apply the
`start-library-first-screen` skill. Call `covalent-discovery_covalent_library_first_intake` first.
If READY, inspect the returned Campaign with `covalent-discovery_covalent_library_first_chain` in
plan mode and show the normalized target/site, sole next stage, candidate ceiling and resource
budget. Submit only after explicit approval of that exact plan. Never invent a project path,
reactive site, structure, library, or missing Campaign.
