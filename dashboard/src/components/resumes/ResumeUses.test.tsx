import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResumeUses } from "./ResumeUses";

describe("ResumeUses", () => {
  it("lists applications with status and a locked badge", () => {
    render(
      <ResumeUses
        uses={[
          { application_id: "a1", job_id: "j1", title: "Backend Engineer", company: "Acme", user_status: "applied", locked: true },
          { application_id: "a2", job_id: "j2", title: "API Developer", company: null, user_status: "found", locked: false },
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "Backend Engineer" })).toHaveAttribute("href", "/applications/a1");
    expect(screen.getAllByText("Locked")).toHaveLength(1);
    expect(screen.getByText(/Unknown company · Found/)).toBeInTheDocument();
  });

  it("says when nothing uses it", () => {
    render(<ResumeUses uses={[]} />);
    expect(screen.getByText("Not used by any application yet.")).toBeInTheDocument();
  });
});
