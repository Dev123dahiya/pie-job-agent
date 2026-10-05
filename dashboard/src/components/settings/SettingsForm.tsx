"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { PLATFORM_LABEL } from "@/lib/home";
import { buildChange, type CurrentSettings, type Limits } from "@/lib/settings";

const FIELD =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

type Diff = { name: string; diff: string };

/** Edit settings: Preview shows exactly what changes; Save backs up the old files first. */
export function SettingsForm({ current }: { current: CurrentSettings }) {
  const router = useRouter();
  const [form, setForm] = useState({
    keywords: current.keywords.join("\n"),
    locations: current.locations.join("\n"),
    excluded_companies: current.excluded_companies.join("\n"),
    excluded_keywords: current.excluded_keywords.join("\n"),
    freshness_hours: current.freshness_hours,
    limits: { ...current.limits },
    min_match_score: current.min_match_score ?? 70,
    answers: Object.fromEntries(Object.keys(current.answer_questions).map((k) => [k, current.answers[k] ?? ""])),
  });
  const [diffs, setDiffs] = useState<Diff[] | null>(null);
  const [state, setState] = useState<{ busy: boolean; message: string | null; error: boolean }>({
    busy: false,
    message: null,
    error: false,
  });
  const change = buildChange(current, form);
  const dirty = Object.keys(change).length > 0;

  const update = (patch: Partial<typeof form>) => {
    setForm({ ...form, ...patch });
    setDiffs(null); // anything edited after a preview needs a new preview
  };

  async function send(path: "preview" | "save") {
    setState({ busy: true, message: null, error: false });
    try {
      const res = await fetch(path === "preview" ? "/backend/api/settings/preview" : "/backend/api/settings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(change),
      });
      const data = (await res.json().catch(() => ({}))) as { detail?: unknown; files?: Diff[]; saved?: string[] };
      if (!res.ok) {
        const detail = Array.isArray(data.detail)
          ? (data.detail as { msg?: string }[]).map((d) => d.msg).join("; ")
          : String(data.detail ?? "Invalid settings");
        throw new Error(detail);
      }
      if (path === "preview") {
        setDiffs(data.files ?? []);
        setState({ busy: false, message: null, error: false });
      } else {
        setDiffs(null);
        setState({ busy: false, message: `Saved ${data.saved?.join(", ")}. Old versions were backed up.`, error: false });
        router.refresh();
      }
    } catch (err) {
      setState({ busy: false, message: (err as Error).message, error: true });
    }
  }

  const limitField = (key: keyof Limits) => (
    <label key={key} className="flex flex-col gap-1">
      <span className="text-xs text-ink-3">{PLATFORM_LABEL[key]}</span>
      <input
        type="number"
        min={0}
        max={200}
        value={form.limits[key]}
        onChange={(e) => update({ limits: { ...form.limits, [key]: Number(e.target.value) } })}
        className={FIELD}
      />
    </label>
  );

  return (
    <div className="flex flex-col gap-6">
      <Card title="Search" description="Keywords and locations are kept the same in your profile and the search config">
        <div className="grid gap-4 md:grid-cols-2">
          {(
            [
              ["keywords", "Keywords (one per line)"],
              ["locations", "Locations (one per line)"],
              ["excluded_companies", "Excluded companies (one per line)"],
              ["excluded_keywords", "Excluded title keywords (one per line)"],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">{label}</span>
              <textarea rows={5} value={form[key]} onChange={(e) => update({ [key]: e.target.value })} className={FIELD} />
            </label>
          ))}
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-3">Job freshness (hours since posting)</span>
            <input
              type="number"
              min={1}
              max={720}
              value={form.freshness_hours}
              onChange={(e) => update({ freshness_hours: Number(e.target.value) })}
              className={FIELD}
            />
          </label>
          <p className="self-end text-xs text-ink-3">Experience range: {current.read_only.experience_range}</p>
        </div>
      </Card>

      <Card title="Limits and matching" description="Daily limits count live submissions; LinkedIn must stay below Naukri">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          {(["total", "naukri", "linkedin", "instahyre", "company_site"] as const).map(limitField)}
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-3">Min. match score</span>
            <input
              type="number"
              min={0}
              max={100}
              value={form.min_match_score}
              onChange={(e) => update({ min_match_score: Number(e.target.value) })}
              className={FIELD}
            />
          </label>
        </div>
        <p className="mt-3 text-xs text-ink-3">
          Models: {current.read_only.models.note} Now: {current.read_only.models.current}. Modes: {current.read_only.modes}
        </p>
      </Card>

      <Card title="Standard answers" description="Used on every application form. True facts only; never guessed.">
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(current.answer_questions).map(([key, question]) => (
            <label key={key} className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">{question}</span>
              <input
                value={form.answers[key] ?? ""}
                maxLength={200}
                placeholder={current.answers[key] ? undefined : "Not set: jobs wait for this"}
                onChange={(e) => update({ answers: { ...form.answers, [key]: e.target.value } })}
                className={FIELD}
              />
            </label>
          ))}
        </div>
        <p className="mt-2 text-xs text-ink-3">To remove an answer, edit config/user_profile.yaml directly.</p>
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={!dirty || state.busy}
          onClick={() => send("preview")}
          className="rounded-lg border border-border px-3 py-2 text-sm font-medium text-ink hover:bg-surface-2 disabled:opacity-50"
        >
          Preview changes
        </button>
        <button
          type="button"
          disabled={!dirty || diffs === null || state.busy}
          onClick={() => send("save")}
          className="rounded-lg bg-ink px-3 py-2 text-sm font-medium text-bg hover:opacity-90 disabled:opacity-50"
        >
          Save
        </button>
        <span className="text-xs text-ink-3">{dirty ? "Preview first: Save shows exactly that." : "No changes."}</span>
      </div>
      {state.message && (
        <p role={state.error ? "alert" : "status"} className={state.error ? "text-sm text-critical" : "text-sm text-ink-2"}>
          {state.message}
        </p>
      )}
      {diffs && (
        <Card title="What will change">
          {diffs.length === 0 ? (
            <p className="text-sm text-ink-2">Nothing would change.</p>
          ) : (
            diffs.map((d) => (
              <div key={d.name} className="mb-3">
                <p className="mb-1 text-xs font-medium text-ink-2">{d.name}</p>
                <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 text-xs text-ink">
                  {d.diff.split("\n").map((line, i) => (
                    <span
                      key={i}
                      className={line.startsWith("+") && !line.startsWith("+++") ? "text-good" : line.startsWith("-") && !line.startsWith("---") ? "text-critical" : undefined}
                    >
                      {line}
                      {"\n"}
                    </span>
                  ))}
                </pre>
              </div>
            ))
          )}
        </Card>
      )}
    </div>
  );
}
