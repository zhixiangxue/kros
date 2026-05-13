"""`kros browse` contract — the driver-neutral surface.

This module defines **what** ``kros browse`` means, independent of the
underlying headless browser. Three things live here:

1. **Data models** (Pydantic): ``Element`` / ``PageState`` / ``ReadResult`` /
   ``FindResult`` / ``SessionInfo``. Used by both the daemon (as driver
   return values) and the CLI (for formatting).

2. **Driver Protocol**: ``BrowseDriver`` — the 14-method contract every
   backend (``lightpanda_mcp`` today; future ``chromium_cdp``, ``browserbase``)
   must satisfy. The daemon holds one driver instance per session.

3. **IPC payload shapes**: ``Request`` / ``Response`` — the wire format
   between the short-lived CLI process and the long-lived session daemon
   over the unix socket. Intentionally plain dict / JSON-friendly so we
   don't need Pydantic on the hot IPC path.

Design stance: these 14 ops were distilled from Lightpanda's 21 MCP tools
but pruned to operations that have a **reasonable cross-backend semantic**.
Lightpanda-only niceties (``semantic_tree``, ``structuredData``, ...) are
not contract-level; drivers may synthesize them internally if it helps
implement ``read()``.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Element(BaseModel):
    """One interactive DOM element, addressable by ``ref``.

    ``ref`` is an opaque driver-assigned integer stable for the lifetime of
    the current page. For Lightpanda it is ``backendNodeId``; other drivers
    must supply their own stable int id.
    """

    model_config = ConfigDict(frozen=True)

    ref: int = Field(..., description="Driver-stable integer id for this element.")
    role: str = Field(..., description="ARIA role (button, link, textbox, ...).")
    name: str = Field("", description="Accessible name; may be empty.")

    # Optional attributes — only populated when meaningful. Absent fields
    # are omitted from the formatted CLI output.
    type: Optional[str] = None  # input type (email/password/submit/...)
    value: Optional[str] = None
    checked: Optional[bool] = None
    href: Optional[str] = None
    placeholder: Optional[str] = None
    disabled: Optional[bool] = None


class PageState(BaseModel):
    """The lightweight "after-action" state returned by every mutating op.

    click / fill / press / scroll / ... all return this so the agent can
    see what changed (URL navigated? title updated?) without a second
    round trip.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""


class ReadResult(BaseModel):
    """Full snapshot: ``read`` returns this in one go."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""
    markdown: str = ""
    elements: list[Element] = Field(default_factory=list)
    truncated: bool = False  # True if markdown was clipped to size budget


class FindResult(BaseModel):
    """Result of ``find --role X --name Y`` — a shortlist of candidates."""

    model_config = ConfigDict(frozen=True)

    elements: list[Element] = Field(default_factory=list)


class SessionInfo(BaseModel):
    """``info`` output: what's currently running."""

    model_config = ConfigDict(frozen=True)

    alive: bool
    url: str = ""
    title: str = ""
    driver: str = ""
    daemon_pid: Optional[int] = None
    browser_pid: Optional[int] = None
    socket_path: str = ""


# ---------------------------------------------------------------------------
# Driver protocol — the 14 atomic operations
# ---------------------------------------------------------------------------


class BrowseDriver(Protocol):
    """Every backend implementing ``kros browse`` satisfies this shape.

    Lifecycle on the daemon side:

        drv = <Driver>()
        drv.open(url)                    # spawn browser, navigate
        drv.read() / drv.click() / ...   # repeated while session alive
        drv.close()                      # tear down browser

    All methods may raise ``DriverError`` (subclass of ``RuntimeError``)
    on backend failures; the daemon catches and turns them into IPC
    error responses.
    """

    name: str

    # --- Tier 1: core 6 ---
    def open(self, url: str, *, timeout_ms: int = 5000) -> ReadResult: ...
    def read(self, *, selector: Optional[str] = None) -> ReadResult: ...
    def click(self, *, ref: int, timeout_ms: int = 5000) -> PageState: ...
    def fill(self, *, ref: int, value: str) -> PageState: ...
    def close(self) -> None: ...
    def info(self) -> SessionInfo: ...

    # --- Tier 2: high-value 4 ---
    def find(
        self, *, role: Optional[str] = None, name: Optional[str] = None
    ) -> FindResult: ...
    def wait(self, *, selector: str, timeout_ms: int = 5000) -> int: ...
    def scroll(
        self,
        *,
        ref: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> PageState: ...
    def eval(self, *, script: str) -> str: ...

    # --- Tier 3: completion 4 ---
    def press(self, *, key: str, ref: Optional[int] = None) -> PageState: ...
    def hover(self, *, ref: int) -> PageState: ...
    def select(self, *, ref: int, value: str) -> PageState: ...
    def check(self, *, ref: int, checked: bool) -> PageState: ...


# ---------------------------------------------------------------------------
# Exception hierarchy — part of the contract, shared by every driver.
#
# CLI only catches these; drivers translate backend-specific failures
# into the appropriate subclass. Transport-layer issues inside a driver
# (sockets, stdio pipes, HTTP) also surface as one of these.
# ---------------------------------------------------------------------------


class DriverError(RuntimeError):
    """Base class for every failure surfaced by a BrowseDriver."""


class NoSessionError(DriverError):
    """A stateful op was called but no session is active.

    CLI should hint the user to run ``kros browse open <url>`` first.
    """

    def __init__(self, msg: str = "no active browse session; run `kros browse open <url>` first") -> None:
        super().__init__(msg)


class SessionExistsError(DriverError):
    """``open`` was called while a session is already active.

    Refusing to silently replace state is an explicit design choice — the
    agent must ``close`` first, then ``open`` again.
    """

    def __init__(self, msg: str = "a browse session is already open; run `kros browse close` first") -> None:
        super().__init__(msg)


class NavigationTimeoutError(DriverError):
    """``open`` or ``click``-fallback-goto did not complete within timeout.

    Design intent: the agent supplied a ``--timeout`` (default 5s) as an
    explicit statement of how long it's willing to wait. When the budget
    is exceeded, we surface a *distinct* exception type so agents can:

    - retry the same op with a larger ``--timeout``,
    - switch to a different tool (``kros read --url`` for PDFs, plain
      ``curl`` for raw bytes),

    …instead of conflating this with "URL unreachable" or "lightpanda
    engine limitation on this page", which are :class:`DriverError`s.

    For ``click``, the driver best-effort restores the tab to the
    pre-click URL so the agent's session isn't stranded on about:blank.
    """


# ---------------------------------------------------------------------------
# IPC wire format — kros CLI <-> kros daemon, over unix socket, newline-delimited JSON
# ---------------------------------------------------------------------------

# Request:  {"op": "<method>", "args": {...}}
# Response: {"ok": true,  "result": <json>}   on success
#           {"ok": false, "error": "<msg>", "type": "<ExceptionName>"}  on failure
#
# One request → one response. Socket is reused across ops for one CLI call
# but the CLI reconnects for every kros browse invocation (daemon stays up).

# Every method on ``BrowseDriver`` is addressable by its name. The args
# dict is the keyword-argument payload; the daemon validates and dispatches.
VALID_OPS: frozenset[str] = frozenset(
    {
        "open",
        "read",
        "click",
        "fill",
        "close",
        "info",
        "find",
        "wait",
        "scroll",
        "eval",
        "press",
        "hover",
        "select",
        "check",
    }
)

# Special meta-op: daemon shutdown (sent by ``kros browse close``). The
# daemon runs driver.close() then exits and unlinks the socket.
OP_SHUTDOWN = "__shutdown__"


# ---------------------------------------------------------------------------
# Paths / env
# ---------------------------------------------------------------------------

#: Where we keep the unix socket and daemon pid file. Single session for now;
#: multi-session is not a goal (see design/01-kros-overview.md §D).
DEFAULT_RUNTIME_SUBDIR = ".kros/browse"
SOCKET_BASENAME = "session.sock"
PID_BASENAME = "session.pid"
DAEMON_LOG_BASENAME = "daemon.log"

#: Env overrides, all optional.
ENV_DRIVER = "KROS_BROWSE_DRIVER"  # default "lightpanda_mcp"
ENV_LIGHTPANDA_BIN = "KROS_BROWSE_LIGHTPANDA_BIN"  # override lightpanda binary
ENV_RUNTIME_DIR = "KROS_BROWSE_RUNTIME_DIR"  # override ~/.kros/browse
