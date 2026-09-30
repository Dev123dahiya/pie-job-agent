"use client";

import clsx from "clsx";
import { Menu, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { NAV_ITEMS, findNavItem } from "@/lib/nav";
import { ThemeToggle } from "./ThemeToggle";

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-3 px-2">
      <span
        aria-hidden
        className="grid size-9 place-items-center rounded-xl bg-ink text-[15px] font-semibold tracking-tight text-bg"
      >
        π
      </span>
      <span className="leading-tight">
        <span className="block text-[15px] font-semibold tracking-[0.18em]">PIE</span>
        <span className="block text-[11px] text-ink-3">Personal Intelligence Engine</span>
      </span>
    </Link>
  );
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const active = findNavItem(pathname);
  return (
    <nav aria-label="Main" className="flex flex-col gap-0.5">
      {NAV_ITEMS.map((item) => {
        const isActive = active?.href === item.href;
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={isActive ? "page" : undefined}
            className={clsx(
              "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
              isActive
                ? "bg-surface-2 font-medium text-ink"
                : "text-ink-2 hover:bg-surface-2/60 hover:text-ink",
            )}
          >
            <Icon className={clsx("size-4", isActive ? "text-accent" : "text-ink-3")} aria-hidden />
            <span className="flex-1">{item.label}</span>
            {item.phase > 1 && (
              <span className="text-[10px] tabular text-ink-3 opacity-0 transition-opacity group-hover:opacity-100">
                P{item.phase}
              </span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

function SidebarBody({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-6 px-3 py-5">
      <Brand />
      <div>
        <p className="mb-2 px-3 text-[11px] font-medium uppercase tracking-wider text-ink-3">
          Job Agent
        </p>
        <NavLinks onNavigate={onNavigate} />
      </div>
      <div className="mt-auto rounded-lg border border-border bg-surface-2/50 p-3 text-xs text-ink-2">
        <p className="font-medium text-ink">Development mode</p>
        <p className="mt-1">No real applications are submitted. Actions are logged only.</p>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const current = findNavItem(pathname);

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 border-r border-border bg-surface lg:block">
        <SidebarBody />
      </aside>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true">
          <button
            aria-label="Close navigation"
            className="absolute inset-0 bg-black/40"
            onClick={() => setOpen(false)}
          />
          <aside className="relative h-full w-72 border-r border-border bg-surface">
            <button
              aria-label="Close navigation"
              className="absolute right-3 top-5 rounded-md p-1.5 text-ink-2 hover:bg-surface-2"
              onClick={() => setOpen(false)}
            >
              <X className="size-4" />
            </button>
            <SidebarBody onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-bg/85 px-4 backdrop-blur sm:px-6">
          <button
            aria-label="Open navigation"
            className="rounded-md p-1.5 text-ink-2 hover:bg-surface-2 lg:hidden"
            onClick={() => setOpen(true)}
          >
            <Menu className="size-5" />
          </button>
          <p className="min-w-0 truncate text-sm text-ink-3">
            Job Agent <span className="px-1.5">/</span>
            <span className="text-ink">{current?.label ?? "Not found"}</span>
          </p>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:py-8">{children}</main>
      </div>
    </div>
  );
}
