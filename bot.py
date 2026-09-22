import email
import imaplib
import re
import secrets
import string
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from adspower_client import AdsPowerClient


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

    def _generate_password(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return "Shp!" + "".join(secrets.choice(alphabet) for _ in range(12))

    def _log(self, msg):
        print(msg, flush=True)

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
        return "shopify.com" in (self.page.url or "").lower()

    def _goto_signup(self, url):
        current = (self.page.url or "").lower()
        if "shopify.com" in current and ("signup" in current or "password" in current or "challenge" in current):
            self._log(f"已在注册流程中，跳过重复打开：{self.page.url}")
            return
        last_error = None
        for i in range(1, 3):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
                return
            except Exception as e:
                last_error = e
                self._log(f"打开页面失败（{i}/2）：{str(e).splitlines()[0]}")
                if self._on_shopify():
                    self._log("导航报错但页面已在 Shopify，继续。")
                    return
                time.sleep(2)
        if self._on_shopify():
            return
        raise RuntimeError(
            "无法打开 Shopify 注册页，AdsPower 环境的代理连不上。"
            f"请先在环境 {self.profile_id} 里把代理测通。原始错误：{last_error}"
        )

    def _maybe_wait_challenge(self):
        overlay = self.page.locator(
            "iframe[src*='challenges.cloudflare.com'], #challenge-running, .cf-challenge"
        ).first
        try:
            if overlay.is_visible(timeout=600):
                input("检测到人机验证，请在 AdsPower 窗口中完成后回到这里按回车继续...")
        except PlaywrightTimeout:
            return

    def _looks_logged_in(self) -> bool:
        url = (self.page.url or "").lower()
        if "admin.shopify.com/store" in url:
            return True
        if "admin.shopify.com" in url and "signup" not in url and "accounts.shopify.com" not in url:
            return True
        return False

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

    def _handle_password_step(self) -> bool:
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
                if self._fill_locator(current, self.password):
                    self._log("已填写 Shopify 密码")
                break
        if not pwd_loc:
            # 密码已填好、输入框可能仍可见：只要页面有 Create account 也算这一步
            create_btn = self.page.get_by_role("button", name="Create account").first
            if not self._locator_ready(create_btn, timeout=600):
                return False
        else:
            try:
                pwd_loc.press("Tab")
            except Exception:
                pass
            self._sleep(short=True)

        clicked = self._click_button(
            ["Create account", "Create your account", "Continue", "创建账户"]
        )
        if not clicked and pwd_loc:
            try:
                pwd_loc.press("Enter")
                self._log("已在密码框按回车提交")
                clicked = True
            except Exception as e:
                self._log(f"回车提交失败：{e}")
        return clicked or pwd_loc is not None

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
            input("请在 AdsPower 窗口中填写邮箱验证码并点继续，完成后回到这里按回车...")
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
        if self._looks_logged_in():
            self._log("当前已在 Shopify 后台，跳过注册，直接做后续设置。")
            return
        saved = (self.data.get("店铺后台") or "").strip()
        if "admin.shopify.com/store/" in saved:
            self._log(f"打开已有后台：{saved}")
            try:
                self.page.goto(saved, wait_until="domcontentloaded", timeout=25000)
                self._sleep(short=True)
            except Exception as e:
                self._log(f"打开已有后台失败：{e}")
            if self._looks_logged_in():
                return
        try:
            self.page.goto("https://admin.shopify.com", wait_until="domcontentloaded", timeout=25000)
            self._sleep(short=True)
        except Exception:
            pass
        if self._looks_logged_in():
            self._log("已登录现有店铺，跳过注册。")
            return

        signup_url = self.config.get("settings", "shopify_signup_url")
        self._log(f"未登录，开始注册：{signup_url}")
        self._goto_signup(signup_url)
        self._sleep(short=True)
        self._maybe_wait_challenge()
        for step in range(20):
            if self._looks_logged_in():
                self._log("已进入 Shopify 后台。")
                return
            acted = (
                self._handle_skip_offer()
                or self._handle_password_step()
                or self._handle_otp_step()
                or self._handle_email_step()
                or self._handle_store_details_step()
                or self._handle_shop_name_step()
                or self._handle_survey()
                or self._handle_profile_step()
            )
            if not acted:
                self._click_button(["Continue", "Next", "Create account", "Skip", "继续"])
            self._sleep(short=True)
            self._maybe_wait_challenge()
            self._log(f"步骤 {step + 1}，当前地址：{self.page.url}")
        if self.config.getboolean("settings", "manual_verify_fallback", fallback=True):
            input("页面尚未进入后台。请在 AdsPower 中登录或完成注册，进入后台后回到这里按回车...")
        if not self._looks_logged_in():
            raise RuntimeError(f"未能进入后台，停在：{self.page.url}")

    def _finish_with_setup(self):
        from store_setup import StoreSetup
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
        }

    def register(self):
        self._log(f"\n=== 开始处理：邮箱={self.email}，店铺名={self.shop_name}，环境={self.profile_id} ===")
        try:
            self._log("正在启动 AdsPower 环境...")
            ws = self.ads.start(self.profile_id)
            self._started_profile = True
            self._log(f"环境已启动，CDP：{ws}")

            with sync_playwright() as p:
                self.browser = p.chromium.connect_over_cdp(ws)
                context = self.browser.contexts[0]
                self.page = context.pages[0] if context.pages else context.new_page()
                self.page.set_default_timeout(8000)
                self._ensure_admin()
                return self._finish_with_setup()

        except Exception as e:
            self._log(f"处理失败：{e}")
            screenshot = f"error_{self.shop_name}_{time.strftime('%Y%m%d%H%M%S')}.png"
            try:
                if self.page:
                    self.page.screenshot(path=screenshot)
                    self._log(f"错误页面已截图：{screenshot}")
            except Exception:
                pass
            return {"status": "failed", "shop_name": self.shop_name, "error": str(e), "password": self.password}

        finally:
            if self.config.getboolean("settings", "close_browser_after", fallback=False) and self._started_profile:
                self.ads.stop(self.profile_id)
            elif self._started_profile:
                self._log("已按配置保持 AdsPower 窗口打开，便于你继续设置店铺。")
