import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { limitTone, loginText, usedPercent } from "@/lib/home";
import type { DailyLimit, HomeStatus, PlatformMode } from "@/lib/types";
import { DailyLimits } from "./DailyLimits";
import { LiveBanner } from "./LiveBanner";
import { PlatformModes } from "./PlatformModes";
import { QuickLinks } from "./QuickLinks";

const limit = (platform: DailyLimit["platform"], used: number, max: number): DailyLimit => ({
  platform,
  used,
  limit: max,
  remaining: Math.max(max - used, 0),
});

describe("Home: daily limits", () => {
  it("shows used / limit per platform and marks a reached limit", () => {
    render(
      <DailyLimits
        limits={{ ok: true, data: [limit("total", 3, 20), limit("naukri", 0, 15), limit("linkedin", 5, 5), limit("company_site", 3, 10)] }}
      />,
    );
    expect(screen.getByText("3 / 20")).toBeInTheDocument();
    expect(screen.getByText("limit reached")).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(4);
  });

  it("shows an error instead of fake numbers", () => {
    render(<DailyLimits limits={{ ok: false, error: "The backend is not reachable." }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("not reachable");
  });

  it("computes tone and percent", () => {
    expect(usedPercent(limit("naukri", 0, 0))).toBe(100);
    expect(limitTone(limit("naukri", 12, 15))).toBe("warning");
    expect(limitTone(limit("naukri", 15, 15))).toBe("critical");
    expect(limitTone(limit("naukri", 1, 15))).toBe("good");
  });
});

describe("Home: live banner and modes", () => {
  it("shows the live banner only when live is possible", () => {
    const { rerender } = render(<LiveBanner livePossible={false} note="DEVELOPMENT_MODE is true" />);
    expect(screen.queryByRole("alert")).toBeNull();
    rerender(<LiveBanner livePossible note="LIVE submissions are switched ON" />);
    expect(screen.getByRole("alert")).toHaveTextContent("LIVE MODE IS ON");
  });

  it("says when a platform's login is not built yet", () => {
    const naukri: PlatformMode = { platform: "naukri", mode: "dry-run", built: false, note: null, login: null };
    expect(loginText(naukri)).toBe("Available after Phase C");
    expect(loginText({ ...naukri, built: true, login: "expired" })).toContain("log in again");
    render(<PlatformModes platforms={[naukri]} livePossible={false} />);
    expect(screen.getByText("Dry run")).toBeInTheDocument();
  });
});

describe("Home: quick links", () => {
  it("links real counts and labels unbuilt features instead of showing 0", () => {
    const links: HomeStatus["quick_links"] = { needs_input: 2, failed: 1, manual: null, waiting_for_review: null };
    render(<QuickLinks links={links} />);
    expect(screen.getByRole("link", { name: /Needs my input/ })).toHaveAttribute("href", "/jobs?status_group=needs_input");
    expect(screen.getAllByText("Available after Phase C")).toHaveLength(2);
  });
});
