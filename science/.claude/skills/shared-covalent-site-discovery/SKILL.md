---
name: shared-covalent-site-discovery
description: Predict, evaluate and freeze candidate cysteine sites for a target before choosing Library-first, Ligand-first or De novo. Use when the user supplies a target but has not fixed a validated covalent site, or explicitly requests site discovery.
---

# Shared covalent-site discovery

Keep this workflow upstream of all three design routes. Do not register or start a
Library-first, Ligand-first or De novo campaign while discovering sites.

1. Read `covalent_capabilities`, `covalent_platform_health`, and
   `covalent_runtime_preflight`. Treat preflight status as inspection status.
   Compare the exact next tool budget with `resource_classes`; missing CUDA does
   not block `covalent_predict_cysteine_sites`, whose contract requires
   `gpu_count: 0`.
2. Resolve the explicit target symbol and taxon, discover candidate structures,
   and create a hash-bound receptor-input artifact. Tools with a historical
   `library_first` prefix may be used only for this shared receptor-input chain;
   do not call campaign intake, screening, docking, parameterization or MD.
3. Do not treat the residue used to identify a structure chain as the selected
   covalent site. Prediction must enumerate every Cys in the selected receptor
   chain and retain every score and explicit failure.
4. Call `covalent_predict_cysteine_sites` with `action: plan` first. Present its
   exact receptor hash, model/runtime hashes, CPU/memory/runtime budget and stop
   conditions. Starting it requires the platform's signed native approval; an
   approval note or `human_approved: true` alone is insufficient.
5. After an approved prediction completes, call
   `covalent_evaluate_covalent_sites` with the exact prediction manifest. Keep
   structure, model, literature, PDB and ChEMBL evidence separate. Missing
   evidence remains unknown.
6. Freeze only eligible sites with `covalent_select_covalent_sites`. Record the
   immutable artifact ID and hash. Stop before any downstream route unless the
   user separately requests it.

Report tool failures, evidence gaps and per-site blockers directly. Never reuse
historical calculations as a new run or claim that a model score validates
reactivity, activity or selectivity.
