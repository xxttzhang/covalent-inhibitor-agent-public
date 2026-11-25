import z from "zod"

// File roles must be explicit in the immutable plan, not inferred from a model's
// shell command or whichever mutable local configuration happens to be loaded.
const file = z.string().startsWith("/inspire/").refine((value) =>
  !/[\\\x00-\x1f\x7f]/.test(value) && value.slice(1).split("/").every((part) => part && part !== "." && part !== ".."),
"Runtime assets require canonical shared-disk paths")
export const refinementRuntime = z.discriminatedUnion("name", [
  z.object({ name: z.literal("af3"), python: file, runner: file, af3_python: file,
    source_data_json: file, model_dir: file, database_dir: file,
    protein_chain: z.literal("A"), ligand_chain: z.literal("L"),
    cysteine_sequence_index: z.number().int().positive(), source_residue_number: z.literal(797) }).strict(),
  z.object({ name: z.literal("covdock"), python: file, receptor: file, schrodinger_root: file,
    // The raw 1M17 receptor uses 773; the registered prepared Maestro receptor
    // uses canonical 797 (its 773 is HIS). Keep the explicit approved value,
    // never copy CarsiDock's number across receptors. The runtime preflight must
    // independently witness a unique CYS/SG in the hash-bound receptor.
    license_file: file, runtime_lib_dir: file, attachment_residue: z.enum(["A:773", "A:797"]) }).strict(),
])

export function refinementPaths(value: z.infer<typeof refinementRuntime>, root: string, release: string) {
  const runtime = refinementRuntime.parse(value)
  const version = `${file.parse(root)}/releases/${z.string().regex(/^[a-f0-9]{64}$/).parse(release)}`
  const directory = `${version}/science`
  const entry = `${directory}/scripts/${runtime.name === "af3" ? "run_af3_registered_batch.py" : "run_covdock_lo_registered.py"}`
  const activation = `${directory}/environment/activate_carsidock.sh`
  const target = `${directory}/configs/covindb/EGFR_C797_mvp_v1.yaml`
  const files = [entry, activation, target, `${version}/refinement_stage.py`, `${version}/refinement_readback.py`, `${version}/code_release.py`,
    `${directory}/src/covscreen/refinement_input.py`,
    `${directory}/src/covscreen/refinement_output.py`, runtime.python,
    ...(runtime.name === "af3" ? [runtime.runner, runtime.af3_python, runtime.source_data_json]
      : [runtime.receptor, runtime.license_file, `${runtime.schrodinger_root}/run`, `${runtime.schrodinger_root}/ligprep`,
        `${runtime.schrodinger_root}/covalent_docking`, `${directory}/scripts/inspect_covdock_output.py`])]
  return { directory, entry, activation, target, files }
}
