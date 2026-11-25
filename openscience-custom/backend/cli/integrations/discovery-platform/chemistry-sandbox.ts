import path from "node:path"
import { realpath } from "node:fs/promises"
import { z } from "zod"
import { Sandbox } from "../../src/sandbox/sandbox"

/** Fixed host runtime, no project filesystem grants, no network, no unsandboxed fallback. */
export async function chemistrySandbox(program: string) {
  const config = z.object({ python: z.string().startsWith("/") }).strict().parse(await Bun.file(
    path.resolve(import.meta.dir, "../../../../configs/discovery-chemistry.json")).json())
  const python = await realpath(config.python)
  const runtime = path.dirname(path.dirname(python))
  const planned = Sandbox.wrapArgv({ file: python, args: ["-I", "-B", "-c", program],
    workspace: [], readable: [runtime], options: { enabled: true, network: "deny", onUnavailable: "error", allowWrite: [] } })
  if (!planned.sandboxed) throw Error("Chemistry requires OS sandbox isolation")
  return { planned, cleanup: () => Sandbox.cleanup(planned),
    env: { PATH: "/usr/bin:/bin", PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", OPENBLAS_NUM_THREADS: "1" } }
}
