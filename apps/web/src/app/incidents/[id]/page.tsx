"use client";

import { use, useEffect, useState } from "react";
import { api, type Confidence } from "@/lib/api";
import { Card } from "@/components/Card";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { CausalFlow } from "@/components/CausalFlow";
import {
  AlertOctagon,
  ArrowRight,
  Clock,
  GitCompare,
  Server,
} from "lucide-react";

type Replay = {
  incident_id: string;
  incident: Record<string, unknown>;
  summary_lines: string[];
  timeline: Array<{
    at: string;
    kind: string;
    service: string | null;
    description: string | null;
    marker: string | null;
  }>;
  top_causes: Array<{
    cause_kind: string;
    confidence: number;
    delta_seconds: number;
    derivation: string;
    strategy: string;
    cause_props: Record<string, unknown>;
  }>;
  service_cascade: Array<{
    service: string;
    first_event_at: string;
    first_event_kind: string;
    first_event_description: string | null;
  }>;
  state_diff: {
    services_involved: string[];
    deployments_during: Array<{
      deployment_id: string;
      service: string;
      description: string | null;
      commit_sha: string | null;
      at: string;
    }>;
    metric_deltas: Array<{
      service: string;
      metric: string;
      before: number | null;
      after: number | null;
      unit: string | null;
    }>;
  } | null;
  confidence: Confidence;
  error?: string;
};

export default function IncidentReplayPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [data, setData] = useState<Replay | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .callTool<Replay>("asil.replay_incident", { incident_id: id })
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, [id]);

  if (err) {
    return (
      <Card className="border-danger/40 bg-danger/10">
        <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
      </Card>
    );
  }
  if (!data) return <p className="text-sm text-ink-400">loading…</p>;
  if (data.error) return <p className="text-sm text-danger">{data.error}</p>;

  return (
    <div className="space-y-6">
      <header className="flex items-start gap-3">
        <AlertOctagon className="text-danger mt-1" size={22} />
        <div>
          <div className="text-[11px] uppercase tracking-wider text-ink-400">
            Incident replay
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-ink-50 font-mono">
            {data.incident_id}
          </h1>
          <p className="text-ink-300 mt-1">
            {String(data.incident.summary ?? "")}
          </p>
        </div>
      </header>

      <Card title="Causal chain" subtitle="Top observable causes → incident">
        <CausalFlow
          incidentId={data.incident_id}
          incidentLabel={String(data.incident.summary ?? data.incident_id)}
          causes={data.top_causes}
        />
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <Card
          className="lg:col-span-2"
          title="Timeline"
          subtitle="Time-ordered events from runtime namespace"
        >
          <ol className="relative border-l border-ink-700 pl-5 space-y-4">
            {data.timeline.map((e, i) => (
              <li key={i} className="relative">
                <span
                  className={
                    "absolute -left-[26px] top-1 w-3 h-3 rounded-full border " +
                    (e.marker === "incident"
                      ? "bg-danger border-danger"
                      : e.kind === "Deployment"
                      ? "bg-accent-500 border-accent-500"
                      : "bg-ink-700 border-ink-500")
                  }
                />
                <div className="text-xs text-ink-400 flex items-center gap-2">
                  <Clock size={12} />
                  {new Date(e.at).toLocaleString()}
                  <span className="text-ink-500">·</span>
                  <span className="uppercase tracking-wider">{e.kind}</span>
                  {e.service && (
                    <span className="text-accent-400">@ {e.service}</span>
                  )}
                </div>
                <div className="text-sm text-ink-100 mt-1">
                  {e.description ?? "—"}
                </div>
              </li>
            ))}
          </ol>
        </Card>

        <div className="space-y-5">
          <Card title="Confidence">
            <ConfidenceBar conf={data.confidence} />
          </Card>

          <Card title="Top causes (ranked)">
            <ul className="space-y-3 text-sm">
              {data.top_causes.map((c, i) => (
                <li
                  key={i}
                  className="rounded border border-ink-700 p-3"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-ink-100 font-medium">
                      {c.cause_kind}
                    </span>
                    <span className="text-xs tabular-nums text-ink-300">
                      {(c.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="text-[11px] text-ink-400 mt-1 uppercase tracking-wider">
                    {c.strategy} · Δ {(c.delta_seconds / 60).toFixed(1)}m
                  </div>
                  <div className="text-xs text-ink-300 mt-2">
                    {c.derivation}
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>

      {data.service_cascade.length > 0 && (
        <Card title="Service cascade" subtitle="Where the incident spread">
          <ol className="flex flex-wrap gap-2 items-center text-sm">
            {data.service_cascade.map((s, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className="rounded border border-accent-600/40 bg-accent-600/10 text-accent-400 px-2 py-1">
                  <Server size={12} className="inline mr-1" />
                  {s.service}
                </span>
                {i < data.service_cascade.length - 1 && (
                  <ArrowRight size={14} className="text-ink-500" />
                )}
              </li>
            ))}
          </ol>
        </Card>
      )}

      {data.state_diff && (
        <Card
          title="State diff"
          subtitle="What changed across the incident window"
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 text-sm">
            <div>
              <div className="text-xs uppercase tracking-wider text-ink-400 mb-2 flex items-center gap-1">
                <GitCompare size={12} /> Deployments during
              </div>
              <ul className="space-y-2">
                {data.state_diff.deployments_during.map((d) => (
                  <li
                    key={d.deployment_id}
                    className="rounded border border-ink-700 p-2"
                  >
                    <div className="text-ink-100 font-mono text-xs">
                      {d.deployment_id}
                    </div>
                    <div className="text-[11px] text-ink-400">
                      {d.service} ·{" "}
                      {d.commit_sha ? d.commit_sha.slice(0, 7) : "—"}
                    </div>
                    {d.description && (
                      <div className="text-xs text-ink-300 mt-1">
                        {d.description}
                      </div>
                    )}
                  </li>
                ))}
                {data.state_diff.deployments_during.length === 0 && (
                  <li className="text-ink-400 text-xs">None.</li>
                )}
              </ul>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-ink-400 mb-2">
                Metric deltas
              </div>
              <ul className="space-y-2">
                {data.state_diff.metric_deltas.map((m, i) => (
                  <li
                    key={i}
                    className="rounded border border-ink-700 p-2 text-xs"
                  >
                    <div className="text-ink-100">
                      {m.service}.{m.metric}
                    </div>
                    <div className="text-ink-400 tabular-nums">
                      {m.before ?? "—"} {m.unit ?? ""} →{" "}
                      <span className="text-warn">
                        {m.after ?? "—"} {m.unit ?? ""}
                      </span>
                    </div>
                  </li>
                ))}
                {data.state_diff.metric_deltas.length === 0 && (
                  <li className="text-ink-400 text-xs">None.</li>
                )}
              </ul>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
