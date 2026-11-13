import { useEffect, useState } from "react";
import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api, Tokens } from "@/api";

export const OPS_CONFIG = {
  recall: { label: "Recall", color: "var(--chart-1)" },
  extract: { label: "Extract", color: "var(--chart-2)" },
  remember: { label: "Remember", color: "var(--chart-3)" },
  forget: { label: "Forget", color: "var(--chart-4)" },
} satisfies ChartConfig;

const SAVINGS_CONFIG = {
  full_history: { label: "Full history every time", color: "var(--muted-foreground)" },
  rufur: { label: "What RUFUR sent", color: "var(--chart-1)" },
} satisfies ChartConfig;

const fmt = (bucket: "hour" | "day") => (t: number) =>
  new Date(t * 1000).toLocaleString(undefined, bucket === "day" ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", hour: "2-digit" });
const compact = (n: number) => Intl.NumberFormat(undefined, { notation: "compact" }).format(n);

export function TokenChart({ data, bucket, height = "h-72" }: { data: Tokens; bucket: "hour" | "day"; height?: string }) {
  return (
    <ChartContainer config={OPS_CONFIG} className={`aspect-auto ${height} w-full`}>
      <LineChart data={data.series} margin={{ left: 0, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={fmt(bucket)} tickLine={false} axisLine={false} minTickGap={40} />
        <YAxis tickFormatter={compact} tickLine={false} axisLine={false} width={40} />
        <ChartTooltip content={<ChartTooltipContent indicator="line" labelFormatter={(_, p) => fmt(bucket)(p?.[0]?.payload?.t)} />} />
        <ChartLegend content={<ChartLegendContent />} />
        {(Object.keys(OPS_CONFIG) as (keyof typeof OPS_CONFIG)[]).map((k) => (
          <Line key={k} dataKey={k} type="monotone" stroke={`var(--color-${k})`} strokeWidth={2} dot={data.series.length < 3 ? { r: 4 } : false} isAnimationActive={false} />
        ))}
      </LineChart>
    </ChartContainer>
  );
}

export function TokensPage({ version }: { version: number }) {
  const [bucket, setBucket] = useState<"hour" | "day">("hour");
  const [data, setData] = useState<Tokens | null>(null);
  useEffect(() => {
    api.tokens(bucket).then(setData);
  }, [bucket, version]);

  if (!data) return <Skeleton className="h-96 w-full" />;
  const last = data.cumulative[data.cumulative.length - 1];
  const saved = last && last.full_history > 0 ? Math.round(100 * (1 - last.rufur / last.full_history)) : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>Tokens read by RUFUR</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{data.totals.tokens_in.toLocaleString()}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Tokens sent to agents</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{data.totals.tokens_out.toLocaleString()}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Local model tokens (extract, consolidate, judge)</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{data.totals.llm_tokens.toLocaleString()}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Token usage over time</CardTitle>
          <CardDescription>Input, output and model tokens per operation, {bucket === "hour" ? "hourly over the last 7 days" : "daily over the last 30 days"}.</CardDescription>
          <CardAction>
            <ToggleGroup type="single" variant="outline" size="sm" value={bucket} onValueChange={(v) => v && setBucket(v as "hour" | "day")}>
              <ToggleGroupItem value="hour">Hourly</ToggleGroupItem>
              <ToggleGroupItem value="day">Daily</ToggleGroupItem>
            </ToggleGroup>
          </CardAction>
        </CardHeader>
        <CardContent>
          {data.series.length ? <TokenChart data={data} bucket={bucket} /> : <p className="text-muted-foreground text-sm">No operations in this period.</p>}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader>
            <CardTitle>Context saved</CardTitle>
            <CardDescription>
              Cumulative tokens if every recall had carried the whole conversation history, against what RUFUR actually sent
              {saved !== null && `: ${saved}% less`}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {data.cumulative.length ? (
              <ChartContainer config={SAVINGS_CONFIG} className="aspect-auto h-64 w-full">
                <AreaChart data={data.cumulative} margin={{ left: 0, right: 12, top: 8 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={fmt(bucket)} tickLine={false} axisLine={false} minTickGap={40} />
                  <YAxis tickFormatter={compact} tickLine={false} axisLine={false} width={40} />
                  <ChartTooltip content={<ChartTooltipContent indicator="line" labelFormatter={(_, p) => fmt("hour")(p?.[0]?.payload?.t)} />} />
                  <ChartLegend content={<ChartLegendContent />} />
                  <Area dataKey="full_history" type="monotone" stroke="var(--color-full_history)" strokeDasharray="4 4" strokeWidth={2} fill="none" isAnimationActive={false} />
                  <Area dataKey="rufur" type="monotone" stroke="var(--color-rufur)" strokeWidth={2} fill="var(--color-rufur)" fillOpacity={0.2} isAnimationActive={false} />
                </AreaChart>
              </ChartContainer>
            ) : (
              <p className="text-muted-foreground text-sm">No recalls yet.</p>
            )}
          </CardContent>
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>By agent</CardTitle>
            <CardDescription>In this period.</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead className="text-right">In</TableHead>
                  <TableHead className="text-right">Out</TableHead>
                  <TableHead className="text-right">Model</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.by_agent.map((a) => (
                  <TableRow key={a.agent}>
                    <TableCell className="max-w-40 truncate font-medium">{a.agent}</TableCell>
                    <TableCell className="text-right tabular-nums">{a.calls}</TableCell>
                    <TableCell className="text-right tabular-nums">{compact(a.tokens_in)}</TableCell>
                    <TableCell className="text-right tabular-nums">{compact(a.tokens_out)}</TableCell>
                    <TableCell className="text-right tabular-nums">{compact(a.llm_tokens)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
