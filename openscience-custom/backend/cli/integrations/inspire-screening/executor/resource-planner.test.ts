import { expect, test } from "bun:test"
import { choose, estimate, minutes, policy } from "./resource-planner"

test("uncapped test100 retains finite scale and deadlines without downgrading", () => {
  expect(policy("test100")).toMatchObject({ shortlist: 100, max_records: 100000 })
  expect(minutes(policy("test100"))).toEqual({ prepare: 60, carsidock: 120, rtmscore: 60, report: 10 })
  expect(choose({ screening_mode: "test100", max_points: null }, { cpu: 2, gpu: 100 })).toMatchObject({
    selected: "test100", max_points: null, requires_confirmation: false, adjusted: false,
  })
  expect(choose({ screening_mode: "test100", max_points: 0 }, { cpu: 2, gpu: 100 })).toMatchObject({
    selected: "test100", requires_confirmation: true, reason: "minimum_budget_required",
  })
  expect(() => choose({ screening_mode: "standard", max_points: null }, { cpu: 0, gpu: 1 })).toThrow()
  expect(() => choose({ screening_mode: "test100", requested_mode: "deep", max_points: null }, { cpu: 0, gpu: 1 })).toThrow()
})

test("versioned modes derive exact scientific scope and stage reservations", () => {
  expect(policy("economy")).toEqual({ scope: "bounded", max_records: 2000, shortlist: 3,
    prepare_minutes: 15, carsidock_minutes: 10, rtmscore_minutes: 5, report_minutes: 5 })
  expect(minutes(policy("standard"))).toEqual({ prepare: 30, carsidock: 20, rtmscore: 10, report: 5 })
  expect(policy("deep")).toMatchObject({ max_records: 50000, shortlist: 20 })
})

test("research pilot v1 freezes 20k/5 with fifteen total single-GPU minutes and a finite cap", () => {
  const profile = policy("research_pilot_v1")
  expect(profile).toEqual({ scope: "bounded", max_records: 20000, shortlist: 5,
    prepare_minutes: 15, carsidock_minutes: 10, rtmscore_minutes: 5, report_minutes: 5 })
  expect(profile.carsidock_minutes + profile.rtmscore_minutes).toBe(15)
  expect(choose({ screening_mode: "research_pilot_v1", max_points: 1 }, { cpu: 0.3, gpu: 2 })).toMatchObject({
    requested: "research_pilot_v1", selected: "research_pilot_v1", estimated_points: 0.6,
    adjusted: false, requires_confirmation: false,
  })
  expect(choose({ screening_mode: "research_pilot_v1", max_points: 0.01 }, { cpu: 0.3, gpu: 2 })).toMatchObject({
    selected: "research_pilot_v1", profile, adjusted: false, requires_confirmation: true, reason: "minimum_budget_required",
  })
  expect(() => choose({ screening_mode: "research_pilot_v1", max_points: null }, { cpu: 0, gpu: 0 })).toThrow()
  expect(() => choose({ screening_mode: "economy", requested_mode: "research_pilot_v1", max_points: 10 }, { cpu: 0, gpu: 1 })).toThrow("cannot be substituted")
  expect(() => choose({ screening_mode: "research_pilot_v1", requested_mode: "deep", max_points: 10 }, { cpu: 0, gpu: 1 })).toThrow("cannot be substituted")
})

test("live quota rates determine cost while a fitting requested mode remains unchanged", () => {
  expect(choose({ screening_mode: "standard", max_points: 0.5 }, { cpu: 0, gpu: 1 })).toMatchObject({
    requested: "standard", selected: "standard", estimated_points: 0.5, adjusted: false,
    reason: "requested_mode_fits", requires_confirmation: false,
  })
  expect(estimate(policy("standard"), { cpu: 0.2, gpu: 2 })).toBeCloseTo(1.1166666667)
})

test("budget never silently downgrades the requested screening mode", () => {
  expect(choose({ screening_mode: "deep", max_points: 0.5 }, { cpu: 0, gpu: 1 })).toMatchObject({
    requested: "deep", selected: "standard", estimated_points: 0.5, adjusted: true,
    reason: "lower_mode_fits", requires_confirmation: true,
  })
  expect(choose({ screening_mode: "economy", max_points: 0.2 }, { cpu: 0, gpu: 1 })).toMatchObject({
    requested: "economy", selected: "economy", estimated_points: 0.25, adjusted: true,
    reason: "minimum_budget_required", requires_confirmation: true,
  })
})

test("an explicitly confirmed lower mode preserves the originally requested mode", () => {
  expect(choose({ screening_mode: "standard", requested_mode: "deep", max_points: 0.5 }, { cpu: 0, gpu: 1 })).toMatchObject({
    requested: "deep", selected: "standard", adjusted: true, requires_confirmation: false,
  })
  expect(() => choose({ screening_mode: "standard", max_points: 1 }, { cpu: -1, gpu: 1 })).toThrow()
})
