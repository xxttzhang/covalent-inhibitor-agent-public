# Persistent screening executor

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

> 2026-10-09 文档状态更新：当前 Library-first 工具与 skill 的统一入口是 [Library-first 技能包](../../library-first-skills/README.md)。EGFR test100 核心筛选、所选八候选 AF3/CovDock 及固定八体系 MD/分析均已有实际批次验收。下文早期“未完成/待验”叙述保留为历史背景，不能代替当前任务状态；新靶点和任意新候选的全链自动化仍未验收。技能包中的阶段说明及证据索引为当前使用依据。

Experimental production integration, not yet scientifically accepted end to end.
Uses realZillionX/InspireSkill CLI, not direct platform API calls or old pilot inputs.

## One-sentence platform entry

The platform also supports an explicit `test100` profile:

> 针对 EGFR C797，使用 CovInDB 虚拟筛选库，100候选测试，点券不设上限。

This freezes `max_points: null` (not a large numerical stand-in), at most 100,000
source rows and exactly 100 QC-passed candidates required before docking. Each GPU
stage uses one GPU sequentially. Stage deadlines are prepare=60, CarsiDock=120,
RTMScore=60 and report=10 minutes; these are safety limits, not throughput claims.
Fewer prepared candidates pauses verification before any downstream dispatch.
Costs are still estimated from live rates and actual stage timing is retained;
this is not unlimited time, automatic failure retries, a billing guarantee or
whole-library/refinement authorization. Native resource approval still applies.
Old tasks retain their original budgets/profiles; no implicit migration is made.

`entry-acceptance.ts --preflight --test100` sends that sentence to the running
platform in a compute-denied test session and checks the resulting persisted plan,
native approval event, no submission, and zero model generation tokens.

The running source host now routes direct EGFR C797 / CovInDB virtual-library
screening commands through `src/session/screening-entry.ts` and the normal wrapped
`inspire_screening_task` tool, before LLM generation. Example:

> 针对 EGFR C797，使用 CovInDB 虚拟筛选库，标准筛选，预算上限50点券。

Missing mode/budget opens the persisted native intake. Complete parameters run
live preflight and present the existing exact resource approval. This entry does
not bypass permission rules or approve spending from model text. After approval,
the existing daemon advances the four core stages without another model choice.
Questions, quotations/examples, negations, unsupported whole-library/refinement
requests, attachments and Plan mode are excluded from the shortcut. Failures end
the turn visibly without automatic task replacement or shell/compute fallback.

The current `standard` policy is still bounded: up to 10,000 source rows and five
promoted docking candidates. It is not full-library screening, nor an empirical
throughput estimate. AF3/CovDock execution remains outside this entry. Changing
policy/scientific scope requires separate implementation and explicit approval.

`entry-acceptance.ts` tests the real running chat endpoint with compute denied:
one actual tool call, zero generation tokens, native missing-condition questions
or a persisted preflight draft cancelled at the approval boundary. No GPU job is
submitted. Host entry changes require an idle backend restart, not plugin reload.

## Contract

Natural-language requests first pass a persisted clarification intake, then create
immutable DRAFT tasks. Only the native
`remote_compute` approval for that exact task digest enables execution. Account,
Notebook, source/engine hashes, target, requested/selected screening mode, derived
scope/shortlist, resource reservations, live rates and deadlines are included in
the digest. Changing host configuration must
not redirect already-approved tasks. No model-supplied approval boolean or shell.

Current core order: fresh virtual-library preparation/QC → CarsiDock-Cov →
RTMScore on the successful CarsiDock poses → verified report. The current audited
target is EGFR C797 with explicitly disclosed 1M17 A:CYS:773 (+24) receptor mapping.
This does not silently claim the separate 4G5J registry template was used.

Full objective remains unfinished: selection has an explicit rank-disagreement
triage implementation, but it has not yet consumed a real completed core run;
separately approved AF3/CovDock execution is still unfinished. No automatic whole-library AF3,
no claim that CarsiScore/RTMScore disagreement is calibrated uncertainty, and no
substitution of historical 130-molecule inputs for the user's virtual library.

## Files and lifecycle

- `model.ts`: strict task/phase/resource schemas and output identity checks.
- `store.ts`: SQLite WAL state, immutable approvals, exclusive lease, dispatch
  reservations, cancellation and event history. Never discard old active tasks.
- `runner.ts`: ordered advancement, reconciliation, quality verification, stop.
- `inspire.ts`: explicit CLI arguments, live preflight, status and output reads.
- `worker.py`: fixed scientific commands; independent input/parent/artifact checks;
  environment, molecular identities, failure tables and report evidence.
- `tool.ts`, `plugin.ts`: platform interface and native approval boundary.
- `refinement.py`: direction-aware tied ranks, disagreement-review quota plus
  separately labelled consensus/diversity controls, identity audit and small-subset caps.
- `host.ts`, `daemon.ts`: detached local observer independent of the chat request.
- `progress.ts`: publishes bounded persisted task/stage metadata onto the exact
  originating tool card; never invokes the model or authorizes/submits compute.
- `register.ts`: safe plugin registration/reload only when research sessions idle.
- `approval.ts`: persist then immediately request native approval in one host call.
- `refinement-approval.ts`: source-only independent single-engine refinement plan,
  SQLite draft and native approval boundary. Not registered as a model tool or
  connected to the observer; it is not yet a runnable refinement feature.
- `refinement-runtime.ts`, `refinement-command.ts`: explicit runtime asset roles
  and deterministic scientific argv derived from the frozen independent plan.
  Command construction is checked before/after approval; it is not submission,
  a resource receipt or proof that an engine is ready.
- `refinement-runs.ts`, `refinement-runner.ts`: source-side single-engine execution
  records, fenced SQLite leases, one submission reservation and persistent
  reconciliation/cancellation. Not connected to the live daemon yet.
- `refinement-inspire.ts`: concrete CLI transport and fixed preparation composition;
  still requires real runtime/license and scientific outcome validators before
  registration. Local CLI simulator tests are not remote scientific execution.
- `refinement-preparation.ts`, `stage_bootstrap.py`: independently approved,
  lease-bound CPU Notebook preparation and retained SQLite readback receipts.
  The bootstrap verifies the frozen checker bytes and entire release before
  sourcing activation; it is trusted host code embedded in the positional command,
  not an extra mutable controller fetched from the remote release.
- `refinement-result.ts`: exact-plan candidate/score/structure/failure outcome
  validation after the transport freshly verifies real engine artifacts.
- `refinement-readback.ts`, `refinement_readback.py`: source-side completed-output
  reader. Rechecks the frozen parent/selection/release, hashes actual artifacts,
  persists the first verified result witness and reuses it across interruptions.
  The fixed transport reads before/after independent scientific validation and
  rejects accepted scores/structures that differ from these files. Not registered
  in the live observer and never a substitute for the scientific validator.
- `selection-evidence.ts`: source-side read-only selection verification through
  the CLI transport and composition with the independent approval boundary.
- `engine_interface.py`, `engine-interface.ts`: read-only exact-input entry/guard
  checks, frozen-source identity comparison and live quota price gate. Static
  support does not prove imports, GPU compatibility, model or license readiness.
- `code_release.py`: bounded content-addressed source archives and exclusive
  installation. Verifies the complete inventory and rejects changed, additional,
  missing or symlinked files; never overwrites an existing release.
- `request.ts`: bind the tool-calling assistant's actual parent user message via
  the platform SDK; preserve text and record source part/message IDs and SHA256.
- `intake.ts`: conservatively recognize explicit conditions or ask real native
  questions; persist answers and atomically bind exact confirmed parameters to a
  single task before resource approval. No model-provided consent fields.
- `resource-planner.ts`: versioned economy/standard/deep profiles that derive the
  exact row cap, shortlist and four stage reservations from a user-facing mode;
  costs use live Inspire quota rates and budget mismatch always needs a second
  explicit native confirmation rather than a silent downgrade or cap increase.
- `report.ts`: read-time revalidation of every core manifest and its actual files;
  bounded remote artifact inventory, without molecular contents or job submission.
- `reload-observer.ts`: gracefully replace an explicitly identified executor observer;
  never restarts the platform server or resets remote jobs/approval receipts.

Local configuration: `/path/to/workspace/configs/inspire-executor.json`.
Local state: `/path/to/workspace/.cache/inspire-executor/tasks.sqlite`.
Remote versioned execution root is recorded in repository `INSPIRE.md`.

`create` first resolves missing/unconfirmed conditions, then performs remote
read-only fingerprinting and four CLI dry-runs, saves the
immutable DRAFT, and immediately requests native approval in the same tool call.
It waits for the human response before storing a receipt and starting the daemon;
no second model decision is needed to present approval. A rejected/interrupted
or expired approval closes its unchanged, unapproved draft as CANCELLED and saves
the reason; late replies cannot revive it. It does not cancel an already-approved
task. `approve` only reopens a still-DRAFT, unexpired plan in the same conversation;
it is not a model-supplied approval flag. Closed tasks require a new explicit user
request, never an automatic replacement.

Native permission waits now accept the host tool's AbortSignal (not serialized
into approval metadata). Interrupting a tool withdraws only its exact native card,
unlike a user's reject action, which retains the host's session-wide rejection
semantics. The original project owns the withdrawal event even when another
project triggers the abort. Terminal tool errors preserve progress metadata so
the closed task and no-compute reason remain visible after refresh. Unapproved
plans have no pending automatic delivery status.

These interruption changes include `src/permission/next.ts`,
`src/session/prompt.ts`, and `src/session/processor.ts`; plugin hot-reload alone
does not activate already-cached host modules. They require a separately
authorized, idle backend deployment. No application/server restart was performed
as part of the local 2026-09-12 approval-interruption verification.

### Host-verified refinement selection

`select_refinement` now routes through `selection-proposal.ts`: re-read and verify
the complete core report before selection, validate the worker's full bounded
proposal against the saved task/report and requested policy, then invoke the
read-only `read-selection` action with the exact selection/manifest digests. Only
an identical second witness is recorded as `refinement_subset_proposed`.

Validation includes candidate identity hashes and uniqueness, source counts,
effective fractional cap, each channel's quota, consistent rank differences and
the requested leading/consensus/disagreement thresholds. Unknown fields are
rejected rather than forwarded as possible molecular tables. Actual ranking and
scaffold-diversity recomputation remain in the frozen remote worker; the host
does not claim to reproduce those checks from the limited returned fields.

`NO_CANDIDATES_MEET_POLICY` is a valid result, including a zero effective cap.
It retains an empty identity digest and does not fill quotas, broaden the pool,
start engines or generate resource approval. Selection evidence is persisted in
the task event history and returned by the tool. `selectionSnapshot` revalidates
the latest saved proposal for status/list and native progress metadata without
remote I/O. The originating card shows the complete bounded identity/reason list,
thresholds, channel quotas, denominator and historical record time; unknown
additional budget is explicitly not zero or resource approval. An invalid latest
record shows UNAVAILABLE rather than silently restoring an older candidate list.
The detached `inspire_selection` metadata is bound to the exact task and digest
and survives an older progress observer's snapshot updates. Card display never
submits jobs or invokes the model. AF3/CovDock still require an independently scoped
plan, live resource cost, native approval and production scientific validation.

### Clarification before task creation

The model may propose conditions or omit unknown ones. Arguments are suggestions,
not evidence of the user's choice. The intake recognizes only explicit target,
library, economy/standard/deep screening mode and maximum points. Users are never
asked to engineer source-row caps, shortlist size, CPU/GPU quotas or phase minutes;
those are derived by the versioned planner and displayed on the resource card.
Unsupported, negated, conflicting or historical clauses are ignored instead of
erasing separate explicit clauses. Ambiguous groups are presented in at most two
native question tabs. A suggested value takes effect only if the user adopts it,
or the user may enter their own values. Examples are not defaults.

Before any remote preflight or screening task exists, SQLite records the bound
request hash, original message IDs, questions and answers. Closing/aborting or
giving invalid answers creates no task and no resource approval. Confirmed values
survive a tool retry; changing model arguments cannot replace them. A later task
must match those values exactly, and task creation plus consuming the intake is
one transaction. Retrying the same original request cannot duplicate a task or
silently revive an expired draft. A pending/dismissed/interrupted intake requires
waiting or a new user message, not an automatic repeat question.

The native bridge dynamically imports the current source-host Question and
Instance singletons, with project/call binding and abort cleanup. The bundle must
remain alongside executor sources, as required by host.ts. Tests exercise both
source and bundled code against the actual Question HTTP routes; this is not a
claim of support for an unrelated compiled-server deployment. Plugin reload now
also refuses pending questions or approvals. No frontend/backend restart is
needed for this source-host integration.

These confirmations establish intent only. Actual available resources, rates and
immutable asset fingerprints are read afterwards. If the live estimate exceeds
the cap, the platform first asks whether to accept a named lower mode or increase
the cap; rejecting that adjustment creates no task. A fitting plan is frozen and
the user must separately approve its exact native resource card before execution.

`status/list` return persisted evidence. `report` requires all four core phases to
have succeeded, validates their saved evidence and exact approved plan, then
rehashes remote artifacts through the verifier and compares current manifests
with the previously recorded ones. It exposes `report_ready=true` only after
these reads pass. Missing/unreachable evidence is not proof of failed computation;
changed/rejected evidence is not deliverable. Neither case resets task state or
resubmits work. The read result is persisted as a `report_read` event.
The inventory returns at most 50 filenames/checksums per phase, marks truncation,
and points to each complete remote manifest. Verification is not truncated.
These remote filesystem paths are not browser download links. New approvals may
include the automatic core-file policy described below; old approvals do not.

`download` now provides a single-file core artifact delivery path. It requires the
same conversation/project, a completed approved task, and an exact phase/name in
its saved manifest. The complete report is freshly verified before transfer and
again after a new transfer. InspireSkill `notebook scp --download` uses the frozen
account and Notebook; unsupported or unavailable transport never starts a new
Notebook or switches accounts. A regular-file/size probe runs first, and local
bytes are checked against the independently retained SHA256 before exclusive
publication under `inspire-results/<task>/<phase>/`. Files larger than 128 MiB
are rejected before/after copying; this is not a filesystem quota against a file
growing during SCP. Changed local files are never silently overwritten. Temporary
partial copies are removed on failure; scientific task state is not changed.
The platform's existing file route can serve the verified copy. This single-file
action alone does not prove complete delivery; independent refinement downloads
are still unfinished.

Single-file `download` now also attempts native Result registration after local
verification. `publication.ts` rechecks saved core evidence and local bytes, uses
the existing local `/file/artifact` endpoint, then hashes the exact registered
version through `/file/artifact-store/:id/raw`. Its SQLite journal reserves a save
before sending it; an uncertain reply is reconciled against existing Results,
never blindly resubmitted. Trashed records are not silently restored. A successful
receipt stores artifact/version IDs, SHA256 and byte size; an unavailable save
does not convert a verified local copy into a claimed Result.

Progress metadata includes these durable records, with a separate `inspire_results`
field so an already-running older observer cannot erase them when refreshing only
the stage snapshot. The progress card opens the native file/history view and shows
the verified version identity. These are saved-version records, not a claim that
the current remote files still exist or the entire result set has been delivered.
Independent refinement Result publication remains unfinished. No new scientific
runtime or remote release is implied.

### Automatic complete core-file delivery

New `create` proposals include `delivery: all-core-artifacts-v1` in the immutable
plan and native approval metadata: at most 128 MiB per file, 1 GiB total file
contents, and 100,000 files. These are delivery content caps, not hard filesystem
quotas; native Results creates another copy and temporary files also occupy disk.
An old plan without this field is never upgraded implicitly. Changing any bound
requires a new approval, not `resume_delivery`.

After native approval `host.ts` ensures a separate `delivery-daemon.ts` alongside
the compute observer. Its `deliverCompleted` cycle selects only completed tasks
with approved policies. `delivery-queue.ts` binds the full four-stage inventory,
not the report API's 50-file preview, persists its digest and progress in SQLite,
and uses a renewable lease so another observer cannot concurrently deliver the
same task. One file is transferred and natively published per cycle; no model call
chooses the next file. Reopening reads saved receipts and continues without
resubmitting computation or creating another saved version.

All entries must have matching native publication receipts and the core report
must pass a final fresh verification before `COMPLETE`. A size/count limit stops
delivery without truncating the inventory or changing the scientific task state.
Transport/service errors pause with a read/transfer retry delay; integrity errors,
explicit abort and unresolved save reservations require inspection. Same-session
`resume_delivery` only resumes this original policy and never clears the native
save journal or resets a compute stage. Previously trashed files are not restored.

The task card has separate computation and delivery status, file counts and byte
counts. It displays the first 100 saved-file links and the total, with the full
list available in Results. Metadata persists across refresh. Completion is a
historical verification, not a promise that the user never later removes files.

Local native-API integration covers 55 files, per-file SQLite reopen, concurrent
observers, a lost successful save response, corrupted transfer, and count/byte
limits. This is not real Inspire end-to-end acceptance. Every transferred file
currently rechecks the entire remote core chain; large-scale performance needs
measurement/optimization. Failed core tasks and independent refinement artifacts
are not automatically transferred by this completed-core policy.

Deployment compatibility: the old compute observer has a strict plan schema and
cannot read new delivery fields. Before activating new proposals, hand off that
idle observer to the new source only after verifying no active jobs/approvals;
do not restart the frontend/backend services or interrupt an active task.

The observer now publishes approved task progress to the originating tool card,
including terminal failure. Binding requires the same session, task ID and plan
digest. Original call output, user/model messages and approval receipts are not
rewritten. Delivery failures back off independently of execution; persisted
fingerprints suppress unchanged updates across observer replacement. The shared
UI displays stage counts (unknown stays unknown), errors and the last sync time.
Find this card under the originating message's “显示步骤”. Failed/cancelled tasks'
unstarted stages explicitly say they were not executed, rather than implying
they will run. Page refresh preserves the delivered metadata. This is a progress
snapshot, not a current heartbeat or a substitute for report file verification.
New platform proposals bind the original triggering message in the host, not a
model-written summary. Synthetic/ignored text and attachment expansion are not
treated as user instructions. Missing or oversized text requires clarification;
it is never silently truncated. `origin` is optional only to keep historical
immutable plans readable; old plans/receipts are not rewritten during migration.
This binding proves provenance, not that every extracted scientific choice is
correct: earlier choices and proposed budgets still require the native plan review.
`wake` only starts the observer: it cannot approve, reset, retry or create jobs.
`cancel` stops this task's pending/running resource and waits for terminal evidence.

`select_refinement` is available only for a scientifically completed core task.
Every policy field is explicit: absolute cap (≤100), fraction (≤25% of jointly
successful scored candidates), disagreement and leading-rank thresholds, and
separate review/consensus/diversity quotas. The fractional cap rounds down; tiny
pools may yield zero. Failed/missing/nonfinite scores stay in the audit denominator
and are not imputed. Tied scores use average ranks and deterministic identity ties.
No unused channel quota is refilled. This is an uncalibrated review heuristic,
not an uncertainty probability, binding affinity or reactive-geometry assessment.

Selection rehashes the completed report/parents and current implementation, writes
an explicit subset table and reasons under `tasks/<id>/selections/<selection_id>/`,
and binds its policy/inputs/implementation to an immutable manifest. The updated
worker source returns counts, hashes, artifact paths and the bounded selected
candidate IDs/rank reasons needed for informed approval, not SMILES, molecular
tables, raw engine scores or the full core audit. Existing selection artifacts are
verified, not overwritten. This proposal is not authorization to execute refinement.

The new read-only `read-selection` worker action rehashes source assets and the
entire core artifact chain, requires the exact saved selection/manifest digests,
checks the complete two-file inventory, recomputes the selection from core scores,
and compares the selected Parquet table against the exact core rows/columns.
Reading parses the same selection bytes that were hashed. A changed reason or
molecular value fails even if somebody updates its manifest checksum. Reads
never repair/recreate a missing artifact or issue compute. `selection-evidence.ts`
also compares returned IDs, reasons, paths, hashes and denominators with the
refinement plan after a fresh core report verification. `proposeVerifiedRefinement`
composes this check before and after native approval, plus a required separate
engine/resource/license preflight supplied by the runtime adapter.
The composition now also requires `preflightRefinementInterface`: the registered
entry must expose all exact-input options and its guard call/helper; their hashes
must match the independent plan's assets, and the live quota price must still
equal the proposed rate. The worker's `refinement-interface` action only reads
source and requires an explicit release ID and manifest checksum. It verifies the
entire release before inspecting its scientific entry point. The independent plan
freezes both code identities, and the native approval metadata displays them.
No old worker or scientific runner is overwritten.

On 2026-09-11 source release
`d99a2ae67194ed9e198d4d00b0958a2a5aa303abb162ab2622bc89aa356def99` was
installed under the remote executor's `releases/` directory through InspireSkill.
Its 171-file inventory and both exact-input interfaces passed read-only checks on
the existing CPU Notebook. It contains code/configuration, not data, weights or
license files. Old entry hashes were checked unchanged. This installation did not
switch the platform plugin, observer, configuration or existing task runtime.
The old frozen worker cannot be assumed to support `read-selection` or the new
interface action. No complete real core run has exercised this boundary yet.

Subsequent local source changes add an explicit CovDock input-score source.
The deployed `d99a2ae...` release predates that change: its CovDock wrapper assumes
AF3 inputs and must NOT be used directly on a core selection. The updated host
interface gate rejects workers that cannot report explicit `core` score-source
support. A new source release is required; the installed old version is retained.

The scientific project's `run_af3_registered_batch.py` and
`run_covdock_lo_registered.py` now accept a complete exact-input contract:
`--expected-input-count`, `--expected-input-sha256`, and
`--expected-identity-sha256`. All three must be supplied together. They verify the
actual table before engine work; AF3 also verifies prepared molecular identities.
AF3 `--top-n` is post-inference retention, not a computation cap. A future platform
refinement adapter MUST derive these values from its independently approved
selection manifest. The old standalone no-contract mode is preserved, is not
evidence of platform authorization, and must not be used for that adapter.
The updated CovDock exact-input mode also requires `--input-score-source`.
`core` preserves generic upstream scores under `core_input_*`; it never fabricates
AF3 scores, ranks or SDF properties. Explicit `af3` and legacy standalone callers
retain the historical AF3-input path. Mixed/colliding provenance fields fail.
These scientific source changes are not resource approval. An earlier exact-input
version is available in an independent remote release; newer core-score provenance
and artifact-snapshot changes still need a new immutable release. The local
coordinator and CLI transport are not yet connected to live refinement execution.

The new `refinement-approval.ts` implements the separate host approval boundary
in source, with tests against the actual native permission service and SQLite.
Each plan freezes one engine (AF3 or CovDock), the exact displayed candidate IDs
and review/control reasons, selected input file/identity/manifest hashes, original
request provenance, engine parameters, assets, resource specification, time and
an additional points cap. It requires verified core evidence from the same
conversation; core approvals cannot authorize refinement. Persisting a draft and
opening its card occur in one function. Rejection, cancellation, interruption,
expiry or changed inputs do not produce a receipt. Before and after the permission
wait, a mandatory runtime-provided verifier must recheck current files, selection
reasons, assets and live resources/license readiness. It must run again at dispatch.

This boundary deliberately remains unregistered until the engine/resource verifier
and dispatch adapter are connected. The selection verifier now has a production
implementation in source, but runtime preflight has no default. Tests use actual
local Parquet/files and do not establish remote readiness. The shared
approval renderer can display the entire bounded candidate list, selection reasons,
hashes and parameters, but there is no live refinement card generated by this code
yet. An engine needs its own approval; the AF3 receipt never implies CovDock approval.

The independent approval path now closes its unapproved draft on native rejection,
interruption, an expired reply, presentation failure or failed post-approval
revalidation. Closing is persisted in SQLite with the exact plan/session/project
identity and an event reason. Late replies cannot revive it. Existing approved
executions are never revoked by this cleanup; a corrupt plan or an unexpected
execution on an unapproved draft requires review instead of a claimed cancellation.
The host must forward its abort signal to the native permission service; the source
host does so, and the actual native-service tests exercise withdrawal without a
synthetic reply. These source changes are not evidence of a deployed refinement
tool or a successful remote scientific run.

The independent plan now also freezes the wrapper interpreter and engine-specific
runtime file roles. AF3 includes its protein/template JSON, runner, interpreter,
model/database paths, chain mapping, search CPUs and seed; CovDock includes the
receptor, software and license paths, explicit core-score provenance and residue
mapping. Referenced execution files must have frozen hashes. Directory paths are
not evidence of complete models/databases or valid licenses: runtime preflight is
still mandatory and unimplemented. Approval UI exposes the runtime asset roles.

The command builder preserves the exact selected Parquet and all three identity
guards; retention counts do not substitute for input limits. It derives a fresh
task-specific output directory, does not request resume/reuse/remote host fan-out,
and uses the approved release's activation after clearing inherited Python paths.
It does not create outputs, install dependencies, run engines or submit workloads.
The CLI dispatch/observation layer is implemented below, but real remote staging,
full runtime/license preflight and scientific output verification remain required.

The generated refinement command now embeds `execution_bootstrap.py` as trusted
host text, instead of sourcing the shared activation immediately. Isolated system
Python checks the exact staged plan bytes, verifies the release checker before
executing it, revalidates the release, hashes every frozen runtime asset and the
selected input, and checks the execution deadline after scheduler waiting. Only
then does it source activation and exec the host-constructed argv. Incoming shell
initialization/Python injection variables are removed, and approved thread counts
are restored after activation. This does not verify model/database directories
that are not in the asset map, GPU compatibility, license entitlement/checkout or
scientific acceptance; those independent preflight gates remain mandatory.

Startup guard text is embedded in the persisted command/bundle, not read from a
mutable remote helper file. Existing execution command digests are not migrated
or silently replaced. The source change has not been deployed and does not grant
permission to start a job. Its actual isolated-process tests use deliberately
harmless local wrapper fixtures for both engine paths, never AF3 or CovDock.

`proposeVerifiedRefinement` now creates an independent WAITING execution record
after native approval and all mandatory checks succeed. A stored core approval or
an unapproved refinement draft cannot create this execution. Its command snapshot,
digest, reserved name, handle, events and outcome persist in `refinement_runs`.
The new `RefinementRunner` stages/preflights before a transaction reserves the one
submission. An uncertain create reply is observed by the same name, never replayed.
Every state write checks the lease owner/expiry, including after asynchronous
calls; a replaced observer cannot write a late response over a newer observer.
The historical approval receipt and core task are not rewritten.

Expired/unsubmitted work cancels locally. Submitted cancellation waits for an
authoritative terminal observation, not a successful stop request; a missing job
query remains unresolved. Invalid output or a permanent preflight rejection pauses
for review and cannot automatically become a new compute attempt. Observations
continue for already reserved jobs without clearing a review. Success requires a
reconciled handle, fresh transport evidence, exact input identities/denominators,
the selected code/engine, coverage, individual scores, distinct structure artifacts
and explicit failure reasons. CovDock structures use a file plus audited zero-based
`structure_index`; two candidates may share a Maestro file but not the same entry.
AF3 uses a standalone CIF with `structure_index: null`. This state machine never dispatches the other
refinement engine or expands the selected candidates.

`refinement-inspire.ts` now provides the actual InspireSkill CLI compute transport:
explicit single-node Job/HPC creation, Live price/dry-run preflight, exact-name
observation with project namespace validation, and cancellation after fresh
identity checks. HPC uses `srun` and requires `Steps: 1/1` before verification.
A separate `refinement_dispatch` row persists the preflight owner/digests and the
single CLI attempt before network I/O. Even a second transport using a reopened
database cannot replay an uncertain create. A changed connection or price, missing
preflight, stale lease, or missing independent approval prevents submission.

The transport requires real staging, full runtime/license checks and scientific
verification dependencies; there are no no-op defaults. Those dependencies and
progress delivery are not fully wired, so the live daemon deliberately does not
instantiate it. Its CLI tests launch a clearly labelled local simulator, not the
actual Inspire binary, and never start a scientific engine. CLI Help was checked
against the installed InspireSkill version for the implemented commands. Tests
do not create tables in the real platform state
database and cannot be used as proof of a real completed refinement run.

`refinement_stage.py` now implements the shared-file preparation component. Its
bounded `freeze`/`read` CLI verifies the parent report chain, actual selected
Parquet rows and full immutable release before retaining the exact plan at
`tasks/<parent>/refinements/<id>/plan.json`. It uses a file lock and exclusive atomic
publication; repeated identical staging is allowed, but changed plans are never
overwritten or repaired. It does not create the engine output directory or invoke
an engine, and its receipt explicitly denies runtime verification/resource approval.
The host still must validate the complete plan and independent native receipt.

New independent plans may explicitly freeze
`result_contract: "screening-refinement-outcome-v2"`. The native approval card
discloses the counting semantics. In v2, `SUCCESS`/`FAILED` and the approved
coverage threshold describe engine execution. Every successful engine row then
has a separate `PASSED` or `REJECTED` quality verdict, with witnessed artifact
references and a nonempty rejection reason. Failed engine rows are
`NOT_EVALUATED`, not scientific rejections. `quality_counts` must match every row;
scores and structures remain attached even when all candidates are rejected.
The host rejects changed engine statuses or artifacts outside fresh readback.
Zero quality passes is a valid negative screening outcome only after the real
independent validator completes its checks; it is not a licence to omit checks.

Such a completed run emits a `quality_evaluated` event with separate counts and
`hits_confirmed: false`: satisfying computational quality criteria does not prove
biological activity. This changes neither spending limits nor retry policy.
Plans without the explicit field keep the v1 contract; old receipts/results are
not converted or retroactively assigned candidate verdicts. A contract mismatch
is rejected even when task IDs and other hashes match. Shared-file staging also
retains this exact field and never replaces an already frozen plan.

These are source-level acceptance/persistence interfaces, not implemented
scientific decision rules. Production runtime/license checks, the independently
validated quality policy and measurements, refinement tool/daemon wiring and
refinement delivery are still required. No production validator is supplied by
the contract tests, which use expressly synthetic verdicts and a local CLI fixture.

New source packages include the staging and CPU diagnostic helpers. Older release
inventories remain valid without them; consumers of the new staging/output contract
must freeze the new helper hashes and choose a new release. The existing 171-file
local copy of release `d99a2ae67194ed9e198d4d00b0958a2a5aa303abb162ab2622bc89aa356def99`
was reverified without edits. No remote release was replaced. Deployment must use
the updated, independently verified installer (an old allowlist need not accept new
helpers). `RefinementPreparation` now connects the exact-plan Notebook preparation
call to the source transport, but the production daemon still does not instantiate
that transport because mandatory runtime/license and scientific acceptance are
unfinished.

Both `stage_bootstrap.py` and `execution_bootstrap.py` are build-time text imports.
Preparation must not read a neighbouring Python source file at runtime: that made
a relocated bundle fail with ENOENT, and a sidecar could otherwise replace its
trusted verifier without changing the bundle. A minified, independently imported
temporary bundle now produces the same freeze/read/output arguments as source
without any sidecar, and ignores a subsequently added same-name file. This test
builds commands only; it neither stages remote files nor runs scientific engines.

Registered scientific wrapper sources now call `covscreen.refinement_output` for
exact-contract runs. It writes `refinement_artifacts.json` exclusively, binding
candidate identities, scores, structure locations and fresh file hashes. The
read-back API requires a separately retained manifest digest and rechecks the
actual Parquet, structures, confidence/audit files and nested gate artifacts.
Legacy uncontracted calls do not create this snapshot. This is artifact integrity
evidence, explicitly not scientific acceptance: the collector always returns
`scientific_quality_verified: false` and cannot produce a final successful outcome.
The real transport must retain the digest, verify scientific results and convert
audited structure references before calling the host completion validator. These
source changes have not been deployed into an immutable remote release.

The daemon survives page closure, not a powered-off/sleeping computer. Remote jobs
continue independently, but next-stage scheduling requires the local host awake.
There is not yet a login service, always-on server deployment or completed-task
notification channel. Do not promise 24/7 orchestration.

Submission is reserved in SQLite before network I/O. An ambiguous reply is
reconciled by the exact reserved name; never call create again. Missing observation
does not prove termination. Permanent preflight/evidence problems enter
NEEDS_REVIEW; transient transport errors preserve handles. Failed or changed plans
require explicit review/new approval, not an automatic retry.

## Verification and deployment

`runtime_probe.py` provides a bounded CPU-only dependency diagnostic. It checks
the requested interpreter and exact import inventory, and requires both platform
modules to come from the selected release. AF3 native imports are checked in their
separate Python environment. Model/database files receive presence/size checks,
not integrity claims. CovDock launchers are inspected, never executed; license
metadata does not establish license entitlement. The frozen CovDock asset list
includes `covalent_docking` as well as `run` and `ligprep`.

This diagnostic always reports `ready: false` and grants no resource approval.
It is not installed as a live worker action or used instead of the required full
engine preflight. It must receive trusted host-resolved paths and independently
verified release sources, not arbitrary model-provided activation scripts.
`runtime_probe_test.py` exercises real CPU subprocesses, malformed/partial output,
wrong interpreter identity, timeout, and license-launcher non-execution.

The source-side refinement transport now fixes file preparation in code. It
requires the exact approved account/Notebook, valid independent receipt and live
execution lease, freezes the selected plan, then independently reads it again.
The retained receipt binds the canonical plan file checksum, code release,
selection, input count and output path. Reopening the local database reuses this
receipt and performs read-only verification; it does not overwrite a remote plan.
Changed evidence blocks the downstream runtime/outcome validators. Preparation
never means runtime or license readiness and cannot submit a job. The bootstrap
rejects symlinked release ancestors and modified interpreters before activation,
then reapplies CPU-only/thread limits after activation.

These additions are local source only: they are not registered in the live
observer, and the old deployed release must not be overwritten to add them.
`stage_bootstrap_test.py` invokes the actual isolated Python bootstrap, release
checker and staging worker against on-disk non-scientific fixtures. The host
transport tests exercise subprocess arguments and SQLite reopen with an explicitly
labelled CLI simulator; they do not establish live Notebook or engine readiness.

Completed refinement readback uses the same isolated bootstrap with a fixed
`outputs` action and a frozen `refinement_readback.py` role. It receives no model
path or replacement plan. The first full verification returns a manifest checksum;
the host persists it before a second read so a lost response cannot allow result
replacement on the next tick. Later reads supply that exact witness. CovDock
container entries remain distinct by zero-based index. Scientific acceptance is
explicitly false in file evidence: mandatory independent quality checks must run,
and their accepted outcome must preserve the verified scores, structures and
artifact hashes. Quality rejection of a computed candidate may remain an explicit
failed row; it must not be silently converted to an accepted candidate.

`refinement_readback_test.py` runs the released reader, bootstrap and real Parquet
collector against non-scientific structures. It verifies read-only behavior,
missing/changed files and stale witnesses. Backend tests exercise witness
persistence, interruptions, cancellation and output changes during a separate
quality callback. Synthetic quality callbacks in tests are not production
validators. No actual AF3/CovDock job is proven by these tests.

AF3 readback now recomputes confidence metrics from the witnessed original JSON
bytes using the frozen runtime's protein/ligand chain roles. It compares mPAE,
the published raw score, ranking score, ipTM/pTM, directional PAE, mean ligand
pLDDT and the reported clash indicator against the actual score table. A rewritten
table is rejected even when its ordinary file hashes are regenerated. Missing
metrics are not inferred, and engine failures stay explicit. This closes a score
provenance gap; it still does not establish structure quality or binding activity.
The reader is source-only until a new immutable release is deployed.

The local scientific source now rejects malformed/nonfinite AF3 confidence
arrays, invalid residue-index mappings, ambiguous selected models or ligand
residues, and mismatched ligand heavy-atom names/elements before recording a
parsed success. Confidence and geometry use the same declared protein chain.
Failed input preparation remains a failed row even if matching old prediction
files are present. These are parser prerequisites, not a complete scientific
quality policy: they do not prove bond topology, stereochemistry, clash freedom,
binding affinity or model calibration. A new immutable source release is required
to deploy these changes; existing remote releases remain unchanged.

The local CovDock gate is now `covdock-lo-pose-gate-v2.1`. Its read-only
`assess_covdock_candidates` function is shared with output readback to recompute
stored row-level decisions. Nonfinite scores cannot be successful or win a pose
sort; missing physical measurements report `UNKNOWN`, not `PASS`. Missing
AF3/CovDock consensus remains distinct from missing physical evidence. Readback
checks physical measurements against the retained structure audit and rejects
rewritten grade tables even when ordinary manifest hashes have been updated.
This authenticates consistency with the audited files and recorded policy, not
fresh licensed structure inspection or approval of that policy. Complete
scientific acceptance and runtime/license validation are still required.

### Failed core diagnostic evidence (source-only)

`report` on a FAILED task now calls `readFailureReport` rather than treating it as
a completed report. It requires the unchanged approved task, a reserved failed
stage, verified upstream manifests and untouched downstream stages. The frozen
worker's `read-failure` action reads only `error.json`, `started.json` and optional
`environment.json`, `engine.log`, `failures.csv`, checking bounded regular files,
the attempt/plan identity, absence of a success manifest and a nonblocking shared
process lock. It includes checksums and sizes, not log contents or unvalidated
poses. This is diagnostic provenance, not scientific acceptance or proof from a
lock alone that a compute resource terminated; the host failed state is required.

The first validated witness is saved in SQLite before a second remote read.
Timeout, reopen and later reads must use the same witness; altered or additional
diagnostic files cannot silently replace it. Upstream manifests are reverified
before and after. `failure_evidence_ready` can become true, but `report_ready`
always remains false and task/approval/stage state is unchanged. No missing error
file is fabricated (for example, a hard-killed worker may never have written one).

Existing frozen worker releases were not changed. This new reader therefore
requires a newly frozen worker in a future explicitly scoped task; an old worker
without this action reports unavailability. Automatic transfer/native Results
publication of failure files and the verified upstream prefix is now implemented
in source through the shared delivery queue. New plans explicitly include
`delivery.failure: diagnostics-and-verified-prefix-v1`; the approval card explains
the diagnostic scope, that logs contain runtime information, and that the same
file/byte caps apply. Old immutable plans without the flag are unchanged and do
not gain automatic failure delivery.

The queue acquires its lease before evidence reads and persists an unknown
inventory (`null`) until the full witness is available. Missing evidence pauses
with an unknown total, never a zero-file "complete" claim. Retries use the same
retained witness. Fresh upstream/diagnostic reads bracket file transfers and final
completion; every delivered file goes through native Results version-byte
readback. Uncertain saves reconcile without reposting, and caps pause without
truncation. The completed inventory is also checked against local immutable
evidence on subsequent ticks; malformed or changed inventories require review.

`delivery.scope=failure_evidence` labels completion separately from task state:
the task stays FAILED, downstream phases stay untouched, and `report_ready` stays
false. Result receipts label `failure_diagnostic` or `verified_prefix`; the progress
card preserves these labels across refresh. Unknown/partial molecular outputs
are not in this delivery inventory. Independent refinement delivery and real
Inspire end-to-end verification remain unfinished. No source changes in this
section have been registered or deployed as evidence of a live run.

Run from `/path/to/workspace/openscience-custom/backend/cli`:

```sh
bun test integrations/inspire-screening/executor/executor.test.ts integrations/inspire-screening/executor/request.test.ts integrations/inspire-screening/executor/progress.test.ts integrations/inspire-screening/executor/refinement-approval.test.ts integrations/inspire-screening/core.test.ts integrations/inspire-screening/workflow.test.ts test/permission/next.test.ts
# Set REFINEMENT_TEST_PYTHON to a pandas/pyarrow/RDKit Python to also exercise
# generated argv against actual scientific entry-point input guards (no compute).
bun x tsc --noEmit -p integrations/inspire-screening/executor/tsconfig.json
python3 integrations/inspire-screening/executor/worker_test.py
python3 integrations/inspire-screening/executor/refinement_test.py
# In a Python environment with pandas, pyarrow and PyYAML:
python3 integrations/inspire-screening/executor/selection_files_test.py
python3 integrations/inspire-screening/executor/engine_interface_test.py
python3 integrations/inspire-screening/executor/code_release_test.py
python3 integrations/inspire-screening/executor/runtime_probe_test.py
python3 integrations/inspire-screening/executor/refinement_stage_test.py
python3 integrations/inspire-screening/executor/stage_bootstrap_test.py
python3 integrations/inspire-screening/executor/refinement_readback_test.py
bun --no-env-file integrations/inspire-screening/executor/prepare-acceptance.ts
bun --no-env-file integrations/inspire-screening/executor/register.ts --reload
```

The last two commands use external/localhost access. Preflight creates a test-only
unapproved draft bound to `executor-preflight-acceptance`; it cannot be approved
from a different real conversation. A genuine chat acceptance must create its own
new draft and show its own native approval card. Reload refuses busy sessions and
does not restart the app/server. No compute is submitted by these commands.

Registration builds a content-addressed `.runtime-<sha256>.mjs` beside the executor
sources, bundles local executor modules, and leaves the unchanged legacy plugin
external to preserve its path base. This bypasses stale ESM imports without a
server restart. Previously generated bundles are retained for rollback; active
runtime bundles are not disposable caches. The registration checks the served
tool description, not just whether its ID exists.

`executor.test.ts` uses actual child processes and SQLite, but the subprocess is
explicitly a non-scientific fixture. `worker_test.py` tests actual disk/hash chains,
not a GPU engine. Passing these tests or dry-runs is not scientific acceptance.
`progress.test.ts` uses the actual internal platform HTTP routes, session part
storage and SQLite reopen; its task state transitions are non-scientific fixtures.
On 2026-09-11 the existing real failed task's progress was also delivered to its
live card and verified after a browser refresh. The observer alone was gracefully
replaced to load the publisher; the platform server was not restarted. No new
compute or approval was introduced. Active remote-job progress across interruption
still needs separate real-run validation.

Approval presentation lives in the shared UI's `inspire-approval.ts` with tests
for per-stage budgets and explicit unknown fields (unknown is never free).

## Still required before claiming completion

Core workers installed in `releases/<release-id>/worker.py` now select that
verified release's `science/` source/configuration. Shared database, receptor,
reference ligand and external engine weights remain separate assets; the whole
source manifest and local configuration join the frozen asset set. Legacy worker
files are not rewritten. After explicit user upload authorization, release
`14c9113cf77373ad901db5ee52cc470d8e347ff86b850269429d4a9f160b1e83`
was installed and its 175 source/config files verified remotely. The new-task
configuration now selects its worker, science directory and activation script.
The actual host settings/transport path returned a matching 216-asset snapshot;
this is read-only asset verification, not a successful screening run. Existing
task plans and their worker paths remain unchanged. See the workspace report
`reports/platform-core-runtime-cutover-2026-09-12.md` for verification boundaries.

The 2026-09-12 diagnostic update is now hot-loaded through the versioned plugin
registration path, without restarting the platform or observer. `status/list`
include the same persisted `preparation_diagnostic` snapshot used by the native
card; these reads never invoke remote diagnostics or resume computation. The
real stopped task's diagnostic card survived an actual browser refresh. This
does not deploy local scientific-engine changes or reload the existing observer.
See `reports/platform-diagnostic-loading-2026-09-12.md` at the workspace root for
the scoped live evidence and remaining deployment limitations.

1. Keep regression coverage for real platform natural-language create → native approval.
   The one-request handoff and page-reopen check passed on 2026-09-11; this is
   not proof of missing-choice behavior, every model, or complete screening.
2. Actual bounded Inspire HPC → GPU CarsiDock → GPU RTMScore → report jobs,
   with terminal status, Slurm step, environments, files and molecular identities.
3. Recovery evidence against a real job across observer/page interruption.
4. Uncertainty method and selection contract, small-subset AF3/CovDock adapters,
   licenses/runtime/resource checks, approval and actual refinement validation.
5. Real-job verification of automatic core delivery, failure/refinement delivery, and reliable
   operational hosting for the desired level of unattended operation. The progress
   card is implemented; live-job interruption recovery and 24/7 operation are not
   established by the terminal-task refresh check or dry-run success.
