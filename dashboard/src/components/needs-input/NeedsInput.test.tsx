import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { answersToSend, type PausedJob } from "@/lib/needsInput";
import { isAllowedProxyPath } from "@/lib/proxy";
import { AnswerForm } from "./AnswerForm";
import { SavedAnswers } from "./SavedAnswers";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));

const job: PausedJob = {
  job_id: "j1", application_id: "a1", title: "Backend Engineer", company: "Acme", url: "https://x.invalid",
  reason: "1 question needs you", paused_at: null, in_browser: true,
  questions: [
    { question_key: "q_night_shifts_1a2b3c4d", question: "Night shifts?", kind: "form", reason: null, answerable: true },
    { question_key: "challenge_otp", question: "OTP sent to phone", kind: "challenge", reason: "Enter it yourself", answerable: false },
  ],
};

describe("Needs my input", () => {
  it("sends only answerable, non-empty answers", () => {
    expect(answersToSend(job.questions, { q_night_shifts_1a2b3c4d: "  No ", challenge_otp: "123456" })).toEqual([
      { question_key: "q_night_shifts_1a2b3c4d", question: "Night shifts?", answer: "No" },
    ]);
    expect(answersToSend(job.questions, {})).toEqual([]);
  });

  it("has no input for a challenge and enables save once answered", () => {
    render(<AnswerForm job={job} />);
    expect(screen.getAllByRole("textbox")).toHaveLength(1); // the OTP is never typed here
    const save = screen.getByRole("button", { name: "Save answers" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Night shifts?"), { target: { value: "No" } });
    expect(save).toBeEnabled();
  });

  it("lists saved answers with edit and delete, or an empty state", () => {
    const { rerender } = render(
      <SavedAnswers answers={[{ id: "s1", question_key: "current_ctc", question: "Current CTC?", answer: "0", source: "you", updated_at: "" }]} />,
    );
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    rerender(<SavedAnswers answers={[]} />);
    expect(screen.getByText(/No saved answers yet/)).toBeInTheDocument();
  });

  it("is reachable through the proxy", () => {
    expect(isAllowedProxyPath("api/needs-input/saved-answers/abc")).toBe(true);
  });
});
