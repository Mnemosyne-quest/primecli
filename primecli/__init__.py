"""primecli - command-line tools for DeltaPrime (Avalanche) and DegenPrime (Base)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("primecli")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0+unknown"
