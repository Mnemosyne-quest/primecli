"""Tests for the externalised named-wallet registry in primecli._wallets.

The published package ships an EMPTY built-in registry; real wallets are loaded
at import time from an external JSON config resolved via $PRIMECLI_WALLETS_CONFIG
(default ~/.primecli/wallets.json). These tests exercise that loader in isolation
with tmp config files / monkeypatched env, plus the consolidation invariant that
deltaprime / arbprime / degenprime all resolve through the same shared registry.

Pure/offline: no config write ever contains a real key, and no test resolves a
real private key (that would read a secret + need the HD libs). Only wallet names
and non-secret metadata (paths, env-var names) are asserted on.
"""

from __future__ import annotations

import importlib
import json

import pytest

W = importlib.import_module("primecli._wallets")


# ──────────────────────────────────────────────────────────────────────────────
# Built-in registry ships empty (guards against personal data leaking into source)


def test_builtin_registry_ships_empty():
    assert W._BUILTIN_AGENTS == {}, (
        "the published package must ship an EMPTY built-in AGENTS registry — no "
        "personal wallet names / paths / env-var names in source"
    )


# ──────────────────────────────────────────────────────────────────────────────
# _entry_from_spec: JSON spec -> internal tuple shape


def test_entry_from_spec_raw_key():
    spec = {"env_file": "/x/.env", "env_var": "FOO_KEY"}
    assert W._entry_from_spec(spec) == ("/x/.env", "FOO_KEY")


def test_entry_from_spec_hd():
    spec = {"seed_path": "/x/wallet.seed", "derivation_path": "m/44'/60'/0'/0/0"}
    assert W._entry_from_spec(spec) == ("/x/wallet.seed", None, "m/44'/60'/0'/0/0")


@pytest.mark.parametrize(
    "spec",
    [
        {},                                   # empty
        {"env_file": "/x/.env"},              # missing env_var
        {"env_var": "FOO"},                   # missing env_file
        {"seed_path": "/x/s"},                # missing derivation_path
        {"derivation_path": "m/44'"},         # missing seed_path
        "not-a-dict",                          # wrong type
        None,
    ],
)
def test_entry_from_spec_rejects_malformed(spec):
    assert W._entry_from_spec(spec) is None


# ──────────────────────────────────────────────────────────────────────────────
# _load_external_agents: present / absent / malformed


def _write(tmp_path, obj):
    p = tmp_path / "wallets.json"
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
    return p


def test_load_present_both_entry_types(tmp_path):
    p = _write(
        tmp_path,
        {
            "raw": {"env_file": "/root/.openclaw/.env", "env_var": "RAW_KEY"},
            "hd": {"seed_path": "/root/.openclaw/wallet.seed", "derivation_path": "m/44'/60'/0'/0/7"},
        },
    )
    got = W._load_external_agents(p)
    assert got == {
        "raw": ("/root/.openclaw/.env", "RAW_KEY"),
        "hd": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/7"),
    }
    # HD detection uses the same predicate the module applies to HD_AGENTS.
    hd = {n for n, e in got.items() if len(e) == 3 and e[1] is None}
    assert hd == {"hd"}


def test_load_absent_file_is_empty(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert W._load_external_agents(missing) == {}


def test_load_malformed_json_warns_and_empty(tmp_path, capsys):
    p = _write(tmp_path, "{ this is not valid json ")
    assert W._load_external_agents(p) == {}
    err = capsys.readouterr().err
    assert "malformed wallets config" in err
    assert str(p) in err


def test_load_non_dict_toplevel_warns_and_empty(tmp_path, capsys):
    p = _write(tmp_path, [1, 2, 3])
    assert W._load_external_agents(p) == {}
    assert "must be a JSON object" in capsys.readouterr().err


def test_load_skips_malformed_entry_keeps_good(tmp_path, capsys):
    p = _write(
        tmp_path,
        {
            "good": {"env_file": "/x/.env", "env_var": "K"},
            "bad": {"env_file": "/x/.env"},  # missing env_var
        },
    )
    got = W._load_external_agents(p)
    assert got == {"good": ("/x/.env", "K")}
    err = capsys.readouterr().err
    assert "skipping malformed wallet entry 'bad'" in err


# ──────────────────────────────────────────────────────────────────────────────
# Config path resolution: env override vs default


def test_config_path_env_override(tmp_path, monkeypatch):
    p = _write(tmp_path, {"w": {"env_file": "/x/.env", "env_var": "K"}})
    monkeypatch.setenv("PRIMECLI_WALLETS_CONFIG", str(p))
    assert W._wallets_config_path() == p
    # No explicit arg => reads via the env-var-resolved path.
    assert W._load_external_agents() == {"w": ("/x/.env", "K")}


def test_config_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PRIMECLI_WALLETS_CONFIG", raising=False)
    from pathlib import Path

    assert W._wallets_config_path() == Path.home() / ".primecli" / "wallets.json"


# ──────────────────────────────────────────────────────────────────────────────
# Overlay semantics: external wins on collision; built-in-only survives


def test_overlay_external_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(
        W,
        "_BUILTIN_AGENTS",
        {
            "known": ("/builtin/.env", "BUILTIN_VAR"),
            "builtin-only": ("/builtin/.env", "ONLY_VAR"),
        },
    )
    p = _write(tmp_path, {"known": {"env_file": "/ext/.env", "env_var": "EXT_VAR"}})
    monkeypatch.setenv("PRIMECLI_WALLETS_CONFIG", str(p))
    reg = W._build_registry()
    assert reg["known"] == ("/ext/.env", "EXT_VAR")          # external wins
    assert reg["builtin-only"] == ("/builtin/.env", "ONLY_VAR")  # built-in survives


# ──────────────────────────────────────────────────────────────────────────────
# Consolidation: siblings resolve through the ONE shared registry


def test_siblings_share_the_registry():
    delta = importlib.import_module("primecli.deltaprime")
    arb = importlib.import_module("primecli.arbprime")
    degen = importlib.import_module("primecli.degenprime")
    # Same dict object — not per-file duplicates.
    assert arb.AGENTS is W.AGENTS
    assert delta.AGENTS is W.AGENTS
    assert degen.AGENTS is W.AGENTS
    # Same resolver functions too.
    assert arb._agent_key is W._agent_key
    assert delta._agent_key is W._agent_key
    assert degen._agent_key is W._agent_key
    assert arb._read_env_var is W._read_env_var
    assert delta._read_env_var is W._read_env_var


def test_agent_key_unknown_agent_still_raises():
    # Public behavior preserved: an unknown agent fails closed (no key read).
    with pytest.raises(RuntimeError):
        W._agent_key("definitely-not-a-real-agent")
