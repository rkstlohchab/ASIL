"use client";

import { useEffect, useState } from "react";
import { api, BASE } from "@/lib/api";
import { Card } from "@/components/Card";
import { Copy, Plug, Wrench } from "lucide-react";

type Tool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};

export default function MCPPage() {
  const [tools, setTools] = useState<Tool[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<Tool[]>("/mcp/tools")
      .then(setTools)
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-ink-50">
          MCP tool catalog
        </h1>
        <p className="text-ink-300 mt-1">
          The surface ASIL exposes to any AI agent (Claude Code, Cursor,
          OpenHands, Aider, Cody). HTTP today; native stdio MCP in Phase 7.
        </p>
      </header>

      <Card title="Endpoint">
        <div className="flex items-center justify-between gap-3">
          <code className="text-sm text-ink-100 font-mono break-all">
            POST {BASE}/mcp/call/&lt;tool_name&gt;
          </code>
          <button
            onClick={() => {
              navigator.clipboard.writeText(`${BASE}/mcp/call/`);
            }}
            className="text-ink-400 hover:text-ink-100"
            title="copy"
          >
            <Copy size={14} />
          </button>
        </div>
        <p className="text-xs text-ink-400 mt-2">
          Body: <code>{`{"arguments": { ... }}`}</code>
        </p>
      </Card>

      {err && (
        <Card className="border-danger/40 bg-danger/10">
          <pre className="text-[11px] text-ink-300 overflow-x-auto">{err}</pre>
        </Card>
      )}

      <Card title={`${tools?.length ?? "—"} tool(s)`}>
        <ul className="divide-y divide-ink-700">
          {tools?.map((t) => {
            const isOpen = open === t.name;
            return (
              <li key={t.name} className="py-4">
                <button
                  onClick={() => setOpen(isOpen ? null : t.name)}
                  className="flex items-start gap-3 text-left w-full hover:bg-ink-800/40 -mx-2 px-2 py-1 rounded transition-colors"
                >
                  <Plug size={14} className="text-accent-400 mt-1" />
                  <div className="flex-1">
                    <div className="text-sm font-mono text-ink-100">
                      {t.name}
                    </div>
                    <div className="text-xs text-ink-400 mt-1">
                      {t.description}
                    </div>
                  </div>
                  <Wrench
                    size={14}
                    className={
                      isOpen ? "text-accent-400 mt-1" : "text-ink-500 mt-1"
                    }
                  />
                </button>
                {isOpen && (
                  <pre className="mt-3 p-3 rounded bg-ink-900 border border-ink-700 text-[11px] text-ink-300 overflow-x-auto">
                    {JSON.stringify(t.input_schema, null, 2)}
                  </pre>
                )}
              </li>
            );
          })}
        </ul>
      </Card>

      <Card
        title="Wiring example — Claude Code"
        subtitle="Add ASIL as a remote MCP server"
      >
        <pre className="text-[11px] text-ink-300 p-3 rounded bg-ink-900 border border-ink-700 overflow-x-auto">
{`# ~/.claude/settings.json
{
  "mcpServers": {
    "asil": {
      "type": "http",
      "url": "${BASE}/mcp"
    }
  }
}`}
        </pre>
      </Card>
    </div>
  );
}
