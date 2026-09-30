import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { availableModes, canSubmit, failureText, liveBlockReason, modeChips, modeView, screenshotUrl, sessionSteps } from "@/lib/execution";
import { isAllowedProxyPath } from "@/lib/proxy";
import type { ApplicationDetail, BrowserSessionView, ExecutionModeStatus } from "@/lib/types";
import { ExecutionBanner } from "./ExecutionBanner";
import { ExecutionModeBar } from "./ExecutionModeBar";
import { LiveSubmit } from "./ExecutionControls";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const session = (over: Partial<BrowserSessionView> = {}): BrowserSessionView => ({
  id: "s1", mode: "DRY_RUN", banner: "", state: "READY_FOR_SUBMISSION", current_step: null,
  target_url: "http://127.0.0.1/jobs/normal", target_domain: "127.0.0.1", current_url: null,
  page_title: null, fields: [], filled_fields: [], skipped_fields: [], missing_answers: [],
  challenges: [], uploaded_resume: null, screenshots: [], errors: [], blocked_requests: 0,
  user_action_required: null, prepared_at: "2026-09-28T10:00:00Z", user_confirmed_at: null,
  submission_evidence: null, dry_run_complete: true, created_at: "2026-09-28T10:00:00Z",
  closed_at: null, ...over,
});

const app = (over: Partial<ApplicationDetail> = {}): ApplicationDetail => ({
  id: "a1", job_id: "j1", company: "Acme", title: "Backend Engineer", application_status: "DRAFT",
  job_status: "READY_FOR_SUBMISSION", requires_user_input: false, applied_at: null,
  updated_at: "2026-09-28T10:00:00Z", latest_session: null, url: "u", application_url: null,
  user_action_required: null, readiness: { ready: true, eligible: true, user_blockers: [], pending_steps: [] },
  external_live_enabled: false, development_mode: true, external_live_configured: false,
  live_block_reason: "DEVELOPMENT_MODE_BLOCKED", browser_headless: true, default_mode: "external_dry_run",
  confirmation_statement: "You are about to submit an application to Acme for Backend Engineer. This will perform an external action.",
  sessions: [], external_actions: [], submission_attempts: [], timeline: [], ...over,
});

describe("execution mode", () => {
  it("never makes LIVE look like a dry run", () => {
    expect(modeView("DRY_RUN").title).toBe("DRY RUN — Nothing has been submitted.");
    expect(modeView("LIVE").title).toBe("LIVE EXTERNAL ACTION");
    const { rerender } = render(<ExecutionBanner mode="DRY_RUN" />);
    expect(screen.getByRole("status")).toHaveTextContent("DRY RUN — Nothing has been submitted.");
    rerender(<ExecutionBanner mode="LIVE" />);
    expect(screen.getByRole("alert")).toHaveTextContent("LIVE EXTERNAL ACTION");
  });

  it("offers live mode only when the backend enables it", () => {
    expect(availableModes(false).map((m) => m.value)).not.toContain("external_live");
    expect(availableModes(true).map((m) => m.value)).toContain("external_live");
    expect(availableModes(false).map((m) => m.value)).toContain("external_dry_run");
  });

  it("a dry-run session can never be submitted from the dashboard", () => {
    expect(canSubmit(session(), true)).toBe(false);
    expect(canSubmit(session({ mode: "LIVE" }), false)).toBe(false);
    expect(canSubmit(session({ mode: "LIVE" }), true)).toBe(true);
    expect(canSubmit(session({ mode: "LIVE", state: "FILLING" }), true)).toBe(false);
  });

  it("describes workflow progress, with the final submit never done in a dry run", () => {
    const steps = sessionSteps(session({ fields: [{ field_id: "f0_0", label: "Email", kind: "email",
      required: true, key: "email", category: "VERIFIED_AUTOFILL", action: "fill", reason: "", answer_status: "VERIFIED", source: null }] }));
    expect(steps.at(-1)).toEqual({ label: "Final submit (never in a dry run)", done: false });
    expect(steps.find((s) => s.label.startsWith("Validated"))?.done).toBe(true);
  });

  it("routes screenshots through the allowed proxy", () => {
    const url = screenshotUrl("a1", "s1", 0);
    expect(url).toBe("/backend/api/applications/a1/sessions/s1/screenshots/0");
    expect(isAllowedProxyPath(url.replace("/backend/", ""))).toBe(true);
  });
});

describe("LiveSubmit", () => {
  it("renders nothing for dry runs or when live is disabled", () => {
    const { container } = render(<LiveSubmit app={app({ external_live_enabled: true })} session={session()} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<LiveSubmit app={app()} session={session({ mode: "LIVE" })} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it("requires an explicit in-page confirmation showing the exact statement", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<LiveSubmit app={app({ external_live_enabled: true })} session={session({ mode: "LIVE" })} />);
    fireEvent.click(screen.getByRole("button", { name: /submit application/i }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("You are about to submit an application to Acme for Backend Engineer. This will perform an external action.");
    const submit = screen.getByRole("button", { name: "Submit now" });
    expect(submit).toBeDisabled(); // nothing happens until the user ticks the box
    fireEvent.click(submit);
    expect(fetchSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(submit).toBeEnabled();
    fetchSpy.mockRestore();
  });
});

const status = (over: Partial<ExecutionModeStatus> = {}): ExecutionModeStatus => ({
  development_mode: true, external_live_configured: false, external_live_enabled: false,
  live_block_reason: "DEVELOPMENT_MODE_BLOCKED", browser_headless: true, default_mode: "external_dry_run", ...over,
});

describe("execution mode bar", () => {
  it("shows DRY RUN, DEVELOPMENT MODE and LIVE DISABLED in development", () => {
    expect(modeChips(status()).map((c) => c.label)).toEqual(["DRY RUN", "DEVELOPMENT MODE", "LIVE DISABLED"]);
    render(<ExecutionModeBar mode={status()} />);
    const bar = screen.getByRole("status", { name: "Execution mode" });
    for (const label of ["DRY RUN", "DEVELOPMENT MODE", "LIVE DISABLED"]) expect(bar).toHaveTextContent(label);
    expect(bar).not.toHaveTextContent("LIVE EXTERNAL ACTIONS ENABLED");
  });

  it("explains that DEVELOPMENT_MODE overrides a configured live flag", () => {
    expect(liveBlockReason(status({ external_live_configured: true }))).toMatch(/DEVELOPMENT_MODE=true blocks/);
    expect(modeChips(status({ external_live_configured: true })).map((c) => c.label)).toContain("LIVE DISABLED");
  });

  it("is loud and unmistakable when a production config enables live", () => {
    const live = status({ development_mode: false, external_live_configured: true, external_live_enabled: true, live_block_reason: null });
    expect(modeChips(live)).toEqual([{ label: "LIVE EXTERNAL ACTIONS ENABLED", tone: "live" }]);
    render(<ExecutionModeBar mode={live} />);
    expect(screen.getByRole("alert", { name: "Execution mode" })).toHaveTextContent("LIVE EXTERNAL ACTIONS ENABLED");
  });

  it("never hides the mode, even when the backend is offline", () => {
    expect(modeChips(null)[0].label).toMatch(/UNKNOWN/);
  });
});

describe("failed runs", () => {
  it("names the step that failed and says nothing was left running", () => {
    const text = failureText({ failed: true, failed_stage: "fill_application_field", error: "renderer crashed" });
    expect(text).toContain("Failed at fill application field: renderer crashed");
    expect(text).toContain("screenshot was saved");
    expect(failureText({ failed: true })).toContain("Failed at the browser run: unknown error");
  });
});
