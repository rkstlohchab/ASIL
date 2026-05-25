import { cn } from "@/lib/cn";
import type { Confidence } from "@/lib/api";

export function ConfidenceBar({ conf }: { conf: Confidence }) {
  const pct = Math.max(0, Math.min(1, conf.score)) * 100;
  const bar =
    pct >= 70
      ? "bg-ok"
      : pct >= 40
      ? "bg-warn"
      : "bg-danger";
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-ink-300">
        <span>Confidence</span>
        <span className="tabular-nums text-ink-100">
          {pct.toFixed(1)}%
        </span>
      </div>
      <div className="h-2 w-full rounded-full bg-ink-700 overflow-hidden">
        <div
          className={cn("h-full transition-all", bar)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="grid grid-cols-3 gap-3 text-[11px] text-ink-400 pt-1">
        <Pill label="evidence" value={conf.evidence_count} />
        <Pill
          label="retrieval"
          value={`${(conf.retrieval_strength * 100).toFixed(0)}%`}
        />
        <Pill
          label="causal"
          value={`${(conf.causal_confidence * 100).toFixed(0)}%`}
        />
      </div>
      {conf.derivation.length > 0 && (
        <ul className="text-xs text-ink-300 list-disc pl-4 space-y-1 pt-2">
          {conf.derivation.map((d, i) => (
            <li key={i}>{d}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Pill({
  label,
  value,
}: {
  label: string;
  value: number | string;
}) {
  return (
    <div className="rounded border border-ink-700 px-2 py-1 flex items-center justify-between">
      <span className="uppercase tracking-wider">{label}</span>
      <span className="tabular-nums text-ink-100">{value}</span>
    </div>
  );
}
