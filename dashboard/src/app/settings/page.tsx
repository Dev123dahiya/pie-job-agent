import type { Metadata } from "next";
import { PagePlaceholder } from "@/components/ui/PagePlaceholder";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <PagePlaceholder
      href="/settings"
      description="Preferences and automation settings the agent follows."
      planned={[
        "Roles, locations, minimum salary, freshness, remote preference",
        "Excluded companies and roles, daily application limit",
        "Resume selection, email account and automation settings",
      ]}
    />
  );
}
