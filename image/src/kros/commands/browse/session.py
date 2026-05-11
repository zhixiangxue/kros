"""Interactive browse session: Playwright ``connect_over_cdp`` wrapper.

The :func:`browse_session` context manager mirrors ``doka.Sandbox`` semantics:

- If a CDP server is already listening on the target endpoint, connect
  to it and leave it running on exit.
- Otherwise spawn one via the driver, and terminate that child on exit.

The yielded tuple is ``(browser, context, page)`` — three Playwright
objects that `kros browse interact` exposes as globals to user scripts.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from .driver import CDPEndpoint, DriverProtocol, get_driver, get_endpoint


@contextmanager
def browse_session(
    *,
    driver: Optional[DriverProtocol] = None,
    endpoint: Optional[CDPEndpoint] = None,
    spawn_if_missing: bool = True,
) -> Iterator[Tuple[object, object, object]]:
    """Yield a live ``(browser, context, page)`` tuple backed by CDP."""
    # Deferred import: Playwright's sync API pulls in greenlet/asyncio plumbing
    # at import time. Keep it out of the hot path when `kros browse get` uses
    # the subprocess-based fast path and never actually touches CDP.
    from playwright.sync_api import sync_playwright

    drv = driver or get_driver()
    ep = endpoint or get_endpoint()

    spawned = None
    if not drv.is_alive(ep):
        if not spawn_if_missing:
            raise RuntimeError(
                f"No CDP server at {ep.host}:{ep.port} and spawn_if_missing=False. "
                f"Run `kros browse serve` in another shell, or unset KROS_BROWSE_CDP_* to auto-spawn."
            )
        spawned = drv.spawn_cdp_server(ep)

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ep.ws_url)
            try:
                # NOTE: on Lightpanda, `browser.new_context()` logs a one-line
                # `not_implemented: Target.createBrowserContext` warning to the
                # server's stderr and returns a context that shares the one
                # underlying browser context (hence cookie jar). That's fine
                # for single-session agent use; multi-tenant isolation is a
                # Lightpanda-side roadmap item, not ours to patch.
                ctx = browser.new_context()
                page = ctx.new_page()
                yield browser, ctx, page
            finally:
                browser.close()
    finally:
        if spawned is not None:
            spawned.terminate()
            try:
                spawned.wait(timeout=3)
            except Exception:
                spawned.kill()
