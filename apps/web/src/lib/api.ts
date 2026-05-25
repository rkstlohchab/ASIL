/**
 * Tiny client for the ASIL FastAPI gateway.
 *
 * Two entry points:
 *   - `api.get(path)` / `api.post(path, body)`: REST endpoints (dashboard,
 *     incidents, health).
 *   - `api.callTool(name, args)`: invokes any MCP tool over the universal
 *     `/mcp/call/{tool}` dispatcher. Returns whatever the tool's `result` is.
 *
 * Both run against `NEXT_PUBLIC_ASIL_API_URL` (default http://localhost:8000).
 */

const BASE =
  process.env.NEXT_PUBLIC_ASIL_API_URL ?? "http://localhost:8000";

class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown) {
    super(`asil api ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "content-type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const text = await r.text();
  const body = text ? JSON.parse(text) : null;
  if (!r.ok) throw new ApiError(r.status, body);
  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  async callTool<T = unknown>(name: string, args: Record<string, unknown>) {
    const r = await request<{ tool: string; result?: T; error?: string }>(
      `/mcp/call/${name}`,
      { method: "POST", body: JSON.stringify({ arguments: args }) },
    );
    if (r.error) throw new ApiError(500, r.error);
    return r.result as T;
  },
};

export { ApiError, BASE };

// ---------------------------------------------------------------- shared shapes

export type Confidence = {
  score: number;
  evidence_count: number;
  retrieval_strength: number;
  causal_confidence: number;
  derivation: string[];
};

export type DashboardStats = {
  code: Record<string, number>;
  runtime: Record<string, number>;
  repos: Array<{
    key: string;
    spec: string | null;
    commit_sha: string | null;
    is_local: boolean | null;
    indexed_at: string | null;
    files: number;
  }>;
  envs: string[];
  memory_count: number;
  llm_profile: string;
};

export type IncidentRow = {
  incident_id: string;
  detected_at: string;
  severity: string | null;
  summary: string | null;
  env_key: string | null;
  services: string[];
};

export type Health = {
  status: "ok" | "degraded";
  services: Array<{ name: string; status: string; detail: string | null }>;
  active_llm_profile: string;
};
