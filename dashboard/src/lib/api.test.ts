import { describe, expect, it, vi } from "vitest";
import { getHealth, getOverview } from "./api";
import type { DashboardOverview } from "./types";

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const overview: DashboardOverview = {
  jobs_discovered_today: 42, jobs_shortlisted: 0, applications_submitted: 0, assessments: 0,
  interviews: 0, recruiter_responses: 0, offers: 0, rejections: 0, funnel: [], recent_activity: [],
  agent: { status: "IDLE", task: null, started_at: null, jobs_processed: 0,
           applications_submitted: 0, error_count: 0 },
};

describe("getOverview", () => {
  it("returns live data when the backend responds", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(overview));
    const result = await getOverview(fetchImpl);
    expect(result.ok && result.data.jobs_discovered_today).toBe(42);
    expect(fetchImpl.mock.calls[0][0]).toBe("http://localhost:8000/api/dashboard/overview");
  });

  it("never substitutes demo numbers when the backend is unreachable", async () => {
    const result = await getOverview(vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    expect(result).toEqual({ ok: false, error: "The backend is not reachable." });
  });

  it("reports backend errors instead of inventing data", async () => {
    const result = await getOverview(vi.fn().mockResolvedValue(jsonResponse({ detail: "boom" }, 500)));
    expect(result.ok).toBe(false);
  });
});

describe("getHealth", () => {
  it("returns null when the backend is down", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    expect(await getHealth(fetchImpl)).toBeNull();
  });
});
