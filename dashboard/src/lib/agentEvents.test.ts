import { describe, expect, it } from "vitest";
import { describeAction, visibleActions } from "./agentEvents";
import type { RunAction } from "./types";

const act = (over: Partial<RunAction>): RunAction => ({
  sequence: 1, action: "THINKING", status: "SUCCESS", tool: null, iteration: 1, job_id: null,
  input: null, output: null, reasoning: null, error: null, at: "2026-09-28T10:00:00Z", ...over,
});

describe("describeAction", () => {
  it("shows only the concise step summary for THINKING", () => {
    const v = describeAction(act({ reasoning: "Counting jobs by status" }));
    expect(v).toMatchObject({ kind: "thinking", title: "Counting jobs by status", failed: false });
  });

  it("surfaces tool failures with their error code", () => {
    const v = describeAction(act({
      action: "TOOL_CALL_FAILED", status: "FAILED", tool: "submit_application",
      output: { ok: false, error_code: "NOT_ENABLED_IN_PHASE_3", message: "Nothing was done." },
    }));
    expect(v.failed).toBe(true);
    expect(v.detail).toBe("NOT_ENABLED_IN_PHASE_3: Nothing was done.");
  });

  it("describes state changes and user-input requests", () => {
    expect(describeAction(act({
      action: "STATE_CHANGED", output: { entity: "job", previous: "SHORTLISTED", new: "BLOCKED_NEEDS_USER" },
    })).title).toBe("job SHORTLISTED → BLOCKED_NEEDS_USER");
    const ask = describeAction(act({ action: "USER_INPUT_REQUIRED", output: { message: "Work authorization?" } }));
    expect(ask).toMatchObject({ kind: "user", detail: "Work authorization?", status: "NEEDS_USER_INPUT" });
  });

  it("never shows a spinner for past run events", () => {
    expect(describeAction(act({ action: "RUN_STARTED" })).status).toBe("info");
    expect(describeAction(act({ action: "RUN_COMPLETED", output: { message: "done" } })).status).toBe("COMPLETED");
  });

  it("hides a started tool call once its result exists", () => {
    const started = act({ sequence: 2, action: "TOOL_CALL_STARTED", tool: "get_job", iteration: 3 });
    const done = act({ sequence: 3, action: "TOOL_CALL_COMPLETED", tool: "get_job", iteration: 3 });
    const running = act({ sequence: 4, action: "TOOL_CALL_STARTED", tool: "search_jobs", iteration: 4 });
    expect(visibleActions([started, done, running]).map((a) => a.sequence)).toEqual([3, 4]);
  });
});
