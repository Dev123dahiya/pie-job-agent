import clsx from "clsx";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  CircleMinus,
  Clock,
  CircleDot,
  Loader2,
  PauseCircle,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { humanize } from "@/lib/format";

export type Tone = "good" | "warning" | "serious" | "critical" | "info" | "neutral";

const TONE: Record<Tone, { icon: LucideIcon; color: string }> = {
  good: { icon: CheckCircle2, color: "text-good" },
  warning: { icon: AlertTriangle, color: "text-warning" },
  serious: { icon: AlertTriangle, color: "text-serious" },
  critical: { icon: XCircle, color: "text-critical" },
  info: { icon: CircleDot, color: "text-accent" },
  neutral: { icon: CircleDashed, color: "text-neutral" },
};

const STATUS_TONE: Record<string, Tone> = {
  OFFER: "good",
  INTERVIEW: "good",
  FINAL_ROUND: "good",
  SUBMITTED: "good",
  APPLIED: "info",
  APPLICATION_CONFIRMED: "info",
  ASSESSMENT: "info",
  RECRUITER_CONTACT: "info",
  SHORTLISTED: "info",
  DISCOVERED: "neutral",
  ANALYZED: "neutral",
  IDLE: "neutral",
  COMPLETED: "good",
  RUNNING: "info",
  PAUSED: "warning",
  REQUIRES_USER_ACTION: "warning",
  NO_RESPONSE: "neutral",
  WITHDRAWN: "neutral",
  REJECTED: "critical",
  FAILED: "critical",
  STOPPED: "serious",
  ok: "good",
  not_configured: "neutral",
  unavailable: "critical",
  REJECTED_BY_AGENT: "neutral",
  STOPPING: "warning",
  READY_TO_APPLY: "info",
  APPLICATION_IN_PROGRESS: "info",
  BLOCKED_NEEDS_USER: "warning",
  MANUAL_APPLY: "warning",
  NEEDS_USER_INPUT: "warning",
  DRAFT: "neutral",
  // Browser execution (Phase 5)
  READY_FOR_SUBMISSION: "info",
  WAITING_FOR_USER: "warning",
  OPEN: "info",
  NAVIGATING: "info",
  FORM_DETECTED: "info",
  FILLING: "info",
  CLOSED: "neutral",
  CREATED: "neutral",
  DRY_RUN: "info",
  LIVE: "critical",
  CONFIRMED: "good",
  USER_CONFIRMED: "good",
  AMBIGUOUS: "warning",
  PENDING: "warning",
  USER_NOT_SUBMITTED: "neutral",
  REFUSED: "neutral",
  BLOCKED: "warning",
  SUCCESS: "good",
  // Match levels and freshness
  strong: "good",
  medium: "warning",
  weak: "critical",
  unknown: "neutral",
  fresh: "good",
  stale: "neutral",
};

// Status-specific icons, so shape (not just color) distinguishes states.
const ICON_OVERRIDE: Record<string, LucideIcon> = {
  RUNNING: Loader2,
  PAUSED: PauseCircle,
  medium: CircleMinus,
  stale: Clock,
  unknown: CircleHelp,
};

export function toneFor(status: string): Tone {
  return STATUS_TONE[status] ?? "neutral";
}

/** Colored icon + text label: status is never conveyed by color alone. */
export function StatusBadge({
  status,
  label,
  className,
}: {
  status: string;
  label?: string;
  className?: string;
}) {
  const tone = TONE[toneFor(status)];
  const Icon = ICON_OVERRIDE[status] ?? tone.icon;
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2 py-0.5 text-xs font-medium text-ink-2",
        className,
      )}
    >
      <Icon aria-hidden className={clsx("size-3.5", tone.color, status === "RUNNING" && "animate-spin")} />
      {label ?? humanize(status)}
    </span>
  );
}
