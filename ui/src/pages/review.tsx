import { useEffect, useState } from "react";
import { InboxIcon, SparklesIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { api, Decision, SettledConflict, since, SOURCE_LABEL, until } from "@/api";

export function ReviewPage({ version, onChanged, onSelectMemory }: { version: number; onChanged: () => void; onSelectMemory: (id: string) => void }) {
  const [items, setItems] = useState<Awaited<ReturnType<typeof api.quarantine>>>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [settled, setSettled] = useState<SettledConflict[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.quarantine().then(setItems);
    api.decisions().then(setDecisions);
    api.conflictHistory().then(setSettled);
  }, [version]);

  async function decide(d: Decision, winner: string | null) {
    await api.decide(d.conflict_id, winner, winner ? "keep" : "neither");
    onChanged();
  }

  async function autoReview() {
    setBusy(true);
    try {
      await api.autoReview();
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  async function review(id: string, action: "confirm" | "reject") {
    await api.confirm(id, action);
    onChanged();
  }

  if (!items.length && !decisions.length && !settled.length) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <InboxIcon />
          </EmptyMedia>
          <EmptyTitle>Nothing to review</EmptyTitle>
          <EmptyDescription>Suspicious or conflicting memories show up here.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-sm">
          RUFUR’s model settles what it can. Whatever only you can answer stays here and is asked about the next time it comes up.
        </p>
        <Button size="sm" variant="outline" disabled={busy} onClick={autoReview}>
          <SparklesIcon />
          {busy ? "Deciding…" : "Let RUFUR decide"}
        </Button>
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <section className="flex flex-col gap-3">
          <div>
            <h2 className="text-base font-semibold">Held for review</h2>
            <p className="text-muted-foreground text-sm">From low-trust sources or tried to give instructions. Agents can’t see them until you approve.</p>
          </div>
          {items.length === 0 && <p className="text-muted-foreground text-sm">Nothing held.</p>}
          {items.map((m) => (
            <Card key={m.id}>
              <CardHeader>
                <CardTitle>
                  <button className="text-left hover:underline" onClick={() => onSelectMemory(m.id)}>
                    {m.text}
                  </button>
                </CardTitle>
                <CardDescription>
                  {SOURCE_LABEL[m.source_type] ?? m.source_type}, by {m.author_agent}, {since(m.created_at)}
                </CardDescription>
              </CardHeader>
              {m.sources[0] && (
                <CardContent>
                  <blockquote className="text-muted-foreground border-l-2 pl-3 text-sm italic">{m.sources[0].quote}</blockquote>
                </CardContent>
              )}
              <CardFooter className="gap-2">
                <Button size="sm" onClick={() => review(m.id, "confirm")}>
                  Approve
                </Button>
                <Button size="sm" variant="outline" onClick={() => review(m.id, "reject")}>
                  Reject
                </Button>
              </CardFooter>
            </Card>
          ))}
        </section>

        <section className="flex flex-col gap-3">
          <div>
            <h2 className="text-base font-semibold">Open decisions</h2>
            <p className="text-muted-foreground text-sm">RUFUR couldn’t settle these itself. Without a decision, the safer option is kept automatically.</p>
          </div>
          {decisions.length === 0 && <p className="text-muted-foreground text-sm">No open decisions.</p>}
          {decisions.map((d) => (
            <Card key={d.conflict_id}>
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-2">
                  {d.question}
                  {d.ask_user ? <Badge variant="secondary">disputed: ask the user</Badge> : d.needs_user && <Badge variant="outline">needs you</Badge>}
                </CardTitle>
                <CardDescription>
                  {d.tried.map((t) => t.outcome).join("; ")}.{" "}
                  {d.default_at === null
                    ? "Not decided automatically: agents ask you for a clarification the next time this comes up."
                    : `Decided automatically ${until(d.default_at)}.`}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {d.options.map((o) => (
                  <div key={o.memory_id} className="flex items-start justify-between gap-3 rounded-md border p-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <button className="text-left text-sm font-medium hover:underline" onClick={() => onSelectMemory(o.memory_id)}>
                        {o.text}
                      </button>
                      <span className="text-muted-foreground text-xs">
                        {SOURCE_LABEL[o.source] ?? o.source} via {o.agent}, {since(o.recorded_at)}
                        {d.default_at !== null && o.memory_id === d.recommendation.memory_id && ` · default (${d.recommendation.reason})`}
                      </span>
                    </div>
                    <Button size="sm" variant="outline" onClick={() => decide(d, o.memory_id)}>
                      Keep
                    </Button>
                  </div>
                ))}
              </CardContent>
              <CardFooter>
                <Button size="sm" variant="ghost" onClick={() => decide(d, null)}>
                  Neither is right
                </Button>
              </CardFooter>
            </Card>
          ))}
        </section>
      </div>

      <section className="flex flex-col gap-3">
        <div>
          <h2 className="text-base font-semibold">Settled conflicts</h2>
          <p className="text-muted-foreground text-sm">Disagreements already decided, by RUFUR, an agent, you, or the default after a week. Newest first.</p>
        </div>
        {settled.length === 0 && <p className="text-muted-foreground text-sm">No conflicts settled yet.</p>}
        {settled.map((c) => (
          <SettledCard key={c.conflict_id} c={c} onSelectMemory={onSelectMemory} />
        ))}
      </section>
    </div>
  );
}

const SETTLED_BY: Record<string, string> = {
  memory_layer: "RUFUR",
  user: "you",
  agent: "an agent",
  default: "the default",
};

function SettledCard({ c, onSelectMemory }: { c: SettledConflict; onSelectMemory: (id: string) => void }) {
  const r = c.resolution;
  const outcome =
    r?.action === "both" ? "Both kept, each for its own situation" : r?.action === "neither" ? "Neither kept" : "Kept the highlighted one";
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
          {c.subject ? `Disagreement about ${c.subject}` : "Disagreement"}
          <Badge variant="outline">{c.kind}</Badge>
        </CardTitle>
        <CardDescription>
          {outcome}, decided by {SETTLED_BY[r?.by ?? ""] ?? r?.by ?? "RUFUR"} {since(c.resolved_at ?? c.created_at)}
          {r?.reason ? `: ${r.reason}` : "."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {c.options.map((o) => {
          const won = r?.action === "both" || o.memory_id === r?.winner;
          return (
            <button
              key={o.memory_id}
              onClick={() => onSelectMemory(o.memory_id)}
              className={`rounded-md border p-2 text-left text-sm hover:underline ${won ? "border-primary bg-primary/5 font-medium" : "text-muted-foreground line-through decoration-1"}`}
            >
              {o.text}
              <span className="text-muted-foreground ml-2 text-xs no-underline">
                {SOURCE_LABEL[o.source] ?? o.source} · {o.status}
              </span>
            </button>
          );
        })}
      </CardContent>
    </Card>
  );
}
