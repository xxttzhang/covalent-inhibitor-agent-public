// Actual UI acceptance only. Does not send chat, approve, submit, or publish.
import { chromium, expect } from "@playwright/test"

const attempt = process.argv.find((arg) => arg.startsWith("--attempt="))?.slice(10)
if (!attempt || !/^[a-z0-9-]{1,40}$/.test(attempt)) throw new Error("Explicit acceptance attempt required")
const workspace = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!workspace?.startsWith("/")) throw Error("Set an absolute COVSCREEN_ACCEPTANCE_ROOT for this historical acceptance")
const root = `${workspace}/reports`
const receipt = await Bun.file(`${root}/inspire-test100-live-acceptance-${attempt}.json`).json()
const audit = await Bun.file(`${root}/inspire-test100-completion-audit-${attempt}.json`).json()
if (!audit.passed || audit.task !== receipt.task || audit.plan_digest !== receipt.digest ||
    audit.state !== "SUCCEEDED" || audit.delivery?.state !== "COMPLETE")
  throw new Error("Complete scientific and HTTP artifact audit must pass before UI completion acceptance")
type File = { phase: string; name: string; title: string; artifact_id: string; version_id: string; sha256: string; bytes: number }
const files = audit.native_files as File[]
const targets = ["report.md", "candidates.csv", "report.json"].map((name) => {
  const matches = files.filter((file) => file.phase === "report" && file.name === name)
  if (matches.length !== 1) throw new Error(`Missing unique report file: ${name}`)
  return matches[0]
})
const browser = await chromium.launch({ headless: true, channel: "chrome" })
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, acceptDownloads: true })
const errors: string[] = []
page.on("pageerror", (error) => errors.push(error.message))
const url = `http://localhost:4444/${receipt.project}/session/${receipt.session}`
const output = `${root}/inspire-test100-browser-audit-${attempt}.json`
const results: { name: string; preview: boolean; download: boolean; sha256: string; bytes: number; screenshot: string }[] = []
try {
  for (const file of targets) {
    await page.goto(url, { waitUntil: "domcontentloaded" })
    const expand = page.getByText("显示步骤", { exact: true })
    await expect(expand).toBeVisible({ timeout: 30000 })
    await expand.click()
    const card = page.locator("[data-component=inspire-progress]").filter({ hasText: receipt.task })
    await expect(card).toContainText("Inspire 筛选进度 · 已完成", { timeout: 30000 })
    await expect(card.getByRole("region", { name: "自动文件交付进度" })).toContainText("完整清单已核验交付")
    const rows = card.locator("tbody tr")
    await expect(rows).toHaveCount(4)
    for (const [index, stage] of audit.stages.entries()) {
      await expect(rows.nth(index)).toContainText("已完成")
      await expect(rows.nth(index)).toContainText(`${stage.input} / ${stage.success} / ${stage.failed}`)
    }
    const button = card.getByRole("button", { name: `查看文件及历史版本 · report/${file.name}`, exact: true })
    if (await button.count()) await button.click()
    else {
      // Long pose inventories can exceed the progress card's first 100 files.
      await page.getByRole("button", { name: /FilesProject files/ }).click()
      const pane = page.getByRole("region", { name: "Files", exact: true })
      const source = pane.locator("[data-source-button]")
      if (await source.getAttribute("data-source-kind") !== "artifacts") {
        await source.click()
        await pane.locator('[data-source-item][data-source-kind="artifacts"]').click()
      }
      await pane.getByRole("searchbox", { name: "Search artifacts" }).fill(file.title)
      await pane.locator("[data-card-open]").filter({ hasText: file.title }).click()
    }
    const view = page.getByRole("region", { name: `Saved Result ${file.title}`, exact: true })
    await expect(view).toBeVisible()
    if (file.name === "report.md") {
      await expect(view.locator("article")).toContainText("Screening acceptance report")
      await expect(view.locator("article")).toContainText(receipt.task)
    } else {
      await expect(view.locator("pre")).not.toBeEmpty()
      await expect(view.locator("pre")).toContainText(file.name === "candidates.csv" ? "compound_id" : receipt.task)
    }
    const link = view.getByRole("link", { name: "Download", exact: true })
    const href = await link.getAttribute("href")
    if (!href?.includes(file.artifact_id) || !href.includes(file.version_id)) throw new Error("UI download points to a different artifact/version")
    const pending = page.waitForEvent("download", { timeout: 30000 })
    await link.click()
    const download = await pending
    const path = await download.path()
    if (!path || await download.failure()) throw new Error("Browser download failed")
    const bytes = await Bun.file(path).arrayBuffer()
    const hash = new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
    if (bytes.byteLength !== file.bytes || hash !== file.sha256) throw new Error("Browser download differs from verified result")
    if (file.name === "report.json") {
      const report = JSON.parse(new TextDecoder().decode(bytes))
      if (report.task_id !== receipt.task || report.selected_count !== 100 || report.plan_digest !== receipt.digest ||
          report.maximum_reserved_points !== null || !Object.values(report.gates).every((value) => value === true))
        throw new Error("Final report content does not satisfy the frozen acceptance")
    }
    const screenshot = `${root}/inspire-test100-${attempt}-${file.name.replaceAll(".", "-")}-preview.png`
    await view.screenshot({ path: screenshot })
    results.push({ name: file.name, preview: true, download: true, sha256: hash, bytes: bytes.byteLength, screenshot })
  }
  if (errors.length) throw new Error(`Browser runtime errors: ${errors.join("; ")}`)
  await Bun.write(output, JSON.stringify({ checked_at: new Date().toISOString(), task: receipt.task,
    plan_digest: receipt.digest, passed: true, url, results, errors }, null, 2))
  console.log(JSON.stringify({ passed: true, task: receipt.task, results, output }))
} catch (error) {
  await page.screenshot({ path: `${root}/inspire-test100-${attempt}-browser-failure.png` })
  await Bun.write(output, JSON.stringify({ checked_at: new Date().toISOString(), task: receipt.task, passed: false,
    error: error instanceof Error ? error.message : "UI audit failure", results, errors }, null, 2))
  throw error
} finally { await browser.close() }
