import type { ExecutionModeName, RunAction } from "./types";

export type EventKind = "run" | "thinking" | "tool" | "state" | "user" | "internal";

export type EventView = {
  kind: EventKind;
  title: string;
  detail: string | null;
  status: string; // a StatusBadge status
  tool: string | null;
  failed: boolean;
};

const RUN_TITLES: Record<string, string> = {
  RUN_STARTED: "Run started",
  RUN_RESUMED: "Run resumed",
  RUN_COMPLETED: "Run completed",
  RUN_FAILED: "Run failed",
  RUN_CANCELLED: "Run cancelled",
};

function resultLine(output: Record<string, unknown> | null): string | null {
  if (!output) return null;
  if (output.ok === false) return `${String(output.error_code ?? "ERROR")}: ${String(output.message ?? "")}`;
  if (typeof output.message === "string") return output.message;
  return null;
}

/**
 * Turns a stored run event into a concise, user-facing line. Only what PIE stores is shown:
 * one-line step summaries, tool names, arguments and results - never hidden reasoning.
 */
export function describeAction(a: RunAction): EventView {
  const failed = a.status === "FAILED";
  if (a.action in RUN_TITLES) {
    const message = (a.output?.message as string | undefined) ?? a.error ?? null;
    // Started/resumed are point-in-time events: a static badge, never a spinner.
    const status = a.action === "RUN_COMPLETED" ? "COMPLETED" : a.action === "RUN_FAILED" ? "FAILED"
      : a.action === "RUN_CANCELLED" ? "STOPPED" : "info";
    return { kind: "run", title: RUN_TITLES[a.action], detail: message, status, tool: null, failed };
  }
  switch (a.action) {
    case "THINKING":
      return {
        kind: "thinking",
        title: failed ? "Claude reply rejected" : (a.reasoning ?? "Decided next step"),
        detail: failed ? a.error : null,
        status: failed ? "FAILED" : "info",
        tool: (a.output?.tool as string | null | undefined) ?? null,
        failed,
      };
    case "TOOL_CALL_STARTED":
      return { kind: "tool", title: `Calling ${a.tool}`, detail: null, status: "RUNNING", tool: a.tool, failed };
    case "TOOL_CALL_COMPLETED":
    case "TOOL_CALL_FAILED": {
      const skipped = a.status === "SKIPPED";
      const bad = a.action === "TOOL_CALL_FAILED";
      return {
        kind: "tool",
        title: `${a.tool}${bad ? " failed" : skipped ? " skipped" : " done"}`,
        detail: resultLine(a.output) ?? (bad ? a.error : null),
        status: bad ? "FAILED" : skipped ? "PAUSED" : "ok",
        tool: a.tool,
        failed: bad,
      };
    }
    case "STATE_CHANGED": {
      const o = a.output ?? {};
      return {
        kind: "state",
        title: `${String(o.entity)} ${String(o.previous ?? "new")} → ${String(o.new)}`,
        detail: (o.reason as string | undefined) ?? null,
        status: String(o.new ?? "info"),
        tool: a.tool,
        failed,
      };
    }
    case "USER_INPUT_REQUIRED":
      return {
        kind: "user",
        title: "Needs your input",
        detail: (a.output?.message as string | undefined) ?? null,
        status: "NEEDS_USER_INPUT",
        tool: null,
        failed,
      };
    case "USER_MESSAGE":
      return {
        kind: "user",
        title: "You replied",
        detail: (a.input?.message as string | undefined) ?? null,
        status: "ok",
        tool: null,
        failed,
      };
    default:
      // Steps recorded by tools themselves (e.g. JOB_ANALYSIS_COMPLETED inside analyze_job).
      return {
        kind: "internal",
        title: a.action.replace(/_/g, " ").toLowerCase(),
        detail: a.reasoning ?? a.error,
        status: failed ? "FAILED" : "neutral",
        tool: a.tool,
        failed,
      };
  }
}

/** Hide a TOOL_CALL_STARTED once its result is recorded (keep it while still running). */
export function visibleActions(actions: RunAction[]): RunAction[] {
  const finished = new Set(
    actions
      .filter((a) => a.action === "TOOL_CALL_COMPLETED" || a.action === "TOOL_CALL_FAILED")
      .map((a) => `${a.iteration}:${a.tool}`),
  );
  return actions.filter(
    (a) => !(a.action === "TOOL_CALL_STARTED" && finished.has(`${a.iteration}:${a.tool}`)),
  );
}

export const TASK_PRESETS: { label: string; task: string; mode: ExecutionModeName }[] = [
  {
    label: "Review my pipeline",
    task: "Review my job pipeline: count jobs by status, name the jobs closest to my profile from existing analyses and why, and list what only I can provide before an application draft could be ready.",
    mode: "read_only",
  },
  {
    label: "Prepare drafts for shortlisted jobs",
    task: "For every SHORTLISTED job, create an internal application draft and report which drafts are blocked and exactly what I need to provide. Do not submit anything.",
    mode: "internal_writes",
  },
  {
    label: "Check readiness to apply",
    task: "Check my verified application answers and master resume, and list every piece of information still missing before any application could be submitted.",
    mode: "read_only",
  },
  {
    label: "Dry-run READY_TO_APPLY applications",
    task: "For every READY_TO_APPLY job, run the application in the browser as a DRY RUN: open it, inspect the form, fill verified answers, upload the tailored resume, prepare the submission, then close the session. Never submit. Report what was filled, skipped and missing.",
    mode: "external_dry_run",
  },
];
