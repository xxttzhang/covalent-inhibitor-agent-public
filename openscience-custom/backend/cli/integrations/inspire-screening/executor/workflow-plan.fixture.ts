import { workflowScreeningIntent } from "./workflow-plan"
import { registeredTarget } from "./target"
import { requestHash } from "./request"
import type { BoundRequest } from "./request"
import type { Task } from "./model"
import { refinementPlan } from "./refinement-approval"
import { refinementWorkflow } from "./refinement-workflow"
import { refinementPaths } from "./refinement-runtime"
import type { verifySelectionProposal } from "./selection-proposal"

// Explicit software-only roles, never scientific input or compute authority.
export function fixture(directory: string) {
  const file = (name: string) => ({ path: "/inspire/workflow-fixture/" + name, sha256: "a".repeat(64) })
  const inputs = { version: "screening-research-inputs-v1" as const,
    library: { ...file("subset.csv"), records: 20000 as const, selection: "frozen-file-order-prefix" as const,
      source: file("source.csv"), manifest: file("subset.json") }, receptor: file("prepared.pdb"),
    reference: file("reference.sdf"), science_config: file("science.yaml") }
  const intent = workflowScreeningIntent.parse({ schema: "library-workflow-screening-intent-v1",
    workflow_id: "library-flow-" + "b".repeat(24), revision: 7, operation_id: "c".repeat(32),
    conditions: { target: "EGFR", site: "C797", target_parameters: registeredTarget,
      library: "CovInDB/Virtual_Screening_Library.csv", screening_mode: "research_pilot_v1", max_points: 10 },
    research_inputs: inputs,
    screening_inputs: { artifact_id: "screening-inputs-" + "e".repeat(32), manifest_sha256: "f".repeat(64) },
    scientific_review: {
      construct_and_components: "Software contract fixture, no claim about a real construct or removed components.",
      reaction_scope: "Software contract fixture; no cysteine reactivity or inhibition claim.",
      prefix_scope: "Software fixture of a file-order prefix, not random or whole-library coverage.",
    }, receptor_evidence: { artifact_id: "screening-receptor-" + "d".repeat(32),
      manifest_sha256: "d".repeat(64), receptor_sha256: inputs.receptor.sha256 } })
  const text = "Software contract fixture; no real science input or permission to submit compute"
  const cpu = { kind: "hpc" as const, workspace: "CPU资源空间", project: "fixture", group: "CPU", quota: "0,4,16",
    image: "fixture", image_type: "SOURCE_OFFICIAL" as const, priority: 4, points_per_hour: 0 }
  const environment = {
    source: { text, origin: { user_message_id: "user-fixture", assistant_message_id: "assistant-fixture",
      part_ids: ["text-fixture"], request_sha256: requestHash(text) } },
    assets: Object.fromEntries([inputs.library, inputs.library.manifest, inputs.receptor, inputs.reference, inputs.science_config,
      file("engine"), file("worker")].map(item => [item.path, item.sha256])),
    runtime: { root: "/inspire/workflow-fixture/tasks", project: "/inspire/workflow-fixture/science",
      worker: "/inspire/workflow-fixture/worker", activation: "/inspire/workflow-fixture/env.sh" },
    connection: { cli: "/nonexistent/workflow-test-only-cli", account: "fixture", notebook: "fixture", workspace: "CPU资源空间" as const },
    cpu, gpu: { ...cpu, kind: "job" as const, workspace: "分布式训练空间", group: "GPU", quota: "1,10,200", points_per_hour: 1 },
  }
  return { intent, environment, scope: { directory, sessionID: "workflow-fixture" } }
}

/** Explicit software ledger fixture, never a scientific plan or permission. */
export function refinementFixture(parent: Task, subset: ReturnType<typeof verifySelectionProposal>, source: BoundRequest) {
  const now = Date.now()
  const runtime = { name: "af3" as const, python: "/inspire/fixture/python", runner: "/inspire/fixture/af3/run.py",
    af3_python: "/inspire/fixture/af3/python", source_data_json: "/inspire/fixture/protein.json",
    model_dir: "/inspire/fixture/models", database_dir: "/inspire/fixture/databases", protein_chain: "A" as const,
    ligand_chain: "L" as const, cysteine_sequence_index: 105, source_residue_number: 797 as const }
  const code = { release_id: "7".repeat(64), manifest_sha256: "8".repeat(64) }
  const af3 = refinementPlan.parse({ version: "screening-refinement-v1", id: "refine-" + "a".repeat(24),
    result_contract: "screening-refinement-outcome-v2", request: source.text, origin: source.origin,
    directory: parent.plan.directory, session: parent.plan.session, parent_id: parent.id, parent_digest: parent.digest,
    report_digest: subset.source_manifest_digest, selection_id: subset.selection_id, selection_manifest_digest: subset.selection_manifest_digest,
    input: subset.input, code, runtime, engine: { name: "af3", num_recycles: 3, num_seeds: 1, num_diffusion_samples: 1,
      run_data_pipeline: false, cpu_per_search: 2, random_seed: 20260725, min_coverage: 0.95 },
    assets: { ...parent.plan.assets, ...Object.fromEntries(refinementPaths(runtime, parent.plan.runtime.root, code.release_id).files.map(file => [file, "a".repeat(64)])) },
    resource: parent.plan.steps[1].resource, minutes: 10, max_points: 0.2,
    created: now, approve_before: now + 60000, finish_before: now + 3600000 })
  const covdockRuntime = { name: "covdock" as const, python: "/inspire/fixture/python", receptor: "/inspire/fixture/receptor.mae",
    schrodinger_root: "/inspire/fixture/schrodinger", license_file: "/inspire/fixture/license.lic",
    runtime_lib_dir: "/inspire/fixture/libs", attachment_residue: "A:797" as const }
  const covdock = refinementPlan.parse({ ...af3, id: "refine-" + "b".repeat(24), runtime: covdockRuntime,
    engine: { name: "covdock", mode: "LO", reaction: "Michael Addition", input_score_source: "core", max_structures_per_compound: 1, min_coverage: 0.95 },
    resource: parent.plan.steps[0].resource, max_points: 0,
    assets: { ...af3.assets, ...Object.fromEntries(refinementPaths(covdockRuntime, parent.plan.runtime.root, code.release_id).files.map(file => [file, "a".repeat(64)])) } })
  return refinementWorkflow.parse({ version: "screening-refinement-workflow-v1", id: "refineflow-" + "c".repeat(24),
    stages: [af3, covdock], execution: "af3_then_covdock_same_subset", failure_policy: "stop_on_execution_failure",
    quality_policy: "retain_rejections_and_continue", max_points: af3.max_points + covdock.max_points })
}
