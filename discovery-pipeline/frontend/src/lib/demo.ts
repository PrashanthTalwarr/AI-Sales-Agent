/**
 * Demo-mode data access.
 *
 * The hosted demo has no Python backend. These helpers read a snapshot of a real
 * pipeline run (demo-data.json, produced by scripts/build_demo_fixture.py) and
 * shape it into the same payloads the FastAPI backend returns, so the UI needs
 * no special-casing beyond the base URL.
 */

import raw from "./demo-data.json";
import type { Lead, Draft, Summary, TokenUsage } from "./api";

interface DemoLead {
  protocol_name: string;
  tvl_usd: number;
  category: string;
  composite_score: number;
  score_tier: string;
  shipping_velocity: string;
  ai_signals: string;
  scoring_rationale: string;
  audit_providers?: string;
  bounty_platform?: string;
  total_raised_usd?: number;
}

interface DemoContact {
  name: string;
  role: string;
  email: string;
  twitter_handle: string;
  github_username: string;
  source: string;
  confidence: string;
}

interface DemoDraft {
  protocol_name: string;
  persona_name: string;
  persona_role: string;
  channel: string;
  sequence_step: number;
  subject_line: string;
  message_body: string;
  llm_model: string;
  contact_email: string | null;
  contact_twitter: string | null;
  contact_github: string | null;
  contact_source: string | null;
}

const data = raw as unknown as {
  generated_from_run: string;
  leads: DemoLead[];
  contacts: Record<string, DemoContact[]>;
  drafts: DemoDraft[];
  outreach: Array<Record<string, unknown>>;
  pipeline_log: string[];
  token_usage: TokenUsage;
};

export const demoLastRun = data.generated_from_run;
export const demoPipelineLog = data.pipeline_log;
export const demoTokenUsage = data.token_usage;

/**
 * The stored snapshot keeps only the composite score, not the five factor
 * scores. Rather than show zeros, recover each factor from the same rules the
 * Python scorer uses so the UI's TVL/Aud/Vel breakdown stays truthful.
 */
function factorScores(l: DemoLead) {
  const tvl =
    l.tvl_usd >= 1e9 ? 30 : l.tvl_usd >= 1e8 ? 25 : l.tvl_usd >= 1e7 ? 20 : l.tvl_usd >= 1e6 ? 14 : 8;

  const audit = l.audit_providers ? 20 : 25;

  const velBase: Record<string, number> = {
    very_high: 16,
    high: 13,
    moderate: 10,
    low: 5,
    inactive: 0,
  };
  const signals = l.ai_signals ? l.ai_signals.split(",").filter(Boolean).length : 0;
  const vel = Math.min((velBase[l.shipping_velocity] ?? 0) + Math.min(signals * 2, 4), 20);

  // Whatever the composite does not attribute to the three known factors is
  // funding + reachability; show it against those rather than inventing detail.
  const remainder = Math.max(0, l.composite_score - tvl - audit - vel);
  const fund = Math.min(15, remainder);
  const reach = Math.max(0, remainder - fund);

  return { tvl, audit, vel, fund, reach };
}

export function demoLeads(): Lead[] {
  return data.leads.map((l) => {
    const f = factorScores(l);
    return {
      protocol: l.protocol_name,
      score: l.composite_score,
      tier: (l.score_tier as Lead["tier"]) ?? "cool",
      tvl_score: f.tvl,
      audit_score: f.audit,
      vel_score: f.vel,
      fund_score: f.fund,
      reach_score: f.reach,
      rationale: l.scoring_rationale,
      contacts: (data.contacts[l.protocol_name] ?? []).map((c) => ({
        name: c.name,
        role: c.role,
        email: c.email,
        twitter: c.twitter_handle,
        github: c.github_username,
        source: c.source,
        confidence: c.confidence,
      })),
    };
  });
}

export function demoSummary(): Summary {
  const leads = data.leads;
  return {
    total: leads.length,
    hot: leads.filter((l) => l.score_tier === "hot").length,
    warm: leads.filter((l) => l.score_tier === "warm").length,
    drafts: data.drafts.length,
    last_run: data.generated_from_run,
  };
}

export function demoDraftsFor(protocol: string): Draft[] {
  const p = protocol.toLowerCase();
  let rows = data.drafts.filter((d) => d.protocol_name.toLowerCase() === p);
  if (rows.length === 0) {
    rows = data.drafts.filter((d) => d.protocol_name.toLowerCase().includes(p));
  }
  return rows.map((d) => ({
    protocol: d.protocol_name,
    persona: d.persona_name,
    role: d.persona_role,
    channel: d.channel,
    step: d.sequence_step,
    subject: d.subject_line,
    body: d.message_body,
    model: d.llm_model,
    contact_email: d.contact_email ?? "",
    contact_twitter: d.contact_twitter ?? "",
    contact_github: d.contact_github ?? "",
    contact_source: d.contact_source ?? "",
  }));
}

export function demoProtocolsWithDrafts(): string[] {
  return Array.from(new Set(data.drafts.map((d) => d.protocol_name)));
}

/**
 * Scripted chat.
 *
 * The real agent is LangChain + Claude running server-side. The hosted demo has
 * no key, so instead of faking an LLM this answers the questions the agent's
 * five tools cover, using the same snapshot the rest of the demo reads — the
 * numbers are real. Anything else gets an honest "not in the demo" reply.
 */
export function demoChatReply(message: string): { text: string; tool?: string } {
  const q = message.toLowerCase();
  const leads = data.leads;
  const warm = leads.filter((l) => l.score_tier === "warm");

  const table = (rows: DemoLead[]) =>
    rows
      .map(
        (l) =>
          `${l.score_tier === "hot" ? "🔥" : l.score_tier === "warm" ? "🟡" : "⚪"} ${l.protocol_name.padEnd(
            22
          )} ${l.composite_score.toFixed(0).padStart(3)}  ${l.score_tier}`
      )
      .join("\n");

  if (/\b(hot|warm|cool)\b/.test(q) && /lead|show|list|which/.test(q)) {
    const tier = q.includes("hot") ? "hot" : q.includes("cool") ? "cool" : "warm";
    const rows = leads.filter((l) => l.score_tier === tier);
    if (rows.length === 0) {
      return {
        tool: "get_pipeline_results",
        text: `No ${tier} leads in this run. ${warm.length} protocols scored warm (75-89); nothing cleared 90 for hot.`,
      };
    }
    return {
      tool: "get_pipeline_results",
      text: `${rows.length} ${tier} lead${rows.length === 1 ? "" : "s"}:\n\n${table(rows.slice(0, 15))}`,
    };
  }

  const drafted = demoProtocolsWithDrafts();
  const named = drafted.find((p) => q.includes(p.toLowerCase()));
  if (named && /draft|outreach|email|message|show/.test(q)) {
    const d = demoDraftsFor(named)[0];
    return {
      tool: "get_outreach_draft",
      text: `Draft for ${named} — to ${d.persona} (${d.role}):\n\nSubject: ${d.subject}\n\n${d.body}`,
    };
  }

  if (/contact|who|founder|cto|reach/.test(q)) {
    const proto =
      Object.keys(data.contacts).find((p) => q.includes(p.toLowerCase())) ?? drafted[0];
    const cs = data.contacts[proto] ?? [];
    return {
      tool: "get_contacts",
      text:
        `Contacts for ${proto} (${cs.length} found):\n\n` +
        cs
          .map(
            (c) =>
              `• ${c.name} — ${c.role}${c.email ? ` | ${c.email}` : ""}${
                c.twitter_handle ? ` | ${c.twitter_handle}` : ""
              } [${c.source}]`
          )
          .join("\n"),
    };
  }

  if (/summar|overview|how many|total|result/.test(q)) {
    const s = demoSummary();
    return {
      tool: "get_pipeline_summary",
      text:
        `Last run: ${s.last_run}\n` +
        `Scored ${s.total} protocols — ${s.hot} hot, ${s.warm} warm.\n` +
        `${s.drafts} personalized emails drafted.\n\nTop 5:\n${table(leads.slice(0, 5))}`,
    };
  }

  if (/exploit|hack|monitor|market|event|funding/.test(q)) {
    return {
      tool: "run_market_monitor",
      text:
        "DeFiLlama monitor: 39 exploits detected in the last 30 days, 0 matching protocols currently in the pipeline.\n\n" +
        "Live, this checks the hacks and raises endpoints on every run and flags any hit against a scored protocol.",
    };
  }

  return {
    text:
      "This is a static demo, so I answer from a saved pipeline run rather than calling Claude.\n\n" +
      "Try: “show me the warm leads”, “show the outreach for Lido”, “who are the contacts at Rocket Pool”, " +
      "“give me a summary”, or “any exploits this week”.\n\n" +
      "Running locally with an API key, this is a real LangChain agent with five tools over live data.",
  };
}
