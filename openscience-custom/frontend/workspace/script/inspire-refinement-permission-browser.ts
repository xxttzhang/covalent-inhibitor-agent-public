// Read-only acceptance: never click a permission, send chat or create a job.
import { chromium, expect } from "@playwright/test"

const root = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!root?.startsWith("/")) throw Error("Set an absolute COVSCREEN_ACCEPTANCE_ROOT for this historical acceptance")
const output = `${root}/reports/inspire-refinement-retry-permission-2026-09-15`
const browser = await chromium.launch({ headless: true, channel: "chrome" })
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  await page.goto("http://localhost:4444/prj_dbfd2a34da2e4c78977238fcf59d39ae/session/ses_f6007fb23ffe8VLlztrSdnvB6b", { waitUntil: "domcontentloaded" })
  const card = page.locator('[data-component="permission-prompt"]').filter({ hasText: "refineflow-b9621b50c4efd528dcf66acf" })
  await expect(card).toBeVisible({ timeout: 20000 })
  await expect(card).toContainText("823a5358407ba6885b6d8267e1d1038bb2531b41a416aea9351f5f51da86c6b5")
  await expect(card).toContainText("修复后重试")
  const button = card.getByRole("button", { name: "仅批准本次", exact: true })
  await expect(button).toBeEnabled()
  await button.scrollIntoViewIfNeeded()
  const colors = await button.evaluate((element) => {
    const style = getComputedStyle(element)
    return { foreground: style.color, background: style.backgroundColor }
  })
  function luminance(color: string) {
    const values = color.match(/^rgb\((\d+), (\d+), (\d+)\)$/)
    if (!values) throw new Error("Opaque computed RGB colors required")
    const channels = values.slice(1).map((value) => {
      const channel = Number(value) / 255
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    })
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
  }
  const foreground = luminance(colors.foreground)
  const background = luminance(colors.background)
  const contrast = (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
  expect(contrast).toBeGreaterThanOrEqual(4.5)
  await page.screenshot({ path: `${output}-fixed.png` })
  const result = { observed: new Date().toISOString(), workflow: "refineflow-b9621b50c4efd528dcf66acf",
    passed: true, approval_sent: false, button: await button.innerText(), colors, contrast,
    screenshot: `${output}-fixed.png`, scope: "visible native confirmation only, not scientific completion" }
  await Bun.write(`${output}-audit.json`, JSON.stringify(result, null, 2) + "\n")
  console.log(JSON.stringify(result))
} finally {
  await browser.close()
}
