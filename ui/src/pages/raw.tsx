import { useEffect, useMemo, useState } from "react";
import { ArrowLeftIcon, ArrowRightIcon, ChevronLeftIcon, ChevronRightIcon, DatabaseIcon, FileIcon, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { api, clock, RawBlob, RawCatalog, RawLink, RawRows, RawTable, RawValue } from "@/api";

type View = { table: string; column?: string; value?: string; offset: number; limit: number; desc: boolean };

const GROUP_LABEL: Record<RawTable["group"], string> = {
  relational: "Relational tables",
  fts: "Full-text index (FTS5)",
  vector: "Vector store (sqlite-vec)",
  graph: "Graph (entities and relations)",
  system: "SQLite internal",
};

const TABS: { id: string; label: string; groups: RawTable["group"][] }[] = [
  { id: "all", label: "All tables", groups: ["relational", "graph", "vector", "fts", "system"] },
  { id: "relational", label: "Relational", groups: ["relational"] },
  { id: "vector", label: "Vector store", groups: ["vector"] },
  { id: "graph", label: "Graph", groups: ["graph"] },
  { id: "fts", label: "Full-text index", groups: ["fts"] },
];

const bytes = (n: number | null) => (n === null ? "–" : n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`);
const isBlob = (v: RawValue): v is RawBlob => typeof v === "object" && v !== null && "$blob" in v;

function decodeBlob(b: RawBlob) {
  const bin = atob(b.$blob);
  const u8 = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  const hex = Array.from(u8, (x) => x.toString(16).padStart(2, "0")).join(" ");
  const floats = u8.byteLength % 4 === 0 ? Array.from(new Float32Array(u8.buffer)) : null;
  return { hex, floats };
}

/** The link a cell follows, if its column points at another table (and, for polymorphic columns, its row matches). */
function linkFor(links: RawLink[], table: string, column: string, row: Record<string, RawValue>): RawLink | undefined {
  return links.find((l) => l.from_table === table && l.from_column === column && (!l.when || String(row[l.when.column]) === l.when.equals));
}

export function RawPage() {
  const [catalog, setCatalog] = useState<RawCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState("all");
  const [view, setView] = useState<View>({ table: "memories", offset: 0, limit: 100, desc: false });
  const [history, setHistory] = useState<View[]>([]);

  const load = () => api.rawCatalog().then((c) => (setCatalog(c), setError(null)), (e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  const go = (v: View) => {
    setHistory((h) => [...h, view]);
    setView(v);
  };
  const back = () => {
    const prev = history[history.length - 1];
    if (!prev) return;
    setHistory((h) => h.slice(0, -1));
    setView(prev);
  };
  const open = (table: string, column?: string, value?: string) => go({ table, column, value, offset: 0, limit: view.limit, desc: false });

  if (error)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn’t read the database</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  if (!catalog) return <Skeleton className="h-96 w-full" />;

  const db = catalog.database;
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <DatabaseIcon className="text-muted-foreground size-4" />
            <span className="font-mono text-sm">{db.path}</span>
          </CardTitle>
          <CardDescription>
            {bytes(db.bytes)} on disk, WAL {bytes(db.wal_bytes)} · SQLite {db.sqlite} · sqlite-vec {db.sqlite_vec} · vectors of {db.embed_dim} float32 ·{" "}
            {catalog.tables.length} tables · read-only connection
          </CardDescription>
          <CardAction>
            <Button size="sm" variant="outline" onClick={load}>
              Reload counts
            </Button>
          </CardAction>
        </CardHeader>
      </Card>

      <Tabs
        value={tab}
        onValueChange={(id) => {
          setTab(id);
          // Keep the open table if it belongs to the new tab, otherwise open the tab's first table.
          const groups = TABS.find((t) => t.id === id)?.groups;
          const inTab = catalog.tables.filter((t) => groups?.includes(t.group));
          if (groups && !inTab.some((t) => t.name === view.table) && inTab[0]) open(inTab[0].name);
        }}
      >
        <TabsList className="flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
            </TabsTrigger>
          ))}
          <TabsTrigger value="links">Links</TabsTrigger>
          <TabsTrigger value="files">Files</TabsTrigger>
        </TabsList>

        {TABS.map((t) => (
          <TabsContent key={t.id} value={t.id}>
            <div className="grid gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
              <TableList tables={catalog.tables.filter((x) => t.groups.includes(x.group))} current={view.table} onPick={(n) => open(n)} />
              <TableView catalog={catalog} view={view} setView={setView} open={open} back={history.length ? back : undefined} />
            </div>
          </TabsContent>
        ))}

        <TabsContent value="links">
          <LinksView catalog={catalog} onOpen={(table) => (open(table), setTab("all"))} />
        </TabsContent>

        <TabsContent value="files">
          <Card>
            <CardHeader>
              <CardTitle>Files</CardTitle>
              <CardDescription className="font-mono">{catalog.files_dir}</CardDescription>
            </CardHeader>
            <CardContent>
              {catalog.files.length === 0 ? (
                <p className="text-muted-foreground text-sm">The directory is empty.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Path</TableHead>
                      <TableHead className="text-right">Size</TableHead>
                      <TableHead>Modified</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {catalog.files.map((f) => (
                      <TableRow key={f.path}>
                        <TableCell className="font-mono text-xs">
                          <FileIcon className="text-muted-foreground mr-2 inline size-3.5" />
                          {f.path}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{bytes(f.bytes)}</TableCell>
                        <TableCell>{clock(f.modified)}</TableCell>
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

function TableList({ tables, current, onPick }: { tables: RawTable[]; current: string; onPick: (name: string) => void }) {
  const groups = [...new Set(tables.map((t) => t.group))];
  return (
    <Card className="h-fit lg:sticky lg:top-4">
      <CardContent className="flex max-h-[75vh] flex-col gap-3 overflow-y-auto">
        {groups.map((g) => (
          <div key={g} className="flex flex-col gap-0.5">
            <p className="text-muted-foreground px-2 pb-1 text-xs font-medium">{GROUP_LABEL[g]}</p>
            {tables
              .filter((t) => t.group === g)
              .map((t) => (
                <button
                  key={t.name}
                  onClick={() => onPick(t.name)}
                  aria-current={t.name === current || undefined}
                  className={cn("hover:bg-muted flex items-center gap-2 rounded-md px-2 py-1 text-left", t.name === current && "bg-muted", t.shadow_of && "pl-5")}
                >
                  <span className={cn("min-w-0 flex-1 truncate font-mono text-xs", t.shadow_of && "text-muted-foreground")}>{t.name}</span>
                  {t.type === "virtual" && <Badge variant="outline">virtual</Badge>}
                  <span className="text-muted-foreground text-xs tabular-nums">{t.rows ?? "?"}</span>
                </button>
              ))}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function TableView({ catalog, view, setView, open, back }: {
  catalog: RawCatalog;
  view: View;
  setView: (v: View) => void;
  open: (table: string, column?: string, value?: string) => void;
  back?: () => void;
}) {
  const meta = catalog.tables.find((t) => t.name === view.table);
  const [data, setData] = useState<RawRows | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [row, setRow] = useState<Record<string, RawValue> | null>(null);

  useEffect(() => {
    setError(null);
    api.rawRows(view.table, view).then(setData, (e) => setError(e.message));
  }, [view]);

  const outgoing = catalog.links.filter((l) => l.from_table === view.table);
  const incoming = catalog.links.filter((l) => l.to_table === view.table);
  const objects = useMemo(() => (data ? data.rows.map((r) => Object.fromEntries(data.columns.map((c, i) => [c, r[i]]))) : []), [data]);

  if (!meta) return <p className="text-muted-foreground text-sm">No table named {view.table}.</p>;
  const page = (offset: number) => setView({ ...view, offset });

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            {back && (
              <Button size="icon-sm" variant="ghost" onClick={back} aria-label="Back">
                <ArrowLeftIcon />
              </Button>
            )}
            <span className="font-mono">{meta.name}</span>
            <Badge variant="outline">{meta.type}</Badge>
            {meta.shadow_of && <Badge variant="secondary">shadow table of {meta.shadow_of}</Badge>}
            <Badge variant="secondary" className="tabular-nums">
              {meta.rows ?? "?"} rows
            </Badge>
          </CardTitle>
          <CardDescription>
            {meta.columns.length} columns{meta.has_rowid ? " plus rowid" : ""} · {meta.indexes.length} indexes
            {meta.error && ` · count failed: ${meta.error}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Collapsible>
            <CollapsibleTrigger asChild>
              <Button variant="outline" size="sm">
                Schema (CREATE statement, columns, indexes)
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="flex flex-col gap-3 pt-3">
              <pre className="bg-muted overflow-x-auto rounded-md p-3 font-mono text-xs whitespace-pre-wrap">{meta.sql ?? "(no SQL: created by SQLite or a module)"}</pre>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Column</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>PK</TableHead>
                    <TableHead>Not null</TableHead>
                    <TableHead>Default</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {meta.columns.map((c) => (
                    <TableRow key={c.name}>
                      <TableCell className="font-mono text-xs">{c.name}</TableCell>
                      <TableCell className="font-mono text-xs">{c.type || "–"}</TableCell>
                      <TableCell>{c.pk || ""}</TableCell>
                      <TableCell>{c.notnull ? "yes" : ""}</TableCell>
                      <TableCell className="font-mono text-xs">{c.default ?? ""}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {meta.indexes.map((i) => (
                <pre key={i.name} className="text-muted-foreground font-mono text-xs whitespace-pre-wrap">
                  {i.sql ?? `${i.name} (automatic index)`}
                </pre>
              ))}
            </CollapsibleContent>
          </Collapsible>

          {(outgoing.length > 0 || incoming.length > 0) && (
            <div className="grid gap-3 md:grid-cols-2">
              <LinkList title="Points to" links={outgoing} side="to" onOpen={(t) => open(t)} />
              <LinkList title="Pointed to by" links={incoming} side="from" onOpen={(t) => open(t)} />
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
            {view.column !== undefined ? (
              <Badge variant="secondary" className="font-mono">
                WHERE {view.column} = {view.value}
                <button onClick={() => setView({ ...view, column: undefined, value: undefined, offset: 0 })} aria-label="Clear filter">
                  <XIcon data-icon="inline-end" />
                </button>
              </Badge>
            ) : (
              <span className="text-muted-foreground font-normal">All rows</span>
            )}
          </CardTitle>
          <CardDescription className="tabular-nums">
            {data ? `${data.total === 0 ? 0 : data.offset + 1}–${data.offset + data.rows.length} of ${data.total}` : "…"} · ordered by {meta.has_rowid ? `rowid ${view.desc ? "descending" : "ascending"}` : "storage order"}
          </CardDescription>
          <CardAction className="flex items-center gap-2">
            {meta.has_rowid && (
              <Button size="sm" variant="outline" onClick={() => setView({ ...view, desc: !view.desc, offset: 0 })}>
                {view.desc ? "Newest first" : "Oldest first"}
              </Button>
            )}
            <Select value={String(view.limit)} onValueChange={(v) => setView({ ...view, limit: Number(v), offset: 0 })}>
              <SelectTrigger size="sm" aria-label="Rows per page">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {[50, 100, 500, 1000].map((n) => (
                    <SelectItem key={n} value={String(n)}>
                      {n} / page
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <Button size="icon-sm" variant="outline" disabled={!data || view.offset === 0} onClick={() => page(Math.max(0, view.offset - view.limit))} aria-label="Previous page">
              <ChevronLeftIcon />
            </Button>
            <Button size="icon-sm" variant="outline" disabled={!data || view.offset + view.limit >= data.total} onClick={() => page(view.offset + view.limit)} aria-label="Next page">
              <ChevronRightIcon />
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Query failed</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : !data ? (
            <Skeleton className="h-64 w-full" />
          ) : data.rows.length === 0 ? (
            <p className="text-muted-foreground text-sm">No rows.</p>
          ) : (
            <div className="max-h-[65vh] overflow-auto rounded-md border">
              <Table>
                <TableHeader className="bg-background sticky top-0">
                  <TableRow>
                    {data.columns.map((c) => (
                      <TableHead key={c} className="font-mono text-xs">
                        {c}
                        {outgoing.some((l) => l.from_column === c) && <ArrowRightIcon className="text-muted-foreground ml-1 inline size-3" />}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {objects.map((r, i) => (
                    <TableRow key={i} className="cursor-pointer" onClick={() => setRow(r)}>
                      {data.columns.map((c) => (
                        <TableCell key={c} className="max-w-80 font-mono text-xs">
                          <Cell value={r[c]} link={linkFor(catalog.links, view.table, c, r)} open={open} />
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <RowSheet row={row} table={view.table} catalog={catalog} onClose={() => setRow(null)} open={(t, c, v) => (setRow(null), open(t, c, v))} />
    </div>
  );
}

function LinkList({ title, links, side, onOpen }: { title: string; links: RawLink[]; side: "to" | "from"; onOpen: (table: string) => void }) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium">{title}</p>
      {links.length === 0 && <p className="text-muted-foreground text-xs">nothing</p>}
      {links.map((l, i) => (
        <button key={i} onClick={() => onOpen(side === "to" ? l.to_table : l.from_table)} className="text-left font-mono text-xs hover:underline">
          {l.from_table}.{l.from_column} → {l.to_table}.{l.to_column}
          {l.when && <span className="text-muted-foreground"> when {l.when.column} = '{l.when.equals}'</span>}
        </button>
      ))}
    </div>
  );
}

function Cell({ value, link, open }: { value: RawValue; link?: RawLink; open: (t: string, c?: string, v?: string) => void }) {
  if (value === null) return <span className="text-muted-foreground italic">NULL</span>;
  if (isBlob(value)) return <Badge variant="outline">BLOB {bytes(value.bytes)}</Badge>;
  const text = String(value);
  if (link)
    return (
      <button
        className="text-primary truncate underline decoration-dotted underline-offset-2"
        title={`${link.to_table}.${link.to_column} = ${text}`}
        onClick={(e) => {
          e.stopPropagation();
          open(link.to_table, link.to_column, text);
        }}
      >
        {text}
      </button>
    );
  return (
    <span className="line-clamp-2 break-all" title={text.length > 80 ? text : undefined}>
      {text}
    </span>
  );
}

function RowSheet({ row, table, catalog, onClose, open }: {
  row: Record<string, RawValue> | null;
  table: string;
  catalog: RawCatalog;
  onClose: () => void;
  open: (t: string, c?: string, v?: string) => void;
}) {
  // Rows elsewhere that point at this one, by the columns this table is referenced through.
  const incoming = catalog.links.filter((l) => l.to_table === table);
  return (
    <Sheet open={!!row} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle className="font-mono">{table}</SheetTitle>
          <SheetDescription>One row, every column as stored.</SheetDescription>
        </SheetHeader>
        {row && (
          <div className="flex flex-col gap-4 px-4 pb-6">
            <dl className="flex flex-col divide-y rounded-md border">
              {Object.entries(row).map(([k, v]) => {
                const link = linkFor(catalog.links, table, k, row);
                return (
                  <div key={k} className="grid grid-cols-[10rem_minmax(0,1fr)] gap-3 p-2">
                    <dt className="text-muted-foreground font-mono text-xs">{k}</dt>
                    <dd className="font-mono text-xs break-all whitespace-pre-wrap">
                      {isBlob(v) ? <BlobView blob={v} /> : v === null ? <span className="text-muted-foreground italic">NULL</span> : String(v)}
                      {link && v !== null && (
                        <Button size="xs" variant="link" onClick={() => open(link.to_table, link.to_column, String(v))}>
                          open {link.to_table}.{link.to_column}
                        </Button>
                      )}
                    </dd>
                  </div>
                );
              })}
            </dl>
            {incoming.length > 0 && (
              <div className="flex flex-col gap-1">
                <p className="text-sm font-medium">Rows that point here</p>
                {incoming.map((l, i) =>
                  row[l.to_column] === null || row[l.to_column] === undefined ? null : (
                    <Button key={i} size="xs" variant="link" className="justify-start font-mono" onClick={() => open(l.from_table, l.from_column, String(row[l.to_column]))}>
                      {l.from_table} WHERE {l.from_column} = {String(row[l.to_column])}
                      {l.when && ` (when ${l.when.column} = '${l.when.equals}')`}
                    </Button>
                  ),
                )}
              </div>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

function BlobView({ blob }: { blob: RawBlob }) {
  const [mode, setMode] = useState<"hex" | "float32">("hex");
  const { hex, floats } = useMemo(() => decodeBlob(blob), [blob]);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span>
          BLOB, {blob.bytes} bytes{blob.truncated && " (first 64 KB shown)"}
        </span>
        <Button size="xs" variant={mode === "hex" ? "secondary" : "ghost"} onClick={() => setMode("hex")}>
          hex
        </Button>
        {floats && (
          <Button size="xs" variant={mode === "float32" ? "secondary" : "ghost"} onClick={() => setMode("float32")}>
            as float32 ({floats.length})
          </Button>
        )}
      </div>
      <pre className="bg-muted max-h-64 overflow-auto rounded-md p-2 text-[11px] leading-relaxed whitespace-pre-wrap">
        {mode === "hex" || !floats ? hex : floats.map((f) => f.toPrecision(6)).join(", ")}
      </pre>
    </div>
  );
}

function LinksView({ catalog, onOpen }: { catalog: RawCatalog; onOpen: (table: string) => void }) {
  const declared = catalog.tables.flatMap((t) => t.foreign_keys.map((fk) => ({ ...fk, from_table: t.name })));
  return (
    <Card>
      <CardHeader>
        <CardTitle>What points where</CardTitle>
        <CardDescription>
          The schema declares {declared.length} foreign keys, so these are RUFUR’s id columns, listed by name. A link with a condition depends on another column in the
          same row (lineage.child_kind, vec_map.kind).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>From</TableHead>
              <TableHead />
              <TableHead>To</TableHead>
              <TableHead>Only when</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {catalog.links.map((l, i) => (
              <TableRow key={i}>
                <TableCell className="font-mono text-xs">
                  <button className="hover:underline" onClick={() => onOpen(l.from_table)}>
                    {l.from_table}
                  </button>
                  .{l.from_column}
                </TableCell>
                <TableCell>
                  <ArrowRightIcon className="text-muted-foreground size-3.5" />
                </TableCell>
                <TableCell className="font-mono text-xs">
                  <button className="hover:underline" onClick={() => onOpen(l.to_table)}>
                    {l.to_table}
                  </button>
                  .{l.to_column}
                </TableCell>
                <TableCell className="text-muted-foreground font-mono text-xs">{l.when ? `${l.when.column} = '${l.when.equals}'` : ""}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
