import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DailySchedule } from "@/lib/types";
import { DailyRunCard } from "./DailyRunCard";

const counts = {
  discovered: 40, new: 12, duplicates: 28, fresh: 9, stale: 20, unknown: 11,
  analyzed: 9, shortlisted: 1, rejected: 8, ready_to_apply: 0, failures: 1, new_fresh_jobs: 4,
};

function schedule(overrides: Partial<DailySchedule> = {}): DailySchedule {
  return {
    enabled: true,
    time: "07:00",
    timezone: "Asia/Kolkata",
    next_run_at: new Date(Date.now() + 3_600_000).toISOString(),
    latest: {
      run_id: "r1",
      status: "COMPLETED",
      trigger: "scheduled",
      started_at: new Date().toISOString(),
      ended_at: new Date().toISOString(),
      summary: "Completed.",
      report: {
        sources: ["greenhouse", "adzuna"],
        counts,
        per_source: {},
        analysis: null,
        failures: [{ step: "discover:adzuna", error: "SourceTimeout: timed out" }],
        newly_shortlisted: [
          { job_id: "j1", title: "Software Engineer I", company: "Acme", location: "Noida", posted_at: null, url: "https://x.invalid", state: "SHORTLISTED" },
        ],
        newly_ready_to_apply: [],
      },
    },
    ...overrides,
  };
}

describe("DailyRunCard", () => {
  it("shows the schedule, the counts and newly shortlisted jobs", () => {
    render(<DailyRunCard daily={{ ok: true, data: schedule() }} />);
    expect(screen.getByText(/Every day at 07:00 \(Asia\/Kolkata\)/)).toBeInTheDocument();
    expect(screen.getByText("Unknown date").nextSibling).toHaveTextContent("11");
    expect(screen.getByText("Ready to apply").nextSibling).toHaveTextContent("0");
    expect(screen.getByText("Fresh postings").nextSibling).toHaveTextContent("9");
    expect(screen.getByText("New fresh jobs").nextSibling).toHaveTextContent("4");
    expect(screen.getByRole("link", { name: "Software Engineer I" })).toHaveAttribute("href", "/jobs/j1");
    expect(screen.getByText("Newly ready to apply (0)")).toBeInTheDocument();
    expect(screen.getByText(/SourceTimeout/)).toBeInTheDocument();
    expect(screen.getByText(/nothing is applied to/)).toBeInTheDocument();
  });

  it("says when the schedule is off and no run happened", () => {
    render(<DailyRunCard daily={{ ok: true, data: schedule({ enabled: false, next_run_at: null, latest: null }) }} />);
    expect(screen.getByText(/Schedule off/)).toBeInTheDocument();
    expect(screen.getByText(/No daily run yet/)).toBeInTheDocument();
  });

  it("shows the backend error instead of numbers", () => {
    render(<DailyRunCard daily={{ ok: false, error: "The backend is not reachable." }} />);
    expect(screen.getByText("The backend is not reachable.")).toBeInTheDocument();
  });
});
