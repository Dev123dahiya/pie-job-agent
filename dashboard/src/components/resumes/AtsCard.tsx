import { CircleCheck, CircleX } from "lucide-react";
import { Card } from "@/components/ui/Card";
import type { Fetched } from "@/lib/types";

export type AtsReport = {
  version_id: string;
  job_id: string | null;
  keyword_coverage: number | null;
  found: string[];
  missing_you_have: string[];
  missing_not_in_profile: string[];
  checks: { name: string; passed: boolean; detail: string }[];
};

/** Local ATS check (offline; nothing is uploaded anywhere). */
export function AtsCard({ ats }: { ats: Fetched<AtsReport> }) {
  return (
    <Card title="ATS check" description="Checked on this computer. Nothing is uploaded anywhere.">
      {!ats.ok ? (
        <p className="text-sm text-ink-2">{ats.error}</p>
      ) : (
        <div className="flex flex-col gap-3 text-sm">
          <p className="text-ink">
            Keyword coverage:{" "}
            <span className="tabular text-lg font-semibold">
              {ats.data.keyword_coverage === null ? "—" : `${ats.data.keyword_coverage}%`}
            </span>{" "}
            <span className="text-xs text-ink-3">of the job&apos;s skills appear in this resume</span>
          </p>
          {ats.data.missing_you_have.length > 0 && (
            <div>
              <p className="text-xs font-medium text-ink-2">You have these, but this resume doesn&apos;t show them</p>
              <p className="text-xs text-ink">{ats.data.missing_you_have.join(", ")}</p>
            </div>
          )}
          {ats.data.missing_not_in_profile.length > 0 && (
            <div>
              <p className="text-xs font-medium text-ink-2">The job asks for these; they are not in your profile</p>
              <p className="text-xs text-ink-3">
                {ats.data.missing_not_in_profile.join(", ")} (never added: PIE does not invent skills)
              </p>
            </div>
          )}
          <ul className="flex flex-col gap-1">
            {ats.data.checks.map((c) => (
              <li key={c.name} className="flex items-start gap-1.5 text-xs">
                {c.passed ? (
                  <CircleCheck aria-label="passed" className="mt-0.5 size-3.5 shrink-0 text-good" />
                ) : (
                  <CircleX aria-label="failed" className="mt-0.5 size-3.5 shrink-0 text-critical" />
                )}
                <span className="text-ink-2">{c.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
