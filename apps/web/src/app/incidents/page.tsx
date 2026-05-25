"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type IncidentRow } from "@/lib/api";
import { Card } from "@/components/Card";
import { AlertOctagon, ChevronRight, Clock, Server } from "lucide-react";

export default function IncidentsPage() {
  const [rows, setRows] = useState<IncidentRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ incidents: IncidentRow[]; count: number }>("/incidents")
      .then((r) => setRows(r.incidents))
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Incidents
        </h1>
        <p className="text-ink-300 mt-1">
          Every postmortem ingested into ASIL. Pick one to replay its timeline,
          causal chain, and architecture diff.
        </p>
      </header>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      <Card>
        {rows === null && <p className="text-sm text-ink-400">loading…</p>}
        {rows && rows.length === 0 && (
          <p className="text-sm text-ink-400">
            None yet. Try{" "}
            <code className="text-ink-100">
              uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
            </code>
          </p>
        )}
        <ul className="divide-y divide-ink-700">
          {rows?.map((i) => (
            <li key={i.incident_id}>
              <Link
                href={`/incidents/${encodeURIComponent(i.incident_id)}`}
                className="flex items-center gap-4 py-4 hover:bg-ink-800/40 -mx-2 px-2 rounded transition-colors"
              >
                <AlertOctagon
                  size={18}
                  className={
                    i.severity === "critical" || i.severity === "sev1"
                      ? "text-danger"
                      : i.severity === "sev2"
                      ? "text-warn"
                      : "text-ink-400"
                  }
                />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-ink-100">
                    {i.incident_id}
                  </div>
                  <div className="text-xs text-ink-400 truncate mt-0.5">
                    {i.summary ?? "—"}
                  </div>
                  <div className="text-[11px] text-ink-400 flex items-center gap-3 mt-1">
                    <span className="flex items-center gap-1">
                      <Clock size={12} />
                      {i.detected_at
                        ? new Date(i.detected_at).toLocaleString()
                        : "—"}
                    </span>
                    {i.services.length > 0 && (
                      <span className="flex items-center gap-1">
                        <Server size={12} />
                        {i.services.join(", ")}
                      </span>
                    )}
                    {i.env_key && (
                      <span className="font-mono">{i.env_key}</span>
                    )}
                  </div>
                </div>
                <ChevronRight size={16} className="text-ink-400" />
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
