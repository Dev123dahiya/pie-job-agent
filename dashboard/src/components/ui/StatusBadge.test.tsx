import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { findNavItem, NAV_ITEMS } from "@/lib/nav";
import { StatusBadge, toneFor } from "./StatusBadge";

describe("StatusBadge", () => {
  it("always renders a text label, not just a color", () => {
    render(<StatusBadge status="REQUIRES_USER_ACTION" />);
    expect(screen.getByText("Requires user action")).toBeInTheDocument();
  });

  it("maps statuses to tones", () => {
    expect(toneFor("OFFER")).toBe("good");
    expect(toneFor("REJECTED")).toBe("critical");
    expect(toneFor("BLOCKED_NEEDS_USER")).toBe("warning");
    expect(toneFor("NEEDS_USER_INPUT")).toBe("warning");
    expect(toneFor("SOMETHING_NEW")).toBe("neutral");
  });
});

describe("navigation", () => {
  it("contains every section from the spec, in order", () => {
    expect(NAV_ITEMS.map((n) => n.label)).toEqual([
      "Dashboard", "Jobs", "Shortlisted", "Applications", "Needs my input", "Manual apply", "Failed", "Emails",
      "Resumes", "Agent", "Settings", "Logs",
    ]);
  });

  it("resolves nested paths to their section", () => {
    expect(findNavItem("/")?.label).toBe("Dashboard");
    expect(findNavItem("/applications/123")?.label).toBe("Applications");
    expect(findNavItem("/unknown")).toBeUndefined();
  });
});
