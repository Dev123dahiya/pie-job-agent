import type { ChangeDetails, ExperienceGap, Readiness } from "./types";

/** Analyzed job states the backend accepts for tailoring (primarily SHORTLISTED). */
export const TAILORABLE_STATES = new Set([
  "ANALYZED",
  "SHORTLISTED",
  "READY_TO_APPLY",
  "BLOCKED_NEEDS_USER",
  "REJECTED_BY_AGENT",
]);

/** Plain-language lines for a stored (deterministic) change summary. No reasoning is shown. */
export function describeChanges(c: ChangeDetails): string[] {
  const lines: string[] = [];
  if (c.summary_changed) lines.push("Summary rewritten for this role");
  if (c.skills_reordered) lines.push("Skills reordered by relevance");
  if (c.skills_removed.length) lines.push(`Skills left out: ${c.skills_removed.join(", ")}`);
  if (c.skills_added.length) lines.push(`Skills surfaced from verified projects: ${c.skills_added.join(", ")}`);
  if (c.experience_reordered) lines.push("Experience reordered");
  if (c.bullets_changed) lines.push(`${c.bullets_changed} experience bullet${c.bullets_changed === 1 ? "" : "s"} reworded`);
  if (c.projects_reordered) lines.push("Projects reordered");
  if (c.projects_removed.length) lines.push(`Projects left out: ${c.projects_removed.join(", ")}`);
  if (c.unsupported_claims_rejected.length) {
    lines.push(`${c.unsupported_claims_rejected.length} unsupported claim(s) rejected before saving`);
  }
  return lines.length ? lines : ["No structural changes"];
}

/** Download link for a stored artifact, served through the dashboard's backend proxy. */
export function artifactUrl(versionId: string, format: "pdf" | "markdown"): string {
  return `/backend/api/resumes/versions/${encodeURIComponent(versionId)}/artifact?format=${format}`;
}

export type ReadinessView = { status: string; label: string };

/** Badge for the draft-readiness state of a job. */
export function readinessView(r: Readiness | null, hasDraft: boolean): ReadinessView {
  if (!r || !r.eligible) return { status: "neutral", label: "Not eligible (not shortlisted)" };
  if (r.ready) return { status: "READY_TO_APPLY", label: "Ready to apply (internal)" };
  if (r.user_blockers.length) return { status: "BLOCKED_NEEDS_USER", label: "Needs your input" };
  return { status: "info", label: hasDraft ? "In preparation" : "No draft yet" };
}

export type ExperienceGapView = { status: string; label: string; detail: string };

/** Plain statement of the experience gap. It is shown as-is: never softened or hidden. */
export function experienceGapView(g: ExperienceGap): ExperienceGapView {
  const candidate = `Verified full-time experience: ${g.candidate}`;
  if (g.gap === true) {
    return {
      status: "BLOCKED_NEEDS_USER",
      label: "Experience gap",
      detail: `Job requires ${g.required}. ${candidate}.`,
    };
  }
  if (g.gap === "unknown") {
    return {
      status: "unknown",
      label: "Experience requirement unknown",
      detail: `The posting states no clear experience requirement. ${candidate}.`,
    };
  }
  return {
    status: "ok",
    label: g.status === "exceeds" ? "Exceeds experience requirement" : "Meets experience requirement",
    detail: `Job requires ${g.required}. ${candidate}.`,
  };
}
