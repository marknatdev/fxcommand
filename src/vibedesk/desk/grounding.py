"""Grounding Rule validator and placeholder rendering (spec §7.3, §9.4, ADR 0005).

Assistant prose may not contain raw digits. Every figure shown must be rendered from
stored data via structured placeholders:
- {{trial:ID.metric}}
- {{spec:ID.param}}
- {{hyp:ID.field}}
- {{date:ISO}}

The only digits allowed in prose are those inside valid placeholders or whitelisted
known tokens: symbol names from the universe, timeframe names (M1, M15, H1, H4, D1),
and Block names.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

# Known placeholder kinds
KIND_TRIAL = "trial"
KIND_SPEC = "spec"
KIND_HYP = "hyp"
KIND_DATE = "date"

KNOWN_PLACEHOLDER_KINDS: frozenset[str] = frozenset({KIND_TRIAL, KIND_SPEC, KIND_HYP, KIND_DATE})

# Grammar for valid placeholders:
# - {{trial:ID.metric}}, {{spec:ID.param}}, {{hyp:ID.field}}
# - {{date:ISO}}
ISO_DATE_PATTERN: str = (
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
ISO_DATE_REGEX: re.Pattern[str] = re.compile(rf"^{ISO_DATE_PATTERN}$")

PLACEHOLDER_PATTERN: str = (
    r"\{\{(?:(?P<kind>trial|spec|hyp):(?P<id>[^\s.:{}]+)\.(?P<field>[^\s.:{}]+)|"
    r"(?P<date_kind>date):(?P<date_iso>" + ISO_DATE_PATTERN + r"))\}\}"
)
PLACEHOLDER_REGEX: re.Pattern[str] = re.compile(PLACEHOLDER_PATTERN)

# Any potential placeholder candidate {{...}}
CANDIDATE_PLACEHOLDER_PATTERN: str = r"\{\{([^{}]*)\}\}"
CANDIDATE_REGEX: re.Pattern[str] = re.compile(CANDIDATE_PLACEHOLDER_PATTERN)


class GroundingError(ValueError):
    """Raised when placeholder resolution or validation fails."""


@dataclass(frozen=True)
class Placeholder:
    """A parsed placeholder token."""

    kind: str
    id: str
    field: str = ""
    raw: str = ""

    @classmethod
    def from_match(cls, m: re.Match[str]) -> Placeholder:
        kind = m.group("kind") or m.group("date_kind") or ""
        id_ = m.group("id") or m.group("date_iso") or ""
        field = m.group("field") or ""
        return cls(kind=kind, id=id_, field=field, raw=m.group(0))


_DEFAULT_KNOWN_TOKENS: frozenset[str] | None = None


def _get_default_known_tokens() -> frozenset[str]:
    global _DEFAULT_KNOWN_TOKENS
    if _DEFAULT_KNOWN_TOKENS is None:
        import vibedesk.blocks  # noqa: F401 - populate LIBRARY
        from vibedesk.blocks.registry import LIBRARY
        from vibedesk.market.bars import Timeframe
        from vibedesk.market.universe import SYMBOLS

        tokens: set[str] = set(SYMBOLS)
        tokens.update(tf.value for tf in Timeframe)
        tokens.update(b.name for b in LIBRARY.all())
        _DEFAULT_KNOWN_TOKENS = frozenset(tokens)
    return _DEFAULT_KNOWN_TOKENS


def default_known_tokens() -> set[str]:
    """Default tokens allowed to contain digits: universe symbols, timeframes, Block names."""
    return set(_get_default_known_tokens())


def _token_pattern(token: str) -> str:
    """Build a boundary-respecting regex pattern for a known token."""
    escaped = re.escape(token)
    prefix = r"(?<!\w)" if re.match(r"^\w", token) else r"(?<!\S)"
    suffix = r"(?!\w)" if re.search(r"\w$", token) else r"(?!\S)"
    return f"{prefix}{escaped}{suffix}"


def _is_iso_date(value: str) -> bool:
    """Validate that value is a well-formed ISO 8601 calendar date or datetime."""
    if not ISO_DATE_REGEX.match(value):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def _inspect_placeholder(raw: str, inner: str) -> tuple[Placeholder | None, str | None]:
    """Inspect a candidate placeholder string.

    Returns (placeholder, None) if valid.
    Returns (None, problem_description) if malformed or unknown kind.
    """
    if ":" not in inner:
        return None, f"malformed placeholder '{raw}': missing ':'"

    kind, rest = inner.split(":", 1)

    if any(c.isspace() for c in kind):
        clean_kind = kind.strip() or "kind"
        expected = "date:<iso>" if clean_kind == KIND_DATE else f"{clean_kind}:<id>.<field>"
        return None, f"malformed placeholder '{raw}': expected '{expected}'"

    if kind not in KNOWN_PLACEHOLDER_KINDS:
        return None, f"unknown placeholder kind '{kind}' in '{raw}'"

    if kind == KIND_DATE:
        if not rest or any(c.isspace() for c in rest) or not _is_iso_date(rest):
            return None, f"malformed placeholder '{raw}': expected 'date:<iso>'"
        if not PLACEHOLDER_REGEX.fullmatch(raw):
            return None, f"malformed placeholder '{raw}': expected 'date:<iso>'"
        return Placeholder(kind=KIND_DATE, id=rest, field="", raw=raw), None

    if "." not in rest:
        return None, f"malformed placeholder '{raw}': expected '{kind}:<id>.<field>'"

    parts = rest.split(".", 1)
    id_part = parts[0]
    field_part = parts[1]

    if not id_part or not field_part or any(c.isspace() for c in rest):
        return None, f"malformed placeholder '{raw}': expected '{kind}:<id>.<field>'"
    if "." in field_part:
        return None, f"malformed placeholder '{raw}': multiple dots in field '{field_part}'"
    if ":" in rest:
        return None, f"malformed placeholder '{raw}': unexpected ':' in '{rest}'"

    if not PLACEHOLDER_REGEX.fullmatch(raw):
        return None, f"malformed placeholder '{raw}': expected '{kind}:<id>.<field>'"

    return Placeholder(kind=kind, id=id_part, field=field_part, raw=raw), None


def _extract_forbidden_tokens(prose: str, forbidden_indices: set[int]) -> list[str]:
    """Find contiguous non-whitespace spans containing forbidden digits."""
    tokens = []
    for match in re.finditer(r"\S+", prose):
        span_set = set(range(match.start(), match.end()))
        if span_set & forbidden_indices:
            token = match.group(0).strip(".,;:?!'\"()[]{}")
            tokens.append(token if token else match.group(0))
    return tokens


def validate(prose: str, *, known_tokens: Iterable[str] | None = None) -> list[str]:
    """Validate that assistant prose contains no disallowed digits or invalid placeholders.

    Returns a list of problems (empty list = ok).
    Prose may not contain any digit except inside:
    - symbol names from the universe;
    - timeframe names (M1, M15, H1, H4, D1);
    - Block names;
    - placeholders {{trial:ID.metric}}, {{spec:ID.param}}, {{hyp:ID.field}}, {{date:ISO}}.

    A malformed or unknown placeholder kind is always reported as a problem.
    Default known_tokens = universe symbols + timeframes + Block names.
    """
    problems: list[str] = []
    allowed_mask = [False] * len(prose)

    # 1. Check for unclosed {{ or stray }}
    stripped = CANDIDATE_REGEX.sub("  ", prose)
    if "{{" in stripped:
        problems.append("malformed placeholder: unclosed '{{' in prose")
    if "}}" in stripped:
        problems.append("malformed placeholder: stray '}}' in prose")

    # 2. Inspect candidate placeholders {{...}}
    for match in CANDIDATE_REGEX.finditer(prose):
        raw = match.group(0)
        inner = match.group(1)
        placeholder, err = _inspect_placeholder(raw, inner)
        if err is not None:
            problems.append(err)
        else:
            for idx in range(match.start(), match.end()):
                allowed_mask[idx] = True

    # 3. Match known tokens
    tokens = _get_default_known_tokens() if known_tokens is None else set(known_tokens)
    for token in tokens:
        if not token:
            continue
        pattern = _token_pattern(token)
        for match in re.finditer(pattern, prose):
            for idx in range(match.start(), match.end()):
                allowed_mask[idx] = True

    # 4. Check for forbidden digits
    forbidden_indices = {
        idx for idx, ch in enumerate(prose) if ch.isdigit() and not allowed_mask[idx]
    }
    if forbidden_indices:
        forbidden_tokens = _extract_forbidden_tokens(prose, forbidden_indices)
        problems.append(
            f"prose contains forbidden digit(s): {', '.join(repr(t) for t in forbidden_tokens)}"
        )

    return problems


def render(prose: str, lookup: Callable[[str, str, str], str | None]) -> str:
    """Replace each placeholder with lookup(kind, id, field).

    For date placeholders: lookup("date", id, "").
    If lookup returns None, raises GroundingError (spec §7.3).
    Unknown placeholder kinds or malformed placeholders also raise GroundingError.
    """
    stripped = CANDIDATE_REGEX.sub("  ", prose)
    if "{{" in stripped:
        raise GroundingError("malformed placeholder: unclosed '{{' in prose")
    if "}}" in stripped:
        raise GroundingError("malformed placeholder: stray '}}' in prose")

    candidates = list(CANDIDATE_REGEX.finditer(prose))
    replacements: list[tuple[int, int, str]] = []

    for match in candidates:
        raw = match.group(0)
        inner = match.group(1)
        placeholder, err = _inspect_placeholder(raw, inner)
        if err is not None:
            raise GroundingError(err)
        assert placeholder is not None

        val = lookup(placeholder.kind, placeholder.id, placeholder.field)
        if val is None:
            raise GroundingError(f"unresolved placeholder '{raw}': lookup returned None")
        replacements.append((match.start(), match.end(), str(val)))

    result = prose
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]

    return result


__all__ = [
    "CANDIDATE_PLACEHOLDER_PATTERN",
    "CANDIDATE_REGEX",
    "GroundingError",
    "ISO_DATE_PATTERN",
    "ISO_DATE_REGEX",
    "KIND_DATE",
    "KIND_HYP",
    "KIND_SPEC",
    "KIND_TRIAL",
    "KNOWN_PLACEHOLDER_KINDS",
    "PLACEHOLDER_PATTERN",
    "PLACEHOLDER_REGEX",
    "Placeholder",
    "default_known_tokens",
    "render",
    "validate",
]
