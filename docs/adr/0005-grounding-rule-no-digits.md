# The Research Desk's prose may contain no digits

The Verdict Card is rendered only from Trial result data. The Research Desk's chat text may not contain digits; any figure must be a placeholder such as `{{trial:42.profit_factor}}`, which the app fills from the stored result. Replies that contain raw digits are rejected and regenerated. This is stricter than Vibe-Trading's grounding gate, which checks figures after the fact. We chose it because it can be enforced mechanically, and an LLM that invents or rounds a profit factor is the cheapest way for the owner to be misled.
