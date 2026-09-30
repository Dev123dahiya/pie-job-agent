import { NAV_ITEMS } from "@/lib/nav";

/** Header + planned-content outline for sections built in later phases. */
export function PagePlaceholder({
  href,
  description,
  planned,
}: {
  href: string;
  description: string;
  planned: string[];
}) {
  const item = NAV_ITEMS.find((n) => n.href === href);
  if (!item) throw new Error(`Unknown nav href ${href}`);
  const Icon = item.icon;
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">{item.label}</h1>
        <p className="mt-1 text-sm text-ink-2">{description}</p>
      </div>
      <section className="rounded-xl border border-dashed border-border bg-surface p-8">
        <div className="mx-auto flex max-w-lg flex-col items-center text-center">
          <span className="grid size-11 place-items-center rounded-xl bg-accent-soft">
            <Icon aria-hidden className="size-5 text-accent-ink" />
          </span>
          <p className="mt-4 text-sm font-medium text-ink">Arrives in Phase {item.phase}</p>
          <ul className="mt-4 w-full space-y-2 text-left text-sm text-ink-2">
            {planned.map((p) => (
              <li key={p} className="flex gap-2">
                <span aria-hidden className="mt-2 size-1 shrink-0 rounded-full bg-ink-3" />
                {p}
              </li>
            ))}
          </ul>
        </div>
      </section>
    </div>
  );
}
