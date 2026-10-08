import { Card } from "@/components/ui/Card";
import { STATUS_LABEL, STATUS_ORDER } from "@/lib/home";
import type { StatusCounts as Counts } from "@/lib/types";

export function StatusCounts({ today, allTime }: { today: Counts; allTime: Counts }) {
  return (
    <Card title="Jobs by status" description="Today (changed today) and all time">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-ink-3">
            <th className="pb-2 font-medium">Status</th>
            <th className="pb-2 text-right font-medium">Today</th>
            <th className="pb-2 text-right font-medium">All time</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {STATUS_ORDER.map((k) => (
            <tr key={k}>
              <td className="py-1.5 text-ink">{STATUS_LABEL[k]}</td>
              <td className="tabular py-1.5 text-right text-ink-2">{today[k]}</td>
              <td className="tabular py-1.5 text-right text-ink">{allTime[k]}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
