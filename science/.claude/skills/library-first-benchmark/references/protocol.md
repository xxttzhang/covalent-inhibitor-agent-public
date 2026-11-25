# BTK-mini frozen benchmark protocol

## Dataset

- Target: Bruton's tyrosine kinase (BTK)
- Reference structure: PDB 5P9J
- Reactive residue: chain A, Cys481 SG
- Reaction class: Michael addition
- Warhead family: acrylamide
- Library: 420 rows, comprising 20 active protomers and 400 property-matched decoys

## Supported routes

| Engine | Ranking direction | Expected input | Expected success | Expected failure |
| --- | --- | ---: | ---: | ---: |
| CarsiDock-Cov | lower is better | 420 | 318 | 102 |
| CarsiDock-Cov + RTMScore | higher is better | 420 | 318 | 102 |
| AlphaFold 3 mPAE | lower is better | 420 | 417 | 3 |
| Schrödinger CovDock-VS | lower is better | 420 | 412 | 8 |
| Schrödinger CovDock-LO | lower is better | 420 | 413 | 7 |

## Required reporting

Use all 420 compounds for conservative virtual-screening metrics. Rank explicit failures after successful scored compounds. Report ROC-AUC, EF1%, EF5%, BEDROC, Top-10 active count, adjusted LogAUC, runtime, and successful/failed counts. Label any alternate missing-data treatment as a sensitivity analysis.

The machine-readable requirements in `benchmark/specs/BTK-mini-v1.json` override this narrative when they disagree.
