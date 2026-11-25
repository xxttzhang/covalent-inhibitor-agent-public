import { expect, test } from "bun:test"
import { parseTargetParameters, targetCommand, targetParameters, targetSite } from "@synsci/util/screening-target"
import { explicitConditions } from "./intake"
import { assertTargetBinding, targetAdmission } from "./target"

const btk = targetParameters.parse({ target: "BTK", receptor: "BTK-prepared.cif", chain: "B", residue: "CYS",
  residue_number: 481, insertion_code: "", numbering: "canonical", reaction_type: "Michael Addition" })

test("task form serializes exact target/chain/site/chemistry without EGFR defaults", () => {
  const text = targetCommand(btk, "standard", 50)
  expect(parseTargetParameters(text)).toEqual(btk)
  expect(explicitConditions(text)).toEqual({ target: "BTK", site: "C481", target_parameters: btk,
    library: "CovInDB/Virtual_Screening_Library.csv", screening_mode: "standard", max_points: 50 })
  expect(targetSite({ ...btk, residue: "SER", residue_number: 195, insertion_code: "A" })).toBe("S195A")
  expect(() => explicitConditions("EGFR C797\n" + text)).toThrow("冲突")
  expect(() => parseTargetParameters(text + "\n" + text)).toThrow()
  expect(() => parseTargetParameters('target_parameters={"target":"BTK"}')).toThrow()
  expect(() => targetParameters.parse({ ...btk, residue_number: 1.5 })).toThrow()
})

test("unregistered targets/reactions/chains stop instead of falling back to the installed EGFR adapter", () => {
  const egfr = { ...btk, target: "EGFR", receptor: "1M17", chain: "A", residue_number: 797 }
  expect(targetAdmission(egfr).supported).toBe(true)
  for (const parameters of [btk, { ...egfr, chain: "B" }, { ...egfr, numbering: "structure" as const },
    { ...egfr, residue: "SER" }, { ...egfr, reaction_type: "Nucleophilic Substitution" }, { ...egfr, receptor: "other.cif" }]) {
    expect(targetAdmission(parameters).supported).toBe(false)
    expect(() => assertTargetBinding(parameters.target, targetSite(parameters), parameters)).toThrow()
  }
  expect(() => assertTargetBinding("BTK", "C481", egfr)).toThrow()
})
