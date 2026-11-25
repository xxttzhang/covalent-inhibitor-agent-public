// Operator-managed science service. Commands and data roots are installation
// bindings, never tool arguments. Native scientific approvals remain enforced.
import path from "node:path"
import { lstat } from "node:fs/promises"
import { start } from "../../openscience-custom/backend/cli/integrations/hypothesis-connector/server"

const root = process.env.COVALENT_RUNTIME_ROOT
if (!root || !path.isAbsolute(root)) throw Error("Explicit absolute runtime root required")
const binding = await Bun.file(path.join(root, "binding.json")).json()
if (![undefined, "local", "ssh", "jupyter"].includes(binding.science_transport)) throw Error("Unknown scientific transport")
if (binding.science_transport === "jupyter" && (typeof binding.transport_python !== "string" || !path.isAbsolute(binding.transport_python)))
  throw Error("Explicit absolute Jupyter transport Python required")
const source = path.resolve(import.meta.dir, "../..")
if (binding.source !== source) throw Error("Runtime belongs to a different checkout")
const filename = path.join(root, "config/connector.key")
const stat = await lstat(filename)
if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077)) throw Error("Owner-only connector key required")
const secret = (await Bun.file(filename).text()).trim()
if (secret.length < 32) throw Error("Connector key too short")
const server = start(secret, binding.science_port, {
  command: binding.science_transport === "jupyter" ? binding.transport_python : binding.python,
  args: binding.science_transport === "jupyter"
    ? ["-B", path.join(source, "ops/ligand_jupyter_mcp.py"), "--binding", path.join(root, "config/science-binding.json"),
      "--connection", path.join(root, "config/science-connection.json"), "--runtime", path.join(root, "config/science-runtime.json")]
    : binding.science_transport === "ssh"
    ? ["-B", path.join(source, "ops/ligand_bound_mcp.py"), "--binding", path.join(root, "config/science-binding.json"),
      "--connection", path.join(root, "config/science-connection.json"), "--verify-runtime-config",
      "--delivery-root", path.join(binding.project, "results/target-hypotheses"),
      "--runtime", path.join(root, "config/science-runtime.json")]
    : ["-B", path.join(source, "science/scripts/openscience_mcp_server.py")],
  env: { COVSCREEN_PROJECT_ROOT: path.join(root, "science-data"),
    COVALENT_APPROVAL_KEY_FILE: path.join(root, "config/native-approval.key"),
    PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1",
    ...(["ssh", "jupyter"].includes(binding.science_transport) && ["asgbie3", "asgbie4"].includes(process.env.COVALENT_LIGAND_VS_INSTANCE ?? "")
      ? { COVALENT_LIGAND_VS_INSTANCE: process.env.COVALENT_LIGAND_VS_INSTANCE! }
      : {}) },
})
console.log(`Independent scientific connector listening on 127.0.0.1:${server.port}`)
