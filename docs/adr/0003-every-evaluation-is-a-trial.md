# Every Strategy Spec run is a Trial, and every grid combination deflates

Every walk-forward run of a Strategy Spec that the Research Desk performs is a Trial. That includes exploratory runs the owner never looks at. Each Trial is recorded in the append-only Ledger and counts against its Idea Family's Trial Budget, which is 25 Trials and can be extended only by the owner.

The Deflated Sharpe does not use the Trial count as its N. Its N is the number of grid combinations evaluated across all of the Idea Family's Trials, and the variance of Sharpe is taken from those combinations' blind-year Sharpe ratios. A strategy found after 200 combinations therefore faces a higher bar than one found on the first try.

The desk-wide Trial count is shown for information only. If it deflated Verdicts, the bar would rise forever and nothing could ever pass. To stop the count from being reset by renaming an Idea, an Idea Family is fixed when its first Hypothesis is registered, and any new Spec with the same entry Block and symbol joins the existing family.

## Considered Options

- **Each grid combination is a Trial**: a single walk-forward of up to 50 combinations would exhaust the budget of 25.
- **Only whole runs count toward deflation**: this would hide the grid search from the Deflated Sharpe.
- **Desk-wide deflation**: honest in theory, but it ratchets the Pass Bar until no Hypothesis can ever pass.
