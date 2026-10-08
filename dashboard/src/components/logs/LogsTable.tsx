import clsx from "clsx";
import { LEVEL_TONE, type LogLine } from "@/lib/logs";

/** Log lines, newest first. Messages are already redacted by the backend. */
export function LogsTable({ items }: { items: LogLine[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
        No log lines match these filters.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full min-w-[720px] border-collapse text-xs">
        <thead className="border-b border-border bg-surface-2/50 text-left text-ink-3">
          <tr>
            <th scope="col" className="whitespace-nowrap px-3 py-2 font-medium">Time</th>
            <th scope="col" className="px-3 py-2 font-medium">Level</th>
            <th scope="col" className="px-3 py-2 font-medium">Source</th>
            <th scope="col" className="px-3 py-2 font-medium">Message</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border font-mono">
          {items.map((line, i) => (
            <tr key={`${line.at}-${i}`} className="align-top">
              <td className="whitespace-nowrap px-3 py-1.5 text-ink-3">{line.at.replace("T", " ")}</td>
              <td className={clsx("px-3 py-1.5 font-semibold", LEVEL_TONE[line.level] ?? "text-ink-2")}>{line.level}</td>
              <td className="max-w-[200px] truncate px-3 py-1.5 text-ink-3" title={line.logger}>
                {line.logger}
              </td>
              <td className="whitespace-pre-wrap break-words px-3 py-1.5 text-ink">{line.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
