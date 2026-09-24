# FXCommand

Autonomous multi-symbol trading on MetaTrader 5, operated from a single dashboard.

## Language

**Account**:
The MT5 trading account the terminal is currently logged into. Either demo or live.
_Avoid_: login, profile

**Live-enabled**:
An operator-set flag on an Account that permits trading when the Account is live. A live Account without it never receives orders.

**Broker**:
The thing that quotes prices and executes orders for the Account — the real MT5 terminal, or the simulated market.
_Avoid_: exchange, gateway

**Symbol**:
A tradable instrument, named exactly as the Broker names it (e.g. `EURUSD`, `GOLD`).
_Avoid_: pair, ticker, instrument

**Session**:
A named, configured run of the engine against one Account, trading a set of Assignments. It is started, paused and stopped as a unit, and owns its own P&L, positions and Journal.
_Avoid_: bot, run, market session (London/New York — see Trading Window)

**Assignment**:
One Symbol inside a Session, bound to one Strategy (with its parameters), one Timeframe and one Risk Profile. A Symbol appears at most once per Session.
_Avoid_: leg, slot, pair config

**Timeframe**:
The bar size an Assignment's Strategy evaluates on (M1, M5, M15, M30, H1, H4, D1).

**Strategy**:
A rule that reads closed bars and produces a Signal. It knows nothing about money, the Account or the Broker.
_Avoid_: algo, model, EA

**Signal**:
A Strategy's output for one closed bar: go long, go short, exit, or do nothing — with stop-loss and take-profit distances and a human-readable reason.

**Risk Profile**:
A named set of per-trade rules: risk percent per trade, maximum spread, and optional breakeven and trailing-stop behaviour.

**Risk Gate**:
The check every Signal passes before becoming an order. It sizes the position and either approves it or rejects it with a reason.

**Risk Limits**:
Caps that apply across trades: maximum open positions and daily loss limit, at Session level and globally.

**Trading Window**:
The hours of the Broker's server time during which a Session may open new positions.
_Avoid_: market session, trading hours

**Magic Number**:
The unique number stamped on every order a Session sends. It is how the system tells which positions belong to which Session, and which positions it owns at all.

**Position**:
An open trade on the Account. An **owned position** carries a Magic Number of some Session; any other position is **foreign** and is never touched.

**Pause**:
A Session state in which no new positions are opened, but existing ones are still managed.

**Stop**:
Ending a Session's run. The operator chooses to either leave its positions (protected by their server-side stop-loss / take-profit) or close them.

**Interrupted**:
The state of a Session that was running when the application shut down. It must be restarted by the operator; on restart it re-adopts its positions by Magic Number.

**Auto-stop**:
A Stop triggered by the engine itself, when a Risk Limit (e.g. daily loss) is hit.

**Kill Switch**:
The operator's emergency action: stop every Session and close every owned position.

**Journal**:
The permanent, ordered record of everything a Session did and why — Signals, Risk Gate decisions (including rejections), orders, fills, closes, state changes and alerts.
_Avoid_: log (Logs are the application's technical diagnostics)

**Alert**:
A Journal entry that demands the operator's attention (Auto-stop, rejected order, lost Broker connection).

### Learning

**Arena**:
A Symbol on one Timeframe, seen as a place where Candidates compete. Learning is kept per Arena and outlives any Session.
_Avoid_: market, slot

**Candidate**:
A Strategy together with a full set of its parameters. Two Candidates with the same Strategy and parameters are the same Candidate.
_Avoid_: config, variant, model

**Champion**:
The Candidate an Assignment actually trades with. Every change of Champion is a new numbered **Champion Version**.

**Challenger**:
A Candidate that Shadow Trades in an Arena alongside the Champion, hoping to replace it.

**Shadow Trade**:
A paper trade taken by a Candidate on the real closed bars of its Arena under the same rules as live trading, but never sent to the Broker. The Champion Shadow Trades too, so Champion and Challengers are compared like for like.
_Avoid_: paper trade (outside this meaning), virtual trade

**R**:
A trade's result divided by the amount it risked at entry (distance to its initial stop-loss). +2R means it won twice what it risked. Learning measures everything in R so results are independent of account size.

**Backtest**:
Replaying a Candidate over historical bars under the Shadow Trade rules. A **Walk-forward** Backtest scores a Candidate only on bars that came after the bars it was chosen on (out-of-sample).

**Optimizer Run**:
One search for better Candidates in an Arena: generates Candidates, Walk-forward Backtests them, and hands the best to the Arena as Challengers.

**Promotion**:
Making a Challenger the Champion of an Assignment. A Promotion is first **pending** and takes effect at the first bar close where the Assignment holds no position — or at the moment the old Champion closes its position (exit or reversal), in which case the new Champion decides from the next bar. **Auto-promotion** happens without the operator only on demo or simulated accounts, and only when every **Guardrail** passes.

**Guardrail**:
One of the conditions a Challenger must meet to be promotable: enough Shadow Trades, beats the Champion on the same bars, better Walk-forward score, robust to small parameter changes, drawdown not materially worse.

**Rollback**:
Returning an Assignment to its previous Champion Version — by the operator, or automatically when a Promotion underperforms in live trading.

**Signal Filter**:
A model, learned from closed trades and Shadow Trades, that estimates how likely a Signal is to win. It first only **observes**; once it has enough evidence it **blocks** unlikely Signals, and it switches itself off if blocked Signals turn out to do better than taken ones.

### Going live

**Execution Mode**:
How a Session's orders are carried out. **Broker** sends them to the Account. **Paper** runs the whole engine on the Broker's real prices but fills orders in a local simulated book; nothing reaches the Account.
_Avoid_: dry run, simulation (the simulated market is the sim Broker, a different thing)

**Paper Position**:
A Position held in the Paper book of a Paper Session. It has no server-side stop-loss, so it is always closed when its Session stops, and is settled from the bars missed if the application was down.

**Order Outcome**:
What the Broker's answer to an order means: **filled**, **not executed** (nothing happened; one re-gated retry is allowed), or **uncertain** (a position may or may not exist; never retried — the Account's positions decide).

**Orphan**:
An owned Position (it carries a Session's Magic Number) that the Journal has no record of opening — typically left by an uncertain Order Outcome. It is adopted on the next engine pass.

**Pinned Login**:
The Account login a Session started on. If the terminal is logged into a different Account while the Session is active, the Session becomes Interrupted.

**Live Caps**:
Hard limits that apply only to Broker-mode Sessions on a live Account, above any Risk Profile: a ceiling on risk percent per trade and a maximum volume per order.

**Equity Floor**:
An equity level for a live Account below which the engine fires the Kill Switch and refuses to start Sessions until the operator resets it.

**Pre-flight Check**:
The list of conditions checked before a Session starts (terminal, Account, Symbols, notifier, engine health). Blocking checks must pass before a live Session may start; on demo they are warnings.

**Weekend Close**:
An optional per-Session rule that closes its positions at a set Friday server time and opens nothing new until the market reopens.

**Notifier**:
Where Alerts are delivered outside the application (Telegram), so the operator hears about them when the dashboard is not open.

## Relationships

- An **Account** has many **Sessions**; a **Session** belongs to exactly one **Account**.
- A **Session** has one or more **Assignments**; each **Assignment** has exactly one **Symbol**, **Strategy**, **Timeframe** and **Risk Profile**.
- A **Symbol** can be in at most one *running or paused* **Session** at a time.
- A **Session** has exactly one **Magic Number**, never reused.
- An **Assignment** holds at most one open **Position** at a time.
- Every **Signal** passes the **Signal Filter** (once it is active) and then the **Risk Gate**; only approved ones become orders.
- An **Assignment** trades in exactly one **Arena** and has exactly one **Champion**; an **Arena** has at most three **Challengers**.
- A **Session** has exactly one **Execution Mode**; Paper and Broker Sessions follow the same Symbol rule.
- A **Session** is pinned to one login while active; **Live Caps** and the **Equity Floor** apply only when that Account is live.
- A **Promotion** or **Rollback** creates a new **Champion Version**; only the operator may promote on a live **Account**.

## Example dialogue

> **Dev:** "The EURUSD **Assignment** in the London **Session** didn't trade today — was it a bug?"
> **Operator:** "Check the **Journal**: the **Strategy** gave a long **Signal** at 09:00, but the **Risk Gate** rejected it — spread was above the **Risk Profile** maximum."
> **Dev:** "And the GBPUSD position still open after I hit **Stop**?"
> **Operator:** "I chose to leave positions, so its server-side stop-loss still protects it. The **Kill Switch** would have closed it."

## Flagged ambiguities

- "Session" was used both for a market session (London/New York) and for an engine run — resolved: **Session** is the engine run; market hours are a **Trading Window**.
- "Log" vs "Journal" — resolved: **Journal** is the domain record of trading decisions; logs are technical diagnostics.
