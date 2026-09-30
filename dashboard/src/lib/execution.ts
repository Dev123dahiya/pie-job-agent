import type { BrowserSessionView, ExecutionModeName, ExecutionModeStatus } from "./types";

export type ModeChip = { label: string; tone: "live" | "warning" | "safe" };

/** What the dashboard shows before any execution. The mode is never hidden. */
export function modeChips(m: ExecutionModeStatus | null): ModeChip[] {
  if (!m) return [{ label: "EXECUTION MODE UNKNOWN (backend offline)", tone: "warning" }];
  if (m.external_live_enabled) return [{ label: "LIVE EXTERNAL ACTIONS ENABLED", tone: "live" }];
  const chips: ModeChip[] = [{ label: "DRY RUN", tone: "safe" }];
  if (m.development_mode) chips.push({ label: "DEVELOPMENT MODE", tone: "warning" });
  chips.push({ label: "LIVE DISABLED", tone: "safe" });
  return chips;
}

/** Why live is off, in plain words. */
export function liveBlockReason(m: ExecutionModeStatus): string | null {
  if (m.external_live_enabled) return null;
  if (m.development_mode && m.external_live_configured) {
    return "EXTERNAL_LIVE_ENABLED is set, but DEVELOPMENT_MODE=true blocks live actions.";
  }
  if (m.development_mode) return "DEVELOPMENT_MODE=true: live external actions are blocked.";
  return "EXTERNAL_LIVE_ENABLED=false: live external actions are disabled.";
}

export const DRY_RUN_BANNER = "DRY RUN — Nothing has been submitted.";
export const LIVE_BANNER = "LIVE EXTERNAL ACTION";

export type ModeView = { live: boolean; title: string; detail: string };

/** The execution banner. LIVE must never look like a dry run (or the reverse). */
export function modeView(mode: "DRY_RUN" | "LIVE"): ModeView {
  return mode === "LIVE"
    ? {
        live: true,
        title: LIVE_BANNER,
        detail: "This session acts on the employer's real website. Submission still needs your explicit confirmation.",
      }
    : {
        live: false,
        title: DRY_RUN_BANNER,
        detail: "PIE fills and checks the form, then stops before the final submit. Outgoing form posts are blocked.",
      };
}

export const AGENT_MODES: { value: ExecutionModeName; label: string; external: boolean }[] = [
  { value: "read_only", label: "Read only", external: false },
  { value: "internal_writes", label: "Internal writes", external: false },
  { value: "external_dry_run", label: "External: DRY RUN", external: true },
  { value: "external_live", label: "External: LIVE", external: true },
];

/** Live is offered only when the backend says it is enabled. */
export function availableModes(liveEnabled: boolean) {
  return AGENT_MODES.filter((m) => m.value !== "external_live" || liveEnabled);
}

export type StepView = { label: string; done: boolean };

/** Where a browser session is in the dry-run / live workflow. */
export function sessionSteps(s: BrowserSessionView): StepView[] {
  const detected = s.fields.length > 0;
  const filled = s.filled_fields.length > 0;
  return [
    { label: "Application page opened", done: s.state !== "CREATED" && s.state !== "NAVIGATING" },
    { label: `Form detected (${s.fields.length} fields)`, done: detected },
    { label: `Fields filled (${s.filled_fields.length})`, done: filled },
    { label: "Tailored resume uploaded", done: s.uploaded_resume !== null },
    { label: "Validated before submit", done: s.prepared_at !== null },
    {
      label: s.mode === "LIVE" ? "Submitted (confirmed)" : "Final submit (never in a dry run)",
      done: s.mode === "LIVE" && s.state === "SUBMITTED",
    },
  ];
}

/** Can the user submit this session from the dashboard right now? */
export function canSubmit(s: BrowserSessionView, liveEnabled: boolean): boolean {
  return liveEnabled && s.mode === "LIVE" && s.state === "READY_FOR_SUBMISSION";
}

export function screenshotUrl(appId: string, sessionId: string, index: number): string {
  return `/backend/api/applications/${encodeURIComponent(appId)}/sessions/${encodeURIComponent(sessionId)}/screenshots/${index}`;
}
