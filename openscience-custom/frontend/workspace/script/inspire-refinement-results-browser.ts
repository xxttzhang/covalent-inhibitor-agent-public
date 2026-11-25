import { chromium } from "@playwright/test"
import { Database } from "bun:sqlite"
import path from "node:path"
import { readArtifactTarget } from "../../../backend/cli/integrations/inspire-screening/executor/artifact-target"

const workspace = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!workspace || !path.isAbsolute(workspace)) throw Error('Set COVSCREEN_ACCEPTANCE_ROOT to the external historical evidence workspace')
const database = new Database(path.join(workspace, ".cache/inspire-executor/tasks.sqlite"), { readonly: true })
const workflow = "refineflow-76378344dae197d0022d4549"
const before = JSON.stringify(database.query("SELECT id,submitted,handle FROM refinement_runs ORDER BY id").all())
const maestro = process.argv.includes("--maestro")
const candidates = process.argv.includes("--candidates")
const gate = process.argv.includes("--gate") || candidates
const structure = process.argv.includes("--structure") || maestro
const ordinal = Number(process.argv.find((arg) => arg.startsWith("--ordinal="))?.split("=")[1] ?? 0)
if (!Number.isInteger(ordinal) || ordinal < 0 || ordinal > 7) throw new Error("Invalid structure ordinal")
const selected = database.query("SELECT name,sha256 FROM refinement_delivery_files WHERE workflow_id=? AND receipt IS NOT NULL AND name LIKE ? ORDER BY name LIMIT 1 OFFSET ?")
  .get(workflow, gate ? "%stage5_gated_candidates.csv" : maestro ? "%.maegz" : structure ? "%_model.cif" : "report/refinement-report.md", ordinal) as { name: string; sha256: string } | null
if (!selected) throw new Error("Requested native result not yet delivered")

// Existing platform only: no server startup, fixture uploads or compute requests.
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true, timeout: 30000,
})
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  page.on("pageerror", (error) => console.log(JSON.stringify({ browser_error: error.message })))
  const preview = process.argv.includes("--preview-build")
  if (preview) await page.route("http://127.0.0.1:4096/**", async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (process.argv.includes("--target-context-preview") && pathname.endsWith("/screening-target")) {
      const url = new URL(request.url())
      url.pathname = pathname.replace(/\/screening-target$/, "")
      const response = await fetch(url)
      if (!response.ok) throw new Error("Cannot inspect the actual saved Result")
      const context = await readArtifactTarget(await response.json(), path.join(workspace, "openscience-custom"))
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(context) })
    }
    const asset = request.isNavigationRequest() ? "index.html" : pathname.startsWith("/assets/") ? pathname.slice(1) : null
    if (!asset) return route.continue()
    const root = path.join(workspace, ".cache/refinement-ui-preview-build")
    const target = path.resolve(root, asset)
    if (!target.startsWith(root + "/")) throw new Error("Preview asset escaped build root")
    const file = Bun.file(target)
    if (!await file.exists()) return route.continue()
    await route.fulfill({ status: 200, contentType: asset.endsWith(".js") ? "application/javascript" : file.type,
      body: Buffer.from(await file.arrayBuffer()) })
  })
  if (process.argv.includes("--target-form")) {
    await page.goto("http://127.0.0.1:4096/prj_dbfd2a34da2e4c78977238fcf59d39ae/session/new", { waitUntil: "domcontentloaded" })
    await page.getByLabel("靶点名称", { exact: true }).fill("BTK")
    await page.getByLabel("受体标识或路径", { exact: true }).fill("BTK-prepared.cif")
    await page.getByLabel("链（auth chain）", { exact: true }).fill("B")
    await page.getByLabel("目标残基编号", { exact: true }).fill("481")
    await page.getByLabel("残基类型", { exact: true }).selectOption("CYS")
    await page.getByLabel("反应类型", { exact: true }).fill("Michael Addition")
    await page.getByLabel("预算上限（点券）", { exact: true }).fill("50")
    await page.getByRole("button", { name: "将任务参数写入对话框", exact: true }).click()
    const composer = page.locator('[contenteditable="true"]').first()
    if (!(await composer.innerText()).includes('"target":"BTK"')) throw new Error("Task form did not reach the composer")
    await page.locator("#screening-target-parameters").scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${workspace}/reports/task-parameters-form-${preview ? "preview" : "production"}.png` })
    console.log(JSON.stringify({ task_form_preview: preview, prompt_written: true, prompt_sent: false }))
    // Do not send this test draft, and don't leave it in the user's new-session composer.
    await composer.fill("")
  }
  await page.goto("http://127.0.0.1:4096/prj_dbfd2a34da2e4c78977238fcf59d39ae/session/ses_f6007fb23ffe8VLlztrSdnvB6b", { waitUntil: "domcontentloaded" })
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().click().catch(async (error) => {
    console.log(JSON.stringify({ navigation_error: (await page.locator("body").innerText()).slice(0, 2000) }))
    throw error
  })
  await page.locator(".files-pane").waitFor()
  const report = page.locator("[data-card-open]").filter({ hasText: `${workflow} · refinement_result · ${selected.name}` })
  await report.click()
  if (!structure && !gate) await page.locator(".markdown-body").filter({ hasText: "精筛综合报告" }).waitFor()
  if (gate && !candidates) await page.locator("pre:not([data-thumb-text])").filter({ hasText: "af3_covdock_pose_consistency_not_evaluated" }).waitFor()
  if (candidates) {
    await page.locator("[data-candidate-table]").waitFor()
    const count = await page.getByLabel("候选分子").locator("option").count()
    if (count !== 8) throw new Error("Candidate count changed")
    for (let i = 0; i < count; i++) {
      await page.getByLabel("候选分子").selectOption(String(i))
      await page.locator('[data-component="chem-2d"] svg').waitFor({ timeout: 45000 })
      console.log(JSON.stringify({ candidate_row: i, rdkit_svg: true, identity: await page.locator('[data-candidate-identity]').innerText() }))
    }
    const rows = await page.locator('[data-candidate-table] tbody tr').allTextContents()
    if (rows.some((row) => !row.includes("B2") || !row.includes("af3_covdock_pose_consistency_not_evaluated"))) throw new Error("Missing evidence hidden")
  }
  if (structure) {
    if (process.argv.includes("--target-context-preview") || process.argv.includes("--target-context")) {
      await page.locator("[data-task-target]").filter({ hasText: "EGFR / C797" }).waitFor()
      console.log(JSON.stringify({ target_context_preview: await page.locator("[data-task-target]").innerText(),
        context_from_real_readonly_receipt: true, production_api_not_deployed: preview }))
    }
    await page.locator('[data-component="mol-structure"][data-status="ready"]').waitFor({ timeout: 45000 }).catch(async (error) => {
      await page.getByText("technical details", { exact: true }).click({ timeout: 1000 }).catch(() => undefined)
      console.log(JSON.stringify({ alerts: await page.locator('[data-maestro-preview]').allTextContents(),
        viewer: await page.locator('[data-component="mol-structure"]').allTextContents(),
        page_tail: (await page.locator('body').innerText()).slice(-4000) }))
      throw error
    })
    await page.getByRole("button", { name: "Reset camera", exact: true }).click()
    await page.waitForTimeout(1500) // Allow camera interpolation and a rendered WebGL frame before visual inspection.
    if (process.argv.includes("--pocket")) {
      const choices = page.getByLabel("口袋配体", { exact: true })
      if (await choices.count()) {
        const options = await choices.locator("option").evaluateAll((items) => items.map((item) => ({ value: (item as HTMLOptionElement).value, text: item.textContent })))
        const ligand = options.filter((item) => item.value && /UNK|LIG/.test(item.text ?? ""))
        if (ligand.length !== 1) throw new Error("Ambiguous actual ligand: select explicitly before testing")
        await choices.selectOption(ligand[0].value)
      }
      await page.getByRole("button", { name: "一键查看结合口袋", exact: true }).click()
      await page.locator("[data-pocket-message]").filter({ hasText: "此视图不代表" }).waitFor({ timeout: 30000 }).catch(async (error) => {
        console.log(JSON.stringify({ pocket_failure: await page.locator("[data-pocket-message]").allTextContents() }))
        throw error
      })
      console.log(JSON.stringify({ pocket: await page.locator("[data-pocket-message]").innerText() }))
      await page.waitForTimeout(1500)
      await page.screenshot({ path: `${workspace}/reports/pocket-${maestro ? "covdock" : "af3"}${process.argv.includes("--target-context-preview") ? "-target-preview" : ""}.png` })
    }
    if (maestro) {
      for (const [pose, input] of [[32,17],[52,19],[2,29],[62,30],[71,33],[84,38],[27,56],[47,81]]) {
        await page.getByLabel("Maestro pose index").selectOption(String(pose))
        await page.locator("[data-maestro-identity]").filter({ hasText: `候选索引：${input} ·` }).waitFor()
        await page.locator('[data-component="mol-structure"][data-status="ready"]').waitFor({ timeout: 45000 })
        console.log(JSON.stringify({ maestro_pose: pose, candidate: input, viewer_ready: true }))
      }
      await page.getByRole("button", { name: "Reset camera", exact: true }).click()
      await page.waitForTimeout(1500)
    }
  }
  await page.getByRole("link", { name: "Download", exact: true }).waitFor()
  const download = page.waitForEvent("download")
  await page.getByRole("link", { name: "Download", exact: true }).click()
  const file = await (await download).path()
  if (!file) throw new Error("No browser download bytes")
  const checksum = new Bun.CryptoHasher("sha256").update(await Bun.file(file).arrayBuffer()).digest("hex")
  if (checksum !== selected.sha256) throw new Error("Browser result download mismatch")
  console.log(JSON.stringify({ name: selected.name, report_preview: !structure && !gate, missing_evidence_preview: gate, browser_download_hash_verified: true,
    preview_build_not_deployed: preview, molecular_viewer_ready: await page.locator('[data-component="mol-structure"][data-status="ready"]').count(),
    unsupported_preview: await page.getByText(/Preview is not available for/).count() }))
  await page.screenshot({ path: `${workspace}/reports/refinement-${gate ? "gate" : maestro ? "maestro" : structure ? `structure-${ordinal}` : "results"}-20260922.png` })
  await page.context().setOffline(true)
  await page.reload({ waitUntil: "domcontentloaded", timeout: 10000 }).catch(() => undefined)
  await page.context().setOffline(false)
  await page.goto("http://127.0.0.1:4096/prj_dbfd2a34da2e4c78977238fcf59d39ae/session/ses_f6007fb23ffe8VLlztrSdnvB6b", { waitUntil: "domcontentloaded" })
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().waitFor()
  console.log(JSON.stringify({ reconnected_original_session: true,
    submissions_unchanged: before === JSON.stringify(database.query("SELECT id,submitted,handle FROM refinement_runs ORDER BY id").all()),
    preview_restored: await page.locator(".markdown-body").filter({ hasText: "精筛综合报告" }).count() }))
  await page.getByRole("button").filter({ hasText: "FilesProject files" }).first().click()
  await page.locator("[data-card-open]").filter({ hasText: `${workflow} · refinement_result · ${selected.name}` }).click()
  if (!structure && !gate) await page.locator(".markdown-body").filter({ hasText: "精筛综合报告" }).waitFor()
  if (gate && !candidates) await page.locator("pre:not([data-thumb-text])").filter({ hasText: "af3_covdock_pose_consistency_not_evaluated" }).waitFor()
  if (candidates) await page.locator('[data-component="chem-2d"] svg').waitFor({ timeout: 45000 })
  if (structure) await page.locator('[data-component="mol-structure"][data-status="ready"]').waitFor({ timeout: 45000 })
  console.log(JSON.stringify({ original_result_reopened_after_reconnect: true, name: selected.name }))
} finally { await browser.close(); database.close() }
