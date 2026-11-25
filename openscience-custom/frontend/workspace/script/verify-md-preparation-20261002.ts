import { chromium } from '@playwright/test'
import path from 'node:path'
const root = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!root || !path.isAbsolute(root)) throw Error('Set COVSCREEN_ACCEPTANCE_ROOT to the external historical evidence workspace')
const folder = path.join(root, 'reports/first-md-20261001/delivery-20261002')
const receipt = await Bun.file(path.join(folder, 'platform-receipt.json')).json()
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
  await page.goto(receipt.url)
  await page.getByRole('button').filter({ hasText: 'FilesProject files' }).first().click()
  await page.getByRole('button', { name: 'Open 首批 MD · 三候选与阿法替尼对照 · 参数化已核验, version 1', exact: true }).click()
  await page.getByText('尚未获得平衡或生产期轨迹', { exact: false }).filter({ visible: true }).first().waitFor()
  const report = await page.locator('body').innerText()
  for (const label of ['denovo_004', 'denovo_001', 'denovo_007', 'control_afatinib', '63,877', '63,857', '63,875', '63,898']) {
    if (!report.includes(label)) throw Error('Missing report content: ' + label)
  }
  await page.screenshot({ path: path.join(folder, 'platform-md-preparation.png') })
  await Bun.write(path.join(folder, 'browser-verification.json'), JSON.stringify({ production: true, four_system_report_visible: true, no_trajectory_claim_visible: true, screenshot: 'platform-md-preparation.png' }, null, 2))
  console.log('MD preparation report verified in platform UI')
} finally { await browser.close() }
