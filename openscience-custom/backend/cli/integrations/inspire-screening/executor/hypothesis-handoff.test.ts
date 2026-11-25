import { expect, test } from "bun:test"
import path from "node:path"
import { targetAdmission } from "./target"
import { schema } from "./tool"

test("registered hypothesis profile uses the real platform screening contract", async () => {
  const registry = await Bun.file(path.resolve(import.meta.dir,
    "../../../../../../science/configs/target_hypothesis_routes.json")).json()
  const profile = registry.profiles.find((item: { route: string }) => item.route === "library-first")
  expect(targetAdmission(profile.target_parameters).supported).toBe(true)
  expect(schema.safeParse({ action: "create", request: "EGFR C797 共价筛选",
    target: profile.target, site: profile.site, target_parameters: profile.target_parameters,
    library: "CovInDB/Virtual_Screening_Library.csv" }).success).toBe(true)
})
