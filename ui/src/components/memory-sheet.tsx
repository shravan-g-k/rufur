import { useEffect, useState } from "react";
import { CheckIcon, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge, TierBadge, TtlBar } from "@/components/memory-ui";
import { PROBE_LABEL } from "@/pages/forgetting";
import { api, ForgetResult, Memory, since, SOURCE_LABEL, STORE_LABEL, Ttl } from "@/api";

interface Props {
  id: string | null;
  ttl: Ttl | null;
  onClose: () => void;
  onChanged: () => void;
  onSelect: (id: string) => void;
}

export function MemorySheet({ id, ttl, onClose, onChanged, onSelect }: Props) {
  const [m, setM] = useState<Memory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [forgetting, setForgetting] = useState(false);
  const [result, setResult] = useState<ForgetResult | null>(null);

  useEffect(() => {
    setM(null);
    setError(null);
    setResult(null);
    setConfirming(false);
    if (id) api.memory(id).then(setM, (e) => setError(String(e.message)));
  }, [id]);

  async function decide(action: "confirm" | "reject") {
    if (!id) return;
    await api.confirm(id, action);
    setM(await api.memory(id));
    onChanged();
  }

  async function forget() {
    if (!id) return;
    setForgetting(true);
    try {
      setResult(await api.forget(id));
      onChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setForgetting(false);
    }
  }

  const now = Date.now() / 1000;
  return (
    <Sheet open={!!id} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        {result ? (
          <ForgetSummary r={result} />
        ) : !m ? (
          <>
            <SheetHeader>
              <SheetTitle>Memory</SheetTitle>
              <SheetDescription>{error ? `No longer available. ${error}` : "Loading…"}</SheetDescription>
            </SheetHeader>
            {!error && <Skeleton className="mx-4 h-40" />}
          </>
        ) : (
          <>
            <SheetHeader>
              <div className="flex flex-wrap gap-2">
                <TierBadge tier={m.tier} />
                <StatusBadge status={m.status} />
                <Badge variant="outline">{m.type}</Badge>
              </div>
              <SheetTitle className="text-lg leading-snug">{m.text}</SheetTitle>
              <SheetDescription>
                {m.tier === "stm"
                  ? m.store === "discard"
                    ? "Kept short-term only; it won't be filed into long-term memory."
                    : "Waiting to be filed into long-term memory by consolidation."
                  : `Filed in ${STORE_LABEL[m.store] ?? m.store}.`}
                {m.scope && ` Applies to: ${m.scope}.`}
                {m.corroboration > 1 && ` Confirmed by ${m.corroboration} independent sources.`}
              </SheetDescription>
            </SheetHeader>

            <div className="flex flex-col gap-6 px-4">
              {m.tier === "stm" && ttl && (
                <div className="flex flex-col gap-2">
                  <p className="text-sm font-medium">Short-term lifetime</p>
                  <TtlBar created={m.created_at} expires={m.expires_at} now={now} ttl={ttl} />
                </div>
              )}

              <dl className="grid grid-cols-3 gap-3 text-sm">
                <Fact label="Trust" value={m.trust.replace("_", " ")} />
                <Fact label="Sensitivity" value={m.sensitivity} />
                <Fact label="Used" value={`${m.access_count} times`} />
                <Fact label="Written by" value={m.author_agent} />
                <Fact label="Pool" value={m.pool} />
                <Fact label="Learned" value={since(m.created_at)} />
              </dl>

              {(m.status === "quarantined" || m.status === "disputed") && (
                <Alert>
                  <AlertTitle>{m.status === "disputed" ? "Is this the correct version?" : "Should agents be allowed to use this?"}</AlertTitle>
                  <AlertDescription className="flex gap-2 pt-2">
                    <Button size="sm" onClick={() => decide("confirm")}>
                      {m.status === "disputed" ? "Keep this version" : "Approve"}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => decide("reject")}>
                      Reject
                    </Button>
                  </AlertDescription>
                </Alert>
              )}

              <section className="flex flex-col gap-2">
                <p className="text-sm font-medium">Where it came from</p>
                {m.sources.length === 0 && <p className="text-muted-foreground text-sm">No source recorded.</p>}
                {m.sources.map((s) => (
                  <figure key={s.event_id} className="flex flex-col gap-1">
                    <blockquote className="border-l-2 pl-3 text-sm italic">{s.quote}</blockquote>
                    <figcaption className="text-muted-foreground text-xs">
                      {SOURCE_LABEL[s.source_type] ?? s.source_type}, via {s.agent}
                      {s.created_at ? `, ${since(s.created_at)}` : ""}
                    </figcaption>
                  </figure>
                ))}
              </section>

              {m.history.length > 1 && (
                <section className="flex flex-col gap-2">
                  <p className="text-sm font-medium">Versions</p>
                  <ol className="flex flex-col gap-1.5">
                    {m.history.map((h) => (
                      <li key={h.id} className="flex flex-col">
                        <button className="text-left text-sm hover:underline disabled:font-medium disabled:no-underline" disabled={h.id === m.id} onClick={() => onSelect(h.id)}>
                          {h.text}
                        </button>
                        <span className="text-muted-foreground text-xs">
                          {since(h.valid_from)}
                          {h.valid_to ? `, replaced ${since(h.valid_to)}` : ""}
                        </span>
                      </li>
                    ))}
                  </ol>
                </section>
              )}

              {m.flags.length > 0 && (
                <section className="flex flex-col gap-2">
                  <p className="text-sm font-medium">Warnings</p>
                  <div className="flex flex-wrap gap-1.5">
                    {m.flags.map((f) => (
                      <Badge key={f} variant="outline">
                        {flagText(f)}
                      </Badge>
                    ))}
                  </div>
                </section>
              )}
            </div>

            <Separator />
            <SheetFooter>
              {!confirming ? (
                <Button variant="destructive" onClick={() => setConfirming(true)}>
                  Forget this memory
                </Button>
              ) : (
                <div className="flex flex-col gap-3">
                  <p className="text-muted-foreground text-sm">
                    Deletes the memory, its earlier versions and everything derived from it, redacts it from transcripts, then probes every store to prove it’s gone. It
                    can’t be undone.
                  </p>
                  <div className="flex gap-2">
                    <Button variant="destructive" onClick={forget} disabled={forgetting}>
                      {forgetting ? "Forgetting and verifying…" : "Forget permanently"}
                    </Button>
                    <Button variant="outline" onClick={() => setConfirming(false)} disabled={forgetting}>
                      Keep it
                    </Button>
                  </div>
                </div>
              )}
            </SheetFooter>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="truncate">{value}</dd>
    </div>
  );
}

function flagText(f: string): string {
  if (f === "instruction_like") return "contains instructions (treated as data)";
  if (f === "unverified_user_claim") return "unverified user claim";
  if (f === "user_confirmed") return "confirmed by the user";
  if (f === "agent_quarantined") return "author quarantined";
  if (f.startsWith("supersede_needs_confirmation")) return "would replace a trusted memory";
  if (f.startsWith("from_pool:")) return `from sub-pool ${f.slice(10)}`;
  return f;
}

function ForgetSummary({ r }: { r: ForgetResult }) {
  const pct = r.report ? Math.round(r.report.completeness * 100) : null;
  return (
    <>
      <SheetHeader>
        <Badge variant="destructive">Forgotten</Badge>
        <SheetTitle className="text-lg">{pct === null ? `Forgot ${r.forgotten.length} memories` : `${pct}% of deletion checks passed`}</SheetTitle>
        <SheetDescription>
          Removed {r.forgotten.length} {r.forgotten.length === 1 ? "memory" : "memories"} and redacted {r.redacted_events} transcript{" "}
          {r.redacted_events === 1 ? "entry" : "entries"}. A tombstone stops it from being learned again. The full receipt is on the Forgetting page.
        </SheetDescription>
      </SheetHeader>
      {r.report && (
        <div className="flex flex-wrap gap-1.5 px-4">
          {Object.entries(r.report.stores).map(([store, v]) => (
            <Badge key={store} variant={v.passed ? "secondary" : "destructive"}>
              {v.passed ? <CheckIcon data-icon="inline-start" /> : <XIcon data-icon="inline-start" />}
              {PROBE_LABEL[store] ?? store}
            </Badge>
          ))}
        </div>
      )}
    </>
  );
}
