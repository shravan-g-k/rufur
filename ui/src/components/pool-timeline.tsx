import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ArchiveIcon,
  ArrowUpRightIcon,
  BoxIcon,
  CircleDotIcon,
  GitMergeIcon,
  PlayIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  SquareIcon,
  Trash2Icon,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { EvolutionPoint, TimelineEvent, clock } from "@/api";

// Stack order follows the validated adjacent palette order: LTM, STM, replaced, expired, forgotten.
// Colours point at --chart-N, not --color-<key>: ChartContainer defines --color-<key> from these, so that would be a cycle.
export const EVOLUTION_CONFIG = {
  ltm: { label: "Long-term", color: "var(--chart-1)" },
  stm: { label: "Short-term", color: "var(--chart-2)" },
  superseded: { label: "Replaced", color: "var(--chart-3)" },
  expired: { label: "Expired", color: "var(--chart-4)" },
  forgotten: { label: "Forgotten", color: "var(--chart-5)" },
} satisfies ChartConfig;

const tick = (t: number) => new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function EvolutionChart({ series }: { series: EvolutionPoint[] }) {
  return (
    <ChartContainer config={EVOLUTION_CONFIG} className="aspect-auto h-64 w-full">
      <AreaChart data={series} margin={{ left: 0, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={tick} tickLine={false} axisLine={false} minTickGap={48} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={32} />
        <ChartTooltip cursor content={<ChartTooltipContent indicator="line" labelFormatter={(_, p) => tick(p?.[0]?.payload?.t)} />} />
        <ChartLegend content={<ChartLegendContent />} />
        {(Object.keys(EVOLUTION_CONFIG) as (keyof typeof EVOLUTION_CONFIG)[]).map((k) => (
          <Area
            key={k}
            dataKey={k}
            type="stepAfter"
            stackId="a"
            // Fills only: a visible stroke on a zero-height band would draw over the band below it. The
            // stroke colour is still set because the legend takes its swatch colour from it.
            stroke={`var(--color-${k})`}
            strokeWidth={0}
            fill={`var(--color-${k})`}
            fillOpacity={0.85}
            isAnimationActive={false}
          />
        ))}
      </AreaChart>
    </ChartContainer>
  );
}

const EVENT: Record<string, { icon: LucideIcon; label: (e: TimelineEvent) => string }> = {
  "session.start": { icon: PlayIcon, label: (e) => `Session started by ${e.agent}` },
  "session.end": { icon: SquareIcon, label: (e) => `Session ended, ${e.detail.stored ?? 0} memories extracted` },
  "pool.spawn": { icon: BoxIcon, label: (e) => `Sub-pool spawned, seeded with ${e.detail.seeded ?? 0} memories` },
  "pool.close": {
    icon: GitMergeIcon,
    label: (e) => `Closed (${e.detail.reason ?? "closed"}): ${e.detail.merged ?? 0} merged back to STM, ${e.detail.discarded ?? 0} discarded`,
  },
  "memory.create": { icon: CircleDotIcon, label: () => "Learned" },
  "memory.promote": { icon: ArrowUpRightIcon, label: (e) => `Promoted to long-term (${e.detail.store})` },
  "memory.supersede": { icon: RefreshCwIcon, label: () => "Replaced by a newer version" },
  "memory.dispute": { icon: ShieldAlertIcon, label: () => "Disputed" },
  "memory.quarantine": { icon: ShieldAlertIcon, label: () => "Held for review" },
  "memory.confirm": { icon: CircleDotIcon, label: () => "Confirmed" },
  "memory.archive": { icon: ArchiveIcon, label: () => "Archived (short-term lifetime ran out)" },
  "memory.delete": {
    icon: Trash2Icon,
    label: (e) => (e.detail.reason ? "Deleted by expiry or merge" : "Forgotten on request (receipt issued)"),
  },
};

export function EventList({ events, onSelect }: { events: TimelineEvent[]; onSelect: (id: string) => void }) {
  if (!events.length) return <p className="text-muted-foreground text-sm">No history yet.</p>;
  const forgotten = new Set(events.filter((e) => e.kind === "memory.delete" && !e.detail.reason).map((e) => e.memory_id));
  return (
    <ol className="flex flex-col">
      {[...events].reverse().map((e, i) => {
        const meta = EVENT[e.kind] ?? { icon: CircleDotIcon, label: () => e.kind };
        const Icon = meta.icon;
        return (
          <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
            <span className="bg-border absolute top-6 bottom-0 left-3 w-px" aria-hidden />
            <span className="bg-muted text-muted-foreground flex size-6 shrink-0 items-center justify-center rounded-full">
              <Icon className="size-3.5" />
            </span>
            <div className="flex min-w-0 flex-col gap-0.5">
              <p className="text-sm font-medium">{meta.label(e)}</p>
              {e.memory_id &&
                (e.text ? (
                  <button className="text-muted-foreground truncate text-left text-sm hover:underline" onClick={() => onSelect(e.memory_id!)}>
                    {e.text}
                  </button>
                ) : (
                  <span className="text-muted-foreground font-mono text-xs">{forgotten.has(e.memory_id) ? "content erased" : "no longer stored"} · {e.memory_id}</span>
                ))}
              <time className="text-muted-foreground text-xs tabular-nums">
                {clock(e.t)}
                {e.agent && ` · ${e.agent}`}
              </time>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function PoolTimeline({ series, events, onSelect }: { series: EvolutionPoint[]; events: TimelineEvent[]; onSelect: (id: string) => void }) {
  return (
    <div className="grid gap-4 xl:grid-cols-5">
      <Card className="xl:col-span-3">
        <CardHeader>
          <CardTitle>How this pool evolved</CardTitle>
          <CardDescription>Memories in each state over time, replayed from the operation log.</CardDescription>
        </CardHeader>
        <CardContent>
          <EvolutionChart series={series} />
        </CardContent>
      </Card>
      <Card className="xl:col-span-2">
        <CardHeader>
          <CardTitle>Events</CardTitle>
          <CardDescription>Newest first.</CardDescription>
        </CardHeader>
        <CardContent className="max-h-[28rem] overflow-y-auto">
          <EventList events={events} onSelect={onSelect} />
        </CardContent>
      </Card>
    </div>
  );
}
