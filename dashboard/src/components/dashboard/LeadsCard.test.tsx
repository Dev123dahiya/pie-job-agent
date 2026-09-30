import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LeadSummary } from "@/lib/types";
import { LeadsCard } from "./LeadsCard";

function summary(overrides: Partial<LeadSummary> = {}): LeadSummary {
  return {
    enabled: false,
    auto_activate_verified: false,
    max_requests_per_run: 60,
    max_requests_per_company: 8,
    max_companies_per_run: 25,
    negative_cache_days: 14,
    leads_total: 356,
    companies_total: 200,
    leads_by_status: { NEW: 314, NO_BOARD: 42 },
    boards_by_status: { CANDIDATE: 1, VERIFIED: 0, REJECTED: 0, NOT_FOUND: 7 },
    negative_cache_active: 7,
    boards_needing_review: [
      { company: "AgileEngine", platform: "smartrecruiters", board_id: "AgileEngine", status: "CANDIDATE",
        active: false, configured: false, rule: "board_name", leads: 16 },
    ],
    active_boards: [],
    ...overrides,
  };
}

describe("LeadsCard", () => {
  it("shows state, review queue and that leads are not jobs", () => {
    render(<LeadsCard leads={{ ok: true, data: summary() }} />);
    expect(screen.getByText(/Off \(lead_resolution.enabled=false\)/)).toBeInTheDocument();
    expect(screen.getByText(/Leads are not jobs/)).toBeInTheDocument();
    expect(screen.getByText(/356 leads from 200 companies · 7 companies/)).toBeInTheDocument();
    expect(screen.getByText("Waiting").nextSibling).toHaveTextContent("314");
    expect(screen.getByText("No board").nextSibling).toHaveTextContent("42");
    expect(screen.getByText("Matched").nextSibling).toHaveTextContent("0");
    expect(screen.getByText("smartrecruiters:AgileEngine")).toBeInTheDocument();
    expect(screen.getByText("Boards needing your review (1)")).toBeInTheDocument();
    expect(screen.getByText("No board approved yet.")).toBeInTheDocument();
  });

  it("flags auto-activation and shows budgets when on", () => {
    render(<LeadsCard leads={{ ok: true, data: summary({ enabled: true, auto_activate_verified: true }) }} />);
    expect(screen.getByText(/On · 60 requests\/run, 8\/company/)).toBeInTheDocument();
    expect(screen.getByText(/auto-activation ON/)).toBeInTheDocument();
  });

  it("shows the backend error instead of numbers", () => {
    render(<LeadsCard leads={{ ok: false, error: "The backend is not reachable." }} />);
    expect(screen.getByText("The backend is not reachable.")).toBeInTheDocument();
  });
});
