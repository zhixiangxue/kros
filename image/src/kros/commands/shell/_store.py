"""``~/.kros/jobs/`` storage layer for ``kros shell spawn / jobs / logs / kill``.

Spawned tasks live as plain directories under ``~/.kros/jobs/<task_id>/``::

    meta.json     {task_id, pid, pgid, cmd, cwd, started_at, timeout_sec}
    stdout        Captured stdout of the spawned task (append by child).
    stderr        Captured stderr of the spawned task (append by child).
    exit_code     Written by the wrapper script after the user command exits.
                  Existence of this file == "task has terminated" (see
                  design/04 §7 for why the wrapper writes this rather than
                  having the kros CLI wait()).

Why filesystem-as-KV (no daemon, no sqlite):
* Same philosophy as the audit ledger (~/.kros/log/) — one process writes,
  any later kros invocation can read.
* Crash-safe: a partially written meta.json is detected at read time
  (json.JSONDecodeError -> degrade to ``state: lost``).
* Zero dependency: the standard library is enough.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Optional

__all__ = [
    "TASK_ID_PREFIX",
    "DEFAULT_TIMEOUT_SEC",
    "JobsStore",
]

# Visual prefix that disambiguates kros task ids from raw OS pids
# (see design/04 §2). Six hex chars after the prefix gives ~16M ids,
# plenty for any spawn rate inside one container.
TASK_ID_PREFIX = "t_"
_TASK_ID_HEX_BYTES = 3  # secrets.token_hex(3) -> 6 hex chars

# spawn's default --timeout. The number is deliberately generous (10
# minutes) so short-lived agent tasks don't get killed mid-flight, while
# still bounding "I forgot to clean up" cases. Override per-call.
DEFAULT_TIMEOUT_SEC = 600

# How many bytes to read from the *end* of a log file when ``tail -n``
# isn't available (we still prefer ``tail`` because it's line-aware).
_TAIL_FALLBACK_BYTES = 64 * 1024


class JobsStore:
    """Thin wrapper around ``~/.kros/jobs/``.

    All methods are static-ish — there's no per-instance state. We use
    a class purely to namespace the helpers (and to make mocking easy
    in any future tests).
    """

    @staticmethod
    def root() -> Path:
        """Return ``~/.kros/jobs/``, creating it if missing."""
        path = Path.home() / ".kros" / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def new_task_id() -> str:
        """Generate a fresh task id like ``t_9x82f1``.

        Collision odds with 16M-space at our spawn rate are negligible;
        we still loop on the off chance the directory already exists
        (a leftover from a prior crashed run with the same id).
        """
        for _ in range(8):
            candidate = TASK_ID_PREFIX + secrets.token_hex(_TASK_ID_HEX_BYTES)
            if not (JobsStore.root() / candidate).exists():
                return candidate
        # 8 collisions in a row is essentially impossible — surface it.
        raise RuntimeError("kros: failed to allocate a unique task_id")

    @staticmethod
    def task_dir(task_id: str) -> Path:
        return JobsStore.root() / task_id

    @staticmethod
    def write_meta(task_id: str, meta: dict) -> None:
        path = JobsStore.task_dir(task_id) / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling tempfile and rename so a concurrent reader
        # never sees half-written JSON.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def read_meta(task_id: str) -> Optional[dict]:
        """Return the meta dict, or None if missing/corrupted."""
        path = JobsStore.task_dir(task_id) / "meta.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def read_exit_code(task_id: str) -> Optional[int]:
        """Return the int exit code if the wrapper has written it, else None.

        Existence of the file == "task terminated" (see design/04 §7).
        We tolerate trailing whitespace / partial writes by stripping;
        if it can't be parsed, return None and let the caller treat the
        task as ``lost``.
        """
        path = JobsStore.task_dir(task_id) / "exit_code"
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def list_task_ids() -> list[str]:
        """Return every ``t_*`` directory under the store, sorted by ctime asc.

        ctime — not name — because task ids are random hex; sorting by
        creation time gives a deterministic newest-last ordering that
        agents find more useful than alphabetical.
        """
        root = JobsStore.root()
        ids = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(TASK_ID_PREFIX)]
        ids.sort(key=lambda p: p.stat().st_ctime)
        return [p.name for p in ids]

    @staticmethod
    def is_alive(pid: int) -> bool:
        """Zero-signal liveness probe.

        ``os.kill(pid, 0)`` returns silently if the pid exists *and*
        we have permission; raises ESRCH if the pid is gone, EPERM if
        the pid exists but belongs to another uid (counts as alive —
        someone else's process, but extant).
        """
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as e:
            return e.errno == errno.EPERM

    @staticmethod
    def tail_file(path: Path, n_lines: int) -> bytes:
        """Return the last ``n_lines`` lines of ``path`` as bytes.

        Prefer GNU ``tail`` — it's line-aware and handles huge files
        without slurping. Fall back to a Python tail of the last 64KB
        if tail isn't on PATH (shouldn't happen in our image, but the
        store should still work in unit tests on a developer laptop).
        """
        if not path.exists():
            return b""
        try:
            out = subprocess.check_output(
                ["tail", "-n", str(int(n_lines)), str(path)],
                stderr=subprocess.DEVNULL,
            )
            return out
        except (FileNotFoundError, subprocess.CalledProcessError):
            return JobsStore._tail_python(path, n_lines)

    @staticmethod
    def read_full(path: Path) -> bytes:
        """``--full`` path of ``logs``: just slurp the file."""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return b""

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    @staticmethod
    def _tail_python(path: Path, n_lines: int) -> bytes:
        """Pure-Python fallback for ``tail`` — read up to 64KB from the end."""
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return b""
        with open(path, "rb") as f:
            if size > _TAIL_FALLBACK_BYTES:
                f.seek(size - _TAIL_FALLBACK_BYTES)
                # Drop the partial first line.
                f.readline()
            data = f.read()
        # Keep at most n_lines from the tail.
        lines = data.splitlines(keepends=True)
        return b"".join(lines[-n_lines:]) if lines else b""
