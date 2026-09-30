import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Analysis } from "@/lib/types";
import { MatchDimensions } from "./MatchDimensions";

const analysis: Analysis = {
  id: "a", created_at: "2026-09-28T10:00:00Z", match_score: 64, role_match: "strong",
  skills_match: "medium", experience_match: "weak", location_match: "strong", salary_match: "unknown",
  freshness: "fresh", matching_skills: [], missing_skills: [], important_requirements: [], concerns: [],
  reason: "r", should_shortlist: false, shortlisted: false, rule_results: [], analyzer: "claude_cli",
  model: "m", prompt_version: "v1",
};

describe("MatchDimensions", () => {
  it("shows every dimension with a text level, not just a color", () => {
    render(<MatchDimensions analysis={analysis} />);
    expect(screen.getByText("64")).toBeInTheDocument();
    expect(screen.getByTitle("Experience: weak")).toBeInTheDocument();
    expect(screen.getByTitle("Salary: unknown")).toBeInTheDocument();
    expect(screen.getByText(": medium")).toBeInTheDocument(); // screen-reader text
  });

  it("says when a job has not been analyzed", () => {
    render(<MatchDimensions analysis={null} />);
    expect(screen.getByText("Not analyzed")).toBeInTheDocument();
  });
});
