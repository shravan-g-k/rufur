import { useCallback, useEffect, useRef, useState } from "react";
import { BrainIcon, CoinsIcon, DatabaseIcon, InboxIcon, LayoutDashboardIcon, MoonIcon, ShieldCheckIcon, SunIcon, TriangleAlertIcon, WorkflowIcon, LayersIcon, type LucideIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { MemorySheet } from "@/components/memory-sheet";
import { AgentsPage } from "@/pages/agents";
import { ForgettingPage } from "@/pages/forgetting";
import { OverviewPage } from "@/pages/overview";
import { ReviewPage } from "@/pages/review";
import { Selection, SessionsPage } from "@/pages/sessions";
import { RawPage } from "@/pages/raw";
import { TokensPage } from "@/pages/tokens";
import { api, Sessions } from "@/api";

type Page = "overview" | "sessions" | "tokens" | "forgetting" | "review" | "agents" | "raw";

const PAGES: { id: Page; label: string; icon: LucideIcon; description: string }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboardIcon, description: "Memory across all sessions at a glance." },
  { id: "sessions", label: "Sessions & pools", icon: LayersIcon, description: "Each session, its subagent pools, and their short- and long-term memory." },
  { id: "tokens", label: "Tokens", icon: CoinsIcon, description: "What memory costs and saves, over time." },
  { id: "forgetting", label: "Forgetting", icon: ShieldCheckIcon, description: "What was deleted, and the proof that it’s gone." },
  { id: "review", label: "Review", icon: InboxIcon, description: "Held memories and conflicts waiting on a decision." },
  { id: "agents", label: "Handovers", icon: WorkflowIcon, description: "Tasks agents passed to each other." },
  { id: "raw", label: "Raw data", icon: DatabaseIcon, description: "Every table, vector and graph row exactly as stored, read-only." },
];

function useTheme() {
  const [dark, setDark] = useState(() => {
    try {
      const saved = localStorage.getItem("rufur-theme");
      if (saved) return saved === "dark";
    } catch {
      /* storage unavailable */
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("rufur-theme", dark ? "dark" : "light");
    } catch {
      /* storage unavailable */
    }
  }, [dark]);
  return [dark, setDark] as const;
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [sessions, setSessions] = useState<Sessions | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pool, setPool] = useState<Selection>(null);
  const [memory, setMemory] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [live, setLive] = useState(false);
  const [reviewCount, setReviewCount] = useState(0);
  const [dark, setDark] = useTheme();

  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    api.sessions().then(
      (s) => {
        setSessions(s);
        setLoadError(null);
      },
      (e) => setLoadError(e.message),
    );
    Promise.all([api.quarantine(), api.decisions()]).then(([q, d]) => setReviewCount(q.length + d.length), () => setReviewCount(0));
  }, [version]);

  // Live updates: any write, forget, pool change or handover anywhere refreshes the views.
  const timer = useRef<number>();
  useEffect(() => {
    const es = new EventSource("/v1/stream");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "recall" || ev.type === "event.created") return;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(refresh, 400);
    };
    return () => es.close();
  }, [refresh]);

  const current = PAGES.find((p) => p.id === page)!;
  const openSession = (id: string) => {
    setPool({ kind: "session", id });
    setPage("sessions");
  };

  return (
    <TooltipProvider>
      <SidebarProvider>
        <Sidebar collapsible="icon">
          <SidebarHeader>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton size="lg" onClick={() => setPage("overview")}>
                  <div className="bg-primary text-primary-foreground flex aspect-square size-8 items-center justify-center rounded-lg">
                    <BrainIcon className="size-4" />
                  </div>
                  <div className="grid flex-1 text-left leading-tight">
                    <span className="truncate font-semibold">RUFUR</span>
                    <span className="text-muted-foreground truncate text-xs">Memory dashboard</span>
                  </div>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarHeader>
          <SidebarContent>
            <SidebarGroup>
              <SidebarGroupContent>
                <SidebarMenu>
                  <NavItems
                    page={page}
                    onNavigate={setPage}
                    badges={{ review: reviewCount || undefined, sessions: sessions?.sessions.length }}
                  />
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>
          <SidebarFooter>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton tooltip={dark ? "Light theme" : "Dark theme"} onClick={() => setDark(!dark)}>
                  {dark ? <SunIcon /> : <MoonIcon />}
                  <span>{dark ? "Light theme" : "Dark theme"}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarFooter>
        </Sidebar>

        <SidebarInset>
          <header className="bg-background/95 sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b px-4 backdrop-blur">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-2 data-[orientation=vertical]:h-4" />
            <div className="flex min-w-0 flex-1 flex-col">
              <h1 className="truncate text-sm font-semibold">{current.label}</h1>
              <p className="text-muted-foreground hidden truncate text-xs sm:block">{current.description}</p>
            </div>
            <Badge variant={live ? "secondary" : "outline"} title={live ? "Receiving live updates" : "Not connected to the daemon"}>
              <span aria-hidden className={live ? "bg-chart-3 size-1.5 rounded-full" : "bg-muted-foreground size-1.5 rounded-full"} />
              {live ? "Live" : "Offline"}
            </Badge>
            <Button variant="ghost" size="sm" onClick={refresh}>
              Refresh
            </Button>
          </header>

          <main className="flex flex-1 flex-col gap-4 p-4 md:p-6">
            {loadError && (
              <Alert variant="destructive">
                <TriangleAlertIcon />
                <AlertTitle>Couldn’t load the dashboard</AlertTitle>
                <AlertDescription>{loadError}</AlertDescription>
              </Alert>
            )}
            {page === "overview" && <OverviewPage data={sessions} version={version} onChanged={refresh} onSelectMemory={setMemory} onOpenSession={openSession} />}
            {page === "sessions" && <SessionsPage data={sessions} selection={pool} onSelectPool={setPool} onSelectMemory={setMemory} version={version} />}
            {page === "tokens" && <TokensPage version={version} />}
            {page === "forgetting" && <ForgettingPage version={version} />}
            {page === "review" && <ReviewPage version={version} onChanged={refresh} onSelectMemory={setMemory} />}
            {page === "agents" && <AgentsPage version={version} onSelectMemory={setMemory} />}
            {page === "raw" && <RawPage />}
          </main>
        </SidebarInset>

        <MemorySheet id={memory} ttl={sessions?.ttl ?? null} onClose={() => setMemory(null)} onChanged={refresh} onSelect={setMemory} />
      </SidebarProvider>
    </TooltipProvider>
  );
}

function NavItems({ page, onNavigate, badges }: { page: Page; onNavigate: (p: Page) => void; badges: Partial<Record<Page, number>> }) {
  // On phones the sidebar is a sheet over the page, so close it once a page is picked.
  const { isMobile, setOpenMobile } = useSidebar();
  return (
    <>
      {PAGES.map((p) => (
        <SidebarMenuItem key={p.id}>
          <SidebarMenuButton
            isActive={page === p.id}
            tooltip={p.label}
            onClick={() => {
              onNavigate(p.id);
              if (isMobile) setOpenMobile(false);
            }}
          >
            <p.icon />
            <span>{p.label}</span>
          </SidebarMenuButton>
          {badges[p.id] !== undefined && <SidebarMenuBadge>{badges[p.id]}</SidebarMenuBadge>}
        </SidebarMenuItem>
      ))}
    </>
  );
}
