import {
  Bot,
  Briefcase,
  FileText,
  LayoutDashboard,
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
