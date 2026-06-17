"""Shared named-wallet key resolution for the primecli siblings.

Single source of truth for the agent→(env_file, var) secrets map and the
helpers that read a private key out of it. degenprime and the bridge command
import from here so the map is defined once; deltaprime/arbprime carry their own
historical copies (kept for now to stay surgical — see the note below).

Security: never log or echo the values these helpers return. They are raw
private keys.
"""

from pathlib import Path

# Named-wallet table shared with deltaprime/arbprime/degenprime/bridge. Allows
# selecting a signer via `--as <agent>` (or the per-tool *_AGENT env vars) rather
# than passing a raw key through the environment.
AGENTS = {
    "parakletos":   ("/root/.openclaw/.env",                "PARAKLETOS_EVM_PRIVATE_KEY"),
    "paraklaudios": ("/root/paraklaudios/.credentials.env", "PARAKLAUDIOS_EVM_PRIVATE_KEY"),
    "core1":        ("/root/.openclaw/.env",                "BRUNO_CORE1_PRIVATE_KEY"),
}


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
    path, var = AGENTS[agent]
    key = _read_env_var(path, var)
    if not key:
        raise RuntimeError(f"{var} not found in {path} (agent '{agent}').")
    return key
