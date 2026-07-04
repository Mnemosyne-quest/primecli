"""Shared named-wallet key resolution for the primecli siblings.

Single source of truth for the agent→(env_file, var) secrets map and the
helpers that read a private key out of it. degenprime and the bridge command
import from here so the map is defined once; deltaprime/arbprime carry their own
historical copies (kept for now to stay surgical — see the note below).

Security: never log or echo the values these helpers return. They are raw
private keys.
"""

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
# Tuple formats:
#   (env_path, env_var)           — raw key from env file (existing)
#   (seed_path, None, deriv_path) — HD-derived from seed file
# The third element (None vs deriv_path) distinguishes the two modes.
# ---------------------------------------------------------------------------

AGENTS = {
    # Raw key entries (from env files)
    "parakletos":   ("/root/.openclaw/.env",                "PARAKLETOS_EVM_PRIVATE_KEY"),
    "paraklaudios": ("/root/paraklaudios/.credentials.env", "PARAKLAUDIOS_EVM_PRIVATE_KEY"),
    "core1":        ("/root/.openclaw/.env",                "BRUNO_CORE1_PRIVATE_KEY"),

    # HD seed-derived entries (from Parakletos's BIP39 seed)
    # Seed file relocated 2026-07-04 (Bruno + Parakletos): workspace/config/wallet.seed -> /root/.openclaw/wallet.seed.
    "parakletos-2": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/0"),
    "parakletos-3": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/1"),
    "parakletos-4": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/2"),
    "parakletos-5": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/3"),
    "parakletos-6": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/0'/0/4"),
    "parakletos-7": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/1'/0/0"),
    "parakletos-8": ("/root/.openclaw/wallet.seed", None, "m/44'/60'/2'/0/0"),
}

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
