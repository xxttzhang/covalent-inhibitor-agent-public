import { z } from "zod"
import type { StudyOverview } from "@synsci/util/discovery-study"

const response = z.object({ version: z.string().min(1), identities: z.array(z.object({
  state: z.enum(["validated", "invalid", "unresolved"]), canonical: z.string().nullable(), reason: z.string(),
})) })

/** Browser-only derived identity. Original candidate evidence is never edited or sent elsewhere. */
export async function browserCandidatePool(view: StudyOverview): Promise<StudyOverview> {
  if (!view.candidates.length || view.pool.rdkitVersion) return view
  const worker = new Worker(new URL("./study-identity.worker.ts", import.meta.url), { type: "module" })
  const result = await new Promise<z.infer<typeof response> | { error: string }>(resolve => {
    const finish = (value: z.infer<typeof response> | { error: string }) => { clearTimeout(timer); worker.terminate(); resolve(value) }
    const timer = setTimeout(() => finish({ error: "Browser chemistry worker timed out" }), 15000)
    worker.onerror = () => finish({ error: "Browser chemistry worker could not load" })
    worker.onmessage = event => {
      const parsed = response.safeParse(event.data)
      finish(parsed.success && parsed.data.identities.length === view.candidates.length ? parsed.data : {
        error: typeof event.data?.error === "string" ? event.data.error.slice(0, 1000) : "Browser chemistry returned invalid identities",
      })
    }
    worker.postMessage(view.candidates.map(record => record.candidate.smiles))
  })
  if ("error" in result) return { ...view, pool: { ...view.pool, policy: "browser-identity-unavailable-no-merge",
    groups: view.pool.groups.map(group => ({ ...group, identity: { ...group.identity,
      reason: `浏览器化学身份验证不可用，保留原记录：${result.error}` } })) } }
  const groups = new Map<string, StudyOverview["pool"]["groups"][number]>()
  for (const [index, record] of view.candidates.entries()) {
    const identity = result.identities[index]
    const key = identity.state === "validated" && identity.canonical ? "molecule:" + identity.canonical : "record:" + record.recordID
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key))
    const id = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("")
    const existing = groups.get(id)
    if (existing) existing.sources.push(record)
    else groups.set(id, { id, identity, sources: [record] })
  }
  return { ...view, pool: { policy: "browser-rdkit-wasm-conservative-isomeric-v1", rdkitVersion: result.version,
    ranked: false, sourceCount: view.candidates.length, groups: [...groups.values()] } }
}
