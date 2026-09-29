import configparser
import unittest
from unittest.mock import MagicMock, Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bot import ShopifyBot
from browser_recovery import FirstPageUnavailable, PageLoadError
from store_setup import StoreSetup
from web_tasks import Cancelled


BASE = "https://admin.shopify.com/store/recovery-test"


class BrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        config = configparser.ConfigParser()
        config.read_dict({"settings": {"close_browser_after": "true"}})
        self.ads = Mock()
        self.ads.start.return_value = "ws://localhost/test"
        self.bot = ShopifyBot(config, {"AdsPower环境ID": "original", "邮箱": "test@example.com",
                                      "店铺名": "test", "店铺后台": BASE}, self.ads)
        self.bot._log = Mock()
        self.bot._sleep = Mock()
        self.bot._ensure_admin = Mock(side_effect=lambda: setattr(self.bot, "admin_url", BASE))
        self.bot._finish_with_setup = Mock(return_value={"status": "success"})
        self.page = MagicMock()
        self.page.url = BASE
        self.page.is_closed.return_value = False
        self.context = MagicMock()
        self.context.pages = [self.page]
        self.browser = Mock(contexts=[self.context])
        self.driver = MagicMock()
        self.driver.__enter__.return_value.chromium.connect_over_cdp.return_value = self.browser
        self.playwright = patch("bot.sync_playwright", return_value=self.driver)
        self.playwright.start()
        self.addCleanup(self.playwright.stop)

    def test_disconnection_reopens_original_profile_and_continues(self):
        self.bot._finish_with_setup.side_effect = [RuntimeError("Target page, context or browser has been closed"),
                                                 {"status": "success"}]
        self.assertEqual(self.bot.register()["status"], "success")
        self.assertEqual([call.args for call in self.ads.start.call_args_list], [("original",), ("original",)])
        self.assertEqual(self.bot.data["店铺后台"], BASE)
        self.ads.stop.assert_called_once_with("original")

    def test_repeated_failure_is_bounded_and_does_not_close_window(self):
        self.bot._finish_with_setup.side_effect = PageLoadError("页面未加载")
        self.assertEqual(self.bot.register()["status"], "failed")
        self.assertEqual(self.ads.start.call_count, 3)
        self.ads.stop.assert_not_called()

    def test_first_page_failure_does_not_restart_environment(self):
        self.bot._ensure_admin.side_effect = FirstPageUnavailable('首屏等待 60 秒仍未加载')
        result = self.bot.register()
        self.assertEqual(result['failure_code'], 'first_page_unavailable')
        self.assertFalse(result['retryable'])
        self.ads.start.assert_called_once_with('original')
        self.ads.stop.assert_not_called()

    def test_returned_setup_timeout_triggers_recovery(self):
        self.bot._finish_with_setup.side_effect = [
            {"status": "failed", "retryable": True, "error": "政策列表未加载"}, {"status": "success"}]
        self.assertEqual(self.bot.register()["status"], "success")
        self.assertEqual(self.ads.start.call_count, 2)

    def test_proxy_auth_failure_does_not_loop_or_close_window(self):
        self.bot._finish_with_setup.side_effect = RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED")
        self.assertEqual(self.bot.register()["status"], "failed")
        self.ads.start.assert_called_once_with("original")
        self.ads.stop.assert_not_called()

    def test_manual_cancellation_is_not_recovered(self):
        self.bot._finish_with_setup.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            self.bot.register()
        self.ads.start.assert_called_once_with("original")
        self.ads.stop.assert_not_called()

    def test_disconnect_while_waiting_on_login_is_not_misclassified_as_manual_login(self):
        self.bot.page = self.page
        self.page.url = "https://accounts.shopify.com/login"
        self.page.wait_for_url.side_effect = RuntimeError("Target page, context or browser has been closed")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.bot._wait_for_admin(timeout=100)

    def test_closed_tab_is_replaced_without_reusing_other_store(self):
        self.page.is_closed.return_value = True
        self.context.new_page.return_value = MagicMock()
        self.assertEqual(self.bot.register()["status"], "success")
        self.context.new_page.assert_called_once()

    def test_reconnect_reuses_single_error_tab_in_original_profile(self):
        self.page.url = "chrome-error://chromewebdata/"
        self.assertEqual(self.bot.register()["status"], "success")
        self.assertIs(self.bot.page, self.page)
        self.context.new_page.assert_not_called()

    def test_failed_page_stops_setup_instead_of_trying_each_following_page(self):
        self.bot.page = self.page
        setup = StoreSetup(self.bot)
        setup._dismiss_modals = Mock()
        self.bot._handle_skip_offer = Mock()
        setup.setup_store_profile = Mock(side_effect=PageLoadError("net::ERR_CONNECTION_CLOSED"))
        setup.setup_return_rules = Mock()
        setup.setup_written_policies = Mock()
        with self.assertRaises(PageLoadError):
            setup.run()
        setup.setup_return_rules.assert_not_called()
        setup.setup_written_policies.assert_not_called()
        self.assertNotIn("profile", self.bot.data.get("_setup_completed", []))

    def test_saved_setup_steps_survive_disconnect(self):
        self.bot.page = self.page
        first = StoreSetup(self.bot)
        first._dismiss_modals = Mock()
        self.bot._handle_skip_offer = Mock()
        first.setup_store_profile = Mock()
        first.setup_return_rules = Mock()
        first.setup_written_policies = Mock(side_effect=RuntimeError("Browser disconnected"))
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            first.run()
        self.assertEqual(self.bot.data["_setup_completed"], ["profile", "return_rules"])
        second = StoreSetup(self.bot)
        second._dismiss_modals = Mock()
        second.setup_store_profile = Mock()
        second.setup_return_rules = Mock()
        second.setup_written_policies = Mock(return_value=[])
        self.assertTrue(second.run()["ok"])
        second.setup_store_profile.assert_not_called()
        second.setup_return_rules.assert_not_called()
        second.setup_written_policies.assert_called_once()

    def test_failed_required_step_is_never_checkpointed(self):
        self.bot.page = self.page
        setup = StoreSetup(self.bot)
        setup._dismiss_modals = Mock()
        self.bot._handle_skip_offer = Mock()
        setup.setup_store_profile = Mock()
        setup.setup_return_rules = Mock(side_effect=PlaywrightTimeout("save timeout"))
        setup.setup_written_policies = Mock(return_value=[])
        result = setup.run()
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertNotIn("return_rules", self.bot.data["_setup_completed"])


if __name__ == "__main__":
    unittest.main()
