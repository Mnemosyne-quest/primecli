"""primecli - command-line tools for DeltaPrime (Avalanche + Arbitrum) and DegenPrime (Base)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
import json
import os
import sys
import urllib.request

try:
    __version__ = _pkg_version("primecli")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0+unknown"

_VERSION_CHECK_URL = "https://pypi.org/pypi/primecli/json"
_VERSION_TIMEOUT = 3  # seconds


def _parse_version(v: str) -> tuple:
    """Parse 'X.Y.Z' into a sortable tuple. Non-numeric suffixes become -inf."""
    parts = v.split(".")
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(-1)
    # Pad to 3 elements
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def check_version(suppress_flag: bool = False) -> None:
    """Print a one-line upgrade hint to stderr when the installed version is behind
    the latest on PyPI. Suppress with the env var PRIMECLI_NO_VERSION_CHECK=1,
    the CLI flag --no-version-check, or pass suppress_flag=True."""
    if suppress_flag or os.environ.get("PRIMECLI_NO_VERSION_CHECK") == "1":
        return
    if "--no-version-check" in sys.argv:
        return
    if __version__ in ("0.0.0+unknown",):
        return
    try:
        req = urllib.request.Request(_VERSION_CHECK_URL)
        with urllib.request.urlopen(req, timeout=_VERSION_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        latest = data.get("info", {}).get("version", "")
        if not latest:
            return
        installed = _parse_version(__version__)
        latest_v = _parse_version(latest)
        if installed < latest_v:
            print(
                f"⚠️  primecli {__version__} is outdated. Latest is {latest}. "
                f"Upgrade: pip install --upgrade primecli",
                file=sys.stderr,
            )
    except Exception:
        pass  # network failure or parse error → silent
