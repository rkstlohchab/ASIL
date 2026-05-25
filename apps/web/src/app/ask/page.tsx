"use client";

import { useState } from "react";
import { api, type Confidence } from "@/lib/api";
import { Card } from "@/components/Card";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { Loader2, Send, AlertCircle, BookOpen } from "lucide-react";

type Candidate = {
  qualified_name?: string;
  file_path?: string;
  line?: number;
  kind?: string;
  similarity?: number;
  derivation?: string;
};

type AskResult = {
  question: string;
  answer?: string;
  candidates?: Candidate[];
  confidence?: Confidence;
  memory_hits?: Array<{ id: string; question: string; similarity: number }>;
  verifier?: {
    claims: Array<{ text: string; supported: boolean }>;
    unsupported_count: number;
  };
};

export default function AskPage() {
  const [q, setQ] = useState("How does the LLM router pick a provider for a given tier?");
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<AskResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      const args: Record<string, unknown> = { question: q };
      if (repo.trim()) args.repo_key = repo.trim();
      const r = await api.callTool<AskResult>("asil.ask", args);
      setRes(r);
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
          Ask the codebase
        </h1>
        <p className="text-ink-300 mt-1">
          Hybrid vector + graph retrieval, verified claims, confidence-scored answers.
          Past answers are cached in episodic memory.
        </p>
      </header>

      <Card>
        <div className="space-y-3">
          <label className="block">
            <span className="text-xs uppercase tracking-wider text-ink-400">
              Question
            </span>
            <textarea
              value={q}
              onChange={(e) => setQ(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100 focus:outline-none focus:ring-2 focus:ring-accent-500"
            />
          </label>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
            <label className="block md:col-span-2">
              <span className="text-xs uppercase tracking-wider text-ink-400">
                Repo key (optional)
              </span>
              <input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="local:/path or org/name"
                className="mt-1 w-full rounded-md bg-ink-900 border border-ink-700 px-3 py-2 text-sm text-ink-100 focus:outline-none focus:ring-2 focus:ring-accent-500"
              />
            </label>
            <button
              onClick={submit}
              disabled={busy || !q.trim()}
              className="rounded-md bg-accent-600 hover:bg-accent-500 disabled:bg-ink-700 text-white text-sm py-2 px-4 flex items-center justify-center gap-2 transition-colors"
            >
              {busy ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
              <span>{busy ? "Asking…" : "Ask"}</span>
            </button>
          </div>
        </div>
      </Card>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <div className="flex items-center gap-2 text-danger">
            <AlertCircle size={16} />
            <span className="font-medium">Request failed</span>
          </div>
          <pre className="mt-2 text-[11px] text-ink-400 overflow-x-auto">{err}</pre>
        </Card>
      )}

      {res && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="lg:col-span-2 space-y-5">
            <Card title="Answer" subtitle="Synthesised from retrieved evidence and verified">
              {res.answer ? (
                <pre className="whitespace-pre-wrap text-sm text-ink-100 leading-relaxed font-sans">
                  {res.answer}
                </pre>
              ) : (
                <p className="text-sm text-ink-400">No answer returned.</p>
              )}
            </Card>

            <Card title="Citations" subtitle="Hybrid retriever — vector + graph">
              {res.candidates && res.candidates.length > 0 ? (
                <ul className="divide-y divide-ink-700">
                  {res.candidates.map((c, i) => (
                    <li key={i} className="py-3 text-sm">
                      <div className="flex items-start gap-2">
                        <BookOpen size={14} className="text-accent-400 mt-0.5" />
                        <div className="flex-1">
                          <div className="font-mono text-ink-100">
                            {c.qualified_name ?? "—"}
                          </div>
                          <div className="text-xs text-ink-400 mt-0.5">
                            {c.file_path}
                            {c.line ? `:${c.line}` : ""} · {c.kind ?? ""}
                            {typeof c.similarity === "number" &&
                              ` · sim ${(c.similarity * 100).toFixed(1)}%`}
                          </div>
                          {c.derivation && (
                            <div className="text-[11px] text-ink-400 mt-0.5">
                              {c.derivation}
                            </div>
                          )}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-ink-400">No citations.</p>
              )}
            </Card>

            {res.verifier && (
              <Card
                title="Verifier"
                subtitle={`${res.verifier.unsupported_count} unsupported claim(s)`}
              >
                <ul className="space-y-2 text-sm">
                  {res.verifier.claims.map((c, i) => (
                    <li
                      key={i}
                      className="flex items-start gap-2 text-ink-200"
                    >
                      <span
                        className={
                          c.supported
                            ? "mt-0.5 text-ok"
                            : "mt-0.5 text-danger"
                        }
                      >
                        {c.supported ? "✓" : "✗"}
                      </span>
                      <span>{c.text}</span>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </div>

          <div className="space-y-5">
            {res.confidence && (
              <Card title="Confidence">
                <ConfidenceBar conf={res.confidence} />
              </Card>
            )}

            {res.memory_hits && res.memory_hits.length > 0 && (
              <Card title="Memory hits" subtitle="Past conclusions reused">
                <ul className="text-sm space-y-2">
                  {res.memory_hits.map((m) => (
                    <li key={m.id} className="text-ink-300">
                      <div className="text-ink-100 truncate">{m.question}</div>
                      <div className="text-xs text-ink-400">
                        sim {(m.similarity * 100).toFixed(1)}%
                      </div>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
