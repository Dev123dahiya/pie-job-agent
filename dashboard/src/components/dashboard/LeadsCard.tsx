import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { Fetched, LeadBoard, LeadSummary } from "@/lib/types";

const LEAD_STATES: [string, string][] = [
  ["NEW", "Waiting"],
  ["VERIFIED", "Board verified"],
  ["BOARD_CANDIDATE", "Board candidate"],
  ["MATCHED", "Matched"],
  ["DUPLICATE", "Linked to known job"],
  ["NO_MATCH", "No match"],
  ["AMBIGUOUS", "Ambiguous"],
  ["NO_BOARD", "No board"],
  ["REJECTED", "Excluded"],
];

function BoardList({ title, boards, empty }: { title: string; boards: LeadBoard[]; empty: string }) {
  return (
    <div>
      <h3 className="text-xs font-medium text-ink-2">
        {title} ({boards.length})
      </h3>
      {boards.length === 0 ? (
        <p className="mt-1 text-xs text-ink-3">{empty}</p>
      ) : (
        <ul className="mt-1 flex flex-col gap-1">
          {boards.map((b) => (
            <li key={`${b.platform}:${b.board_id}`} className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-ink">{b.company}</span>
              <span className="font-mono text-xs text-ink-3">
                {b.platform}:{b.board_id}
              </span>
              <StatusBadge status={b.status} />
              <span className="text-xs text-ink-3">
                {b.rule ?? "no evidence"} · {b.leads} lead{b.leads === 1 ? "" : "s"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Lead -> trusted ATS resolution: aggregator leads never count as jobs and never make
 * anything fresh; boards are only read once the user approves them. */
export function LeadsCard({ leads }: { leads: Fetched<LeadSummary> }) {
  if (!leads.ok) {
    return (
      <Card title="Lead resolution" description="Aggregator leads → employer ATS boards">
        <p className="text-sm text-ink-2">{leads.error}</p>
      </Card>
    );
  }
  const s = leads.data;
  const mode = s.enabled
    ? `On · ${s.max_requests_per_run} requests/run, ${s.max_requests_per_company}/company`
    : "Off (lead_resolution.enabled=false)";
  return (
    <Card
      title="Lead resolution"
      description={`${mode}. Leads are not jobs; board approval is manual${
        s.auto_activate_verified ? " (auto-activation ON)" : ""
      }.`}
    >
      <div className="flex flex-col gap-4">
        <p className="text-sm text-ink-2">
          {s.leads_total} leads from {s.companies_total} companies · {s.negative_cache_active} companies
          without a supported board (re-checked after {s.negative_cache_days} days)
        </p>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-5">
          {LEAD_STATES.map(([key, label]) => (
            <div key={key}>
              <dt className="text-xs text-ink-3">{label}</dt>
              <dd className="text-sm font-semibold tabular-nums text-ink">{s.leads_by_status[key] ?? 0}</dd>
            </div>
          ))}
        </dl>
        <BoardList
          title="Boards needing your review"
          boards={s.boards_needing_review}
          empty="None. Approve with: python -m backend.scripts.leads boards --approve platform:board"
        />
        <BoardList title="Approved boards" boards={s.active_boards} empty="No board approved yet." />
      </div>
    </Card>
  );
}
