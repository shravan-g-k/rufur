import { useEffect, useMemo, useState } from "react";
import { BoxIcon, ChevronRightIcon, MessagesSquareIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dot, RedactedText, TierDot } from "@/components/memory-ui";
import { PoolTimeline } from "@/components/pool-timeline";
import { TierTable } from "@/components/tier-table";
import { cn } from "@/lib/utils";
import {
  api,
  clock,
  EvolutionPoint,
  Forgetting,
  PoolMemory,
  PoolSummary,
  Scope,
  SessionSummary,
  Sessions,
  since,
  TimelineEvent,
  until,
} from "@/api";

export type Selection = { kind: Scope; id: string } | null;

const sum = (o: Record<string, number | undefined>) => Object.values(o).reduce<number>((a, b) => a + (b ?? 0), 0);

export function SessionsPage({
  data,
  selection,
  onSelectPool,
  onSelectMemory,
  version,
}: {
  data: Sessions | null;
  selection: Selection;
  onSelectPool: (s: Selection) => void;
  onSelectMemory: (id: string) => void;
  version: number;
}) {
  if (!data) return <Skeleton className="h-96 w-full" />;
  if (!data.sessions.length && !data.unattached_pools.length) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <MessagesSquareIcon />
          </EmptyMedia>
          <EmptyTitle>No sessions yet</EmptyTitle>
          <EmptyDescription>Connect Claude Code or another agent over MCP. Each conversation shows up here as a session.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }
  // Open on the newest session that holds any memory; many sessions are short and learn nothing.
  const first = data.sessions.find((s) => sum(s.stm) + sum(s.ltm) > 0) ?? data.sessions[0];
  const current = selection ?? (first ? { kind: "session" as const, id: first.id } : null);
  return (
    <div className="grid gap-4 lg:grid-cols-[20rem_minmax(0,1fr)]">
      <PoolTree data={data} selection={current} onSelect={onSelectPool} />
      {current && <PoolDetail data={data} selection={current} onSelectMemory={onSelectMemory} version={version} />}
    </div>
  );
}

function PoolTree({ data, selection, onSelect }: { data: Sessions; selection: Selection; onSelect: (s: Selection) => void }) {
  return (
    <Card className="h-fit lg:sticky lg:top-4">
      <CardHeader>
        <CardTitle>Sessions</CardTitle>
        <CardDescription>
          {data.sessions.length} sessions, {data.sessions.reduce((n, s) => n + s.pools.length, 0) + data.unattached_pools.length} sub-pools
        </CardDescription>
      </CardHeader>
      <CardContent className="flex max-h-[70vh] flex-col gap-1 overflow-y-auto">
        {data.sessions.map((s) => (
          <Collapsible key={s.id} defaultOpen={s.pools.length > 0}>
            <div className="flex items-center gap-1">
              <CollapsibleTrigger
                className={cn("text-muted-foreground rounded-sm p-1 [&[data-state=open]>svg]:rotate-90", !s.pools.length && "invisible")}
                aria-label="Show sub-pools"
              >
                <ChevronRightIcon className="size-4 transition-transform" />
              </CollapsibleTrigger>
              <TreeRow
                active={selection?.kind === "session" && selection.id === s.id}
                onClick={() => onSelect({ kind: "session", id: s.id })}
                title={s.agent}
                subtitle={`${since(s.started_at)}${s.ended_at ? "" : " · live"}`}
                stm={sum(s.stm)}
                ltm={sum(s.ltm)}
              />
            </div>
            <CollapsibleContent className="ml-6 flex flex-col gap-1 border-l pl-2">
              {s.pools.map((p) => (
                <PoolRow key={p.id} p={p} active={selection?.kind === "scratch" && selection.id === p.id} onSelect={onSelect} />
              ))}
            </CollapsibleContent>
          </Collapsible>
        ))}
        {sum(data.totals.unsessioned.stm) + sum(data.totals.unsessioned.ltm) > 0 && (
          <>
            <Separator className="my-2" />
            <div className="flex items-center gap-1">
              <span className="w-6" />
              <TreeRow
                active={selection?.kind === "unsessioned"}
                onClick={() => onSelect({ kind: "unsessioned", id: "all" })}
                title="Without a session"
                subtitle="explicit remembers, merged sub-pool results, journals"
                stm={sum(data.totals.unsessioned.stm)}
                ltm={sum(data.totals.unsessioned.ltm)}
              />
            </div>
          </>
        )}
        {data.unattached_pools.length > 0 && (
          <>
            <Separator className="my-2" />
            <p className="text-muted-foreground px-2 text-xs font-medium">Sub-pools without a session</p>
            {data.unattached_pools.map((p) => (
              <PoolRow key={p.id} p={p} active={selection?.kind === "scratch" && selection.id === p.id} onSelect={onSelect} />
            ))}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function PoolRow({ p, active, onSelect }: { p: PoolSummary; active: boolean; onSelect: (s: Selection) => void }) {
  return (
    <TreeRow
      active={active}
      onClick={() => onSelect({ kind: "scratch", id: p.id })}
      icon
      title={p.purpose || p.id}
      subtitle={p.status === "open" ? `open · expires ${p.expires_at ? until(p.expires_at) : "never"}` : p.status}
      stm={p.seeded_stm}
      ltm={p.seeded_ltm}
      own={p.own}
    />
  );
}

function TreeRow(props: { active: boolean; onClick: () => void; title: string; subtitle: string; stm: number; ltm: number; own?: number; icon?: boolean }) {
  return (
    <button
      onClick={props.onClick}
      aria-current={props.active || undefined}
      className={cn(
        "hover:bg-muted flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left",
        props.active && "bg-muted",
      )}
    >
      {props.icon && <BoxIcon className="text-muted-foreground size-4 shrink-0" />}
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">{props.title}</span>
        <span className="text-muted-foreground truncate text-xs">{props.subtitle}</span>
      </span>
      <span className="text-muted-foreground flex shrink-0 items-center gap-2 text-xs tabular-nums">
        <span className="flex items-center gap-1" title="short-term">
          <TierDot tier="stm" />
          {props.stm}
        </span>
        <span className="flex items-center gap-1" title="long-term">
          <TierDot tier="ltm" />
          {props.ltm}
        </span>
        {props.own !== undefined && props.own > 0 && <span title="own writes">+{props.own}</span>}
      </span>
    </button>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-muted-foreground text-xs">{label}</span>
      <span className="text-lg font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function PoolDetail({ data, selection, onSelectMemory, version }: { data: Sessions; selection: NonNullable<Selection>; onSelectMemory: (id: string) => void; version: number }) {
  const [mems, setMems] = useState<{ items: PoolMemory[]; now: number } | null>(null);
  const [tl, setTl] = useState<{ events: TimelineEvent[]; series: EvolutionPoint[] } | null>(null);
  const [forgetting, setForgetting] = useState<Forgetting | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    api.poolMemories(selection.kind, selection.id).then(setMems, (e) => setError(e.message));
    if (selection.kind !== "unsessioned") api.timeline(selection.kind, selection.id).then(setTl, () => setTl(null));
    else setTl(null);
    api.forgetting().then(setForgetting, () => setForgetting(null));
  }, [selection.kind, selection.id, version]);

  const session = selection.kind === "session" ? data.sessions.find((s) => s.id === selection.id) : undefined;
  const pool = selection.kind === "scratch" ? [...data.sessions.flatMap((s) => s.pools), ...data.unattached_pools].find((p) => p.id === selection.id) : undefined;

  const items = mems?.items ?? [];
  const now = mems?.now ?? data.now;
  const pick = (origin: PoolMemory["origin"], tier?: "stm" | "ltm") => items.filter((m) => m.origin === origin && (!tier || m.tier === tier));
  const redacted = useMemo(
    () => (forgetting?.redacted_events ?? []).filter((e) => session && e.session_id === session.id),
    [forgetting, session],
  );
  const removals = (tl?.events ?? []).filter((e) => e.kind === "memory.delete" || e.kind === "memory.archive");

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn’t load this pool</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {session && <SessionHeader s={session} />}
      {selection.kind === "unsessioned" && (
        <Card>
          <CardHeader>
            <CardTitle>Memories without a session</CardTitle>
            <CardDescription>
              Written outside a conversation: explicit remember calls, results merged back from sub-pools, consolidation journals.
            </CardDescription>
          </CardHeader>
        </Card>
      )}
      {pool && <PoolHeader p={pool} />}

      <Tabs defaultValue="memory">
        <TabsList>
          <TabsTrigger value="memory">Memory</TabsTrigger>
          <TabsTrigger value="timeline" disabled={selection.kind === "unsessioned"}>
            Timeline
          </TabsTrigger>
          <TabsTrigger value="deleted" disabled={selection.kind === "unsessioned"}>
            Deleted
            {removals.length + redacted.length > 0 && (
              <Badge variant="secondary" className="tabular-nums">
                {removals.length + redacted.length}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="memory" className="flex flex-col gap-4">
          {!mems ? (
            <Skeleton className="h-64 w-full" />
          ) : selection.kind !== "scratch" ? (
            <div className="grid gap-4 2xl:grid-cols-2">
              <TierTable tier="stm" title="Short-term" description="Shared by all your agents until it is filed or expires." items={pick("session", "stm")} now={now} ttl={data.ttl} onSelect={onSelectMemory} />
              <TierTable tier="ltm" title="Long-term" description="Promoted by consolidation, same id and sources." items={pick("session", "ltm")} now={now} ttl={data.ttl} onSelect={onSelectMemory} />
            </div>
          ) : (
            <>
              {pool?.status === "closed" && (
                <Alert>
                  <AlertTitle>Pool closed {pool.closed_at ? since(pool.closed_at) : ""}</AlertTitle>
                  <AlertDescription>
                    {pool.merged ?? 0} of {pool.own_written} subagent writes merged back into short-term memory; {pool.discarded ?? 0} discarded. Seeded memories were
                    only referenced, so they stay where they were.
                  </AlertDescription>
                </Alert>
              )}
              <TierTable tier="own" title="Subagent writes" description="Written by the subagent. Kept here until the pool closes." items={pick("own")} now={now} ttl={data.ttl} onSelect={onSelectMemory} />
              <div className="grid gap-4 2xl:grid-cols-2">
                <TierTable tier="stm" title="Seeded from short-term" description="Shared by reference, so forgets and updates reach it." items={pick("seeded", "stm")} now={now} ttl={data.ttl} onSelect={onSelectMemory} />
                <TierTable tier="ltm" title="Seeded from long-term" description="Shared by reference, so forgets and updates reach it." items={pick("seeded", "ltm")} now={now} ttl={data.ttl} onSelect={onSelectMemory} />
              </div>
            </>
          )}
        </TabsContent>

        <TabsContent value="timeline">
          {tl ? <PoolTimeline series={tl.series} events={tl.events} onSelect={onSelectMemory} /> : <Skeleton className="h-64 w-full" />}
        </TabsContent>

        <TabsContent value="deleted" className="flex flex-col gap-4">
          {selection.kind === "session" && (
            <Card>
              <CardHeader>
                <CardTitle>Redacted messages</CardTitle>
                <CardDescription>Transcript lines from this session where forgotten content was erased in place.</CardDescription>
              </CardHeader>
              <CardContent>
                {redacted.length === 0 ? (
                  <p className="text-muted-foreground text-sm">No messages in this session were redacted.</p>
                ) : (
                  <ul className="flex flex-col gap-3">
                    {redacted.map((e) => (
                      <li key={e.id} className="flex flex-col gap-1 rounded-md border p-3">
                        <span className="text-muted-foreground text-xs">
                          {e.role} · {e.agent} · {clock(e.created_at)}
                        </span>
                        <p className="text-sm">
                          <RedactedText text={e.content} placeholder={forgetting!.placeholder} />
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader>
              <CardTitle>Removed memories</CardTitle>
              <CardDescription>Forgotten on request, expired from short-term memory, or merged away. Only ids remain.</CardDescription>
            </CardHeader>
            <CardContent>
              {removals.length === 0 ? (
                <p className="text-muted-foreground text-sm">Nothing has been removed from this pool.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>When</TableHead>
                      <TableHead>What happened</TableHead>
                      <TableHead>Memory id</TableHead>
                      <TableHead>By</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {removals.map((e, i) => (
                      <TableRow key={i}>
                        <TableCell className="tabular-nums">{clock(e.t)}</TableCell>
                        <TableCell>
                          {e.kind === "memory.archive" ? (
                            <Badge variant="outline">
                              <Dot className="bg-expired" />
                              expired
                            </Badge>
                          ) : e.detail.reason ? (
                            <Badge variant="outline">
                              <Dot className="bg-expired" />
                              {e.detail.reason === "expiry_or_merge" ? "expired or merged" : e.detail.reason}
                            </Badge>
                          ) : (
                            <Badge variant="destructive">forgotten on request</Badge>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs">{e.memory_id}</TableCell>
                        <TableCell>{e.agent}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function SessionHeader({ s }: { s: SessionSummary }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {s.agent}
          <Badge variant={s.ended_at ? "secondary" : "default"}>{s.ended_at ? "ended" : "live"}</Badge>
        </CardTitle>
        <CardDescription className="font-mono">
          {s.id} · {clock(s.started_at)}
          {s.ended_at && ` → ${clock(s.ended_at)}`}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
        <Stat label="Messages" value={s.events} />
        <Stat label="Transcript tokens" value={s.event_tokens.toLocaleString()} />
        <Stat label="Short-term" value={sum(s.stm)} />
        <Stat label="Long-term" value={sum(s.ltm)} />
        <Stat label="Forgotten" value={s.forgotten} />
        <Stat label="Sub-pools" value={s.pools.length} />
      </CardContent>
    </Card>
  );
}

function PoolHeader({ p }: { p: PoolSummary }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <BoxIcon className="text-muted-foreground size-4" />
          {p.purpose || "Sub-pool"}
          <Badge variant={p.status === "open" ? "default" : "secondary"}>{p.status}</Badge>
        </CardTitle>
        <CardDescription className="font-mono">
          {p.id} · spawned by {p.owner_agent} {since(p.created_at)}
          {p.status === "open" && p.expires_at && ` · expires ${until(p.expires_at)}`}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
        <Stat label="Seeded short-term" value={p.seeded_stm} />
        <Stat label="Seeded long-term" value={p.seeded_ltm} />
        <Stat label="Subagent writes" value={p.own_written} />
        <Stat label="Merged back" value={p.merged ?? "–"} />
        <Stat label="Discarded" value={p.discarded ?? "–"} />
        <Stat label="Closed because" value={p.close_reason ?? "–"} />
      </CardContent>
    </Card>
  );
}
