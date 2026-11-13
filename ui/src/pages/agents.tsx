import { useEffect, useState } from "react";
import { ArrowRightIcon, WorkflowIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { api, since, Task } from "@/api";

export function AgentsPage({ version, onSelectMemory }: { version: number; onSelectMemory: (id: string) => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  useEffect(() => {
    api.tasks().then(setTasks);
  }, [version]);

  if (!tasks.length) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <WorkflowIcon />
          </EmptyMedia>
          <EmptyTitle>No handovers yet</EmptyTitle>
          <EmptyDescription>When an agent stops mid-task it leaves a handover here for the next one to resume.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {tasks.map((t) => (
        <Card key={t.id}>
          <CardHeader>
            <CardTitle>{t.title || t.packet.goal}</CardTitle>
            <CardDescription className="flex flex-wrap items-center gap-2">
              <span>{t.from_agent ?? "unknown"}</span>
              <ArrowRightIcon className="size-3.5" />
              <span>{t.to_agent ?? "waiting for an agent"}</span>
              <Badge variant="secondary">{t.status.replace("_", " ")}</Badge>
              <span>· {since(t.updated_at)}</span>
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4 text-sm">
            <div className="grid gap-4 sm:grid-cols-2">
              <List title="Done" items={t.packet.steps_done} />
              <List title="Next" items={t.packet.steps_pending} />
            </div>
            {t.packet.decisions.length > 0 && (
              <List title="Decisions" items={t.packet.decisions.map((d) => (d.reason ? `${d.decision}, because ${d.reason}` : d.decision))} />
            )}
            {t.packet.memory_ids.length > 0 && (
              <div className="flex flex-wrap items-center gap-1">
                <span className="text-muted-foreground">Carries memories:</span>
                {t.packet.memory_ids.map((id, i) => (
                  <Badge key={id} variant="outline" asChild>
                    <button onClick={() => onSelectMemory(id)}>{i + 1}</button>
                  </Badge>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="flex flex-col gap-1">
      <p className="font-medium">{title}</p>
      {items.length === 0 ? (
        <p className="text-muted-foreground">–</p>
      ) : (
        <ul className="text-muted-foreground flex list-disc flex-col gap-0.5 pl-4">
          {items.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
