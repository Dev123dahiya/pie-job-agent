import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { isAllowedProxyPath } from "@/lib/proxy";
import { logsDownloadUrl, logsQueryString } from "@/lib/logs";
import { LogsTable } from "./LogsTable";

describe("Logs", () => {
  it("keeps only known filters and drops the offset from the download", () => {
    const params = { level: "ERROR", run: "abc", offset: "200", evil: "x" };
    expect(logsQueryString(params)).toBe("?level=ERROR&run=abc&offset=200");
    expect(logsDownloadUrl(params)).toBe("/backend/api/logs/download?level=ERROR&run=abc");
    expect(isAllowedProxyPath("api/logs/download")).toBe(true);
  });

  it("renders lines with their level, and an empty state", () => {
    const { rerender } = render(
      <LogsTable items={[{ at: "2026-10-02T10:00:06", level: "ERROR", logger: "backend.main", message: "Boom\nTraceback" }]} />,
    );
    expect(screen.getByText("ERROR")).toBeInTheDocument();
    expect(screen.getByText(/Boom/)).toBeInTheDocument();
    rerender(<LogsTable items={[]} />);
    expect(screen.getByText("No log lines match these filters.")).toBeInTheDocument();
  });
});
