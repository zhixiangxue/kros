"""Tab registry for ``kros browse`` — driver-neutral.

Every backend (``lightpanda_mcp`` today; ``chromium_cdp``, ``browserbase``
tomorrow) shares the same notion of "a tab" and the same on-disk
conventions for where that tab's state lives.

Layout under ``~/.kros/browse/`` (or whatever ``$KROS_BROWSE_RUNTIME_DIR``
points at)::

    <runtime_dir>/
    ├── counter         # monotonic int: next tab id to hand out
    ├── current         # absent if no current tab; else one integer line
    ├── .lock           # flock file guarding counter/current writes
    └── tabs/
        └── <tab-id>/   # each tab is a directory; driver picks what goes in
            ├── session.sock   (lightpanda_mcp)
            ├── session.pid    (lightpanda_mcp)
            └── daemon.log     (lightpanda_mcp)

Tab ids are monotonically increasing integers starting at 1; they are
never reused, so ``close``ing tab 3 and ``open``ing a new one gives you
tab 4 (same mental model as a browser's tab counter).

Why "never reused" is a hard invariant — **do not "fix" this**:

The primary consumer of ``kros browse`` is an LLM agent, whose context
may carry stale tab handles across turns. Consider::

    turn 1: open https://A           -> tab 3    (agent remembers "tab 3 = A")
    turn 5: close tab 3
    turn 9: open https://B           -> if we reused id, this is "tab 3" too
    turn 12: agent calls `read --tab 3`  expecting A, silently gets B

Id reuse turns a crisp error ("tab 3 not found") into a **silent content
swap** — the worst failure mode for a system that LLMs reason over. The
counter growing unbounded is pure cosmetics; monotonicity is correctness.

Python ints have no upper bound, the counter file is a single line of
ASCII, and nothing downstream cares about the magnitude. If humans ever
find a 6-digit tab id visually offensive, the escape hatch is a manual
``rm ~/.kros/browse/counter`` when no tabs are live — not code that
reuses ids automatically.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator, Optional

from kros.commands.browse.contract import DEFAULT_RUNTIME_SUBDIR, ENV_RUNTIME_DIR

# Top-level files under runtime_dir() that aren't part of the per-tab layout.
_COUNTER_BASENAME = "counter"
_CURRENT_BASENAME = "current"
_LOCK_BASENAME = ".lock"
_TABS_SUBDIR = "tabs"


# ---------------------------------------------------------------------------
# directory layout
# ---------------------------------------------------------------------------


def runtime_dir() -> Path:
    override = os.environ.get(ENV_RUNTIME_DIR)
    if override:
        return Path(override)
    return Path.home() / DEFAULT_RUNTIME_SUBDIR


def tabs_dir() -> Path:
    return runtime_dir() / _TABS_SUBDIR


def tab_dir(tab_id: int) -> Path:
    """Per-tab working directory. Driver decides what files go inside."""
    return tabs_dir() / str(tab_id)


# ---------------------------------------------------------------------------
# counter / current / lock
# ---------------------------------------------------------------------------


def _counter_path() -> Path:
    return runtime_dir() / _COUNTER_BASENAME


def _current_path() -> Path:
    return runtime_dir() / _CURRENT_BASENAME


def _lock_path() -> Path:
    return runtime_dir() / _LOCK_BASENAME


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Exclusive flock on ``<runtime_dir>/.lock`` for counter/current writes."""
    rd = runtime_dir()
    rd.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def allocate_next_tab_id() -> int:
    """Reserve a fresh tab id; never returns a value seen before.

    The "never reused" contract is load-bearing for LLM safety — see the
    module docstring for why reusing ids causes silent content swaps in
    agent context. Do not add a reset/recycle path here.

    Also creates the tab directory so the driver's daemon/engine has a
    valid cwd to chdir into.
    """
    with _locked():
        cp = _counter_path()
        try:
            n = int(cp.read_text().strip())
        except (OSError, ValueError):
            n = 0
        n += 1
        cp.write_text(f"{n}\n")
        tab_dir(n).mkdir(parents=True, exist_ok=True)
        return n


def read_current_tab() -> Optional[int]:
    try:
        txt = _current_path().read_text().strip()
    except OSError:
        return None
    if not txt:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def write_current_tab(tab_id: Optional[int]) -> None:
    """Set the current-tab pointer, or clear it if ``tab_id is None``."""
    with _locked():
        cp = _current_path()
        if tab_id is None:
            try:
                cp.unlink()
            except FileNotFoundError:
                pass
            return
        cp.write_text(f"{tab_id}\n")


def list_tab_ids() -> list[int]:
    """All tab ids that have an on-disk directory, sorted ascending.

    Includes dead tabs (driver crashed). The CLI filters for aliveness
    via the driver's ``info()`` before showing them to the user.
    """
    td = tabs_dir()
    if not td.exists():
        return []
    ids: list[int] = []
    for entry in td.iterdir():
        if not entry.is_dir():
            continue
        try:
            ids.append(int(entry.name))
        except ValueError:
            continue
    ids.sort()
    return ids
