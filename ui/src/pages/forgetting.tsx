import { useEffect, useState } from "react";
import { CheckIcon, CircleSlashIcon, ShieldCheckIcon, ShieldXIcon, XIcon } from "lucide-react";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RedactedText } from "@/components/memory-ui";
import { cn } from "@/lib/utils";
import { api, clock, Forgetting, Probe, Receipt, since } from "@/api";

export const PROBE_LABEL: Record<string, string> = {
  memories: "Memory rows",
  memory_sources: "Source quotes",
  lineage: "Lineage links",
  vec_map: "Vector map",
  edges: "Graph edges",
  pool_members: "Sub-pool references",
  conflicts: "Conflicts",
  tasks: "Handover tasks",
  ledger: "Memory text",
  fts: "Keyword index",
  vector: "Nearest-neighbour search",
  file_chunks: "Document chunks",
  graph: "Relation graph",
  typed_rows: "Tables",
  events: "Raw transcripts",
  handover_packets: "Handover packets",
  recall: "Recall (paraphrased question)",
  byte_scan: "Raw bytes on disk",
};

const COUNT_LABEL: Record<string, string> = {
  ledger: "memory rows",
  fts: "keyword index rows",
  vector: "vectors",
  edge: "graph edges",
  typed_row: "table rows",
  file_chunk: "document chunks",
  file: "files",
  sources: "source quotes",
  journals: "journal entries",
  events_redacted: "messages redacted",
  tasks_redacted: "handovers redacted",
  conflicts_dropped: "conflicts dropped",
};

export function probeResult(p: Probe): "erased" | "absent" | "uninformative" | "found" {
  if (p.after_hits > 0) return "found";
  if (p.before_hits === null) return "absent"; // checked after only, e.g. the raw byte scan
  return p.informative ? "erased" : "uninformative";
}

export function ProbeMatrix({ probes }: { probes: Probe[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Where we looked</TableHead>
          <TableHead>Probe</TableHead>
          <TableHead className="text-right">Before</TableHead>
          <TableHead className="text-right">After</TableHead>
          <TableHead>Result</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {probes.map((p, i) => {
          const r = probeResult(p);
          return (
            <TableRow key={i}>
              <TableCell className="font-medium">{PROBE_LABEL[p.store] ?? p.store}</TableCell>
              <TableCell className="text-muted-foreground">{p.kind}</TableCell>
              <TableCell className="text-right tabular-nums">{p.before_hits === null ? "–" : p.before_hits}</TableCell>
              <TableCell className="text-right tabular-nums">{p.after_hits}</TableCell>
              <TableCell>
                {r === "erased" && (
                  <Badge variant="secondary">
                    <CheckIcon data-icon="inline-start" />
                    found before, gone after
                  </Badge>
                )}
                {r === "absent" && (
                  <Badge variant="secondary">
                    <CheckIcon data-icon="inline-start" />
                    absent after
                  </Badge>
                )}
                {r === "uninformative" && (
                  <Badge variant="outline">
                    <CircleSlashIcon data-icon="inline-start" />
                    couldn’t see it before
                  </Badge>
                )}
                {r === "found" && (
                  <Badge variant="destructive">
                    <XIcon data-icon="inline-start" />
                    still present
                  </Badge>
                )}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function ReceiptDetail({ r }: { r: Receipt }) {
  const pct = r.completeness === null ? null : Math.round(r.completeness * 100);
  const informative = r.probes.filter((p) => p.informative).length;
  const counts = Object.entries(r.counts).filter(([k, v]) => typeof v === "number" && v > 0 && COUNT_LABEL[k]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {pct === null ? "Forgotten without probes" : `${pct}% of deletion checks passed`}
        </CardTitle>
        <CardDescription>
          {r.memory_ids.length} {r.memory_ids.length === 1 ? "memory" : "memories"} forgotten by {r.agent} on {clock(r.issued_at)}.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {pct !== null && (
          <div className="flex flex-col gap-2">
            <Progress value={pct} aria-label="Deletion completeness" />
            <p className="text-muted-foreground text-xs">
              {informative} of {r.probes.length} probes could see the content before deletion; each must find nothing after.
            </p>
          </div>
        )}
        {r.probes.length > 0 && <ProbeMatrix probes={r.probes} />}
        {counts.length > 0 && (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">What was erased</p>
            <div className="flex flex-wrap gap-2">
              {counts.map(([k, v]) => (
                <Badge key={k} variant="outline" className="tabular-nums">
                  {v} {COUNT_LABEL[k]}
                </Badge>
              ))}
            </div>
          </div>
        )}
        <dl className="text-muted-foreground grid gap-1 font-mono text-xs break-all">
          <div>
            <dt className="inline">receipt </dt>
            <dd className="inline">{r.receipt_id}</dd>
          </div>
          <div>
            <dt className="inline">hash </dt>
            <dd className="inline">{r.hash}</dd>
          </div>
          <div>
            <dt className="inline">prev </dt>
            <dd className="inline">{r.prev_hash}</dd>
          </div>
          <div>
            <dt className="inline">erasure </dt>
            <dd className="inline">{r.erasure}</dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  );
}

export function ForgettingPage({ version }: { version: number }) {
  const [data, setData] = useState<Forgetting | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verifiedAt, setVerifiedAt] = useState<number | null>(null);

  useEffect(() => {
    api.forgetting().then(setData);
  }, [version]);

  async function verify() {
    setVerifying(true);
    try {
      const chain = await api.verifyForgetting();
      setData((d) => (d ? { ...d, chain } : d));
      setVerifiedAt(Date.now() / 1000);
    } finally {
      setVerifying(false);
    }
  }

  if (!data) return <Skeleton className="h-96 w-full" />;
  const receipt = data.receipts.find((r) => r.receipt_id === selected) ?? data.receipts[0];
  const avg = data.receipts.filter((r) => r.completeness !== null);
  const avgPct = avg.length ? Math.round((100 * avg.reduce((a, r) => a + (r.completeness ?? 0), 0)) / avg.length) : null;

  return (
    <div className="flex flex-col gap-4">
      <Alert variant={data.chain.valid ? "default" : "destructive"}>
        {data.chain.valid ? <ShieldCheckIcon /> : <ShieldXIcon />}
        <AlertTitle>{data.chain.valid ? "Every deletion receipt verifies" : "Receipt chain check failed"}</AlertTitle>
        <AlertDescription>
          {data.chain.valid
            ? `${data.chain.receipts} signed, hash-chained receipts. Their memory ids are still absent and nothing matching a tombstone has come back.`
            : [...data.chain.problems.map((p) => `${p.receipt}: ${p.problem}`), ...data.chain.resurfaced_memories.map((m) => `${m} resurfaced`)].join("; ")}
          {verifiedAt && ` Checked ${since(verifiedAt)}.`}
        </AlertDescription>
        <AlertAction>
          <Button size="sm" variant="outline" onClick={verify} disabled={verifying}>
            {verifying ? "Verifying…" : "Verify again"}
          </Button>
        </AlertAction>
      </Alert>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <Kpi label="Forgotten on request" value={data.counts.forgotten ?? 0} />
        <Kpi label="Average completeness" value={avgPct === null ? "–" : `${avgPct}%`} />
        <Kpi label="Messages redacted" value={data.redacted_events.length} />
        <Kpi label="Expired from short-term" value={(data.counts.archived ?? 0) + (data.counts.purged ?? 0)} />
        <Kpi label="Blocked from relearning" value={data.tombstones} hint="tombstones" />
      </div>

      {data.receipts.length === 0 ? (
        <Empty className="border">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <ShieldCheckIcon />
            </EmptyMedia>
            <EmptyTitle>Nothing forgotten yet</EmptyTitle>
            <EmptyDescription>Ask an agent to forget something, or use “Forget” on any memory. Each one issues a signed receipt here.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
          <Card className="h-fit">
            <CardHeader>
              <CardTitle>Receipts</CardTitle>
              <CardDescription>Newest first.</CardDescription>
            </CardHeader>
            <CardContent className="flex max-h-[32rem] flex-col gap-1 overflow-y-auto">
              {data.receipts.map((r) => (
                <button
                  key={r.receipt_id}
                  onClick={() => setSelected(r.receipt_id)}
                  className={cn("hover:bg-muted flex items-center justify-between gap-2 rounded-md px-2 py-2 text-left", r === receipt && "bg-muted")}
                >
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate text-sm font-medium">
                      {r.memory_ids.length} {r.memory_ids.length === 1 ? "memory" : "memories"} · {r.agent}
                    </span>
                    <span className="text-muted-foreground text-xs">{clock(r.issued_at)}</span>
                  </span>
                  <Badge variant={r.completeness === 1 ? "secondary" : r.completeness === null ? "outline" : "destructive"} className="tabular-nums">
                    {r.completeness === null ? "no probes" : `${Math.round(r.completeness * 100)}%`}
                  </Badge>
                </button>
              ))}
            </CardContent>
          </Card>
          {receipt && <ReceiptDetail r={receipt} />}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Redacted messages</CardTitle>
          <CardDescription>Transcript lines where forgotten content was erased in place. The rest of each message is kept.</CardDescription>
        </CardHeader>
        <CardContent>
          {data.redacted_events.length === 0 ? (
            <p className="text-muted-foreground text-sm">No messages have been redacted.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Session</TableHead>
                  <TableHead>Message</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.redacted_events.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell className="tabular-nums">{clock(e.created_at)}</TableCell>
                    <TableCell className="font-mono text-xs">{e.session_id ?? "–"}</TableCell>
                    <TableCell className="max-w-xl whitespace-normal">
                      <RedactedText text={e.content} placeholder={data.placeholder} />
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

function Kpi({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
        {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
      </CardHeader>
    </Card>
  );
}
