"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { api, Lead, Draft, ToolCall, TokenUsage } from "@/lib/api";

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

function DraftDrawer({
  protocol,
  onClose,
  onAskAgent,
}: {
  protocol: string;
  onClose: () => void;
  onAskAgent: (msg: string) => void;
}) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setSelectedIdx(0);
    api.getAllDrafts(protocol)
      .then((res) => setDrafts(res.drafts))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [protocol]);

  const draft = drafts[selectedIdx] ?? null;

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

      </div>
    </div>
  );
}

// ── Pipeline Log Modal ────────────────────────────────────────────────────────

function PipelineModal({
  status,
  logs,
  onClose,
}: {
  status: PipelineStatus;
  logs: string[];
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

  const refreshLeads = useCallback(async () => {
    try {
      const data = await api.getLeads();
      setLeads(data.leads);
    } catch {
      // no leads yet
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

  const handleRunPipeline = useCallback(() => {
    setPipelineLogs([]);
    setPipelineStatus("running");
    setShowPipelineModal(true);

    const es = new EventSource(api.pipelineRunUrl());

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
      setPipelineStatus("error");
      setPipelineLogs((prev) => [...prev, "Connection error — pipeline may still be running."]);
    };
  }, [refreshLeads, refreshTokens]);

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
          <button
            onClick={handleRunPipeline}
            disabled={pipelineStatus === "running"}
            className="px-3 py-1.5 text-xs rounded-lg bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {pipelineStatus === "running" ? "Running..." : "Run Pipeline"}
          </button>
        </div>
      </header>

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
          onClose={() => {
            setShowPipelineModal(false);
            setPipelineStatus("idle");
          }}
        />
      )}
    </div>
  );
}
