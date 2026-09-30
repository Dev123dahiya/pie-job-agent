import type { Metadata } from "next";
import { PagePlaceholder } from "@/components/ui/PagePlaceholder";

export const metadata: Metadata = { title: "Logs" };

export default function LogsPage() {
  return (
    <PagePlaceholder
      href="/logs"
      description="Every agent run and every tool call: what Claude did and why."
      planned={[
        "Agent runs with duration, counts and errors",
        "Tool calls with inputs, results and reasoning",
      ]}
    />
  );
}
