import unittest
from unittest.mock import Mock, patch

from browser_recovery import wait_for_ready
from playwright.sync_api import Error as PlaywrightError
from web_tasks import Cancelled


class LoadWaitTests(unittest.TestCase):
    """Exercise production wait budgets with a simulated slow network clock."""

    def setUp(self):
        self.seconds = 0
        self.listeners = {}
        self.events = []
        self.page = Mock()
        self.page.on.side_effect = lambda name, fn: self.listeners.update({name: fn})
        self.page.remove_listener.side_effect = lambda name, fn: self.listeners.pop(name)
        self.page.wait_for_timeout.side_effect = lambda ms: self.advance(ms / 1000)
        self.clock = patch("browser_recovery.time.monotonic", side_effect=lambda: self.seconds)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.log = Mock()

    def advance(self, seconds):
        self.seconds += seconds
        while self.events and self.events[0][0] <= self.seconds:
            _, event, value = self.events.pop(0)
            self.listeners[event](value)

    def ready_after(self, seconds):
        def ready(timeout):
            self.advance(timeout / 1000)
            return self.seconds >= seconds
        return ready

    def resource(self, **kwargs):
        values = {"url": "https://cdn.shopify.com/app.js?token=DO-NOT-LOG",
                  "resource_type": "script", "failure": None}
        values.update(kwargs)
        return Mock(**values)

    def test_slow_bootstrap_can_finish_after_old_twenty_second_limit(self):
        # Observed: a tiny script took ~114 s; more chunks arrived before the UI.
        self.events = [(s, "requestfinished", self.resource()) for s in (114, 215, 240)]
        self.assertTrue(wait_for_ready(self.page, self.ready_after(260), self.log, "后台"))
        self.assertLess(self.seconds, 262)
        self.page.reload.assert_not_called()
        self.assertFalse(self.listeners)

    def test_blank_page_without_progress_times_out(self):
        self.assertFalse(wait_for_ready(self.page, self.ready_after(999), self.log, "后台"))
        self.assertLess(self.seconds, 122)
        self.assertFalse(self.listeners)

    def test_progress_cannot_extend_the_five_minute_cap(self):
        self.events = [(s, "requestfinished", self.resource()) for s in range(30, 900, 30)]
        self.assertFalse(wait_for_ready(self.page, self.ready_after(999), self.log, "后台"))
        self.assertGreaterEqual(self.seconds, 300)
        self.assertLess(self.seconds, 302)

    def test_background_fetch_and_http_errors_do_not_keep_blank_page_alive(self):
        script = self.resource()
        for s in range(10, 250, 10):
            self.events.extend([(s, "requestfinished", self.resource(resource_type="fetch")),
                                (s, "response", Mock(request=script, status=503)),
                                (s, "requestfinished", script)])
        self.assertFalse(wait_for_ready(self.page, self.ready_after(999), self.log, "后台"))
        self.assertLess(self.seconds, 122)

    def test_proxy_failure_stops_wait_and_logs_no_resource_query(self):
        self.events = [(5, "requestfailed", self.resource(failure="net::ERR_SOCKS_CONNECTION_FAILED"))]
        with self.assertRaisesRegex(RuntimeError, "ERR_SOCKS_CONNECTION_FAILED"):
            wait_for_ready(self.page, self.ready_after(999), self.log, "后台")
        self.assertLess(self.seconds, 7)
        self.assertFalse(self.listeners)
        self.assertNotIn("DO-NOT-LOG", str(self.log.call_args_list))

    def test_manual_stop_interrupts_wait_and_removes_observers(self):
        def cancel():
            if self.seconds >= 5:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            wait_for_ready(self.page, self.ready_after(999), self.log, "后台", cancel)
        self.assertLess(self.seconds, 7)
        self.assertFalse(self.listeners)

    def test_redirect_to_login_exits_instead_of_waiting_five_minutes(self):
        self.assertFalse(wait_for_ready(self.page, self.ready_after(999), self.log, "后台",
                                        still_applicable=lambda: self.seconds < 5))
        self.assertLess(self.seconds, 7)

    def test_transient_navigation_exception_during_check_keeps_waiting(self):
        def ready(timeout):
            self.advance(timeout / 1000)
            if self.seconds < 10:
                raise RuntimeError("net::ERR_ABORTED; frame was detached")
            return True
        self.assertTrue(wait_for_ready(self.page, ready, self.log, "后台"))
        self.assertGreaterEqual(self.seconds, 10)
        self.page.reload.assert_not_called()
        self.assertFalse(self.listeners)

    def test_destroyed_execution_context_rechecks_without_refreshing(self):
        ready = Mock(side_effect=[PlaywrightError(
            "Locator.all: Execution context was destroyed, most likely because of a navigation"), True])
        self.assertTrue(wait_for_ready(self.page, ready, self.log, "店铺列表"))
        self.assertEqual(ready.call_count, 2)
        self.page.reload.assert_not_called()
        self.page.goto.assert_not_called()
        self.assertFalse(self.listeners)

    def test_missing_execution_context_respects_original_wait_budget(self):
        ready = Mock(side_effect=PlaywrightError('Cannot find context with specified id'))
        self.assertFalse(wait_for_ready(self.page, ready, self.log, "店铺列表", timeout=1000, max_timeout=1000))
        self.assertGreaterEqual(self.seconds, 1)
        self.assertLess(self.seconds, 1.3)
        self.page.reload.assert_not_called()
        self.assertFalse(self.listeners)

    def test_closed_browser_is_not_hidden_as_a_navigation_read_error(self):
        ready = Mock(side_effect=PlaywrightError('Target page, context or browser has been closed'))
        with self.assertRaisesRegex(PlaywrightError, 'has been closed'):
            wait_for_ready(self.page, ready, self.log, "店铺列表")
        self.assertEqual(ready.call_count, 1)
        self.assertFalse(self.listeners)

    def test_cancellation_interrupts_execution_context_recovery(self):
        ready = Mock(side_effect=PlaywrightError('Execution context was destroyed'))
        def cancel():
            if self.seconds >= .5:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            wait_for_ready(self.page, ready, self.log, "店铺列表", cancel)
        self.assertLess(self.seconds, 1)
        self.assertFalse(self.listeners)


if __name__ == "__main__":
    unittest.main()
