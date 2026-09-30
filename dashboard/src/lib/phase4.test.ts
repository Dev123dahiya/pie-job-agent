import { describe, expect, it } from "vitest";
import { isAllowedProxyPath } from "./proxy";
import { TAILORABLE_STATES, artifactUrl, describeChanges, readinessView } from "./resume";
import type { ChangeDetails } from "./types";

const base: ChangeDetails = {
  sections_changed: [],
  section_order: [],
  summary_changed: false,
  skills_reordered: false,
  skills_added: [],
  skills_removed: [],
  projects_reordered: false,
  projects_removed: [],
  experience_reordered: false,
  bullets_changed: 0,
  keywords_emphasized: [],
  unsupported_claims_rejected: [],
  facts_preserved: true,
  notes: [],
};

describe("describeChanges", () => {
  it("reports no structural changes for an unchanged resume", () => {
    expect(describeChanges(base)).toEqual(["No structural changes"]);
  });

  it("describes reordering, removals, rewording and rejected claims", () => {
    const lines = describeChanges({
      ...base,
      summary_changed: true,
      skills_reordered: true,
      skills_removed: ["Excel"],
      bullets_changed: 1,
      projects_removed: ["Leave Portal"],
      unsupported_claims_rejected: [{ code: "UNCONFIRMED_SKILL", detail: "Go" }],
    });
    expect(lines).toContain("Summary rewritten for this role");
    expect(lines).toContain("Skills reordered by relevance");
    expect(lines).toContain("Skills left out: Excel");
    expect(lines).toContain("1 experience bullet reworded");
    expect(lines).toContain("Projects left out: Leave Portal");
    expect(lines).toContain("1 unsupported claim(s) rejected before saving");
  });
});

describe("readinessView", () => {
  const r = { ready: false, eligible: true, user_blockers: [], pending_steps: [] };

  it("marks non-shortlisted jobs as not eligible", () => {
    expect(readinessView(null, false).label).toMatch(/Not eligible/);
    expect(readinessView({ ...r, eligible: false }, true).status).toBe("neutral");
  });

  it("shows ready, blocked and in-preparation states", () => {
    expect(readinessView({ ...r, ready: true }, true).status).toBe("READY_TO_APPLY");
    expect(readinessView({ ...r, user_blockers: ["Missing notice period"] }, true).status).toBe("BLOCKED_NEEDS_USER");
    expect(readinessView({ ...r, pending_steps: ["Create a tailored resume"] }, true).label).toBe("In preparation");
    expect(readinessView(r, false).label).toBe("No draft yet");
  });
});

describe("resume links and states", () => {
  it("builds proxied artifact URLs that the proxy guard allows", () => {
    const url = artifactUrl("0d2c5f4e-1111-2222-3333-444455556666", "pdf");
    expect(url).toBe("/backend/api/resumes/versions/0d2c5f4e-1111-2222-3333-444455556666/artifact?format=pdf");
    expect(isAllowedProxyPath("api/resumes/versions/0d2c5f4e-1111-2222-3333-444455556666/artifact")).toBe(true);
    expect(isAllowedProxyPath("api/resumes/master")).toBe(true);
  });

  it("never offers tailoring for duplicate, withdrawn or submitted jobs", () => {
    expect(TAILORABLE_STATES.has("SHORTLISTED")).toBe(true);
    for (const s of ["DUPLICATE", "WITHDRAWN", "SUBMITTED", "DISCOVERED"]) {
      expect(TAILORABLE_STATES.has(s)).toBe(false);
    }
  });
});
