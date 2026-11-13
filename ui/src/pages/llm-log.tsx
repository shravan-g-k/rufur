import { useEffect, useState } from "react";
import { CircleCheckIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, LlmLog, since, until } from "@/api";

export function LlmLogPage({ version }: { version: number }) {
  const [data, setData] = useState<LlmLog | null>(null);
  useEffect(() => {
    api.llmLog().then(setData);
  }, [version]);

  if (!data) return <Skeleton className="h-96 w-full" />;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardDescription>Fell back to another model (last 24 h)</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{data.last_24h.fallback}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Failed on every model (last 24 h)</CardDescription>
            <CardTitle className={`text-2xl tabular-nums ${data.last_24h.failed ? "text-destructive" : ""}`}>{data.last_24h.failed}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Model chain per role</CardTitle>
          <CardDescription>Tried left to right. Local Ollama always comes last, so a rate limit or outage falls back to it.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {Object.entries(data.chains).map(([role, models]) => (
            <div key={role} className="flex flex-wrap items-center gap-2 text-sm">
              <span className="w-24 font-medium">{role}</span>
              {models.map((m, i) => (
                <span key={m} className="flex items-center gap-2">
                  {i > 0 && <span className="text-muted-foreground">→</span>}
                  <Badge variant="outline">{m}</Badge>
                </span>
              ))}
            </div>
          ))}
          {Object.entries(data.keys).map(([env, k]) => (
            <p key={env} className="text-muted-foreground mt-2 text-sm">
              <span className="text-foreground font-medium">{env}</span>: {k.total} key{k.total === 1 ? "" : "s"} in rotation
              {k.spent > 0 ? `, ${k.spent} at their daily limit (next back ${until(k.next_back!)})` : ", none at their daily limit"}. Add more
              as <code>{env}S=key1,key2</code> in <code>~/.rufur/.env</code>.
            </p>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Log</CardTitle>
          <CardDescription>Every call that needed a fallback or failed outright, newest first. Calls that worked first time aren’t listed.</CardDescription>
        </CardHeader>
        <CardContent>
          {data.rows.length === 0 ? (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <CircleCheckIcon />
                </EmptyMedia>
                <EmptyTitle>No fallbacks or failures</EmptyTitle>
                <EmptyDescription>Every model call so far was answered by the first model in its chain.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>What went wrong</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.rows.map((r) => (
                  <TableRow key={r.id} className="align-top">
                    <TableCell className="text-muted-foreground whitespace-nowrap">{since(r.created_at)}</TableCell>
                    <TableCell className="font-medium">{r.role}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {r.outcome === "failed" ? <Badge variant="destructive">failed</Badge> : <Badge variant="secondary">answered by {r.model}</Badge>}
                    </TableCell>
                    <TableCell className="max-w-xl">
                      <ul className="flex flex-col gap-1 font-mono text-xs break-all whitespace-normal">
                        {r.errors.map((e, i) => (
                          <li key={i}>{e}</li>
                        ))}
                      </ul>
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
