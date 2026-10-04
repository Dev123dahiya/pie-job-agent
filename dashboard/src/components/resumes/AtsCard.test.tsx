import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AtsCard } from "./AtsCard";

describe("AtsCard", () => {
  it("shows coverage, what you have but don't show, and what you don't have", () => {
    render(
      <AtsCard
        ats={{
          ok: true,
          data: {
            version_id: "v1", job_id: "j1", keyword_coverage: 80, found: ["Python"],
            missing_you_have: ["Docker"], missing_not_in_profile: ["Kubernetes"],
            checks: [{ name: "text_readable", passed: true, detail: "787 characters of text can be read" }],
          },
        }}
      />,
    );
    expect(screen.getByText("80%")).toBeInTheDocument();
    expect(screen.getByText("Docker")).toBeInTheDocument();
    expect(screen.getByText(/Kubernetes \(never added/)).toBeInTheDocument();
    expect(screen.getByLabelText("passed")).toBeInTheDocument();
  });

  it("shows the reason when there is no check", () => {
    render(<AtsCard ats={{ ok: false, error: "Only a tailored resume with a PDF can be checked" }} />);
    expect(screen.getByText(/tailored resume with a PDF/)).toBeInTheDocument();
  });
});
