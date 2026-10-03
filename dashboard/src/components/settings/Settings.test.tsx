import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { isAllowedProxyPath } from "@/lib/proxy";
import { buildChange, lines, type CurrentSettings } from "@/lib/settings";
import { SettingsForm } from "./SettingsForm";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const current: CurrentSettings = {
  keywords: ["Python Developer"], locations: ["Gurugram"], excluded_companies: [], excluded_keywords: [],
  freshness_hours: 48, limits: { total: 20, naukri: 15, linkedin: 5, company_site: 10 }, min_match_score: 70,
  answers: { notice_period: "Immediate" },
  answer_questions: { notice_period: "What is your notice period?", current_ctc: "What is your current CTC?" },
  read_only: { models: { note: "E1", current: "claude-opus-5" }, experience_range: "Phase C", modes: "Runs page" },
};

const form = () => ({
  keywords: "Python Developer", locations: "Gurugram", excluded_companies: "", excluded_keywords: "",
  freshness_hours: 48, limits: { ...current.limits }, min_match_score: 70,
  answers: { notice_period: "Immediate", current_ctc: "" },
});

describe("Settings", () => {
  it("sends only what changed", () => {
    expect(buildChange(current, form())).toEqual({});
    expect(buildChange(current, { ...form(), keywords: "Python Developer\n\n Backend Developer " })).toEqual({
      keywords: ["Python Developer", "Backend Developer"],
    });
    expect(buildChange(current, { ...form(), answers: { notice_period: "Immediate", current_ctc: "0" } })).toEqual({
      answers: { current_ctc: "0" },
    });
    expect(lines(" a \n\nb\n")).toEqual(["a", "b"]);
  });

  it("needs a preview before saving", () => {
    render(<SettingsForm current={current} />);
    const save = screen.getByRole("button", { name: "Save" });
    const preview = screen.getByRole("button", { name: "Preview changes" });
    expect(preview).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Min. match score"), { target: { value: "75" } });
    expect(preview).toBeEnabled();
    expect(save).toBeDisabled(); // no preview yet
    expect(isAllowedProxyPath("api/settings/preview")).toBe(true);
  });
});
