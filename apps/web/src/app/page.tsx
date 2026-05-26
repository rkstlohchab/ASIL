"use client";

import { useEffect, useState } from "react";
import { api, type DashboardStats } from "@/lib/api";
import { Card, StatTile } from "@/components/Card";
import { AlertCircle, Database, GitCommit, Layers, Sparkles } from "lucide-react";

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<DashboardStats>("/dashboard/stats")
      .then(setStats)
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <div className="space-y-8">
      <header className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Engineering intelligence at a glance
        </h1>
        <p className="text-ink-300">
          Persistent, temporal, causal understanding of your codebase + runtime.
        </p>
      </header>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <div className="flex items-center gap-2 text-danger">
            <AlertCircle size={16} />
            <span className="font-medium">API unreachable</span>
          </div>
          <p className="text-sm text-ink-300 mt-2">
            Make sure FastAPI is running: <code className="text-ink-100">uv run uvicorn asil_api.main:app --reload</code>
          </p>
          <pre className="mt-2 text-[11px] text-ink-400 overflow-x-auto">{err}</pre>
        </Card>
      )}

      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatTile
          label="Repos indexed"
          value={stats?.code["Repo"] ?? "—"}
          hint={`${stats?.code["File"] ?? 0} files`}
        />
        <StatTile
          label="Functions"
          value={stats?.code["Function"] ?? "—"}
          hint={`${stats?.code["Class"] ?? 0} classes`}
        />
        <StatTile
          label="Incidents"
          value={stats?.runtime["Incident"] ?? "—"}
          hint={`${stats?.runtime["Deployment"] ?? 0} deployments`}
        />
        <StatTile
          label="Memories"
          value={stats?.memory_count ?? "—"}
          hint="conclusions reused across sessions"
        />
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card
          title="Code namespace"
          subtitle="What ASIL parsed from your repos"
        >
          <ul className="text-sm text-ink-200 divide-y divide-ink-700">
            {Object.entries(stats?.code ?? {}).map(([k, v]) => (
              <li key={k} className="flex justify-between py-2">
                <span className="text-ink-400">{k}</span>
                <span className="tabular-nums">{v}</span>
              </li>
            ))}
            {!stats && <li className="text-ink-400 py-2">loading…</li>}
          </ul>
        </Card>

        <Card
          title="Runtime namespace"
          subtitle="Services, deployments, incidents from postmortems / adapters"
        >
          <ul className="text-sm text-ink-200 divide-y divide-ink-700">
            {Object.entries(stats?.runtime ?? {}).map(([k, v]) => (
              <li key={k} className="flex justify-between py-2">
                <span className="text-ink-400">{k}</span>
                <span className="tabular-nums">{v}</span>
              </li>
            ))}
            {!stats && <li className="text-ink-400 py-2">loading…</li>}
          </ul>
        </Card>
      </section>

      <Card title="Indexed repos" subtitle="Newest first">
        {stats && stats.repos.length === 0 && (
          <p className="text-sm text-ink-400">
            Nothing yet. Try <code className="text-ink-100">uv run asil ingest .</code> in any project.
          </p>
        )}
        <ul className="divide-y divide-ink-700">
          {stats?.repos.map((r, ri) => (
            <li
              key={r.key ?? `repo-${ri}`}
              className="flex items-center justify-between py-3 text-sm"
            >
              <div className="flex items-center gap-3">
                <Database size={14} className="text-accent-400" />
                <div>
                  <div className="text-ink-100 font-medium">{r.key}</div>
                  <div className="text-xs text-ink-400 flex items-center gap-2">
                    <GitCommit size={12} />
                    <span className="font-mono">{r.commit_sha?.slice(0, 7) ?? "—"}</span>
                    <span>·</span>
                    <Layers size={12} />
                    <span>{r.files} files</span>
                  </div>
                </div>
              </div>
              <div className="text-xs text-ink-400">
                {r.indexed_at ? new Date(r.indexed_at).toLocaleString() : "—"}
              </div>
            </li>
          ))}
        </ul>
      </Card>

      <Card
        title="Active LLM profile"
        subtitle="Tier-routed: tight (cheap) / balanced / generous (hero demo)"
      >
        <div className="flex items-center gap-3">
          <Sparkles size={16} className="text-accent-400" />
          <span className="font-mono text-ink-100">
            {stats?.llm_profile ?? "—"}
          </span>
        </div>
      </Card>
    </div>
  );
}
