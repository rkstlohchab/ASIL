"use client";

import { useEffect, useState } from "react";
import { api, type DashboardStats } from "@/lib/api";
import { Card } from "@/components/Card";
import { GitBranch, Loader2, ShieldAlert } from "lucide-react";

type DriftEvent = {
  kind: string;
  caller: string | null;
  callee: string | null;
  severity: string | null;
  description: string | null;
  boundary_name: string | null;
};

export default function DriftPage() {
  const [repos, setRepos] = useState<string[]>([]);
  const [repo, setRepo] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState<{
    repo_key: string;
    drift_events: DriftEvent[];
    count: number;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<DashboardStats>("/dashboard/stats")
      .then((s) => {
        const keys = s.repos.map((r) => r.key);
        setRepos(keys);
        if (keys.length > 0 && !repo) setRepo(keys[0]);
      })
      .catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function run() {
    if (!repo) return;
    setBusy(true);
    setErr(null);
    setData(null);
    try {
      const r = await api.callTool<{
        repo_key: string;
        drift_events: DriftEvent[];
        count: number;
      }>("asil.drift_check", { repo_key: repo });
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
          Architecture drift
        </h1>
        <p className="text-ink-300 mt-1">
          New dependencies and boundary violations versus a stored baseline.
          Use this before merging a PR.
        </p>
      </header>

      <Card>
        <div className="flex gap-2 items-end">
          <label className="flex-1">
            <span className="text-xs uppercase tracking-wider text-ink-400">
              Repo
            </span>
            <select
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              className="mt-1 w-full rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100 focus:outline-none focus:ring-2 focus:ring-accent-500"
            >
              <option value="">— pick a repo —</option>
              {repos.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={run}
            disabled={busy || !repo}
            className="rounded-md bg-accent-600 hover:bg-accent-500 disabled:bg-ink-700 text-white text-sm py-2 px-4 flex items-center gap-2 transition-colors"
          >
            {busy ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <GitBranch size={16} />
            )}
            Check drift
          </button>
        </div>
        <p className="text-[11px] text-ink-400 mt-3">
          Use{" "}
          <code className="text-ink-100">
            uv run asil drift baseline {repo || "{repo}"} --output baseline.json
          </code>{" "}
          to capture a snapshot first.
        </p>
      </Card>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      {data && (
        <Card
          title={`${data.count} drift event(s)`}
          subtitle={data.count === 0 ? "Clean. No drift." : "Review before merging"}
        >
          <ul className="divide-y divide-ink-700">
            {data.drift_events.map((e, i) => (
              <li key={i} className="py-3 flex items-start gap-3">
                <ShieldAlert
                  size={16}
                  className={
                    e.severity === "high"
                      ? "text-danger mt-0.5"
                      : e.severity === "medium"
                      ? "text-warn mt-0.5"
                      : "text-ink-400 mt-0.5"
                  }
                />
                <div className="flex-1">
                  <div className="text-sm text-ink-100">
                    {e.description ?? `${e.caller ?? "?"} → ${e.callee ?? "?"}`}
                  </div>
                  <div className="text-[11px] text-ink-400 mt-1 flex items-center gap-3">
                    <span className="uppercase tracking-wider">{e.kind}</span>
                    {e.severity && (
                      <span className="uppercase tracking-wider">
                        {e.severity}
                      </span>
                    )}
                    {e.boundary_name && (
                      <span className="font-mono">{e.boundary_name}</span>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
