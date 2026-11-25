import type { RDKitLoader } from "@rdkit/rdkit"
import { wasmIdentity } from "@synsci/util/discovery-wasm-identity"

globalThis.onmessage = async (event: MessageEvent<string[]>) => {
  try {
    const inputs = event.data
    if (!Array.isArray(inputs) || inputs.length > 1000 || inputs.some(s => typeof s !== "string" || s.length > 20000)
      || inputs.reduce((total, s) => total + s.length, 0) > 1000000) throw Error("Identity batch limit exceeded")
    const init = ((await import("@rdkit/rdkit")) as unknown as { default: RDKitLoader }).default
    const wasm = (await import("@rdkit/rdkit/dist/RDKit_minimal.wasm?url")).default
    const rdkit = await init({ locateFile: () => wasm })
    globalThis.postMessage({ version: rdkit.version(), identities: inputs.map(text => wasmIdentity(rdkit, text)) })
  } catch (error) { globalThis.postMessage({ error: String(error) }) }
}
