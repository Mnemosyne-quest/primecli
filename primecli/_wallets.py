"""Shared named-wallet key resolution for the primecli siblings.

Single source of truth for the agent→key-resolution registry and the helpers
that read a private key out of it. deltaprime, arbprime, degenprime and the
bridge command all import `AGENTS` / `_read_env_var` / `_agent_key` from here, so
the registry is defined exactly once.

The published package ships with an EMPTY built-in registry — no personal wallet
names, file paths, or env-var names live in the source. At import time the
registry is overlaid with entries from an external JSON config file (see
`_load_external_agents`), resolved from `$PRIMECLI_WALLETS_CONFIG` and defaulting
to `~/.primecli/wallets.json`. A plain path/wallet change is then just a config
edit — no version bump or PyPI release. Loading is fail-soft: a missing file
yields an empty overlay, and malformed JSON warns to stderr and is ignored
rather than crashing every invocation.

Security: never log or echo the values these helpers return — they are raw
private keys. Only wallet names and non-secret metadata (paths, env-var names)
ever appear in output.
"""

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# HD wallet derivation from a BIP39 seed file.
# The seed file is a plaintext mnemonic with tight permissions (chmod 600).
# The derivation functions NEVER log or print the mnemonic.
# ---------------------------------------------------------------------------

_HD_DERIVE_CACHE = {}


def _ensure_hd_libs():
    """Lazy-import HD derivation libs. Never echoes the seed."""
    global _HD_DERIVE_CACHE
    if "mnemonic" not in _HD_DERIVE_CACHE:
        from mnemonic import Mnemonic
        from bip32 import BIP32
        _HD_DERIVE_CACHE["mnemonic"] = Mnemonic
        _HD_DERIVE_CACHE["BIP32"] = BIP32


def _derive_private_key(seed_path: str, derivation_path: str) -> str:
    """
    Read the BIP39 seed file and derive the private key at `derivation_path`.
    NEVER logs or returns the mnemonic.
    """
    _ensure_hd_libs()
    Mnemonic = _HD_DERIVE_CACHE["mnemonic"]
    BIP32 = _HD_DERIVE_CACHE["BIP32"]

    raw = Path(seed_path).read_text().strip()
    if not raw:
        raise RuntimeError(f"Seed file {seed_path} is empty")
    words = raw.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise RuntimeError(
            f"Seed file {seed_path} has {len(words)} words; expected 12-24"
        )

    mnemo = Mnemonic("english")
    if not mnemo.check(raw):
        raise RuntimeError(f"Seed file {seed_path} contains an invalid mnemonic (checksum fail)")

    seed = mnemo.to_seed(raw)
    bip = BIP32.from_seed(seed)
    privkey = bip.get_privkey_from_path(derivation_path)
    return privkey.hex()


# ---------------------------------------------------------------------------
# Agent / wallet registry
#
# Internal tuple formats (what _agent_key / HD_AGENTS consume):
#   (env_path, env_var)           — raw key read from an env file
#   (seed_path, None, deriv_path) — HD-derived from a BIP39 seed file
# The third element (None vs deriv_path) distinguishes the two modes.
#
# The built-in registry ships EMPTY: personal wallet names / paths / env-var
# names do NOT live in the published source. Entries come from an external JSON
# config file, overlaid on top of the (empty) built-in at import time. See
# `_load_external_agents`.
# ---------------------------------------------------------------------------

# Default location for the external registry when $PRIMECLI_WALLETS_CONFIG is
# unset. A neutral path under the invoking user's home, so every agent that
# shares a home reads the same file regardless of which one runs the tool.
_DEFAULT_WALLETS_CONFIG = Path.home() / ".primecli" / "wallets.json"


def _wallets_config_path():
    """Resolve the external wallets-config path: $PRIMECLI_WALLETS_CONFIG if set,
    else the default ~/.primecli/wallets.json."""
    env = os.environ.get("PRIMECLI_WALLETS_CONFIG")
    return Path(env) if env else _DEFAULT_WALLETS_CONFIG


def _entry_from_spec(spec):
    """Convert one external JSON wallet spec into the internal tuple shape.

    Accepts either:
      {"env_file": "...", "env_var": "..."}          -> (env_file, env_var)
      {"seed_path": "...", "derivation_path": "..."} -> (seed_path, None, derivation_path)
    Returns None if the spec is not a recognised shape (caller skips + warns).
    """
    if not isinstance(spec, dict):
        return None
    if "seed_path" in spec and "derivation_path" in spec:
        return (str(spec["seed_path"]), None, str(spec["derivation_path"]))
    if "env_file" in spec and "env_var" in spec:
        return (str(spec["env_file"]), str(spec["env_var"]))
    return None


def _load_external_agents(config_path=None):
    """Load the external wallet registry from JSON. Fail-soft and never raises:

      * file missing             -> {} (silent — a fresh install with no config)
      * unreadable / bad JSON    -> {} + one-line stderr warning
      * top-level not an object  -> {} + warning
      * a single malformed entry -> that entry skipped + warning, others kept

    Only wallet names and non-secret paths / var-names are read here; the secrets
    themselves are read lazily by _agent_key when a key is actually resolved.
    """
    path = Path(config_path) if config_path is not None else _wallets_config_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        print(f"primecli: cannot read wallets config {path}: {e}", file=sys.stderr)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"primecli: malformed wallets config {path}: {e}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(
            f"primecli: wallets config {path} must be a JSON object of "
            f"name -> spec; ignoring.",
            file=sys.stderr,
        )
        return {}
    out = {}
    for name, spec in data.items():
        entry = _entry_from_spec(spec)
        if entry is None:
            print(
                f"primecli: skipping malformed wallet entry '{name}' in {path}",
                file=sys.stderr,
            )
            continue
        out[name] = entry
    return out


# Built-in registry ships EMPTY — see the module docstring. External entries win
# on a name collision, so a future non-empty built-in default would be an
# overridable fallback, not a hard override.
_BUILTIN_AGENTS = {}


def _build_registry():
    """Overlay the external config on top of the (empty) built-in registry."""
    return {**_BUILTIN_AGENTS, **_load_external_agents()}


AGENTS = _build_registry()

HD_AGENTS = {name for name, entry in AGENTS.items() if len(entry) == 3 and entry[1] is None}


def _read_env_var(path, var):
    """Return the value of `var` from a KEY=VALUE env file, or None if absent."""
    try:
        for line in Path(path).read_text().splitlines():
            s = line.strip()
            if s.startswith(var + "="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def _agent_key(agent):
    if agent not in AGENTS:
        raise RuntimeError(
            f"Unknown agent '{agent}'. Known agents: {', '.join(AGENTS)}. "
            f"Or set DEGENPRIME_PRIVATE_KEY, or DEGENPRIME_KEY_FILE."
        )
    entry = AGENTS[agent]

    # HD wallet derivation
    if len(entry) == 3 and entry[1] is None:
        seed_path, _, deriv_path = entry
        try:
            return _derive_private_key(seed_path, deriv_path)
        except RuntimeError as e:
            # Re-raise with the agent name for context, but never leak the seed
            raise RuntimeError(
                f"Failed to derive key for agent '{agent}' (deriv_path={deriv_path}): {e}"
            ) from e

    # Raw key from env file (existing logic)
    path, var = entry
    key = _read_env_var(path, var)
    if not key:
        raise RuntimeError(f"{var} not found in {path} (agent '{agent}').")
    return key
