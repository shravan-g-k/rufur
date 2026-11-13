import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge, TierDot, TtlBar } from "@/components/memory-ui";
import { PoolMemory, since, STORE_LABEL, Ttl } from "@/api";

interface Props {
  title: string;
  description: string;
  tier: "stm" | "ltm" | "own";
  items: PoolMemory[];
  now: number;
  ttl: Ttl;
  onSelect: (id: string) => void;
}

/** One tier of one pool: its memories, with time left for short-term items and the store for long-term ones. */
export function TierTable({ title, description, tier, items, now, ttl, onSelect }: Props) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {tier !== "own" && <TierDot tier={tier} />}
          {title}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction>
          <Badge variant="outline" className="tabular-nums">
            {items.length}
          </Badge>
        </CardAction>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="text-muted-foreground text-sm">Nothing here.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Memory</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>{tier === "ltm" ? "Filed in" : "Time left"}</TableHead>
                <TableHead className="text-right">Used</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((m) => (
                <TableRow key={m.id} className="cursor-pointer" onClick={() => onSelect(m.id)}>
                  <TableCell className="min-w-48 max-w-72 whitespace-normal">
                    <span className="line-clamp-2">{m.text}</span>
                    <span className="text-muted-foreground text-xs">
                      {m.type} · {m.author_agent} · {since(m.created_at)}
                    </span>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={m.status} />
                  </TableCell>
                  <TableCell>
                    {m.tier === "stm" ? (
                      <TtlBar created={m.created_at} expires={m.expires_at} now={now} ttl={ttl} />
                    ) : (
                      <span className="text-muted-foreground text-sm">{STORE_LABEL[m.store] ?? m.store}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{m.access_count}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
