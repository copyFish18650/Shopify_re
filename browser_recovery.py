"""Bounded recovery for page loads and disconnected browser sessions."""
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout


class PageLoadError(RuntimeError):
    pass


class FirstPageUnavailable(RuntimeError):
    """Entry failed before signup; mark this task and let the queue advance."""


def browser_error_page(url):
    return (url or "").startswith(("chrome-error://", "edge-error://"))


def page_unavailable(error):
    """Stop the setup sequence when the page/session itself has failed."""
    return (isinstance(error, PageLoadError) or browser_disconnected(error)
            or "net::ERR_" in str(error))


class LoadProgress:
    """Observe bootstrap resources, excluding analytics and background polling."""

    def __init__(self, page):
        self.page = page
        self.last_progress = time.monotonic()
        self.completed = 0
        self.pending = set()
        self.http_errors = set()
        self.failures = []
        self.proxy_error = ""
        self.listeners = {
            "request": self._requested,
            "response": self._responded,
            "requestfinished": self._finished,
            "requestfailed": self._failed,
        }

    @staticmethod
    def relevant(request):
        host = (urlsplit(request.url).hostname or "").lower()
        return request.resource_type in ("document", "script", "stylesheet") and (
            host == "shopify.com" or host.endswith(".shopify.com")
            or host.endswith(".shopifycdn.com") or host.endswith(".shopifycdn.net")
        )

    def _requested(self, request):
        if self.relevant(request):
            self.pending.add(request)

    def _responded(self, response):
        if self.relevant(response.request):
            if response.status < 400:
                self.last_progress = time.monotonic()
            else:
                self.http_errors.add(response.request)
                self.failures.append("HTTP " + str(response.status))

    def _finished(self, request):
        if self.relevant(request):
            self.pending.discard(request)
            if request not in self.http_errors:
                self.completed += 1
                self.last_progress = time.monotonic()
            self.http_errors.discard(request)

    def _failed(self, request):
        if self.relevant(request):
            self.pending.discard(request)
            # Log only error codes: URLs/headers may contain account credentials.
            match = re.search(r"net::ERR_[A-Z_]+", request.failure or "")
            code = match.group(0) if match else "资源请求失败"
            self.failures.append(code)
            if not retryable_browser_error(code) and any(
                    token in code for token in ("ERR_SOCKS_", "ERR_PROXY_", "ERR_TUNNEL_")):
                self.proxy_error = code

    def describe(self):
        text = f"本轮收到 {self.completed} 个页面资源，待完成 {len(self.pending)} 个"
        if self.failures:
            text += "，最近错误：" + self.failures[-1]
        return text

    def __enter__(self):
        for event, listener in self.listeners.items():
            self.page.on(event, listener)
        return self

    def __exit__(self, *_):
        for event, listener in self.listeners.items():
            self.page.remove_listener(event, listener)


def wait_for_ready(page, ready, log, label, check_cancel=lambda: None,
                   timeout=120000, max_timeout=300000, still_applicable=lambda: True):
    """Wait for UI in short cancellable slices; extend only on resource progress.

    `ready(milliseconds)` must only inspect readiness, never submit form writes.
    Returning False or raising PlaywrightTimeout means the UI is not ready yet.
    """
    started = time.monotonic()
    deadline = started + max(timeout, max_timeout) / 1000
    next_log = started + 30
    last_error = ""
    with LoadProgress(page) as progress:
        while True:
            check_cancel()
            if not still_applicable():
                return False
            now = time.monotonic()
            remaining = min(deadline, progress.last_progress + timeout / 1000) - now
            if remaining <= 0:
                log(f"{label}等待结束，{progress.describe()}")
                return False
            try:
                if ready(max(1, min(1000, int(remaining * 1000)))) is not False:
                    return True
            except PlaywrightTimeout:
                pass
            except Exception as exc:
                if browser_disconnected(exc) or not retryable_browser_error(exc):
                    raise
                # A detached frame/aborted navigation is still part of this
                # wait budget; it must not turn into an immediate refresh loop.
                message = str(exc).splitlines()[0]
                if message != last_error:
                    log(f"{label}检查被导航打断，继续等待：{message}")
                    last_error = message
            if progress.proxy_error:
                raise RuntimeError("页面资源代理连接失败：" + progress.proxy_error)
            # Also pump browser events if a readiness check returns immediately.
            page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))
            now = time.monotonic()
            if now >= next_log:
                log(f"{label}仍在加载（已等 {int(now - started)} 秒），{progress.describe()}；暂不刷新")
                next_log = now + 30


def browser_disconnected(error):
    text = str(error).lower()
    return any(token in text for token in (
        "target page, context or browser has been closed", "target closed",
        "browser has been closed", "browser closed", "browser disconnected",
        "connection closed", "connection reset", "econnrefused", "websocket error",
    ))


def retryable_browser_error(error):
    text = str(error).lower()
    # Refreshing or reopening cannot repair rejected/expired proxy credentials.
    if any(token in text for token in ("err_socks_", "err_proxy_", "err_tunnel_connection_failed")):
        return False
    return isinstance(error, (PageLoadError, PlaywrightTimeout)) or browser_disconnected(error) or any(
        token in text for token in ("err_aborted", "frame was detached", "err_connection_reset",
                                  "err_connection_closed", "err_connection_timed_out", "timeout",
                                  "execution context was destroyed", "cannot find context with specified id")
    )


def reload_page(page, log, label, attempt, limit=2):
    log(f"{label}加载停滞，刷新页面后重试（{attempt}/{limit}）")
    try:
        response = page.reload(wait_until="commit", timeout=25000)
        if response and response.status >= 400:
            error_type = PageLoadError if response.status in (408, 429) or response.status >= 500 else RuntimeError
            raise error_type(f"刷新页面返回 HTTP {response.status}")
    except Exception as exc:
        if browser_disconnected(exc) or not retryable_browser_error(exc):
            raise
        # A navigation can be interrupted after the document commits. The
        # caller must still verify its required UI before doing any writes.
        log("刷新尚未完成，继续检查页面内容：" + str(exc).splitlines()[0])
        return exc
