"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/Card";
import { Loader2, Sparkles } from "lucide-react";

type Cause = {
  cause_kind: string;
  confidence: number;
  delta_seconds: number;
  derivation: string;
  strategy: string;
  cause_props: Record<string, unknown>;
};

export default function CausalityPage() {
  const [id, setId] = useState("INC-2026-04-12-payments-cascade");
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState<{
    incident_id: string;
    causes: Cause[];
    count: number;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.callTool<{
        incident_id: string;
        causes: Cause[];
        count: number;
      }>("asil.find_causes", { incident_id: id });
      setData(r);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Causality
        </h1>
        <p className="text-ink-300 mt-1">
          THE MOAT. Composite causal scoring across three observable strategies:
          temporal proximity, lagged correlation, explicit reference.
          Every edge carries confidence + derivation.
        </p>
      </header>

      <Card>
        <div className="flex gap-2 items-end">
          <label className="flex-1">
            <span className="text-xs uppercase tracking-wider text-ink-400">
              Incident ID
            </span>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              className="mt-1 w-full rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100 font-mono focus:outline-none focus:ring-2 focus:ring-accent-500"
            />
          </label>
          <button
            onClick={run}
            disabled={busy || !id.trim()}
            className="rounded-md bg-accent-600 hover:bg-accent-500 disabled:bg-ink-700 text-white text-sm py-2 px-4 flex items-center gap-2 transition-colors"
          >
            {busy ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Sparkles size={16} />
            )}
            Find causes
          </button>
        </div>
      </Card>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      {data && (
        <Card
          title={`${data.count} candidate cause(s)`}
          subtitle="Ranked by composite confidence (highest first)"
        >
          {data.causes.length === 0 && (
            <p className="text-sm text-ink-400">
              No causes linked yet. Run{" "}
              <code className="text-ink-100">uv run asil temporal link prod</code>{" "}
              first.
            </p>
          )}
          <ul className="space-y-3">
            {data.causes.map((c, i) => {
              const pct = c.confidence * 100;
              const tone =
                pct >= 70
                  ? "border-ok/40 bg-ok/5"
                  : pct >= 40
                  ? "border-warn/40 bg-warn/5"
                  : "border-ink-700 bg-ink-900/40";
              return (
                <li
                  key={i}
                  className={`rounded-lg border ${tone} p-4 space-y-2`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-xs uppercase tracking-wider text-ink-400">
                        #{i + 1}
                      </span>
                      <span className="text-ink-100 font-medium">
                        {c.cause_kind}
                      </span>
                      <span className="text-[11px] uppercase tracking-wider text-ink-400 border border-ink-700 rounded px-1.5 py-0.5">
                        {c.strategy}
                      </span>
                    </div>
                    <span className="text-sm tabular-nums text-ink-100">
                      {pct.toFixed(1)}%
                    </span>
                  </div>
                  <div className="text-xs text-ink-400">
                    Δ {(c.delta_seconds / 60).toFixed(2)} min before incident
                  </div>
                  <p className="text-sm text-ink-200">{c.derivation}</p>
                  <details className="text-xs text-ink-400">
                    <summary className="cursor-pointer hover:text-ink-200">
                      cause_props
                    </summary>
                    <pre className="mt-2 p-2 rounded bg-ink-900 overflow-x-auto">
                      {JSON.stringify(c.cause_props, null, 2)}
                    </pre>
                  </details>
                </li>
              );
            })}
          </ul>
        </Card>
      )}
    </div>
  );
}
