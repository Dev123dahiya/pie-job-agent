import Link from "next/link";
import type { HomeStatus } from "@/lib/types";

/** Quick links. Features not built yet say so instead of showing a fake 0. */
export function QuickLinks({ links }: { links: HomeStatus["quick_links"] }) {
  const items: { label: string; count: number | null; href: string }[] = [
    { label: "Needs my input", count: links.needs_input, href: "/needs-input" },
    { label: "Manual apply", count: links.manual, href: "/manual" },
    { label: "Failed", count: links.failed, href: "/failed" },
    { label: "Waiting for review", count: links.waiting_for_review, href: "/review" },
  ];
  return (
    <nav aria-label="Quick links" className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {items.map((i) =>
        i.count === null ? (
          <div key={i.label} className="rounded-xl border border-border bg-surface p-4">
            <span className="text-xs font-medium text-ink-2">{i.label}</span>
            <span className="mt-1 block text-xs text-ink-3">Available after Phase C</span>
          </div>
        ) : (
          <Link key={i.label} href={i.href} className="rounded-xl border border-border bg-surface p-4 hover:border-accent">
            <span className="text-xs font-medium text-ink-2">{i.label}</span>
            <span className="tabular mt-1 block text-2xl font-semibold text-ink">{i.count}</span>
          </Link>
        ),
      )}
    </nav>
  );
}
