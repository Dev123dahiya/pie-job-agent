"""C8: a frozen browser call restarts the browser instead of stalling every later step."""

import contextlib
import threading
import time

import pytest

from integrations.browser.manager import BrowserError, BrowserManager


def test_a_frozen_call_fails_alone_and_the_next_call_gets_a_fresh_thread() -> None:
    bm = BrowserManager()
    release = threading.Event()
    first = bm._call(lambda: threading.current_thread(), timeout=5)

    with pytest.raises(BrowserError) as exc:
        bm._call(lambda: release.wait(10), timeout=0.3)  # the page hangs
    assert exc.value.kind == "TIMEOUT" and "restarted" in str(exc.value)
    assert bm.restarts == 1

    started = time.monotonic()
    second = bm._call(lambda: threading.current_thread(), timeout=5)
    assert time.monotonic() - started < 2  # not stuck behind the frozen call
    assert second is not first and second.is_alive()

    release.set()  # the old thread finishes its stuck call, then shuts itself down
    first.join(timeout=5)
    assert not first.is_alive()
    bm.shutdown()


def test_calls_queued_behind_a_frozen_call_fail_at_once() -> None:
    bm = BrowserManager()
    release = threading.Event()
    errors: list[BrowserError] = []

    def queued() -> None:
        try:
            bm._call(lambda: "never runs", timeout=10)
        except BrowserError as exc:
            errors.append(exc)

    stuck = threading.Thread(target=lambda: _swallow(bm, release))
    stuck.start()
    time.sleep(0.1)
    waiter = threading.Thread(target=queued)
    waiter.start()
    stuck.join(timeout=5)
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert errors and errors[0].kind in ("CRASHED", "TIMEOUT")
    release.set()
    bm.shutdown()


def _swallow(bm: BrowserManager, release: threading.Event) -> None:
    with contextlib.suppress(BrowserError):
        bm._call(lambda: release.wait(10), timeout=0.5)
