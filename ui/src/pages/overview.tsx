import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Dot, TtlBar } from "@/components/memory-ui";
import { TokenChart } from "@/pages/tokens";
import { api, CompositionKey, Forgetting, Memory, Sessions, since, STORE_LABEL, Tokens } from "@/api";

// Five groups, so the stacks still add up to everything the session ever created but stay readable.
// The memory sheet and the Review / Forgetting pages keep the fine-grained status.
const COMPOSITION_CONFIG = {
  ltm: { label: "Long-term", color: "var(--chart-1)" },
  stm: { label: "Short-term", color: "var(--chart-2)" },
  review: { label: "Needs review", color: "oklch(0.62 0.22 350)" },
  history: { label: "Replaced", color: "var(--chart-3)" },
  gone: { label: "Gone", color: "var(--chart-4)" },
} satisfies ChartConfig;
type CompositionGroup = keyof typeof COMPOSITION_CONFIG;
const GROUPS = Object.keys(COMPOSITION_CONFIG) as CompositionGroup[];
const GROUP_OF: Record<CompositionKey, CompositionGroup> = {
  ltm: "ltm",
  stm: "stm",
  held: "review", // quarantined: waiting on the reviewer or the user
  disputed: "review", // a conflict nobody has settled yet
  superseded: "history", // an older version, kept so "what was it before" works
  fading: "gone", // unused long-term memory on its way out (a recall brings it back)
  expired: "gone",
  rejected: "gone",
  forgotten: "gone",
};

function grouped(c: Record<CompositionKey, number>): Record<CompositionGroup, number> {
  const out = Object.fromEntries(GROUPS.map((g) => [g, 0])) as Record<CompositionGroup, number>;
  for (const [k, n] of Object.entries(c) as [CompositionKey, number][]) out[GROUP_OF[k]] += n;
  return out;
}

// What backs each long-term store, so the dashboard says how knowledge is kept, not just how much.
const STORE_BACKING: Record<string, string> = {
  profile: "memories row, keyed user|attribute; loaded first into every session",
  fact: "memories row + vector (vec_items) + full-text (memories_fts)",
  table: "typed_rows, shape fixed by table_schemas",
  graph: "edges (src → relation → dst) over entities",
  procedure: "memories row, never expires",
  journal: "one dated summary per day",
};
export function OverviewPage({
  data,
  version,
  onChanged,
  onSelectMemory,
  onOpenSession,
}: {
  data: Sessions | null;
  version: number;
  onChanged: () => void;
  onSelectMemory: (id: string) => void;
  onOpenSession: (id: string) => void;
}) {
  const [tokens, setTokens] = useState<Tokens | null>(null);
  const [forgetting, setForgetting] = useState<Forgetting | null>(null);
  const [stm, setStm] = useState<Memory[]>([]);
  const [stmStats, setStmStats] = useState<any>(null);
  const [runs, setRuns] = useState<Awaited<ReturnType<typeof api.runs>>>([]);
  const [busy, setBusy] = useState(false);
  const [filed, setFiled] = useState<string | null>(null);

  useEffect(() => {
    api.tokens("session").then(setTokens);
    api.forgetting().then(setForgetting);
    api.memories("?tier=stm&status=active&limit=200").then(setStm);
    api.stmStats().then(setStmStats);
    api.runs().then(setRuns);
  }, [version]);

  async function consolidateNow() {
    setBusy(true);
    setFiled(null);
    try {
      const r = await api.consolidate();
      setFiled(r.items ? `Filed ${r.items} (${r.promoted} promoted, ${r.merged} merged, ${r.discarded} discarded).` : "Nothing waiting to be filed.");
      onChanged();
    } catch (e) {
      setFiled(`Consolidation failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <Skeleton className="h-96 w-full" />;
  // Totals cover every memory, including ones with no session (explicit remembers, merged subagent results).
  const t = data.totals;
  const count = (o: Record<string, number | undefined>) => Object.values(o).reduce<number>((a, b) => a + (b ?? 0), 0);
  const scratch = t.pools.scratch ?? { open: 0, closed: 0 };
  const stmLive = (t.memories.stm.active ?? 0) + (t.memories.stm.disputed ?? 0) + (t.memories.stm.quarantined ?? 0);
  const ltmLive = (t.memories.ltm.active ?? 0) + (t.memories.ltm.disputed ?? 0);
  const last = tokens?.cumulative[tokens.cumulative.length - 1];
  const saved = last && last.full_history > 0 ? Math.round(100 * (1 - last.rufur / last.full_history)) : null;
  const scored = forgetting?.receipts.filter((r) => r.completeness !== null) ?? [];
  const completeness = scored.length ? Math.round((100 * scored.reduce((a, r) => a + (r.completeness ?? 0), 0)) / scored.length) : null;
  const expiring = [...stm]
    .filter((m) => m.expires_at)
    .sort((a, b) => a.expires_at! - b.expires_at!)
    .slice(0, 6);
  // Per-session composition: where each session's memories are now.
  const composition = data.sessions
    .slice(0, 12)
    .reverse()
    .map((s) => ({
      id: s.id,
      tick: `${s.agent} ${new Date(s.started_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`,
      label: `${s.agent} · ${since(s.started_at)}`,
      ...grouped(s.composition),
    }));
  const empty = composition.filter((c) => GROUPS.every((k) => !c[k])).length;
  const st = t.storage;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Kpi label="Sessions" value={data.sessions.length} hint={`${data.sessions.filter((s) => !s.ended_at).length} live`} />
        <Kpi label="Sub-pools" value={scratch.open + scratch.closed} hint={`${scratch.open} open, ${scratch.closed} closed`} />
        <Kpi
          label="Short-term"
          value={stmLive}
          hint={`${t.memories.stm.active ?? 0} active, ${t.memories.stm.quarantined ?? 0} held · ${count(t.memories.stm)} rows in all`}
          dot="bg-stm"
        />
        <Kpi
          label="Long-term"
          value={ltmLive}
          hint={`${t.memories.ltm.active ?? 0} active, ${t.memories.ltm.disputed ?? 0} disputed · ${count(t.memories.ltm)} rows in all`}
          dot="bg-ltm"
        />
        <Kpi label="Context saved" value={saved === null ? "–" : `${saved}%`} hint="vs sending full history" />
        <Kpi
          label="Forget completeness"
          value={completeness === null ? "–" : `${completeness}%`}
          hint={`${forgetting?.receipts.length ?? 0} ${forgetting?.receipts.length === 1 ? "receipt" : "receipts"}`}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader>
            <CardTitle>Token usage</CardTitle>
            <CardDescription>Per operation, one bar per working session (a new one after 20 quiet minutes), last 7 days.</CardDescription>
          </CardHeader>
          <CardContent>
            {tokens?.series.length ? (
              <TokenChart data={tokens} height="h-64" />
            ) : (
              <p className="text-muted-foreground text-sm">No operations yet.</p>
            )}
          </CardContent>
        </Card>

        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Short-term expiring soonest</CardTitle>
            <CardDescription>
              {stmStats
                ? `${stmStats.unconsolidated} waiting to be filed. ${
                    stmStats.suggested_h ? `Reuse data suggests a ${stmStats.suggested_h} h lifetime.` : `Lifetime ${stmStats.current_h} h.`
                  }`
                : "Lifetimes of short-term memories."}
              {filed && <span className="block">{filed}</span>}
            </CardDescription>
            <CardAction>
              <Button size="sm" variant="outline" onClick={consolidateNow} disabled={busy}>
                {busy ? "Filing…" : "Consolidate now"}
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {expiring.length === 0 && <p className="text-muted-foreground text-sm">Nothing in short-term memory.</p>}
            {expiring.map((m) => (
              <button key={m.id} onClick={() => onSelectMemory(m.id)} className="hover:bg-muted flex flex-col gap-1.5 rounded-md p-2 text-left">
                <span className="line-clamp-1 text-sm">{m.text}</span>
                <TtlBar created={m.created_at} expires={m.expires_at} now={data.now} ttl={data.ttl} />
              </button>
            ))}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Where each session’s memories are now</CardTitle>
          <CardDescription>
            The latest {composition.length} sessions. Each bar counts every memory the session ever created, once, by where it is now (from the memories table
            and the ops ledger for deleted ones).{empty > 0 && ` ${empty} wrote no memories.`} Select a bar to open that session.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {composition.length ? (
            <ChartContainer config={COMPOSITION_CONFIG} className="aspect-auto h-64 w-full cursor-pointer">
              <BarChart
                data={composition}
                margin={{ left: 0, right: 12, top: 8 }}
                // recharts 3 reports the hovered category as activeIndex (activePayload is gone), so the whole column is clickable.
                onClick={(e) => {
                  const row = e?.activeIndex != null ? composition[Number(e.activeIndex)] : undefined;
                  if (row) onOpenSession(row.id);
                }}
              >
                <CartesianGrid vertical={false} />
                <XAxis dataKey="tick" tickLine={false} axisLine={false} interval={0} tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={32} />
                <ChartTooltip
                  cursor={{ fill: "var(--muted)" }}
                  content={(props) => (
                    <ChartTooltipContent
                      active={props.active}
                      label={props.label}
                      payload={props.payload?.filter((p) => Number(p.value) > 0)}
                      labelFormatter={(_v, p) => p?.[0]?.payload?.label}
                    />
                  )}
                />
                <ChartLegend content={<ChartLegendContent />} />
                {GROUPS.map((k) => (
                  <Bar
                    key={k}
                    dataKey={k}
                    stackId="a"
                    fill={`var(--color-${k})`}
                    stroke="var(--card)"
                    strokeWidth={1}
                    onClick={(_d, i) => composition[i] && onOpenSession(composition[i].id)}
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            </ChartContainer>
          ) : (
            <p className="text-muted-foreground text-sm">No sessions yet.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How long-term memory is stored</CardTitle>
          <CardDescription>
            Live long-term memories by store, and what backs each one. Everything is one SQLite file; the ops ledger ({st.ops} entries) and raw events (
            {st.events}) are what every store is rebuilt from.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Store</TableHead>
                <TableHead className="text-right">Memories</TableHead>
                <TableHead>Kept in</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Object.keys(STORE_BACKING).map((k) => (
                <TableRow key={k}>
                  <TableCell>{STORE_LABEL[k] ?? k}</TableCell>
                  <TableCell className="text-right tabular-nums">{st.stores[k] ?? 0}</TableCell>
                  <TableCell className="text-muted-foreground">{STORE_BACKING[k]}</TableCell>
                </TableRow>
              ))}
              {Object.entries(st.stores)
                .filter(([k]) => !(k in STORE_BACKING))
                .map(([k, v]) => (
                  <TableRow key={k}>
                    <TableCell>{STORE_LABEL[k] ?? k}</TableCell>
                    <TableCell className="text-right tabular-nums">{v}</TableCell>
                    <TableCell className="text-muted-foreground">not filed yet</TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
          <p className="text-muted-foreground mt-3 text-xs">
            Indexes: {st.vectors} vectors · {st.edges} graph edges between {st.entities} entities · {st.typed_rows} rows in {st.tables} typed{" "}
            {st.tables === 1 ? "table" : "tables"}.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Consolidation runs</CardTitle>
          <CardDescription>Each run files short-term memories into long-term stores, keeping their ids and sources.</CardDescription>
        </CardHeader>
        <CardContent>
          {runs.length === 0 ? (
            <p className="text-muted-foreground text-sm">No runs yet. Consolidation runs nightly, when an agent asks, or when short-term memory grows large.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Trigger</TableHead>
                  <TableHead className="text-right">Promoted</TableHead>
                  <TableHead className="text-right">Kept short-term</TableHead>
                  <TableHead className="text-right">Merged</TableHead>
                  <TableHead>Filed into</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.slice(0, 10).map((r) => (
                  <TableRow key={r.id}>
                    <TableCell>{since(r.started_at)}</TableCell>
                    <TableCell>{r.trigger}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.stats.promoted ?? 0}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.stats.discarded ?? 0}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.stats.merged ?? 0}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {Object.entries((r.stats.by_store ?? {}) as Record<string, number>)
                        .map(([k, v]) => `${v} ${STORE_LABEL[k] ?? k}`)
                        .join(", ") || "–"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Kpi({ label, value, hint, dot }: { label: string; value: React.ReactNode; hint?: string; dot?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription className="flex items-center gap-2">
          {dot && <Dot className={dot} />}
          {label}
        </CardDescription>
        <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      {hint && (
        <CardFooter>
          <p className="text-muted-foreground text-xs">{hint}</p>
        </CardFooter>
      )}
    </Card>
  );
}
