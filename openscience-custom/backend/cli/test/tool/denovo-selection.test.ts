import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm } from "node:fs/promises"

const python = process.env.DENOVO_TEST_PYTHON
const code = path.resolve(import.meta.dir, "../../../../../routes/de-novo/src")

test.skipIf(!python)("batch Python bridge selects raw parents and exports mapped SDF", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "denovo-batch-test-"))
  async function run(module: string, value: unknown, args: string[]) {
    const process = Bun.spawn([python!, "-B", "-m", module, ...args], {
      env: { ...globalThis.process.env, PYTHONPATH: code },
      stdin: new TextEncoder().encode(JSON.stringify(value)), stdout: "pipe", stderr: "pipe",
    })
    const [exit, output, error] = await Promise.all([process.exited,
      new Response(process.stdout).text(), new Response(process.stderr).text()])
    if (exit !== 0) throw Error(error)
    return JSON.parse(output)
  }
  try {
    const task = "denovo-aaaaaaaaaaaaaaaaaaaaaaaa"
    const input = { state: "RESULTS_READ", requested_slots: 2, task_id: task, provenance: {},
      candidates: ["CNC(=O)C=C", "C=CC(=O)NC"].flatMap((smiles, slot) => ["raw", "relaxed"].map(representation => ({
        representation, slot, candidate_id: `${task}:${slot}:${representation}`, smiles,
        source_sha256: "a".repeat(64), structural_qc_passed: false, failures: ["generation_geometry"],
      }))) }
    const selection = await run("covscreen_de_novo.batch_selection", input, ["-"])
    expect(selection.chemical_qc.attempted).toBe(2)
    expect(selection.diversity.selected_count).toBe(1)
    expect(selection.chemical_qc.rows[1].duplicate_of).toBe(`${task}:0:raw`)
    const output = path.join(root, "input")
    const manifest = await run("covscreen_de_novo.batch_input", selection, ["--output", output])
    expect(manifest.candidate_count).toBe(1)
    expect(manifest.candidates[0].reaction_atom_maps).toMatchObject({ alpha: 905, beta: 906 })
    expect(await Bun.file(path.join(output, "ligands.sdf")).exists()).toBe(true)
    await expect(run("covscreen_de_novo.batch_input", selection, ["--output", output])).rejects.toThrow()
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
