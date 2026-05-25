"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Brain,
  DollarSign,
  GitBranch,
  HomeIcon,
  MessageSquare,
  Network,
  Plug,
  ShieldAlert,
} from "lucide-react";
import { cn } from "@/lib/cn";

const NAV = [
  { href: "/", label: "Dashboard", icon: HomeIcon },
  { href: "/ask", label: "Ask", icon: MessageSquare },
  { href: "/incidents", label: "Incidents", icon: ShieldAlert },
  { href: "/causality", label: "Causality", icon: Network },
  { href: "/drift", label: "Drift", icon: GitBranch },
  { href: "/memory", label: "Memory", icon: Brain },
  { href: "/cost", label: "Cost", icon: DollarSign },
  { href: "/mcp", label: "MCP tools", icon: Plug },
  { href: "/health", label: "Health", icon: Activity },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="w-60 border-r border-ink-700 bg-ink-900/60 p-4 flex flex-col gap-1 shrink-0">
      <div className="px-2 pb-4">
        <div className="text-lg font-semibold text-ink-50 tracking-tight">
          ASIL
        </div>
        <div className="text-[11px] uppercase tracking-wider text-ink-400">
          Engineering Intelligence
        </div>
      </div>
      <nav className="flex flex-col gap-1">
        {NAV.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-accent-600/20 text-accent-400 border border-accent-600/30"
                  : "text-ink-300 hover:bg-ink-800 hover:text-ink-50",
              )}
            >
              <Icon size={16} />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto text-[11px] text-ink-400 px-2 pt-4 border-t border-ink-700">
        Phase 7 · v0.1 · <a className="underline" href="https://github.com/rkstlohchab/ASIL" target="_blank" rel="noreferrer">GitHub</a>
      </div>
    </aside>
  );
}
