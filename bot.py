import email
import imaplib
import re
import secrets
import string
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from adspower_client import AdsPowerClient
from shopify_admin import admin_store_base
from browser_recovery import FirstPageUnavailable, PageLoadError, browser_disconnected, browser_error_page, reload_page, retryable_browser_error, wait_for_ready


class ShopifyBot:
    """使用 AdsPower 环境，按表格中的真实资料填写 Shopify 官方注册页。"""

    def __init__(self, config, register_data, ads_client: AdsPowerClient):
        self.config = config
        self.data = register_data
        self.ads = ads_client
        self.profile_id = self.data["AdsPower环境ID"]
        self.email = self.data["邮箱"]
        self.email_pwd = self.data.get("邮箱密码", "")
        self.shop_name = self.data["店铺名"]
        self.contact_name = self.data.get("联系人姓名", "")
        self.country = self.data.get("国家", "")
        self.address = self.data.get("地址", "")
        self.city = self.data.get("城市", "")
        self.province = self.data.get("州/省", "")
        self.zip_code = self.data.get("邮编", "")
        self.phone = self.data.get("电话", "")
        self.password = self.data.get("Shopify密码") or self._generate_password()
        self.browser = None
        self.page = None
        self._started_profile = False
        self.admin_url = ""
        self._password_refreshes = 0
        self._account_handoffs = 0

    def _generate_password(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return "Shp!" + "".join(secrets.choice(alphabet) for _ in range(12))

    def _log(self, msg):
        print(msg, flush=True)

    def _ask_user(self, message, kind="continue"):
        return input(message)

    def _check_cancel(self):
        pass

    def _sleep(self, short=False):
        lo = 0.4 if short else self.config.getfloat("settings", "min_wait")
        hi = 0.8 if short else self.config.getfloat("settings", "max_wait")
        time.sleep(secrets.SystemRandom().uniform(lo, hi))

    def _locator_ready(self, locator, timeout=800, need_editable=False):
        try:
            locator.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeout:
            return False
        try:
            if not locator.is_visible():
                return False
            if need_editable and not locator.is_editable():
                return False
        except Exception:
            return False
        return True

    def _fill_locator(self, locator, value, timeout=3000) -> bool:
        if not value or not self._locator_ready(locator, timeout=800, need_editable=True):
            return False
        try:
            locator.click(timeout=timeout)
            locator.fill(value, timeout=timeout)
            return True
        except Exception as e:
            self._log(f"填写失败，改用逐字输入：{e}")
            try:
                locator.click(timeout=timeout)
                locator.press("Control+A")
                locator.press_sequentially(value, delay=30, timeout=timeout)
                return True
            except Exception as e2:
                self._log(f"逐字输入仍失败：{e2}")
                return False

    def _fill_first(self, selectors, value, timeout=800) -> bool:
        if not value:
            return False
        for sel in selectors:
            if self._fill_locator(self.page.locator(sel).first, value, timeout=max(timeout, 2000)):
                return True
        return False

    def _click_button(self, texts, timeout=4000) -> bool:
        for text in texts:
            locators = [
                self.page.get_by_role("button", name=text).first,
                self.page.get_by_role("link", name=text).first,
                self.page.locator(f'button:has-text("{text}")').first,
                self.page.locator(f'a:has-text("{text}")').first,
                self.page.get_by_text(text, exact=True).first,
            ]
            for loc in locators:
                if not self._locator_ready(loc, timeout=min(timeout, 800)):
                    continue
                try:
                    loc.wait_for(state="visible", timeout=timeout)
                    for _ in range(12):
                        disabled = True
                        try:
                            disabled = loc.is_disabled()
                        except Exception:
                            disabled = False
                        if not disabled:
                            break
                        time.sleep(0.25)
                    loc.click(timeout=timeout, force=True)
                    self._log(f"已点击：{text}")
                    return True
                except Exception as e:
                    self._log(f"点击「{text}」失败：{e}")
        return False

    def _on_shopify(self) -> bool:
        try:
            parts = urlsplit(self.page.url or "")
        except ValueError:
            return False
        return parts.scheme == "https" and parts.netloc.lower() in (
            "shopify.com", "www.shopify.com", "admin.shopify.com", "accounts.shopify.com",
        )

    def _entry_ready(self, timeout=1000):
        """Recognize an interactive entry or hand off a store URL to admin checks."""
        if not self._on_shopify():
            return False
        if admin_store_base(self.page.url):
            # This is not login success: _wait_for_admin still checks store UI.
            self._remember_admin()
            return True
        if self._on_account_profile():
            return self._locator_ready(self.page.get_by_text(self.email, exact=True).first, timeout=timeout)
        if self._on_account_selection():
            return self._locator_ready(self.page.get_by_role("heading", name="Choose an account").first.or_(
                self.page.get_by_text(self.email, exact=True)).first, timeout=timeout)
        if self._on_store_selection():
            try:
                stores = self._visible_store_links(timeout=timeout)
            except PageLoadError:
                return False
            if stores is None:
                return False
            return bool(stores) or self._locator_ready(
                self.page.get_by_text("Create your first online store", exact=True).first, timeout=timeout)
        fields = self.page.locator(
            'input[type="email"]:enabled:not([readonly]):visible, '
            'input[name="email"]:enabled:not([readonly]):visible, '
            'input[type="password"]:enabled:not([readonly]):visible, '
            'input[autocomplete="one-time-code"]:enabled:not([readonly]):visible, '
            'input[name="verificationCode"]:enabled:not([readonly]):visible, '
            'input[name="shopName"]:enabled:not([readonly]):visible, '
            'input[name="shop_name"]:enabled:not([readonly]):visible, '
            'iframe[src*="challenges.cloudflare.com"]:visible, #challenge-running:visible, .cf-challenge:visible'
        )
        actions = re.compile(r"^(Continue(?: with email)?|Start free trial|Create(?: your)? account|"
                             r"Skip(?: all| for now)?|Next|Log in|Sign in|继续|跳过|创建账户)$", re.I)
        visible = self.page.locator(":visible:not([disabled]):not([aria-disabled='true'])")
        ready = fields.or_(self.page.get_by_role("button", name=actions).and_(visible)).or_(
            self.page.get_by_role("link", name=actions).and_(visible)
        )
        return self._locator_ready(ready.first, timeout=timeout)

    def _open_entry(self, url, label, timeout=180000, max_timeout=300000, skip_slow=False):
        """Do not restart a slow first document or an existing signup redirect."""
        if skip_slow:
            timeout = min(timeout, 60000)
            max_timeout = timeout
        attempts = 1 if skip_slow else 2
        current = urlsplit(self.page.url or "")
        in_signup = self._on_shopify() and (
            any(part in current.path.lower() for part in ("signup", "password", "challenge", "verify", "verification"))
            or self.page.locator('input[autocomplete="one-time-code"]:visible, input[name="verificationCode"]:visible').count() > 0
        )
        reuse = self._on_shopify() and (
            url == "https://admin.shopify.com" or self.page.url == url
            or in_signup or bool(admin_store_base(self.page.url))
            or (urlsplit(url).netloc == "accounts.shopify.com"
                and (self._on_account_profile() or self._on_account_selection()
                     or self._on_store_selection() or current.path.rstrip("/") == "/login"))
        )
        last_error = None
        for attempt in range(attempts):
            self._check_cancel()
            if (reuse or attempt) and self._entry_ready(timeout=200):
                self._log(f"{label}已就绪，沿用当前页面")
                return
            started = time.monotonic()
            try:
                if attempt and self._on_shopify():
                    last_error = reload_page(self.page, self._log, label, attempt, limit=1)
                elif attempt or not reuse:
                    self._log(f"打开{label}，等待页面响应和表单加载（{attempt + 1}/{attempts}）")
                    response = self.page.goto(url, wait_until="commit", timeout=min(timeout, 30000))
                    if response and response.status >= 400:
                        if response.status == 429:
                            raise RuntimeError("Shopify 返回 HTTP 429，请稍后重试；停止自动刷新")
                        error_type = PageLoadError if response.status in (408, 429) or response.status >= 500 else RuntimeError
                        raise error_type(f"{label}返回 HTTP {response.status}")
                else:
                    self._log(f"沿用正在加载的{label}，暂不重新打开")
            except Exception as exc:
                if skip_slow and any(token in str(exc) for token in (
                        "ERR_SOCKS_", "ERR_PROXY_", "ERR_TUNNEL_CONNECTION_FAILED")):
                    raise FirstPageUnavailable(f"首屏代理连接失败，已跳过；{str(exc).splitlines()[0]}") from exc
                if browser_disconnected(exc) or not retryable_browser_error(exc):
                    raise
                last_error = exc
                self._log(f"{label}导航尚未完成，保留当前页面继续等待：{str(exc).splitlines()[0]}")
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                ready = wait_for_ready(self.page, self._entry_ready, self._log, label, self._check_cancel,
                                       timeout=max(1, timeout - elapsed), max_timeout=max(1, max_timeout - elapsed))
            except RuntimeError as exc:
                if skip_slow and any(token in str(exc) for token in (
                        "ERR_SOCKS_", "ERR_PROXY_", "ERR_TUNNEL_CONNECTION_FAILED")):
                    raise FirstPageUnavailable(f"首屏代理连接失败，已跳过；{str(exc).splitlines()[0]}") from exc
                raise
            if ready or self._entry_ready(timeout=200):
                self._log(f"{label}已加载，继续处理")
                return
        detail = str(last_error).splitlines()[0] if last_error else "未出现可用的登录/注册表单"
        if skip_slow:
            raise FirstPageUnavailable(f"首屏等待 {timeout // 1000} 秒仍未加载，已跳过；{detail}") from last_error
        raise PageLoadError(f"{label}等待并重试后仍未就绪，请检查当前环境网络或稍后重试；{detail}") from last_error

    def _goto_signup(self, url):
        self._open_entry(url, "Shopify 注册页", skip_slow=True)

    def _on_account_profile(self):
        return bool(re.match(r"^https://accounts\.shopify\.com/accounts/[^/?#]+/personal(?:[/?#]|$)",
                             self.page.url or "", re.I))

    def _on_account_selection(self):
        return bool(re.match(r"^https://accounts\.shopify\.com/select(?:[/?#]|$)", self.page.url or "", re.I))

    def _handle_account_selection(self):
        if not self._on_account_selection():
            return False
        account = self.page.get_by_text(self.email, exact=True).and_(self.page.locator(":visible")).first
        if not self._locator_ready(account, timeout=3000):
            raise RuntimeError("账号选择页未找到本任务邮箱，请确认当前环境中的登录账号")
        submitted_url = self.page.url
        self._check_cancel()
        account.click(timeout=10000, no_wait_after=True)
        self._log("已选择本任务 Shopify 账号，等待继续")
        if not wait_for_ready(self.page, lambda _: self.page.url != submitted_url,
                              self._log, "账号选择结果", self._check_cancel,
                              timeout=60000, max_timeout=60000):
            raise PageLoadError("选择账号后未进入下一步，已停止重复点击")
        return True

    def _on_store_selection(self):
        current = urlsplit(self.page.url or "")
        return current.scheme == "https" and current.netloc == "admin.shopify.com" and current.path in ("", "/")

    def _visible_store_links(self, timeout=60000):
        """Read one document snapshot; None means navigation, not an empty list."""
        source_url = self.page.url
        stores = None

        def read_snapshot(_):
            nonlocal stores
            if self.page.url != source_url:
                return True
            # Reading each locator separately could mix two documents when
            # Shopify redirects. Capture the URL, hrefs and names together.
            snapshot = self.page.locator('a[href]:visible').evaluate_all("""links => ({
                url: location.href,
                links: links.map(link => ({href: link.href, name: link.innerText || ''}))
            })""")
            if snapshot["url"] != source_url or self.page.url != source_url:
                return True
            stores = {}
            for link in snapshot["links"]:
                base = admin_store_base(link["href"])
                if base:
                    stores[base] = link["name"].strip()
            return True

        if not wait_for_ready(self.page, read_snapshot, self._log, "店铺列表读取", self._check_cancel,
                              timeout=timeout, max_timeout=timeout):
            raise PageLoadError("店铺列表在等待期限内仍无法读取，已保留当前账号和任务进度")
        return stores

    def _choose_existing_store(self, stores):
        saved = admin_store_base(self.data.get("店铺后台"))
        if saved:
            return saved
        if len(stores) == 1:
            return next(iter(stores))
        if stores:
            matches = [base for base, name in stores.items() if name.casefold() == self.shop_name.casefold()]
            if len(matches) != 1:
                raise RuntimeError("当前账号有多个店铺，无法确认本任务店铺；请绑定对应的店铺后台地址")
            return matches[0]
        return ""

    def _handle_store_selection(self):
        if not self._on_store_selection():
            return False
        stores = self._visible_store_links()
        if stores is None or not self._on_store_selection():
            return True
        target = self._choose_existing_store(stores)
        if target:
            self._log("沿用账号已有店铺：" + target)
            self._open_entry(target, "Shopify 已有店铺")
            return True
        empty = self.page.get_by_text("Create your first online store", exact=True).first
        if not empty.is_visible():
            return False
        create = self.page.get_by_role("button", name="Create store", exact=True).or_(
            self.page.get_by_role("link", name="Create store", exact=True)
        ).and_(self.page.locator(":visible")).first
        self._check_cancel()
        submitted_url = self.page.url
        create.click(timeout=10000, no_wait_after=True)
        self._log("账号尚无店铺，已点击 Create store，继续创建本任务店铺")
        if not wait_for_ready(self.page, lambda _: self.page.url != submitted_url or not empty.is_visible(),
                              self._log, "Create store 提交结果", self._check_cancel,
                              timeout=90000, max_timeout=90000):
            raise PageLoadError("Create store 提交后未进入下一步，已停止重复点击")
        return True

    def _continue_from_account_profile(self):
        """An account profile is not a store: resume the merchant flow."""
        if not self._on_account_profile():
            return False
        ready = wait_for_ready(self.page, self._entry_ready, self._log, "Shopify 账号资料页",
                               self._check_cancel, timeout=60000, max_timeout=60000,
                               still_applicable=self._on_account_profile)
        if not self._on_account_profile():
            return True
        if not ready and not self._entry_ready(timeout=200):
            raise RuntimeError("账号资料页未显示本任务邮箱，请确认登录的是本任务账号；未继续开店")
        # Reuse an existing store displayed on this account before offering to
        # create one. Never guess a handle from the requested store name.
        stores = self._visible_store_links()
        if stores is None or not self._on_account_profile():
            return True
        target = self._choose_existing_store(stores)
        if self._account_handoffs >= 2:
            raise RuntimeError("账号已登录，但开店入口仍返回账号资料页；请检查页面提示，已停止重复跳转")
        self._account_handoffs += 1
        self._log("已进入 Shopify 账号资料页；沿用当前账号继续" + ("已有店铺" if target else "查看店铺列表"))
        target = target or "https://admin.shopify.com/?no_redirect=true"
        self._open_entry(target, "Shopify 店铺入口")
        return True

    def _maybe_wait_challenge(self):
        overlay = self.page.locator(
            "iframe[src*='challenges.cloudflare.com'], #challenge-running, .cf-challenge"
        ).first
        try:
            if overlay.is_visible(timeout=600):
                self._ask_user("检测到人机验证，请在 AdsPower 窗口中完成后继续...")
        except PlaywrightTimeout:
            return

    def _looks_logged_in(self, timeout=1200) -> bool:
        base = admin_store_base(self.page.url)
        saved = admin_store_base(self.data.get("店铺后台"))
        if not base or (saved and saved != base):
            return False
        # A URL alone also matches a loading/error page. Require the actual
        # store navigation before treating registration/login as complete.
        path = base[len("https://admin.shopify.com"):]
        selectors = [
            '{} a[href="{}{}"]:visible'.format(nav, prefix, suffix)
            for nav in ("nav", '[role="navigation"]')
            for prefix in (path, base)
            for suffix in ("/orders", "/products", "/settings", "/settings/general")
        ]
        if not self._locator_ready(self.page.locator(", ".join(selectors)).first, timeout=timeout):
            return False
        if admin_store_base(self.page.url) != base:
            return False
        self.admin_url = base
        return True

    def _remember_admin(self):
        """Persist an observed store address without declaring its UI ready."""
        base = admin_store_base(self.page.url)
        if base and not admin_store_base(self.data.get("店铺后台")):
            self.data["店铺后台"] = base
            self._log(f"已记录店铺地址：{base}；继续等待后台加载")
            return base
        return ""

    def _wait_for_admin(self, timeout=120000, max_timeout=300000) -> bool:
        for attempt in range(3):
            self._check_cancel()
            try:
                if attempt:
                    if self._looks_logged_in(timeout=200):
                        self._log("Shopify 后台已恢复，取消本次刷新")
                        return True
                    # Only one navigation per retry. Never reload an error
                    # document and immediately follow it with another goto.
                    saved = admin_store_base(self.data.get("店铺后台"))
                    if browser_error_page(self.page.url) and saved:
                        self._log(f"Shopify 后台等待后仍未恢复，重新打开原店铺（{attempt}/2）")
                        self.page.goto(saved, wait_until="commit", timeout=25000)
                    else:
                        reload_page(self.page, self._log, "Shopify 后台", attempt)
                self.page.wait_for_url(
                    re.compile(r"^https://admin\.shopify\.com/store/[a-z0-9][a-z0-9-]*(?:[/?#]|$)", re.I),
                    wait_until="commit", timeout=min(timeout, 20000),
                )
            except Exception as exc:
                if browser_disconnected(exc) or not retryable_browser_error(exc):
                    raise
                self._log("后台导航未完成，先检查页面恢复，暂不刷新：" + str(exc).splitlines()[0])
            self._remember_admin()
            expected = admin_store_base(self.data.get("店铺后台"))
            # Login/verification forms need input, not repeated reloads.
            if not expected or (not admin_store_base(self.page.url) and not browser_error_page(self.page.url)):
                return False
            if wait_for_ready(self.page, self._looks_logged_in, self._log, "Shopify 后台",
                              self._check_cancel, timeout=timeout, max_timeout=max_timeout,
                              still_applicable=lambda: (admin_store_base(self.page.url) == expected
                                                       or browser_error_page(self.page.url))):
                return True
        return False

    def _checkpoint_setup(self, step):
        completed = self.data.setdefault("_setup_completed", [])
        if step not in completed:
            completed.append(step)

    def _get_email_verification_code(self, retries=8, delay=8):
        imap_server = self.data.get("IMAP服务器") or self.config.get("settings", "imap_server")
        for i in range(retries):
            try:
                with imaplib.IMAP4_SSL(imap_server) as client:
                    client.login(self.email, self.email_pwd)
                    client.select("INBOX")
                    status, data = client.search(None, '(FROM "shopify.com")')
                    if status != "OK" or not data or not data[0]:
                        self._log(f"未找到 Shopify 邮件，{delay} 秒后重试... ({i + 1}/{retries})")
                        time.sleep(delay)
                        continue
                    ids = data[0].split()
                    for msg_id in reversed(ids[-8:]):
                        status, msg_data = client.fetch(msg_id, "(RFC822)")
                        if status != "OK":
                            continue
                        msg = email.message_from_bytes(msg_data[0][1])
                        body = self._extract_email_body(msg)
                        match = re.search(r"\b(\d{6})\b", body)
                        if match:
                            code = match.group(1)
                            self._log(f"成功获取验证码：{code}")
                            return code
                self._log(f"邮件中未提取到 6 位验证码，{delay} 秒后重试... ({i + 1}/{retries})")
            except Exception as e:
                self._log(f"读取邮箱出错：{e}，{delay} 秒后重试... ({i + 1}/{retries})")
            time.sleep(delay)
        return None

    def _extract_email_body(self, msg) -> str:
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html") and "attachment" not in str(part.get("Content-Disposition", "")):
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        parts.append(payload.decode(charset, errors="ignore"))
                    except LookupError:
                        parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode("utf-8", errors="ignore"))
        return "\n".join(parts)

    def _recover_password_page(self, reason):
        self._check_cancel()
        if self._password_refreshes >= 2:
            raise RuntimeError("密码页刷新 2 次后仍未推进，请检查页面提示；已停止重复提交")
        self._password_refreshes += 1
        self._log(reason + "；刷新密码页后继续，保留原邮箱和密码")
        reload_page(self.page, self._log, "Shopify 密码页", self._password_refreshes)
        if not wait_for_ready(self.page, self._entry_ready, self._log, "Shopify 密码页", self._check_cancel,
                              timeout=180000, max_timeout=300000):
            raise PageLoadError("密码页刷新后尚未恢复")

    def _pending_signup_name_fields(self):
        fields = (
            ("First name", r"^(First name|Given name|名字|名)$",
             'input[autocomplete="given-name"], input[name="firstName"], input[name="first_name"]'),
            ("Last name", r"^(Last name|Family name|Surname|姓氏|姓)$",
             'input[autocomplete="family-name"], input[name="lastName"], input[name="last_name"]'),
        )
        pending = []
        for index, (label, pattern, selector) in enumerate(fields):
            field = self.page.get_by_label(re.compile(pattern, re.I)).or_(
                self.page.get_by_placeholder(re.compile(pattern, re.I))
            ).or_(self.page.locator(selector)).and_(self.page.locator("input:visible")).first
            if field.is_visible() and field.is_editable() and not field.input_value().strip():
                pending.append((index, label, field))
        return pending

    def _fill_signup_contact_names(self):
        pending = self._pending_signup_name_fields()
        if not pending:
            return False
        full_name = " ".join(self.contact_name.split())
        first_name = " ".join(self.data.get("联系人名", "").split())
        last_name = " ".join(self.data.get("联系人姓", "").split())
        # Preserve compound surnames from the contact source. Old records and
        # manually edited full names use the final word as the surname.
        if not first_name or not last_name or full_name != first_name + " " + last_name:
            parts = full_name.rsplit(None, 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) == 2 else ""
        values = (first_name, last_name)
        if any(not values[index] for index, _, _ in pending):
            raise RuntimeError("注册页要求 First name / Last name，但联系人姓名不完整，请补全本任务联系人资料")
        for index, label, field in pending:
            self._check_cancel()
            if not self._fill_locator(field, values[index]) or field.input_value().strip() != values[index]:
                raise RuntimeError("注册页联系人姓名填写失败：" + label)
            field.press("Tab")
        self._log("已按本任务联系人资料补填注册姓名（First name / Last name）")
        return True

    def _wait_password_result(self, submitted_url, timeout=90000, action="Create account"):
        """Observe one submission instead of retyping/clicking on every loop."""
        started = time.monotonic()
        next_log = started + 30
        password = self.page.locator('input[type="password"]:visible, input[name="password"]:visible, input[autocomplete="new-password"]:visible').first
        challenge = self.page.locator("iframe[src*='challenges.cloudflare.com']:visible, #challenge-running:visible, .cf-challenge:visible").first
        code = self.page.locator('input[autocomplete="one-time-code"]:visible, input[name="verificationCode"]:visible').first
        while (time.monotonic() - started) * 1000 < timeout:
            self._check_cancel()
            if self.page.url != submitted_url or code.is_visible() or not password.is_visible():
                return
            if challenge.is_visible():
                self._maybe_wait_challenge()
                return
            if action == "Create account" and self._pending_signup_name_fields():
                # The server may request names after the initial submission.
                # Return to the form handler instead of refreshing them away.
                return
            if password.get_attribute("aria-invalid") == "true":
                raise RuntimeError("密码页提示输入有误，请检查页面的校验提示；已停止重复提交")
            self.page.wait_for_timeout(500)
            if time.monotonic() >= next_log:
                self._log(f"等待 {action} 提交结果、页面跳转或验证，暂不重复点击")
                next_log = time.monotonic() + 30
        # One last read before refreshing, in case a navigation just completed.
        if self.page.url != submitted_url or code.is_visible() or not password.is_visible():
            return
        if challenge.is_visible():
            self._maybe_wait_challenge()
            return
        if action == "Create account" and self._pending_signup_name_fields():
            return
        self._recover_password_page(f"{action} 提交后长时间停在同一密码页，未出现下一步")

    def _handle_password_step(self) -> bool:
        if not self._on_shopify() or self._on_account_profile():
            return False
        login_button = self.page.get_by_role("button", name=re.compile(r"^(Log in|Login|Sign in|登录)$", re.I)).and_(self.page.locator(":visible")).first
        is_login = login_button.is_visible() or (
            urlsplit(self.page.url).netloc == "accounts.shopify.com"
            and urlsplit(self.page.url).path.rstrip("/") == "/login"
        )
        action = "Log in" if is_login else "Create account"
        pwd_loc = None
        candidates = [
            self.page.get_by_label("Create a password"),
            self.page.get_by_label("Password"),
            self.page.locator('input[type="password"]'),
            self.page.locator('input[autocomplete="new-password"]'),
            self.page.locator('input[name="password"]'),
        ]
        for loc in candidates:
            current = loc.first
            if self._locator_ready(current, timeout=800, need_editable=True):
                pwd_loc = current
                if current.input_value() == self.password:
                    break
                if self._fill_locator(current, self.password):
                    self._log("已填写 Shopify 密码")
                break
        if not pwd_loc:
            # 密码已填好、输入框可能仍可见：只要页面有 Create account 也算这一步
            create_btn = login_button if is_login else self.page.get_by_role("button", name="Create account").first
            if not self._locator_ready(create_btn, timeout=600):
                return False
        else:
            try:
                pwd_loc.press("Tab")
            except Exception:
                pass
            self._sleep(short=True)

        submitted_url = self.page.url
        if pwd_loc and pwd_loc.input_value() != self.password:
            self._recover_password_page("密码输入后没有保留在字段中")
            return True
        if not is_login:
            self._fill_signup_contact_names()
        submit = self.page.get_by_role("button", name=re.compile(
            r"^(Log in|Login|Sign in|Continue|登录)$" if is_login else
            r"^(Create account|Create your account|Continue|创建账户)$", re.I
        )).and_(self.page.locator(":visible")).first
        challenge = self.page.locator("iframe[src*='challenges.cloudflare.com']:visible, #challenge-running:visible, .cf-challenge:visible").first

        def can_submit(wait):
            if self.page.url != submitted_url or challenge.is_visible():
                return True
            if not is_login and self._pending_signup_name_fields():
                return True
            return self._locator_ready(submit, timeout=wait) and submit.is_enabled()

        ready = wait_for_ready(self.page, can_submit, self._log, action + " 按钮", self._check_cancel,
                               timeout=120000, max_timeout=120000)
        # The button may become enabled as the wait expires. Re-read it before
        # deciding to refresh, so an actionable form is submitted immediately.
        if not ready and not can_submit(250):
            self._recover_password_page(action + " 按钮长时间不可用")
            return True
        if self.page.url != submitted_url:
            return True
        if challenge.is_visible():
            self._maybe_wait_challenge()
            return True
        if not is_login and self._fill_signup_contact_names():
            # Fields appeared while waiting. The next form pass checks the
            # button after input validation, without a refresh or forced click.
            return True
        try:
            # Normal actionability checks prevent a disabled button from being
            # force-clicked and incorrectly logged as a successful submission.
            submit.click(timeout=10000, no_wait_after=True)
            self._log(f"已点击：{action}；开始等待提交结果")
        except PlaywrightTimeout as exc:
            if self.page.url != submitted_url:
                return True
            if pwd_loc and pwd_loc.get_attribute("aria-invalid") == "true":
                raise RuntimeError("密码页提示输入有误，请检查页面的校验提示")
            if challenge.is_visible():
                self._maybe_wait_challenge()
                return True
            detail = str(exc)
            if ("intercepts pointer events" in detail and "click action done" not in detail
                    and submit.is_visible() and submit.is_enabled()
                    and not self.page.locator('[role="dialog"][aria-modal="true"]:visible').count()):
                # An enabled submit can be covered by a presentation span.
                # Activate the actual button with the keyboard; never strip a
                # disabled attribute or bypass a visible verification dialog.
                self._check_cancel()
                self._log(f"{action} 已可用，但鼠标点击被页面元素遮挡；改用键盘 Enter 提交")
                try:
                    submit.press("Enter", timeout=5000, no_wait_after=True)
                    self._log(f"已提交：{action}（键盘 Enter）；开始等待提交结果")
                except PlaywrightTimeout:
                    self._log("键盘提交结果待确认，先等待页面变化，暂不刷新")
            else:
                # A click may have been dispatched before navigation timed out.
                self._log(action + " 点击结果待确认，先等待页面变化，暂不刷新：" + detail.splitlines()[0])
        if is_login:
            self._wait_password_result(submitted_url, action=action)
        else:
            self._wait_password_result(submitted_url)
        return True

    def _handle_email_step(self) -> bool:
        candidates = [
            self.page.get_by_label("Email address"),
            self.page.locator('input[type="email"]'),
            self.page.locator('input[name="email"]'),
            self.page.locator('input[autocomplete="email"]'),
        ]
        filled = False
        for loc in candidates:
            if self._fill_locator(loc.first, self.email):
                self._log(f"已填写邮箱：{self.email}")
                filled = True
                break
        if not filled:
            return False
        self._sleep(short=True)
        self._click_button(["Continue with email", "Continue", "Start free trial", "继续使用邮箱", "继续"])
        return True

    def _handle_otp_step(self) -> bool:
        candidates = [
            self.page.locator('input[autocomplete="one-time-code"]'),
            self.page.locator('input[name="verificationCode"]'),
            self.page.get_by_label("Enter code"),
            self.page.get_by_label("Verification code"),
        ]
        box = None
        for loc in candidates:
            if self._locator_ready(loc.first, timeout=600, need_editable=True):
                box = loc.first
                break
        if not box:
            return False
        self._log("页面出现验证码输入框。")
        code = None
        if self.email_pwd:
            self._log("正在从邮箱读取验证码...")
            code = self._get_email_verification_code()
        if code:
            self._fill_locator(box, code)
            self._sleep(short=True)
            self._click_button(["Continue", "Verify", "Submit", "继续", "验证"])
            return True
        if self.config.getboolean("settings", "manual_verify_fallback", fallback=True):
            answer = self._ask_user("请填写邮箱验证码，或在 AdsPower 窗口完成后继续...", kind="otp")
            if answer.strip():
                if not re.fullmatch(r"\d{6}", answer.strip()):
                    raise RuntimeError("邮箱验证码应为 6 位数字")
                self._fill_locator(box, answer.strip())
                self._click_button(["Continue", "Verify", "Submit", "继续", "验证"])
            return True
        raise RuntimeError("获取邮箱验证码失败，且未开启手动验证。")

    def _handle_store_details_step(self) -> bool:
        """Let's get started：用途随机选，填店铺名，点 Continue。"""
        help_q = self.page.get_by_text("What can we help you do?", exact=False).first
        name_q = self.page.get_by_text("What should we call your store?", exact=False).first
        on_page = self._locator_ready(help_q, timeout=500) or self._locator_ready(name_q, timeout=400)
        if not on_page:
            return False
        chips = [
            "Sell online",
            "Sell in-store",
            "Dropshipping",
            "Sell digital products",
            "Move existing store",
        ]
        extras = chips[1:]
        n = secrets.choice([0, 1, 2])
        picks = secrets.SystemRandom().sample(extras, n) if n else []
        if not picks:
            self._log("用途保持默认：Sell online")
        for label in picks:
            clicked = False
            for loc in (
                self.page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first,
                self.page.get_by_text(label, exact=False).first,
            ):
                if not self._locator_ready(loc, timeout=600):
                    continue
                try:
                    loc.click(timeout=2500)
                    self._log(f"已随机选择：{label}")
                    clicked = True
                    self._sleep(short=True)
                    break
                except Exception:
                    continue
            if not clicked:
                self._log(f"未点到选项：{label}")
        name_box = None
        for loc in (
            self.page.get_by_placeholder(re.compile(r"My Store", re.I)).first,
            self.page.get_by_label("Store name").first,
            self.page.get_by_label("Shop name").first,
        ):
            if self._locator_ready(loc, timeout=600, need_editable=True):
                name_box = loc
                break
        if name_box and self.shop_name:
            self._fill_locator(name_box, self.shop_name)
            self._log(f"已填写店铺名：{self.shop_name}")
        self._sleep(short=True)
        if self._click_button(["Continue", "Next", "继续"]):
            return True
        return True

    def _handle_shop_name_step(self) -> bool:
        if self._locator_ready(self.page.get_by_text("What can we help you do?", exact=False).first, timeout=400):
            return False
        candidates = [
            self.page.get_by_placeholder(re.compile(r"My Store", re.I)),
            self.page.get_by_label("Store name"),
            self.page.get_by_label("Shop name"),
            self.page.locator('input[name="shopName"]'),
            self.page.locator('input[name="shop_name"]'),
        ]
        filled = False
        for loc in candidates:
            if self._fill_locator(loc.first, self.shop_name):
                self._log(f"已填写店铺名：{self.shop_name}")
                filled = True
                break
        if not filled:
            return False
        self._sleep(short=True)
        self._click_button(["Continue", "Next", "Save", "继续", "下一步"])
        return True

    def _handle_survey(self) -> bool:
        if self._locator_ready(self.page.get_by_text("What can we help you do?", exact=False).first, timeout=400):
            return False
        if self._locator_ready(self.page.get_by_text("Let's get started", exact=False).first, timeout=300):
            return False
        return self._click_button(["Skip all", "Skip", "跳过全部", "跳过"])

    def _handle_skip_offer(self) -> bool:
        """订阅优惠页右上角 Skip，跳过付款继续试用。"""
        if self._locator_ready(self.page.get_by_text("What can we help you do?", exact=False).first, timeout=300):
            return False
        skip = self.page.get_by_role("button", name="Skip").or_(
            self.page.get_by_role("link", name="Skip")
        ).or_(self.page.locator('a:has-text("Skip")')).or_(
            self.page.locator('button:has-text("Skip")')
        ).first
        if not self._locator_ready(skip, timeout=1200):
            return False
        try:
            skip.click(timeout=4000, force=True)
            self._log("已点击右上角 Skip")
            return True
        except Exception as e:
            self._log(f"点击 Skip 失败：{e}")
            return self._click_button(["Skip", "Skip for now", "Maybe later", "跳过"])

    def _handle_profile_step(self) -> bool:
        did = False
        if self._fill_first(['input[name="contactName"]', 'input[autocomplete="name"]'], self.contact_name):
            did = True
        if self.country and self._fill_first(['input[name="country"]', 'select[name="country"]'], self.country):
            did = True
        if self._fill_first(['input[name="addressLine1"]', 'input[autocomplete="address-line1"]'], self.address):
            did = True
        if self._fill_first(['input[name="city"]', 'input[autocomplete="address-level2"]'], self.city):
            did = True
        if self._fill_first(['input[name="province"]', 'input[name="state"]'], self.province):
            did = True
        if self._fill_first(['input[name="zip"]', 'input[autocomplete="postal-code"]'], self.zip_code):
            did = True
        if self._fill_first(['input[type="tel"]', 'input[name="phone"]'], self.phone):
            did = True
        if did:
            self._click_button(["Continue", "Next", "Save", "Submit", "继续"])
        return did

    def _ensure_admin(self):
        """已登录则进后台；未登录才走注册。"""
        self._remember_admin()
        if self._looks_logged_in():
            self._log("当前已在 Shopify 后台，跳过注册，直接做后续设置。")
            return
        saved = admin_store_base(self.data.get("店铺后台")) or admin_store_base(self.page.url)
        if saved:
            if admin_store_base(self.page.url) == saved:
                self._log(f"沿用正在加载的后台：{saved}")
            else:
                self._log(f"打开已有后台：{saved}")
                try:
                    self.page.goto(saved, wait_until="commit", timeout=25000)
                except Exception as e:
                    self._log(f"打开已有后台失败：{e}")
                    if browser_disconnected(e) or not retryable_browser_error(e):
                        raise
            if self._wait_for_admin():
                return
            if admin_store_base(self.page.url) or browser_error_page(self.page.url):
                raise PageLoadError("已有店铺后台刷新后仍未加载，等待自动恢复")
            self._log("已有店铺需要登录，沿用已保存的账号密码继续")
        else:
            signup_url = self.config.get("settings", "shopify_signup_url",
                                         fallback="https://accounts.shopify.com/signup")
            self._log(f"打开 Shopify 注册入口：{signup_url}")
            self._goto_signup(signup_url)
        self._sleep(short=True)
        self._maybe_wait_challenge()
        for step in range(20):
            self._check_cancel()
            if self._looks_logged_in():
                self._log("已进入 Shopify 后台。")
                return
            if admin_store_base(self.page.url):
                if self._wait_for_admin():
                    self._log("刷新后已进入 Shopify 后台。")
                    return
                raise PageLoadError("店铺后台刷新后仍未加载，等待自动恢复")
            acted = (
                self._continue_from_account_profile()
                or self._handle_account_selection()
                or self._handle_store_selection()
                or self._handle_skip_offer()
                or self._handle_password_step()
                or self._handle_otp_step()
                or self._handle_email_step()
                or (not saved and (
                    self._handle_store_details_step()
                    or self._handle_shop_name_step()
                    or self._handle_survey()
                    or self._handle_profile_step()
                ))
            )
            if not acted:
                if saved:
                    # Unknown existing-store screens require confirmation;
                    # never fall into the generic new-account actions.
                    break
                self._click_button(["Continue", "Next", "Create account", "Skip", "继续"])
            self._sleep(short=True)
            self._maybe_wait_challenge()
            self._log(f"步骤 {step + 1}，当前地址：{self.page.url}")
        if self.config.getboolean("settings", "manual_verify_fallback", fallback=True):
            self._ask_user("已有店铺后台尚未加载或需要确认，请在 AdsPower 中完成登录后继续..." if saved else
                           "页面尚未进入后台，请在 AdsPower 中完成登录或注册后继续...")
        if not self._wait_for_admin():
            raise RuntimeError(f"未能进入{'已有店铺后台' if saved else '后台'}，停在：{self.page.url}")

    def _finish_with_setup(self):
        from store_setup import StoreSetup
        if not self._looks_logged_in():
            raise RuntimeError("尚未确认进入真实店铺后台，未执行资料和政策设置；请先完成登录或注册")
        self._log("开始店铺设置：资料 / 退货规则 / 书面政策（主题不做）...")
        setup_result = StoreSetup(self).run()
        notes = setup_result.get("setup_notes") or ""
        admin_url = setup_result.get("admin_url") or self.page.url
        self._log(f"店铺设置结束：{notes}")
        if setup_result.get("ok"):
            return {
                "status": "success",
                "shop_name": self.shop_name,
                "password": self.password,
                "admin_url": admin_url,
                "setup_notes": notes,
            }
        return {
            "status": "failed",
            "shop_name": self.shop_name,
            "password": self.password,
            "admin_url": admin_url,
            "setup_notes": notes,
            "error": f"后续设置未完成：{notes}",
            "retryable": setup_result.get("retryable", False),
        }

    def _run_browser_attempt(self):
        self._log("正在启动 AdsPower 环境...")
        ws = self.ads.start(self.profile_id)
        self._started_profile = True
        self._log(f"环境已启动，CDP：{ws}")
        with sync_playwright() as p:
            try:
                self.browser = p.chromium.connect_over_cdp(ws)
                if not self.browser.contexts:
                    raise PageLoadError("浏览器连接尚未就绪")
                context = self.browser.contexts[0]
                pages = [page for page in context.pages if not page.is_closed()]
                saved = admin_store_base(self.data.get("店铺后台"))
                candidates = [page for page in pages if admin_store_base(page.url) == saved] if saved else [
                    page for page in pages if re.match(r"^https://(?:admin|accounts|www)\.shopify\.com/", page.url)
                ]
                if not candidates and saved:
                    errors = [page for page in pages if browser_error_page(page.url)]
                    if len(errors) == 1:
                        candidates = errors
                self.page = candidates[0] if candidates else context.new_page()
                self.page.set_default_timeout(8000)
                self._ensure_admin()
                self.data["店铺后台"] = self.admin_url
                return self._finish_with_setup()
            except Exception:
                try:
                    if self.page and not self.page.is_closed():
                        screenshot = f"error_{self.shop_name}_{time.strftime('%Y%m%d%H%M%S')}.png"
                        self.page.screenshot(path=screenshot, timeout=5000)
                        self._log(f"错误页面已截图：{screenshot}")
                except Exception:
                    pass
                raise

    def register(self):
        self._log(f"\n=== 开始处理：邮箱={self.email}，店铺名={self.shop_name}，环境={self.profile_id} ===")
        result = {}
        recoveries = max(0, self.config.getint("settings", "browser_recovery_attempts", fallback=2))
        try:
            for attempt in range(recoveries + 1):
                self._check_cancel()
                try:
                    result = self._run_browser_attempt()
                except FirstPageUnavailable as exc:
                    result = {"status": "failed", "shop_name": self.shop_name, "error": str(exc),
                              "password": self.password, "retryable": False,
                              "failure_code": "first_page_unavailable"}
                except Exception as exc:
                    result = {"status": "failed", "shop_name": self.shop_name, "error": str(exc),
                              "password": self.password, "retryable": retryable_browser_error(exc)}
                if result.get("status") == "success":
                    return result
                self._log("本次处理未完成：" + result.get("error", "未知错误"))
                if not result.get("retryable") or attempt == recoveries:
                    break
                self._log(f"自动恢复（{attempt + 1}/{recoveries}）：重新连接原环境 {self.profile_id}，继续未完成步骤")
                self._sleep()
            if result.get("retryable") and recoveries:
                self._log("自动恢复次数已用完，已保留任务进度")
            return result
        finally:
            if (result.get("status") == "success" and self._started_profile
                    and self.config.getboolean("settings", "close_browser_after", fallback=False)):
                self.ads.stop(self.profile_id)
            elif self._started_profile:
                self._log("保留 AdsPower 环境和任务进度，未主动关闭窗口。")
