import { Lock } from "lucide-react";
import Link from "next/link";
import { STATUS_LABEL } from "@/lib/home";
import type { ResumeUse } from "@/lib/types";

export function LockedBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-2 px-2 py-0.5 text-xs font-semibold text-ink">
      <Lock aria-hidden className="size-3" /> Locked
    </span>
  );
}

/** Applications that use a resume version, each with its status and lock. */
export function ResumeUses({ uses }: { uses: ResumeUse[] }) {
  if (uses.length === 0) return <p className="text-xs text-ink-3">Not used by any application yet.</p>;
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {uses.map((u) => (
        <li key={u.application_id} className="flex flex-wrap items-center gap-1.5">
          <Link href={`/applications/${u.application_id}`} className="text-ink hover:underline">
            {u.title}
          </Link>
          <span className="text-ink-3">
            {u.company ?? "Unknown company"} · {STATUS_LABEL[u.user_status]}
          </span>
          {u.locked && <LockedBadge />}
        </li>
      ))}
    </ul>
  );
}
