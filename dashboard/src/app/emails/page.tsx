import type { Metadata } from "next";
import { PagePlaceholder } from "@/components/ui/PagePlaceholder";

export const metadata: Metadata = { title: "Emails" };

export default function EmailsPage() {
  return (
    <PagePlaceholder
      href="/emails"
      description="Job-related mail, classified and linked to applications."
      planned={[
        "Sender, subject, company, role and classification",
        "Linked application and the agent's interpretation",
        "Low-confidence matches flagged for your review",
      ]}
    />
  );
}
