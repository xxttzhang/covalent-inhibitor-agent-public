import path from "node:path"
import { z } from "zod"
import { projectPath } from "@synsci/util/discovery-config"
const pointer = z.object({ version: z.string().regex(/^[a-f0-9]{64}$/), directory: projectPath, files: z.record(z.string(), z.string().regex(/^[a-f0-9]{64}$/)) })
export async function installedRelease(directory: string) {
  const file = Bun.file(path.join(directory, "active.json"))
  if (!await file.exists()) return
  const release = pointer.parse(await file.json())
  const entries: Record<string, string> = {}
  for (const [url, hash] of Object.entries(release.files)) {
    if (!url.startsWith("/") || !projectPath.safeParse(url.slice(1)).success) throw Error("Invalid release path")
    const filename = path.join(directory, release.directory, url.slice(1))
    if (new Bun.CryptoHasher("sha256").update(await Bun.file(filename).arrayBuffer()).digest("hex") !== hash) throw Error("Installed frontend integrity failed: " + url)
    entries[url] = filename
  }
  if (!entries["/index.html"]) throw Error("Installed frontend has no entry")
  return { assets: entries, index: entries["/index.html"], version: release.version }
}
