import type { Metadata } from "next";
import { SettingsForm } from "@/components/settings/SettingsForm";
import { getSettings } from "@/lib/api";

export const metadata: Metadata = { title: "Settings" };
export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const res = await getSettings();
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-2">
          What the agent searches for, its limits and your standard answers. Every save is checked, shown to you as a
          diff and backed up first. Safety rules are enforced by the backend whatever is set here.
        </p>
      </div>
      {res.ok ? (
        <SettingsForm current={res.data} />
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      )}
    </div>
  );
}
