# Library migration from cancelled screening to CovDock-VS

The original acceptance study has an approved screening task that was cancelled
before any stage was submitted. Ordinary `release_screening` deliberately only
releases unapproved work. It must not be relaxed to reinterpret an old approval
as permission for a new engine.

`migrate_screening_to_vs` is an authenticated host action with an explicit reason.
It is offered after the workflow has observed `CANCELLED` / `not_submitted`.
The host then independently rereads the exact bound task in its existing SQLite
ledger. It requires cancellation, no live observer lease, and every stage in
`WAITING` with no submission timestamp, remote handle or result manifest. The
original plan, intent, project and conversation identities must still match.
An unresolved, active, failed or previously dispatched task is not eligible.

The host seals the original operation in the same database transaction used to
exclude late compilers. Its closure is specifically
`CANCELLED_UNSUBMITTED_FOR_VS`; an ordinary release cannot consume this purpose.
The old task stays cancelled and its approval stays attached to its original
plan. No replacement task, permission prompt, remote command or computation is
created by migration.

The signed backend exchange archives the old handoff, ledger observation and
closure. It retains the workflow ID, target/site, prepared receptor and frozen
20,000-record input prefix, invalidates only the old screening plan, and enters
`VS_SCREENING_PLAN`. Its current execution is `not_submitted`, current approval
is false, and a separate field retains whether historical approval existed.
If transport fails after sealing but before the backend commit,
`recover_screening` finishes the same migration. It does not reopen the old
compiler. Ordinary status/resume reads the committed state after a lost reply.

## Native CPU prescreen

`plan_vs_prescreen` now binds the retained prefix, row-identity manifest,
receptor/reference/science configuration and fresh scientific review to an
immutable prescreen input. Its native plan uses `covindb-prescreen` with exactly
20,000 source records, at most five selected candidates, deterministic file-order
selection, and 0 GPU / 4 CPU / 16 GiB / 15 minutes. The global full-library path
is not used for either command compilation or dataset provenance.

`start_vs_prescreen` uses the existing host/MCP native approval bridge and job
registry. It registers an external CPU HPC task; it never launches local
chemistry. `vs_prescreen_status` verifies the original signed task and source
hashes. Uncertain registration is recovered by exact submission identity through
`reconcile`. Ordinary status/resume also revalidates the source, removes the
verified identity flag on tampering and does not increment unchanged revisions.
An unapproved unregistered plan can be explicitly released; a registered task
cannot be released or replaced through this action.

`evaluate_vs_prescreen` reads only the original signed task's registered output
files. It checks the frozen source/target identities, 20,000-row coverage across
the unique-parent and rejection tables, duplicate/invalid/hard-gate denominators,
and the selected Parquet/CSV/SDF identities. Candidate parents are independently
reconstructed from their source rows. The producer's electrophile index belongs
to its standardization-time atom order; it is never copied onto the regenerated
SDF. Unique Michael alpha/beta atoms are matched in the actual SDF and assigned
maps 905/906 for the later VS preparation contract.

The recorded quality gates must agree with their counts and the task exit
status. Zero candidates and quality-gate rejection are retained as explicit
outcomes, with no mapped VS input if ineligible. No unused shortlist slots are
filled. The immutable review and mapped SDF are revalidated on subsequent
status/resume; altered evidence withdraws the visible candidate summary. These
chemical checks and heuristic channel scores do not establish binding, reaction
feasibility, a common activity ranking, or MD readiness.

An explicitly requested reconsideration may archive a terminal, identity-verified
prescreen branch, including a zero-candidate outcome, before returning to target,
site or structure selection. It retains the task and review history. Queued,
running, uncertain or tampered evidence continues to block this navigation;
reconsideration never releases an approved task for reuse.

The host uses a separate owner-only `COVALENT_LIBRARY_DISPATCH_PROFILE`, with
the same fields as the Ligand profile but the exact configured
`*_covalent_library_research` tool. It invokes
`routes/library-first/ops/dispatch_library_vs.py`. The dispatcher checks the
signed route/engine and CPU budget, requires an immutable release/data binding,
runs the existing CLI dry-run before reservation, and keeps a receipt before
create. Any subsequent observation uses that receipt and never repeats create.
The Library profile has not been installed in the live acceptance environment.

## Native VS input, approval and task recovery

`prepare_vs_input` independently revalidates the signed prescreen task and its
registered products, binds the exact mapped SDF in accepted row order, and
verifies the receptor against both retained screening inputs and original
receptor evidence. The current pilot supports the already-bound 1M17 EGFR
receptor: canonical C797 remains author chain A residue773. No implicit
renumbering, receptor replacement, new candidate selection or quota filling
occurs. The original context and complete source inventory remain available
for review. Receptor preparation is not environmental suitability.

`review_vs_environment` records independent source-bound judgments for assembly,
excluded components, construct/site and reaction scope. An insufficient or
negative assessment does not admit execution. Changing a judgment retires the
previous private runtime plan. `check_vs_runtime` compiles the exact prepared
input with 0 GPU / 4 CPU / 16 GiB / 60 minutes, at most five actual candidates,
and concurrency limited by both candidate count and available CPUs. Its
normalized plan stays in an owner-only file; public output contains the digest
and budget, not installation paths. A configured license is not proof of a
successful vendor license checkout.

`start_vs` uses separate native approval and registers a CPU external-HPC task.
The host selects the Library dispatcher for `start_vs` and `vs_status`, using
the same receipt-preserving protocol as prescreen. An uncertain native
registration is recovered through `reconcile`; it does not re-submit the
prescreen or VS task. Source changes invalidate observations and produce an
unknown outcome, retaining the original task identity. Returning original bytes
allows that same task to be verified again. An active or uncertain VS task
cannot be replaced by reconsidering the study.

`evaluate_vs` independently rereads registered execution receipts, prepared
receptor/ligand Maestro files, their preparation witness and the signed task's
exact compressed raw output. Candidate IDs, row order, heavy-atom lineage,
Michael product chemistry and the full prepared receptor graph must agree.
Preparation also compares every receptor heavy-atom chain/residue/name/element
with the approved original PDB. A retained target sulfur alone is insufficient.
The receptor recipe is now named `prepwizard-heavy-identity-bound-noepik-ph7-v2`:
identity preservation does not claim fixed coordinates or MD readiness.
Every output pose retains its own receptor coordinates. Missing output
candidates remain in the original prescreen-selected denominator.

The Library-specific policy and reference contact sites are frozen into the
input before environment review and approval. It checks C–S distance1.65–2.15Å,
product angle80–140°, zero severe nonbonded clashes at0.75 of summed vdW radii,
clashing-ligand fraction≤0.20, converged candidate local relaxation energy≤20
kcal/mol, and reference polar-contact-site retention≥0.70. Contacts are unique
protein N/O sites within3.5Å of the bound reference's N/O atoms in the retained
receptor frame; candidate N/O proximity to those exact sites is the proxy.
Missing sites or unavailable strain remain unknown. All gates must pass on the
same pose. These are explicitly provisional research limits, not validated
potency, reactivity, hydrogen-bond assignment or energetic-hotspot claims.

There is no declared common scaffold across library molecules, so scaffold RMSD
is explicitly not applicable. No strain of an unrelated reference molecule is
subtracted. The local relaxation estimator uses the hydrogen-capped postreaction
ligand fragment and is not the vendor's local-strain field or a global minimum.
Clash exclusions use the actual product bond graph through1–4 across the new
C–S bond, never a blanket exemption for the entire target cysteine.

`report_vs` saves an immutable report with per-pose measurements, gate failures,
missing evidence, denominators and limitations.
The public summary also includes the frozen policy and aggregate per-pose
failure/missing reasons, so the webpage can explain an outcome without shell
inspection. Status/resume revalidate the
registered products and implementation before showing a saved review; changed
evidence withdraws summaries and reports. Restoring original bytes recovers the
same task. A completed, freshly verified reviewed branch can be explicitly
archived through reconsideration, preserving all task/review history; an active,
uncertain or changed-evidence branch cannot. A passing pose is eligible for
further review only: MD admission remains separate.

## Remaining integration

The user clarified on 2026-11-03 that VS products must not feed MD directly.
VS serves screening and candidate review; retain CovDock-LO refinement before
the existing MD admission path. Direct VS-to-MD adapter development is outside
the requested migration and is not a release prerequisite. The added direct
VS pose-source contract was reverted before deployment.

The source changes still need a unified fixed host/scientific-worker release
and actual webpage acceptance. `native_vs_workflow_ready` is enabled in the
fixed acceptance candidate source; runtime/source/environment checks and
separate exact native approvals still apply. It is not a deployment or
scientific acceptance flag. Do not count isolated registration/dispatcher tests as live
web acceptance.

## Verification

- `workflow-execution.test.ts` uses real temporary SQLite stores to verify
  eligible migration, refusal of submitted/uncertain/leased work, historical
  approval retention, restart recovery and rejection of late compilation or
  reapproval.
- `workflow-host.test.ts` runs the real MCP Python process and host tool
  projection, including a lost transport after the SQLite closure and recovery
  into VS planning. Historical approval is seeded only in the isolated test
  ledger. Its fake CLI accepts dry runs only; no scientific worker runs.
- `test_workflow_screening_handoff.py` checks signed purpose/task/intent binding,
  rejected model-only calls, retained inputs/history and no transferred approval.
- `test_library_vs_prescreen.py` compiles with an intentionally unavailable
  global full-library path, verifies the exact frozen prefix and CPU scope,
  checks native signing/registration and lost-response recovery, and rejects
  changed sources. It never launches a worker or scheduler.
- `test_dispatch_library_vs.py` checks the Library engine/route/resource limits
  and observation-only recovery with bounded fake CLI responses. Existing
  Ligand VS and HPC reservation tests exercise the shared transport as well.
- `test_library_vs_prescreen_evidence.py` runs the actual chemical prescreen on
  synthetic 20,000-row fixtures with one and zero eligible candidates, registers
  those outputs in isolated signed test jobs, then exercises native review,
  atom-order remapping, zero-candidate retention, tampering rejection and recovery.
  It performs no remote or vendor computation and is not live acceptance.
- Host approval tests compare Python/TypeScript action classification; host
  dispatch tests check separate Library configuration and retained receipts.
- `test_library_vs_input.py` starts with actual synthetic prescreen products and
  the verified external receptor fixture. It checks original residue mapping,
  source-bound judgments, private plans, tampering/restore behavior, independent
  approval and recovery after a lost native registration response. The
  controlled vendor files are planning fixtures only and are never executed.
- `test_library_vs_evidence.py` produces actual synthetic Maestro files for a
  separately signed native VS job. It checks original product identity,
  Library-specific measurements, missing candidate retention, immutable report,
  source tamper/restore and explicit terminal branch archival. Random fixture
  coordinates deliberately fail geometry; no vendor calculation is performed.
- `test_library_vs_geometry.py` checks that contact or strain evidence cannot be
  replaced by a physical-only pass and that actual cross-bond exclusions never
  exempt the entire cysteine residue.

The tests require the existing external, hash-verified receptor fixture via
`COVSCREEN_RECEPTOR_FIXTURE_ROOT`. The host test accepts `COVSCREEN_TEST_PYTHON`
for the prepared scientific test environment rather than requiring an excluded
cache directory inside the source tree.
