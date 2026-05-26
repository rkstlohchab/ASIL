"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/Card";
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
};

export default function MemoryPage() {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

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

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Episodic memory
        </h1>
        <p className="text-ink-300 mt-1">
          Every conclusion ASIL ever reached, with full provenance. Recalling
          one of these costs ~$0.0001 vs. ~$0.01 to re-derive it from scratch.
        </p>
      </header>

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
