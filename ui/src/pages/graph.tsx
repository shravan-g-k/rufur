import { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, GraphData, GraphLink, GraphNode } from "@/api";

// Where each kind of link is stored, so the picture says how the graph is kept, not just what's in it.
const LINKS: Record<GraphLink["kind"], { label: string; stored: string; dash?: string; color: string }> = {
  relation: { label: "Relation", stored: "edges table: src → relation → dst, one row per relation memory", color: "var(--chart-1)" },
  about: { label: "About", stored: "memories.subject: the entity a memory is about", color: "var(--border)" },
  supersedes: { label: "Replaced by", stored: "memories.superseded_by: the newer version", dash: "6 4", color: "var(--chart-3)" },
  co_source: { label: "Learned together", stored: "memory_sources: two memories quoting the same message", dash: "2 4", color: "var(--chart-4)" },
};

const STATUS_COLOR: Record<string, string> = {
  active: "var(--chart-1)",
  disputed: "oklch(0.86 0.17 95)",
  quarantined: "oklch(0.62 0.22 350)",
  superseded: "var(--chart-3)",
  archived: "var(--chart-4)",
  rejected: "oklch(0.6 0.02 260)",
};

type P = GraphNode & { x: number; y: number; vx: number; vy: number };

// A plain force layout: links pull, everything repels, a weak pull to the centre keeps islands on screen.
// Computed once per data load; graphs here are a few hundred nodes, so O(n²) repulsion is fine.
function layout(nodes: GraphNode[], links: GraphLink[], w: number, h: number): Map<string, P> {
  const pts = new Map<string, P>();
  nodes.forEach((n, i) => {
    const a = (i / Math.max(1, nodes.length)) * Math.PI * 2;
    const r = n.kind === "memory" ? 260 : 120;
    pts.set(n.id, { ...n, x: w / 2 + Math.cos(a) * r, y: h / 2 + Math.sin(a) * r, vx: 0, vy: 0 });
  });
  const arr = [...pts.values()];
  const edges = links.map((l) => [pts.get(l.source), pts.get(l.target), l.kind] as const).filter(([a, b]) => a && b) as [P, P, string][];
  for (let it = 0; it < 300; it++) {
    const cool = 1 - it / 300;
    for (let i = 0; i < arr.length; i++) {
      for (let j = i + 1; j < arr.length; j++) {
        const a = arr[i],
          b = arr[j];
        let dx = a.x - b.x,
          dy = a.y - b.y;
        const d2 = Math.max(dx * dx + dy * dy, 1);
        const f = 1800 / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        a.vx += dx * f;
        a.vy += dy * f;
        b.vx -= dx * f;
        b.vy -= dy * f;
      }
    }
    for (const [a, b, kind] of edges) {
      const want = kind === "relation" ? 110 : kind === "about" ? 70 : 90;
      const dx = b.x - a.x,
        dy = b.y - a.y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = (d - want) * 0.02;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (const p of arr) {
      p.vx += (w / 2 - p.x) * 0.002;
      p.vy += (h / 2 - p.y) * 0.002;
      const v = Math.sqrt(p.vx * p.vx + p.vy * p.vy);
      const cap = 12 * cool + 0.5;
      if (v > cap) {
        p.vx = (p.vx / v) * cap;
        p.vy = (p.vy / v) * cap;
      }
      p.x += p.vx;
      p.y += p.vy;
      p.vx *= 0.6;
      p.vy *= 0.6;
    }
  }
  return pts;
}

export function GraphPage({ version, onSelectMemory }: { version: number; onSelectMemory: (id: string) => void }) {
  const [data, setData] = useState<GraphData | null>(null);
  const [kinds, setKinds] = useState<Record<GraphLink["kind"], boolean>>({ relation: true, about: true, supersedes: true, co_source: false });
  const [showMemories, setShowMemories] = useState(true);
  const [hover, setHover] = useState<string | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const W = 1000,
    H = 640;

  useEffect(() => {
    api.graph().then(setData);
  }, [version]);

  const { nodes, links, pts } = useMemo(() => {
    if (!data) return { nodes: [], links: [], pts: new Map<string, P>() };
    const nodes = data.nodes.filter((n) => showMemories || n.kind !== "memory");
    const keep = new Set(nodes.map((n) => n.id));
    const links = data.links.filter((l) => kinds[l.kind] && keep.has(l.source) && keep.has(l.target));
    // Entities with nothing left to connect to would just float; hide them.
    const linked = new Set(links.flatMap((l) => [l.source, l.target]));
    const shown = nodes.filter((n) => n.kind === "memory" || linked.has(n.id));
    return { nodes: shown, links, pts: layout(shown, links, W, H) };
  }, [data, kinds, showMemories]);

  if (!data) return <Skeleton className="h-[640px] w-full" />;

  const counts = data.links.reduce<Record<string, number>>((a, l) => ((a[l.kind] = (a[l.kind] ?? 0) + 1), a), {});
  const near = hover ? new Set(links.filter((l) => l.source === hover || l.target === hover).flatMap((l) => [l.source, l.target])) : null;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Knowledge graph</CardTitle>
          <CardDescription>
            {nodes.length} nodes, {links.length} links shown. Entities are the named things memories are about; relation memories also write an edge between two
            entities. Drag to pan, scroll to zoom, select a memory to inspect it.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {(Object.keys(LINKS) as GraphLink["kind"][]).map((k) => (
              <Button key={k} size="sm" variant={kinds[k] ? "secondary" : "outline"} onClick={() => setKinds({ ...kinds, [k]: !kinds[k] })}>
                <svg width="22" height="8" aria-hidden>
                  <line x1="1" y1="4" x2="21" y2="4" stroke={LINKS[k].color} strokeWidth="2" strokeDasharray={LINKS[k].dash} />
                </svg>
                {LINKS[k].label} <span className="text-muted-foreground tabular-nums">{counts[k] ?? 0}</span>
              </Button>
            ))}
            <Button size="sm" variant={showMemories ? "secondary" : "outline"} onClick={() => setShowMemories(!showMemories)}>
              {showMemories ? "Hide memories (entities only)" : "Show memories"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setView({ x: 0, y: 0, k: 1 })}>
              Reset view
            </Button>
          </div>
          {nodes.length === 0 ? (
            <p className="text-muted-foreground py-12 text-center text-sm">Nothing to draw with these filters.</p>
          ) : (
            <svg
              viewBox={`0 0 ${W} ${H}`}
              className="bg-muted/30 h-[640px] w-full cursor-grab touch-none rounded-md border active:cursor-grabbing"
              onWheel={(e) => {
                const k = Math.min(4, Math.max(0.3, view.k * (e.deltaY < 0 ? 1.1 : 0.9)));
                setView({ ...view, k });
              }}
              onPointerDown={(e) => (drag.current = { x: e.clientX - view.x, y: e.clientY - view.y })}
              onPointerMove={(e) => drag.current && setView({ ...view, x: e.clientX - drag.current.x, y: e.clientY - drag.current.y })}
              onPointerUp={() => (drag.current = null)}
              onPointerLeave={() => (drag.current = null)}
            >
              <defs>
                <marker id="arrow" viewBox="0 0 10 10" refX="16" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                  <path d="M0,0 L10,5 L0,10 z" fill="var(--chart-3)" />
                </marker>
              </defs>
              <g transform={`translate(${view.x} ${view.y}) translate(${W / 2} ${H / 2}) scale(${view.k}) translate(${-W / 2} ${-H / 2})`}>
                {links.map((l, i) => {
                  const a = pts.get(l.source)!,
                    b = pts.get(l.target)!;
                  const dim = near && !(near.has(l.source) && near.has(l.target));
                  return (
                    <g key={i} opacity={dim ? 0.15 : 1}>
                      <line
                        x1={a.x}
                        y1={a.y}
                        x2={b.x}
                        y2={b.y}
                        stroke={LINKS[l.kind].color}
                        strokeWidth={l.kind === "relation" ? 2 : 1}
                        strokeDasharray={LINKS[l.kind].dash}
                        markerEnd={l.kind === "supersedes" || l.kind === "relation" ? "url(#arrow)" : undefined}
                      />
                      {l.kind === "relation" && l.label && (
                        <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4} textAnchor="middle" className="fill-foreground text-[10px]">
                          {l.label}
                        </text>
                      )}
                    </g>
                  );
                })}
                {nodes.map((n) => {
                  const p = pts.get(n.id)!;
                  const dim = near && !near.has(n.id);
                  const isMem = n.kind === "memory";
                  return (
                    <g
                      key={n.id}
                      transform={`translate(${p.x} ${p.y})`}
                      opacity={dim ? 0.2 : 1}
                      onPointerEnter={() => setHover(n.id)}
                      onPointerLeave={() => setHover(null)}
                      onPointerDown={(e) => isMem && e.stopPropagation()}
                      onClick={() => isMem && onSelectMemory(n.id)}
                      className={isMem ? "cursor-pointer" : undefined}
                    >
                      {isMem ? (
                        <circle
                          r={4 + 4 * (n.importance ?? 0.5)}
                          fill={STATUS_COLOR[n.status ?? "active"] ?? "var(--chart-1)"}
                          stroke="var(--card)"
                          strokeWidth={1.5}
                        />
                      ) : (
                        <rect x={-7} y={-7} width={14} height={14} rx={3} fill="var(--foreground)" />
                      )}
                      {(!isMem || hover === n.id) && (
                        <text y={isMem ? -12 : -11} textAnchor="middle" className={`fill-foreground ${isMem ? "text-[11px]" : "text-[12px] font-medium"}`}>
                          {n.label.length > 60 ? n.label.slice(0, 57) + "…" : n.label}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            </svg>
          )}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span className="flex items-center gap-1.5">
              <svg width="12" height="12" aria-hidden>
                <rect x="1" y="1" width="10" height="10" rx="2" fill="var(--foreground)" />
              </svg>
              entity / subject
            </span>
            {Object.entries(STATUS_COLOR).map(([s, c]) => (
              <span key={s} className="flex items-center gap-1.5">
                <svg width="10" height="10" aria-hidden>
                  <circle cx="5" cy="5" r="4" fill={c} />
                </svg>
                {s === "quarantined" ? "held" : s} memory
              </span>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How the graph is stored</CardTitle>
          <CardDescription>
            The graph isn’t a separate database. It’s drawn from these SQLite tables, which are also what recall and forgetting read.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          {(Object.keys(LINKS) as GraphLink["kind"][]).map((k) => (
            <div key={k} className="flex flex-wrap items-baseline gap-2">
              <Badge variant="outline">{LINKS[k].label}</Badge>
              <span className="text-muted-foreground">{LINKS[k].stored}</span>
              <span className="tabular-nums">· {counts[k] ?? 0}</span>
            </div>
          ))}
          <p className="text-muted-foreground mt-1 text-xs">
            Recall today finds relations through the sentence each relation memory also keeps; it does not walk the edges table yet, so two-hop questions depend
            on vector and full-text search (docs/results/storage_choice.md measures how much a code walk would add). Forgetting a memory deletes its edges, and
            entities left with no edges. See Raw data → Graph for the rows themselves.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
