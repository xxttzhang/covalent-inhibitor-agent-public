import { chromium } from "@playwright/test"
import path from "node:path"
const root = path.resolve(import.meta.dir, "../../../..")
const receipt = await Bun.file(path.join(root, "science/reports/ligand_viewer_20260924/platform_receipt.json")).json()
const preview = !process.argv.includes("--production")
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  page.on("pageerror", (error) => console.log(JSON.stringify({ error: error.message })))
  if (preview) await page.route("http://127.0.0.1:4096/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname
    const relative = route.request().isNavigationRequest() ? "index.html" : pathname.startsWith("/assets/") ? pathname.slice(1) : undefined
    if (!relative) return route.continue()
    const build = process.env.RESULTS_PREVIEW_BUILD ?? path.join(root, ".cache/ligand-overview-preview")
    const filename = path.resolve(build, relative)
    if (!filename.startsWith(build + "/")) throw new Error("Invalid asset path")
    const file = Bun.file(filename)
    if (!await file.exists()) return route.continue()
    return route.fulfill({ body: Buffer.from(await file.arrayBuffer()), contentType: file.type })
  })
  await page.goto(receipt.url)
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().click()
  await page.locator("[data-card-open]").filter({ hasText: "Ligand-first · 3 个候选 · 查看结构／结合口袋" }).click()
  await page.locator("[data-ligand-overview]").waitFor()
  await page.getByRole("button", { name: "展开结果总览", exact: true }).click()
  await page.waitForFunction(() => document.querySelectorAll('[data-ligand-overview] [data-component="chem-2d"] svg').length === 4)
  for (const suffix of ["000060", "000066", "000058"]) {
    await page.getByRole("button", { name: `查看候选 fragment_000001_product_${suffix}`, exact: true }).click()
    await page.locator("[data-pocket-message]").filter({ hasText: "个蛋白残基" }).waitFor({ timeout: 45000 })
    if (!(await page.locator("[data-candidate-identity]").innerText()).endsWith(suffix)) throw new Error("Wrong selection")
    if (suffix === "000058") {
      if (!await page.getByRole("button", { name: "AF3 预测构象", exact: true }).isDisabled()) throw new Error("Missing AF3 not disabled")
      if (!(await page.locator(".ligand-notice").innerText()).includes("B2")) throw new Error("B2 lost")
    } else {
      await page.getByRole("button", { name: "AF3 预测构象", exact: true }).click()
      await page.locator("[data-pocket-message]").filter({ hasText: "CYS 105" }).waitFor({ timeout: 45000 })
    }
    await page.getByRole("button", { name: "恢复完整结构", exact: true }).click()
    await page.locator('[data-component="mol-structure"][data-status="ready"]').waitFor({ timeout: 45000 })
    if (await page.locator("[data-pocket-message]").count()) throw new Error("Restore re-applied pocket focus")
    if (await page.getByRole("button", { name: "恢复完整结构", exact: true }).count()) throw new Error("Restore did not clear pocket state")
    await page.screenshot({ path: path.join(root, `reports/ligand-full-structure-${suffix}-${preview ? "preview" : "production"}.png`) })
    await page.getByRole("button", { name: "一键查看结合口袋", exact: true }).click()
    await page.locator("[data-pocket-message]").filter({ hasText: "个蛋白残基" }).waitFor({ timeout: 15000 })
  }
  await page.getByRole("button", { name: "查看候选 fragment_000001_product_000066", exact: true }).click()
  await page.locator("[data-pocket-message]").filter({ hasText: "CYS 797" }).waitFor({ timeout: 45000 })
  await page.screenshot({ path: path.join(root, `reports/ligand-overview-${preview ? "preview" : "production"}.png`) })
  await page.getByRole("button", { name: "退出大图", exact: true }).click()
  await page.setViewportSize({ width: 900, height: 1000 })
  await page.screenshot({ path: path.join(root, "reports/ligand-overview-narrow.png") })
  console.log(JSON.stringify({ production: !preview, molecular_depictions: 4, candidate_cards: 3, auto_pocket: true, methods_checked: 5, b2_preserved: true, restore_full_and_return_to_pocket: 3 }))
} catch (error) {
  console.log((await browser.contexts()[0]?.pages()[0]?.locator("body").innerText())?.slice(-3500))
  throw error
} finally { await browser.close() }
