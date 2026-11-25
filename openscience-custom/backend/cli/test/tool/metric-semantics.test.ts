import { expect, test } from "bun:test"
import { interpretMetric, metricInterpretation, prereactionGeometry, mdMetricDefinitions, reportedNumber } from "@synsci/util/metric-semantics"

test("metric interpretation requires exact stage, definition and unit without rewriting historical values", () => {
  for (const metric of prereactionGeometry) {
    const matched = metricInterpretation.parse(interpretMetric(metric, "prereaction"))
    expect(matched.state).toBe("registered")
    expect(interpretMetric(metric, "adduct").state).toBe("definition_mismatch")
    expect(interpretMetric(metric).state).toBe("definition_mismatch")
    expect(interpretMetric({ ...metric, unit: "nm" }, "prereaction").state).toBe("definition_mismatch")
    expect(interpretMetric({ ...metric, definition: "legacy angle" }, "prereaction").state).toBe("definition_mismatch")
    expect(matched).not.toHaveProperty("threshold")
  }
  expect(interpretMetric({ id: "historical_vendor_score" }).state).toBe("unregistered")
})

test("MD contracts preserve percent conversion and replica-specific definitions without declaring convergence", () => {
  for (const count of [1, 2, 3]) {
    const definitions = mdMetricDefinitions("ligand-first", count)
    for (const entry of definitions) {
      const interpretation = metricInterpretation.parse(interpretMetric(entry, "md"))
      expect(interpretation.state).toBe("registered")
      expect(interpretMetric(entry, "adduct").state).toBe("definition_mismatch")
    }
    const contact = definitions.find(entry => entry.key === "initial_contact_retention_excluding_tether")!
    expect(0.7 * contact.scale).toBe(70)
    expect(interpretMetric({ ...contact, unit: "fraction" }, "md").state).toBe("definition_mismatch")
  }
  expect(mdMetricDefinitions("library-first", 3)).toHaveLength(3)
  expect(() => mdMetricDefinitions("library-first", 1)).toThrow("coverage")
  expect(() => mdMetricDefinitions("ligand-first", 0)).toThrow("coverage")
  expect(interpretMetric({ ...mdMetricDefinitions("library-first", 3)[0], definition: "Last 2 ns only" }, "md").state)
    .toBe("definition_mismatch")
})

test("missing numeric fields remain unknown while a measured zero remains zero", () => {
  for (const value of [undefined, null, "", "  ", "NaN", "unknown", Infinity, true, [], {}])
    expect(reportedNumber(value)).toBeNull()
  for (const value of [0, "0", "0.0"])
    expect(reportedNumber(value)).toBe(0)
  expect(reportedNumber("2.5e-2")).toBe(0.025)
})
