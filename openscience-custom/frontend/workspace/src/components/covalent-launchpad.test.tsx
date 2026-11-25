import { afterAll, afterEach, describe, expect, test } from "bun:test"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"
import solid from "vite-plugin-solid"

const cleanups: Array<() => void> = []
const server = await createServer({
  root: fileURLToPath(new URL("../..", import.meta.url)),
  mode: "production",
  logLevel: "silent",
  plugins: [solid({ ssr: false, dev: false })],
  server: { middlewareMode: true },
  appType: "custom",
  resolve: { conditions: ["browser", "production"], dedupe: ["solid-js", "solid-js/web"] },
  ssr: {
    noExternal: true,
    resolve: { conditions: ["browser", "production"] },
  },
})
const [subject, web] = await Promise.all([
  server.ssrLoadModule("/src/components/covalent-launchpad.tsx") as Promise<typeof import("./covalent-launchpad")>,
  server.ssrLoadModule("solid-js/web") as Promise<typeof import("solid-js/web")>,
])

afterAll(() => server.close())

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  document.body.replaceChildren()
})

function mount(onPrompt: (value: string) => void) {
  const host = document.createElement("div")
  document.body.append(host)
  cleanups.push(web.render(() => subject.CovalentLaunchpad({ onPrompt }), host))
  return host
}

test("task form has no EGFR defaults and writes complete target parameters without submitting compute", () => {
  const prompts: string[] = []
  const host = mount((value) => prompts.push(value))
  const field = (name: string, value: string) => {
    const input = host.querySelector(`[aria-label="${name}"]`) as HTMLInputElement
    input.value = value
    input.dispatchEvent(new Event(input.tagName === "SELECT" ? "change" : "input", { bubbles: true }))
  }
  expect((host.querySelector('[aria-label="靶点名称"]') as HTMLInputElement).value).toBe("")
  field("靶点名称", "BTK"); field("受体标识或路径", "BTK.cif"); field("链（auth chain）", "B")
  field("目标残基编号", "481"); field("反应类型", "Michael Addition"); field("残基类型", "CYS")
  field("预算上限（点券）", "50")
  host.querySelector("#screening-target-parameters form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))
  expect(prompts).toHaveLength(1)
  const parameters = JSON.parse(prompts[0].split("target_parameters=")[1])
  expect(parameters).toEqual({ target: "BTK", receptor: "BTK.cif", chain: "B", residue: "CYS", residue_number: 481,
    insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" })
})

function mountWithDashboard() {
  const host = document.createElement("div")
  document.body.append(host)
  cleanups.push(
    web.render(
      () =>
        subject.CovalentLaunchpad({
          onPrompt: () => {},
          dashboard: {
            schema_version: "1.2",
            tasks: [
              {
                id: "ligand-first-egfr-geometry",
                name: "EGFR 候选构象与独立评价",
                route: "ligand-first",
                status: "completed",
                progress: 100,
              },
              {
                id: "de-novo-toolchain",
                name: "De novo 生成器适配与烟雾测试",
                route: "de-novo",
                status: "ready",
                progress: 40,
              },
            ],
            library_first: {
              run_id: "20260901T000000-covindb-prescreen-test",
              state: "completed",
              target: "EGFR_C797",
              source: "CovalentInDB 2.0",
              processed: 10000,
              selected: 500,
              progress: 100,
              campaign_id: "egfr-c797-covindb-pilot-v1",
              next_stage: "carsidock",
              chain_terminal_state: "ready",
              quality_gates: {
                "prescreen:rdkit_parse_rate_ge_0_99": true,
                "prescreen:selected_warhead_mapping_rate_ge_0_98": true,
              },
              stages: [
                { id: "catalog", label: "CovalentInDB", status: "completed", count: 10000 },
                {
                  id: "carsidock",
                  label: "CarsiDock-Cov",
                  status: "pending",
                  input_count: 500,
                  run_id: "carsidock-plan-001",
                },
              ],
            },
          },
        }),
      host,
    ),
  )
  return host
}

describe("CovalentLaunchpad", () => {
  test("shows all three discovery routes and transfers the selected route into the composer", () => {
    const prompts: string[] = []
    const host = mount((value) => prompts.push(value))

    expect(host.querySelectorAll(".covalent-route-card")).toHaveLength(3)
    expect(host.textContent).toContain("Library-first")
    expect(host.textContent).toContain("Ligand-first")
    expect(host.textContent).toContain("De novo")
    expect(host.textContent).toContain("Candidate Passport")
    expect(host.textContent).toContain("DISCOVERY WORKBENCH")
    expect(host.textContent).toContain("INPUT")
    expect(host.textContent).toContain("OUTPUT")
    expect(host.querySelector('[aria-label="Discovery system status"]')).toBeTruthy()

    host.querySelector<HTMLButtonElement>('[data-route="ligand"]')?.click()
    expect(prompts).toHaveLength(1)
    expect(prompts[0]).toContain("Ligand-first")

    host.querySelector<HTMLButtonElement>('[data-route="library"]')?.click()
    expect(prompts).toHaveLength(2)
    expect(prompts[1]).toContain("请先确认本次靶点")
    expect(prompts[1]).not.toContain("EGFR C797")
    expect(prompts[1]).toContain("CovInDB 虚拟筛选库")
  })

  test("renders the persisted Library-first run, stage counts, and quality gates", () => {
    const host = mountWithDashboard()
    expect(host.textContent).toContain("Library-first 实时工作流")
    expect(host.textContent).toContain("EGFR_C797")
    expect(host.textContent).toContain("10,000")
    expect(host.textContent).toContain("候选 warhead 映射率 ≥98%")
    expect(host.textContent).toContain("CarsiDock-Cov")
    expect(host.textContent).toContain("待运行")
    expect(host.textContent).toContain("egfr-c797-covindb-pilot-v1")
    expect(host.textContent).toContain("NEXT carsidock")
    expect(host.querySelector('[data-route="ligand"]')?.textContent).toContain("已完成")
    expect(host.querySelector('[data-route="denovo"]')?.textContent).toContain("可启动")
  })
})
