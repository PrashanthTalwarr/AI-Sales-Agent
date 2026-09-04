"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { api, waitForApi, IS_DEMO, Health, Lead, Draft, ToolCall, TokenUsage } from "@/lib/api";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Message {
  id: string;
  role: "user" | "agent";
  content: string;
  tool_calls?: ToolCall[];
  timestamp: Date;
  streaming?: boolean;
}

type PipelineStatus = "idle" | "running" | "done" | "error";

// ── Helpers ───────────────────────────────────────────────────────────────────

function uid() {
  return Math.random().toString(36).slice(2);
}

// ── Test recipient ────────────────────────────────────────────────────────────
// Every outreach email is redirected to this single address. Replace it with
// your own so the emails land in an inbox you can actually check.

const DEFAULT_TEST_EMAIL = "dr.nobody0501@gmail.com";
const TEST_EMAIL_KEY = "discovery.testEmail";

function isValidEmail(value: string) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

/** localStorage throws in some privacy contexts — never let that break the page. */
function readStoredTestEmail(): string {
  try {
    return localStorage.getItem(TEST_EMAIL_KEY) ?? DEFAULT_TEST_EMAIL;
  } catch {
    return DEFAULT_TEST_EMAIL;
  }
}

function storeTestEmail(value: string) {
  try {
    localStorage.setItem(TEST_EMAIL_KEY, value);
  } catch {
    /* ignore — the field still works for this session */
  }
}

function TierBadge({ tier }: { tier: string }) {
  const styles: Record<string, string> = {
    hot:  "bg-red-500/20 text-red-400 border border-red-500/30",
    warm: "bg-amber-500/20 text-amber-400 border border-amber-500/30",
    cool: "bg-gray-500/20 text-gray-400 border border-gray-500/30",
  };
  const icons: Record<string, string> = { hot: "🔥", warm: "🟡", cool: "⚪" };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${styles[tier] ?? styles.cool}`}>
      {icons[tier] ?? ""} {tier}
    </span>
  );
}

// ── Leads Panel ───────────────────────────────────────────────────────────────

function LeadsPanel({
  leads,
  onSelect,
  selectedProtocol,
}: {
  leads: Lead[];
  onSelect: (lead: Lead) => void;
  selectedProtocol: string | null;
}) {
  if (leads.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-600 text-sm px-4 text-center">
        No leads yet.<br />Run the pipeline or load last results.
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {leads.map((lead) => (
        <button
          key={lead.protocol}
          onClick={() => onSelect(lead)}
          className={`w-full text-left px-4 py-3 border-b border-discovery-border hover:bg-discovery-border/50 transition-colors ${
            selectedProtocol === lead.protocol ? "bg-discovery-border/70" : ""
          }`}
        >
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-gray-200 truncate mr-2">{lead.protocol}</span>
            <TierBadge tier={lead.tier} />
          </div>
          <div className="flex items-center gap-3 text-xs text-gray-500">
            <span className="font-mono font-bold text-purple-400">{lead.score.toFixed(0)}</span>
            <span>TVL {lead.tvl_score.toFixed(0)}</span>
            <span>Aud {lead.audit_score.toFixed(0)}</span>
            <span>Vel {lead.vel_score.toFixed(0)}</span>
          </div>
          {lead.contacts && lead.contacts.length > 0 && (
            <div className="mt-1 text-xs text-blue-400/70">
              {lead.contacts.length} contact{lead.contacts.length !== 1 ? "s" : ""} found
            </div>
          )}
        </button>
      ))}
    </div>
  );
}

// ── Draft Drawer ──────────────────────────────────────────────────────────────

function SourceBadge({ source }: { source: string }) {
  if (source === "github") return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-gray-700/60 text-gray-300">
      GitHub
    </span>
  );
  if (source === "web_search") return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-blue-900/40 text-blue-300">
      Web
    </span>
  );
  return null;
}

type SendState = { status: "idle" | "sending" | "sent" | "error"; message: string };

function DraftDrawer({
  protocol,
  testEmail,
  onClose,
  onAskAgent,
}: {
  protocol: string;
  testEmail: string;
  onClose: () => void;
  onAskAgent: (msg: string) => void;
}) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [send, setSend] = useState<SendState>({ status: "idle", message: "" });

  useEffect(() => {
    setLoading(true);
    setError(null);
    setSelectedIdx(0);
    api.getAllDrafts(protocol)
      .then((res) => setDrafts(res.drafts))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [protocol]);

  // Switching person clears any previous send result
  useEffect(() => setSend({ status: "idle", message: "" }), [selectedIdx, protocol]);

  const draft = drafts[selectedIdx] ?? null;

  const handleSend = async () => {
    if (!draft) return;
    const to = testEmail.trim();
    if (!isValidEmail(to)) {
      setSend({ status: "error", message: "Set a valid address in \"Send test emails to:\" first." });
      return;
    }
    setSend({ status: "sending", message: "" });
    try {
      const res = await api.sendDraft(draft.protocol, draft.persona, to);
      setSend({ status: "sent", message: `Delivered to ${res.to} · id ${res.id.slice(0, 8)}` });
    } catch (e) {
      setSend({ status: "error", message: e instanceof Error ? e.message : "Send failed." });
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/60" onClick={onClose} />
      <div className="w-[560px] bg-discovery-surface border-l border-discovery-border flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-discovery-border shrink-0">
          <div>
            <h2 className="font-semibold text-gray-200">{protocol}</h2>
            {drafts.length > 0 && (
              <p className="text-xs text-gray-500 mt-0.5">
                {drafts.length} personalized email{drafts.length !== 1 ? "s" : ""} ready
              </p>
            )}
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none">×</button>
        </div>

        {/* Person tabs */}
        {drafts.length > 1 && (
          <div className="flex gap-1 px-4 py-2 border-b border-discovery-border overflow-x-auto shrink-0">
            {drafts.map((d, i) => (
              <button
                key={i}
                onClick={() => setSelectedIdx(i)}
                className={`shrink-0 px-3 py-1.5 rounded-lg text-xs transition-colors ${
                  i === selectedIdx
                    ? "bg-purple-600 text-white"
                    : "text-gray-400 hover:text-gray-200 hover:bg-discovery-border/50"
                }`}
              >
                {d.persona.split(" ")[0]}
              </button>
            ))}
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {loading && <div className="text-gray-500 text-sm">Finding contacts and generating emails...</div>}
          {error && (
            <div className="text-red-400 text-sm">
              {error}
              <button
                className="ml-3 text-purple-400 underline text-xs"
                onClick={() => onAskAgent(`generate outreach for ${protocol}`)}
              >
                Generate via agent
              </button>
            </div>
          )}

          {draft && (
            <>
              {/* Contact card */}
              <div className="border border-discovery-border rounded-lg p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <div>
                    <span className="text-gray-200 font-medium text-sm">{draft.persona}</span>
                    <span className="text-gray-500 text-xs ml-2">{draft.role}</span>
                  </div>
                  <SourceBadge source={draft.contact_source} />
                </div>
                <div className="flex flex-wrap gap-3 text-xs">
                  {draft.contact_email && (
                    <a
                      href={`mailto:${draft.contact_email}`}
                      className="flex items-center gap-1 text-green-400 hover:underline"
                    >
                      <span>✉</span> {draft.contact_email}
                    </a>
                  )}
                  {draft.contact_github && (
                    <a
                      href={`https://github.com/${draft.contact_github}`}
                      target="_blank" rel="noreferrer"
                      className="text-gray-400 hover:text-gray-200 hover:underline"
                    >
                      gh/{draft.contact_github}
                    </a>
                  )}
                  {draft.contact_twitter && (
                    <span className="text-gray-400">{draft.contact_twitter}</span>
                  )}
                  {!draft.contact_email && !draft.contact_github && !draft.contact_twitter && (
                    <span className="text-gray-600 italic">No contact details found</span>
                  )}
                </div>
              </div>

              {/* Email */}
              <div className="border border-discovery-border rounded-lg p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="text-xs text-gray-500 font-medium uppercase tracking-wider">Email</div>
                  <span className="text-xs text-gray-600">{draft.model}</span>
                </div>
                <div className="text-gray-200 font-medium text-sm">{draft.subject}</div>
                <hr className="border-discovery-border" />
                <pre className="text-gray-300 text-sm whitespace-pre-wrap leading-relaxed">
                  {draft.body.split("[Book a call]").map((part, i, arr) =>
                    i < arr.length - 1 ? (
                      <span key={i}>
                        {part}
                        <a href="#" onClick={e => e.preventDefault()} className="text-blue-400 underline">Book a call</a>
                      </span>
                    ) : part
                  )}
                </pre>
              </div>
            </>
          )}
        </div>

        {/* Send bar */}
        {draft && (
          <div className="border-t border-discovery-border px-5 py-3 shrink-0 bg-discovery-surface">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xs text-gray-500 min-w-0">
                {send.status === "sent" ? (
                  <span className="text-green-400">✓ {send.message}</span>
                ) : send.status === "error" ? (
                  <span className="text-red-400 break-words">{send.message}</span>
                ) : (
                  <>
                    Test send goes to{" "}
                    <span className="text-purple-300 font-mono">{testEmail.trim() || "— not set —"}</span>
                    {draft.contact_email && (
                      <>
                        , not to{" "}
                        <span className="font-mono text-gray-400">{draft.contact_email}</span>
                      </>
                    )}
                  </>
                )}
              </div>
              <button
                onClick={handleSend}
                disabled={send.status === "sending"}
                className="shrink-0 px-4 py-2 text-xs rounded-lg bg-purple-600 hover:bg-purple-500 text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {send.status === "sending"
                  ? "Sending..."
                  : send.status === "sent"
                  ? "Send again"
                  : "Send test email"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Pipeline Log Modal ────────────────────────────────────────────────────────

function PipelineModal({
  status,
  logs,
  testEmail,
  onClose,
}: {
  status: PipelineStatus;
  logs: string[];
  testEmail: string;
  onClose: () => void;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div className="w-[680px] max-h-[80vh] bg-discovery-surface border border-discovery-border rounded-xl flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-discovery-border">
          <div className="flex items-center gap-3">
            <h2 className="font-semibold text-gray-200">Pipeline Run</h2>
            {status === "running" && (
              <span className="flex items-center gap-1.5 text-xs text-amber-400">
                <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                Running...
              </span>
            )}
            {status === "done" && (
              <span className="text-xs text-green-400">✓ Complete</span>
            )}
            {status === "error" && (
              <span className="text-xs text-red-400">✗ Error</span>
            )}
          </div>
          {status !== "running" && (
            <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none">×</button>
          )}
        </div>
        <div className="px-5 py-2 border-b border-discovery-border bg-purple-500/5 text-xs text-gray-400">
          Test mode — every outreach email is delivered to{" "}
          <span className="text-purple-300 font-mono">{testEmail}</span>, never to the real
          contacts. Swap in your own address in the header to receive them yourself.
        </div>
        <div ref={logRef} className="flex-1 overflow-y-auto p-4 font-mono text-xs text-gray-400 space-y-0.5 bg-black/30">
          {logs.map((line, i) => (
            <div key={i} className={
              line.includes("✓") ? "text-green-400" :
              line.includes("✗") || line.includes("ERROR") ? "text-red-400" :
              line.includes("===") ? "text-purple-400 font-bold" :
              line.includes("🔥") ? "text-red-400" :
              line.includes("🟡") ? "text-amber-400" :
              "text-gray-400"
            }>
              {line}
            </div>
          ))}
          {status === "running" && (
            <div className="text-gray-600 animate-pulse">▊</div>
          )}
        </div>
        {status === "done" && (
          <div className="border-t border-discovery-border p-4">
            <button
              onClick={onClose}
              className="w-full py-2 text-sm bg-purple-600 hover:bg-purple-500 text-white rounded-lg transition-colors"
            >
              Done — View Results
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Chat Message ──────────────────────────────────────────────────────────────

function ChatMessage({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";
  const text = msg.content.replace(/\*\*/g, "");
  const isMultiline = text.includes("\n") || text.includes("─");

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      <div className={`max-w-[80%] ${isUser ? "order-2" : "order-1"}`}>
        {!isUser && msg.tool_calls && msg.tool_calls.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {msg.tool_calls.map((tc, i) => (
              <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-purple-900/40 border border-purple-700/30 text-purple-300 text-xs">
                <span className="opacity-60">⚡</span>
                {tc.tool.replace(/_/g, " ")}
              </span>
            ))}
          </div>
        )}
        <div className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
          isUser
            ? "bg-purple-600 text-white rounded-br-sm"
            : "bg-discovery-surface border border-discovery-border text-gray-200 rounded-bl-sm"
        }`}>
          {isMultiline ? (
            <pre className="whitespace-pre-wrap font-mono text-xs leading-5">
              {text}
              {msg.streaming && <span className="animate-pulse ml-0.5">▊</span>}
            </pre>
          ) : (
            <span>
              {text}
              {msg.streaming && <span className="animate-pulse ml-0.5">▊</span>}
            </span>
          )}
        </div>
        <div className="text-xs text-gray-600 mt-1 px-1" suppressHydrationWarning>
          {msg.timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: uid(),
      role: "agent",
      content: "Hi — I'm the Discovery Pipeline Agent. Run the pipeline or load last results, then ask me anything about your leads, outreach messages, or market events.",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [selectedProtocol, setSelectedProtocol] = useState<string | null>(null);
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus>("idle");
  const [pipelineLogs, setPipelineLogs] = useState<string[]>([]);
  const [showPipelineModal, setShowPipelineModal] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  // Server-render with the default, then hydrate from localStorage in an effect
  // so the markup matches on first paint.
  const [testEmail, setTestEmail] = useState(DEFAULT_TEST_EMAIL);
  const [testEmailError, setTestEmailError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [waking, setWaking] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const refreshTokens = useCallback(async () => {
    try {
      const data = await api.getTokenUsage();
      setTokenUsage(data);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    refreshTokens();
    const interval = setInterval(refreshTokens, 5000);
    return () => clearInterval(interval);
  }, [refreshTokens]);

  // Restore the saved test recipient after mount (localStorage is client-only)
  useEffect(() => {
    setTestEmail(readStoredTestEmail());
  }, []);

  // Wake the API if it is asleep, then pull whatever it already has loaded.
  // A free-tier host takes ~50s to spin up; showing that beats a blank screen.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const quick = await Promise.race([
        fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? ""}/api/health`).then((r) => r.ok).catch(() => false),
        new Promise<boolean>((r) => setTimeout(() => r(false), 2500)),
      ]);
      if (cancelled) return;
      if (!quick) setWaking(true);
      const h = await waitForApi();
      if (cancelled) return;
      setWaking(false);
      setHealth(h);
      if (h && h.leads > 0) refreshLeads();
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshLeads = useCallback(async () => {
    try {
      const data = await api.getLeads();
      setLeads(data.leads);
      setLoadError(null);
    } catch (e) {
      // A silent failure here is indistinguishable from "no results yet", which
      // hides the most common deployment fault: the API is up but its CORS
      // allowlist does not include this origin, so the browser blocks the call.
      const msg = e instanceof Error ? e.message : String(e);
      setLoadError(
        /failed to fetch|networkerror|load failed/i.test(msg)
          ? "Could not reach the API. If it is deployed, its CORS_ORIGINS must include this site's URL."
          : msg
      );
    }
  }, []);

  const sendMessage = useCallback(async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || isLoading) return;
    setInput("");

    const userMsg: Message = { id: uid(), role: "user", content, timestamp: new Date() };
    const agentId = uid();
    const agentMsg: Message = { id: agentId, role: "agent", content: "", timestamp: new Date(), streaming: true };
    setMessages((prev) => [...prev, userMsg, agentMsg]);
    setIsLoading(true);

    try {
      const response = await fetch(api.chatStreamUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: content }),
      });

      if (!response.body) throw new Error("No response body");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const data = JSON.parse(line.slice(6));

          if (data.type === "token") {
            setMessages((prev) => prev.map((m) =>
              m.id === agentId ? { ...m, content: m.content + data.text } : m
            ));
          } else if (data.type === "done") {
            setMessages((prev) => prev.map((m) =>
              m.id === agentId ? { ...m, tool_calls: data.tool_calls, streaming: false } : m
            ));
            if (data.refresh) await refreshLeads();
          } else if (data.type === "error") {
            setMessages((prev) => prev.map((m) =>
              m.id === agentId ? { ...m, content: `Error: ${data.text}`, streaming: false } : m
            ));
          }
        }
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Request failed";
      setMessages((prev) => prev.map((m) =>
        m.id === agentId ? { ...m, content: `Error: ${msg}`, streaming: false } : m
      ));
    } finally {
      setIsLoading(false);
      inputRef.current?.focus();
      refreshTokens();
    }
  }, [input, isLoading, refreshLeads, refreshTokens]);

  const handleLoadResults = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await api.loadResults();
      if (!res.loaded) {
        setMessages((prev) => [...prev, {
          id: uid(), role: "agent",
          content: "No leads in the database yet. Run the pipeline first to discover and score protocols.",
          timestamp: new Date(),
        }]);
        return;
      }
      await refreshLeads();
      const msg = `Loaded ${res.total} leads (${res.hot} hot, ${res.warm} warm) from the database. Last run: ${res.last_run ?? "unknown"}.`;
      setMessages((prev) => [...prev, { id: uid(), role: "agent", content: msg, timestamp: new Date() }]);
    } catch (e: unknown) {
      const err = e instanceof Error ? e.message : "Failed";
      setMessages((prev) => [...prev, { id: uid(), role: "agent", content: `Error: ${err}`, timestamp: new Date() }]);
    } finally {
      setIsLoading(false);
    }
  }, [refreshLeads]);

  // The API reports whether live runs are permitted; honour it rather than
  // letting the click fail as an opaque EventSource connection error.
  const runsDisabled = !IS_DEMO && health !== null && !health.pipeline_runs_allowed;

  const handleRunPipeline = useCallback(() => {
    const recipient = testEmail.trim();
    if (!isValidEmail(recipient)) {
      setTestEmailError(
        recipient
          ? "That does not look like a valid email address."
          : "Enter a test recipient — without one, nothing will be sent."
      );
      return;
    }
    setTestEmailError(null);
    storeTestEmail(recipient);

    setPipelineLogs([]);
    setPipelineStatus("running");
    setShowPipelineModal(true);

    const es = new EventSource(api.pipelineRunUrl(recipient));

    es.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "done") {
        es.close();
        setPipelineStatus("done");
        refreshLeads();
        refreshTokens();
        setMessages((prev) => [...prev, {
          id: uid(),
          role: "agent",
          content: "Pipeline run complete. Results are loaded — ask me anything about your leads.",
          timestamp: new Date(),
        }]);
      } else if (data.type === "log") {
        setPipelineLogs((prev) => [...prev, data.text]);
      }
    };

    es.onerror = () => {
      es.close();
      // Losing the stream does not mean the run died — it keeps going server-side.
      // Poll health and finish properly when it completes, instead of leaving the
      // user with an error for a run that actually succeeded.
      (async () => {
        // Distinguish "the stream dropped mid-run" from "the run never started".
        // Both surface as an EventSource error, but only one of them means the
        // pipeline is working — claiming a run is in progress when the request
        // was refused sends the user off waiting for nothing.
        let started = false;
        try {
          const h0 = await fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? ""}/api/health`, {
            cache: "no-store",
          }).then((r) => r.json());
          setHealth(h0);
          started = h0.pipeline_run_in_progress;
          if (!started) {
            setPipelineStatus("error");
            setPipelineLogs((prev) => [
              ...prev,
              h0.pipeline_runs_allowed
                ? "The run was refused — another run may have just finished (they are rate limited)."
                : "Live pipeline runs are disabled on this deployment. The results on screen came from a real run.",
            ]);
            return;
          }
        } catch {
          setPipelineStatus("error");
          setPipelineLogs((prev) => [
            ...prev,
            "Could not reach the API. If it is deployed, check that CORS_ORIGINS or CORS_ORIGIN_REGEX allows this site.",
          ]);
          return;
        }

        setPipelineLogs((prev) => [
          ...prev,
          "Log stream dropped — the run is still going. Waiting for it to finish...",
        ]);
        for (let i = 0; i < 120; i++) {
          await new Promise((r) => setTimeout(r, 5000));
          try {
            const h = await fetch(`${process.env.NEXT_PUBLIC_API_BASE ?? ""}/api/health`, {
              cache: "no-store",
            }).then((r) => r.json());
            if (!h.pipeline_run_in_progress) {
              setPipelineStatus("done");
              setPipelineLogs((prev) => [...prev, "Run finished. Results reloaded."]);
              refreshLeads();
              refreshTokens();
              return;
            }
          } catch {
            /* API still waking or briefly unreachable */
          }
        }
        setPipelineStatus("error");
        setPipelineLogs((prev) => [...prev, "Gave up waiting — check the API logs."]);
      })();
    };
  }, [refreshLeads, refreshTokens, testEmail]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="h-screen flex flex-col bg-discovery-bg text-gray-200 overflow-hidden">
      {/* Header */}
      <header className="flex items-center justify-between px-5 py-3 border-b border-discovery-border bg-discovery-surface shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="text-gray-500 hover:text-gray-300 transition-colors p-1"
            title="Toggle leads panel"
          >
            ☰
          </button>
          <span className="text-lg font-bold text-white">🪐 Discovery</span>
          <span className="text-sm text-gray-500">Pipeline Agent</span>
        </div>
        <div className="flex items-center gap-2">
          {leads.length > 0 && (
            <span className="text-xs text-gray-500">
              {leads.filter((l) => l.tier === "hot").length} hot · {leads.filter((l) => l.tier === "warm").length} warm
            </span>
          )}
          <button
            onClick={handleLoadResults}
            disabled={isLoading}
            className="px-3 py-1.5 text-xs rounded-lg border border-discovery-border text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors disabled:opacity-50"
          >
            Load Last Results
          </button>
          <span
            className="px-3 py-1.5 text-xs rounded-lg border border-discovery-border text-gray-400 font-mono"
            title={tokenUsage ? `${tokenUsage.calls} API calls · ${tokenUsage.input_tokens.toLocaleString()} in / ${tokenUsage.output_tokens.toLocaleString()} out` : "No token data yet"}
          >
            ⚡ {tokenUsage ? tokenUsage.total_tokens.toLocaleString() : "0"} tok · ${tokenUsage ? tokenUsage.estimated_cost_usd.toFixed(4) : "0.0000"}
          </span>
          <div className={`flex flex-col ${IS_DEMO ? "hidden" : ""}`}>
            <div className="flex items-center gap-2">
              <label htmlFor="test-email" className="text-xs text-gray-500 whitespace-nowrap">
                Send test emails to:
              </label>
              <input
                id="test-email"
                type="email"
                value={testEmail}
                onChange={(e) => {
                  setTestEmail(e.target.value);
                  if (testEmailError) setTestEmailError(null);
                }}
                onBlur={(e) => storeTestEmail(e.target.value.trim())}
                placeholder="you@example.com"
                title="Every outreach email is redirected here. Replace it with your own address."
                className={`w-56 px-2 py-1.5 text-xs rounded-lg bg-discovery-bg border text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500 transition-colors ${
                  testEmailError ? "border-red-500/70" : "border-discovery-border"
                }`}
              />
            </div>
            <span className={`text-[10px] mt-0.5 ${testEmailError ? "text-red-400" : "text-gray-600"}`}>
              {testEmailError ?? "All outreach is redirected here — swap in your own address."}
            </span>
          </div>
          <button
            onClick={handleRunPipeline}
            disabled={pipelineStatus === "running" || runsDisabled}
            title={
              runsDisabled
                ? "Live pipeline runs are disabled on this deployment — they spend Claude credits and can send email. The results on screen came from a real run."
                : "Run the full pipeline"
            }
            className="px-3 py-1.5 text-xs rounded-lg bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {pipelineStatus === "running"
              ? "Running..."
              : runsDisabled
              ? "Run Pipeline (disabled)"
              : "Run Pipeline"}
          </button>
        </div>
      </header>

      {loadError && !waking && (
        <div className="shrink-0 px-5 py-2 bg-red-500/10 border-b border-red-500/30 text-xs text-red-200/90">
          <span className="font-semibold">API unreachable</span>
          <span className="text-red-200/60"> · </span>
          <span>{loadError}</span>
        </div>
      )}

      {waking && (
        <div className="shrink-0 px-5 py-2 bg-blue-500/10 border-b border-blue-500/25 text-xs text-blue-200/90 flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
          <span>
            Waking the API — free-tier instances sleep when idle and take up to a minute to
            start. Everything loads automatically once it is up.
          </span>
        </div>
      )}

      {!IS_DEMO && health && !health.pipeline_runs_allowed && (
        <div className="shrink-0 px-5 py-2 bg-amber-500/10 border-b border-amber-500/25 text-xs text-amber-200/90">
          <span className="font-semibold">Live API</span>
          <span className="text-amber-200/60"> · </span>
          <span>
            Serving {health.leads} protocols from a real run. Live pipeline runs are disabled
            here — they cost Claude credits — so Run Pipeline is off. The chat agent below is
            the real thing: live Claude, real tool calls.
          </span>
        </div>
      )}

      {IS_DEMO && (
        <div className="shrink-0 px-5 py-2 bg-amber-500/10 border-b border-amber-500/25 text-xs text-amber-200/90 flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-semibold">Demo</span>
          <span className="text-amber-200/60">·</span>
          <span>
            Showing a saved run of the real pipeline — 58 protocols scored, contacts found, emails
            written by Claude. Run Pipeline replays that run; email sending is off.
          </span>
          <a
            href="https://github.com/PrashanthTalwar05/AI-Sales-Agent"
            target="_blank"
            rel="noreferrer"
            className="underline underline-offset-2 hover:text-amber-100"
          >
            Run it live →
          </a>
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar — Leads */}
        {sidebarOpen && (
          <aside className="w-64 shrink-0 border-r border-discovery-border bg-discovery-surface flex flex-col">
            <div className="px-4 py-3 border-b border-discovery-border">
              <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                Leads {leads.length > 0 ? `(${leads.length})` : ""}
              </span>
            </div>
            <LeadsPanel
              leads={leads}
              onSelect={(lead) => setSelectedProtocol(lead.protocol)}
              selectedProtocol={selectedProtocol}
            />
          </aside>
        )}

        {/* Chat */}
        <main className="flex-1 flex flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto px-6 py-5">
            {messages.map((msg) => (
              <ChatMessage key={msg.id} msg={msg} />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* Suggestions */}
          {messages.length === 1 && (
            <div className="px-6 pb-3 flex flex-wrap gap-2">
              {[
                "Show me the warm leads",
                "What are the top 3 targets?",
                "Show me Pendle's outreach message",
                "What exploits happened this week?",
              ].map((s) => (
                <button
                  key={s}
                  onClick={() => sendMessage(s)}
                  className="px-3 py-1.5 text-xs rounded-full border border-discovery-border text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors"
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          {/* Input */}
          <div className="px-6 pb-5 shrink-0">
            <div className="flex items-end gap-3 bg-discovery-surface border border-discovery-border rounded-2xl px-4 py-3">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask anything about your leads, outreach, or market events..."
                rows={1}
                className="flex-1 bg-transparent resize-none outline-none text-sm text-gray-200 placeholder-gray-600 max-h-32"
                style={{ lineHeight: "1.5" }}
              />
              <button
                onClick={() => sendMessage()}
                disabled={!input.trim() || isLoading}
                className="shrink-0 w-8 h-8 flex items-center justify-center rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-white"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="22" y1="2" x2="11" y2="13" />
                  <polygon points="22 2 15 22 11 13 2 9 22 2" />
                </svg>
              </button>
            </div>
            <p className="text-center text-xs text-gray-700 mt-2">Enter to send · Shift+Enter for newline</p>
          </div>
        </main>
      </div>

      {/* Outreach Drawer */}
      {selectedProtocol && (
        <DraftDrawer
          protocol={selectedProtocol}
          testEmail={testEmail}
          onClose={() => setSelectedProtocol(null)}
          onAskAgent={(msg) => {
            setSelectedProtocol(null);
            sendMessage(msg);
          }}
        />
      )}

      {/* Pipeline Modal */}
      {showPipelineModal && (
        <PipelineModal
          status={pipelineStatus}
          logs={pipelineLogs}
          testEmail={testEmail.trim()}
          onClose={() => {
            setShowPipelineModal(false);
            setPipelineStatus("idle");
          }}
        />
      )}
    </div>
  );
}
