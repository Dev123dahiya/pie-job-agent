import {
  Bot,
  Briefcase,
  CircleX,
  Hand,
  ListChecks,
  PlayCircle,
  FileText,
  LayoutDashboard,
  MessageCircleQuestion,
  Mail,
  ScrollText,
  Send,
  Settings,
  Star,
  type LucideIcon,
} from "lucide-react";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Build phase in which the page becomes functional. */
  phase: number;
};

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, phase: 1 },
  { href: "/jobs", label: "Jobs", icon: Briefcase, phase: 2 },
  { href: "/shortlisted", label: "Shortlisted", icon: Star, phase: 2 },
  { href: "/applications", label: "Applications", icon: Send, phase: 5 },
  { href: "/runs", label: "Runs", icon: PlayCircle, phase: 1 },
  { href: "/review", label: "Review queue", icon: ListChecks, phase: 1 },
  { href: "/needs-input", label: "Needs my input", icon: MessageCircleQuestion, phase: 1 },
  { href: "/manual", label: "Manual apply", icon: Hand, phase: 1 },
  { href: "/failed", label: "Failed", icon: CircleX, phase: 1 },
  { href: "/emails", label: "Emails", icon: Mail, phase: 6 },
  { href: "/resumes", label: "Resumes", icon: FileText, phase: 4 },
  { href: "/agent", label: "Agent", icon: Bot, phase: 3 },
  { href: "/settings", label: "Settings", icon: Settings, phase: 8 },
  { href: "/logs", label: "Logs", icon: ScrollText, phase: 3 },
];

export function findNavItem(pathname: string): NavItem | undefined {
  if (pathname === "/") return NAV_ITEMS[0];
  return NAV_ITEMS.find((item) => item.href !== "/" && pathname.startsWith(item.href));
}
