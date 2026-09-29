import configparser
import os
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError, Locator

from bot import ShopifyBot
from browser_recovery import FirstPageUnavailable, PageLoadError
from web_tasks import Cancelled


SIGNUP = "https://accounts.shopify.com/signup?signup_strategy=password"
PASSWORD = '<label>Create a password<input type="password"></label><button>Create account</button>'
NAME_FIELDS = '<label>First name<input name="firstName" autocomplete="given-name"></label><label>Last name<input name="lastName" autocomplete="family-name"></label>'
NAME_FORM = NAME_FIELDS + PASSWORD + '''<script>
  const button = document.querySelector('button');
  const first = document.querySelector('[name=firstName]');
  const last = document.querySelector('[name=lastName]');
  button.disabled = true;
  document.addEventListener('input', () => {button.disabled = !first.value || !last.value});
  button.onclick = () => {
    sessionStorage.names = JSON.stringify([first.value, last.value]);
    sessionStorage.clicks = Number(sessionStorage.clicks || 0) + 1;
    document.body.innerHTML = '<input autocomplete="one-time-code">';
  };
</script>'''


def make_bot():
    bot = ShopifyBot(configparser.ConfigParser(), {
        "AdsPower环境ID": "test", "邮箱": "test@example.com", "店铺名": "test",
    }, None)
    bot._log = Mock()
    bot._sleep = Mock()
    return bot


class FirstRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.page = self.bot.page = Mock(url="about:blank")
        self.seconds = 0
        self.starts = []

        def navigate(*args, **kwargs):
            self.starts.append(self.seconds)
            self.seconds += 30
            raise PlaywrightTimeout("Page.goto: Timeout 30000ms exceeded")

        self.page.goto.side_effect = navigate
        self.page.wait_for_timeout.side_effect = lambda ms: setattr(self, "seconds", self.seconds + ms / 1000)
        self.clock = patch("bot.time.monotonic", side_effect=lambda: self.seconds)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_first_document_arriving_after_goto_timeout_is_not_requested_again(self):
        self.bot._entry_ready = Mock(side_effect=lambda timeout: self.seconds >= 90)
        self.bot._open_entry(SIGNUP, "普通页面")
        self.assertEqual(self.starts, [0])
        self.assertGreaterEqual(self.seconds, 90)
        self.page.reload.assert_not_called()

    def test_no_response_waits_full_budget_and_does_not_diagnose_expired_proxy(self):
        self.bot._entry_ready = Mock(return_value=False)
        with self.assertRaises(PageLoadError) as error:
            self.bot._open_entry(SIGNUP, "普通页面")
        self.assertEqual(len(self.starts), 2)
        self.assertGreaterEqual(self.starts[1], 180)
        self.assertNotIn("代理连不上", str(error.exception))

    def test_first_page_is_skipped_after_one_minute_without_reloading(self):
        self.bot._entry_ready = Mock(return_value=False)
        with self.assertRaisesRegex(FirstPageUnavailable, "60 秒"):
            self.bot._goto_signup(SIGNUP)
        self.assertEqual(self.starts, [0])
        self.assertGreaterEqual(self.seconds, 60)
        self.assertLess(self.seconds, 62)
        self.page.reload.assert_not_called()

    def test_first_page_ready_before_deadline_continues(self):
        self.bot._entry_ready = Mock(side_effect=lambda timeout: self.seconds >= 45)
        self.bot._goto_signup(SIGNUP)
        self.assertEqual(self.starts, [0])
        self.page.reload.assert_not_called()

    def test_expired_proxy_at_entry_is_skipped_without_retry(self):
        self.bot._entry_ready = Mock(return_value=False)
        self.page.goto.side_effect = RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED")
        with self.assertRaises(FirstPageUnavailable):
            self.bot._goto_signup(SIGNUP)
        self.page.goto.assert_called_once()
        self.page.reload.assert_not_called()

    def test_rate_limit_is_not_classified_as_a_slow_ip_to_skip(self):
        self.bot._entry_ready = Mock(return_value=False)
        self.page.goto.side_effect = None
        self.page.goto.return_value = Mock(status=429)
        with self.assertRaisesRegex(RuntimeError, "HTTP 429") as error:
            self.bot._goto_signup(SIGNUP)
        self.assertNotIsInstance(error.exception, FirstPageUnavailable)
        self.page.goto.assert_called_once()

    def test_manual_stop_during_first_request_wait_never_retries(self):
        self.bot._entry_ready = Mock(return_value=False)

        def cancel():
            if self.seconds >= 35:
                raise Cancelled()

        self.bot._check_cancel = cancel
        with self.assertRaises(Cancelled):
            self.bot._goto_signup(SIGNUP)
        self.assertEqual(len(self.starts), 1)


class SignupPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        kwargs = {"headless": True}
        if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            kwargs["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
        cls.browser = cls.driver.chromium.launch(**kwargs)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=PASSWORD))
        self.page.goto(SIGNUP)
        self.bot = make_bot()
        self.bot.page = self.page

    def tearDown(self):
        self.context.close()

    def test_new_environment_opens_signup_first_without_login_probe(self):
        signup = "https://accounts.shopify.com/signup"
        admin = "https://admin.shopify.com/store/existing-shop"
        requested = []

        def serve(route):
            requested.append(route.request.url)
            body = ('<nav><a href="/store/existing-shop/products">Products</a></nav>'
                    if route.request.url == admin else '<input type="email">')
            route.fulfill(content_type="text/html", body=body)

        self.page.route("**/*", serve)
        self.page.goto("about:blank")
        with patch.object(self.bot, "_handle_skip_offer", return_value=False), \
                patch.object(self.bot, "_handle_password_step", return_value=False), \
                patch.object(self.bot, "_handle_otp_step", return_value=False), \
                patch.object(self.bot, "_handle_email_step", side_effect=lambda: self.page.goto(admin) or True), \
                patch.object(self.bot, "_wait_for_admin") as wait:
            self.bot._ensure_admin()
        self.assertEqual(requested, [signup, admin])
        self.assertEqual(self.bot.admin_url, admin)
        wait.assert_not_called()

    def test_signup_redirect_to_existing_store_skips_registration(self):
        admin = "https://admin.shopify.com/store/existing-shop"
        self.page.route("https://accounts.shopify.com/signup", lambda route: route.fulfill(
            content_type="text/html", body='<script>location.replace("' + admin + '")</script>'
        ))
        self.page.route(admin, lambda route: route.fulfill(content_type="text/html",
            body='<nav><a href="/store/existing-shop/products">Products</a></nav>'))
        self.page.goto("about:blank")
        with patch.object(self.bot, "_handle_email_step") as email:
            self.bot._ensure_admin()
        email.assert_not_called()
        self.assertEqual(self.bot.admin_url, admin)

    def test_existing_login_page_is_reused_and_saved_password_submits_once(self):
        self.page.goto('https://accounts.shopify.com/login?rid=existing')
        self.page.set_content('''<label>Password<input type="password"></label><button disabled>Log in</button>
          <script>
          document.querySelector('input').oninput=()=>document.querySelector('button').disabled=false;
          document.querySelector('button').onclick=()=>{
            sessionStorage.password=document.querySelector('input').value;
            sessionStorage.clicks=Number(sessionStorage.clicks||0)+1;
            document.body.innerHTML='<input autocomplete="one-time-code">';
          };</script>''')
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto, \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.bot._goto_signup('https://accounts.shopify.com/signup')
            self.assertTrue(self.bot._handle_password_step())
        goto.assert_not_called()
        reload.assert_not_called()
        self.assertEqual(self.page.evaluate('sessionStorage.password'), self.bot.password)
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        self.assertTrue(any('已点击：Log in' in str(c) for c in self.bot._log.call_args_list))

    def test_wrong_login_password_stops_without_refresh_or_second_submission(self):
        self.page.goto('https://accounts.shopify.com/login')
        self.page.set_content('''<label>Password<input type="password"></label><button>Log in</button>
          <script>document.querySelector('button').onclick=()=>{
            sessionStorage.clicks=Number(sessionStorage.clicks||0)+1;
            document.querySelector('input').setAttribute('aria-invalid','true');
          };</script>''')
        with patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            with self.assertRaisesRegex(RuntimeError, '输入有误'):
                self.bot._handle_password_step()
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()

    def test_account_chooser_selects_only_task_email_and_keeps_session(self):
        self.page.goto('https://accounts.shopify.com/select?rid=current')
        self.page.set_content('''<h1>Choose an account</h1>
          <a href="/login?wrong">Other user<span>other@example.com</span></a>
          <a href="/login?correct">Task user<span>test@example.com</span></a>''')
        self.assertTrue(self.bot._entry_ready(timeout=100))
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto, \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.bot._goto_signup('https://accounts.shopify.com/signup')
            self.assertTrue(self.bot._handle_account_selection())
        self.assertEqual(self.page.url, 'https://accounts.shopify.com/login?correct')
        goto.assert_not_called()
        reload.assert_not_called()

    def test_account_chooser_does_not_use_another_account(self):
        self.page.goto('https://accounts.shopify.com/select')
        self.page.set_content('<h1>Choose an account</h1><a href="/login">other@example.com</a>')
        with self.assertRaisesRegex(RuntimeError, '未找到本任务邮箱'):
            self.bot._handle_account_selection()
        self.assertEqual(self.page.url, 'https://accounts.shopify.com/select')

    def test_account_profile_continues_to_merchant_signup_and_actual_store(self):
        profile = 'https://accounts.shopify.com/accounts/123/personal'
        admin = 'https://admin.shopify.com/store/new-shop'
        requested = []

        def serve(route):
            url = route.request.url
            requested.append(url)
            if url == 'https://accounts.shopify.com/signup':
                body = '<script>location.replace("' + profile + '")</script>'
            elif url == profile:
                body = '<h1>General</h1><div>Verification email sent</div><span>test@example.com</span>'
            elif url == 'https://admin.shopify.com/?no_redirect=true':
                body = ('<h1>Create your first online store</h1><button onclick=\'location.href="' +
                        admin + '"\'>Create store</button>')
            else:
                body = '<nav><a href="/store/new-shop/products">Products</a></nav>'
            route.fulfill(content_type='text/html', body=body)

        self.page.route('**/*', serve)
        self.page.goto('about:blank')
        with patch.object(self.bot, '_handle_password_step') as password:
            self.bot._ensure_admin()
        self.assertEqual(requested, ['https://accounts.shopify.com/signup', profile,
                                     'https://admin.shopify.com/?no_redirect=true', admin])
        self.assertEqual(self.bot.admin_url, admin)
        password.assert_not_called()

    def test_store_list_reuses_existing_store_without_clicking_create(self):
        self.page.goto('https://admin.shopify.com/?no_redirect=true')
        self.page.set_content('<a href="/store/already-created">Existing shop</a><button>Create store</button>')
        self.assertTrue(self.bot._entry_ready(timeout=100))
        with patch.object(self.bot, '_open_entry') as entry:
            self.assertTrue(self.bot._handle_store_selection())
        self.assertEqual(entry.call_args.args[0], 'https://admin.shopify.com/store/already-created')

    def test_store_entry_recovers_destroyed_context_without_another_navigation(self):
        entry_url = 'https://admin.shopify.com/?no_redirect=true'
        self.page.route(entry_url, lambda route: route.fulfill(content_type='text/html',
            body='<a href="/store/existing">Existing shop</a>'))
        self.page.goto('about:blank')
        original = Locator.evaluate_all
        reads = []

        def read(locator, *args, **kwargs):
            reads.append(1)
            if len(reads) == 1:
                raise PlaywrightError('Execution context was destroyed, most likely because of a navigation')
            return original(locator, *args, **kwargs)

        with patch.object(Locator, 'evaluate_all', read), \
                patch.object(self.page, 'goto', wraps=self.page.goto) as goto, \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.bot._open_entry(entry_url, '店铺列表', timeout=1500, max_timeout=1500)
        self.assertGreaterEqual(len(reads), 2)
        goto.assert_called_once()
        reload.assert_not_called()

    def test_partial_store_scan_is_not_used_after_redirect_to_existing_admin(self):
        self.page.goto('https://admin.shopify.com/?no_redirect=true')
        self.page.set_content('<h1>Create your first online store</h1><button>Create store</button>')
        admin = 'https://admin.shopify.com/store/already-created'
        self.page.route(admin, lambda route: route.fulfill(content_type='text/html',
            body='<nav><a href="/store/already-created/products">Products</a></nav>'))

        def interrupted_read(*args, **kwargs):
            self.page.goto(admin)
            raise PlaywrightError('Execution context was destroyed')

        with patch.object(Locator, 'evaluate_all', interrupted_read), \
                patch.object(self.bot, '_open_entry') as entry, \
                patch.object(Locator, 'click') as click:
            self.assertTrue(self.bot._handle_store_selection())
        entry.assert_not_called()
        click.assert_not_called()
        self.assertTrue(self.bot._looks_logged_in())

    def test_profile_redirect_during_store_read_is_not_sent_back_to_store_list(self):
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        self.page.set_content('<span>test@example.com</span>')

        def interrupted_read(*args, **kwargs):
            self.page.goto('https://accounts.shopify.com/select')
            raise PlaywrightError('Execution context was destroyed')

        with patch.object(Locator, 'evaluate_all', interrupted_read), \
                patch.object(self.bot, '_open_entry') as entry:
            self.assertTrue(self.bot._continue_from_account_profile())
        entry.assert_not_called()
        self.assertEqual(self.bot._account_handoffs, 0)

    def test_unreadable_store_list_never_means_zero_stores(self):
        self.page.goto('https://admin.shopify.com/?no_redirect=true')
        self.page.set_content('<h1>Create your first online store</h1><button>Create store</button>')
        with patch.object(Locator, 'evaluate_all', side_effect=PlaywrightError('Execution context was destroyed')):
            with self.assertRaisesRegex(PageLoadError, '等待期限'):
                self.bot._visible_store_links(timeout=100)
            self.assertFalse(self.bot._entry_ready(timeout=100))
        with patch.object(self.bot, '_visible_store_links', return_value=None), \
                patch.object(Locator, 'click') as click:
            self.assertTrue(self.bot._handle_store_selection())
        click.assert_not_called()

    def test_unexpected_store_read_error_is_not_silently_retried(self):
        self.page.goto('https://admin.shopify.com/')
        with patch.object(Locator, 'evaluate_all', side_effect=PlaywrightError('Unexpected selector error')):
            with self.assertRaisesRegex(PlaywrightError, 'Unexpected selector error'):
                self.bot._visible_store_links(timeout=100)

    def test_empty_store_list_cannot_create_when_task_has_saved_store(self):
        self.bot.data['店铺后台'] = 'https://admin.shopify.com/store/saved-shop'
        self.page.goto('https://admin.shopify.com/?no_redirect=true')
        self.page.set_content('<h1>Create your first online store</h1><button>Create store</button>')
        with patch.object(self.bot, '_open_entry') as entry:
            self.bot._handle_store_selection()
        self.assertEqual(entry.call_args.args[0], self.bot.data['店铺后台'])

    def test_multiple_stores_require_matching_task_name(self):
        self.page.goto('https://admin.shopify.com/')
        self.page.set_content('<a href="/store/first">Other shop</a><a href="/store/second">Test</a>')
        with patch.object(self.bot, '_open_entry') as entry:
            self.bot._handle_store_selection()
        self.assertEqual(entry.call_args.args[0], 'https://admin.shopify.com/store/second')
        self.bot.shop_name = 'Unknown'
        with patch.object(self.bot, '_open_entry') as entry:
            with self.assertRaisesRegex(RuntimeError, '多个店铺'):
                self.bot._handle_store_selection()
        entry.assert_not_called()

    def test_profile_is_reused_without_refilling_names_or_marking_store_success(self):
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        self.page.set_content('<span>test@example.com</span>' + NAME_FIELDS)
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
            self.bot._goto_signup('https://accounts.shopify.com/signup')
        goto.assert_not_called()
        self.assertFalse(self.bot._looks_logged_in(timeout=100))
        self.assertEqual(self.bot.admin_url, '')
        self.assertFalse(self.bot._handle_password_step())

    def test_saved_store_login_returns_to_same_store_without_new_signup(self):
        admin = 'https://admin.shopify.com/store/saved-shop'
        profile = 'https://accounts.shopify.com/accounts/123/personal'
        self.bot.data['店铺后台'] = admin
        requested = []

        def serve(route):
            url = route.request.url
            requested.append(url)
            if url == admin and requested.count(admin) == 1:
                body = '<script>location.replace("https://accounts.shopify.com/login")</script>'
            elif url == 'https://accounts.shopify.com/login':
                body = ('<label>Password<input type="password"></label><button onclick=\'location.href="' +
                        profile + '"\'>Log in</button>')
            elif url == profile:
                body = '<h1>General</h1><span>test@example.com</span>'
            else:
                body = '<nav><a href="/store/saved-shop/products">Products</a></nav>'
            route.fulfill(content_type='text/html', body=body)

        self.page.route('**/*', serve)
        self.page.goto('about:blank')
        def wait_for_login():
            self.page.wait_for_url('https://accounts.shopify.com/login')
            return False

        with patch.object(self.bot, '_wait_for_admin', side_effect=wait_for_login), \
                patch.object(self.bot, '_ask_user') as ask:
            self.bot._ensure_admin()
        self.assertEqual(requested, [admin, 'https://accounts.shopify.com/login', profile, admin])
        self.assertEqual(self.bot.admin_url, admin)
        ask.assert_not_called()

    def test_profile_with_existing_store_reuses_it_instead_of_creating_duplicate(self):
        admin = 'https://admin.shopify.com/store/already-created'
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        self.page.set_content('<span>test@example.com</span><a href="' + admin + '">Store</a>')
        with patch.object(self.bot, '_open_entry') as entry:
            self.assertTrue(self.bot._continue_from_account_profile())
        self.assertEqual(entry.call_args.args[0], admin)

    def test_blank_profile_and_wrong_account_never_start_store_creation(self):
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        for body in ('<div>Loading...</div>', '<h1>General</h1><span>someone@example.com</span>'):
            self.page.set_content(body)
            self.assertFalse(self.bot._entry_ready(timeout=100))
            with patch.object(self.bot, '_open_entry') as entry, patch('bot.wait_for_ready', return_value=False):
                with self.assertRaisesRegex(RuntimeError, '本任务邮箱'):
                    self.bot._continue_from_account_profile()
            entry.assert_not_called()

    def test_account_profile_waits_for_slow_details_without_refreshing(self):
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        self.page.set_content('''<div>Loading...</div><script>
          setTimeout(()=>document.body.innerHTML='<span>test@example.com</span>',1200);
        </script>''')
        with patch.object(self.page, 'reload', wraps=self.page.reload) as reload, \
                patch.object(self.bot, '_open_entry') as entry:
            self.assertTrue(self.bot._continue_from_account_profile())
        reload.assert_not_called()
        entry.assert_called_once()

    def test_repeated_profile_redirects_stop_instead_of_creating_loop(self):
        self.page.goto('https://accounts.shopify.com/accounts/123/personal')
        self.page.set_content('<span>test@example.com</span>')
        with patch.object(self.bot, '_open_entry') as entry:
            self.bot._continue_from_account_profile()
            self.bot._continue_from_account_profile()
            with self.assertRaisesRegex(RuntimeError, '已停止重复跳转'):
                self.bot._continue_from_account_profile()
        self.assertEqual(entry.call_count, 2)

    def test_existing_password_is_preserved_and_no_navigation_is_issued(self):
        self.page.locator('input').fill("already-entered")
        with patch.object(self.page, "goto", wraps=self.page.goto) as goto:
            self.bot._goto_signup("https://admin.shopify.com/signup")
        goto.assert_not_called()
        self.assertEqual(self.page.locator('input').input_value(), "already-entered")

    def test_required_names_are_filled_before_create_account(self):
        self.bot.contact_name = "Jean Paul Martin"
        self.page.set_content(NAME_FORM)
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.assertEqual(self.page.evaluate('JSON.parse(sessionStorage.names)'), ["Jean Paul", "Martin"])
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()

    def test_existing_first_name_is_preserved_while_last_name_is_filled(self):
        self.bot.contact_name = "Jean Martin"
        self.page.set_content(NAME_FORM)
        self.page.get_by_label('First name', exact=True).fill('Existing')
        self.bot._handle_password_step()
        self.assertEqual(self.page.evaluate('JSON.parse(sessionStorage.names)'), ['Existing', 'Martin'])

    def test_structured_names_preserve_compound_surname(self):
        self.bot.contact_name = "Sofia Maria de la Cruz"
        self.bot.data.update({"联系人名": "Sofia Maria", "联系人姓": "de la Cruz"})
        self.page.set_content(NAME_FORM)
        self.bot._handle_password_step()
        self.assertEqual(self.page.evaluate('JSON.parse(sessionStorage.names)'), ['Sofia Maria', 'de la Cruz'])

    def test_edited_contact_name_overrides_old_structured_name(self):
        self.bot.contact_name = "New Owner"
        self.bot.data.update({"联系人名": "Old", "联系人姓": "Name"})
        self.page.set_content(NAME_FORM)
        self.bot._handle_password_step()
        self.assertEqual(self.page.evaluate('JSON.parse(sessionStorage.names)'), ['New', 'Owner'])

    def test_hidden_name_fields_do_not_require_contact_name(self):
        self.page.set_content('<div hidden>' + NAME_FIELDS + '</div>' + PASSWORD)
        with patch.object(self.bot, '_wait_password_result') as outcome:
            self.bot._handle_password_step()
        self.assertEqual(self.page.locator('[name=firstName]').input_value(), '')
        outcome.assert_called_once()

    def test_missing_contact_name_stops_without_refreshing_or_submitting(self):
        self.page.set_content(NAME_FORM)
        with patch.object(self.bot, '_recover_password_page') as recover, \
                patch.object(self.bot, '_wait_password_result') as outcome:
            with self.assertRaisesRegex(RuntimeError, '联系人姓名不完整'):
                self.bot._handle_password_step()
        recover.assert_not_called()
        outcome.assert_not_called()

    def test_names_appearing_during_button_wait_are_filled_without_refresh(self):
        self.bot.contact_name = 'Jean Martin'
        self.page.set_content(PASSWORD.replace('<button>', '<button disabled>'))
        from bot import wait_for_ready as original_wait

        def show_names(*args, **kwargs):
            self.page.set_content(NAME_FORM)
            # Keep the already entered password when the page adds name fields.
            self.page.locator('input[type=password]').fill(self.bot.password)
            return original_wait(*args, **kwargs)

        with patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            with patch('bot.wait_for_ready', side_effect=show_names):
                self.bot._handle_password_step()
            self.assertEqual(self.page.get_by_label('First name', exact=True).input_value(), 'Jean')
            self.bot._handle_password_step()
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()

    def test_names_requested_after_submission_return_to_form_without_refresh(self):
        self.bot.contact_name = 'Jean Martin'
        self.page.set_content(PASSWORD)
        self.page.locator('button').evaluate('(button, html) => button.onclick=()=>{document.body.innerHTML=html}', NAME_FIELDS + PASSWORD)
        with patch.object(self.bot, '_recover_password_page') as recover:
            self.bot._handle_password_step()
        self.assertEqual(self.page.get_by_label('First name', exact=True).input_value(), '')
        recover.assert_not_called()
        self.assertTrue(self.bot._fill_signup_contact_names())
        self.assertEqual(self.page.get_by_label('Last name', exact=True).input_value(), 'Martin')

    def test_signup_url_alone_does_not_make_blank_page_ready(self):
        self.page.set_content('<div id="app"></div>')
        self.page.route("**/*", lambda route: route.fulfill(body='<div id="app"></div>'))
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            with self.assertRaises(PageLoadError):
                self.bot._open_entry(SIGNUP, "测试注册页", timeout=100, max_timeout=200)
        reload.assert_called_once()

    def test_existing_code_page_is_preserved_when_entering_signup(self):
        self.page.goto("https://accounts.shopify.com/verify")
        self.page.set_content('<input autocomplete="one-time-code" value="123456">')
        with patch.object(self.page, "goto", wraps=self.page.goto) as goto:
            self.bot._goto_signup("https://admin.shopify.com/signup")
        goto.assert_not_called()
        self.assertEqual(self.page.locator('input').input_value(), "123456")

    def test_slow_existing_form_is_waited_for_without_reloading(self):
        self.page.set_content('''<div>Loading</div><script>
          setTimeout(()=>document.body.innerHTML='<input type="email">',100);
        </script>''')
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.bot._open_entry(SIGNUP, "测试注册页", timeout=500)
        reload.assert_not_called()

    def test_challenge_is_handed_to_manual_handler_without_reload(self):
        self.page.set_content(PASSWORD + '<div id="challenge-running">Verify</div>')
        self.bot._maybe_wait_challenge = Mock()
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.bot._wait_password_result(SIGNUP, timeout=100)
        self.bot._maybe_wait_challenge.assert_called_once()
        reload.assert_not_called()

    def test_delayed_submission_clicks_once_and_waits_for_otp(self):
        self.page.set_content(PASSWORD + '''<script>let n=0;
          document.querySelector('button').onclick=()=>{
            sessionStorage.clicks=++n;
            setTimeout(()=>document.body.innerHTML='<input autocomplete="one-time-code">',100);
          };</script>''')
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()

    def test_button_enabled_at_wait_deadline_is_clicked_instead_of_refreshed(self):
        self.page.set_content(PASSWORD + '''<script>
          document.querySelector('button').onclick=()=>{
            sessionStorage.clicks=Number(sessionStorage.clicks||0)+1;
            document.body.innerHTML='<input autocomplete="one-time-code">';
          };</script>''')
        with patch('bot.wait_for_ready', return_value=False), \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()

    def test_click_does_not_wait_for_slow_navigation_or_refresh_the_pending_request(self):
        self.page.set_content('<form action="/slow-submit">' + PASSWORD + '</form>')
        pending = []
        self.page.route('**/slow-submit*', lambda route: pending.append(route))
        # Leave the navigation genuinely pending: a normal click would wait
        # until its timeout, even though the submit was already sent.
        with patch.object(self.bot, '_wait_password_result') as outcome, \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.page.wait_for_timeout(50)
        self.assertEqual(len(pending), 1)
        self.assertTrue(any('已点击：Create account' in str(c) for c in self.bot._log.call_args_list))
        outcome.assert_called_once_with(SIGNUP)
        reload.assert_not_called()
        pending[0].fulfill(body='<input autocomplete="one-time-code">')

    def test_click_timeout_waits_for_outcome_before_any_refresh(self):
        from playwright.sync_api import Locator
        original_click = Locator.click

        def click(locator, **kwargs):
            if locator.count() and locator.first.get_attribute('id') == 'submit':
                raise PlaywrightTimeout('click timed out while page changed')
            return original_click(locator, **kwargs)

        self.page.set_content(PASSWORD.replace('<button>', '<button id="submit">'))
        with patch.object(Locator, 'click', click), \
                patch.object(self.bot, '_wait_password_result') as outcome, \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        outcome.assert_called_once_with(SIGNUP)
        reload.assert_not_called()

    def test_aria_disabled_button_is_not_submitted(self):
        self.page.set_content(PASSWORD.replace('<button>', '<button aria-disabled="true">'))
        original_wait = __import__('bot').wait_for_ready
        with patch('bot.wait_for_ready', side_effect=lambda *a, **kw: original_wait(*a, **dict(kw, timeout=100, max_timeout=100))), \
                patch.object(self.bot, '_recover_password_page') as recover, \
                patch.object(self.bot, '_wait_password_result') as outcome:
            self.assertTrue(self.bot._handle_password_step())
        recover.assert_called_once()
        outcome.assert_not_called()

    def test_enabled_button_under_pointer_overlay_is_submitted_with_keyboard(self):
        from playwright.sync_api import Locator
        self.page.set_content('''<label>Create a password<input type="password"></label>
          <div style="position:relative;width:200px;height:60px">
            <button style="width:200px;height:60px">Create account</button>
            <span style="position:absolute;inset:0">Create account</span>
          </div><script>
            document.querySelector('button').onclick=()=>{
              sessionStorage.clicks=Number(sessionStorage.clicks||0)+1;
              document.body.innerHTML='<input autocomplete="one-time-code">';
            };
          </script>''')
        original_click = Locator.click

        def click(locator, **kwargs):
            return original_click(locator, **dict(kwargs, timeout=200))

        with patch.object(Locator, 'click', click), \
                patch.object(self.page, 'reload', wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_not_called()
        self.assertTrue(any('键盘 Enter' in str(c) for c in self.bot._log.call_args_list))

    def test_stuck_password_page_refreshes_after_one_click(self):
        self.page.set_content(PASSWORD + '''<script>
          document.querySelector('button').onclick=()=>sessionStorage.clicks=Number(sessionStorage.clicks||0)+1;
        </script>''')
        original = self.bot._wait_password_result
        with patch.object(self.bot, "_wait_password_result", side_effect=lambda url: original(url, timeout=100)), \
                patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            self.assertTrue(self.bot._handle_password_step())
        self.assertEqual(self.page.evaluate('sessionStorage.clicks'), '1')
        reload.assert_called_once()
        self.assertEqual(self.bot._password_refreshes, 1)

    def test_password_refreshes_have_an_overall_limit(self):
        self.bot._password_refreshes = 2
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            with self.assertRaisesRegex(RuntimeError, "已停止重复提交"):
                self.bot._recover_password_page("Still stuck")
        reload.assert_not_called()

    def test_invalid_password_does_not_refresh_or_submit_again(self):
        self.page.set_content(PASSWORD.replace('type="password"', 'type="password" aria-invalid="true"'))
        with patch.object(self.page, "reload", wraps=self.page.reload) as reload:
            with self.assertRaisesRegex(RuntimeError, "输入有误"):
                self.bot._wait_password_result(SIGNUP, timeout=100)
        reload.assert_not_called()

    def test_untrusted_url_containing_shopify_is_not_accepted(self):
        self.page.goto("https://example.com/?next=https://accounts.shopify.com/signup")
        self.assertFalse(self.bot._entry_ready(timeout=100))


if __name__ == "__main__":
    unittest.main()
