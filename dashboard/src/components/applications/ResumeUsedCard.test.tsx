import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResumeUsedCard } from "./ResumeUsedCard";

const resume = {
  version_id: "v1", version_number: 2, is_master: false, file_name: "acme_backend_v2.pdf",
  locked: true, lock_reason: "A browser session is open for this application.",
};

describe("ResumeUsedCard", () => {
  it("shows a locked badge and why", () => {
    render(<ResumeUsedCard resume={resume} />);
    expect(screen.getByText("Locked")).toBeInTheDocument();
    expect(screen.getByText(/browser session is open/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tailored v2" })).toHaveAttribute("href", "/resumes/v1");
  });

  it("has no badge while the resume may still change, and an empty state", () => {
    const { rerender } = render(<ResumeUsedCard resume={{ ...resume, locked: false, lock_reason: null }} />);
    expect(screen.queryByText("Locked")).toBeNull();
    rerender(<ResumeUsedCard resume={null} />);
    expect(screen.getByText(/No resume linked yet/)).toBeInTheDocument();
  });
});
