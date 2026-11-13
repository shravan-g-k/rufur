import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Access, api, Role } from "@/api";

const who = (entries: string[]) => entries.map((e) => (e === "*" ? "every agent" : e.startsWith("user:") ? `all of ${e.slice(5)}’s agents` : e)).join(", ");

export function AccessPage({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [data, setData] = useState<Access | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.access().then(setData);
  }, [version]);

  async function setRole(agent: string, role: Role) {
    setError(null);
    try {
      await api.setRole(agent, role);
    } catch (e: any) {
      setError(e.message);
    }
    onChanged();
  }

  if (!data) return <Skeleton className="h-96 w-full" />;
  const levels = data.roles.map((r) => r.role);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Who can read which memories</CardTitle>
          <CardDescription>
            Role-based access. Every memory has a sensitivity level, and an agent reads only memories at or below its role. RUFUR applies this
            to recall, search and subagent pools before anything reaches the agent.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-2 sm:grid-cols-4">
            {data.roles.map((r) => (
              <div key={r.role} className="rounded-md border p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium capitalize">{r.role}</span>
                  <span className="text-muted-foreground text-xs tabular-nums">{r.memories} memories</span>
                </div>
                <p className="text-muted-foreground mt-1 text-xs">{r.description}</p>
              </div>
            ))}
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Agent</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Can read</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.agents.map((a) => (
                <TableRow key={a.name}>
                  <TableCell className="font-medium">
                    {a.name}
                    {a.quarantined && (
                      <Badge variant="destructive" className="ml-2">
                        quarantined
                      </Badge>
                    )}
                    {!a.editable && <span className="text-muted-foreground ml-2 text-xs">(this dashboard)</span>}
                  </TableCell>
                  <TableCell>
                    <Select value={a.role} disabled={!a.editable} onValueChange={(v) => setRole(a.name, v as Role)}>
                      <SelectTrigger size="sm" className="w-36 capitalize" aria-label={`Role for ${a.name}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {levels.map((l) => (
                            <SelectItem key={l} value={l} className="capitalize">
                              {l}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">{levels.slice(0, levels.indexOf(a.role) + 1).join(", ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {error && <p className="text-destructive text-sm">{error}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Pool access</CardTitle>
          <CardDescription>On top of roles, each pool lists who may read and write it.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Pool</TableHead>
                <TableHead>Read</TableHead>
                <TableHead>Write</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.pools.map((p) => (
                <TableRow key={p.name}>
                  <TableCell className="font-medium">{p.name}</TableCell>
                  <TableCell className="text-muted-foreground text-sm whitespace-normal">{who(p.read ?? [])}</TableCell>
                  <TableCell className="text-muted-foreground text-sm whitespace-normal">{who(p.write ?? [])}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
