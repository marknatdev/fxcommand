import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { api } from "../api/client";
import type { PreflightReport } from "../api/types";
import { cn } from "../lib/format";
import { Badge, Button, ErrorBox, Loading } from "./ui";

/** The Pre-flight Check list. Blocking failures (live Broker Sessions only) are marked. */
export function PreflightList({ report, compact = false }: { report: PreflightReport; compact?: boolean }) {
  return (
    <div className="space-y-2" data-testid="preflight">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {report.ok ? <Badge tone="up" testId="preflight-status">ready</Badge> : <Badge tone="down" testId="preflight-status">blocked</Badge>}
        {report.paper && <Badge tone="accent">PAPER — nothing is sent to the account</Badge>}
        {report.live && !report.paper && <Badge tone="down">LIVE account — blocking checks enforced</Badge>}
        {!report.live && !report.paper && <span className="text-xs text-dim">Demo account: checks are advisory.</span>}
      </div>
      <ul className={cn("divide-y divide-line/60 rounded-md border border-line", compact && "max-h-72 overflow-y-auto")}>
        {report.checks.map((c) => (
          <li key={c.id} className="flex items-start gap-2.5 px-3 py-2 text-sm" data-testid={`check-${c.id}`} data-status={c.status}>
            {c.status === "pass" ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-up" />
            ) : c.status === "warn" ? (
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
            ) : (
              <XCircle className={cn("mt-0.5 size-4 shrink-0", c.blocking ? "text-down" : "text-warn")} />
            )}
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className={cn(c.status === "fail" && c.blocking && "font-medium text-down")}>{c.label}</span>
                {c.status === "fail" && c.blocking && <Badge tone="down">blocks start</Badge>}
              </div>
              <div className="break-words text-xs text-dim">{c.detail}</div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Loads and shows the Pre-flight Check for a Session (or the general one when no id). */
export function PreflightPanel({ sessionId, compact }: { sessionId?: number; compact?: boolean }) {
  const q = useQuery({ queryKey: ["preflight", sessionId ?? null], queryFn: () => api.preflight(sessionId), refetchInterval: 15_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div className="space-y-2">
      <PreflightList report={q.data!} compact={compact} />
      <Button size="sm" variant="ghost" icon={<RefreshCw className="size-3.5" />} loading={q.isFetching} onClick={() => q.refetch()} data-testid="preflight-refresh">
        Re-check
      </Button>
    </div>
  );
}
