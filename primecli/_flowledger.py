"""Live external-flow ledger appends for the PnL flow ledgers.

Going-forward counterpart to scripts/pnl_backfill.py (which reconstructs history
by scanning Diamond event logs). When a fund or withdrawal-execute broadcast
succeeds, the calling tool appends one record here so the flow is captured at the
moment it happens — no rescan of the chain needed later. Borrow/repay and
internal swaps/rebalances are NOT flows (they don't cross the account boundary);
only the fund/withdraw paths call in (methodology: docs/yield-pnl-methodology.md §3).

The record schema and the `<chain>__<account.lower()>.jsonl` file naming are
IDENTICAL to what pnl_backfill writes, so pnlctl and the backfill consume one
shared ledger. Records carry: ts(int), type("deposit"|"withdraw"), asset(str),
token_amount(float), usd_value(float|None), tx(str), block(int), source(str),
and an optional log_index(int) — omitted for live appends (a broadcast tx has no
single log index to attribute the flow to; the backfill fills it from getLogs).

FAILURE-ISOLATED BY CONTRACT: append_flow never raises. A logging error must not
fail a financial operation, so every path catches and downgrades to a stderr
warning. The caller wraps the call defensively too — belt and suspenders.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_FLOW_LEDGER_DIR = "/root/defi-sims/state/flow-ledgers"


def _ledger_dir(ledger_dir=None) -> Path:
    if ledger_dir:
        return Path(ledger_dir)
    return Path(os.environ.get("FLOW_LEDGER_DIR", DEFAULT_FLOW_LEDGER_DIR))


def ledger_path(chain: str, account: str, ledger_dir=None) -> Path:
    """Path to the JSONL ledger for (chain, account). The account is lowercased so
    the filename matches pnl_backfill's `<chain>__<account.lower()>.jsonl`."""
    return _ledger_dir(ledger_dir) / f"{chain}__{account.lower()}.jsonl"


def _dedupe_key(rec: dict) -> tuple:
    """Identity of a live flow for idempotency: (tx, asset, type). A single fund or
    withdrawal-execute broadcast moves one asset in one direction, so this uniquely
    keys it without needing a log_index (which live appends don't carry)."""
    return (rec.get("tx"), rec.get("asset"), rec.get("type"))


def append_flow(chain: str, account: str, record: dict, ledger_dir=None) -> bool:
    """Append `record` to the (chain, account) flow ledger, idempotently.

    Dedupes on (tx, asset, type): if a record with the same key is already present,
    nothing is written (safe to call twice for the same broadcast). Creates the
    ledger directory if missing and keeps the file sorted ascending by ts.

    NEVER raises — any IO/serialisation error is swallowed with a stderr warning and
    returns False. Returns True if a new record was written, False otherwise (already
    present, or an error was swallowed).
    """
    try:
        path = ledger_path(chain, account, ledger_dir)
        path.parent.mkdir(parents=True, exist_ok=True)

        existing: list[dict] = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"  WARN flowledger: skipping malformed line in {path}",
                          file=sys.stderr)

        key = _dedupe_key(record)
        if any(_dedupe_key(r) == key for r in existing):
            return False  # already logged this flow — idempotent no-op

        merged = existing + [record]
        merged.sort(key=lambda r: (r.get("ts", 0), r.get("block", 0)))

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(json.dumps(r) for r in merged) + "\n")
        tmp.replace(path)
        return True
    except Exception as e:  # noqa: BLE001 — logging must NEVER break the caller
        print(f"  WARN flowledger: failed to append flow ({type(e).__name__}: {e})",
              file=sys.stderr)
        return False


def make_record(*, ts: int, ftype: str, asset: str, token_amount: float,
                usd_value, tx: str, block: int, source: str) -> dict:
    """Build a ledger record with exactly the keys pnl_backfill writes (minus the
    optional log_index, which live appends omit). Centralised so the three tools
    construct byte-identical records."""
    # web3.py 7.x HexBytes.hex() drops the 0x prefix; pnl_backfill normalises tx hashes
    # to a 0x-prefixed string, so do the same here or the (tx, …) dedupe would never
    # match a backfilled record for the same tx.
    if tx and not tx.startswith("0x"):
        tx = "0x" + tx
    return {
        "ts": int(ts),
        "type": ftype,
        "asset": asset,
        "token_amount": float(token_amount),
        "usd_value": (None if usd_value is None else float(usd_value)),
        "tx": tx,
        "block": int(block),
        "source": source,
    }
