import { chromium } from "@playwright/test"
import path from "node:path"
import { Database } from "bun:sqlite"

const root = path.resolve(import.meta.dir, "../../../..")
const receipt = await Bun.file(path.join(root, "science/reports/ligand_viewer_20260924/platform_receipt.json")).json()
const base = process.env.LIGAND_VIEWER_URL ?? "http://127.0.0.1:4100"
const preview = !process.argv.includes("--production")
const db = new Database(path.join(root, ".cache/inspire-executor/tasks.sqlite"), { readonly: true })
const snapshot = () => JSON.stringify(db.query("SELECT id,submitted,handle FROM refinement_runs ORDER BY id").all())
const before = snapshot()
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true,
})
const observations: unknown[] = []
try {
  const page = await browser.newPage({ viewport: { width: 1550, height: 1100 } })
  page.on("pageerror", (error) => console.log(JSON.stringify({ browser_error: error.message })))
  if (preview) await page.route(`${base}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    const relative = request.isNavigationRequest() ? "index.html" : pathname.startsWith("/assets/") ? pathname.slice(1) : null
    if (!relative) return route.continue()
    const build = path.join(root, ".cache/ligand-viewer-build-20260924")
    const target = path.resolve(build, relative)
    if (!target.startsWith(build + "/")) throw new Error("Escaped preview build")
    const file = Bun.file(target)
    if (!await file.exists()) return route.continue()
    return route.fulfill({ status: 200, body: Buffer.from(await file.arrayBuffer()), contentType: file.type })
  })
  await page.goto(`${base}/${receipt.project}/session/${receipt.session}`, { waitUntil: "domcontentloaded" })
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().click()
  await page.locator("[data-card-open]").filter({ hasText: "Ligand-first · 3 个候选 · 查看结构／结合口袋" }).click()
  await page.locator("[data-candidate-table]").waitFor()
  if (await page.getByLabel("候选分子", { exact: true }).locator("option").count() !== 3) throw new Error("Unexpected candidate count")
  for (const [index, compound, methods] of [[0, "000060", ["AF3", "CovDock"]], [1, "000066", ["AF3", "CovDock"]], [2, "000058", ["CovDock"]]] as const) {
    await page.getByLabel("候选分子", { exact: true }).selectOption(String(index))
    await page.locator('[data-component="chem-2d"] svg').waitFor({ timeout: 45000 })
    if (!(await page.locator("[data-candidate-identity]").innerText()).endsWith(compound)) throw new Error("Wrong candidate")
    if (index === 2 && await page.getByRole("button", { name: "查看 AF3结构／结合口袋", exact: true }).count()) throw new Error("Invented AF3 link")
    for (const method of methods) {
      await page.getByRole("button", { name: `查看 ${method}结构／结合口袋`, exact: true }).click()
      await page.locator('[data-candidate-structure] [data-component="mol-structure"][data-status="ready"]').waitFor({ timeout: 45000 })
      await page.getByRole("button", { name: "一键查看结合口袋", exact: true }).click()
      await page.locator("[data-pocket-message]").filter({ hasText: "个蛋白残基" }).waitFor({ timeout: 15000 })
      const message = await page.locator("[data-pocket-message]").innerText()
      observations.push({ compound, method, ready: true, pocket: message })
      await page.locator("[data-candidate-structure]").screenshot({ path: path.join(root, `reports/ligand-viewer-${compound}-${method}.png`) })
      await page.getByRole("button", { name: "关闭三维查看", exact: true }).click()
      if (await page.locator("[data-candidate-structure]").count()) throw new Error("Old structure remained")
    }
  }
  const rows = await page.locator("[data-candidate-table] tbody tr").allTextContents()
  if (!rows[2].includes("B2") || !rows[2].includes("af3_covdock_pose_consistency_not_evaluated")) throw new Error("B2 evidence lost")
  for (const artifact of Object.values(receipt.artifacts) as { id: string; current: { id: string; sha256: string } }[]) {
    const response = await fetch(`${base}/file/artifact-store/${artifact.id}/raw?project=${receipt.project}&versionID=${artifact.current.id}`)
    if (!response.ok) throw new Error("Download failed")
    const digest = new Bun.CryptoHasher("sha256").update(await response.arrayBuffer()).digest("hex")
    if (digest !== artifact.current.sha256) throw new Error("Downloaded artifact changed")
  }
  if (before !== snapshot()) throw new Error("Scientific submission state changed")
  await Bun.write(path.join(root, "reports/ligand-viewer-browser.json"), JSON.stringify({ preview_build_not_deployed: preview, observations, downloads_verified: 7, scientific_submissions_unchanged: true }, null, 2))
  console.log(JSON.stringify({ preview_build_not_deployed: preview, views: observations.length, downloads_verified: 7, scientific_submissions_unchanged: true }))
} catch (error) {
  const page = browser.contexts()[0]?.pages()[0]
  if (page) console.log(JSON.stringify({ page: (await page.locator("body").innerText()).slice(-4500) }))
  throw error
} finally {
  await browser.close()
  db.close()
}
