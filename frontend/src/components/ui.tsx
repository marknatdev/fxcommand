import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { Loader2, X } from "lucide-react";
import { forwardRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { SessionStatus, Side } from "../api/types";
import { cn, pnlClass, signed } from "../lib/format";

/* ------------------------------------------------------------------ Button */
type Variant = "primary" | "secondary" | "ghost" | "danger" | "success" | "warn";
const variants: Record<Variant, string> = {
  primary: "bg-accent text-bg hover:bg-cyan-300 font-semibold",
  secondary: "bg-panel-2 text-ink border border-line-2 hover:border-faint hover:bg-line",
  ghost: "text-dim hover:text-ink hover:bg-panel-2",
  danger: "bg-down/15 text-down border border-down/40 hover:bg-down/25",
  success: "bg-up/15 text-up border border-up/40 hover:bg-up/25",
  warn: "bg-warn/15 text-warn border border-warn/40 hover:bg-warn/25",
};

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean; icon?: ReactNode }
>(({ variant = "secondary", size = "md", loading, icon, className, children, disabled, ...rest }, ref) => (
  <button
    ref={ref}
    className={cn(
      "inline-flex items-center justify-center gap-1.5 rounded-md text-sm transition-colors disabled:pointer-events-none disabled:opacity-40 cursor-pointer",
      size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5",
      variants[variant],
      className,
    )}
    disabled={disabled || loading}
    {...rest}
  >
    {loading ? <Loader2 className="size-3.5 animate-spin" /> : icon}
    {children}
  </button>
));
Button.displayName = "Button";

/* -------------------------------------------------------------------- Card */
export function Card({
  title,
  actions,
  children,
  className,
  bodyClass,
  testId,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
  testId?: string;
}) {
  return (
    <section data-testid={testId} className={cn("min-w-0 rounded-xl border border-line bg-panel/80 shadow-[0_1px_0_0_rgba(255,255,255,0.03)_inset]", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={cn("p-4", bodyClass)}>{children}</div>
    </section>
  );
}

/* ------------------------------------------------------------------- Badge */
export function Badge({ children, tone = "neutral", className, testId }: { children: ReactNode; tone?: "neutral" | "up" | "down" | "warn" | "info" | "accent"; className?: string; testId?: string }) {
  const tones = {
    neutral: "bg-line/60 text-dim border-line-2",
    up: "bg-up/10 text-up border-up/30",
    down: "bg-down/10 text-down border-down/30",
    warn: "bg-warn/10 text-warn border-warn/30",
    info: "bg-info/10 text-info border-info/30",
    accent: "bg-accent/10 text-accent border-accent/30",
  };
  return (
    <span data-testid={testId} className={cn("inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium leading-none", tones[tone], className)}>
      {children}
    </span>
  );
}

const statusTone: Record<SessionStatus, "up" | "warn" | "neutral" | "down"> = {
  running: "up",
  paused: "warn",
  stopped: "neutral",
  interrupted: "down",
};

export function StatusBadge({ status }: { status: SessionStatus }) {
  return (
    <Badge tone={statusTone[status]} testId="session-status" className="uppercase tracking-wide">
      <span className={cn("size-1.5 rounded-full bg-current", status === "running" && "pulse-dot")} />
      {status}
    </Badge>
  );
}

export function SideBadge({ side }: { side: Side | string }) {
  return <Badge tone={side === "long" ? "up" : "down"} className="uppercase">{side === "long" ? "Buy" : "Sell"}</Badge>;
}

export function Pnl({ value, className, digits = 2 }: { value: number | null | undefined; className?: string; digits?: number }) {
  return <span className={cn("num", pnlClass(value), className)}>{signed(value, digits)}</span>;
}

/* --------------------------------------------------------------------- KPI */
export function Kpi({ label, value, sub, tone, testId, icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; testId?: string; icon?: ReactNode }) {
  return (
    <div data-testid={testId} className="min-w-0 rounded-xl border border-line bg-panel/80 px-4 py-3">
      <div className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-faint">
        {label}
        {icon && <span className="text-faint">{icon}</span>}
      </div>
      <div className={cn("num mt-1 text-xl font-semibold text-ink", tone)}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-dim">{sub}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ Layout */
export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 data-testid="page-title" className="text-2xl font-semibold tracking-tight text-ink">
          {title}
        </h1>
        {subtitle && <p className="mt-1 text-sm text-dim">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Empty({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center text-sm text-faint">
      {icon}
      {children}
    </div>
  );
}

export function Loading() {
  return (
    <div className="flex items-center justify-center py-16 text-faint">
      <Loader2 className="size-5 animate-spin" />
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  return <div className="rounded-lg border border-down/40 bg-down/10 px-4 py-3 text-sm text-down">{(error as Error)?.message ?? String(error)}</div>;
}

/* ------------------------------------------------------------------ Dialog */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  wide,
  testId,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  testId?: string;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" />
        <DialogPrimitive.Content
          data-testid={testId}
          aria-describedby={undefined}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-line-2 bg-panel shadow-2xl",
            wide ? "max-w-4xl" : "max-w-md",
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
            <div>
              <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
              {description && <DialogPrimitive.Description className="mt-1 text-sm text-dim">{description}</DialogPrimitive.Description>}
            </div>
            <DialogPrimitive.Close className="rounded p-1 text-faint hover:bg-panel-2 hover:text-ink" aria-label="Close">
              <X className="size-4" />
            </DialogPrimitive.Close>
          </div>
          {children && <div className="px-5 py-4">{children}</div>}
          {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  variant = "danger",
  onConfirm,
  children,
  testId,
  confirmDisabled,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  variant?: Variant;
  onConfirm: () => Promise<unknown> | void;
  children?: ReactNode;
  testId?: string;
  confirmDisabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      testId={testId}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant={variant}
            loading={busy}
            disabled={confirmDisabled}
            data-testid="confirm-button"
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm();
                onOpenChange(false);
              } catch {
                // the handler reported the failure (toast); keep the dialog open so the operator can retry
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

/* ------------------------------------------------------------------ Switch */
export function Switch({ checked, onChange, label, testId, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; testId?: string; disabled?: boolean }) {
  return (
    <label className={cn("inline-flex cursor-pointer items-center gap-2 text-sm text-ink", disabled && "opacity-50")}>
      <SwitchPrimitive.Root
        data-testid={testId}
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
        className="relative h-5 w-9 shrink-0 rounded-full border border-line-2 bg-bg transition data-[state=checked]:border-accent data-[state=checked]:bg-accent/30"
      >
        <SwitchPrimitive.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-dim transition data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-accent" />
      </SwitchPrimitive.Root>
      {label}
    </label>
  );
}

/* -------------------------------------------------------------------- Tabs */
export function Tabs({ value, onChange, tabs, children }: { value: string; onChange: (v: string) => void; tabs: { value: string; label: ReactNode }[]; children: ReactNode }) {
  return (
    <TabsPrimitive.Root value={value} onValueChange={onChange}>
      <TabsPrimitive.List className="mb-4 inline-flex max-w-full gap-1 overflow-x-auto rounded-lg border border-line bg-panel p-1">
        {tabs.map((t) => (
          <TabsPrimitive.Trigger
            key={t.value}
            value={t.value}
            data-testid={`tab-${t.value}`}
            className="rounded-md px-3 py-1.5 text-sm text-dim transition hover:text-ink data-[state=active]:bg-panel-2 data-[state=active]:text-ink data-[state=active]:shadow"
          >
            {t.label}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      {children}
    </TabsPrimitive.Root>
  );
}
export const TabPanel = TabsPrimitive.Content;

/* ------------------------------------------------------------------- Field */
export function Field({ label, children, hint, className }: { label: string; children: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <div className={className}>
      <label className="label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-faint">{hint}</p>}
    </div>
  );
}

export function ProgressBar({ value, max, tone = "accent", testId }: { value: number; max: number; tone?: "accent" | "down" | "warn" | "up"; testId?: string }) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  const color = { accent: "bg-accent", down: "bg-down", warn: "bg-warn", up: "bg-up" }[tone];
  return (
    <div data-testid={testId} className="h-2 w-full overflow-hidden rounded-full bg-line">
      <div className={cn("h-full rounded-full transition-all", color)} style={{ width: `${pct}%` }} />
    </div>
  );
}
