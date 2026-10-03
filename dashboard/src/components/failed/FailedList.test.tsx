import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { failedScreenshotUrl, type FailedJob } from "@/lib/failed";
import { isAllowedProxyPath } from "@/lib/proxy";
import { FailedList } from "./FailedList";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const f = (over: Partial<FailedJob> = {}): FailedJob => ({
  job_id: "j1", title: "Backend Engineer", company: "Acme", url: "https://x.invalid", error: "No application form",
  stage: "inspect_application_form", failed_at: null, screenshot: "s.png", retries: 0,
  application_id: "a1", session_id: "s1", screenshot_index: 3, ...over,
});

describe("Failed page", () => {
  it("builds a proxied screenshot URL only when there is one", () => {
    const url = failedScreenshotUrl(f());
    expect(url).toBe("/backend/api/applications/a1/sessions/s1/screenshots/3");
    expect(isAllowedProxyPath(url!.replace("/backend/", ""))).toBe(true);
    expect(failedScreenshotUrl(f({ screenshot_index: null }))).toBeNull();
  });

  it("shows error and stage, and retry-selected needs a selection", () => {
    render(<FailedList items={[f(), f({ job_id: "j2", title: "API Developer" })]} />);
    expect(screen.getAllByText("No application form")).toHaveLength(2);
    const retrySelected = screen.getByRole("button", { name: /Retry selected/ });
    expect(retrySelected).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Select API Developer"));
    expect(retrySelected).toBeEnabled();
    expect(retrySelected).toHaveTextContent("(1)");
  });

  it("has an empty state", () => {
    render(<FailedList items={[]} />);
    expect(screen.getByText("No failed jobs.")).toBeInTheDocument();
  });
});
