import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { JobSummary } from "@/lib/types";
import { JobsTable } from "./JobsTable";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const job = (over: Partial<JobSummary>): JobSummary => ({
  id: "1", source: "greenhouse", company: "Acme", title: "Software Engineer", location: "Gurugram",
  remote_type: "unknown", salary_min: null, salary_max: null, salary_currency: null,
  experience_required: null, posted_at: null, discovered_at: "2026-09-28T10:00:00Z",
  freshness: "unknown", status: "DISCOVERED", url: "https://example.invalid/1", times_seen: 1,
  last_error: null, analysis: null, tailored_version: null, ...over,
});

describe("JobsTable", () => {
  it("offers analysis only where the backend allows it", () => {
    render(<JobsTable emptyMessage="none" jobs={[
      job({ id: "a", title: "Waiting job" }),
      job({ id: "b", title: "Submitted job", status: "SUBMITTED" }),
    ]} />);
    expect(screen.getAllByRole("button", { name: /analyze/i })).toHaveLength(1);
    expect(screen.getAllByText("Not listed")).toHaveLength(2); // salary is never guessed
    expect(screen.getAllByText("No post date")).toHaveLength(2);
  });

  it("shows the tailored resume version when one exists", () => {
    render(<JobsTable emptyMessage="none" jobs={[job({ id: "c", status: "SHORTLISTED", tailored_version: 2 })]} />);
    expect(screen.getByText("Tailored resume v2")).toBeInTheDocument();
  });

  it("shows provenance: post-date kind, other sources and undisclosed salary", () => {
    render(<JobsTable emptyMessage="none" jobs={[job({
      id: "p", posted_at: "2026-09-28T08:00:00Z", posted_at_kind: "created", also_on: ["ashby"],
      salary_note: "As stated: ₹8L - ₹12L",
    })]} />);
    expect(screen.getByText("created (published on or after)")).toBeInTheDocument();
    expect(screen.getByText("also on ashby")).toBeInTheDocument();
    expect(screen.getByText("Not listed")).toBeInTheDocument(); // no number is invented
    expect(screen.getByText("As stated: ₹8L - ₹12L")).toBeInTheDocument();
  });

  it("shows an empty state", () => {
    render(<JobsTable jobs={[]} emptyMessage="No jobs match these filters." />);
    expect(screen.getByText("No jobs match these filters.")).toBeInTheDocument();
  });
});
