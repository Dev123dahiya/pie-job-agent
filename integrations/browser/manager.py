"""Playwright browser service.

Playwright's sync API is bound to the thread that started it, while PIE's work arrives from
API worker threads and the agent thread. So one dedicated thread owns Playwright and every
live page; callers submit FIXED operations (open, snapshot, fill, upload, click submit,
screenshot, close) and wait for the result. There is no operation that runs caller-supplied
JavaScript, selectors or shell commands: fields are addressed only by the ids PIE assigned
during extraction (see dom.py).

Dry-run safety net: a DRY_RUN session aborts every non-GET/HEAD/OPTIONS request the page
makes, so even an accidental click cannot send a form or upload a file to the employer.
"""

import contextlib
import logging
import queue
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from integrations.browser.dom import COMBOBOX_VALUE, EXTRACT_FORM, FIND_PANEL
from integrations.browser.signals import domain_of, redact_url

logger = logging.getLogger(__name__)


def launch_channels(channel: str) -> list[str | None]:
    """Browsers to try, in order, for the BROWSER_CHANNEL setting. A named browser (e.g.
    "chrome") is the only one tried: PIE never silently falls back to another browser.
    "auto" = Playwright's bundled Chromium, else Microsoft Edge, else Google Chrome."""
    return [None, "msedge", "chrome"] if channel == "auto" else [channel]


T = TypeVar("T")

# Fixed page scripts for reading search results; their only input is a selectors-file spec.
READ_CARDS = """(spec) => [...document.querySelectorAll(spec.card)].map((card) => {
  const out = { id: spec.id_attr ? card.getAttribute(spec.id_attr) : null };
  for (const [name, f] of Object.entries(spec.fields)) {
    const el = f.css ? card.querySelector(f.css) : card;
    if (!el) { out[name] = null; continue; }
    const value = f.attr ? el.getAttribute(f.attr) : el.innerText;
    out[name] = value == null ? null : String(value).trim().slice(0, 2000);
  }
  return out;
})"""
RENDER_ALL = """async (card) => {
  for (const el of document.querySelectorAll(card)) {
    el.scrollIntoView({ block: "center" });
    await new Promise((r) => setTimeout(r, 150));
  }
}"""
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# What sits on top of an element's centre, if it is not the element itself (or inside it).
COVERED_BY = """(el) => {
  const r = el.getBoundingClientRect();
  const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  if (!top || el === top || el.contains(top)) return null;
  return (top.tagName + "." + String(top.className || "")).slice(0, 120);
}"""
# A page's own schema.org data (e.g. JobPosting with datePosted): read, never executed.
READ_LD_JSON = """() => [...document.querySelectorAll('script[type="application/ld+json"]')]
  .map((s) => s.textContent || "")"""
# GET a same-origin address from inside the page, with the page's own session: the same
# request the site's own search makes. Only a relative path is accepted (checked in Python).
FETCH_JSON = """async (path) => {
  const r = await fetch(path, { method: "GET", credentials: "same-origin" });
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  return { status: r.status, data };
}"""


# LinkedIn's confirmation after Submit: "Your application was sent to X" (2026-10-04) or
# "Application submitted" (2026-10-05).
LI_SENT = re.compile(r"application (was )?sent|application submitted", re.I)


def _css_text(value: str) -> str:
    """A value safe inside a double-quoted CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class BrowserError(RuntimeError):
    """A browser operation failed (navigation, timeout, crash, missing element...)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        # NAVIGATION | TIMEOUT | CRASHED | NOT_FOUND | UPLOAD | UNAVAILABLE | NOT_CLICKED (stopped
        # before an irreversible click: nothing was sent)
        self.kind = kind


@dataclass
class Snapshot:
    url: str
    title: str
    http_status: int | None
    fields: list[dict[str, Any]]
    submit_buttons: list[dict[str, Any]]
    apply_buttons: list[dict[str, Any]]
    signals: dict[str, Any]
    blocked_requests: list[dict[str, str]] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return domain_of(self.url)


@dataclass
class UploadResult:
    names: list[str]
    blocked_requests: int  # non-GET requests the dry-run guard aborted during the upload


@dataclass
class _Worker:
    """One browser thread and everything it owns. A frozen worker is abandoned and a new
    one starts; the old thread tears down its own browser once its stuck call returns."""

    queue: "queue.Queue[tuple[Callable[[], Any], Future[Any]] | None]"
    thread: threading.Thread | None = None
    pw: Any = None
    browser: Any = None
    persistent: Any = None
    live: "dict[str, _Live]" = field(default_factory=dict)


@dataclass
class _Live:
    context: Any
    page: Any
    dry_run: bool
    owns_context: bool
    http_status: int | None = None
    blocked: list[dict[str, str]] = field(default_factory=list)


class BrowserManager:
    def __init__(
        self,
        *,
        channel: str = "auto",
        headless: bool = True,
        timeout_ms: int = 30_000,
        user_data_dir: Path | None = None,
    ) -> None:
        self.channel = channel
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_data_dir = user_data_dir
        self._lock = threading.Lock()
        self._worker: _Worker | None = None
        # Each browser thread sees only its own worker (thread-confined Playwright state).
        self._tls = threading.local()
        self.restarts = 0  # browsers abandoned after a freeze, since this process started

    # --- thread plumbing ---

    @property
    def _w(self) -> _Worker:
        """The current thread's worker. Only valid on a browser thread."""
        worker: _Worker = self._tls.worker
        return worker

    def _ensure_thread(self) -> _Worker:
        with self._lock:
            w = self._worker
            if w is None or w.thread is None or not w.thread.is_alive():
                w = _Worker(queue=queue.Queue())
                w.thread = threading.Thread(
                    target=self._loop, args=(w,), name="pie-browser", daemon=True
                )
                self._worker = w
                w.thread.start()
            return w

    def _abandon(self, w: _Worker) -> None:
        """A call on ``w`` froze. Later calls get a fresh browser thread at once instead of
        each waiting for its own timeout behind the stuck one. Calls still queued on ``w``
        fail now; the old thread closes its own browser when (if) its stuck call returns."""
        with self._lock:
            if self._worker is not w:
                return  # already replaced
            self._worker = None
            self.restarts += 1
        while True:
            try:
                item = w.queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[1].set_exception(
                    BrowserError("CRASHED", "The browser froze and was restarted")
                )
        w.queue.put(None)
        logger.warning("Browser froze; started a fresh one for the next job")

    def _loop(self, w: _Worker) -> None:
        self._tls.worker = w
        while True:
            item = w.queue.get()
            if item is None:
                break
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BrowserError as exc:
                fut.set_exception(exc)
            except Exception as exc:  # a Playwright error: a structured browser failure
                kind = "TIMEOUT" if "Timeout" in type(exc).__name__ else "BROWSER"
                fut.set_exception(BrowserError(kind, f"{type(exc).__name__}: {exc}"[:1500]))
            except BaseException as exc:  # delivered to the caller
                fut.set_exception(exc)
        self._teardown()

    def _call(self, fn: Callable[[], T], timeout: float | None = None) -> T:
        w = self._ensure_thread()
        fut: Future[T] = Future()
        w.queue.put((fn, fut))
        wait = timeout or (self.timeout_ms / 1000) * 4 + 30
        try:
            return fut.result(timeout=wait)
        except FutureTimeout as exc:  # a hung page: report it like any other browser failure
            fut.cancel()  # succeeds only if it never started (stuck behind an earlier call)
            self._abandon(w)
            message = (
                f"The browser did not respond within {wait:.0f}s; it was restarted for the next job"
            )
            raise BrowserError("TIMEOUT", message) from exc

    def shutdown(self) -> None:
        with self._lock:
            w, self._worker = self._worker, None
        if w is not None and w.thread is not None and w.thread.is_alive():
            w.queue.put(None)
            w.thread.join(timeout=30)

    # --- browser-thread internals ---

    def _launch(self) -> None:
        if self._w.browser is not None or self._w.persistent is not None:
            return
        from playwright.sync_api import sync_playwright

        self._w.pw = sync_playwright().start()
        chromium = self._w.pw.chromium
        channels = launch_channels(self.channel)
        last: Exception | None = None
        for ch in channels:
            kwargs: dict[str, Any] = {"headless": self.headless}
            if ch:
                kwargs["channel"] = ch
            try:
                if self.user_data_dir:
                    self._w.persistent = chromium.launch_persistent_context(
                        str(self.user_data_dir), **kwargs
                    )
                else:
                    self._w.browser = chromium.launch(**kwargs)
                return
            except Exception as exc:  # try the next installed browser
                last = exc
        raise BrowserError("UNAVAILABLE", f"No usable browser could be launched: {last}")

    def _teardown(self) -> None:
        for sid in list(self._w.live):
            self._close_live(sid)
        for obj in (self._w.persistent, self._w.browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                logger.debug("browser close failed", exc_info=True)
        if self._w.pw is not None:
            try:
                self._w.pw.stop()
            except Exception:
                logger.debug("playwright stop failed", exc_info=True)
        self._w.pw = self._w.browser = self._w.persistent = None

    def _close_live(self, sid: str) -> None:
        live = self._w.live.pop(sid, None)
        if live is None:
            return
        try:
            if live.owns_context:
                live.context.close()
            else:
                live.page.close()
        except Exception:
            logger.debug("context close failed", exc_info=True)

    def _get(self, sid: str) -> _Live:
        live = self._w.live.get(sid)
        if live is None or live.page.is_closed():
            raise BrowserError("CRASHED", "The browser page for this session is gone")
        return live

    def _snapshot(self, live: _Live) -> Snapshot:
        page = live.page
        fields: list[dict[str, Any]] = []
        submits: list[dict[str, Any]] = []
        applies: list[dict[str, Any]] = []
        signals: dict[str, Any] = {
            "iframes": [],
            "captcha_elements": 0,
            "password_inputs": 0,
            "otp_inputs": 0,
            "text": "",
        }
        for index, frame in enumerate(page.frames):
            try:
                data = frame.evaluate(EXTRACT_FORM, f"f{index}_")
            except Exception:  # cross-origin frame that is not ready, detached frame...
                continue
            for item in data["fields"]:
                item["frame"] = index
            fields += data["fields"]
            submits += [{**b, "frame": index} for b in data["submit_buttons"]]
            applies += [{**b, "frame": index} for b in data["apply_buttons"]]
            s = data["signals"]
            signals["iframes"] += s["iframes"]
            for k in ("captcha_elements", "password_inputs", "otp_inputs"):
                signals[k] += s[k]
            signals["text"] = (signals["text"] + "\n" + s["text"])[:40000]
        return Snapshot(
            url=redact_url(page.url) or "",
            title=(page.title() or "")[:500],
            http_status=live.http_status,
            fields=fields,
            submit_buttons=submits,
            apply_buttons=applies,
            signals=signals,
            blocked_requests=list(live.blocked),
        )

    def _frame(self, live: _Live, element_id: str) -> Any:
        try:
            index = int(element_id.split("_", 1)[0].lstrip("f"))
            return live.page.frames[index]
        except (ValueError, IndexError) as exc:
            raise BrowserError("NOT_FOUND", f"Unknown element id {element_id!r}") from exc

    # --- public operations (each runs on the browser thread) ---

    def open(self, sid: str, url: str, *, dry_run: bool) -> Snapshot:
        def op() -> Snapshot:
            self._launch()
            self._close_live(sid)
            if self._w.persistent is not None:
                context, owns = self._w.persistent, False
                page = context.new_page()
            else:
                context, owns = self._w.browser.new_context(accept_downloads=False), True
                page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            live = _Live(context=context, page=page, dry_run=dry_run, owns_context=owns)
            if dry_run:
                page.route("**/*", lambda route: self._guard(live, route))
            self._w.live[sid] = live
            last: Exception | None = None
            for _ in range(2):  # navigation is a safe read: one retry
                try:
                    response = page.goto(url, wait_until="domcontentloaded")
                    live.http_status = response.status if response else None
                    self._settle(page)
                    return self._snapshot(live)
                except Exception as exc:
                    last = exc
            kind = "TIMEOUT" if "Timeout" in type(last).__name__ else "NAVIGATION"
            raise BrowserError(kind, f"Could not load {redact_url(url)}: {last}")

        return self._call(op)

    def _guard(self, live: _Live, route: Any) -> None:
        request = route.request
        if request.method.upper() in SAFE_METHODS:
            route.continue_()
            return
        live.blocked.append({"method": request.method, "domain": domain_of(request.url)})
        route.abort()

    @staticmethod
    def _settle(page: Any) -> None:
        # Long-polling pages never go idle; the DOM is loaded already.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5_000)

    def start(self) -> None:
        """Launch the browser now (no page), so a browser that cannot start is reported before
        PIE creates a session or changes a job."""
        self._call(self._launch)

    def is_alive(self, sid: str) -> bool:
        def op() -> bool:
            live = self._w.live.get(sid)
            return live is not None and not live.page.is_closed()

        return self._call(op)

    def snapshot(self, sid: str) -> Snapshot:
        return self._call(lambda: self._snapshot(self._get(sid)))

    def click_apply(self, sid: str, apply_id: str) -> Snapshot:
        """Click an 'Apply' link/button to reveal the application form (a navigation)."""

        def op() -> Snapshot:
            live = self._get(sid)
            loc = self._frame(live, apply_id).locator(f'[data-pie-apply="{apply_id}"]')
            if loc.count() != 1:
                raise BrowserError("NOT_FOUND", "Apply button not found")
            loc.click()
            self._settle(live.page)
            return self._snapshot(live)

        return self._call(op)

    def fill(
        self, sid: str, field_id: str, kind: str, value: str | None, option: int | None
    ) -> bool:
        """Set one field and read it back. Returns whether the page now holds the value."""

        def op() -> bool:
            live = self._get(sid)
            frame = self._frame(live, field_id)
            if kind in ("radio", "checkbox_group"):
                target = frame.locator(f'[data-pie-field="{field_id}"][data-pie-option="{option}"]')
                if target.count() != 1:
                    raise BrowserError("NOT_FOUND", f"Option {option} of {field_id} not found")
                if target.is_visible():
                    target.check()
                else:  # a hidden input under a drawn stand-in (role=radio / label): click that
                    stand_in = target.locator(
                        "xpath=ancestor::*[@role='radio' or @role='checkbox' or self::label][1]"
                    )
                    if stand_in.count() != 1:
                        raise BrowserError("NOT_FOUND", f"Option {option} of {field_id} is hidden")
                    question = stand_in.get_attribute("aria-label") or ""
                    text = " ".join(stand_in.inner_text().split())
                    stand_in.click()
                    live.page.wait_for_timeout(500)
                    # LinkedIn marks the drawn option (aria-checked) and redraws the group, so
                    # PIE's own markers are gone: find the chosen option again by its text.
                    chosen = frame.locator("[role=radio][aria-checked='true'], "
                                           "[role=checkbox][aria-checked='true']")  # fmt: skip
                    if question:
                        chosen = chosen.and_(frame.locator(f'[aria-label="{_css_text(question)}"]'))
                    return chosen.filter(has_text=text).count() > 0 if text else False
                return bool(target.is_checked())
            loc = frame.locator(f'[data-pie-field="{field_id}"]')
            if loc.count() != 1:
                raise BrowserError("NOT_FOUND", f"Field {field_id} not found")
            if kind == "select":
                values = loc.evaluate("(el) => Array.from(el.options).map((o) => o.value)")
                loc.select_option(value=values[option or 0])
                return bool(loc.input_value() == values[option or 0])
            if kind == "checkbox":
                loc.set_checked(value == "true")
                return bool(loc.is_checked()) == (value == "true")
            loc.fill(value or "")
            loc.dispatch_event("change")
            loc.evaluate("(el) => el.blur()")
            return bool(loc.input_value() == (value or ""))

        return self._call(op)

    def upload(self, sid: str, field_id: str, path: Path) -> UploadResult:
        """Set the file on the input and report what happened: the file names the input now
        holds and how many requests the dry-run guard blocked while the page reacted."""

        def op() -> UploadResult:
            live = self._get(sid)
            loc = self._frame(live, field_id).locator(f'[data-pie-field="{field_id}"]')
            if loc.count() != 1:
                raise BrowserError("NOT_FOUND", f"File field {field_id} not found")
            blocked_before = len(live.blocked)
            try:
                loc.set_input_files(str(path))
            except Exception as exc:
                raise BrowserError("UPLOAD", f"Upload failed: {exc}") from exc
            live.page.wait_for_timeout(800)  # let the page's own upload handler (if any) run
            names: list[str] = loc.evaluate("(el) => Array.from(el.files).map((f) => f.name)")
            return UploadResult(names=names, blocked_requests=len(live.blocked) - blocked_before)

        return self._call(op)

    # --- custom dropdowns (react-select style comboboxes) ---

    @staticmethod
    def _listbox(frame: Any, loc: Any) -> Any:
        controls = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
        if controls:
            return frame.locator(f"id={controls}")
        return frame.locator("[role=listbox]").first

    def combobox_options(self, sid: str, field_id: str, search: str | None) -> dict[str, Any]:
        """Open the dropdown (typing ``search`` if it is searchable) and list its options."""

        def op() -> dict[str, Any]:
            live = self._get(sid)
            frame = self._frame(live, field_id)
            loc = frame.locator(f'[data-pie-field="{field_id}"]')
            if loc.count() != 1:
                raise BrowserError("NOT_FOUND", f"Dropdown {field_id} not found")
            selected = str(loc.evaluate(COMBOBOX_VALUE))
            loc.click()
            if (loc.get_attribute("aria-expanded") or "") != "true":
                with contextlib.suppress(Exception):
                    loc.press("ArrowDown")
            if search and loc.is_editable():  # only searchable dropdowns accept typing
                loc.fill(search)
            live.page.wait_for_timeout(300)
            box = self._listbox(frame, loc)
            options: list[str] = []
            if box.count():
                options = [t.strip() for t in box.locator("[role=option]").all_inner_texts()]
            return {"options": options, "selected": selected,
                    "expanded": loc.get_attribute("aria-expanded") == "true"}  # fmt: skip

        return self._call(op)

    def combobox_select(self, sid: str, field_id: str, index: int, expected: str) -> str:
        """Click option ``index`` (only if it still reads ``expected``); return the label the
        dropdown now shows, so the caller can verify the selection actually took effect."""

        def op() -> str:
            live = self._get(sid)
            frame = self._frame(live, field_id)
            loc = frame.locator(f'[data-pie-field="{field_id}"]')
            option = self._listbox(frame, loc).locator("[role=option]").nth(index)
            if option.count() != 1 or " ".join(option.inner_text().split()) != " ".join(
                expected.split()
            ):
                raise BrowserError("NOT_FOUND", "The dropdown options changed; nothing selected")
            option.click()
            live.page.wait_for_timeout(200)
            return str(loc.evaluate(COMBOBOX_VALUE))

        return self._call(op)

    def combobox_close(self, sid: str, field_id: str) -> None:
        def op() -> None:
            live = self._get(sid)
            loc = self._frame(live, field_id).locator(f'[data-pie-field="{field_id}"]')
            with contextlib.suppress(Exception):
                loc.press("Escape")

        self._call(op)

    def click_submit(self, sid: str, submit_id: str) -> Snapshot:
        """The ONE irreversible operation. Refused outright for dry-run sessions."""

        def op() -> Snapshot:
            live = self._get(sid)
            if live.dry_run:
                raise BrowserError("NOT_FOUND", "Dry-run sessions can never submit")
            loc = self._frame(live, submit_id).locator(f'[data-pie-submit="{submit_id}"]')
            if loc.count() != 1:
                raise BrowserError("NOT_FOUND", "Submit button not found")
            responses: list[int] = []
            live.page.on(
                "response",
                lambda r: responses.append(r.status) if r.request.is_navigation_request() else None,
            )
            loc.click()
            self._settle(live.page)
            if responses:
                live.http_status = responses[-1]
            return self._snapshot(live)

        return self._call(op)

    def screenshot(self, sid: str, path: Path, timeout: float | None = None) -> Path:
        """Full-page screenshot; password and one-time-code inputs are masked."""

        def op() -> Path:
            live = self._get(sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            page = live.page
            mask = [
                page.locator("input[type=password]"),
                page.locator('input[autocomplete="one-time-code"]'),
            ]
            page.screenshot(path=str(path), full_page=True, mask=mask)
            return path

        return self._call(op, timeout)

    # --- read-only platform pages (search results, job pages) -------------------------------

    def read_cards(
        self, url: str, spec: dict[str, Any], *, render_all: bool = False
    ) -> dict[str, Any]:
        """Open ``url`` in a new tab, read every card matching ``spec`` and close the tab.
        ``spec`` comes only from a platform's selectors file (never from the agent):
        {"card": css, "id_attr": name | None, "fields": {name: {"css": css, "attr": name}}}.
        ``render_all`` scrolls each card into view first (lists that draw lazily)."""

        def op() -> dict[str, Any]:
            self._launch()
            context = self._w.persistent or self._w.browser.new_context()
            page = context.new_page()
            try:
                page.set_default_timeout(self.timeout_ms)
                response = page.goto(url, wait_until="domcontentloaded")
                self._settle(page)
                page.wait_for_timeout(2500)
                if render_all:
                    page.evaluate(RENDER_ALL, spec["card"])
                items = page.evaluate(READ_CARDS, spec)
                text = page.inner_text("body")[:3000] if page.locator("body").count() else ""
                return {
                    "status": response.status if response else None,
                    "url": page.url,
                    "text": text,
                    "items": items,
                }
            finally:
                page.close()
                if context is not self._w.persistent:
                    context.close()

        return self._call(op)

    def read_page_text(self, url: str, limit: int = 12_000) -> dict[str, Any]:
        """Open ``url`` in a new tab and return its visible text (for job descriptions)."""

        def op() -> dict[str, Any]:
            self._launch()
            context = self._w.persistent or self._w.browser.new_context()
            page = context.new_page()
            try:
                page.set_default_timeout(self.timeout_ms)
                response = page.goto(url, wait_until="domcontentloaded")
                self._settle(page)
                page.wait_for_timeout(1500)
                text = page.inner_text("body") if page.locator("body").count() else ""
                ld_json = [x[:20_000] for x in page.evaluate(READ_LD_JSON)[:5]]
                return {"status": response.status if response else None, "url": page.url,
                        "text": text[:limit], "ld_json": ld_json}  # fmt: skip
            finally:
                page.close()
                if context is not self._w.persistent:
                    context.close()

        return self._call(op)

    def fetch_json(self, page_url: str, path: str, allowed_prefix: str) -> dict[str, Any]:
        """Open ``page_url`` (a logged-in page) and GET ``path`` from it, as the page's own
        search does. ``path`` must be a relative path starting with ``allowed_prefix`` (from a
        platform's selectors file): never another site, never anything but GET."""
        if not path.startswith(allowed_prefix) or not allowed_prefix.startswith("/"):
            raise BrowserError("NOT_FOUND", "That address is not an allowed search address")
        if "//" in path or "\\" in path:
            raise BrowserError("NOT_FOUND", "That address is not an allowed search address")

        def op() -> dict[str, Any]:
            self._launch()
            context = self._w.persistent or self._w.browser.new_context()
            page = context.new_page()
            try:
                page.set_default_timeout(self.timeout_ms)
                response = page.goto(page_url, wait_until="domcontentloaded")
                self._settle(page)
                page.wait_for_timeout(1500)
                out = page.evaluate(FETCH_JSON, path)
                text = page.inner_text("body")[:3000] if page.locator("body").count() else ""
                return {
                    "status": response.status if response else None,
                    "url": page.url,
                    "text": text,
                    "api_status": out.get("status"),
                    "data": out.get("data"),
                }
            finally:
                page.close()
                if context is not self._w.persistent:
                    context.close()

        return self._call(op)

    def click_platform_apply(
        self, url: str, button: str, expect_text: str, follow_up: str, applied_text: str = "Applied"
    ) -> dict[str, Any]:
        """LIVE, irreversible: open a platform job page, check the apply ``button`` reads
        exactly ``expect_text`` and click it once. Only called after the user's explicit
        "yes" in the review queue (backend/services/naukri_apply.py). The tab is left open:
        if the site asks follow-up questions, the user answers them there."""

        def op() -> dict[str, Any]:
            self._launch()
            context = self._w.persistent or self._w.browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.goto(url, wait_until="domcontentloaded")
            self._settle(page)
            page.wait_for_timeout(2500)  # pages finish drawing (and drop loaders) after load
            target = page.locator(button).first
            if target.count() == 0:
                raise BrowserError("NOT_CLICKED", "The apply button is not on the job page")
            before = " ".join(target.inner_text().split())
            if before.lower() != expect_text.lower():
                raise BrowserError(
                    "NOT_CLICKED", f"The button reads '{before}', not '{expect_text}'"
                )
            # Something on top of the button (a loader, a pop-up) would swallow the click:
            # wait for it to go; if it stays, stop WITHOUT clicking (nothing is sent).
            target.scroll_into_view_if_needed()
            covered: str | None = None
            for _ in range(15):
                covered = target.evaluate(COVERED_BY)
                if covered is None:
                    break
                page.wait_for_timeout(1000)
            if covered is not None:
                raise BrowserError(
                    "NOT_CLICKED", f"Something covers the apply button ({covered}); not clicked"
                )
            # A trial click runs every check without clicking. Instahyre wraps its button in an
            # <a> that trips Playwright's hit test although the page puts the button itself
            # on top (checked above): then the button itself is pressed, once.
            try:
                target.click(timeout=8_000, trial=True)
                trial_error = None
            except Exception as exc:
                trial_error = str(exc)
            if trial_error is None:
                target.click(timeout=15_000)
            elif "intercepts pointer events" in trial_error and target.evaluate(COVERED_BY) is None:
                target.dispatch_event("click")
            else:
                raise BrowserError(
                    "NOT_CLICKED", f"The apply button could not be clicked: {trial_error[:300]}"
                )
            # The site may take a while to turn the button into "Applied" (the old button
            # can even be replaced): wait for that before judging what happened.
            applied = page.locator("button").filter(
                has_text=re.compile(rf"^\s*{re.escape(applied_text)}\s*$", re.I)
            )
            waited = 0
            while waited < 20_000 and applied.count() == 0:
                page.wait_for_timeout(1000)
                waited += 1000
            after = page.locator(button).first
            is_applied = applied.count() > 0
            return {
                "url": page.url,
                "applied": is_applied,
                "button_after": applied_text
                if is_applied
                else (" ".join(after.inner_text().split()) if after.count() else None),
                "follow_up": not is_applied and page.locator(follow_up).count() > 0,
                "text": page.inner_text("body")[:3000] if page.locator("body").count() else "",
            }

        return self._call(op)

    # --- LinkedIn Easy Apply panel (a form drawn inside #interop-outlet's shadow root) --------

    _PANEL_STEP_BUTTONS = ("Next", "Review", "Back")  # the only buttons panel_click may press

    # LinkedIn gives step buttons a visible text, sometimes also a longer aria-label.
    _STEP_ARIA = {
        "Next": "Continue to next step",
        "Review": "Review your application",
        "Submit application": "Submit application",
    }

    def _step_button(self, page: Any, text: str) -> Any:
        by_text = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
        aria = self._STEP_ARIA.get(text)
        return by_text.or_(page.locator(f"button[aria-label='{aria}']")) if aria else by_text

    def _wait_for_step(self, page: Any, timeout_ms: int = 20_000) -> None:
        """LinkedIn draws the panel title first and its content later (a spinner meanwhile):
        wait until a step button (Next / Review / Submit application) is there, or a
        "sent" message. Gives up quietly; the caller then reports what it sees."""
        waited = 0
        while waited < timeout_ms:
            names = (*self._PANEL_STEP_BUTTONS, "Submit application")
            if any(self._step_button(page, n).count() for n in names):
                break
            if page.get_by_text(LI_SENT).count():
                break
            page.wait_for_timeout(500)
            waited += 500
        page.wait_for_timeout(500)

    def _panel(self, live: _Live) -> dict[str, Any]:
        page = live.page
        heading = page.locator("h2, h3").filter(has_text=re.compile(r"^\s*Apply to ")).first
        root = heading.evaluate_handle(FIND_PANEL) if heading.count() else None
        data = page.evaluate(EXTRACT_FORM, {"prefix": "f0_", "root": root})
        names = (*self._PANEL_STEP_BUTTONS, "Submit application")
        buttons = [n for n in names if self._step_button(page, n).count()]
        text = data["signals"]["ui_text"]
        return {
            "fields": data["fields"],
            "buttons": buttons,
            "heading": heading.inner_text().strip() if heading.count() else None,
            "ui_text": text,
            # Locators see inside the shadow root; body.innerText does not.
            "sent": page.get_by_text(LI_SENT).count() > 0,
            "url": redact_url(page.url) or "",
        }

    def li_open(
        self,
        sid: str,
        url: str,
        *,
        dry_run: bool,
        button_name: str,
        logged_out: tuple[tuple[str, ...], tuple[str, ...]] = ((), ()),
    ) -> dict[str, Any]:
        """Open a LinkedIn job page and its Easy Apply panel (opening sends nothing). The
        page is kept for the session. No request guard here: LinkedIn's own UI needs its
        requests; a dry run is safe because nothing ever clicks Submit for it."""

        def op() -> dict[str, Any]:
            self._launch()
            self._close_live(sid)
            if self._w.persistent is not None:
                context, owns = self._w.persistent, False
            else:
                context, owns = self._w.browser.new_context(), True
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            live = _Live(context=context, page=page, dry_run=dry_run, owns_context=owns)
            self._w.live[sid] = live
            response = page.goto(url, wait_until="domcontentloaded")
            live.http_status = response.status if response else None
            self._settle(page)
            button = page.get_by_role("button", name=button_name, exact=True).first
            with contextlib.suppress(Exception):  # the job page draws its buttons late
                button.wait_for(state="visible", timeout=10_000)
            if button.count() == 0:
                url_parts, texts = logged_out
                body = page.inner_text("body").lower() if page.locator("body").count() else ""
                if any(p in page.url for p in url_parts) or any(t in body for t in texts):
                    raise BrowserError("LOGIN", "LinkedIn shows the sign-in page: not logged in")
                raise BrowserError("NOT_FOUND", "No Easy Apply button on the job page")
            button.click()
            heading = page.locator("h2, h3").filter(has_text=re.compile(r"^\s*Apply to "))
            heading.first.wait_for(timeout=15_000)
            self._wait_for_step(page)
            return self._panel(live)

        return self._call(op)

    def panel_snapshot(self, sid: str) -> dict[str, Any]:
        return self._call(lambda: self._panel(self._get(sid)))

    def panel_click(self, sid: str, text: str) -> dict[str, Any]:
        """Press Next / Review / Back (never Submit) and read the next step."""
        if text not in self._PANEL_STEP_BUTTONS:
            raise BrowserError("NOT_FOUND", f"'{text}' is not a step button")

        def op() -> dict[str, Any]:
            live = self._get(sid)
            button = self._step_button(live.page, text).first
            if button.count() == 0:
                raise BrowserError("NOT_FOUND", f"No '{text}' button")
            button.click()
            live.page.wait_for_timeout(800)
            self._wait_for_step(live.page)
            return self._panel(live)

        return self._call(op)

    def panel_submit(self, sid: str) -> dict[str, Any]:
        """The ONE irreversible LinkedIn click. Refused outright for dry-run sessions."""

        def op() -> dict[str, Any]:
            live = self._get(sid)
            if live.dry_run:
                raise BrowserError("NOT_FOUND", "Dry-run sessions can never submit")
            button = self._step_button(live.page, "Submit application")
            if button.count() != 1:
                raise BrowserError("NOT_FOUND", "Submit button not found")
            button.click()
            live.page.wait_for_timeout(4000)
            return self._panel(live)

        return self._call(op)

    def panel_discard(self, sid: str, dismiss_label: str, discard_text: str) -> None:
        """Close the panel without submitting (Dismiss, then Discard if asked) and the page."""

        def op() -> None:
            live = self._w.live.get(sid)
            if live is None or live.page.is_closed():
                return
            page = live.page
            with contextlib.suppress(Exception):
                page.locator(f"button[aria-label='{dismiss_label}']").first.click(timeout=5000)
                page.wait_for_timeout(1000)
                discard = page.get_by_role("button", name=discard_text, exact=True)
                if discard.count():
                    discard.first.click(timeout=5000)
                    page.wait_for_timeout(800)
            self._close_live(sid)

        self._call(op)

    def close(self, sid: str) -> None:
        self._call(lambda: self._close_live(sid))

    def crash_for_test(self, sid: str) -> None:
        """Test hook: simulate a browser crash by closing the page underneath the session."""
        self._call(lambda: self._w.live[sid].page.close() if sid in self._w.live else None)


_default: BrowserManager | None = None
_default_lock = threading.Lock()


def get_browser_manager(settings: Any) -> BrowserManager:
    """Process-wide manager configured from Settings."""
    global _default
    with _default_lock:
        if _default is None:
            _default = BrowserManager(
                channel=settings.browser_channel,
                headless=settings.browser_headless,
                timeout_ms=settings.browser_timeout_ms,
                user_data_dir=settings.browser_user_data_dir,
            )
        return _default
