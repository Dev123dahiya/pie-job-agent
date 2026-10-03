import {
  Briefcase,
  ClipboardCheck,
  Handshake,
  Inbox,
  MessagesSquare,
  Send,
  Star,
  XCircle,
} from "lucide-react";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { DailyRunCard } from "@/components/dashboard/DailyRunCard";
import { LeadsCard } from "@/components/dashboard/LeadsCard";
import { AgentActivity } from "@/components/agent/AgentActivity";
import { Funnel } from "@/components/dashboard/Funnel";
import { StatTile } from "@/components/dashboard/StatTile";
import { SystemStatus } from "@/components/dashboard/SystemStatus";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { DailyLimits } from "@/components/home/DailyLimits";
import { LiveBanner } from "@/components/home/LiveBanner";
import { PlatformModes } from "@/components/home/PlatformModes";
import { QuickLinks } from "@/components/home/QuickLinks";
import { StatusCounts } from "@/components/home/StatusCounts";
import {
  getAgentStatus,
  getDailyLimits,
  getDailyRun,
  getHealth,
  getHome,
  getLeadSummary,
  getOverview,
} from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const [overview, health, agentStatus, daily, leads, home, limits] = await Promise.all([
    getOverview(),
    getHealth(),
    getAgentStatus(),
    getDailyRun(),
    getLeadSummary(),
    getHome(),
    getDailyLimits(),
  ]);

  if (!overview.ok) {
    return (
      <div className="flex flex-col gap-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-ink">Overview</h1>
            <p className="mt-1 text-sm text-ink-2">Your job search, as the agent sees it.</p>
          </div>
          <StatusBadge status="unavailable" label="Backend offline" />
        </div>
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center">
          <p className="text-sm font-medium text-ink">No numbers to show</p>
          <p className="mt-1 text-sm text-ink-2">
            {overview.error} The overview only ever shows real data from the database.
          </p>
        </div>
        <div className="grid gap-6 lg:grid-cols-3">
          <AgentActivity initial={agentStatus} className="lg:col-span-2" />
          <SystemStatus health={health} />
        </div>
      </div>
    );
  }
  const data = overview.data;

  const tiles = [
    { label: "Discovered today", value: data.jobs_discovered_today, icon: Inbox },
    { label: "Shortlisted", value: data.jobs_shortlisted, icon: Star },
    { label: "Applications submitted", value: data.applications_submitted, icon: Send, hint: "Confirmed submissions only" },
    { label: "Assessments", value: data.assessments, icon: ClipboardCheck },
    { label: "Interviews", value: data.interviews, icon: MessagesSquare },
    { label: "Recruiter responses", value: data.recruiter_responses, icon: Briefcase },
    { label: "Offers", value: data.offers, icon: Handshake },
    { label: "Rejections", value: data.rejections, icon: XCircle },
  ];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Overview</h1>
          <p className="mt-1 text-sm text-ink-2">Your job search, as the agent sees it.</p>
        </div>
        <StatusBadge status="ok" label="Live data" />
      </div>

      {home.ok && <LiveBanner livePossible={home.data.live_possible} note={home.data.live_note} />}
      {home.ok ? (
        <>
          <QuickLinks links={home.data.quick_links} />
          <div className="grid gap-6 lg:grid-cols-3">
            <PlatformModes platforms={home.data.platforms} livePossible={home.data.live_possible} />
            <DailyLimits limits={limits} />
            <StatusCounts today={home.data.today} allTime={home.data.all_time} />
          </div>
        </>
      ) : (
        <p role="alert" className="rounded-xl border border-border bg-surface px-5 py-4 text-sm text-ink-2">
          Modes, limits and status counts are unavailable: {home.error}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {tiles.map((t) => (
          <StatTile key={t.label} {...t} />
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card
          title="Application funnel"
          description="All-time, from discovery to offer"
          className="lg:col-span-2"
        >
          <Funnel stages={data.funnel} />
        </Card>
        <AgentActivity initial={agentStatus} />
      </div>

      <DailyRunCard daily={daily} />
      <LeadsCard leads={leads} />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <ActivityFeed items={data.recent_activity} />
        </div>
        <SystemStatus health={health} />
      </div>
    </div>
  );
}
