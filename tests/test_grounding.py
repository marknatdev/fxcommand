"""Tests for Grounding Rule validator and placeholder rendering (spec §7.3, §9.4, ADR 0005)."""

import pytest

from vibedesk.desk.grounding import (
    KNOWN_PLACEHOLDER_KINDS,
    PLACEHOLDER_PATTERN,
    PLACEHOLDER_REGEX,
    GroundingError,
    Placeholder,
    default_known_tokens,
    render,
    validate,
)


def test_prose_with_digits_is_rejected():
    """Raw digits in assistant prose must be rejected."""
    bad_examples = [
        "The profit factor was 1.5 across eight years.",
        "The strategy executed 100 trades blind.",
        "We saw a Sharpe ratio of 0.95 in backtesting.",
        "In 2024 the strategy made profit.",
        "The win rate was 60%.",
        "Stop loss distance was 50 points.",
        "A loss of -0.80 was recorded.",
    ]
    for text in bad_examples:
        problems = validate(text)
        assert len(problems) > 0, f"Expected problems for {text!r}, got empty list"
        assert any("forbidden digit" in p for p in problems)


def test_prose_without_digits_passes():
    """Assistant prose with no digits passes without problems."""
    good_examples = [
        "The strategy showed no edge in the walk-forward evaluation.",
        "Every blind year was profitable.",
        "The closest miss was the trade count check.",
        "We recommend loosening the entry conditions.",
    ]
    for text in good_examples:
        assert validate(text) == []


def test_universe_symbols_pass_validation():
    """All symbols from the universe pass validation."""
    text = "We evaluated GOLD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, and NZDUSD."
    assert validate(text) == []


def test_timeframes_with_digits_pass_validation():
    """Whitelisted timeframes (M1, M15, H1, H4, D1) containing digits pass."""
    text = "We tested on M1, M15, H1, H4, and D1 charts."
    assert validate(text) == []


def test_block_names_pass_validation():
    """Whitelisted Block names pass validation."""
    text = "Using donchian_breakout with ma_slope filter, atr_stop stop, and fixed_risk sizing."
    assert validate(text) == []


def test_whitelisted_tokens_with_punctuation_pass():
    """Whitelisted tokens surrounded by common punctuation pass."""
    assert validate("Tested on (M15).") == []
    assert validate("Symbols: GOLD; timeframes: H1, D1.") == []
    assert validate("Comparing H1/H4 timeframes.") == []
    assert validate("Range 'M1' through 'D1'.") == []


def test_custom_known_tokens():
    """Custom known_tokens can whitelist additional tokens with or without digits."""
    # XAUUSD is not in XM's SYMBOLS (XM uses GOLD), but custom token whitelist permits it
    assert validate("Tested XAUUSD on M15.", known_tokens=["XAUUSD", "M15"]) == []

    # Block name with digits
    assert validate("Used custom_ma_200 on H1.", known_tokens=["custom_ma_200", "H1"]) == []

    # Without the whitelist entry, the digits in custom_ma_200 must fail
    problems = validate("Used custom_ma_200 on H1.")
    assert len(problems) > 0
    assert any("custom_ma_200" in p or "forbidden digit" in p for p in problems)


def test_digits_glued_to_words_are_rejected():
    """Digits glued to words or units (20x, 1.5R) are rejected."""
    glued_examples = [
        "The return was 20x.",
        "Risk was 1.5R per trade.",
        "Leverage was 100x.",
        "Distance of 2R was hit.",
        "M150 is not a timeframe.",
        "D10 is invalid.",
        "GOLD1 is not a valid symbol.",
        "2M15 is malformed.",
        "donchian_breakout2 has a trailing digit.",
    ]
    for text in glued_examples:
        problems = validate(text)
        assert len(problems) > 0, f"Expected problems for {text!r}"
        assert any("forbidden digit" in p for p in problems)


def test_valid_placeholders_pass_validation():
    """Valid placeholders with numbers inside pass validation."""
    examples = [
        "Profit factor was {{trial:42.profit_factor}}.",
        "Entry period was {{spec:12.period}}.",
        "Hypothesis symbol is {{hyp:3.symbol}}.",
        "Holdout started on {{date:2025-01-01}}.",
        "Time was {{date:2024-10-23T12:00:00Z}}.",
        (
            "Hypothesis {{hyp:1.symbol}} on {{date:2024-01-01}} reached "
            "{{trial:99.profit_factor}} with {{spec:5.atr_stop}}."
        ),
    ]
    for text in examples:
        assert validate(text) == [], f"Expected no problems for {text!r}"


def test_unknown_placeholder_kind_fails_validation():
    """Placeholders with unknown kinds fail validation even without digits."""
    problems1 = validate("Result: {{unknown:42.profit_factor}}")
    assert len(problems1) > 0
    assert any("unknown placeholder kind 'unknown'" in p for p in problems1)

    problems2 = validate("Value: {{candle:sym.field}}")
    assert len(problems2) > 0
    assert any("unknown placeholder kind 'candle'" in p for p in problems2)


def test_malformed_placeholders_fail_validation():
    """Malformed placeholders are detected and reported."""
    cases = [
        ("Missing dot: {{trial:42}}", "expected 'trial:<id>.<field>'"),
        ("Missing field: {{trial:42.}}", "expected 'trial:<id>.<field>'"),
        ("Missing id: {{trial:.profit_factor}}", "expected 'trial:<id>.<field>'"),
        ("Missing colon: {{trial}}", "missing ':'"),
        ("Empty braces: {{}}", "missing ':'"),
        ("Whitespace in placeholder: {{trial: 42.pf}}", "expected 'trial:<id>.<field>'"),
        ("Empty date: {{date:}}", "expected 'date:<iso>'"),
        ("Unclosed placeholder: {{trial:42.pf", "unclosed '{{'"),
        ("Stray closing brace: stray }} here", "stray '}}'"),
    ]
    for text, expected_fragment in cases:
        problems = validate(text)
        assert len(problems) > 0, f"Expected problems for {text!r}"
        assert any(expected_fragment in p for p in problems), (
            f"Expected {expected_fragment!r} in problems: {problems}"
        )


def test_render_with_fake_lookup():
    """render correctly replaces placeholders using the lookup callable."""
    store = {
        ("trial", "42", "profit_factor"): "1.85",
        ("spec", "10", "period"): "20",
        ("hyp", "1", "symbol"): "GOLD",
        ("date", "2024-01-01", ""): "2024-01-01",
    }

    def fake_lookup(kind: str, id_: str, field: str) -> str | None:
        return store.get((kind, id_, field))

    prose = (
        "Hypothesis {{hyp:1.symbol}} with period {{spec:10.period}} "
        "achieved profit factor {{trial:42.profit_factor}} on {{date:2024-01-01}}."
    )
    rendered = render(prose, fake_lookup)
    expected = "Hypothesis GOLD with period 20 achieved profit factor 1.85 on 2024-01-01."
    assert rendered == expected


def test_render_date_lookup_passes_empty_field():
    """Date placeholders pass kind='date', id=iso, and field='' to lookup."""
    recorded_calls = []

    def tracking_lookup(kind: str, id_: str, field: str) -> str | None:
        recorded_calls.append((kind, id_, field))
        return "2025-01-01"

    rendered = render("Start: {{date:2025-01-01}}", tracking_lookup)
    assert rendered == "Start: 2025-01-01"
    assert recorded_calls == [("date", "2025-01-01", "")]


def test_render_without_placeholders_returns_unchanged():
    """render returns prose unchanged when no placeholders exist."""

    def fake_lookup(kind: str, id_: str, field: str) -> str | None:
        return None

    text = "The strategy was rejected due to lack of edge."
    assert render(text, fake_lookup) == text


def test_render_unresolved_placeholder_raises_error():
    """If lookup returns None, render raises GroundingError."""

    def fake_lookup(kind: str, id_: str, field: str) -> str | None:
        return None

    with pytest.raises(GroundingError) as exc_info:
        render("Result was {{trial:99.profit_factor}}.", fake_lookup)

    assert "unresolved placeholder" in str(exc_info.value)
    assert "{{trial:99.profit_factor}}" in str(exc_info.value)


def test_render_unknown_placeholder_kind_raises_error():
    """Unknown placeholder kind in render raises GroundingError."""

    def fake_lookup(kind: str, id_: str, field: str) -> str | None:
        return "anything"

    with pytest.raises(GroundingError) as exc_info:
        render("Value: {{unknown:1.metric}}", fake_lookup)

    assert "unknown placeholder kind 'unknown'" in str(exc_info.value)


def test_render_malformed_placeholder_raises_error():
    """Malformed placeholders in render raise GroundingError."""

    def fake_lookup(kind: str, id_: str, field: str) -> str | None:
        return "val"

    with pytest.raises(GroundingError):
        render("Value: {{trial:42}}", fake_lookup)

    with pytest.raises(GroundingError):
        render("Value: {{unclosed:1.pf", fake_lookup)

    with pytest.raises(GroundingError):
        render("Value: stray }} here", fake_lookup)


def test_exported_placeholder_grammar():
    """Verify exported regex pattern, compiled regex, and known kinds."""
    assert isinstance(PLACEHOLDER_PATTERN, str)
    assert PLACEHOLDER_REGEX.pattern == PLACEHOLDER_PATTERN
    assert {"trial", "spec", "hyp", "date"} == KNOWN_PLACEHOLDER_KINDS

    match = PLACEHOLDER_REGEX.search("Check {{trial:42.pf}} here")
    assert match is not None
    p = Placeholder.from_match(match)
    assert p.kind == "trial"
    assert p.id == "42"
    assert p.field == "pf"
    assert p.raw == "{{trial:42.pf}}"


def test_default_known_tokens():
    """default_known_tokens returns universe symbols, timeframes, and block names."""
    tokens = default_known_tokens()
    assert "GOLD" in tokens
    assert "EURUSD" in tokens
    assert "M15" in tokens
    assert "H1" in tokens
    assert "donchian_breakout" in tokens
    assert "atr_stop" in tokens
