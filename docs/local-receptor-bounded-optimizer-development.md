# Bounded local receptor optimization: development status

This document retains the development rationale and records the subsequent RC13 trial.
The versioned `v2-local-bounded` policy was deployed in RC13 and approved once at
revision86. That authorization was consumed by the single trial below; this document
does not authorize another trial. Earlier policy versions and results remain immutable.

## Evidence motivating the change

The single approved trial `20261105T104448-library-first-receptor-preparation-6a196e2f09`
finished with local_motion_limits_failed after 229.04 seconds. Its observed-atom
comparison found 35 violations among 125 movable atoms. All 2271 fixed atoms,
including the six observed CYS797 heavy atoms, remained unchanged. Four pocket atoms
in THR854/ASP855 exceeded the 0.2 angstrom limit. No complete geometry audit was
produced because the displacement gate failed first. These results do not establish
that the source is irreparable or the molecules inactive.

The harmonic force was implemented as planned; it penalizes displacement but does
not enforce a hard positional boundary. Raising post hoc acceptance limits would
change the scientific question and is not the remedy.

## Implemented development prototype

`science/src/covscreen/receptor_bounded_minimizer.py` implements projected gradient
steps on the product of per-atom Euclidean balls, exact fixed coordinates and free
coordinates for other atoms. Every energy/gradient evaluation is feasible. Armijo
backtracking accepts only energy-decreasing proposals; safeguarded spectral steps
adapt the step length. Termination uses a projected gradient residual, so a boundary
optimum need not have zero raw force. Iteration, evaluation and backtracking limits
produce explicit nonconverged results. Invalid/nonfinite input or oracle output
fails closed. Convergence never sets scientific_acceptance=true.

Projection uses a small inward floating-point margin and representable-coordinate
correction to survive translation of the coordinate frame and nm-to-angstrom
conversion in the independent audit. This is a numerical guard, not a widened
acceptance threshold.

The OpenMM adapter verifies exact zero-mass/fixed-atom agreement and rejects periodic
systems, holonomic constraints and virtual sites. It restores initial coordinates
on exceptions and the last accepted coordinates on normal termination. It is used only by the explicit bounded policy in the RC13 final minimization stage.
Earlier policies do not select this adapter.

Projected-gradient and feasible line-search methods are established optimization
approaches; see [PETSc/TAO's optimization manual](https://petsc.org/main/manual/tao/).
That reference does not validate this custom spherical-domain implementation or its
suitability for receptor preparation.

## Verification and remaining gates

Thirty tests pass across the development optimizer and existing local policy:
analytic constrained optima, free/fixed/coupled variables, spherical rotation
invariance, 200 translated-coordinate cases, energy monotonicity, budget exhaustion,
invalid inputs, nonfinite gradients, an OpenMM synthetic force system, and independent
local-motion audit after unit conversion. No receptor minimization was run for these
tests; the toy systems are not scientific repair evidence.

Before another scientific trial:

1. Register a distinct versioned bounded policy; keep the old v2-local behavior and
   hashes immutable. Bind optimizer configuration and implementation to the exact
   execution scope. Do not reinterpret old approval as approval for this method.
2. Preserve the original atom mask, source/reference coordinates, 0.2/0.5 angstrom
   limits, fixed CYS797 and geometry thresholds. Use the existing harmonic objective
   plus hard displacement balls, changing only the final-stage optimization method.
3. Add the explicit recovery action for a registered failed local trial, validate
   its signed task and provenance, and preserve its failure/history. No silent
   fallback or automatic retry.
4. Integrate final-stage optimizer diagnostics with immutable receipts and independent
   replay. Distinguish projected stationarity, unconstrained force and scientific
   geometry acceptance. Nonconvergence must not claim a repaired receptor.
5. Verify workflow/compiler/worker/receipt/admission integration, exact native scope,
   rejection of altered policy and no unapproved dispatch. Package a new immutable
   release only after those tests pass.
6. Compile the exact new plan, resource and evaluation/time budgets; present it for
   fresh user approval before any molecular calculation. The previous approval is
   exhausted. After that trial, recheck full geometry, displacement, CYS797 and pocket
   environment independently. VS/LO/MD remain separately gated.

If the unchanged spatial domain cannot yield a geometrically acceptable structure,
return to evidence-based source revision. Optimizer stationarity alone cannot decide
that question.

## Working-tree integration (2026-11-05)

- Recovery verifies the signed failed harmonic-local task, its registered motion
  failure, the unique original v1 source-collision witness, and all original
  observed coordinates. Fixed-atom or identity failures cannot use this recovery.
- Selection archives both the task and the earlier selection; it does not start
  a task. Compilation revalidates those witnesses and preserves the spatial mask.
- Optimizer settings: at most 1000 iterations, 3000 evaluations, 40 backtracks;
  projected gradient max tolerance 10 kJ/mol/nm, initial step 1e-4, maximum step
  1e-2, residual step 1e-3. These are frozen algorithm parameters, not new
  geometry or displacement acceptance thresholds. Nonconvergence fails closed.
- Independent receipt audit rejects missing, nonconverged, overbudget or internally
  inconsistent numerical records. Existing identity, geometry, motion, environment
  and downstream admission checks remain required.
- Core tests: 61 passed; focused workflow tests: 4 passed. Their fixtures copy
  historical outputs into isolated test registries and replace worker launches
  with no-op processes; they are not additional receptor calculations.

## RC13 trial and current recovery evidence

The separately approved trial `20261105T112703-library-first-receptor-preparation-668e9e8a1c`
failed after 1000 accepted iterations and 2180 evaluations. Maximum projected gradient
was 24.0305909 against tolerance 10. Workflow revision88 preserves that failure.
Diagnostic reconstruction found zero observed-motion violations, fixed C797, and
seven original VDW pairs passing their unchanged thresholds; full geometry is unverified.

A static single-point audit identified 30 modeled heavy atoms and 37 hydrogens above
10, while the maximum among 125 movable observed heavy atoms was 1.05525. Recomputed
energy differs by -0.00222373 kJ/mol; numerical identity is not claimed. The registered
energy trace has 1179 rejected candidate evaluations; its last 100 accepted steps
still reduce energy by 20.7747 kJ/mol. There is no per-step gradient trace, so these
numbers cannot establish a required additional iteration budget or prove an optimizer
replacement will converge.

The next development comparison should test a free-atom block method on analytic
coupled objectives before any receptor calculation: keep observed coordinates fixed
within that block, preserve the full spherical projection when updating observed
atoms, and use the same full-system projected residual for final convergence.
A block's convergence cannot substitute for full-system convergence. Count every
oracle call against one shared budget and preserve monotone energy acceptance.
No molecular restart, source substitution, changed tolerance, or new trial is authorized.
Implementation, integration tests, immutable release and an exact native plan must
precede a new approval request.

Undeployed diagnostic changes now persist stage identity and topology before
optimization, verify connectivity across hydrogen rebuilds, and expose registered
inner failure diagnostics without workflow mutation. These do not alter RC13's
historical receipts or turn failed coordinates into admitted inputs.


## Undeployed free-block warmup prototype

`receptor_block_minimizer.py` now implements an explicit free-coordinate warmup
followed by the original full bounded solve. It first validates the full domain
and computes the initial full residual without moving coordinates. The warmup
holds both fixed and bounded observed atoms at their current positions. The joint
phase restores the original reference balls. All phase starts and line-search
calls consume one shared evaluation budget; accepted iterations share one cap.
If the budget ends in the warmup, the full residual is marked unavailable and
convergence is not claimed, even when the free block converged.

Seven analytic tests passed: a coupled constrained optimum, exact fixed/held
coordinates, every-call ball feasibility, shared evaluation caps, full-system
versus free-block convergence, and rejection before evaluating invalid input.
A small quadratic comparison used identical 2000 iteration/5000 evaluation caps
and tolerance 1e-5. Joint versus warmup+joint evaluations were 5 versus 7 for zero
coupling, 13 versus 9 for coupling30, and 30 versus 9 for coupling43. This is
limited numerical evidence, not receptor evidence or a general speed guarantee.
The prototype is not registered in policies, releases, worker routes or approval
schemas. It must not be used on the saved receptor coordinates without a new plan.


### Failure semantics and coupled verification

The prototype now stops immediately on warmup line-search failure/stalling rather
than treating failure as permission to enter the joint phase. Only a converged
warmup or its predetermined iteration cap can transition, with remaining shared
budget. A nonfinite oracle raises before any fallback. Six seeded dense rotated
positive-definite quadratics (eigenvalues1..300) verify the final residual using
an independently coded spherical projection; fixed coordinates and per-call
feasibility are asserted. Prototype tests:15 passed; together with the unchanged
bounded optimizer:41 passed.

A local, unregistered `FREE_BLOCK_PLAN_DRAFT.json` binds the exact historical source
and unchanged atom mask. Proposed warmup:100 accepted steps within the existing
1000-step/3000-evaluation total, with one CPU, zero GPU,4GB,30min and one trial.
It starts from the same source/preparation stages rather than reusing failed
unadmitted terminal coordinates. The draft is not a frozen native approval scope.
Receipt validation, versioned policy/worker/compiler integration and release checks
remain prerequisites. No molecular calculations were performed for these tests.


### Numerical receipt and OpenMM adapter verification

The prototype now exports coordinate-free phase receipts and independently validates
full convergence, exact phase order, accepted-step/evaluation accounting, shared
budgets, stage termination conditions, energy continuity, and full residual agreement.
Missing fields, falsified totals, a failed warmup followed by joint work, and free-only
convergence are rejected. This validator checks numerical evidence only; it cannot
replace molecular geometry, identity, displacement or environment audits.

The original OpenMM adapter now delegates through a shared private context wrapper;
its public bounded entry point still selects the original minimizer. The new
undeployed block entry point shares periodic/constraint/virtual-site/mass checks and
exception restoration. A three-particle synthetic force test verified feasible final
coordinates and restoration after a nonfinite-energy exception. No receptor or job
was used. Combined original and block tests:53 passed (block27, original26).
Neither the block policy nor its worker/compiler route is registered yet.


### Working-tree versioned worker integration

Registered `v2-local-block` as a distinct exact policy configuration. It can be
constructed only from `v2-local-bounded`, preserves the entire source atom mask,
stages and total budgets, and cannot be relabeled as an older version. Worker CLI
and execution validation recognize it explicitly. Final-stage dispatch chooses the
block adapter only for the exact block configuration; other policies retain their
previous minimizer. Phase receipts strip coordinate arrays before JSON serialization.
The independent quality audit dispatches to the block receipt validator and still
runs the unchanged identity/motion gates. Release allowlists include the module/tests.

Validation:64 optimizer/policy tests and28 quality/relaxation tests passed. A synthetic
block receipt around historical coordinates verifies that numerical convergence
cannot admit a moved fixed reactive-site atom, even after rehashing local fixtures.
No real receptor minimization or worker dispatch occurred. This is working-tree
integration only: workflow recovery selection, predecessor evidence derivation,
readback and native compiler integration remain incomplete. RC13 stays deployed.


### Working-tree recovery and compiler binding

`select_block_local_receptor_policy` now selects only a verified failed bounded
predecessor. Derivation verifies its native signature/scope, exact policy digest,
current input manifest, registered failed pipeline and iteration-limit numerical
receipt. It rederives the unique prior harmonic-local policy from the original
registered source-collision evidence and compares all observed source coordinates.
The new selection preserves predecessor tasks and earlier selections. Every request
compilation rederives evidence and rejects changed artifacts before any dispatch.
Readback recognizes the new version; unavailable full residuals after a failed free
phase are explicitly distinguished from zero or converged residuals.

This code remains undeployed. The existing RC13 task and revision88 are unchanged.
Native request freezing, release verification, deployment and fresh user approval
have not occurred. Recovery integration uses isolated signed fixtures and no-op
workers; its results cannot be presented as a successful receptor trial.
