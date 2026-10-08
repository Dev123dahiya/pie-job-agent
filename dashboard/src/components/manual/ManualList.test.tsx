import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ManualList } from "./ManualList";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

describe("ManualList", () => {
  it("links to the site and offers mark-applied and skip", () => {
    render(
      <ManualList
        items={[{ job_id: "j1", title: "Backend Engineer", company: "Acme", url: "https://acme.invalid/apply", match_score: 82, reason: "Unsupported site", added_at: null }]}
      />,
    );
    expect(screen.getByRole("link", { name: /Apply/ })).toHaveAttribute("href", "https://acme.invalid/apply");
    expect(screen.getByRole("button", { name: /Mark as applied/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Skip/ })).toBeInTheDocument();
    expect(screen.getByText("82")).toBeInTheDocument();
  });

  it("has an empty state", () => {
    render(<ManualList items={[]} />);
    expect(screen.getByText("Nothing to apply to by hand.")).toBeInTheDocument();
  });
});
