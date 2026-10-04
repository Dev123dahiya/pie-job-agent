"use client";

import { OctagonAlert, Play } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PLATFORM_LABEL } from "@/lib/home";
import { RUN_STARTED_EVENT } from "@/lib/jobs";
import {
  STEP_LABEL,
  autoBlocker,
  reviewPhrases,
  type Platform,
  type RunMode,
  type RunOptions,
  type RunStep,
} from "@/lib/runs";

/** Start a run. Dry run is always pre-selected; review (live) needs the typed confirmation. */
export function StartRunForm({ options, busyRun }: { options: RunOptions; busyRun: boolean }) {
  const router = useRouter();
  const [steps, setSteps] = useState<RunStep[]>(["apply"]);
  const [platforms, setPlatforms] = useState<Platform[]>(["company_site"]);
  const [mode, setMode] = useState<RunMode>("dry-run");
  const [typed, setTyped] = useState("");
  const [analysisLimit, setAnalysisLimit] = useState(25);
  const [state, setState] = useState<{ busy: boolean; message: string | null; error: boolean }>({
    busy: false,
    message: null,
    error: false,
  });

  const phrases = reviewPhrases(options, platforms, mode);
  const needsPhrase = mode !== "dry-run" && steps.includes("apply");
  const autoBlocked = autoBlocker(options, platforms);
  const phraseOk = !needsPhrase || (phrases.length === 1 && typed.trim() === phrases[0]);
  const unsupported =
    steps.includes("apply") && options.platforms.some((p) => platforms.includes(p.platform) && !p.apply_supported);
  const canStart = steps.length > 0 && platforms.length > 0 && phraseOk && !unsupported && !busyRun && !state.busy && !(mode === "auto" && autoBlocked !== null);

  const toggle = <T,>(list: T[], value: T, set: (v: T[]) => void) =>
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    if (!canStart) return;
    setState({ busy: true, message: null, error: false });
    try {
      const res = await fetch("/backend/api/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          steps,
          platforms,
          mode,
          analysis_limit: analysisLimit,
          confirmation: needsPhrase ? typed.trim() : null,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as { detail?: unknown; message?: string };
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The run could not start");
      setState({ busy: false, message: data.message ?? "Started", error: false });
      setTyped("");
      window.dispatchEvent(new Event(RUN_STARTED_EVENT));
      router.refresh();
    } catch (err) {
      setState({ busy: false, message: (err as Error).message, error: true });
    }
  }

  return (
    <form onSubmit={start} className="flex flex-col gap-4">
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs font-medium text-ink-2">Steps</legend>
        {options.steps.map((s) => (
          <label key={s} className="flex items-center gap-2 text-sm text-ink">
            <input type="checkbox" checked={steps.includes(s)} onChange={() => toggle(steps, s, setSteps)} />
            {STEP_LABEL[s]}
          </label>
        ))}
        {steps.includes("analyze") && (
          <label className="mt-1 flex items-center gap-2 text-xs text-ink-2">
            Analyze at most
            <input
              type="number"
              min={1}
              max={options.max_analysis}
              value={analysisLimit}
              onChange={(e) => setAnalysisLimit(Math.min(options.max_analysis, Math.max(1, Number(e.target.value) || 1)))}
              className="w-16 rounded border border-border bg-surface px-1.5 py-0.5 text-ink"
            />
            jobs (Claude usage)
          </label>
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs font-medium text-ink-2">Platforms</legend>
        {options.platforms.map((p) => (
          <label key={p.platform} className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={platforms.includes(p.platform)}
              onChange={() => toggle(platforms, p.platform, setPlatforms)}
            />
            {PLATFORM_LABEL[p.platform]}
            {!p.apply_supported && <span className="text-xs text-ink-3">(applying: not built yet)</span>}
          </label>
        ))}
        {unsupported && (
          <p className="text-xs text-critical">Applying works on company sites only for now. Untick the others.</p>
        )}
      </fieldset>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs font-medium text-ink-2">Mode</legend>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input type="radio" name="mode" checked={mode === "dry-run"} onChange={() => setMode("dry-run")} />
          Dry run: fill everything, never submit
        </label>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="radio"
            name="mode"
            checked={mode === "review"}
            disabled={!options.live_possible}
            onChange={() => setMode("review")}
          />
          Review (live): prepare each form, then wait for your yes/no
        </label>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="radio"
            name="mode"
            checked={mode === "auto"}
            disabled={!options.live_possible}
            onChange={() => setMode("auto")}
          />
          Auto (live): fill and submit without asking each time
        </label>
        {mode === "auto" && autoBlocked && <p className="text-xs text-critical">{autoBlocked}</p>}
        {!options.live_possible && options.live_note && <p className="text-xs text-ink-3">{options.live_note}</p>}
        <p className="text-xs text-ink-3">
          Auto still stops at daily limits and site warnings, and pauses on questions PIE cannot answer truthfully.
        </p>
      </fieldset>

      {needsPhrase && (
        <div role="alert" className="flex flex-col gap-2 rounded-xl border-2 border-critical bg-critical/10 p-3">
          <p className="flex items-center gap-2 text-sm font-bold text-critical">
            <OctagonAlert aria-hidden className="size-4" />{" "}
            {mode === "auto" ? "This is a FULLY AUTOMATIC live run: nothing asks you before submitting" : "This is a LIVE run"}
          </p>
          {phrases.length === 1 ? (
            <>
              <p className="text-xs text-ink-2">
                Type this exactly to confirm: <span className="font-mono text-ink">{phrases[0]}</span>
              </p>
              <input
                aria-label="Live confirmation"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-sm text-ink"
              />
            </>
          ) : (
            <p className="text-xs text-ink-2">Choose exactly one platform for a live run.</p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={!canStart}
          className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3 py-2 text-sm font-medium text-bg hover:opacity-90 disabled:opacity-50"
        >
          <Play aria-hidden className="size-4" /> {state.busy ? "Starting…" : "Start run"}
        </button>
        {busyRun && <span className="text-xs text-ink-3">A run is in progress.</span>}
      </div>
      {state.message && (
        <p role="status" className={state.error ? "text-sm text-critical" : "text-sm text-ink-2"}>
          {state.message}
        </p>
      )}
    </form>
  );
}
