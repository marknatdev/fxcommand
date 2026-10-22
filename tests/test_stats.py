"""M2 T3: Sharpe, the Deflated Sharpe and PBO.

No numbers are pinned from the papers (the sources could not be fetched). The Deflated Sharpe is
checked on its limit cases and against a formula written out again here in plain Python, with
its own Φ, Φ⁻¹ and Euler–Mascheroni constant. PBO is checked against a loop over every split
and on matrices built to have a known answer.
"""

import math
import time
from itertools import combinations

import numpy as np
import pytest

from vibedesk.gauntlet.stats import (
    EULER_GAMMA,
    deflated_sharpe,
    expected_max_sharpe,
    pbo,
    probabilistic_sharpe,
    sharpe,
)

# --- the formula, written out again ---------------------------------------------------------


def phi(z):
    return 0.5 * math.erfc(-z / math.sqrt(2))


def probit(p):
    """Φ⁻¹ by bisection on Φ, so it shares nothing with NormalDist.inv_cdf."""
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if phi(mid) < p else (lo, mid)
    return (lo + hi) / 2


def euler_gamma(n=1000):
    """H(n) - ln(n), with the asymptotic correction terms; the error is of order 1/n⁶."""
    harmonic = math.fsum(1 / k for k in range(1, n + 1))
    return harmonic - math.log(n) - 1 / (2 * n) + 1 / (12 * n**2) - 1 / (120 * n**4)


def reference_deflated_sharpe(x, n_trials, var_trial_sharpe):
    """Bailey & López de Prado (2014), term by term, from the raw series."""
    t = len(x)
    mean = math.fsum(x) / t
    dev = [v - mean for v in x]
    sr = mean / math.sqrt(math.fsum(d * d for d in dev) / (t - 1))
    m2, m3, m4 = (math.fsum(d**k for d in dev) / t for k in (2, 3, 4))
    skew, kurtosis = m3 / m2**1.5, m4 / m2**2
    g = euler_gamma()
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var_trial_sharpe) * (
            (1 - g) * probit(1 - 1 / n_trials) + g * probit(1 - 1 / (n_trials * math.e))
        )
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - skew * sr + (kurtosis - 1) / 4 * sr**2)
    return phi(z)


def moments(x):
    x = np.asarray(x)
    d = x - x.mean()
    m2 = np.mean(d**2)
    return np.mean(d**3) / m2**1.5, np.mean(d**4) / m2**2


def scaled(z, mean, std):
    """`z` moved to exactly this mean and sample std, keeping its shape (skew and kurtosis)."""
    z = (z - z.mean()) / z.std(ddof=1)
    return mean + std * z


# --- Sharpe -----------------------------------------------------------------------------------


def test_sharpe_is_mean_over_sample_std():
    x = np.random.default_rng(1).normal(0.05, 1.3, 500)
    assert sharpe(x) == pytest.approx(x.mean() / x.std(ddof=1), rel=1e-12)
    assert sharpe(list(x)) == sharpe(x)


def test_sharpe_of_short_or_flat_series():
    assert math.isnan(sharpe([]))
    assert math.isnan(sharpe([1.5]))
    assert sharpe(np.zeros(250)) == 0.0  # no trades: no edge
    assert sharpe(np.full(250, 0.1)) == 0.0  # flat up to rounding: never an infinite Sharpe
    assert sharpe([0.0, 1.0]) == pytest.approx(0.5 / math.sqrt(0.5))


# --- Probabilistic and Deflated Sharpe ------------------------------------------------------


def test_probabilistic_sharpe_at_and_around_the_benchmark():
    assert probabilistic_sharpe(0.05, 0.05, 1000, -1.0, 8.0) == pytest.approx(0.5)
    assert probabilistic_sharpe(0.08, 0.05, 1000, 0.0, 3.0) > 0.5
    assert probabilistic_sharpe(0.02, 0.05, 1000, 0.0, 3.0) < 0.5
    assert math.isnan(probabilistic_sharpe(0.05, 0.0, 1, 0.0, 3.0))
    assert math.isnan(probabilistic_sharpe(1.0, 0.0, 100, 4.0, 1.0))  # impossible moments


def test_the_euler_gamma_constant():
    assert euler_gamma() == pytest.approx(EULER_GAMMA, abs=1e-15)


def test_expected_max_sharpe_limits():
    for v in (0.0, 1e-4, 0.5):
        assert expected_max_sharpe(1, v) == 0.0
    for n in (1, 2, 100, 10**6):
        assert expected_max_sharpe(n, 0.0) == 0.0
    by_trials = [expected_max_sharpe(n, 1e-3) for n in (2, 5, 20, 100, 1000)]
    assert all(a < b for a, b in zip(by_trials, by_trials[1:], strict=False))
    by_variance = [expected_max_sharpe(50, v) for v in (1e-5, 1e-4, 1e-3, 1e-2)]
    assert all(a < b for a, b in zip(by_variance, by_variance[1:], strict=False))
    with pytest.raises(ValueError):
        expected_max_sharpe(0, 1e-3)
    with pytest.raises(ValueError):
        expected_max_sharpe(10, -1e-3)
    with pytest.raises(ValueError):
        expected_max_sharpe(10, math.nan)


@pytest.fixture(scope="module")
def blind_daily_r():
    """1,000 days with a daily Sharpe of exactly 0.06: mid-range, so no probability saturates."""
    return scaled(np.random.default_rng(7).standard_t(5, 1000), 0.06, 1.0)


def test_one_trial_is_the_probabilistic_sharpe_against_zero(blind_daily_r):
    x = blind_daily_r
    skew, kurtosis = moments(x)
    expected = probabilistic_sharpe(sharpe(x), 0.0, len(x), skew, kurtosis)
    assert 0.2 < expected < 0.99
    for v in (0.0, 1e-4, 1e-2):
        assert deflated_sharpe(x, 1, v) == pytest.approx(expected, abs=1e-15)


def test_no_variance_between_trials_means_no_deflation(blind_daily_r):
    one_trial = deflated_sharpe(blind_daily_r, 1, 0.0)
    for n in (2, 100, 10**6):
        assert deflated_sharpe(blind_daily_r, n, 0.0) == pytest.approx(one_trial, abs=1e-15)


def test_deflated_sharpe_falls_as_trials_or_variance_rise(blind_daily_r):
    by_trials = [deflated_sharpe(blind_daily_r, n, 1e-3) for n in (1, 2, 5, 20, 100, 1000)]
    assert all(a > b for a, b in zip(by_trials, by_trials[1:], strict=False))
    assert by_trials[-1] > 1e-6  # still above zero, so the strict fall above is real

    by_variance = [deflated_sharpe(blind_daily_r, 50, v) for v in (0.0, 1e-5, 1e-4, 1e-3)]
    assert all(a > b for a, b in zip(by_variance, by_variance[1:], strict=False))
    assert by_variance[-1] > 1e-6


def test_negative_skew_lowers_deflated_sharpe():
    right = scaled(np.random.default_rng(3).exponential(1.0, 1500), 0.05, 1.0)
    left = 2 * right.mean() - right  # the mirror image: same mean, std and kurtosis
    assert moments(right)[0] > 1 and moments(left)[0] < -1
    assert sharpe(left) == pytest.approx(sharpe(right), rel=1e-12)
    for n, v in ((1, 0.0), (10, 1e-4)):
        assert deflated_sharpe(left, n, v) < deflated_sharpe(right, n, v)


def test_fat_tails_lower_deflated_sharpe():
    rng = np.random.default_rng(4)
    normal, t3 = rng.normal(size=1000), rng.standard_t(3, 1000)
    thin = scaled(np.r_[normal, -normal], 0.05, 1.0)  # symmetric: skew exactly 0
    fat = scaled(np.r_[t3, -t3], 0.05, 1.0)
    assert abs(moments(thin)[0]) < 1e-9 and abs(moments(fat)[0]) < 1e-9
    assert moments(fat)[1] > moments(thin)[1] + 3
    for n, v in ((1, 0.0), (10, 1e-4)):
        assert deflated_sharpe(fat, n, v) < deflated_sharpe(thin, n, v)


def test_deflated_sharpe_matches_the_formula_written_out_on_random_inputs():
    rng = np.random.default_rng(2014)
    middling = 0
    for _ in range(50):
        t = int(rng.integers(50, 3000))
        z = [rng.normal(size=t), rng.exponential(size=t), -rng.exponential(size=t),
             rng.standard_t(4, t)][int(rng.integers(4))]  # fmt: skip
        x = rng.uniform(-0.05, 0.15) + z * (rng.random(t) < rng.uniform(0.2, 1.0))  # zero days
        n_trials = 1 if rng.random() < 0.1 else int(rng.integers(2, 2000))
        var_trial_sharpe = 0.0 if rng.random() < 0.1 else 10 ** rng.uniform(-5, -2)

        got = deflated_sharpe(x, n_trials, var_trial_sharpe)
        assert got == pytest.approx(
            reference_deflated_sharpe(x.tolist(), n_trials, var_trial_sharpe),
            rel=1e-9, abs=1e-12,
        )  # fmt: skip
        middling += 0.05 < got < 0.95
    assert middling >= 10  # the agreement isn't only 0 ≈ 0 and 1 ≈ 1


def test_deflated_sharpe_of_flat_or_short_series():
    assert deflated_sharpe(np.zeros(500), 1, 0.0) == pytest.approx(0.5)  # Sharpe 0 against 0
    sr0 = expected_max_sharpe(48, 1e-3)
    assert deflated_sharpe(np.zeros(500), 48, 1e-3) == pytest.approx(phi(-sr0 * math.sqrt(499)))
    assert math.isnan(deflated_sharpe([0.5], 1, 0.0))


def test_deflated_sharpe_rejects_bad_inputs():
    with pytest.raises(ValueError):
        deflated_sharpe([0.1, math.nan, 0.2], 1, 0.0)
    with pytest.raises(ValueError):
        deflated_sharpe([0.1, 0.2], 0, 0.0)
    with pytest.raises(ValueError):
        deflated_sharpe([0.1, 0.2], 5, -1.0)
    with pytest.raises(ValueError):
        deflated_sharpe(np.zeros((10, 2)), 1, 0.0)


# --- PBO --------------------------------------------------------------------------------------


def reference_pbo(returns, blocks):
    """CSCV the slow way: concatenate the chosen blocks and take each Sharpe directly."""
    parts = np.array_split(returns, blocks)
    n = returns.shape[1]
    overfit = splits = 0
    for train in combinations(range(blocks), blocks // 2):
        test = [b for b in range(blocks) if b not in train]
        in_sample = [sharpe(np.concatenate([parts[b][:, j] for b in train])) for j in range(n)]
        out_sample = [sharpe(np.concatenate([parts[b][:, j] for b in test])) for j in range(n)]
        best = out_sample[int(np.argmax(in_sample))]
        rank = 1 + sum(s < best for s in out_sample)  # continuous noise: no ties
        omega = rank / (n + 1)
        overfit += math.log(omega / (1 - omega)) <= 0
        splits += 1
    return overfit / splits


def test_pbo_matches_a_loop_over_every_split():
    rng = np.random.default_rng(11)
    returns = rng.normal(0.02, 1.0, (203, 6)) * (rng.random((203, 6)) < 0.4)  # 203: uneven
    assert pbo(returns, blocks=8) == reference_pbo(returns, 8)


def test_pbo_is_zero_when_one_combination_is_best_everywhere():
    # Same noise, different drifts: on any union of blocks every column has the same std, so
    # the Sharpe order is the drift order, in-sample and out-of-sample alike.
    noise = np.random.default_rng(5).normal(size=(2000, 1))
    returns = noise + 0.01 * np.arange(10)
    assert pbo(returns) == 0.0


def test_pbo_of_iid_noise_is_about_one_half():
    # On noise the out-of-sample rank of the in-sample pick is uniform, so PBO is 0.5 on
    # average. One matrix is not enough to see it: its 12,870 splits share the same days, and
    # PBO spreads widely from seed to seed (seed 0 alone gives about 0.31, matching the loop
    # reference). The mean over seeds 0–49 has a standard error near 0.025.
    results = [pbo(np.random.default_rng(seed).normal(size=(2000, 48))) for seed in range(50)]
    assert np.mean(results) == pytest.approx(0.5, abs=0.1)
    assert min(results) < 0.4 and max(results) > 0.6  # the spread the comment describes


def test_pbo_is_high_when_the_in_sample_best_gives_it_back():
    # Each column has its own full-sample mean removed, and 2,000 days make 16 equal blocks:
    # whatever a column earns in-sample it loses out-of-sample.
    noise = np.random.default_rng(6).normal(size=(2000, 10))
    returns = noise - noise.mean(axis=0)
    assert pbo(returns) >= 0.9


def test_pbo_with_blocks_without_trades():
    rng = np.random.default_rng(8)
    returns = rng.normal(size=(1600, 12))
    returns[:400] = 0.0  # the first 4 of 16 blocks have no trades
    returns[:, 3] = 0.0  # one combination never trades
    assert 0.0 <= pbo(returns) <= 1.0
    # Nothing ever trades: every combination ties everywhere, the in-sample pick sits at the
    # out-of-sample median (logit 0), and that counts as overfit.
    assert pbo(np.zeros((1600, 12))) == 1.0


def test_pbo_of_identical_combinations():
    # A parameter that never binds makes duplicate columns. They tie exactly everywhere, so the
    # pick sits at the out-of-sample median, as in the all-zero case.
    rng = np.random.default_rng(12)
    column = rng.normal(size=(2000, 1)) * (rng.random((2000, 1)) < 0.3)
    assert pbo(np.tile(column, (1, 12))) == 1.0


def test_pbo_of_a_single_combination_is_not_applicable():
    assert math.isnan(pbo(np.random.default_rng(9).normal(size=(2000, 1))))


def test_pbo_rejects_bad_inputs():
    with pytest.raises(ValueError):
        pbo(np.zeros(2000))
    with pytest.raises(ValueError):
        pbo(np.zeros((2000, 4)), blocks=15)
    with pytest.raises(ValueError):
        pbo(np.zeros((31, 4)))  # under 2 days per block
    bad = np.zeros((2000, 4))
    bad[10, 2] = math.inf
    with pytest.raises(ValueError):
        pbo(bad)


@pytest.mark.perf
def test_pbo_speed():
    returns = np.random.default_rng(10).normal(size=(2000, 48))
    pbo(returns)  # build the split table once
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        pbo(returns)
        timings.append(time.perf_counter() - start)
    assert min(timings) <= 2.0, f"best of 3: {min(timings):.3f} s"
