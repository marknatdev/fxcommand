import {
  Activity,
  BookOpen,
  Brain,
  CandlestickChart,
  FlaskConical,
  Gauge,
  LayoutDashboard,
  Octagon,
  PlugZap,
  ScrollText,
  Settings,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { cn, money, serverTime, signed } from "../lib/format";
import { Badge, Button, ConfirmDialog } from "./ui";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/sessions", label: "Sessions", icon: Activity },
  { to: "/symbols", label: "Symbols", icon: CandlestickChart },
  { to: "/strategies", label: "Strategies", icon: Brain },
  { to: "/learning", label: "Learning", icon: FlaskConical },
  { to: "/risk", label: "Risk", icon: ShieldAlert },
  { to: "/positions", label: "Positions", icon: Wallet },
  { to: "/history", label: "History & Journal", icon: BookOpen },
  { to: "/account", label: "Account", icon: PlugZap },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function ModeBadge() {
  const { snapshot } = useLive();
  if (!snapshot) return <Badge>…</Badge>;
  if (snapshot.mode === "sim") return <Badge tone="info" testId="mode-badge">SIM</Badge>;
  const demo = snapshot.account?.is_demo ?? true;
  return (
    <Badge tone={demo ? "warn" : "down"} testId="mode-badge" className={cn(!demo && "font-bold")}>
      MT5 {demo ? "DEMO" : "LIVE"}
    </Badge>
  );
}

function TopBar() {
  const { snapshot, wsConnected } = useLive();
  const [killOpen, setKillOpen] = useState(false);
  const acct = snapshot?.account;
  const running = snapshot?.sessions.filter((s) => s.status === "running").length ?? 0;
  const owned = snapshot?.positions.filter((p) => p.owned).length ?? 0;
  const online = wsConnected && snapshot?.connected;
  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-4 border-b border-line bg-bg/85 px-5 backdrop-blur">
      <div className="flex items-center gap-2">
        <ModeBadge />
        <span data-testid="connection" className={cn("flex items-center gap-1.5 text-xs", online ? "text-up" : "text-down")}>
          <span className={cn("size-2 rounded-full bg-current", online && "pulse-dot")} />
          {online ? "Connected" : wsConnected ? "Broker offline" : "Dashboard offline"}
        </span>
      </div>
      <div className="hidden items-center gap-5 text-xs text-dim md:flex">
        <span>
          Server <span className="num text-ink" data-testid="server-time">{serverTime(snapshot?.server_time)}</span>
        </span>
        <span>
          Equity <span className="num text-ink">{money(acct?.equity, acct?.currency)}</span>
        </span>
        <span>
          Today{" "}
          <span data-testid="topbar-day-pnl" className={cn("num", (snapshot?.day_pnl ?? 0) >= 0 ? "text-up" : "text-down")}>
            {signed(snapshot?.day_pnl)}
          </span>
        </span>
        <span>
          Running <span className="num text-ink">{running}</span> · Positions <span className="num text-ink">{owned}</span>
        </span>
      </div>
      <div className="ml-auto">
        <Button variant="danger" size="sm" icon={<Octagon className="size-3.5" />} onClick={() => setKillOpen(true)} data-testid="kill-switch">
          Kill switch
        </Button>
      </div>
      <ConfirmDialog
        open={killOpen}
        onOpenChange={setKillOpen}
        testId="kill-dialog"
        title="Activate kill switch?"
        confirmLabel="Stop everything & close positions"
        description="Stops every Session and closes every position opened by FXCommand. Positions opened manually or by other EAs are never touched."
        onConfirm={async () => {
          const r = await api.killSwitch();
          toast.error(`Kill switch: stopped ${r.stopped_sessions.length} sessions, closed ${r.closed_positions} positions`);
        }}
      />
    </header>
  );
}

export function Layout() {
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-panel/60 lg:flex">
        <div className="flex h-14 items-center gap-2 border-b border-line px-5">
          <div className="grid size-8 place-items-center rounded-lg bg-accent/15 text-accent">
            <Gauge className="size-4.5" />
          </div>
          <div>
            <div className="text-sm font-bold tracking-tight">FXCommand</div>
            <div className="text-[10px] uppercase tracking-widest text-faint">MT5 autonomous</div>
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 overflow-y-auto p-3" data-testid="sidebar">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              data-testid={`nav-${label.split(" ")[0].toLowerCase()}`}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition",
                  isActive ? "bg-accent/10 text-accent" : "text-dim hover:bg-panel-2 hover:text-ink",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line p-4 text-[11px] leading-relaxed text-faint">
          Times shown in broker server time. Only positions carrying a FXCommand magic number are ever managed.
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <nav className="flex gap-1 overflow-x-auto border-b border-line px-3 py-2 lg:hidden">
          {NAV.map(({ to, label, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => cn("whitespace-nowrap rounded px-2.5 py-1 text-xs", isActive ? "bg-accent/10 text-accent" : "text-dim")}>
              {label}
            </NavLink>
          ))}
        </nav>
        <main className="mx-auto w-full max-w-[1500px] flex-1 p-5 lg:p-7">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
