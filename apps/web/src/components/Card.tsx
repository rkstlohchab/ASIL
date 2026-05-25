import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export function Card({
  children,
  className,
  title,
  subtitle,
  right,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
  subtitle?: string;
  right?: ReactNode;
}) {
  return (
    <section
      className={cn(
        "rounded-xl border border-ink-700 bg-ink-800/60 backdrop-blur p-5 shadow-lg",
        className,
      )}
    >
      {(title || right) && (
        <header className="flex items-start justify-between mb-3">
          <div>
            {title && (
              <h2 className="text-sm font-medium text-ink-100">{title}</h2>
            )}
            {subtitle && (
              <p className="text-xs text-ink-400 mt-0.5">{subtitle}</p>
            )}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-900/40 p-4">
      <div className="text-[11px] uppercase tracking-wider text-ink-400">
        {label}
      </div>
      <div className="text-2xl font-semibold text-ink-50 mt-1 tabular-nums">
        {value}
      </div>
      {hint && <div className="text-xs text-ink-400 mt-1">{hint}</div>}
    </div>
  );
}
