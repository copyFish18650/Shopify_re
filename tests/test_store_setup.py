import configparser
import os
import time
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from bot import ShopifyBot
from store_setup import StoreSetup
from shopify_admin import admin_store_base
from browser_recovery import PageLoadError


BASE = "https://admin.shopify.com/store/test-shop"
GENERAL = """
<button onclick="document.body.innerHTML='Settings closed'">Close</button>
<a href="/store/test-shop/settings/general/store-contact-details">My Store</a>
<button onclick="document.querySelector('#address').hidden=false">Store address</button>
<div id="address" role="dialog" hidden>
  <label>Company name<input></label>
  <label>Street and house number<input></label>
  <label>City<input></label>
  <label>Postal code<input></label>
  <label for="province">Province</label><select id="province"><option value="">Select</option><option value="M">Madrid</option></select>
  <button onclick="document.querySelector('#address').hidden=true">Cancel</button>
  <button onclick="localStorage.address=JSON.stringify(Array.from(document.querySelectorAll('#address input,#address select'),e=>e.value));document.querySelector('#address').hidden=true">Save</button>
</div>
"""
CONTACT = """
<a href="/store/test-shop/settings/general">General</a>
<test-field label="Store name" initial="My Store"></test-field>
<test-field label="Store email" initial="existing@example.com"></test-field>
<test-field label="Store phone" initial=""></test-field>
<button id="save" onclick="localStorage.contact=JSON.stringify(Array.from(document.querySelectorAll('test-field'),e=>e.shadowRoot.querySelector('input').value));this.hidden=true">Save</button>
<script>
customElements.define('test-field', class extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode:'open'}).innerHTML='<label for="field">'+this.getAttribute('label')+'</label><input id="field">';
    this.shadowRoot.querySelector('input').value=this.getAttribute('initial');
  }
});
</script>
"""


class StoreSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        kwargs = {"headless": True}
        if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            kwargs["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
        cls.browser = cls.playwright.chromium.launch(**kwargs)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=CONTACT if route.request.url.endswith("store-contact-details") else GENERAL
        ))
        self.page.goto(BASE + "/settings/general")
        config = configparser.ConfigParser()
        self.bot = ShopifyBot(config, {
            "AdsPower环境ID": "test", "邮箱": "new@example.com", "店铺名": "Test shop",
            "电话": "+34910000000", "地址": "Example street 1", "城市": "Madrid",
            "邮编": "28001", "州/省": "Madrid",
        }, None)
        self.bot.page = self.page
        self.bot._sleep = lambda **kwargs: None
        self.bot._log = Mock()
        self.setup = StoreSetup(self.bot)

    def tearDown(self):
        self.context.close()

    def test_new_contact_route_shadow_fields_and_address(self):
        self.setup.setup_store_profile()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.contact)"), [
            "Test shop", "existing@example.com", "+34910000000"
        ])
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.address)"), [
            "Test shop", "Example street 1", "Madrid", "28001", "M"
        ])

    def test_disabled_save_waits_for_request_to_finish(self):
        self.page.set_content('''<button onclick="this.disabled=true;
          setTimeout(() => {window.saved=true;this.hidden=true}, 350)">Save</button>''')
        self.setup._save_profile_changes(timeout=2000)
        self.assertTrue(self.page.evaluate("window.saved === true"))

    def test_disabled_save_without_completion_does_not_report_success(self):
        self.page.set_content('<button onclick="this.disabled=true">Save</button>')
        with self.assertRaisesRegex(PageLoadError, "保存尚未确认"):
            self.setup._save_profile_changes(timeout=100)
        self.assertTrue(self.page.get_by_role("button", name="Save").is_visible())

    def test_contact_retry_reuses_current_page_and_preserves_saved_values(self):
        self.page.goto(BASE + "/settings/general/store-contact-details")
        self.page.get_by_label("Store name", exact=True).fill("Established shop")
        self.page.get_by_label("Store phone", exact=True).fill("12345")
        self.page.get_by_role("button", name="Save", exact=True).click()
        self.bot.address = self.bot.city = ""
        with patch.object(self.setup, "_goto_admin", wraps=self.setup._goto_admin) as navigate, \
                patch.object(self.setup, "_save_profile_changes", wraps=self.setup._save_profile_changes) as save:
            self.setup.setup_store_profile()
        navigate.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.page.get_by_label("Store name", exact=True).input_value(), "Established shop")

    def test_ignored_return_link_opens_general_and_finishes_address(self):
        self.page.route("**/store-contact-details", lambda route: route.fulfill(
            content_type="text/html", body=CONTACT.replace(
                '<a href=', '<a onclick="event.preventDefault()" href=', 1
            )
        ))
        wait_for_url = self.page.wait_for_url

        def short_wait(url, **kwargs):
            return wait_for_url(url, **dict(kwargs, timeout=1000))

        with patch.object(self.page, "wait_for_url", side_effect=short_wait), \
                patch.object(self.page, "goto", wraps=self.page.goto) as goto, \
                patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.setup.setup_store_profile()
        goto.assert_called_once_with(BASE + "/settings/general", wait_until="commit", timeout=45000)
        reload.assert_not_called()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.address)"), [
            "Test shop", "Example street 1", "Madrid", "28001", "M"
        ])

    def test_ignored_return_link_does_not_discard_pending_save(self):
        self.page.goto(BASE + "/settings/general/store-contact-details")
        self.page.set_content('''<a href="/store/test-shop/settings/general"
          onclick="event.preventDefault()">General</a><button disabled>Save</button>''')
        wait_for_url = self.page.wait_for_url
        with patch.object(self.page, "wait_for_url", side_effect=lambda url, **kwargs:
                          wait_for_url(url, **dict(kwargs, timeout=100))), \
                patch.object(self.page, "goto", wraps=self.page.goto) as goto:
            with self.assertRaises(PlaywrightTimeout):
                self.setup._goto_admin("settings/general")
        goto.assert_not_called()
        self.assertTrue(self.page.url.endswith("store-contact-details"))

    def test_successful_return_link_keeps_spa_navigation(self):
        self.page.goto(BASE + "/settings/general/store-contact-details")
        with patch.object(self.page, "goto", wraps=self.page.goto) as goto:
            self.setup._goto_admin("settings/general")
        goto.assert_not_called()
        self.assertEqual(self.page.url, BASE + "/settings/general")

    def test_address_card_with_country_summary_and_no_province(self):
        general = GENERAL.replace('>Store address</button>', '>Store address <span>France</span></button>')
        general = general.replace('<label for="province">Province</label><select id="province"><option value="">Select</option><option value="M">Madrid</option></select>', '')
        self.page.route("**/settings/general", lambda route: route.fulfill(content_type="text/html", body=general))
        self.page.reload()
        self.setup.setup_store_profile()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.address)"), [
            "Test shop", "Example street 1", "Madrid", "28001"
        ])

    def test_address_link_with_country_summary(self):
        general = GENERAL.replace(
            '<button onclick="document.querySelector(\'#address\').hidden=false">Store address</button>',
            '<a href="#" onclick="event.preventDefault();document.querySelector(\'#address\').hidden=false">Store address Spain</a>'
        )
        self.page.route("**/settings/general", lambda route: route.fulfill(content_type="text/html", body=general))
        self.page.reload()
        self.setup.setup_store_profile()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.address)[1]"), "Example street 1")

    def test_legacy_inline_contact_fields(self):
        self.page.route("**/settings/general", lambda route: route.fulfill(
            content_type="text/html", body=CONTACT.replace('href="/store/test-shop/settings/general"', 'href="#"')
        ))
        self.page.reload()
        self.bot.address = self.bot.city = ""
        self.setup.setup_store_profile()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.contact)[0]"), "Test shop")

    def test_existing_contact_values_are_preserved_without_save(self):
        self.page.goto(BASE + "/settings/general/store-contact-details")
        self.page.get_by_label("Store name", exact=True).fill("Established shop")
        self.page.get_by_label("Store phone", exact=True).fill("12345")
        self.bot.address = self.bot.city = ""
        with patch.object(self.setup, "_open_store_contact_details"), patch.object(self.setup, "_save_profile_changes") as save:
            self.setup.setup_store_profile()
        save.assert_not_called()
        self.assertEqual(self.page.get_by_label("Store name", exact=True).input_value(), "Established shop")

    def test_missing_requested_profile_field_fails(self):
        self.page.set_content('<label>Store name<input value="Existing"></label>')
        with patch.object(self.setup, "_open_store_contact_details"):
            with self.assertRaisesRegex(RuntimeError, "找不到店铺资料字段"):
                self.setup.setup_store_profile()

    def test_admin_home_is_not_a_logged_in_store_even_with_store_links(self):
        self.page.goto("https://admin.shopify.com/")
        self.page.set_content('<nav><a href="/store/test-shop/products">Products</a></nav>')
        self.assertFalse(self.bot._looks_logged_in())
        self.assertEqual(self.bot.admin_url, "")
        self.bot.shop_name = "guessed-task-name"
        with self.assertRaisesRegex(RuntimeError, "不会用任务名称拼接"):
            StoreSetup(self.bot).admin_base()

    def test_store_route_without_loaded_navigation_is_not_logged_in(self):
        self.page.set_content('<h1>Loading</h1>')
        self.assertFalse(self.bot._looks_logged_in(timeout=100))
        self.page.set_content('<h1>Access denied</h1><a href="/store/test-shop/products">Try again</a>')
        self.assertFalse(self.bot._looks_logged_in(timeout=100))

    def test_loaded_store_records_actual_handle_not_task_name(self):
        self.bot.shop_name = "different-display-name"
        self.page.set_content('<nav><a href="/store/test-shop/products">Products</a></nav>')
        self.assertTrue(self.bot._looks_logged_in())
        self.assertEqual(self.bot.admin_url, BASE)
        setup = StoreSetup(self.bot)
        self.page.goto("https://accounts.shopify.com/login")
        self.assertEqual(setup.admin_base(), BASE)

    def test_navigation_must_belong_to_current_store(self):
        self.page.set_content('<nav><a href="/store/other-shop/products">Products</a></nav>')
        self.assertFalse(self.bot._looks_logged_in(timeout=100))

    def test_waits_for_delayed_store_navigation(self):
        self.page.set_content('''<h1>Loading</h1><script>setTimeout(() => {
          document.body.innerHTML='<nav><a href="/store/test-shop/products">Products</a></nav>';
        }, 100);</script>''')
        self.assertTrue(self.bot._wait_for_admin(timeout=2000))

    def test_welcome_page_refreshes_then_recognizes_loaded_admin(self):
        self.page.goto(BASE + "?welcome")
        self.page.set_content("<h1>Loading</h1>")
        self.page.route("**/store/test-shop?welcome", lambda route: route.fulfill(
            content_type="text/html", body='<nav><a href="/store/test-shop/products">Products</a></nav>'
        ))
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._wait_for_admin(timeout=150))
        reload.assert_called_once()
        self.assertEqual(self.bot.admin_url, BASE)

    def test_login_form_is_not_refreshed(self):
        self.page.goto("https://accounts.shopify.com/login")
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertFalse(self.bot._wait_for_admin(timeout=100))
        reload.assert_not_called()

    def test_loading_settings_page_recovers_after_refresh(self):
        requests = []

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(content_type="text/html", body='<h1>Loading</h1>' if len(requests) == 1 else
                          '<a href="/store/test-shop/settings/legal/refund">Refund</a>')

        self.page.route("**/settings/legal", serve)
        ready = lambda timeout: self.page.locator('a[href$="/refund"]').wait_for(state="visible", timeout=timeout)
        self.setup._open_loaded("settings/legal", "书面政策列表", ready, timeout=100)
        self.assertEqual(len(requests), 2)

    def test_settings_refresh_is_bounded_and_never_accepts_empty_page(self):
        self.page.route("**/settings/legal", lambda route: route.fulfill(body="Loading"))
        ready = lambda timeout: self.page.locator("#return-rules").wait_for(state="visible", timeout=timeout)
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            with self.assertRaisesRegex(PageLoadError, "重试 2 次"):
                self.setup._open_loaded("settings/legal", "退货规则入口", ready, timeout=100)
        self.assertEqual(reload.call_count, 2)

    def test_goto_error_after_commit_waits_for_ui_without_reloading(self):
        self.page.route("**/settings/legal", lambda route: route.fulfill(content_type="text/html", body='''
          <div>Loading</div><script>setTimeout(()=>document.body.innerHTML='<div id="ready">Ready</div>', 100)</script>
        '''))
        original = self.setup._goto_admin

        def interrupted(path, **kwargs):
            original(path, **kwargs)
            raise RuntimeError("Page.goto: net::ERR_ABORTED; maybe frame was detached?")

        with patch.object(self.setup, "_goto_admin", side_effect=interrupted) as navigate, \
                patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.setup._open_loaded("settings/legal", "政策列表",
                                    lambda timeout: self.page.locator("#ready").wait_for(timeout=timeout), timeout=300)
        navigate.assert_called_once()
        reload.assert_not_called()

    def test_connection_closed_gets_wait_budget_between_single_navigation_retries(self):
        started = []

        def serve(route):
            started.append(time.monotonic())
            if len(started) < 3:
                route.abort("connectionclosed")
            else:
                route.fulfill(content_type="text/html", body='''<div>Loading</div><script>
                  setTimeout(()=>document.body.innerHTML='<div id="ready">Ready</div>', 75)
                </script>''')

        self.page.route("**/settings/legal", serve)
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.setup._open_loaded("settings/legal", "政策列表",
                                    lambda timeout: self.page.locator("#ready").wait_for(timeout=timeout), timeout=200)
        self.assertEqual(len(started), 3)
        self.assertTrue(all(b - a >= .18 for a, b in zip(started, started[1:])))
        reload.assert_not_called()

    def test_final_navigation_error_still_checks_late_ready_content(self):
        attempts = []
        original = self.setup._goto_admin

        def interrupted(path, **kwargs):
            attempts.append(path)
            if len(attempts) < 3:
                self.page.goto("about:blank")
            else:
                original(path, **kwargs)
            raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")

        self.page.route("**/settings/legal", lambda route: route.fulfill(content_type="text/html", body='''
          <script>setTimeout(()=>document.body.innerHTML='<div id="ready">Ready</div>', 75)</script>
        '''))
        with patch.object(self.setup, "_goto_admin", side_effect=interrupted):
            self.setup._open_loaded("settings/legal", "政策列表",
                                    lambda timeout: self.page.locator("#ready").wait_for(timeout=timeout), timeout=200)
        self.assertEqual(len(attempts), 3)

    def test_admin_error_document_recovers_automatically_without_manual_login(self):
        self.bot.data["店铺后台"] = BASE
        self.page.goto("about:blank")
        attempts = []

        def serve(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.abort("connectionclosed")
            else:
                route.fulfill(content_type="text/html", body='<nav><a href="/store/test-shop/products">Products</a></nav>')

        self.page.route(BASE, serve)
        wait_admin = self.bot._wait_for_admin
        with patch.object(self.bot, "_wait_for_admin", side_effect=lambda: wait_admin(timeout=100)), \
                patch.object(self.bot, "_ask_user") as ask, patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.bot._ensure_admin()
        self.assertEqual(len(attempts), 2)
        ask.assert_not_called()
        reload.assert_not_called()

    def test_refresh_never_replays_form_writes(self):
        original = self.setup._open_loaded
        ready_calls = []

        def short_ready(path, label, ready):
            def check(timeout):
                ready_calls.append(label)
                if len(ready_calls) == 1:
                    raise PlaywrightTimeout("temporary loading")
                ready(timeout)
            return original(path, label, check, timeout=100)

        self.bot.address = self.bot.city = ""
        with patch.object(self.setup, "_open_loaded", side_effect=short_ready), \
                patch.object(self.setup, "_save_profile_changes", wraps=self.setup._save_profile_changes) as save:
            self.setup.setup_store_profile()
        save.assert_called_once()

    def test_setup_refuses_unconfirmed_admin_without_writing_policies(self):
        self.page.goto("https://admin.shopify.com/")
        with patch.object(StoreSetup, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "未执行资料和政策"):
                self.bot._finish_with_setup()
        run.assert_not_called()

    def test_existing_store_login_failure_does_not_start_new_registration(self):
        self.bot.data["店铺后台"] = BASE
        self.page.goto("https://accounts.shopify.com/login")
        self.page.route(BASE, lambda route: route.fulfill(
            content_type="text/html", body='<script>location.replace("https://accounts.shopify.com/login")</script>'
        ))
        wait_admin = self.bot._wait_for_admin
        with patch.object(self.bot, "_looks_logged_in", return_value=False), \
                patch.object(self.bot, "_wait_for_admin", side_effect=lambda: wait_admin(timeout=100)), \
                patch.object(self.bot, "_ask_user") as ask, patch.object(self.bot, "_goto_signup") as signup:
            with self.assertRaisesRegex(RuntimeError, "已有店铺后台"):
                self.bot._ensure_admin()
        ask.assert_called_once()
        signup.assert_not_called()

    def test_resource_progress_prevents_refresh_during_slow_bootstrap(self):
        requests = []

        def chunk(route):
            requests.append(route.request.url)
            route.fulfill(content_type="application/javascript", body=(
                'setTimeout(loadChunk, 150)' if len(requests) < 5 else
                'document.body.innerHTML=\'<nav><a href="/store/test-shop/products">Products</a></nav>\''
            ))

        self.page.route("https://cdn.shopify.com/slow-*.js", chunk)
        self.page.set_content('''<div id="app"></div><script>
          let n=0; function loadChunk(){let s=document.createElement('script');
          s.src='https://cdn.shopify.com/slow-'+(++n)+'.js';document.head.appendChild(s)}
          setTimeout(loadChunk, 100);
        </script>''')
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._wait_for_admin(timeout=400, max_timeout=2500))
        self.assertEqual(len(requests), 5)
        reload.assert_not_called()

    def test_reconnect_preserves_loading_page_and_checkpoints_store_before_ready(self):
        self.page.goto(BASE + "?welcome")
        self.page.set_content('''<h1>Loading</h1><script>setTimeout(() => {
          document.body.innerHTML='<nav><a href="/store/test-shop/products">Products</a></nav>';
        }, 200);</script>''')
        with patch.object(self.page, "goto", wraps=self.page.goto) as goto, \
                patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.bot._ensure_admin()
        goto.assert_not_called()
        reload.assert_not_called()
        self.assertEqual(self.bot.data["店铺后台"], BASE)
        self.assertEqual(self.bot.admin_url, BASE)

    def test_detecting_blank_store_remembers_address_without_accepting_login(self):
        self.page.set_content('<div id="app"></div>')
        self.bot._remember_admin()
        self.assertEqual(self.bot.data["店铺后台"], BASE)
        self.assertEqual(self.bot.admin_url, "")
        self.assertFalse(self.bot._looks_logged_in(timeout=100))

    def test_another_store_cannot_replace_saved_destination(self):
        self.bot.data["店铺后台"] = "https://admin.shopify.com/store/original"
        self.page.set_content('<nav><a href="/store/test-shop/products">Products</a></nav>')
        self.assertEqual(self.bot._remember_admin(), "")
        self.assertFalse(self.bot._looks_logged_in(timeout=100))
        self.assertTrue(self.bot.data["店铺后台"].endswith("/original"))

    def test_existing_store_loading_failure_uses_recovery_before_manual_input(self):
        self.bot.data["店铺后台"] = BASE
        with patch.object(self.bot, "_looks_logged_in", return_value=False), \
                patch.object(self.bot, "_wait_for_admin", return_value=False), \
                patch.object(self.bot, "_ask_user") as ask, patch.object(self.bot, "_goto_signup") as signup:
            with self.assertRaises(PageLoadError):
                self.bot._ensure_admin()
        ask.assert_not_called()
        signup.assert_not_called()

    def test_settings_http_503_recovers_after_refresh(self):
        requests = []

        def serve(route):
            requests.append(route.request.url)
            route.fulfill(status=503 if len(requests) == 1 else 200, content_type="text/html",
                          body="Unavailable" if len(requests) == 1 else '<div id="ready">Ready</div>')

        self.page.route("**/settings/legal", serve)
        self.setup._open_loaded("settings/legal", "政策列表", lambda timeout: self.page.locator("#ready").wait_for(timeout=timeout), timeout=100)
        self.assertEqual(len(requests), 2)

    def test_navigation_http_error_is_reported_before_policy_waits(self):
        self.page.route("**/settings/legal", lambda route: route.fulfill(status=403, body="Forbidden"))
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.setup._goto_admin("settings/legal")

    def policy_list(self, sale):
        self.page.goto(BASE + "/settings/legal")
        paths = [p for name, p in self.setup.POLICY_PATHS.items() if sale or name != "Terms of sale"]
        self.page.set_content('<div id="written-policies">' + ''.join(
            '<div id="{slug}"><a href="/store/test-shop/{path}">{slug}</a></div>'.format(
                slug=p.rsplit('/', 1)[-1], path=p
            ) for p in paths
        ) + '</div>')

    def test_missing_optional_sale_does_not_fail_six_policies(self):
        self.policy_list(sale=False)
        with patch.object(self.setup, "_goto_admin"), patch.object(self.setup, "_ensure_policy", return_value=None) as ensure:
            notes = self.setup.setup_written_policies()
        self.assertEqual(ensure.call_count, 6)
        self.assertIn("无 Terms of sale 入口", notes[0])

    def test_available_sale_is_still_processed(self):
        self.policy_list(sale=True)
        with patch.object(self.setup, "_goto_admin"), patch.object(self.setup, "_ensure_policy", return_value=None) as ensure:
            notes = self.setup.setup_written_policies()
        self.assertEqual(ensure.call_count, 7)
        self.assertEqual(ensure.call_args.args[0], "Terms of sale")
        self.assertEqual(notes, [])

    def test_explicit_sale_content_cannot_be_silently_skipped(self):
        self.policy_list(sale=False)
        self.setup.data["销售条款"] = "Requested sale terms"
        with patch.object(self.setup, "_goto_admin"), patch.object(self.setup, "_ensure_policy", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "表格已填写销售条款"):
                self.setup.setup_written_policies()

    def test_profile_warning_does_not_fail_completed_policies(self):
        with patch.object(self.setup, "_dismiss_modals"), patch.object(self.bot, "_handle_skip_offer"), \
                patch.object(self.setup, "setup_store_profile", side_effect=RuntimeError("地址页面加载失败")), \
                patch.object(self.setup, "setup_return_rules"), patch.object(self.setup, "setup_written_policies", return_value=[]):
            result = self.setup.run()
        self.assertTrue(result["ok"])
        self.assertIn("店铺资料提示（不影响成功）", result["setup_notes"])
        self.assertIn("地址页面加载失败", result["setup_notes"])


class AdminAddressTests(unittest.TestCase):
    def test_only_real_store_paths_on_exact_admin_origin_are_accepted(self):
        self.assertEqual(admin_store_base(BASE + "/settings/legal?x=1"), BASE)
        for url in ("https://admin.shopify.com/", "https://admin.shopify.com/store",
                    "https://admin.shopify.com/store/", "https://admin.shopify.com/?next=" + BASE,
                    "https://accounts.shopify.com/login?return_to=" + BASE,
                    "https://admin.shopify.com.example.com/store/test-shop",
                    "https://example.com/" + BASE, "http://admin.shopify.com/store/test-shop",
                    "https://someone@admin.shopify.com/store/test-shop", "https://[", None):
            with self.subTest(url=url):
                self.assertEqual(admin_store_base(url), "")


if __name__ == "__main__":
    unittest.main()
