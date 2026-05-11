"""Human-and-agent-friendly stdout formatters for ``kros browse``.

Every subcommand's success output goes through one of the ``format_*``
helpers here. The resulting text is semi-structured — easy for an LLM
to grep/parse while still being human-readable in a terminal.

Conventions:

- Every block starts with a plain ``UPPERCASE:`` or ``--- SECTION ---``
  header on its own line, so grep-style extraction is trivial.
- Interactive elements are one per line, starting with ``ref=<int>`` so
  the agent can copy the number directly into ``--ref``.
- We never quote ``ref``; we do quote free-text names.
- We never output JSON by default. A ``--json`` flag is reserved for a
  future iteration.

These formatters consume **plain dicts** (the JSON payloads returned
over the IPC socket), not Pydantic models — keeps the CLI layer free of
model imports.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# page state (after click / fill / scroll / ...)
# ---------------------------------------------------------------------------


def format_page_state(state: dict) -> str:
    """Two-line summary: ``URL:`` + ``TITLE:``."""
    url = state.get("url") or ""
    title = state.get("title") or ""
    return f"URL:   {url}\nTITLE: {title}"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def format_read_result(r: dict) -> str:
    """Four-section snapshot: url/title + markdown + elements."""
    url = r.get("url") or ""
    title = r.get("title") or ""
    md = r.get("markdown") or ""
    elements = r.get("elements") or []
    truncated = bool(r.get("truncated"))

    parts: list[str] = [f"URL:   {url}", f"TITLE: {title}", "", "--- MARKDOWN ---"]
    parts.append(md.rstrip())
    if truncated:
        parts.append("")
        parts.append(
            "[markdown truncated — page is larger than the default budget; "
            "use `find --role X` or `wait --selector CSS` to focus]"
        )
    parts.append("")
    parts.append(f"--- ELEMENTS ({len(elements)}) ---")
    if not elements:
        parts.append("(none)")
    else:
        parts.extend(format_element_line(e) for e in elements)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def format_find_result(f: dict) -> str:
    elements = f.get("elements") or []
    if not elements:
        return "MATCHES: 0\n(none)"
    header = f"MATCHES: {len(elements)}"
    return "\n".join([header, *(format_element_line(e) for e in elements)])


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def format_session_info(info: dict) -> str:
    alive = info.get("alive")
    if not alive:
        return "ALIVE: no\n(no active browse session; run `kros browse open <url>` to start one)"
    lines = [
        "ALIVE: yes",
        f"DRIVER: {info.get('driver') or ''}",
        f"URL:   {info.get('url') or ''}",
        f"TITLE: {info.get('title') or ''}",
    ]
    dp = info.get("daemon_pid")
    bp = info.get("browser_pid")
    if dp:
        lines.append(f"DAEMON_PID: {dp}")
    if bp:
        lines.append(f"BROWSER_PID: {bp}")
    sp = info.get("socket_path")
    if sp:
        lines.append(f"SOCKET: {sp}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# element line
# ---------------------------------------------------------------------------


_BASE_ATTRS = ("type", "value", "placeholder", "checked", "href", "disabled")


def format_element_line(e: dict) -> str:
    """One element per line, optional fields only if set.

    Example::

        ref=42  role=button   name="Sign in"
        ref=37  role=textbox  name="Email"  type=email  value=""
    """
    ref = e.get("ref")
    role = e.get("role") or ""
    name = e.get("name") or ""
    parts = [f"ref={ref}", f"role={role}"]
    parts.append(f"name={_quote(name)}")
    for key in _BASE_ATTRS:
        val = e.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            parts.append(f"{key}={str(val).lower()}")
        elif isinstance(val, str):
            parts.append(f"{key}={_quote(val)}")
        else:
            parts.append(f"{key}={val}")
    return "  ".join(parts)


def _quote(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s)
    # Minimal shell-friendly quoting — enough to disambiguate whitespace
    # and keep the line grep-friendly. Escape backslash and quote chars.
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
