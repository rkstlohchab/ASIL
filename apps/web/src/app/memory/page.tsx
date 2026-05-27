"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, StatTile } from "@/components/Card";
import { Brain, Loader2 } from "lucide-react";

type MemoryHit = {
  id: string;
  question: string;
  answer: string;
  similarity: number;
  repo_key: string | null;
  created_at: string | null;
  cost_usd: number | null;
  model: string | null;
  confidence?: { score: number };
  origin_agent?: string;
  user_id?: string;
  team_id?: string;
};

type DashboardMemory = {
  days: number;
  memory_count: number;
  write_log_stats: null | {
    total_writes: number;
    inserted: number;
    folded: number;
    dedupe_rate_pct: number;
    by_agent: Record<string, number>;
    by_source: Record<string, number>;
  };
  top_recalled: Array<{
    id: string;
    question: string;
    answer_excerpt: string;
    recall_hits: number;
    origin_agent: string;
    user_id: string;
    team_id: string;
    repo_key: string;
    created_at: string | null;
    source: string | null;
  }>;
};

export default function MemoryPage() {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [stats, setStats] = useState<DashboardMemory | null>(null);

  useEffect(() => {
    api
      .get<DashboardMemory>("/dashboard/memory?days=30&top_n=10")
      .then(setStats)
      .catch(() => setStats(null));
  }, []);

  async function run() {
    setBusy(true);
    setErr(null);
    setHits(null);
    try {
      const r = await api.callTool<{ hits: MemoryHit[]; count: number }>(
        "asil.recall",
        { query: q, limit: 20 },
      );
      setHits(r.hits);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const wl = stats?.write_log_stats;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Episodic memory
        </h1>
        <p className="text-ink-300 mt-1">
          Every conclusion ASIL ever reached, with full provenance. The cache
          short-circuit returns these directly on a high-similarity hit,
          skipping the reasoning + verifier LLM calls.
        </p>
      </header>

      {stats && (
        <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatTile
            label="Memories"
            value={stats.memory_count.toLocaleString()}
            hint="rows in asil_memories"
          />
          <StatTile
            label="Writes (30d)"
            value={(wl?.total_writes ?? 0).toLocaleString()}
            hint={`${wl?.inserted ?? 0} inserted, ${wl?.folded ?? 0} folded`}
          />
          <StatTile
            label="Dedupe rate"
            value={`${(wl?.dedupe_rate_pct ?? 0).toFixed(1)}%`}
            hint="folded / total"
          />
          <StatTile
            label="Agents writing"
            value={Object.keys(wl?.by_agent ?? {}).length}
            hint="distinct origin_agent"
          />
        </section>
      )}

      {wl && Object.keys(wl.by_source).length > 0 && (
        <Card title="Writes by source (30d)" subtitle="where new memories came from">
          <ul className="space-y-1.5">
            {Object.entries(wl.by_source).map(([k, v]) => (
              <li key={k} className="flex justify-between text-sm">
                <span className="text-ink-300">{k}</span>
                <span className="tabular-nums text-ink-100">{v.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {stats && stats.top_recalled.length > 0 && (
        <Card
          title="Top recalled memories"
          subtitle="rows with the most cache hits — the questions your team keeps asking"
        >
          <ul className="divide-y divide-ink-700">
            {stats.top_recalled.map((m) => (
              <li key={m.id} className="py-3 space-y-1">
                <div className="flex items-start justify-between gap-3">
                  <div className="text-sm font-medium text-ink-100">{m.question}</div>
                  <span className="text-xs tabular-nums text-accent-400 shrink-0">
                    {m.recall_hits} hit{m.recall_hits === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="text-xs text-ink-400 line-clamp-2">{m.answer_excerpt}</p>
                <div className="text-[11px] text-ink-500 flex flex-wrap gap-3">
                  <span>via {m.origin_agent}</span>
                  <span>by {m.user_id}</span>
                  {m.source && <span>source: {m.source}</span>}
                  <span className="font-mono">{m.repo_key}</span>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        <div className="flex gap-2 items-end">
          <label className="flex-1">
            <span className="text-xs uppercase tracking-wider text-ink-400">
              Search past conclusions
            </span>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder='e.g. "how does the LLM router pick a provider?"'
              className="mt-1 w-full rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100 focus:outline-none focus:ring-2 focus:ring-accent-500"
              onKeyDown={(e) => {
                if (e.key === "Enter") run();
              }}
            />
          </label>
          <button
            onClick={run}
            disabled={busy || !q.trim()}
            className="rounded-md bg-accent-600 hover:bg-accent-500 disabled:bg-ink-700 text-white text-sm py-2 px-4 flex items-center gap-2 transition-colors"
          >
            {busy ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Brain size={16} />
            )}
            Recall
          </button>
        </div>
      </Card>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      {hits && (
        <Card title={`${hits.length} recalled conclusion(s)`}>
          {hits.length === 0 && (
            <p className="text-sm text-ink-400">
              No matches. ASIL hasn&apos;t answered anything close to that yet.
            </p>
          )}
          <ul className="divide-y divide-ink-700">
            {hits.map((m, mi) => (
              <li key={m.id ?? `mem-${mi}`} className="py-4 space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="text-sm font-medium text-ink-100">
                    {m.question}
                  </div>
                  <span className="text-xs tabular-nums text-accent-400 shrink-0">
                    sim {(m.similarity * 100).toFixed(0)}%
                  </span>
                </div>
                <pre className="whitespace-pre-wrap text-sm text-ink-300 font-sans leading-relaxed">
                  {m.answer}
                </pre>
                <div className="text-[11px] text-ink-400 flex flex-wrap items-center gap-3 pt-1">
                  {m.repo_key && (
                    <span className="font-mono">{m.repo_key}</span>
                  )}
                  {m.model && <span>model: {m.model}</span>}
                  {typeof m.cost_usd === "number" && (
                    <span>cost: ${m.cost_usd.toFixed(4)}</span>
                  )}
                  {m.confidence && (
                    <span>
                      conf: {(m.confidence.score * 100).toFixed(0)}%
                    </span>
                  )}
                  {m.created_at && (
                    <span>{new Date(m.created_at).toLocaleString()}</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
