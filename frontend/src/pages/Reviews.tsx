import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ResearchTrial, Review } from "../api/types";
import { r2 } from "../components/evidence";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader } from "../components/ui";
import { serverTime, strategyTitle } from "../lib/format";

const day = (ts: number | null | undefined) => serverTime(ts).slice(0, 10);

function paramsText(t: ResearchTrial) {
  const p = Object.entries(t.params ?? {}).map(([k, v]) => `${k}=${v}`);
  return `${strategyTitle[t.strategy] ?? t.strategy}${p.length ? ` (${p.join(", ")})` : ""}`;
}

function HoldoutResult({ t }: { t: ResearchTrial }) {
  const r = t.result as { candidate?: { trades: number; mean_r: number }; champion?: { trades: number; mean_r: number } | null };
  if (!r?.candidate) return <span className="text-xs text-faint">—</span>;
  return (
    <span className="text-xs">
      <span className="num">{r2(r.candidate.mean_r, 3)}</span> over {r.candidate.trades} trades
      {r.champion && (
        <span className="text-dim">
          {" "}
          · Champion <span className="num">{r2(r.champion.mean_r, 3)}</span>
        </span>
      )}
    </span>
  );
}

function ActionLink({ a }: { a: Record<string, unknown> }) {
  const url = typeof a.url === "string" && /^https:\/\//.test(a.url) ? a.url : null;
  const label = String(a.label ?? a.kind ?? a.type ?? "action");
  if (url)
    return (
      <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
        {label} <ExternalLink className="size-3" />
      </a>
    );
  if (a.kind === "challenger" || a.type === "challenger")
    return (
      <Link to="/learning" className="text-accent hover:underline">
        {label}
      </Link>
    );
  return <span>{label}</span>;
}

function ReviewItem({ r }: { r: Review }) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({ queryKey: ["review", r.id], queryFn: () => api.review(r.id), enabled: open });
  return (
    <div className="rounded-lg border border-line-2 bg-bg/40" data-testid="review-item">
      <button className="flex w-full items-start gap-2 p-3 text-left" onClick={() => setOpen(!open)} data-testid="review-toggle">
        {open ? <ChevronDown className="mt-0.5 size-4 shrink-0" /> : <ChevronRight className="mt-0.5 size-4 shrink-0" />}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{r.title}</span>
            <span className="text-xs text-faint">{day(r.ts)}</span>
            {r.arenas.map((a) => (
              <Badge key={a}>{a}</Badge>
            ))}
          </div>
          <p className="text-sm text-dim">{r.summary}</p>
        </div>
      </button>
      {open && (
        <div className="space-y-3 border-t border-line p-3 text-sm">
          {r.finalists.length > 0 && (
            <div>
              <div className="label">Finalists</div>
              <ul className="list-disc space-y-0.5 pl-5">
                {r.finalists.map((f, i) => (
                  <li key={i}>{String(f.label ?? f.hypothesis ?? JSON.stringify(f))}{f.holdout ? ` — holdout ${String(f.holdout)}` : ""}</li>
                ))}
              </ul>
            </div>
          )}
          {r.actions.length > 0 && (
            <div>
              <div className="label">Actions</div>
              <ul className="space-y-0.5">
                {r.actions.map((a, i) => (
                  <li key={i}>
                    <ActionLink a={a} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <div className="label">Ledger when submitted</div>
            <p className="text-xs text-dim">
              {Object.entries(r.ledger).map(([k, v]) => `${k}: ${v.trials} trials, ${v.holdout_uses} holdout uses`).join(" · ") || "no trials recorded"}
            </p>
          </div>
          <div>
            <div className="label">Report</div>
            {detail.isLoading ? <Loading /> : <pre className="whitespace-pre-wrap break-words rounded-md bg-panel-2/60 p-3 font-sans text-sm" data-testid="review-report">{detail.data?.report || "—"}</pre>}
          </div>
        </div>
      )}
    </div>
  );
}

export default function Reviews() {
  const reviews = useQuery({ queryKey: ["reviews"], queryFn: api.reviews, refetchInterval: 60_000 });
  const trials = useQuery({ queryKey: ["trials"], queryFn: () => api.trials({ limit: 500 }), refetchInterval: 60_000 });
  const holdouts = (trials.data?.trials ?? []).filter((t) => t.holdout_used);
  return (
    <div className="space-y-5">
      <PageHeader
        title="Strategy Reviews"
        subtitle="The weekly Claude review: mistakes found, hypotheses tested (every one counted in the ledger), finalists scored once on the sealed holdout, and what was submitted. It can only submit Challengers and reports, and propose code as pull requests."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <Card title="Trial ledger" testId="ledger-card" bodyClass="p-0">
          {trials.isLoading ? (
            <Loading />
          ) : trials.error ? (
            <ErrorBox error={trials.error} />
          ) : !trials.data!.arenas.length ? (
            <Empty>No trials recorded yet.</Empty>
          ) : (
            <table className="w-full text-sm" data-testid="ledger-table">
              <thead className="border-b border-line">
                <tr>
                  <th className="th">Arena</th>
                  <th className="th text-right">Trials</th>
                  <th className="th text-right">Holdout uses</th>
                  <th className="th">Sealed holdout from</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line/60">
                {trials.data!.arenas.map((a) => (
                  <tr key={`${a.symbol}|${a.timeframe}`} data-testid="ledger-row">
                    <td className="td font-medium">
                      {a.symbol} {a.timeframe}
                    </td>
                    <td className="td num text-right">{a.trials}</td>
                    <td className="td num text-right">{a.holdout_uses}</td>
                    <td className="td num">{day(a.holdout_from)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Holdout results" testId="holdout-card" bodyClass="p-0">
          {!holdouts.length ? (
            <Empty>No finalist has been scored on a holdout yet. Each is scored once; a failure is final.</Empty>
          ) : (
            <table className="w-full text-sm" data-testid="holdout-table">
              <thead className="border-b border-line">
                <tr>
                  <th className="th">Arena</th>
                  <th className="th">Finalist</th>
                  <th className="th">Result</th>
                  <th className="th">Scored</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line/60">
                {holdouts.map((t) => (
                  <tr key={t.id} data-testid="holdout-row" className="align-top">
                    <td className="td whitespace-nowrap font-medium">
                      {t.symbol} {t.timeframe}
                    </td>
                    <td className="td">
                      <div>{paramsText(t)}</div>
                      <div className="text-xs text-faint">{t.hypothesis}</div>
                    </td>
                    <td className="td space-y-1">
                      <Badge tone={t.status === "passed" ? "up" : t.status === "evaluating" ? "info" : "down"} testId="holdout-status">
                        {t.status}
                      </Badge>
                      <div>
                        <HoldoutResult t={t} />
                      </div>
                    </td>
                    <td className="td num whitespace-nowrap text-xs">{day(t.ts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
      <Card title="Reports" testId="reviews-card" bodyClass="space-y-3">
        {reviews.isLoading ? (
          <Loading />
        ) : reviews.error ? (
          <ErrorBox error={reviews.error} />
        ) : !reviews.data!.length ? (
          <Empty>No reviews yet. The weekly review runs on Saturdays while GOLD is closed.</Empty>
        ) : (
          reviews.data!.map((r) => <ReviewItem key={r.id} r={r} />)
        )}
      </Card>
    </div>
  );
}
