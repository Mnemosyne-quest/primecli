"""Pytest bootstrap for the primecli test suite.

Puts the repo root on sys.path so `import primecli.deltaprime` resolves without
an editable install. The whole suite is pure/offline: it imports the modules and
exercises encoders, validators, and pure helpers. It must never make a network
call, build a real RPC connection, or sign/broadcast a transaction.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
