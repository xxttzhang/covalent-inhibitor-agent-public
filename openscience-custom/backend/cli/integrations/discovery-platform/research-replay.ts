import path from "node:path"
import { realpath, readFile } from "node:fs/promises"
import { z } from "zod"
import { emptyResources, researchContract, researchDigest } from "./research-loop-contract"
import { ResearchLoopStore } from "./research-loop-store"
import { captureResearchArtifact, importLibraryFirstMigrationObservation, verifyResearchObservation } from "./research-observations"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const source = {
  release: "5d6e9c48825c18668bec9ceeddf3502482197258becc4a54daed8fbcc8491910",
  report: "17f52a7c432f5febef486cb5710fabb42931c102baa79c17cec9e739b1d1aade",
  handoff: "148a6a21ba9a0c4cc5e638b6dbfcdb8ec591008f82bc52d345af646a1cc37cf0",
  selection: "2e0ae01360162f4eabb7215b16ca096617c3f3ac7563d9a8822f3731efea33a7",
  identity: "b4d414caf8a2c29a2e6872c04d0620681041d043b554dfd3bf6d9ed729822673",
  library: "83b2c5022a43c33aa98740ed30e7ccc02e92e2fc89a0aa17cd5f0bf36b411fe9",
  receptor: "5b020cd186307d657de80611793554b074f6936a3443d33f81d9b1a58004962e",
  config: "7673a3121cd63ab42283f43f64ed6e16224b8e3aefa818bfd729960606cf2bfe",
} as const

/** Host entry point: import a fixed historical source, never call models or remote executors. */
export async function importHistoricalResearchReplay(store: ResearchLoopStore, input: {
  directory: string
  project: string
  session: string
  /** Migration report directory; relocated copies must retain the pinned bytes. */
  evidenceRoot?: string
  now?: number
}) {
  const directory = await realpath(input.directory)
  const root = await realpath(input.evidenceRoot ?? path.join(workspace, "reports/layout-migration-20261012"))
  const observation = verifyResearchObservation(await importLibraryFirstMigrationObservation({
    reportPath: path.join(root, "gpu-validation.json"), artifactsDirectory: path.join(root, "gpu-final"),
    expectedReportSha256: source.report, expectedHandoffSha256: source.handoff,
    expectedReleaseId: source.release, expectedInputIdentityDigest: source.identity,
  }))
  if (observation.classification === "integrity_failure" || observation.identity.status !== "pass")
    throw Error("Historical replay source failed integrity or identity verification")
  if (observation.counts.input_records !== 20000 || observation.counts.selected !== 5 || observation.origin !== "historical_import" || observation.new_compute)
    throw Error("Historical replay scope does not match the pinned 20k / 5 migration source")
  const selection = z.object({
    source: z.object({ path: z.string(), sha256: z.literal(source.library), records_read: z.literal(20000), record_selection: z.literal("file_order_prefix") }),
    target: z.object({ config_sha256: z.literal(source.config), target_id: z.literal("EGFR"), site_id: z.literal("EGFR_C797"), warhead_class: z.literal("acrylamide"), reaction_type: z.literal("Michael Addition") }),
  }).parse(JSON.parse(await readFile(path.join(root, "gpu-final/prepare/stage2_selection_manifest.json"), "utf8")))
  const local = await Promise.all([
    captureResearchArtifact({ path: path.join(workspace, "science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb"), ref: "local-receptor", expectedSha256: source.receptor }),
    captureResearchArtifact({ path: path.join(workspace, "routes/library-first/covindb-configs/EGFR_C797_mvp_v1.yaml"), ref: "local-science-config", expectedSha256: source.config }),
  ])
  const contract = researchContract.parse({
    schema: "drug-design-research-contract-v1", directory, project: input.project, session: input.session,
    objective: "Import and replay the historical Library-first / EGFR C797 20,000-record, 5-candidate migration evidence; preserve unknown scientific checks and stop or inspect without new computation.",
    mode: "offline-replay", route: "library-first",
    target: { gene: "EGFR", taxon: 9606, uniprot: "P00533", canonical_site: "C797", pdb: "1M17", chain: "A", author_residue: 773, reactive_atom: "SG", warhead: "acrylamide", receptor_sha256: source.receptor, mapping_sha256: null },
    inputs: { library: selection.source.path, library_sha256: source.library, subset_sha256: null, source_release: source.release, science_config_sha256: source.config },
    quality: { manifest_sha256: null, required_checks: observation.required_scientific_checks, minimum_candidates: 5, conclusion: "computational-shortlist-only" },
    limits: { resources: emptyResources(), max_library_records: 20000, max_rounds: 1, max_candidates_per_round: 5,
      max_total_candidates: 5, max_gpu_concurrency: 1, max_gpu_hours_per_round: 0.25, max_decisions: 12, max_elapsed_ms: 2 * 60 * 60 * 1000 },
    allowed_adaptations: ["inspect", "stop"],
  })
  const receipt = {
    schema: "drug-design-historical-replay-source-v1", origin: "historical_import", report_root: root,
    report_sha256: source.report, handoff_sha256: source.handoff, selection_manifest_sha256: source.selection,
    historical_run_id: observation.run.id, historical_observation_id: observation.id,
    evidence_scope: {
      local_reverified: local,
      historical_remote: { library_sha256: source.library, library_path: selection.source.path, source_release: source.release,
        record_selection: selection.source.record_selection, records_read: selection.source.records_read },
      remote_revalidated_now: false, independent_raw_subset_frozen: false, mapping_audit_frozen: false,
      scientific_acceptance_manifest_frozen: false,
    },
    missing_bindings: ["target.mapping_sha256", "inputs.subset_sha256", "quality.manifest_sha256"],
    field_semantics: {
      minimum_candidates: "Historical observed count retained for replay identity only; this offline contract introduces no scientific acceptance threshold",
      max_rounds: "One historical execution is imported; offline mode prohibits compute reservations",
      max_gpu_hours_per_round: "Schema-compatible ceiling, never permission: total GPU budget is zero and offline mode rejects scientific computation",
      model_tokens: "Zero: importing this contract does not call a model; a funded new contract is required for any model execution",
    },
    new_compute: false, model_called: false,
  }
  const imported = store.db.transaction(() => {
    const matches = store.list(directory).filter(loop => loop.digest === researchDigest(contract))
    if (matches.length > 1) throw Error("Historical replay contract has ambiguous duplicate stored loops")
    const current = matches[0] ?? store.create(contract, input.now)
    store.record(current.id, "historical-source:" + source.report, receipt, input.now)
    store.observe(current.id, observation.id, observation, input.now)
    return { id: current.id, resumed: matches.length === 1 }
  }).immediate()
  const current = store.get(imported.id)
  const restored = store.evidence(current.id, "observation").map(item => verifyResearchObservation(item.payload))
  if (restored.length !== 1 || restored[0]!.id !== observation.id) throw Error("Historical replay restored unexpected observation evidence")
  return {
    schema: "drug-design-historical-replay-import-v1", loop_id: current.id, resumed: imported.resumed,
    state: current.state, state_revision: current.revision, contract_digest: current.digest, contract: current.contract,
    observation: restored[0]!, host_evidence: store.evidence(current.id, "host"), ledger: current.ledger,
    events: store.events(current.id), new_compute: false, model_called: false,
  }
}
