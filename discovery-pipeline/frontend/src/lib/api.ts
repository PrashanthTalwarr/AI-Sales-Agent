/**
 * Where the API lives.
 *
 * Set NEXT_PUBLIC_API_BASE to point at a running FastAPI backend (local dev uses
 * frontend/.env.local). Leave it unset — as on the hosted demo — and requests go
 * to this app's own /api routes, which serve a snapshot of a real pipeline run.
 */
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/** True when no backend is configured and the built-in demo routes are serving. */
export const IS_DEMO = BASE === "";

export interface Contact {
  name: string;
  role: string;
  email: string;
  twitter: string;
  github: string;
  source: string;
  confidence: string;
}

export interface Lead {
  protocol: string;
  score: number;
  tier: "hot" | "warm" | "cool";
  tvl_score: number;
  audit_score: number;
  vel_score: number;
  fund_score: number;
  reach_score: number;
  rationale: string;
  contacts: Contact[];
}

export interface Draft {
  protocol: string;
  persona: string;
  role: string;
  channel: string;
  step: number;
  subject: string;
  body: string;
  model: string;
  contact_email: string;
  contact_twitter: string;
  contact_github: string;
  contact_source: string;
}

export interface ToolCall {
  tool: string;
  input: Record<string, unknown>;
}

export interface ChatResponse {
  response: string;
  tool_calls: ToolCall[];
}

export interface Summary {
  total: number;
  hot: number;
  warm: number;
  drafts: number;
  last_run: string | null;
}

export interface SentOutreach {
  protocol_name: string;
  persona_name: string;
  persona_role: string;
  to_email: string;
  subject: string;
  sent_at: string;
  status: "sent" | "replied";
  score: number | null;
  tier: string;
  tvl_usd: number | null;
}

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  estimated_cost_usd: number;
}

export interface LoadResult {
  loaded: boolean;
  total: number;
  hot: number;
  warm: number;
  drafts: number;
  last_run: string | null;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? res.statusText);
  }
  return res.json();
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? res.statusText);
  }
  return res.json();
}

export const api = {
  getLeads: () =>
    get<{ leads: Lead[]; last_run: string | null }>("/api/leads"),

  getSummary: () =>
    get<Summary>("/api/summary"),

  getDraft: (protocol: string) =>
    get<Draft>(`/api/leads/${encodeURIComponent(protocol)}/draft`),

  getAllDrafts: (protocol: string) =>
    get<{ protocol: string; drafts: Draft[] }>(`/api/leads/${encodeURIComponent(protocol)}/drafts`),

  loadResults: () =>
    post<LoadResult>("/api/pipeline/load"),

  clearChat: () =>
    post<{ cleared: boolean }>("/api/chat/clear"),

  getSentOutreach: () =>
    get<{ results: SentOutreach[] }>("/api/outreach/sent"),

  /** Send one drafted email on demand, to the test recipient. */
  sendDraft: (protocol_name: string, persona_name: string, test_email: string) =>
    post<{
      sent: boolean;
      to: string;
      real_recipient: string;
      protocol: string;
      persona: string;
      id: string;
    }>("/api/outreach/send", { protocol_name, persona_name, test_email }),

  markReplied: (protocol_name: string, persona_name: string, reply_body: string) =>
    post<{ updated: boolean; protocol: string; persona: string }>(
      "/api/outreach/replied",
      { protocol_name, persona_name, reply_body }
    ),

  /**
   * Returns the EventSource URL for pipeline streaming.
   * testEmail redirects every outreach email to that address for this run.
   * If it is empty the backend falls back to RESEND_TEST_EMAIL, and if that is
   * empty too it sends nothing — real contacts are never emailed.
   */
  pipelineRunUrl: (testEmail?: string) =>
    testEmail
      ? `${BASE}/api/pipeline/run?test_email=${encodeURIComponent(testEmail)}`
      : `${BASE}/api/pipeline/run`,

  /** Returns the URL for chat streaming */
  chatStreamUrl: () => `${BASE}/api/chat/stream`,

  getTokenUsage: () =>
    get<TokenUsage>("/api/tokens"),

  resetTokenUsage: () =>
    post<{ reset: boolean }>("/api/tokens/reset"),
};
