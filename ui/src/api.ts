// Thin client for the daemon's REST API. The UI acts as the human user ("ui" agent).

const HEADERS = { "Content-Type": "application/json", "X-Rufur-Agent": "ui" };

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
  // A daemon older than this UI answers unknown API paths with the app's index.html.
  if (!r.headers.get("content-type")?.includes("application/json")) {
    throw new Error(`${path} isn't available on this daemon. It is probably older than the UI: restart it with \`rufur stop && rufur start\`.`);
  }
  return r.json();
}

export type Status = "active" | "superseded" | "disputed" | "quarantined" | "archived" | "rejected";

export interface Memory {
  id: string;
  type: string;
  subject: string;
  predicate: string;
  value: string;
  text: string;
  scope: string | null;
  status: Status;
  trust: string;
  confidence: number;
  importance: number;
  sensitivity: string;
  author_agent: string;
  source_type: string;
  valid_from: number;
  valid_to: number | null;
  superseded_by: string | null;
  created_at: number;
  tier: "stm" | "ltm";
  store: string;
  expires_at: number | null;
  revalidate_at: number | null;
  volatility: string;
  corroboration: number;
  flags: string[];
  pool: string;
  access_count: number;
  retention_score: number;
  sources: { event_id: string; quote: string; role: string; agent: string; source_type: string; created_at: number }[];
  history: { id: string; text: string; status: Status; valid_from: number; valid_to: number | null; source_type: string }[];
  lineage: { child_kind: string; child_id: string }[];
  related: { id: string; text: string; status: Status; trust: string; source_type: string }[];
  edges: { src: string; relation: string; dst: string }[];
}

export interface ForgetResult {
  forgotten: { id: string; type: string }[];
  redacted_events: number;
  report: {
    completeness: number;
    probes_passed: number;
    probes_total: number;
    stores: Record<string, { passed: boolean; detail: unknown }>;
    probe_queries: string[];
    note: string;
  } | null;
}

export interface Task {
  id: string;
  title: string | null;
  status: string;
  from_agent: string | null;
  to_agent: string | null;
  updated_at: number;
  packet: {
    goal: string;
    steps_done: string[];
    steps_pending: string[];
    decisions: { decision: string; reason: string }[];
    open_questions: string[];
    memory_ids: string[];
  };
}

export interface Decision {
  conflict_id: string;
  kind: string;
  question: string;
  needs_user: boolean;
  options: { memory_id: string; text: string; source: string; trust: string; agent: string; recorded_at: number; quote: string }[];
  recommendation: { memory_id: string; reason: string };
  tried: { step: string; outcome: string }[];
  default_at: number;
}

export interface Pool {
  id: string;
  name: string;
  memories: number;
  acl: { read: string[]; write: string[] };
}

export type Scope = "session" | "scratch" | "unsessioned";

export interface PoolSummary {
  id: string;
  purpose: string | null;
  owner_agent: string;
  session_id: string | null;
  created_at: number;
  expires_at: number | null;
  closed_at: number | null;
  status: "open" | "closed" | "expired";
  seeded_stm: number;
  seeded_ltm: number;
  own: number;
  own_written: number;
  merged: number | null;
  discarded: number | null;
  close_reason: string | null;
}

export interface SessionSummary {
  id: string;
  agent: string;
  started_at: number;
  ended_at: number | null;
  task_id: string | null;
  events: number;
  redacted_events: number;
  event_tokens: number;
  stm: Partial<Record<Status, number>>;
  ltm: Partial<Record<Status, number>>;
  forgotten: number;
  pools: PoolSummary[];
}

export interface Ttl {
  base_h: number;
  extend_h: number;
  max_h: number;
  grace_h: number;
  scratch_min: number;
}

type TierCounts = { stm: Partial<Record<Status, number>>; ltm: Partial<Record<Status, number>> };

export interface Sessions {
  sessions: SessionSummary[];
  unattached_pools: PoolSummary[];
  totals: {
    memories: TierCounts;
    unsessioned: TierCounts;
    scratch: TierCounts;
    pools: Record<string, { open: number; closed: number }>;
  };
  ttl: Ttl;
  now: number;
}

export interface PoolMemory {
  id: string;
  text: string;
  type: string;
  tier: "stm" | "ltm";
  status: Status;
  store: string;
  trust: string;
  created_at: number;
  expires_at: number | null;
  access_count: number;
  last_accessed: number | null;
  author_agent: string;
  origin: "session" | "seeded" | "own";
}

export interface TimelineEvent {
  t: number;
  kind: string;
  agent: string | null;
  memory_id?: string;
  text?: string | null;
  detail: Record<string, any>;
}

export interface EvolutionPoint {
  t: number;
  stm: number;
  ltm: number;
  superseded: number;
  forgotten: number;
  expired: number;
}

export interface Tokens {
  bucket: "hour" | "day";
  series: { t: number; recall: number; remember: number; extract: number; forget: number; llm: number; served: number }[];
  cumulative: { t: number; full_history: number; rufur: number }[];
  by_agent: { agent: string; calls: number; tokens_in: number; tokens_out: number; llm_tokens: number }[];
  totals: { tokens_in: number; tokens_out: number; llm_tokens: number };
}

export interface Probe {
  store: string;
  kind: string;
  informative: boolean;
  before_hits: number | null;
  after_hits: number;
}

export interface Receipt {
  receipt_id: string;
  agent: string;
  issued_at: number;
  memory_ids: string[];
  target_hashes: string[];
  counts: Record<string, number>;
  probes: Probe[];
  completeness: number | null;
  erasure: string;
  prev_hash: string;
  hash: string;
  sig: string;
}

export interface Forgetting {
  receipts: Receipt[];
  chain: { receipts: number; valid: boolean; problems: { receipt: string; problem: string }[]; resurfaced_memories: string[] };
  redacted_events: { id: string; session_id: string | null; agent: string; role: string; content: string; created_at: number }[];
  placeholder: string;
  counts: Partial<Record<"archived" | "forgotten" | "purged", number>>;
  tombstones: number;
}

export interface RawColumn {
  name: string;
  type: string;
  notnull: boolean;
  default: string | null;
  pk: number;
}

export interface RawTable {
  name: string;
  type: "table" | "view" | "virtual";
  sql: string | null;
  rows: number | null;
  error: string | null;
  group: "relational" | "fts" | "vector" | "graph" | "system";
  shadow_of: string | null;
  columns: RawColumn[];
  has_rowid: boolean;
  indexes: { name: string; sql: string | null }[];
  foreign_keys: { table: string; from: string; to: string }[];
}

export interface RawLink {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  when: { column: string; equals: string } | null;
}

export interface RawCatalog {
  database: { path: string; bytes: number; wal_bytes: number | null; sqlite: string; sqlite_vec: string; embed_dim: number };
  tables: RawTable[];
  links: RawLink[];
  files_dir: string;
  files: { path: string; bytes: number; modified: number }[];
}

export type RawBlob = { $blob: string; bytes: number; truncated: boolean };
export type RawValue = string | number | null | RawBlob;

export interface RawRows {
  table: string;
  columns: string[];
  rows: RawValue[][];
  total: number;
  limit: number;
  offset: number;
}

export const api = {
  memory: (id: string) => req<Memory>("GET", `/v1/memories/${id}`),
  memories: (q = "") => req<Memory[]>("GET", `/v1/memories${q}`),
  confirm: (id: string, action: "confirm" | "reject") => req<unknown>("POST", `/v1/memories/${id}/confirm`, { action }),
  forget: (id: string) => req<ForgetResult>("POST", "/v1/forget", { memory_id: id }),
  quarantine: () => req<(Memory & { sources: { quote: string }[] })[]>("GET", "/v1/quarantine"),
  tasks: () => req<Task[]>("GET", "/v1/tasks"),
  pools: () => req<Pool[]>("GET", "/v1/pools"),
  metrics: () => req<any>("GET", "/v1/metrics/summary"),
  audit: () => req<{ id: number; agent: string; action: string; target: string; created_at: number }[]>("GET", "/v1/audit?limit=60"),
  health: () => req<any>("GET", "/v1/health"),
  consolidate: () => req<any>("POST", "/v1/consolidate", { journal: false }),
  runs: () => req<{ id: string; trigger: string; started_at: number; finished_at: number | null; stats: any }[]>("GET", "/v1/consolidation/runs"),
  stmStats: () => req<any>("GET", "/v1/stm/stats"),
  scratchPools: () => req<any[]>("GET", "/v1/scratch-pools"),
  decisions: () => req<Decision[]>("GET", "/v1/decisions"),
  sessions: () => req<Sessions>("GET", "/v1/dashboard/sessions"),
  poolMemories: (kind: Scope, id: string) => req<{ items: PoolMemory[]; now: number }>("GET", `/v1/dashboard/${kind}/${id}/memories`),
  timeline: (kind: Scope, id: string) => req<{ events: TimelineEvent[]; series: EvolutionPoint[] }>("GET", `/v1/dashboard/${kind}/${id}/timeline`),
  tokens: (bucket: "hour" | "day") => req<Tokens>("GET", `/v1/dashboard/tokens?bucket=${bucket}`),
  forgetting: () => req<Forgetting>("GET", "/v1/dashboard/forgetting"),
  verifyForgetting: () => req<Forgetting["chain"]>("POST", "/v1/forget/verify"),
  rawCatalog: () => req<RawCatalog>("GET", "/v1/raw/catalog"),
  rawRows: (table: string, q: { limit: number; offset: number; column?: string; value?: string; desc?: boolean }) => {
    const p = new URLSearchParams({ limit: String(q.limit), offset: String(q.offset), desc: String(!!q.desc) });
    if (q.column !== undefined && q.value !== undefined) {
      p.set("column", q.column);
      p.set("value", q.value);
    }
    return req<RawRows>("GET", `/v1/raw/tables/${encodeURIComponent(table)}?${p}`);
  },
  decide: (id: string, winner: string | null, action: "keep" | "neither" = "keep") =>
    req<unknown>("POST", `/v1/decisions/${id}`, { action, winner, decided_by: "user" }),
};

export function until(ts: number): string {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.ceil(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}

export const STORE_LABEL: Record<string, string> = {
  profile: "user profile",
  fact: "facts",
  table: "a table",
  graph: "the relation graph",
  procedure: "procedures",
  journal: "the daily journal",
  discard: "nowhere (short-term only)",
};

export function hours(h: number): string {
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  if (h < 48) return `${Math.round(h)} h`;
  return `${Math.round(h / 24)} days`;
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function since(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const SOURCE_LABEL: Record<string, string> = {
  user: "said by the user",
  document: "from a document",
  agent: "inferred by an agent",
  tool: "from tool output",
  web: "from the web",
};
