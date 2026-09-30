import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Funnel } from "./Funnel";

const stages = [
  { stage: "Discovered", count: 10 },
  { stage: "Shortlisted", count: 5 },
  { stage: "Offer", count: 0 },
];

describe("Funnel", () => {
  it("renders every stage with its count and conversion", () => {
    render(<Funnel stages={stages} />);
    expect(screen.getByLabelText("Discovered: 10. Top of funnel")).toBeInTheDocument();
    expect(screen.getByLabelText("Shortlisted: 5. 50% of discovered")).toBeInTheDocument();
    expect(screen.getByLabelText("Offer: 0. 0% of shortlisted")).toBeInTheDocument();
  });

  it("scales bars relative to the largest stage and leaves zero stages empty", () => {
    render(<Funnel stages={stages} />);
    const bars = screen.getAllByTestId("funnel-bar");
    expect(bars[0].style.width).toBe("max(100%, 3px)");
    expect(bars[1].style.width).toBe("max(50%, 3px)");
    expect(bars[2].style.width).toBe("0%");
  });

  it("handles an all-zero funnel without dividing by zero", () => {
    render(<Funnel stages={[{ stage: "Discovered", count: 0 }]} />);
    expect(screen.getByTestId("funnel-bar").style.width).toBe("0%");
  });
});
