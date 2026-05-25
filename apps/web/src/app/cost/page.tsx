"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, StatTile } from "@/components/Card";
import { AlertCircle, DollarSign, PiggyBank, TrendingUp } from "lucide-react";

type CostData = {
  days: number;
  ledger_available: boolean;
  total_usd: number;
  calls: number;
  by_provider: Record<string, number>;
  by_tier: Record<string, number>;
  by_day: Array<{ day: string; cost: number }>;
  memory_count: number;
  savings: null | {
    memory_conclusions: number;
    fresh_cost_estimate_usd: number;
    with_memory_cost_estimate_usd: number;
    saved_usd: number;
    savings_pct: number;
    fresh_per_call_usd: number;
    cached_per_call_usd: number;
  };
};

export default function CostPage() {
  const [days, setDays] = useState(30);
  const [d, setD] = useState<CostData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<CostData>(`/dashboard/cost?days=${days}`)
      .then(setD)
      .catch((e) => setErr(String(e)));
  }, [days]);

  const maxDayCost = d?.by_day.reduce((m, x) => Math.max(m, x.cost), 0) ?? 0;

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
            LLM cost + memory savings
          </h1>
          <p className="text-ink-300 mt-1">
            Persistent ledger of every LLM call, plus an estimate of how much
            episodic memory saved by recalling cached conclusions.
          </p>
        </div>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100"
        >
          {[7, 14, 30, 60, 90].map((n) => (
            <option key={n} value={n}>
              last {n} days
            </option>
          ))}
        </select>
      </header>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <div className="flex items-center gap-2 text-danger">
            <AlertCircle size={16} />
            <span className="font-medium">API unreachable</span>
          </div>
          <pre className="mt-2 text-[11px] text-ink-400 overflow-x-auto">
            {err}
          </pre>
        </Card>
      )}

      {d && !d.ledger_available && (
        <Card className="border-warn/40 bg-warn/10">
          <p className="text-sm text-ink-200">
            Postgres ledger not reachable yet. Cost numbers below are zeroed
            out until the ledger comes up. Run <code>make up</code> and retry.
          </p>
        </Card>
      )}

      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatTile
          label="Total spent"
          value={`$${(d?.total_usd ?? 0).toFixed(4)}`}
          hint={`${d?.calls ?? 0} LLM calls`}
        />
        <StatTile
          label="Memories"
          value={d?.memory_count ?? 0}
          hint="cached conclusions"
        />
        <StatTile
          label="Estimated saved"
          value={`$${(d?.savings?.saved_usd ?? 0).toFixed(4)}`}
          hint={`${(d?.savings?.savings_pct ?? 0).toFixed(0)}% of fresh-only cost`}
        />
        <StatTile
          label="Avg / call"
          value={
            d && d.calls > 0
              ? `$${(d.total_usd / d.calls).toFixed(6)}`
              : "—"
          }
          hint="excludes recalled answers"
        />
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <Card className="lg:col-span-2" title="Daily spend" subtitle="bar = relative cost">
          {d && d.by_day.length === 0 && (
            <p className="text-sm text-ink-400">No data yet.</p>
          )}
          <ul className="space-y-1.5">
            {d?.by_day.map((row) => {
              const pct = (row.cost / (maxDayCost || 1)) * 100;
              return (
                <li key={row.day} className="flex items-center gap-3 text-xs">
                  <span className="w-24 font-mono text-ink-400">{row.day}</span>
                  <div className="flex-1 h-4 rounded bg-ink-800 overflow-hidden">
                    <div
                      className="h-full bg-accent-500/70"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="w-20 text-right tabular-nums text-ink-200">
                    ${row.cost.toFixed(4)}
                  </span>
                </li>
              );
            })}
          </ul>
        </Card>

        <div className="space-y-5">
          <Card title="By provider">
            {d?.by_provider && Object.keys(d.by_provider).length === 0 ? (
              <p className="text-sm text-ink-400">No calls yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {Object.entries(d?.by_provider ?? {}).map(([k, v]) => (
                  <li key={k} className="flex justify-between text-sm">
                    <span className="text-ink-300">{k}</span>
                    <span className="tabular-nums text-ink-100">
                      ${v.toFixed(4)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="By tier">
            {d?.by_tier && Object.keys(d.by_tier).length === 0 ? (
              <p className="text-sm text-ink-400">No calls yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {Object.entries(d?.by_tier ?? {}).map(([k, v]) => (
                  <li key={k} className="flex justify-between text-sm">
                    <span className="text-ink-300">{k}</span>
                    <span className="tabular-nums text-ink-100">
                      ${v.toFixed(4)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </section>

      {d?.savings && (
        <Card
          title="Memory savings (estimated)"
          subtitle={`Assumes ~$${d.savings.fresh_per_call_usd} per fresh ask, ~$${d.savings.cached_per_call_usd} per recall`}
        >
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <Bucket
              icon={<DollarSign size={14} />}
              label="without memory"
              value={`$${d.savings.fresh_cost_estimate_usd}`}
              tone="danger"
            />
            <Bucket
              icon={<DollarSign size={14} />}
              label="with memory"
              value={`$${d.savings.with_memory_cost_estimate_usd}`}
              tone="ok"
            />
            <Bucket
              icon={<PiggyBank size={14} />}
              label="saved"
              value={`$${d.savings.saved_usd}`}
              tone="ok"
            />
            <Bucket
              icon={<TrendingUp size={14} />}
              label="savings %"
              value={`${d.savings.savings_pct}%`}
              tone="ok"
            />
          </div>
        </Card>
      )}
    </div>
  );
}

function Bucket({
  icon,
  label,
  value,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone: "ok" | "danger" | "neutral";
}) {
  const color =
    tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink-100";
  return (
    <div className="rounded border border-ink-700 p-3">
      <div className="text-[11px] uppercase tracking-wider text-ink-400 flex items-center gap-1">
        {icon}
        <span>{label}</span>
      </div>
      <div className={`text-xl font-semibold tabular-nums mt-1 ${color}`}>
        {value}
      </div>
    </div>
  );
}
