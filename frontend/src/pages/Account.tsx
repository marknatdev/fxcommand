import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlugZap, RefreshCw, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import { ModeBadge } from "../components/Layout";
import { Badge, Button, Card, ConfirmDialog, Dialog, ErrorBox, Field, Kpi, Loading, PageHeader, Switch } from "../components/ui";
import { money, serverTime } from "../lib/format";

export default function Account() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["account"], queryFn: api.account, refetchInterval: 3000 });
  const [enableOpen, setEnableOpen] = useState(false);
  const [disableOpen, setDisableOpen] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const a = q.data!;
  const acct = a.account;
  const live = acct && !acct.is_demo;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Account & Connection"
        subtitle={a.mode === "mt5" ? "Attached to the MetaTrader 5 terminal that is already running and logged in. No credentials are stored." : "Running against the simulated broker (BROKER=sim)."}
        actions={
          <Button
            icon={<RefreshCw className="size-4" />}
            loading={reconnecting}
            data-testid="reconnect"
            onClick={async () => {
              setReconnecting(true);
              try {
                await api.reconnect();
                toast.success("Reconnected");
                qc.invalidateQueries();
              } catch (e) {
                toast.error((e as Error).message);
              } finally {
                setReconnecting(false);
              }
            }}
          >
            Reconnect
          </Button>
        }
      />
      <div className="grid gap-5 xl:grid-cols-3">
        <Card
          title={
            <span className="flex items-center gap-2">
              <PlugZap className="size-4" /> Connection
            </span>
          }
          testId="connection-card"
        >
          <dl className="space-y-2.5 text-sm">
            <div className="flex justify-between">
              <dt className="text-dim">Broker mode</dt>
              <dd>
                <ModeBadge />
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-dim">Status</dt>
              <dd data-testid="account-connected">{a.connected ? <Badge tone="up">connected</Badge> : <Badge tone="down">disconnected</Badge>}</dd>
            </div>
            {a.last_error && <div className="rounded border border-down/40 bg-down/10 p-2 text-xs text-down">{a.last_error}</div>}
            <div className="flex justify-between">
              <dt className="text-dim">Server time</dt>
              <dd className="num">{serverTime(a.server_time)}</dd>
            </div>
            {a.terminal_path && (
              <div className="flex justify-between gap-3">
                <dt className="text-dim">Terminal</dt>
                <dd className="num truncate text-xs">{a.terminal_path}</dd>
              </div>
            )}
            <div className="flex justify-between gap-3">
              <dt className="text-dim">Database</dt>
              <dd className="num truncate text-xs" title={a.db}>
                {a.db.replace("sqlite:///", "")}
              </dd>
            </div>
          </dl>
        </Card>
        <Card title="Account" className="xl:col-span-2" testId="account-card">
          {acct ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-lg font-semibold" data-testid="account-login">
                  {acct.login}
                </span>
                <span className="text-dim">
                  {acct.name} · {acct.server} · {acct.company}
                </span>
                {acct.is_demo ? <Badge tone="info">DEMO</Badge> : <Badge tone="down">LIVE</Badge>}
                {!acct.trade_allowed && <Badge tone="warn">trading disabled in terminal</Badge>}
              </div>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Kpi label="Balance" value={money(acct.balance)} sub={acct.currency} />
                <Kpi label="Equity" value={money(acct.equity)} sub={acct.currency} />
                <Kpi label="Free margin" value={money(acct.margin_free)} sub={`used ${money(acct.margin)}`} />
                <Kpi label="Leverage" value={`1:${acct.leverage}`} />
              </div>
            </div>
          ) : (
            <p className="text-sm text-dim">No account — is the terminal running and logged in?</p>
          )}
        </Card>
      </div>

      <Card
        title={
          <span className="flex items-center gap-2">
            <ShieldAlert className="size-4 text-down" /> Live trading
          </span>
        }
        testId="live-card"
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="max-w-2xl text-sm text-dim">
            {live ? (
              <>
                This account is <b className="text-down">LIVE</b>. The engine refuses to start Sessions or send orders on a live account unless you enable it here.
              </>
            ) : (
              <>Demo and simulated accounts can always trade. If you connect a live account, it stays blocked until enabled here.</>
            )}
          </div>
          <Switch
            checked={a.live_enabled}
            testId="live-enabled"
            label={a.live_enabled ? "Live trading enabled" : "Live trading disabled"}
            onChange={(v) => {
              setErr(null);
              setConfirm("");
              if (v) setEnableOpen(true);
              else setDisableOpen(true);
            }}
          />
        </div>
      </Card>

      <Dialog
        open={enableOpen}
        onOpenChange={setEnableOpen}
        title="Enable live trading?"
        testId="live-dialog"
        description="Real money will be at risk. Type the account number to confirm."
        footer={
          <>
            <Button variant="ghost" onClick={() => setEnableOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              data-testid="confirm-live"
              onClick={async () => {
                try {
                  await api.setLiveEnabled(true, confirm);
                  toast.warning("Live trading enabled");
                  setEnableOpen(false);
                  qc.invalidateQueries({ queryKey: ["account"] });
                } catch (e) {
                  setErr((e as Error).message);
                }
              }}
            >
              Enable live trading
            </Button>
          </>
        }
      >
        <Field label={`Account number (${acct?.login ?? ""})`}>
          <input className="field num" value={confirm} onChange={(e) => setConfirm(e.target.value)} data-testid="live-confirm-input" autoFocus />
        </Field>
        {err && <p className="mt-2 text-sm text-down">{err}</p>}
      </Dialog>
      <ConfirmDialog
        open={disableOpen}
        onOpenChange={setDisableOpen}
        variant="primary"
        title="Disable live trading?"
        description="Sessions on a live account will be refused new orders."
        confirmLabel="Disable"
        onConfirm={async () => {
          await api.setLiveEnabled(false, "");
          qc.invalidateQueries({ queryKey: ["account"] });
        }}
      />
    </div>
  );
}
