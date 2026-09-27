# One Magic Number per Assignment; an Arena runs once

Supersedes ADR 0003.

Each Assignment has its own never-reused Magic Number, so the same Symbol can trade on two Timeframes at once. For example, GOLD Trend on H4 and GOLD Reopen Drift on H1 each hold a GOLD position on the hedging Account (ADR 0006). The magic belongs to an *identity* `(Session, Symbol, Strategy, Timeframe)` kept in its own table, because Assignment rows are recreated on every edit. An identity always gets its magic back, and its row is kept after the Session is deleted (under the negated Session id, since SQLite can reuse the id), so no magic is ever reused.

An Arena `(Symbol, Timeframe)` appears at most once per Session and in at most one running Session. Learning keys by Arena, and per-Session Learning slots and pending changes key by `(Session, Symbol, Timeframe)`, so nothing collides.

**Ownership.** A position belongs to a Session if it carries any of the Session's magics. Every magic the Session has ever had is included. The Kill Switch, stop-and-close, global counts, Live Caps, Orphan adoption and Paper routing all use that set. Within the Session, the Assignment that manages a position is:

1. the Assignment whose magic the position carries, if the Symbol matches;
2. otherwise, the Assignment in the Arena of the identity the magic belongs to. The Strategy was changed while the position stayed open, for example after "stop and leave positions", an edit, then a restart;
3. otherwise, for a position carrying the Session's own magic with another Symbol, the Session's first Assignment on that Symbol. Such a position was opened before this ADR.

Rule 2 is why the Strategy in the identity (decision D37) is safe. Promotions and Rollbacks also change the Strategy, but they only apply when the Assignment is flat.

**Migration.** A Session from before this ADR keeps its magic. The magic moves to its first Assignment, and the others get new ones. Positions those Sessions opened all carry the old magic, and rule 3 hands each to the Assignment of its Symbol. Learning slots and pending changes get the timeframe of the Session's single Assignment on that Symbol.

**Paper routing.** The `PaperRouter` lists every magic of every Paper Session. The engine still refuses, in the same broker call, a Paper entry whose Assignment magic the router does not list (ADR 0007). The list is refreshed after a Promotion, because a new Strategy means a new identity.

## Considered Options

- Keep one magic per Session (ADR 0003). Two GOLD strategies would then need two Sessions and two Symbols. Rejected: the GOLD pair trades one Symbol.
- Key the identity by `(Session, Symbol, Timeframe)` only. Then a strategy change keeps the magic. Rejected in favour of D37 plus rule 2: a changed Strategy is a different thing to the operator and in trade history, and rule 2 keeps its open position managed.
- Magic per Assignment row id. Rejected: the id changes on every edit, so positions would be orphaned by an edit.
