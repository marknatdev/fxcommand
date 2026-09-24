"""Candidate = Strategy + full parameter set, with a stable identity and search moves."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from ..strategies import STRATEGIES, get_strategy


def _canonical(params: dict) -> dict:
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in sorted(params.items())}


@dataclass(frozen=True)
class Candidate:
    strategy: str
    params: tuple[tuple[str, object], ...]  # resolved, canonical, hashable

    @classmethod
    def of(cls, strategy: str, params: dict | None) -> "Candidate":
        resolved = get_strategy(strategy).resolve(params)
        return cls(strategy, tuple(_canonical(resolved).items()))

    @property
    def param_dict(self) -> dict:
        return dict(self.params)

    @property
    def key(self) -> str:
        blob = json.dumps({"s": self.strategy, "p": dict(self.params)}, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def label(self) -> str:
        s = get_strategy(self.strategy)
        defaults = s.defaults()
        changed = [f"{k}={v}" for k, v in self.params if defaults.get(k) != v]
        return f"{s.title}" + (f" ({', '.join(changed)})" if changed else " (defaults)")

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "params": self.param_dict, "key": self.key, "label": self.label()}


def random_candidate(strategy: str, rng: np.random.Generator) -> Candidate:
    """Log-uniform within [default/3, default*3], clipped to each parameter's bounds."""
    s = get_strategy(strategy)
    out = {}
    for p in s.params:
        if p.type == "bool":
            out[p.name] = bool(rng.random() < 0.5)
            continue
        d = float(p.default)
        lo = max(float(p.min if p.min is not None else 0), d / 3 if d > 0 else 0)
        hi = min(float(p.max if p.max is not None else d * 3), d * 3 if d > 0 else 10)
        v = float(np.exp(rng.uniform(np.log(max(lo, 1e-3)), np.log(max(hi, lo + 1e-3)))))
        out[p.name] = int(round(v)) if p.type == "int" else round(v, 2)
    return _fix(strategy, out)


def perturb(c: Candidate, rng: np.random.Generator, scale: float = 0.4) -> Candidate:
    """A neighbour: each numeric param moved by up to ±scale relatively; bools flip with small probability."""
    s = get_strategy(c.strategy)
    out = dict(c.params)
    for p in s.params:
        v = out[p.name]
        if p.type == "bool":
            if rng.random() < 0.15:
                out[p.name] = not v
            continue
        nv = float(v) * (1 + rng.uniform(-scale, scale))
        if p.type == "int":
            nv = int(round(nv))
            if nv == v:
                nv = v + int(rng.choice([-1, 1]))
        out[p.name] = nv if p.type == "int" else round(nv, 2)
    return _fix(c.strategy, out)


def _fix(strategy: str, params: dict) -> Candidate:
    """Clamp to bounds and repair relations between params (e.g. fast < slow)."""
    resolved = get_strategy(strategy).resolve(params)
    if strategy == "ema_cross" and resolved["fast"] >= resolved["slow"]:
        resolved["slow"] = resolved["fast"] + max(2, resolved["fast"] // 2)
    if strategy == "donchian_breakout" and resolved["exit_period"] >= resolved["period"]:
        resolved["exit_period"] = max(0, resolved["period"] // 2)
    if strategy == "rsi_reversion" and resolved["oversold"] >= resolved["overbought"]:
        resolved["oversold"], resolved["overbought"] = 30.0, 70.0
    return Candidate.of(strategy, resolved)


def generate(champion: Candidate, n: int, rng: np.random.Generator, local_share: float = 0.7) -> list[Candidate]:
    """``n`` distinct Candidates: mostly neighbours of the Champion, the rest random across all Strategies."""
    seen = {champion.key}
    out: list[Candidate] = []
    families = list(STRATEGIES)
    attempts = 0
    while len(out) < n and attempts < n * 20:
        attempts += 1
        if rng.random() < local_share:
            c = perturb(champion, rng)
        else:
            c = random_candidate(families[int(rng.integers(len(families)))], rng)
        if c.key not in seen:
            seen.add(c.key)
            out.append(c)
    return out
