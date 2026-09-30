"use client";

import clsx from "clsx";
import { FilePlus, FileText, Loader2, Radar, Sparkles, WandSparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { RUN_STARTED_EVENT } from "@/lib/jobs";


type ActionState = { busy: boolean; message: string | null; error: boolean };

async function post(path: string, body?: unknown): Promise<{ ok: boolean; data: Record<string, unknown> }> {
  const res = await fetch(`/backend/api/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    // Non-JSON error bodies fall through to the generic message below.
  }
  return { ok: res.ok, data };
}

function useAction() {
  const [state, setState] = useState<ActionState>({ busy: false, message: null, error: false });
  const run = async (fn: () => Promise<string>) => {
    setState({ busy: true, message: null, error: false });
    try {
      setState({ busy: false, message: await fn(), error: false });
    } catch (err) {
      setState({ busy: false, message: (err as Error).message, error: true });
    }
  };
  return { state, run };
}

function ActionButton({
  onClick,
  busy,
  icon: Icon,
  children,
  variant = "secondary",
}: {
  onClick: () => void;
  busy: boolean;
  icon: typeof Radar;
  children: React.ReactNode;
  variant?: "primary" | "secondary";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition-colors disabled:cursor-wait disabled:opacity-70",
        variant === "primary"
          ? "bg-ink text-bg hover:opacity-90"
          : "border border-border bg-surface text-ink hover:bg-surface-2",
      )}
    >
      {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <Icon aria-hidden className="size-4" />}
      {children}
    </button>
  );
}

function Message({ state }: { state: ActionState }) {
  if (!state.message) return null;
  return (
    <p role="status" className={clsx("text-xs", state.error ? "text-critical" : "text-ink-3")}>
      {state.message}
    </p>
  );
}

const detail = (data: Record<string, unknown>, fallback: string) =>
  typeof data.detail === "string" ? data.detail : fallback;

/** Start discovery (all enabled sources) and/or batch analysis. */
export function PipelineActions() {
  const router = useRouter();
  const discover = useAction();
  const analyze = useAction();

  const start = (path: string, body: unknown, action: ReturnType<typeof useAction>) =>
    action.run(async () => {
      const { ok, data } = await post(path, body);
      if (!ok) throw new Error(detail(data, "Request failed"));
      window.dispatchEvent(new Event(RUN_STARTED_EVENT));
      router.refresh();
      return String(data.message ?? "Started");
    });

  return (
    <div className="flex flex-col items-start gap-1 sm:items-end">
      <div className="flex flex-wrap gap-2">
        <ActionButton
          icon={Radar}
          variant="primary"
          busy={discover.state.busy}
          onClick={() => start("jobs/discover", {}, discover)}
        >
          Discover jobs
        </ActionButton>
        <ActionButton
          icon={WandSparkles}
          busy={analyze.state.busy}
          onClick={() => start("jobs/analyze-shortlist", { limit: 25 }, analyze)}
        >
          Analyze &amp; shortlist
        </ActionButton>
      </div>
      <Message state={discover.state.message ? discover.state : analyze.state} />
    </div>
  );
}

/** Analyze one job now (synchronous; a Claude call can take up to a minute). */
export function AnalyzeJobButton({ jobId, analyzed }: { jobId: string; analyzed: boolean }) {
  const router = useRouter();
  const { state, run } = useAction();
  return (
    <div className="flex flex-col items-start gap-1">
      <ActionButton
        icon={Sparkles}
        busy={state.busy}
        onClick={() =>
          run(async () => {
            const { ok, data } = await post(`jobs/${jobId}/analyze`);
            if (!ok) throw new Error(detail(data, "Analysis failed"));
            router.refresh();
            return `Now ${String(data.status ?? "").toLowerCase().replace(/_/g, " ")}`;
          })
        }
      >
        {state.busy ? "Analyzing…" : analyzed ? "Re-analyze" : "Analyze"}
      </ActionButton>
      <Message state={state} />
    </div>
  );
}

/** Creates an INTERNAL application draft. Nothing is sent to the employer. */
export function CreateDraftButton({ jobId }: { jobId: string }) {
  const router = useRouter();
  const { state, run } = useAction();
  return (
    <div className="flex flex-col items-start gap-1">
      <ActionButton
        icon={FilePlus}
        busy={state.busy}
        onClick={() =>
          run(async () => {
            const { ok, data } = await post(`jobs/${jobId}/application-draft`, {});
            if (!ok) throw new Error(detail(data, "Could not create the draft"));
            router.refresh();
            const d = (data.data ?? {}) as { created?: boolean; job_status?: string; blockers?: string[] };
            if (!d.created) return "A draft already exists";
            return d.blockers?.length
              ? `Draft created - blocked: ${d.blockers.length} item(s) need you`
              : "Draft created - ready to apply";
          })
        }
      >
        Create application draft
      </ActionButton>
      <p className="text-[11px] text-ink-3">Internal only. Nothing is sent.</p>
      <Message state={state} />
    </div>
  );
}

const STRATEGIES = [
  { value: "balanced", label: "Balanced" },
  { value: "skills_first", label: "Skills first" },
  { value: "projects_first", label: "Projects first" },
  { value: "experience_first", label: "Experience first" },
] as const;

type Violation = { code: string; detail: string };

/** Creates a NEW validated tailored resume version from the master. Internal only. */
export function CreateTailoredResumeButton({ jobId, hasVersion }: { jobId: string; hasVersion: boolean }) {
  const router = useRouter();
  const { state, run } = useAction();
  const [strategy, setStrategy] = useState<string>("balanced");
  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`strategy-${jobId}`}>
          Tailoring strategy
        </label>
        <select
          id={`strategy-${jobId}`}
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          disabled={state.busy}
          className="rounded-lg border border-border bg-surface px-2 py-2 text-sm text-ink"
        >
          {STRATEGIES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <ActionButton
          icon={FileText}
          busy={state.busy}
          onClick={() =>
            run(async () => {
              const { ok, data } = await post(`jobs/${jobId}/tailored-resume`, {
                tailoring_strategy: strategy,
                requested_format: "pdf",
                force_new: hasVersion,
              });
              if (!ok) {
                const violations = (Array.isArray(data.violations) ? data.violations : []) as Violation[];
                const rejected = violations.length
                  ? ` Rejected claims: ${violations.slice(0, 4).map((v) => v.detail).join("; ")}`
                  : "";
                throw new Error(detail(data, "Tailoring failed") + rejected);
              }
              router.refresh();
              const d = (data.data ?? {}) as { created?: boolean; version_number?: number };
              return d.created
                ? `Validated tailored resume v${d.version_number} created`
                : `An identical version (v${d.version_number}) already exists`;
            })
          }
        >
          {state.busy ? "Tailoring… (up to a few minutes)" : hasVersion ? "Create new tailored version" : "Create Tailored Resume"}
        </ActionButton>
      </div>
      <p className="text-[11px] text-ink-3">Internal only. Nothing is submitted.</p>
      <Message state={state} />
    </div>
  );
}
