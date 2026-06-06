"""Tests for the frontend-exact getHealthMeter health computation.

`_health_meter_pct` is the shared, byte-identical core across deltaprime / arbprime /
degenprime (the cross-file identity test pins that parity). It is a pure function over a
per-asset list of {symbol, dc, supplied_usd, borrowed_usd}, so these tests are fully
offline. `_compute_health_pct` is exercised with `_resolve_debt_coverages` monkeypatched
so no chain is ever touched (the dc read is the only network seam).

The reference formula (HealthMeterFacetProd.getHealthMeter):
    net_i = supplied_usd_i - borrowed_usd_i
    weightedCollateral = Σ dc_i·net_i  (net-long legs) - Σ dc_i·(-net_i)  (net-short legs)
    weightedBorrowed   = Σ dc_i·borrowed_usd_i
    borrowed           = Σ borrowed_usd_i        (UNWEIGHTED)
    borrowed == 0                                                  -> 100
    wc > 0 and wc + weightedBorrowed > borrowed
        -> (wc + weightedBorrowed - borrowed) / wc · 100  (clamped 0..100)
    else                                                           -> 0
"""

from __future__ import annotations

import importlib
import random

import pytest

DELTA = importlib.import_module("primecli.deltaprime")
ARB = importlib.import_module("primecli.arbprime")
DEGEN = importlib.import_module("primecli.degenprime")

DC_MAJOR = 10.0 / 11.0   # 0.909090909…  (10x class)
DC_RISKY = 5.0 / 6.0     # 0.833333333…  (5x class)


def _leveraged(dc: float, equity: float, debt: float) -> list:
    """Model a leveraged-farm position: one collateral asset holding (equity+debt) long,
    one debt asset borrowed `debt`. equity = supplied - borrowed across the two legs."""
    return [
        {"symbol": "COLL", "dc": dc, "supplied_usd": equity + debt, "borrowed_usd": 0.0},
        {"symbol": "DEBT", "dc": dc, "supplied_usd": 0.0, "borrowed_usd": debt},
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Reference anchors from the brief (queried on-chain): dc 0.909091 and 0.833333


def test_uniform_major_dc_zero_half_full():
    """dc = 0.909091 (10x class), equity = 100: D=0 → 100%, D=500 → 50%, D=1000 → 0%."""
    f = DELTA._health_meter_pct
    assert f(_leveraged(DC_MAJOR, 100, 0))["health_pct"] == 100.0
    assert f(_leveraged(DC_MAJOR, 100, 500))["health_pct"] == 50.0
    assert f(_leveraged(DC_MAJOR, 100, 1000))["health_pct"] == 0.0


def test_uniform_risky_dc_max_debt_500():
    """dc = 0.833333 (5x class), equity = 100: health hits exactly 0 at D = 500 (the 5x
    max-debt line). Just under is barely positive, at/over is 0."""
    f = DELTA._health_meter_pct
    assert f(_leveraged(DC_RISKY, 100, 499))["health_pct"] > 0.0
    assert f(_leveraged(DC_RISKY, 100, 500))["health_pct"] == 0.0
    assert f(_leveraged(DC_RISKY, 100, 501))["health_pct"] == 0.0


def test_no_debt_is_full_health():
    f = DELTA._health_meter_pct
    assert f([{"symbol": "A", "dc": DC_MAJOR, "supplied_usd": 1234.5, "borrowed_usd": 0.0}])["health_pct"] == 100.0
    # No assets at all (empty account) → no debt → 100.
    assert f([])["health_pct"] == 100.0


def test_net_short_leg_subtracts_from_collateral():
    """An asset borrowed MORE than its free balance is a net-short leg: it subtracts a
    dc-weighted amount from weightedCollateral (it does not count as collateral)."""
    f = DELTA._health_meter_pct
    # COLL long 1000; X borrowed 600 with only 100 free balance → net short 500 on X.
    assets = [
        {"symbol": "COLL", "dc": DC_MAJOR, "supplied_usd": 1000.0, "borrowed_usd": 0.0},
        {"symbol": "X", "dc": DC_MAJOR, "supplied_usd": 100.0, "borrowed_usd": 600.0},
    ]
    wc_plus = DC_MAJOR * 1000.0
    wc_minus = DC_MAJOR * 500.0
    wc = wc_plus - wc_minus
    wb = DC_MAJOR * 600.0
    borrowed = 600.0
    expected = max(0.0, min(100.0, (wc + wb - borrowed) / wc * 100.0))
    assert f(assets)["health_pct"] == pytest.approx(round(expected, 1), abs=0.05)


# ──────────────────────────────────────────────────────────────────────────────
# Mixed-dc: sweep vs an independent inline re-implementation of the contract formula


def _reference_health(assets: list) -> float:
    """Independent re-implementation of getHealthMeter — deliberately NOT sharing code
    with _health_meter_pct, so the sweep cross-checks the real implementation."""
    wcp = wcm = wb = bor = 0.0
    for a in assets:
        dc = a["dc"]
        net = a["supplied_usd"] - a["borrowed_usd"]
        if net > 0:
            wcp += dc * net
        elif net < 0:
            wcm += dc * (-net)
        wb += dc * a["borrowed_usd"]
        bor += a["borrowed_usd"]
    wc = wcp - wcm
    if bor <= 0:
        return 100.0
    if wc > 0 and wc + wb > bor:
        return max(0.0, min(100.0, (wc + wb - bor) / wc * 100.0))
    return 0.0


def test_mixed_dc_sweep_matches_reference():
    f = DELTA._health_meter_pct
    rng = random.Random(20260606)
    dcs = [DC_MAJOR, DC_RISKY, 0.9, 0.8, 0.75, 0.5]
    for _ in range(20000):
        n = rng.randint(1, 5)
        assets = [
            {
                "symbol": f"S{i}",
                "dc": rng.choice(dcs),
                "supplied_usd": rng.uniform(0, 5000),
                "borrowed_usd": rng.uniform(0, 5000),
            }
            for i in range(n)
        ]
        got = f(assets)["health_pct"]
        exp = round(_reference_health(assets), 1)
        # Both round to one decimal; the only gap is half-ULP rounding, well under 0.05.
        assert abs(got - exp) <= 0.05, (assets, got, exp)


def test_core_is_identical_object_across_siblings():
    """Defensive: the three siblings expose the same callable behaviour (the cross-file
    identity test pins the source; this pins runtime behaviour on a shared input)."""
    assets = _leveraged(DC_MAJOR, 250, 1000)
    r_delta = DELTA._health_meter_pct(assets)
    r_arb = ARB._health_meter_pct(assets)
    r_degen = DEGEN._health_meter_pct(assets)
    assert r_delta == r_arb == r_degen


# ──────────────────────────────────────────────────────────────────────────────
# _compute_health_pct — dc resolution mocked (no network), tier label + max_debt


def test_compute_health_pct_delta_mocks_dc(monkeypatch):
    """deltaprime._compute_health_pct resolves dc via _resolve_debt_coverages (mocked here),
    merges per-symbol supplied/borrowed, and reports the contract health plus a display
    max_debt and the tier label. PREMIUM (tier 1) → dc 0.909091 → 50% at the half-debt line."""
    monkeypatch.setattr(
        DELTA, "_resolve_debt_coverages",
        lambda w3, syms, tier_code=0: {s: DC_MAJOR for s in syms},
    )
    data = {
        "w3": object(),  # never used — dc is mocked
        "supplied": [{"symbol": "USDC", "usd": 600.0}],
        "borrowed": [{"symbol": "AVAX", "usd": 500.0}],
    }
    hp = DELTA._compute_health_pct(data, tier_code=1)
    assert hp["equity"] == 100.0
    assert hp["health_pct"] == 50.0
    assert hp["tier"] == "PREMIUM"
    assert hp["max_debt"] == pytest.approx(1000.0, abs=0.5)  # equity·dc/(1-dc) = 100·10 = 1000


def test_compute_health_pct_delta_uses_row_dc_without_w3(monkeypatch):
    """When rows already carry `dc` (stamped by gather_lending), no resolver call is needed
    even with w3 absent."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("resolver must not be called when rows carry dc")

    monkeypatch.setattr(DELTA, "_resolve_debt_coverages", _boom)
    data = {
        "supplied": [{"symbol": "USDC", "usd": 1500.0, "dc": DC_RISKY}],
        "borrowed": [{"symbol": "BTC", "usd": 500.0, "dc": DC_RISKY}],
    }
    hp = DELTA._compute_health_pct(data, tier_code=0)
    assert called["n"] == 0
    assert hp["equity"] == 1000.0
    # equity 1000, dc 0.833333 → max_debt 5000 → debt 500 → health 90%.
    assert hp["health_pct"] == 90.0
    assert hp["tier"] == "BASIC"


def test_compute_health_pct_degen_list_signature(monkeypatch):
    """degenprime._compute_health_pct takes (supplied, borrowed) lists and resolves dc with
    the un-tiered Base coverage (mocked). Equity near zero → error branch."""
    monkeypatch.setattr(
        DEGEN, "_resolve_debt_coverages",
        lambda w3, syms, tier_code=0: {s: DC_MAJOR for s in syms},
    )
    supplied = [{"symbol": "USDC", "usd": 1000.0}]
    borrowed = [{"symbol": "USDC", "usd": 1000.0}]
    hp = DEGEN._compute_health_pct(supplied, borrowed, w3=object())
    assert hp["error"] == "equity near zero"
    assert hp["health_pct"] == 0.0


def test_compute_health_pct_equity_near_zero_delta(monkeypatch):
    monkeypatch.setattr(
        DELTA, "_resolve_debt_coverages",
        lambda w3, syms, tier_code=0: {s: DC_MAJOR for s in syms},
    )
    data = {
        "w3": object(),
        "supplied": [{"symbol": "USDC", "usd": 1000.0}],
        "borrowed": [{"symbol": "USDC", "usd": 1000.0}],
    }
    hp = DELTA._compute_health_pct(data, tier_code=0)
    assert hp["error"] == "equity near zero"
    assert hp["health_pct"] == 0.0
    assert hp["max_debt"] == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# _resolve_debt_coverages — the on-chain dc read wiring, multicall mocked


class _FakeCodec:
    def encode(self, types, values):
        from eth_abi import encode as _enc
        return _enc(types, values)

    def decode(self, types, data):
        from eth_abi import decode as _dec
        return _dec(types, data)


class _FakeW3:
    """Just enough of a w3 for _resolve_debt_coverages: a codec and a contract whose
    encode_abi produces real selectors+args. multicall is monkeypatched, so no RPC."""

    def __init__(self):
        self.codec = _FakeCodec()
        self.eth = self

    def contract(self, address=None, abi=None):
        import web3
        return web3.Web3().eth.contract(address=address, abi=abi)


def _enc_addr(addr):
    from eth_abi import encode
    from web3 import Web3
    return encode(["address"], [Web3.to_checksum_address(addr)])


def _enc_dc(dc):
    from eth_abi import encode
    return encode(["uint256"], [int(round(dc * 1e18))])


def _patch_dc_cache(mod, monkeypatch):
    """Give the module a fresh dc cache so cases don't bleed into each other."""
    monkeypatch.setattr(mod, "_dc_cache", {})


def test_resolve_dc_tiered_success_avalanche(monkeypatch):
    """Avalanche/Arbitrum path: getAssetAddress resolves, tieredDebtCoverage(tier) returns a
    nonzero value, so the un-tiered fallback is never consulted."""
    _patch_dc_cache(DELTA, monkeypatch)
    addr = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"
    calls = {"batches": 0}

    def fake_multicall(w3, legs):
        calls["batches"] += 1
        if calls["batches"] == 1:  # getAssetAddress batch
            return [(True, _enc_addr(addr)) for _ in legs]
        if calls["batches"] == 2:  # tieredDebtCoverage batch — succeeds
            return [(True, _enc_dc(DC_MAJOR)) for _ in legs]
        # untiered batch — should not be needed, but return something harmless
        return [(True, _enc_dc(DC_RISKY)) for _ in legs]

    monkeypatch.setattr(DELTA, "multicall", fake_multicall)
    out = DELTA._resolve_debt_coverages(_FakeW3(), ["USDC", "ETH"], tier_code=1)
    assert out == {"USDC": pytest.approx(DC_MAJOR), "ETH": pytest.approx(DC_MAJOR)}


def test_resolve_dc_tiered_reverts_falls_back_untiered(monkeypatch):
    """Base path: tieredDebtCoverage reverts (success=False), so the resolver falls back to
    the un-tiered debtCoverage value per asset."""
    _patch_dc_cache(DEGEN, monkeypatch)
    addr = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"
    calls = {"batches": 0}

    def fake_multicall(w3, legs):
        calls["batches"] += 1
        if calls["batches"] == 1:  # getAssetAddress
            return [(True, _enc_addr(addr)) for _ in legs]
        if calls["batches"] == 2:  # tieredDebtCoverage — reverts on Base
            return [(False, b"") for _ in legs]
        return [(True, _enc_dc(DC_RISKY)) for _ in legs]  # untiered debtCoverage

    monkeypatch.setattr(DEGEN, "multicall", fake_multicall)
    out = DEGEN._resolve_debt_coverages(_FakeW3(), ["DEGEN"], tier_code=0)
    assert out == {"DEGEN": pytest.approx(DC_RISKY)}
    assert calls["batches"] == 3  # addr + tiered(revert) + untiered


def test_resolve_dc_unresolvable_symbol_is_zero(monkeypatch):
    """A symbol whose getAssetAddress returns the zero address (not listed) gets dc=0 — it
    then contributes nothing to the health meter, matching the contract skipping it."""
    _patch_dc_cache(DELTA, monkeypatch)

    def fake_multicall(w3, legs):
        # getAssetAddress returns zero address for the lone symbol; no further batches.
        return [(True, _enc_addr("0x0000000000000000000000000000000000000000")) for _ in legs]

    monkeypatch.setattr(DELTA, "multicall", fake_multicall)
    out = DELTA._resolve_debt_coverages(_FakeW3(), ["MYSTERY"], tier_code=0)
    assert out == {"MYSTERY": 0.0}


def test_resolve_dc_is_cached(monkeypatch):
    """Second lookup of the same (symbol, tier) is served from cache — multicall not re-hit."""
    _patch_dc_cache(DELTA, monkeypatch)
    addr = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"
    calls = {"n": 0}

    def fake_multicall(w3, legs):
        calls["n"] += 1
        b = calls["n"]
        if b == 1:
            return [(True, _enc_addr(addr)) for _ in legs]
        return [(True, _enc_dc(DC_MAJOR)) for _ in legs]

    monkeypatch.setattr(DELTA, "multicall", fake_multicall)
    w3 = _FakeW3()
    first = DELTA._resolve_debt_coverages(w3, ["USDC"], tier_code=1)
    n_after_first = calls["n"]
    second = DELTA._resolve_debt_coverages(w3, ["USDC"], tier_code=1)
    assert first == second
    assert calls["n"] == n_after_first  # no new batches on the cached call
