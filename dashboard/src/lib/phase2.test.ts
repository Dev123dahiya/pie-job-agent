import { describe, expect, it, vi } from "vitest";
import { runMetrics, runProgress } from "@/components/agent/AgentActivity";
import { getJob, getJobs, jobsQueryString } from "./api";
import { formatSalary } from "./format";
import { isAllowedProxyPath } from "./proxy";
import type { RunInfo } from "./types";

describe("formatSalary", () => {
  it("shows INR in lakhs per annum and never invents a value", () => {
    expect(formatSalary(1_200_000, 1_800_000, "INR")).toBe("₹12–18 LPA");
    expect(formatSalary(4_408_400, 4_408_400, "INR")).toBe("₹44.1 LPA");
    expect(formatSalary(null, 1_000_000, "INR")).toBe("₹10 LPA");
    expect(formatSalary(null, null, null)).toBe("Not listed");
  });

  it("keeps other currencies as published (no conversion)", () => {
    expect(formatSalary(244_000, 305_000, "USD")).toBe("$244–305k");
    expect(formatSalary(50_000, 60_000, "SGD")).toBe("SGD 50–60k");
  });
});

describe("proxy path guard", () => {
  it("allows only PIE API paths", () => {
    expect(isAllowedProxyPath("api/jobs")).toBe(true);
    expect(isAllowedProxyPath("api/jobs/123/analyze")).toBe(true);
    expect(isAllowedProxyPath("api/agent/runs/abc/cancel")).toBe(true);
    expect(isAllowedProxyPath("docs")).toBe(false);
    expect(isAllowedProxyPath("api/../admin")).toBe(false);
    expect(isAllowedProxyPath("api/secrets")).toBe(false);
  });
});

describe("jobs API client", () => {
  it("forwards only known filter keys", () => {
    expect(jobsQueryString({ q: "backend", freshness: "fresh", evil: "x", sort: ["match", "newest"] })).toBe(
      "?q=backend&freshness=fresh&sort=match",
    );
    expect(jobsQueryString({})).toBe("");
  });

  it("reports an offline backend instead of inventing data", async () => {
    const res = await getJobs({}, vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    expect(res).toEqual({ ok: false, error: "The backend is not reachable." });
  });

  it("surfaces HTTP errors", async () => {
    const res = await getJob("nope", vi.fn().mockResolvedValue(new Response("{}", { status: 404 })));
    expect(res.ok).toBe(false);
    expect(!res.ok && res.error).toContain("404");
  });
});

const run = (over: Partial<RunInfo>): RunInfo => ({
  id: "r1", task: "t", run_type: "discovery", status: "RUNNING", source: "greenhouse",
  search_terms: [], locations: [], current_task: null, started_at: "2026-09-28T10:00:00Z",
  ended_at: null, searches_total: 8, searches_completed: 4, jobs_found: 42, jobs_new: 27,
  jobs_duplicate: 15, jobs_failed: 0, jobs_analyzed: 0, jobs_shortlisted: 0, jobs_rejected: 0,
  errors: [], summary: null, model: null, mode: null, iterations: 0, max_iterations: null,
  pending_user_input: null, ...over,
});

describe("agent activity", () => {
  it("describes discovery progress", () => {
    expect(runProgress(run({}))).toEqual({ label: "Searches completed", done: 4, total: 8 });
    expect(runMetrics(run({})).map((m) => m.value)).toEqual([42, 27, 15, 0]);
  });

  it("describes analysis progress", () => {
    const r = run({ run_type: "analysis", searches_total: 10, searches_completed: 3, jobs_analyzed: 3, jobs_shortlisted: 2, jobs_rejected: 1 });
    expect(runProgress(r).label).toBe("Jobs analyzed");
    expect(runMetrics(r).map((m) => m.label)).toEqual(["Analyzed", "Shortlisted", "Rejected", "Failed"]);
  });
});
