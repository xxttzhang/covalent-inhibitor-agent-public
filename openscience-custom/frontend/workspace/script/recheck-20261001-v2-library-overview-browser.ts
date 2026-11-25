import { chromium } from "@playwright/test"
import path from "node:path"
import { Database } from "bun:sqlite"
import { candidateTable } from "../src/artifacts/candidate-table"
const root = path.resolve(import.meta.dir, "../../../..")
const receipt = await Bun.file(path.join(root, "reports/library-viewer-receipt.json")).json()
const table = candidateTable(await Bun.file(receipt.artifact.current.sourcePath).text())!
const db = new Database(path.join(root, ".cache/inspire-executor/tasks.sqlite"), { readonly: true })
const state = () => JSON.stringify(db.query("SELECT id,submitted,handle FROM refinement_runs ORDER BY id").all())
const before = state()
const preview = !process.argv.includes("--production")
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  page.on("pageerror", (error) => console.log(JSON.stringify({ browser_error: error.message })))
  if (preview) await page.route("http://127.0.0.1:4096/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname
    const relative = route.request().isNavigationRequest() ? "index.html" : pathname.startsWith("/assets/") ? pathname.slice(1) : undefined
    if (!relative) return route.continue()
    const build = process.env.RESULTS_PREVIEW_BUILD ?? path.join(root, ".cache/library-overview-preview")
    const filename = path.resolve(build, relative)
    if (!filename.startsWith(build + "/")) throw new Error("Invalid asset path")
    const file = Bun.file(filename)
    if (!await file.exists()) return route.continue()
    return route.fulfill({ body: Buffer.from(await file.arrayBuffer()), contentType: file.type })
  })
  await page.goto(receipt.url)
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().click()
  await page.locator("[data-card-open]").filter({ hasText: "Library-first · 8 个候选 · 结构与结合口袋总览" }).click()
  await page.getByRole("button", { name: "展开结果总览", exact: true }).click()
  await page.waitForFunction(() => document.querySelectorAll('[data-ligand-overview] [data-component="chem-2d"] svg').length === 8)
  if (await page.locator(".ligand-parent").count()) throw new Error("Library-first incorrectly shows erlotinib parent")
  const observations = []
  for (const row of table.rows) {
    await page.getByRole("button", { name: `查看候选 ${row.compound_id}`, exact: true }).click()
    await page.locator("[data-pocket-message]").filter({ hasText: "CYS 797" }).waitFor({ timeout: 45000 })
    if (await page.locator("[data-candidate-identity]").innerText() !== row.compound_id) throw new Error("Candidate mismatch")
    if (!(await page.locator(".ligand-notice").innerText()).includes("B2")) throw new Error("B2 changed")
    const covdock = await page.locator("[data-pocket-message]").innerText()
    await page.getByRole("button", { name: "AF3 预测构象", exact: true }).click()
    await page.locator("[data-pocket-message]").filter({ hasText: "CYS 105" }).waitFor({ timeout: 45000 })
    observations.push({ id: row.compound_id, index: row.input_sdf_index, pose: row.covdock_structure_pose, covdock, af3: await page.locator("[data-pocket-message]").innerText() })
  }
  await page.locator(".ligand-overview").evaluate((element) => element.scrollTo(0, 0))
  await page.screenshot({ path: path.join(root, `reports/recheck-20261001-v2-library-overview-${preview ? "preview" : "production"}.png`) })
  const response = await fetch(`http://127.0.0.1:4096/file/artifact-store/${receipt.source.artifact_id}/raw?project=${receipt.project}&versionID=${receipt.source.version_id}`)
  if (!response.ok || new Bun.CryptoHasher("sha256").update(await response.arrayBuffer()).digest("hex") !== receipt.source.sha256) throw new Error("Original result changed")
  if (state() !== before) throw new Error("Scientific submission state changed")
  const result = { production: !preview, cards: 8, views: observations.length * 2, original_table_unchanged: true, scientific_submissions_unchanged: true, observations }
  await Bun.write(path.join(root, "reports/recheck-20261001-v2-library-overview-browser.json"), JSON.stringify(result, null, 2))
  console.log(JSON.stringify({ ...result, observations: undefined }))
} catch (error) {
  console.log((await browser.contexts()[0]?.pages()[0]?.locator("body").innerText())?.slice(-3500))
  throw error
} finally { await browser.close(); db.close() }
