import path from "node:path"
import { mkdir } from "node:fs/promises"
import { discoveryProject, routeDefaults } from "../../openscience-custom/tooling/util/src/discovery-config"
const root = path.resolve(import.meta.dir, "../..")
const output = path.join(root, "reports/workbench-standardization-20261008/verification")
await mkdir(output, { recursive: true })
const project = await Bun.file(path.join(root, "reports/workbench-standardization-20261008/reuse-project.json")).json()
const study = await Bun.file(path.join(root, "openscience-custom/.discovery/project.json")).json()
const browser = async (...args: string[]) => {
  const process = Bun.spawn(["bunx", "agent-browser", "--session", "denovo-design", "--json", ...args], { cwd: path.join(root, "openscience-custom"), stdout: "pipe", stderr: "pipe" })
  const [text, error, code] = await Promise.all([new Response(process.stdout).text(), new Response(process.stderr).text(), process.exited])
  if (code !== 0) throw Error(error + text)
  const response = JSON.parse(text)
  if (!response.success) throw Error(JSON.stringify(response))
  return response.data.result
}
const evaluate = (code: string) => browser("eval", code)
const open = async (id: string, route: string) => {
  const same = await evaluate(`location.pathname.startsWith(${JSON.stringify("/" + id + "/discovery/")})`)
  if (same) await evaluate(`document.querySelector('a[href="/${id}/discovery/${route}"]').click()`)
  else await browser("open", `http://127.0.0.1:4096/${id}/discovery/${route}`)
  await evaluate(`(async()=>{for(let i=0;i<180;i++){if(document.querySelector('.discovery h3') && document.querySelector('.discovery h3').textContent!=='配置需要检查' && document.querySelector('.discovery h1')?.textContent.includes(${JSON.stringify(route === 'de-novo' ? 'De novo' : route === 'library-first' ? 'Library-first' : 'Ligand-first')}))return true;await new Promise(r=>setTimeout(r,150))}throw Error('page not ready')})()`)
}
const inspect = () => evaluate(`({phase:document.querySelector(".discovery h3")?.textContent,url:location.href,title:document.querySelector('.discovery h1')?.textContent,version:document.querySelector('.discovery-version')?.textContent,cards:document.querySelectorAll('[data-discovery-candidate]').length,alerts:[...document.querySelectorAll('.discovery [role=alert]')].map(e=>e.textContent),overflow:document.documentElement.scrollWidth>innerWidth,text:document.querySelector('.discovery')?.textContent})`)
const report: unknown[] = []
await browser("set", "viewport", "1600", "1100")
for (const route of ["library-first", "ligand-first", "de-novo"]) {
  await open(study.project, route)
  await evaluate(`(async()=>{for(let i=0;i<60;i++){if(document.querySelectorAll('.discovery-molecule svg').length>=3)return true;await new Promise(r=>setTimeout(r,150))}throw Error('molecules not drawn')})()`)
  const before = await inspect()
  if (before.cards !== 3 || before.alerts.length || before.overflow || before.version !== "Workbench v1.1.0") throw Error("Existing project UI failed: " + JSON.stringify(before))
  await browser("screenshot", path.join(output, route + "-desktop.png"))
  await evaluate(`[...document.querySelectorAll('.discovery-tabs button')].find(e=>e.textContent==='MD 证据').click()`)
  if (route === "de-novo") {
    await evaluate(`(async()=>{for(let i=0;i<80;i++){const image=document.querySelector('.md-analysis img');if(image?.complete&&image.naturalWidth>0)return true;await new Promise(r=>setTimeout(r,150))}throw Error('MD plot not loaded')})()`)
    await browser("screenshot", path.join(output, "de-novo-md.png"))
  } else {
    const md = await evaluate(`({waiting:document.querySelector('.refinement-md-empty')?.textContent,plots:document.querySelectorAll('.md-analysis img').length})`)
    if (!md.waiting || md.plots) throw Error("Foreign MD data shown")
  }
  await evaluate(`[...document.querySelectorAll('.discovery-segment button')].find(e=>e.textContent.startsWith('全部候选')).click()`)
  const all = await inspect()
  if (all.cards !== (route === "de-novo" ? 7 : route === "library-first" ? 8 : 3)) throw Error("Candidate coverage changed")
  report.push({ route, originalProject: before, allCandidates: all.cards, mdCheck: "passed" })
  await browser("set", "viewport", "390", "844")
  const mobile = await inspect()
  if (mobile.overflow) throw Error("Mobile horizontal overflow")
  await browser("screenshot", path.join(output, route + "-mobile.png"))
  await browser("set", "viewport", "1600", "1100")
}
for (const phase of ["unconfigured", "configured"] as const) {
  if (phase === "unconfigured") await Bun.write(path.join(project.worktree, ".discovery/project.json"), JSON.stringify(discoveryProject.parse({ schema: "discovery-project-v1", project: project.id, target: null, routes: routeDefaults })))
  if (phase === "configured") {
    const config = discoveryProject.parse({ schema: "discovery-project-v1", project: project.id, target: { id: "reuse-test-target", name: "复用验收靶点", label: "复用验收靶点 · 尚未提交计算" }, routes: Object.fromEntries(Object.entries(routeDefaults).map(([route, settings]) => [route, { ...settings, dataPath: `results/discovery/${route}.json`, inputPath: `.discovery/inbox/${route}.json` }])) })
    await Bun.write(path.join(project.worktree, ".discovery/project.json"), JSON.stringify(config, null, 2))
  }
  for (const route of ["library-first", "ligand-first", "de-novo"]) {
    await open(project.id, route)
    const check = await inspect()
    if (check.cards !== 0 || check.alerts.length || /EGFR|C797|denovo_00|covindb|fragment_000001/.test(check.text) || check.phase !== (phase === "configured" ? "待运行" : "待配置")) throw Error("Reuse isolation failed: " + JSON.stringify(check))
    await browser("screenshot", path.join(output, `reuse-${phase}-${route}.png`))
    report.push({ phase, route, ...check })
  }
}
await Bun.write(path.join(output, "browser.json"), JSON.stringify({ checkedAt: new Date().toISOString(), reusedProject: project.id, checks: report }, null, 2))
await open(study.project, "library-first")
console.log(JSON.stringify({ passed: true, checks: report.length, output }))
