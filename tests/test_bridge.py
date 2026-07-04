"""Tests for the cross-chain bridge command (primecli.bridge).

Pure/offline like the rest of the suite: the one path that would hit the network
(LiFi quote) is monkeypatched, and key resolution is monkeypatched so no real
private key is read. Nothing here builds an RPC connection or signs a tx.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from primecli import bridge


# ── arg parsing ───────────────────────────────────────────────────────────────


def test_parser_basic():
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "avalanche", "--to", "base",
         "--token", "AVAX", "--amount", "1"]
    )
    assert args.agent == "core1"
    assert args.from_chain == "avalanche"
    assert args.to_chain == "base"
    assert args.token == "AVAX"
    assert args.amount == "1"
    assert args.to_token is None          # defaults to dest-native downstream
    assert args.to_address is None
    assert args.slippage == 1.0           # default cap
    assert args.execute is False          # dry-run by default
    assert args.poll is False


def test_parser_rejects_unknown_chain():
    with pytest.raises(SystemExit):
        bridge.build_parser().parse_args(
            ["--as", "core1", "--from", "ethereum", "--to", "base",
             "--token", "ETH", "--amount", "1"]
        )


def test_parser_requires_agent():
    with pytest.raises(SystemExit):
        bridge.build_parser().parse_args(
            ["--from", "avalanche", "--to", "base", "--token", "AVAX", "--amount", "1"]
        )


# ── token resolution ──────────────────────────────────────────────────────────


def test_resolve_native_token():
    t = bridge.resolve_token("avalanche", "avax")
    assert t["native"] is True
    assert t["address"] is None
    assert t["lifi"] == bridge.NATIVE_SENTINEL
    assert t["decimals"] == 18


def test_resolve_erc20_token():
    t = bridge.resolve_token("base", "usdc")
    assert t["native"] is False
    assert t["lifi"] == t["address"]
    assert t["decimals"] == 6


def test_resolve_unknown_token_refuses():
    with pytest.raises(SystemExit):
        bridge.resolve_token("base", "DOGE")


# ── hex/int tolerance (LiFi returns mixed types) ──────────────────────────────


@pytest.mark.parametrize("value,expected", [
    (123, 123),
    ("123", 123),
    ("0x7b", 123),
    ("0X7B", 123),
    (None, 0),
])
def test_hexint(value, expected):
    assert bridge._hexint(value) == expected


# ── preview math + slippage gate ──────────────────────────────────────────────


def _quote(to_amount, to_amount_min, tool="across", fee_usd=0.12, gas_usd=0.30):
    """Minimal LiFi-quote shape covering the fields build_preview reads."""
    return {
        "tool": tool,
        "estimate": {
            "toAmount": str(to_amount),
            "toAmountMin": str(to_amount_min),
            "feeCosts": [{"amountUSD": str(fee_usd)}],
            "gasCosts": [{"amountUSD": str(gas_usd)}],
        },
    }


def test_build_preview_fields():
    to_tok = bridge.resolve_token("base", "ETH")  # 18 decimals
    q = _quote(to_amount=10 ** 18, to_amount_min=int(0.995 * 10 ** 18))
    p = bridge.build_preview(q, "avalanche", "base",
                             bridge.resolve_token("avalanche", "AVAX"), to_tok,
                             Decimal("1"), 0.01)
    assert p["from_chain"] == "avalanche"
    assert p["to_chain"] == "base"
    assert p["from_token"] == "AVAX"
    assert p["to_token"] == "ETH"
    assert p["to_amount"] == Decimal(1)
    assert p["tool"] == "across"
    assert p["fee_usd"] == pytest.approx(0.12)
    assert p["gas_usd"] == pytest.approx(0.30)
    # 1.0 -> 0.995 is 0.5% implied slippage
    assert p["implied_slippage"] == pytest.approx(0.005, abs=1e-6)


def test_enforce_slippage_passes_within_cap():
    to_tok = bridge.resolve_token("base", "ETH")
    q = _quote(to_amount=10 ** 18, to_amount_min=int(0.995 * 10 ** 18))
    p = bridge.build_preview(q, "avalanche", "base",
                             bridge.resolve_token("avalanche", "AVAX"), to_tok,
                             Decimal("1"), 0.01)
    bridge.enforce_slippage(p)  # 0.5% < 1% cap → no raise


def test_enforce_slippage_refuses_over_cap():
    to_tok = bridge.resolve_token("base", "ETH")
    q = _quote(to_amount=10 ** 18, to_amount_min=int(0.97 * 10 ** 18))  # 3% slippage
    p = bridge.build_preview(q, "avalanche", "base",
                             bridge.resolve_token("avalanche", "AVAX"), to_tok,
                             Decimal("1"), 0.01)  # 1% cap
    with pytest.raises(SystemExit) as ei:
        bridge.enforce_slippage(p)
    assert "slippage" in str(ei.value).lower()


# ── self-bridge enforcement + full dry-run (mocked quote, no network) ──────────

SIGNER = "0x8282fb51649Ce5f474db3e274C47ed04C97b504B"


@pytest.fixture
def fake_signer(monkeypatch):
    """Make _agent_key return a deterministic key whose address is SIGNER, so no
    real secret is read and Account.from_key yields a known address."""
    # Private key for the SIGNER address above is not known; instead patch
    # Account.from_key to return a stub account with .address == SIGNER.
    class _Acct:
        address = SIGNER

        def sign_transaction(self, tx):  # never called in dry-run
            raise AssertionError("sign_transaction must not run in a dry-run test")

    # Register the test agent so the `args.agent not in AGENTS` gate in
    # bridge.run() passes regardless of environment. The built-in registry now
    # ships EMPTY (wallets load from an external config that isn't present in CI
    # / a fresh install), so the tests must supply their own agent rather than
    # rely on personal wallet data being baked into the package. The dummy entry
    # is never dereferenced — _agent_key is stubbed right below.
    monkeypatch.setattr(bridge, "AGENTS", {"core1": ("/dev/null", "CORE1_TEST_KEY")})
    monkeypatch.setattr(bridge, "_agent_key", lambda agent: "0x" + "11" * 32)
    monkeypatch.setattr(bridge.Account, "from_key", staticmethod(lambda key: _Acct()))
    return _Acct()


def test_run_refuses_foreign_to_address(fake_signer):
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "avalanche", "--to", "base",
         "--token", "AVAX", "--amount", "1",
         "--to-address", "0x000000000000000000000000000000000000dEaD"]
    )
    with pytest.raises(SystemExit) as ei:
        bridge.run(args)
    assert "self-bridge" in str(ei.value).lower()


def test_run_allows_own_to_address(fake_signer, monkeypatch):
    # Passing the signer's own address explicitly must be accepted (and reach the quote).
    captured = {}

    def _fake_quote(from_chain, to_chain, from_tok, to_tok, raw_amount, address, slippage):
        captured["address"] = address
        captured["raw_amount"] = raw_amount
        return _quote(to_amount=10 ** 18, to_amount_min=int(0.997 * 10 ** 18))

    monkeypatch.setattr(bridge, "lifi_quote", _fake_quote)
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "avalanche", "--to", "base",
         "--token", "AVAX", "--amount", "1", "--to-address", SIGNER]
    )
    bridge.run(args)  # dry-run: no execute, must not raise
    assert captured["address"] == SIGNER
    assert captured["raw_amount"] == 10 ** 18  # 1 AVAX at 18 decimals


def test_run_dryrun_core1_avax_to_base(fake_signer, monkeypatch, capsys):
    """The motivating use case: core1 bridges 1 AVAX (Avalanche) -> ETH (Base),
    dry-run. Mocked quote, asserts the preview is sane and nothing broadcasts."""
    def _fake_quote(from_chain, to_chain, from_tok, to_tok, raw_amount, address, slippage):
        assert from_chain == "avalanche" and to_chain == "base"
        assert from_tok["symbol"] == "AVAX" and to_tok["symbol"] == "ETH"
        assert to_tok["native"] is True  # default dest token = Base native gas token
        return _quote(to_amount=int(0.0123 * 10 ** 18),
                      to_amount_min=int(0.0122 * 10 ** 18), tool="across")

    monkeypatch.setattr(bridge, "lifi_quote", _fake_quote)
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "avalanche", "--to", "base",
         "--token", "AVAX", "--amount", "1"]
    )
    bridge.run(args)
    out = capsys.readouterr().out
    assert "BRIDGE (DRY-RUN)" in out
    assert "AVAX on avalanche" in out
    assert "ETH on base" in out
    assert "Dry-run only" in out
    assert "Broadcast" not in out  # nothing signed/sent


def test_run_dryrun_refuses_when_slippage_blown(fake_signer, monkeypatch):
    def _fake_quote(*a, **k):
        return _quote(to_amount=10 ** 18, to_amount_min=int(0.95 * 10 ** 18))  # 5%

    monkeypatch.setattr(bridge, "lifi_quote", _fake_quote)
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "avalanche", "--to", "base",
         "--token", "AVAX", "--amount", "1"]  # default 1% cap
    )
    with pytest.raises(SystemExit):
        bridge.run(args)


def test_run_refuses_same_chain(fake_signer):
    args = bridge.build_parser().parse_args(
        ["--as", "core1", "--from", "base", "--to", "base",
         "--token", "ETH", "--amount", "1"]
    )
    with pytest.raises(SystemExit) as ei:
        bridge.run(args)
    assert "same-chain" in str(ei.value).lower() or "swap" in str(ei.value).lower()
