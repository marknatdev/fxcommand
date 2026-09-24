import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Save, Send } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import type { NotifySettings } from "../api/types";
import { wallTime } from "../lib/format";
import { Badge, Button, Card, Field, Switch } from "./ui";

/** Telegram alerts. The bot token is write-only: the API only ever returns it masked. */
export function NotifyCard({ notify }: { notify: NotifySettings }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [chat, setChat] = useState(notify.telegram_chat_id);
  const [enabled, setEnabled] = useState(notify.enabled);
  const [busy, setBusy] = useState(false);
  const outbox = useQuery({ queryKey: ["outbox"], queryFn: api.outbox, refetchInterval: 5000 });
  const save = async () => {
    setBusy(true);
    try {
      await api.saveNotify({ enabled, telegram_chat_id: chat, ...(token ? { telegram_token: token } : {}) });
      setToken("");
      toast.success("Notification settings saved");
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Bell className="size-4" /> Alerts to Telegram
        </span>
      }
      testId="notify-card"
    >
      <p className="mb-3 text-sm text-dim">
        Kill Switch, Auto-stops, Equity Floor, failed or uncertain orders, a broker disconnect longer than 60 s, account switches, restarts with Interrupted
        Sessions and a daily summary. Create a bot with @BotFather, send it a message, then paste its token and your chat id here.{" "}
        {notify.configured ? <Badge tone="up" testId="notify-status">configured</Badge> : <Badge tone="warn" testId="notify-status">not configured</Badge>}
      </p>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Bot token" hint={notify.telegram_token ? `saved: ${notify.telegram_token} — leave empty to keep` : "stored in the local database only"}>
          <input type="password" autoComplete="off" className="field num" value={token} onChange={(e) => setToken(e.target.value)} placeholder={notify.telegram_token || "123456:ABC…"} data-testid="notify-token" />
        </Field>
        <Field label="Chat id">
          <input className="field num" value={chat} onChange={(e) => setChat(e.target.value)} data-testid="notify-chat" />
        </Field>
        <div className="flex items-end pb-1.5">
          <Switch checked={enabled} onChange={setEnabled} label="Send alerts" testId="notify-enabled" />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" icon={<Save className="size-4" />} loading={busy} onClick={save} data-testid="notify-save">
          Save
        </Button>
        <Button
          icon={<Send className="size-4" />}
          data-testid="notify-test"
          onClick={async () => {
            try {
              await api.testNotify();
              toast.success("Test message sent");
            } catch (e) {
              toast.error((e as Error).message);
            }
            outbox.refetch();
          }}
        >
          Send test
        </Button>
      </div>
      <div className="mt-4">
        <div className="mb-1 text-xs uppercase tracking-wide text-faint">Recent notifications</div>
        {outbox.data?.length ? (
          <ul className="max-h-56 divide-y divide-line/60 overflow-y-auto rounded border border-line text-xs" data-testid="outbox">
            {outbox.data.map((o, i) => (
              <li key={i} className="flex gap-2 px-2.5 py-1.5">
                <span className="num shrink-0 text-faint">{wallTime(o.wall)}</span>
                <Badge tone={o.status === "sent" ? "up" : o.status.startsWith("failed") ? "down" : "neutral"}>{o.status}</Badge>
                <span className="min-w-0 truncate text-dim" title={o.text}>
                  {o.text.replace(/\n/g, " · ")}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-dim">Nothing sent yet.</p>
        )}
      </div>
    </Card>
  );
}
