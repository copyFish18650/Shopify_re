import configparser
import os
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from bot import ShopifyBot
from store_setup import StoreSetup


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

    def test_legacy_inline_contact_fields(self):
        self.page.route("**/settings/general", lambda route: route.fulfill(
            content_type="text/html", body=CONTACT.replace('href="/store/test-shop/settings/general"', 'href="#"')
        ))
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

    def policy_list(self, sale):
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

    def test_profile_failure_keeps_overall_result_failed(self):
        with patch.object(self.setup, "_dismiss_modals"), patch.object(self.bot, "_handle_skip_offer"), \
                patch.object(self.setup, "setup_store_profile", side_effect=RuntimeError("page unavailable")), \
                patch.object(self.setup, "setup_return_rules"), patch.object(self.setup, "setup_written_policies", return_value=[]):
            result = self.setup.run()
        self.assertFalse(result["ok"])
        self.assertIn("店铺资料失败", result["setup_notes"])


if __name__ == "__main__":
    unittest.main()
