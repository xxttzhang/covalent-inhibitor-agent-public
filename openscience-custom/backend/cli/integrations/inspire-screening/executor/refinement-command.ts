import { canonical, digest, type Task } from "./model"
import { createHash } from "node:crypto"
import bootstrap from "./execution_bootstrap.py" with { type: "text" }
import admission from "./engine_startup.py" with { type: "text" }
import { refinementPlan, type RefinementPlan } from "./refinement-approval"
import { refinementPaths } from "./refinement-runtime"
import { quote } from "./inspire"

// Pure command construction. This never submits compute, creates files, grants
// approval or proves readiness. The future dispatcher must verify the independent
// receipt, current selection/source/runtime and resource reservation before use.
export function refinementCommand(value: RefinementPlan, task: Task) {
  const plan = refinementPlan.parse(value)
  if (plan.parent_id !== task.id || plan.parent_digest !== task.digest || digest(task.plan) !== task.digest ||
    plan.directory !== task.plan.directory || plan.session !== task.plan.session) throw new Error("Refinement command source identity differs")
  if (plan.input.path !== `${task.plan.runtime.root}/tasks/${task.id}/selections/${plan.selection_id}/candidates.parquet`) {
    throw new Error("Refinement command requires the frozen selection, not another candidate file")
  }
  const paths = refinementPaths(plan.runtime, task.plan.runtime.root, plan.code.release_id)
  if (paths.files.some((file) => !plan.assets[file])) throw new Error("Runtime file roles missing from frozen assets")
  const output = `${task.plan.runtime.root}/tasks/${task.id}/refinements/${plan.id}/${plan.engine.name}`
  const count = String(plan.input.candidates.length)
  const common = [plan.runtime.python, "-B", paths.entry, "--candidate-table", plan.input.path, "--output-dir", output,
    "--expected-input-count", count, "--expected-input-sha256", plan.input.sha256,
    "--expected-identity-sha256", plan.input.identity_sha256]
  const specific = (() => {
    if (plan.engine.name === "af3" && plan.runtime.name === "af3") return [
      "--source-data-json", plan.runtime.source_data_json, "--target-config", paths.target,
      "--runner", plan.runtime.runner, "--af3-python", plan.runtime.af3_python,
      "--model-dir", plan.runtime.model_dir, "--database-dir", plan.runtime.database_dir,
      "--protein-chain", plan.runtime.protein_chain, "--ligand-chain", plan.runtime.ligand_chain,
      "--cysteine-sequence-index", String(plan.runtime.cysteine_sequence_index), "--target-id", task.plan.target,
      "--source-residue-number", String(plan.runtime.source_residue_number), "--nucleophilic-atom", "SG",
      "--top-n", count, "--gpu", "0", "--num-recycles", String(plan.engine.num_recycles),
      "--num-seeds", String(plan.engine.num_seeds), "--num-diffusion-samples", String(plan.engine.num_diffusion_samples),
      "--cpu-per-search", String(plan.engine.cpu_per_search), "--random-seed", String(plan.engine.random_seed),
      "--min-coverage", String(plan.engine.min_coverage), ...(plan.engine.run_data_pipeline ? ["--run-data-pipeline"] : []),
    ]
    if (plan.engine.name === "covdock" && plan.runtime.name === "covdock") return [
      "--input-score-source", plan.engine.input_score_source, "--receptor", plan.runtime.receptor,
      "--attachment-residue", plan.runtime.attachment_residue, "--nucleophilic-atom", "SG",
      "--reaction-type", plan.engine.reaction, "--schrodinger-root", plan.runtime.schrodinger_root,
      "--license-file", plan.runtime.license_file, "--runtime-lib-dir", plan.runtime.runtime_lib_dir,
      "--njobs", "1", "--host", "localhost", "--final-n", count, "--min-success-rate", String(plan.engine.min_coverage),
      ...(plan.quality_policy?.version === "refinement-structural-qc-v2-provisional"
        ? ["--gate-algorithm-version", "covdock-lo-pose-gate-v3.0-provisional"] : []),
    ]
    throw new Error("Runtime and engine mismatch")
  })()
  const argv = [...common, ...specific]
  const release = `${task.plan.runtime.root}/releases/${plan.code.release_id}`
  const packet = { root: task.plan.runtime.root, release: plan.code.release_id, manifest: plan.code.manifest_sha256,
    checker_sha256: plan.assets[`${release}/code_release.py`], parent: plan.parent_id, task: plan.id, argv,
    plan_sha256: createHash("sha256").update(JSON.stringify(canonical({ plan, digest: digest(plan) })) + "\n").digest("hex") }
  const encoded = Buffer.from(JSON.stringify(packet)).toString("base64")
  if (encoded.length > 2 * 1024 * 1024) throw new Error("Refinement startup packet exceeds bounded size")
  // Trusted host source is embedded in the persisted command. Isolated system
  // Python rechecks the release/plan/runtime bytes AFTER scheduler waiting and
  // BEFORE loading activation or the wrapper interpreter from shared storage.
  const shell = ["env", "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
    ...(plan.environment_contract ? ["-u", "LD_AUDIT"] : []),
    "-u", "BASH_ENV", "-u", "ENV", "python3", "-I", "-B", "-c", (plan.runtime_admission ? admission + "\n" : "") + bootstrap, encoded].map(quote).join(" ")
  return { argv, shell, output, input_count: plan.input.candidates.length, plan_digest: digest(plan),
    grants_resource_approval: false as const, runtime_verified: false as const }
}
