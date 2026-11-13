import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { clock, hours, Status, Ttl } from "@/api";

const TIER_DOT = { stm: "bg-stm", ltm: "bg-ltm" } as const;

/** A colour key for a memory state; the colour classes come from the shared chart palette tokens. */
export function Dot({ className }: { className: string }) {
  return <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", className)} />;
}

export function TierDot({ tier }: { tier: "stm" | "ltm" }) {
  return <Dot className={TIER_DOT[tier]} />;
}

export function TierBadge({ tier }: { tier: "stm" | "ltm" }) {
  return (
    <Badge variant="outline">
      <TierDot tier={tier} />
      {tier === "stm" ? "Short-term" : "Long-term"}
    </Badge>
  );
}

const STATUS_VARIANT: Record<Status, "secondary" | "outline" | "destructive" | "ghost"> = {
  active: "secondary",
  disputed: "outline",
  quarantined: "outline",
  superseded: "ghost",
  archived: "ghost",
  rejected: "destructive",
};

const STATUS_LABEL: Record<Status, string> = {
  active: "active",
  disputed: "disputed",
  quarantined: "held for review",
  superseded: "replaced",
  archived: "archived",
  rejected: "rejected",
};

export function StatusBadge({ status }: { status: Status }) {
  return <Badge variant={STATUS_VARIANT[status]}>{STATUS_LABEL[status]}</Badge>;
}

/**
 * Where a short-term item sits in its life: the bar spans creation to the hard cap, the filled part is
 * the time it has left. Ticks mark the base lifetime and the cap; each use pushes expiry out by the
 * extension, never past the cap.
 */
export function TtlBar({ created, expires, now, ttl }: { created: number; expires: number | null; now: number; ttl: Ttl }) {
  if (!expires) return <span className="text-muted-foreground text-xs">no expiry</span>;
  const span = ttl.max_h * 3600;
  const pos = (t: number) => `${Math.min(100, Math.max(0, ((t - created) / span) * 100))}%`;
  const left = (expires - now) / 3600;
  const extended = expires - created > ttl.base_h * 3600 + 60;
  const urgent = left < 12;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex min-w-36 flex-col gap-1" tabIndex={0}>
          <div className="bg-muted relative h-1.5 w-full rounded-full">
            <div
              className={cn("absolute inset-y-0 rounded-full", urgent ? "bg-forgotten" : "bg-stm")}
              style={{ left: pos(now), right: `calc(100% - ${pos(expires)})` }}
            />
            <span className="bg-foreground/40 absolute -top-0.5 h-2.5 w-px" style={{ left: pos(created + ttl.base_h * 3600) }} />
          </div>
          <span className="text-muted-foreground text-xs tabular-nums">
            {left <= 0 ? "expired, awaiting sweep" : `expires in ${hours(left)}`}
            {extended && " · extended by use"}
          </span>
        </div>
      </TooltipTrigger>
      <TooltipContent>
        <p>Created {clock(created)}, expires {clock(expires)}.</p>
        <p>
          Lives {ttl.base_h} h (tick), +{ttl.extend_h} h per use, at most {ttl.max_h} h.
        </p>
      </TooltipContent>
    </Tooltip>
  );
}

/** Transcript text with forgotten spans drawn as redaction bars. The bars hold no content. */
export function RedactedText({ text, placeholder }: { text: string; placeholder: string }) {
  const parts = text.split(placeholder);
  return (
    <span>
      {parts.map((p, i) => (
        <span key={i}>
          {p}
          {i < parts.length - 1 && (
            <span role="img" aria-label="forgotten text" className="bg-foreground mx-0.5 inline-block h-[1em] w-16 rounded-sm align-middle" />
          )}
        </span>
      ))}
    </span>
  );
}
