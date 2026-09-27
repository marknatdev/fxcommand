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
One Symbol inside a Session, bound to one Strategy (with its parameters), one Timeframe and one Risk Profile. A Symbol appears at most once per Timeframe in a Session, so GOLD may trade on H4 and H1 side by side.
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
The unique number stamped on every order an Assignment sends. Each Assignment has its own, kept for the same Session, Symbol, Strategy and Timeframe across edits and never reused. A Session's positions are those carrying any of its Assignments' Magic Numbers, and a position carrying none of them is not the system's.

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

**Paper Account**:
The notional balance every Paper Session sizes its trades from and measures its limits against. It is separate from the real Account, and resetting it starts a new epoch that keeps the old records.

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

### Strategy evidence

**Strategy Family**:
A group of Strategies that Learning may search across. A Candidate is only ever drawn from its Champion's family, so the classic Strategies and the GOLD Strategies never replace each other.

**Trading Hours**:
When the Broker quotes a Symbol, per weekday, in server time (GOLD is shut 00:00–01:00 every day and all weekend). Different from the Trading Window, which is the operator's choice of when a Session may open positions.

**Next Tradable Time**:
The first moment at or after a given time when both the Trading Hours and the Trading Window are open. A signal that arrives while either is shut is acted on then.

**Fill Window**:
How long after the Next Tradable Time a Strategy's entry may still be sent. Past it the entry is dropped, because the edge it was meant to capture has gone.

**Cost Model**:
The costs a Backtest charges a trade: spread, slippage and overnight swap, scaled to the price at the time so older, cheaper years are not overcharged.

**Pending Entry**:
A Signal that could not become an order yet because the market or the Trading Window was shut, or the spread too wide. It waits and is sent at the Next Tradable Time, and is dropped when its Fill Window ends or the Session stops. A strategy exit that the shut market refuses waits the same way.
_Avoid_: queued order, pending order (MT5's resting limit/stop orders)

**Cost Check**:
The cost of one round trip (spread and slippage) as a share of an Assignment's stop, in R. An Assignment above the limit cannot start unless the operator overrides it. Swap is shown beside it but never blocks.
_Avoid_: spread filter (that is the Risk Profile's per-order maximum spread)

**Evidence Run**:
A read-only Backtest of one Candidate on an Arena's Trusted History from the connected feed, priced at twice the typical spread and run with an Assignment's exit rules, Trading Window and Weekend Close. Evidence belongs to exactly those settings: an Assignment whose settings differ has no matching Evidence.
_Avoid_: backtest result (a Backtest is also what an Optimizer Run does many times)

**Scorecard**:
Per Arena, the Paper Account's record set against the range the Evidence says a record of that many trades should fall in, beside what one minimum lot would risk on the real Account. It advises the operator; it never blocks.
_Avoid_: graduation check (nothing is checked or gated)

**Strategy Review**:
A weekly research pass by Claude over the week's trades, Evidence and Scorecards. It may submit Challengers and reports, and propose new Strategy code only as pull requests; it never controls Sessions, risk or money.

**Sealed Holdout**:
The most recent year of an Arena's history (three months for M1/M5), rolling forward monthly, that research never sees. A finalist is scored on it once; a failure there is final.
_Avoid_: test set, out-of-sample (the Optimizer's out-of-sample part is research data)

**Trusted History**:
The part of a Symbol's history whose bars are real quotes, and so may be judged: Evidence Runs and research count only trades signalled inside it. Older bars only warm indicators up. BTCUSD's starts in 2018, because its earlier intraday bars are daily bars copied down.
_Avoid_: full history (the feed's full history includes untrusted bars)

**Research Snapshot**:
An Arena's history up to the start of its Sealed Holdout, exported read-only for research.

**Trial Ledger**:
The record of every hypothesis tested on an Arena, whatever its result, and every use of its Sealed Holdout. The more trials an Arena has seen, the higher the bar a Challenger from the Strategy Review must clear.

## Relationships

- An **Account** has many **Sessions**; a **Session** belongs to exactly one **Account**.
- A **Session** has one or more **Assignments**; each **Assignment** has exactly one **Symbol**, **Strategy**, **Timeframe** and **Risk Profile**.
- An **Arena** (a Symbol on one Timeframe) can be in at most one *running or paused* **Session** at a time; the same Symbol on another Timeframe can run elsewhere.
- An **Assignment** has exactly one **Magic Number**, never reused; a **Session** has one per **Assignment**.
- An **Assignment** holds at most one open **Position** at a time.
- Every **Signal** passes the **Signal Filter** (once it is active) and then the **Risk Gate**; only approved ones become orders.
- An **Assignment** trades in exactly one **Arena** and has exactly one **Champion**; an **Arena** has at most three **Challengers**.
- A **Session** has exactly one **Execution Mode**; Paper and Broker Sessions follow the same Symbol rule.
- A **Session** is pinned to one login while active; **Live Caps** and the **Equity Floor** apply only when that Account is live.
- A **Promotion** or **Rollback** creates a new **Champion Version**; only the operator may promote on a live **Account**.
- An **Evidence Run** tests one **Candidate** on one **Arena** under one set of settings; an **Assignment** has matching Evidence only when all of them agree.

## Example dialogue

> **Dev:** "The EURUSD **Assignment** in the London **Session** didn't trade today — was it a bug?"
> **Operator:** "Check the **Journal**: the **Strategy** gave a long **Signal** at 09:00, but the **Risk Gate** rejected it — spread was above the **Risk Profile** maximum."
> **Dev:** "And the GBPUSD position still open after I hit **Stop**?"
> **Operator:** "I chose to leave positions, so its server-side stop-loss still protects it. The **Kill Switch** would have closed it."

## Flagged ambiguities

- "Session" was used both for a market session (London/New York) and for an engine run — resolved: **Session** is the engine run; market hours are a **Trading Window**.
- "Log" vs "Journal" — resolved: **Journal** is the domain record of trading decisions; logs are technical diagnostics.
