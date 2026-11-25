import { test, expect } from "bun:test"
import path from "node:path"
import { study } from "@synsci/util/discovery-study"
import { studyExecutionContract } from "./study-execution-contract"
import { schema } from "../inspire-screening/executor/tool"

test("actual proposal is checked against executor schema and study inputs without create", async () => {
  const root = path.resolve(import.meta.dir, "../../../..")
  const plan = study.parse(await Bun.file(path.join(root, ".discovery/studies/study-e58f69ec-8a34-45dc-b8cc-900049265515/plan.json")).json())
  const proposal = await Bun.file(path.join(root, "results/target-hypotheses/hyp-8f4bc876d5e3430a85d8dd70/proposal-1b0ce2c8f9d3/hypothesis.json")).json()
  expect(schema.safeParse(proposal.handoff.next_call.arguments).success).toBe(true)
  const original = JSON.stringify(proposal)
  const missing = studyExecutionContract(plan, original)
  expect(missing.state).toBe("NEEDS_INPUTS_OR_ADAPTER")
  expect(missing.blockers.some(item => item.includes("library input"))).toBe(true)
  plan.routes["library-first"].inputs = { library: "CovInDB/Virtual_Screening_Library.csv", warhead: "acrylamide" }
  proposal.handoff.next_call.arguments.screening_mode = "test100"
  proposal.handoff.next_call.arguments.max_points = 1
  expect(studyExecutionContract(plan, JSON.stringify(proposal)).state).toBe("NEEDS_INPUTS_OR_ADAPTER")
  plan.routes["library-first"].planning = { screeningMode: "test100", maxPoints: 1 }
  expect(studyExecutionContract(plan, JSON.stringify(proposal)).state).toBe("CONTRACT_VALIDATED_NOT_COMPILED")
  proposal.handoff.next_call.arguments.max_points = 2
  expect(studyExecutionContract(plan, JSON.stringify(proposal)).blockers).toContain("Proposal scale or budget differs from the immutable study plan")
  delete proposal.handoff.next_call.arguments.max_points
  delete proposal.handoff.next_call.arguments.screening_mode
  const bound = studyExecutionContract(plan, JSON.stringify(proposal))
  expect(bound.parameters?.max_points).toBe(1)
  expect(bound.parameters?.screening_mode).toBe("test100")
  for (const target of [{ ...plan.target, taxon: 10090 }, { ...plan.target, uniprot: "Q01279" }]) {
    const changed = structuredClone(plan)
    changed.target = target
    const checked = studyExecutionContract(changed, JSON.stringify(proposal))
    expect(checked.state).toBe("NEEDS_INPUTS_OR_ADAPTER")
    expect(checked.blockers.some(item => item.includes("物种"))).toBe(true)
    expect(checked.submitted).toBe(false)
  }
  plan.target.gene = "BTK"
  plan.target.site.number = 481
  expect(studyExecutionContract(plan, JSON.stringify(proposal)).blockers.some(item => item.includes("differs"))).toBe(true)
  Object.assign(proposal.handoff.next_call.arguments, { target: "BTK", site: "C481" })
  Object.assign(proposal.handoff.next_call.arguments.target_parameters, { target: "BTK", residue_number: 481 })
  const changed = studyExecutionContract(plan, JSON.stringify(proposal))
  expect(changed.state).toBe("NEEDS_INPUTS_OR_ADAPTER")
  expect(changed.blockers.some(item => item.includes("不会回退"))).toBe(true)
  expect(changed.submitted).toBe(false)
  proposal.handoff.next_call.arguments.library = "other-library.csv"
  expect(schema.safeParse(proposal.handoff.next_call.arguments).success).toBe(false)
  expect(studyExecutionContract(plan, JSON.stringify(proposal)).state).toBe("CONTRACT_REJECTED")
})
