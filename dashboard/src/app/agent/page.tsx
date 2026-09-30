import type { Metadata } from "next";
import { AgentConsole } from "@/components/agent/AgentConsole";
import { getAgentConsoleData } from "@/lib/api";

export const metadata: Metadata = { title: "Agent" };
export const dynamic = "force-dynamic";

export default async function AgentPage() {
  const { status, run, history, tools } = await getAgentConsoleData();
  return <AgentConsole initialStatus={status} initialRun={run} initialHistory={history} tools={tools} />;
}
