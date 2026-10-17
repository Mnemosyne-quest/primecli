"""Authenticated-gateway selection for the RedStone fetcher (2026-10 decommission).

RedStone's public gateways are being switched off; the fleet must run against
authenticated gateways (url + apiKey pairs, x-api-key header) before the
2026-10-29 cutoff. These tests pin the selection order and the failure
semantics without touching the network.
"""

from __future__ import annotations

import importlib
import json

import pytest

MODULES = [
    importlib.import_module("primecli.deltaprime"),
    importlib.import_module("primecli.arbprime"),
    importlib.import_module("primecli.degenprime"),
]

AUTH = [
    {"url": "https://gw-a.example/", "apiKey": "key-a"},
    {"url": "https://gw-b.example", "apiKey": "key-b"},
]

PACKAGES = {"ETH": [{"dataFeedId": "ETH", "value": 1}]}


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return PACKAGES


def _install(monkeypatch, mod, handler):
    calls = []

    def fake_get(url, timeout=None, headers=None):
        calls.append({"url": url, "headers": headers or {}})
        return handler(len(calls), url)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def _reset_caches():
    for mod in MODULES:
        mod._redstone_gateway_cache = None
    yield
    for mod in MODULES:
        mod._redstone_gateway_cache = None


@pytest.mark.parametrize("mod", MODULES)
def test_unset_env_uses_legacy_hosts_only(monkeypatch, mod):
    monkeypatch.delenv("REDSTONE_AUTH_GATEWAYS", raising=False)
    calls = _install(monkeypatch, mod, lambda n, u: _Resp(200))
    assert mod._redstone_fetch_packages(use_cache=False) == PACKAGES
    assert len(calls) == 1
    assert calls[0]["url"].startswith(mod.REDSTONE_GATEWAYS[0])
    assert "x-api-key" not in calls[0]["headers"]


@pytest.mark.parametrize("mod", MODULES)
def test_auth_pairs_are_tried_in_order_with_header(monkeypatch, mod):
    monkeypatch.setenv("REDSTONE_AUTH_GATEWAYS", json.dumps(AUTH))
    calls = _install(monkeypatch, mod, lambda n, u: _Resp(200))
    mod._redstone_fetch_packages(use_cache=False)
    assert calls[0]["url"] == (
        "https://gw-a.example/data-packages/latest/" + mod.REDSTONE_DATA_SERVICE
    )
    assert calls[0]["headers"] == {"x-api-key": "key-a"}


@pytest.mark.parametrize("mod", MODULES)
def test_first_auth_pair_failure_falls_to_second(monkeypatch, mod):
    monkeypatch.setenv("REDSTONE_AUTH_GATEWAYS", json.dumps(AUTH))
    calls = _install(monkeypatch, mod, lambda n, u: _Resp(500 if n == 1 else 200))
    mod._redstone_fetch_packages(use_cache=False)
    hosts = [c["url"].split("/")[2] for c in calls]
    assert hosts[:2] == ["gw-a.example", "gw-b.example"]
    assert calls[1]["headers"] == {"x-api-key": "key-b"}


@pytest.mark.parametrize("mod", MODULES)
def test_malformed_env_falls_back_to_legacy(monkeypatch, mod):
    monkeypatch.setenv("REDSTONE_AUTH_GATEWAYS", "{not json")
    calls = _install(monkeypatch, mod, lambda n, u: _Resp(200))
    mod._redstone_fetch_packages(use_cache=False)
    assert calls and calls[0]["url"].startswith(mod.REDSTONE_GATEWAYS[0])


@pytest.mark.parametrize("mod", MODULES)
def test_all_failures_raise_with_last_error(monkeypatch, mod):
    monkeypatch.delenv("REDSTONE_AUTH_GATEWAYS", raising=False)
    _install(monkeypatch, mod, lambda n, u: _Resp(403))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        mod._redstone_fetch_packages(use_cache=False)


@pytest.mark.parametrize("mod", MODULES)
def test_cache_short_circuits(monkeypatch, mod):
    monkeypatch.delenv("REDSTONE_AUTH_GATEWAYS", raising=False)
    calls = _install(monkeypatch, mod, lambda n, u: _Resp(200))
    mod._redstone_fetch_packages()
    mod._redstone_fetch_packages()
    assert len(calls) == 1
