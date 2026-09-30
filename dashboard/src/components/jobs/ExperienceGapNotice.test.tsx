import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { experienceGapView } from "@/lib/resume";
import type { ExperienceGap } from "@/lib/types";
import { ExperienceGapNotice } from "./ExperienceGapNotice";

const gap = (over: Partial<ExperienceGap>): ExperienceGap => ({
  required: "2-5 years", required_source: "job.experience_required",
  required_min_years: 2, required_max_years: 5, candidate: "0.4 years", candidate_years: 0.4,
  candidate_basis: "Verified full-time experience", internships: ["ML Intern at Acme Labs (2024-01 - 2024-03)"],
  internships_counted_as_full_time: false, shortfall_years: 1.6, status: "gap", gap: true,
  note: "", ...over,
});

describe("experienceGapView", () => {
  it("states a gap plainly with the job's own wording", () => {
    const v = experienceGapView(gap({}));
    expect(v.label).toBe("Experience gap");
    expect(v.detail).toBe("Job requires 2-5 years. Verified full-time experience: 0.4 years.");
  });

  it("distinguishes meets, exceeds and unknown", () => {
    expect(experienceGapView(gap({ status: "meets", gap: false })).label).toBe("Meets experience requirement");
    expect(experienceGapView(gap({ status: "exceeds", gap: false })).label).toBe("Exceeds experience requirement");
    const unknown = experienceGapView(gap({ status: "unknown", gap: "unknown", required: null }));
    expect(unknown.label).toBe("Experience requirement unknown");
  });
});

describe("ExperienceGapNotice", () => {
  it("shows the gap, the internship rule and the tailoring/eligibility distinction", () => {
    render(<ExperienceGapNotice gap={gap({})} />);
    expect(screen.getByText("Experience gap")).toBeInTheDocument();
    expect(screen.getByText(/0\.4 years/)).toBeInTheDocument();
    expect(screen.getByText(/not counted as full-time experience/)).toBeInTheDocument();
    expect(screen.getByText(/Shortlist eligibility is decided separately/)).toBeInTheDocument();
    expect(screen.getByText(/never concealed/)).toBeInTheDocument();
  });
});
