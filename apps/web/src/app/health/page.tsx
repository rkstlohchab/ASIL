"use client";

import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";
import { Card } from "@/components/Card";
import { CheckCircle2, XCircle, HelpCircle } from "lucide-react";

const ICONS: Record<string, typeof CheckCircle2> = {
  ok: CheckCircle2,
  down: XCircle,
  unknown: HelpCircle,
};

export default function HealthPage() {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const tick = () =>
      api
        .get<Health>("/health")
        .then(setH)
        .catch((e) => setErr(String(e)));
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          Service health
        </h1>
        <p className="text-ink-300 mt-1">
          Auto-refreshing every 5s. Polls /health on the FastAPI gateway.
        </p>
      </header>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      <Card
        title="Overall"
        subtitle={`LLM profile: ${h?.active_llm_profile ?? "—"}`}
      >
        <div
          className={
            "text-2xl font-semibold " +
            (h?.status === "ok" ? "text-ok" : "text-warn")
          }
        >
          {h?.status ?? "loading…"}
        </div>
      </Card>

      <Card title="Backing services">
        <ul className="divide-y divide-ink-700">
          {h?.services.map((s) => {
            const Icon = ICONS[s.status] ?? HelpCircle;
            const tone =
              s.status === "ok"
                ? "text-ok"
                : s.status === "down"
                ? "text-danger"
                : "text-ink-400";
            return (
              <li
                key={s.name}
                className="py-3 flex items-center justify-between"
              >
                <div className="flex items-center gap-3">
                  <Icon size={16} className={tone} />
                  <div>
                    <div className="text-sm text-ink-100 capitalize">
                      {s.name}
                    </div>
                    <div className="text-xs text-ink-400">
                      {s.detail ?? "—"}
                    </div>
                  </div>
                </div>
                <span
                  className={`text-xs uppercase tracking-wider ${tone}`}
                >
                  {s.status}
                </span>
              </li>
            );
          })}
          {!h && <li className="text-sm text-ink-400 py-2">loading…</li>}
        </ul>
      </Card>
    </div>
  );
}
