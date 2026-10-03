import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { isAllowedProxyPath } from "@/lib/proxy";
import type { ReviewItem, RunOptions } from "@/lib/runs";
import { ReviewQueue } from "@/components/review/ReviewQueue";
import { StartRunForm } from "./StartRunForm";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const options = (live: boolean): RunOptions => ({
  steps: ["discover", "analyze", "tailor", "apply"],
  platforms: [
    { platform: "naukri", apply_supported: false, review_confirmation: { platform: "naukri", remaining: 15, phrase: "LIVE on naukri: up to 15 today" } },
    { platform: "company_site", apply_supported: true, review_confirmation: { platform: "company_site", remaining: 10, phrase: "LIVE on company_site: up to 10 today" } },
  ],
  modes: ["dry-run", "review"], default_mode: "dry-run", live_possible: live,
  live_note: live ? null : "DEVELOPMENT_MODE is true", max_analysis: 50,
});

describe("Start run form", () => {
  it("pre-selects dry run and blocks review while live is off", () => {
    render(<StartRunForm options={options(false)} busyRun={false} />);
    expect(screen.getByLabelText(/Dry run/)).toBeChecked();
    expect(screen.getByLabelText(/Review \(live\)/)).toBeDisabled();
    expect(screen.getByRole("button", { name: /Start run/ })).toBeEnabled();
  });

  it("review needs the exact phrase naming the platform and remaining limit", () => {
    render(<StartRunForm options={options(true)} busyRun={false} />);
    fireEvent.click(screen.getByLabelText(/Review \(live\)/));
    const start = screen.getByRole("button", { name: /Start run/ });
    expect(screen.getByText("LIVE on company_site: up to 10 today")).toBeInTheDocument();
    expect(start).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Live confirmation"), { target: { value: "yes" } });
    expect(start).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Live confirmation"), { target: { value: "LIVE on company_site: up to 10 today" } });
    expect(start).toBeEnabled();
  });

  it("refuses applying on a platform that is not built", () => {
    render(<StartRunForm options={options(false)} busyRun={false} />);
    fireEvent.click(screen.getByLabelText(/Naukri/));
    expect(screen.getByRole("button", { name: /Start run/ })).toBeDisabled();
  });

  it("can't start while another run is in progress", () => {
    render(<StartRunForm options={options(false)} busyRun />);
    expect(screen.getByRole("button", { name: /Start run/ })).toBeDisabled();
  });
});

describe("Review queue", () => {
  const item: ReviewItem = {
    session_id: "s1", application_id: "a1", job_id: "j1", title: "Backend Engineer", company: "Acme",
    platform: "company_site", url: "https://x.invalid", match_score: 81, match_reason: "Strong match",
    resume: { version_id: "v1", version_number: 2 }, uploaded_resume: "cv.pdf", prepared_at: null,
    answers: [
      { label: "Notice period", key: "notice_period", source: "profile", ai_suggested: false },
      { label: "Why us?", key: "q_why", source: "Claude (from your resume)", ai_suggested: true },
    ],
    confirmation_statement: "You are about to submit an application to Acme for Backend Engineer.",
  };

  it("shows resume, answer sources and marks AI-suggested answers", () => {
    render(<ReviewQueue items={[item]} />);
    expect(screen.getByText(/Filled from: notice_period/)).toBeInTheDocument();
    expect(screen.getAllByText("AI-suggested")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Tailored v2" })).toHaveAttribute("href", "/resumes/v1");
    expect(screen.getByRole("button", { name: /Approve selected \(0\)/ })).toBeDisabled();
  });

  it("has an empty state and proxy access", () => {
    render(<ReviewQueue items={[]} />);
    expect(screen.getByText(/Nothing is waiting for review/)).toBeInTheDocument();
    expect(isAllowedProxyPath("api/review/approve-batch")).toBe(true);
    expect(isAllowedProxyPath("api/runs/options")).toBe(true);
  });
});
