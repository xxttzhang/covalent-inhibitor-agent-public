---
name: start-library-first-screen
description: Convert a natural-language target request into a traceable CovalentInDB Library-first covalent-inhibitor campaign. Use when a user asks in Chinese or English to find, screen, rank, or design covalent inhibitors for a target from a compound library, including requests such as “针对 EGFR C797 从库里筛选共价抑制剂”.
---

# Start a Library-first screen

Treat the user's sentence as the intake request. Do not require a project ID before resolving it.

1. Call `covalent-discovery_covalent_library_first_intake` with the verbatim request, source library, and `scope: auto`.
2. Handle the returned state exactly:
   - `READY`: show the normalized target/site card. If `target_inferred` is true, say that the site came from the frozen registry.
   - `SITE_MISMATCH` or `AMBIGUOUS_TARGET`: stop and request correction.
   - `TARGET_NOT_REGISTERED`: request a Target Passport, exact residue/chain, and structure evidence.
   - `CAMPAIGN_NOT_CONFIGURED`, `SCOPE_NOT_CONFIGURED`, or `INPUTS_MISSING`: report only the named blocker and required asset.
   - `UNSUPPORTED_LIBRARY`: do not substitute another library silently.
3. On `READY`, call `covalent-discovery_covalent_library_first_chain` with the returned `campaign_config_path`, `submit: false`, and `execution_mode: plan`.
4. Report the current parent-linked stages, quality gates, sole legal next stage, exact candidate ceiling, runtime, CPU/GPU/memory budget, and stop conditions. Never choose an orphan run.
5. Ask for one concise confirmation of the displayed next stage. After the user confirms that exact stage and budget, call the chain tool with `submit: true`, `execution_mode: execute`, `human_approved: true`, and a meaningful approval note.
6. Use `$covalent-job-execution` to monitor the returned run. Keep every failed molecule in the denominator and report artifacts and checksums.
7. After a stage reaches a terminal state, re-inspect the same Campaign. Advance only if the previous quality gate passed; otherwise stop with the recorded failure reason.

Use this frozen sequence:

`CovalentInDB QC/warhead mapping → target-aware 2D prescreen → CarsiDock-Cov → independent RTMScore → high-recall pool → AF3–mPAE → CovDock-LO pose gate → Pareto Top 20–30`.

Do not interpret AF3 mPAE as affinity, CovDock-LO as a full-library primary ranker, or incomparable raw scores as a weighted sum. Do not claim experimental validation.
