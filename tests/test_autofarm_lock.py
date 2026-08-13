"""Regression tests for _autofarm_lock_held (2026-08-13 p2 collision guard)."""
import fcntl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from primecli.health_monitor import _autofarm_lock_held  # noqa: E402


def test_held_lock_detected(tmp_path):
    lock = tmp_path / "held.lock"
    f = open(lock, "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        assert _autofarm_lock_held(str(lock)) is True
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def test_free_lock_not_held(tmp_path):
    lock = tmp_path / "free.lock"
    lock.write_text("")
    assert _autofarm_lock_held(str(lock)) is False


def test_none_lock_never_defers():
    assert _autofarm_lock_held(None) is False


def test_missing_lock_file_not_held(tmp_path):
    assert _autofarm_lock_held(str(tmp_path / "nope.lock")) is False
