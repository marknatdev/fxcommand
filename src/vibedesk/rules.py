"""The version of the trade rules a Trial is judged under.

A Holdout re-simulates its Trial's Combination on the day it runs. If a rule changed in between,
the Trial and its Holdout would be judged by different rules. So every Trial stores
RULES_VERSION, and a Holdout whose latest Trial carries another version is refused: the Idea
needs a new Trial under today's rules first.

Bump RULES_VERSION, and note the change below, for anything that can alter the trades or the
Verdict of an existing Spec on the same bars and Cost Model:
- the engine's fills and exits;
- what a Block means;
- walk-forward choice and stitching;
- the Coin-Flip Baseline;
- the Pass Bar, Prosecutor or Holdout thresholds.
A new Block, a refactor or a change to the app is no bump. tests/test_rules_version.py
fingerprints each part of the rules and names the one that changed without a bump.

Versions:
1. 2026-10-23: the M2 rules, with opposite-signal stop and reverse and time filters that judge
   the entry bar.
2. 2026-10-23: the Coin-Flip Baseline's seed comes from what a Trial judges (its Combinations,
   its bars and M1 bars, and this version), not from the Trial's number, so re-running an
   unchanged Spec on unchanged data draws the same runs instead of re-rolling its luck.
3. 2026-10-24: XM server clock switched to European EET/EEST (Europe/Athens, CCC-85). In the
   weeks between the US and European daylight-saving switches, server days and hours shift,
   altering day_of_week filters, swap nights, daily returns and baseline hours on unchanged
   bars. Old-clock Trials in the Ledger stay distinguishable.
"""

RULES_VERSION = 3
