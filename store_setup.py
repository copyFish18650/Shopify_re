import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout


class StoreSetup:
    """注册完成后的店铺初始化：资料、退货规则、书面政策。主题不做。"""

    def __init__(self, bot):
        self.bot = bot
        self.page = bot.page
        self.data = bot.data

    def _log(self, msg):
        self.bot._log(msg)

    def _sleep(self, short=True):
        self.bot._sleep(short=short)

    def admin_base(self) -> str:
        m = re.search(r"(https://admin\.shopify\.com/store/[^/?#]+)", self.page.url or "")
        if m:
            return m.group(1)
        handle = re.sub(r"[^a-z0-9-]", "", (self.bot.shop_name or "").lower())
        return f"https://admin.shopify.com/store/{handle}"

    POLICY_PATHS = {
        "Return and refund policy": "settings/legal/refund",
        "Privacy policy": "settings/legal/privacy",
        "Terms of service": "settings/legal/terms-of-service",
        "Shipping policy": "settings/legal/shipping",
        "Contact information": "settings/legal/contact-information",
        "Legal notice": "settings/legal/legal-notice",
        "Terms of sale": "settings/legal/terms-of-sale",
    }

    def _goto_admin(self, path: str, dismiss=True):
        url = self.admin_base().rstrip("/") + "/" + path.lstrip("/")
        self._log(f"打开：{url}")
        # 使用后台已有导航，避免每个步骤都重新加载整个 Shopify 应用。
        link = self.page.locator(f'a[href="{urlsplit(url).path}"]:visible').first
        if link.count():
            link.click(timeout=10000)
        else:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self._sleep()
        # 设置本身也是弹层；通用 Close/Escape 会把设置页一起关掉。
        if dismiss and not path.lstrip("/").startswith("settings"):
            self._dismiss_modals()

    def _dismiss_modals(self, allow_escape=True):
        for name in ("Skip", "Close", "Not now", "Maybe later", "Got it", "跳过"):
            try:
                loc = self.page.get_by_role("button", name=name).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=1500)
                    self._sleep()
            except Exception:
                pass
        if allow_escape:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

    def _click(self, names, timeout=4000) -> bool:
        return self.bot._click_button(names, timeout=timeout)

    def _visible(self, loc, timeout=800) -> bool:
        return self.bot._locator_ready(loc, timeout=timeout)

    def run(self) -> dict:
        notes = []
        csv_path = (self.data.get("产品CSV") or "").strip()

        try:
            self._dismiss_modals()
            self.bot._handle_skip_offer()
        except Exception as e:
            notes.append(f"关闭弹窗：{e}")

        try:
            self.setup_store_profile()
            notes.append("店铺资料已核对")
        except Exception as e:
            notes.append(f"店铺资料失败：{e}")
            self._log(notes[-1])

        try:
            self.setup_return_rules()
            notes.append("退货规则已保存")
        except Exception as e:
            notes.append(f"退货规则失败：{e}")
            self._log(notes[-1])

        try:
            policy_notes = self.setup_written_policies()
            notes.append("书面政策已发布")
            notes.extend(policy_notes or [])
        except Exception as e:
            notes.append(f"书面政策失败：{e}")
            self._log(notes[-1])

        if csv_path:
            try:
                self.import_products(csv_path)
                notes.append("产品已导入")
            except Exception as e:
                notes.append(f"导入产品失败：{e}")
                self._log(notes[-1])
        else:
            notes.append("未填产品CSV，跳过导入")
            self._log(notes[-1])

        ok = (
            "失败" not in "；".join(notes)
            and "退货规则已保存" in notes
            and "书面政策已发布" in notes
        )
        if csv_path and "产品已导入" not in notes:
            ok = False
        return {"ok": ok, "setup_notes": "；".join(notes), "admin_url": self.page.url}

    def _page_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def _close_dialog(self):
        for name in ("Cancel", "Close", "Done"):
            try:
                loc = self.page.get_by_role("button", name=name).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=1500)
                    self._sleep()
                    return
            except Exception:
                pass
        try:
            self.page.locator('[aria-label="Close"], button[aria-label="Close"]').last.click(timeout=1500)
            self._sleep()
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

    def _full_address(self) -> str:
        bits = [
            self.bot.address,
            self.bot.city,
            self.bot.province,
            self.bot.zip_code,
            self.bot.country,
        ]
        return ", ".join(b for b in bits if b)

    POLICY_FILES = {
        "Return and refund policy": "refund.txt",
        "Privacy policy": "privacy.txt",
        "Terms of service": "service.txt",
        "Shipping policy": "shipping.txt",
    }

    def _contact_block(self) -> str:
        return (
            f"Trade name: {self.bot.shop_name or ''}\n"
            f"Phone number: {self.bot.phone or ''}\n"
            f"Email: {self.bot.email or ''}\n"
            f"Physical address: {self._full_address() or self.bot.country or ''}"
        )

    def _policy_file_dir(self) -> Path:
        return Path(__file__).resolve().parent / "信息"

    def _load_branded_policy(self, filename: str) -> str:
        path = self._policy_file_dir() / filename
        if not path.is_file():
            raise FileNotFoundError(f"找不到政策文件：{path}")
        text = path.read_text(encoding="utf-8")
        shop = self.bot.shop_name or "Our Store"
        mail = self.bot.email or ""
        text = re.sub(r"support@sculpfun\.com", mail, text, flags=re.I)
        text = re.sub(r"SCULPFUN|Sculpfun|sculpfun", shop, text)
        return text

    def _field_value(self, label: str) -> str:
        loc = self.page.get_by_label(re.compile(label, re.I)).first
        if not self._visible(loc, timeout=1200):
            return ""
        try:
            return (loc.input_value() or "").strip()
        except Exception:
            try:
                return (loc.inner_text() or "").strip()
            except Exception:
                return ""

    def _fill_profile_field(self, label: str, value: str, replace_default=False) -> bool:
        """按可访问标签定位字段，保留已填写内容（默认店名除外）。"""
        if not value:
            return False
        loc = self.page.get_by_label(re.compile(label, re.I)).first
        if not self._visible(loc, timeout=3000):
            raise RuntimeError(f"找不到店铺资料字段：{label}")
        current = (loc.input_value() or "").strip()
        is_default = replace_default and current.lower() in ("my store", "我的商店", "我的店铺")
        if current and not is_default or current == value:
            return False
        if loc.evaluate("el => el.tagName.toLowerCase()") == "select":
            options = loc.locator("option").evaluate_all(
                "els => els.map(el => ({value: el.value, label: el.textContent.trim()}))"
            )
            match = next((o for o in options if value.casefold() in (
                o["value"].casefold(), o["label"].casefold()
            )), None)
            if match is None:
                raise RuntimeError(f"店铺资料选项不匹配：{label}={value}")
            loc.select_option(value=match["value"])
        elif not self.bot._fill_locator(loc, value):
            raise RuntimeError(f"填写店铺资料失败：{label}")
        return True

    def _save_profile_changes(self):
        save = self.page.get_by_role("button", name=re.compile(r"^(Save|保存)$", re.I)).last
        save.click(timeout=10000)
        for _ in range(60):
            if not save.is_visible() or save.is_disabled():
                return
            self.page.wait_for_timeout(250)
        raise RuntimeError("店铺资料点击保存后仍有未保存内容，请检查页面校验提示")

    def _open_store_contact_details(self):
        self._goto_admin("settings/general", dismiss=False)
        link = self.page.locator('a[href$="/settings/general/store-contact-details"]:visible').first
        field = self.page.get_by_label(re.compile(r"^(Store name|店铺名称)$", re.I)).first
        try:
            link.or_(field).first.wait_for(state="visible", timeout=30000)
            if not field.is_visible():
                link.click(timeout=10000)
            field.wait_for(state="visible", timeout=30000)
        except PlaywrightTimeout as e:
            raise RuntimeError(
                f"店铺联系信息页面未加载或入口已变化：{self.page.url}；请检查 Shopify 页面是否提示加载错误"
            ) from e

    def setup_store_profile(self):
        """兼容常规页直接编辑和新版独立联系信息页，补齐空白字段。"""
        self._open_store_contact_details()
        changed = False
        for label, value, replace_default in (
            (r"^(Store name|店铺名称)$", self.bot.shop_name, True),
            (r"^(Store email|店铺邮箱)$", self.bot.email, False),
            (r"^(Store phone|店铺电话)$", self.bot.phone, False),
        ):
            changed = self._fill_profile_field(label, value, replace_default) or changed
        if changed:
            self._save_profile_changes()
            self._log("已保存店铺联系信息")
        else:
            self._log("店铺联系信息已填写，跳过")

        if not (self.bot.address or self.bot.city):
            self._log("未提供店铺地址，跳过")
            return

        self._goto_admin("settings/general", dismiss=False)
        address = self.page.get_by_role("button", name=re.compile(r"^(Store address|店铺地址)$", re.I)).first
        if self._visible(address, timeout=5000):
            address.click(timeout=5000)
        elif not self._click(["Edit store address", "Edit", "编辑"], timeout=3000):
            raise RuntimeError("找不到店铺地址编辑入口")
        street = self.page.get_by_label(re.compile(r"^(Street and house number|Address|地址)$", re.I)).first
        street.wait_for(state="visible", timeout=15000)
        changed = False
        for label, value in (
            (r"^(Company name|公司名称)$", self.bot.shop_name),
            (r"^(Street and house number|Address|地址)$", self.bot.address),
            (r"^(City|城市)$", self.bot.city),
            (r"^(ZIP code|Postal code|邮编)$", self.bot.zip_code),
            (r"^(Province|State|State/province|州/省)$", self.bot.province),
        ):
            changed = self._fill_profile_field(label, value) or changed
        if changed:
            self._save_profile_changes()
            street.wait_for(state="hidden", timeout=15000)
            self._log("已保存店铺地址")
        else:
            self._log("店铺地址已填写，关闭窗口")
            self._close_dialog()

    def _products_already_imported(self) -> bool:
        empty = self.page.get_by_text(re.compile(r"Start by stocking your store", re.I)).first
        add_heading = self.page.get_by_role("heading", name=re.compile(r"Add your products", re.I)).first
        rows = self.page.locator(
            'table tbody tr, [class*="IndexTable"] [class*="TableRow"], [class*="ResourceItem"]'
        )
        for _ in range(20):
            empty_vis = self._visible(empty, timeout=250) or self._visible(add_heading, timeout=200)
            if empty_vis:
                return False
            try:
                n = rows.count()
            except Exception:
                n = 0
            if n >= 1:
                return True
            self.page.wait_for_timeout(350)
        return False

    def import_products(self, csv_path: str):
        path = Path(csv_path)
        if not path.is_file():
            raise FileNotFoundError(f"找不到产品 CSV：{csv_path}")
        self._goto_admin("products")
        self._dismiss_modals()
        try:
            self.page.get_by_role("heading", name=re.compile(r"Products|Add your products", re.I)).first.wait_for(
                timeout=20000
            )
        except PlaywrightTimeout:
            pass
        if self._products_already_imported():
            self._log("产品列表已有商品，跳过导入")
            return

        if not self._click(["Import", "导入"], timeout=5000):
            raise RuntimeError("找不到 Import 按钮")
        self._sleep()

        csv_radio = self.page.get_by_text("Upload a Shopify-formatted CSV file", exact=False).first
        if self._visible(csv_radio, timeout=2500):
            try:
                csv_radio.click(timeout=2000)
            except Exception:
                pass
            self._click(["Next", "下一步"], timeout=4000)
            self._sleep()

        file_input = self.page.locator('input[type="file"]').first
        try:
            file_input.set_input_files(str(path.resolve()), timeout=8000)
        except Exception:
            with self.page.expect_file_chooser(timeout=8000) as fc:
                self._click(["Add file", "Add files", "添加文件"])
            fc.value.set_files(str(path.resolve()))
        self._sleep()

        publish_all = self.page.get_by_text("Publish new products to all sales channels", exact=False).first
        if self._visible(publish_all, timeout=1500):
            try:
                box = publish_all.locator("xpath=ancestor::label[1]").locator("input[type=checkbox]").first
                if box.count() and not box.is_checked():
                    box.check()
            except Exception:
                pass

        if not self._click(["Upload and preview", "Upload & preview", "上传并预览"], timeout=8000):
            self._click(["Continue", "下一步"])
        self.page.get_by_role("button", name=re.compile(r"Import products", re.I)).wait_for(
            state="visible", timeout=60000
        )
        self._click(["Import products", "导入产品"], timeout=8000)
        self._log("已确认导入产品，等待完成...")
        try:
            self.page.get_by_text(re.compile(r"imported|import complete|products added", re.I)).wait_for(
                timeout=180000
            )
        except PlaywrightTimeout:
            self._log("未等到导入完成提示，继续后续步骤。")
        self._sleep()

    def _switch_for(self, heading: str):
        title = self.page.get_by_text(heading, exact=True).first
        if not self._visible(title, timeout=2500):
            return None
        sw = title.locator("xpath=following::*[@role='switch'][1]")
        try:
            if sw.count() and sw.first.is_visible(timeout=800):
                return sw.first
        except Exception:
            pass
        return None

    def _switch_is_on(self, heading: str) -> bool:
        sw = self._switch_for(heading)
        if sw is None:
            return False
        try:
            return sw.get_attribute("aria-checked") == "true"
        except Exception:
            return False

    def _turn_on_switch(self, heading: str) -> bool:
        sw = self._switch_for(heading)
        if sw is None:
            self._log(f"找不到开关：{heading}")
            return False
        try:
            if sw.get_attribute("aria-checked") != "true":
                sw.click(timeout=3000)
                self._log(f"已开启 {heading}")
            return True
        except Exception as e:
            self._log(f"开启 {heading} 失败：{e}")
            return False

    def _checkbox_control(self, label: str):
        role = self.page.get_by_role("checkbox", name=re.compile(label, re.I)).first
        if self._visible(role, timeout=800):
            return role
        loc = self.page.get_by_text(label, exact=False).first
        if not self._visible(loc, timeout=1200):
            return None
        try:
            box = loc.locator("xpath=ancestor::label[1]").locator("input[type=checkbox], [role='checkbox']").first
            if box.count() == 0:
                box = loc.locator("xpath=preceding::input[@type='checkbox'][1]")
            if box.count() and box.first.is_visible(timeout=400):
                return box.first
        except Exception:
            pass
        return loc

    def _control_is_checked(self, loc) -> bool:
        if loc is None:
            return False
        try:
            aria = loc.get_attribute("aria-checked")
            if aria is not None:
                return aria == "true"
        except Exception:
            pass
        try:
            if loc.evaluate("el => el.tagName && (el.tagName.toLowerCase() === 'input' || el.getAttribute('role') === 'checkbox')"):
                return bool(loc.is_checked())
        except Exception:
            pass
        try:
            inner = loc.locator("input[type=checkbox], [role='checkbox']").first
            if inner.count():
                aria = inner.get_attribute("aria-checked")
                if aria is not None:
                    return aria == "true"
                return bool(inner.is_checked())
        except Exception:
            pass
        return False

    def _checkbox_is_checked(self, label: str) -> bool:
        return self._control_is_checked(self._checkbox_control(label))

    def _set_checkbox(self, label: str, checked: bool) -> bool:
        loc = self._checkbox_control(label)
        if loc is None:
            self._log(f"找不到勾选：{label}")
            return False
        if self._control_is_checked(loc) == checked:
            return True
        try:
            loc.click(timeout=3000)
            self._log(f"{'已勾选' if checked else '已取消'}：{label}")
            self._sleep()
            return True
        except Exception as e:
            self._log(f"设置勾选失败 {label}：{e}")
            return False

    def _radio_control(self, label: str):
        role = self.page.get_by_role("radio", name=re.compile(rf"^{re.escape(label)}$", re.I)).first
        if self._visible(role, timeout=800):
            return role
        loc = self.page.get_by_text(label, exact=True).first
        if self._visible(loc, timeout=1200):
            return loc
        return None

    def _radio_is_selected(self, label: str) -> bool:
        loc = self._radio_control(label)
        if loc is None:
            return False
        try:
            aria = loc.get_attribute("aria-checked")
            if aria is not None:
                return aria == "true"
        except Exception:
            pass
        try:
            return bool(loc.is_checked())
        except Exception:
            return False

    def _select_radio(self, label: str) -> bool:
        loc = self._radio_control(label)
        if loc is None:
            self._log(f"找不到选项：{label}")
            return False
        if self._radio_is_selected(label):
            return True
        try:
            loc.click(timeout=3000)
            self._log(f"已选：{label}")
            self._sleep()
            return True
        except Exception as e:
            self._log(f"选择 {label} 失败：{e}")
            return False

    def _check_box_by_label(self, label: str):
        self._set_checkbox(label, True)

    def _return_rules_list_text(self) -> str:
        loc = self.page.locator("#return-rules")
        try:
            if self._visible(loc, timeout=2000):
                return loc.inner_text() or ""
        except Exception:
            pass
        return ""

    def _return_rules_list_has_values(self, list_txt: str = "") -> bool:
        text = list_txt or self._return_rules_list_text()
        return bool(re.search(r"\d+\s*-?\s*day|until fulfilled|\d+\s*minute", text, re.I))

    def _open_default_rules(self) -> bool:
        """Policies 列表里 Default rules 是 a[href*=cancel-return-rules]，不是按钮。"""
        self._goto_admin("settings/legal")
        self.page.locator("#return-rules").wait_for(state="visible", timeout=20000)
        existing = self.page.locator(
            '#return-rules a[href*="/settings/legal/cancel-return-rules/"]:not([href$="/new"])'
        ).first
        create = self.page.locator(
            '#return-rules a[href*="/settings/legal/cancel-return-rules/"]'
        ).first
        link = existing if self._visible(existing, timeout=2500) else create
        if not self._visible(link, timeout=4000):
            link = self.page.get_by_role("link", name=re.compile(r"Default rules", re.I)).first
        if not self._visible(link, timeout=3000):
            self._log("找不到 Default rules 链接")
            return False
        href = link.get_attribute("href") or ""
        self._log(f"打开 Default rules：{href}")
        try:
            link.click(timeout=5000)
        except Exception as e:
            self._log(f"点击 Default rules 失败：{e}")
        try:
            self.page.wait_for_url(re.compile(r"cancel-return-rules/"), timeout=8000)
        except PlaywrightTimeout:
            if href.startswith("/"):
                self.page.goto(
                    "https://admin.shopify.com" + href,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
            elif href:
                self.page.goto(href, wait_until="domcontentloaded", timeout=45000)
            else:
                return False
        self._sleep()
        if not re.search(r"cancel-return-rules/", self.page.url or ""):
            self._log(f"未进入 Default rules 页：{self.page.url}")
            return False
        return True

    def _turn_on_all_rule_switches(self):
        switches = self.page.locator('[role="switch"]')
        n = 0
        try:
            n = switches.count()
        except Exception:
            n = 0
        self._log(f"找到规则开关 {n} 个")
        for i in range(min(n, 4)):
            sw = switches.nth(i)
            try:
                if sw.get_attribute("aria-checked") != "true":
                    sw.click(timeout=3000, force=True)
                    self._log(f"已开启规则开关 {i + 1}")
            except Exception as e:
                self._log(f"开关 {i + 1}：{e}")

    def _select_dropdown(self, hints, option: str) -> bool:
        want = option.strip().lower()
        try:
            n = self.page.locator("select").count()
        except Exception:
            n = 0
        for i in range(n):
            sel = self.page.locator("select").nth(i)
            try:
                labels = [t.strip() for t in sel.locator("option").all_inner_texts()]
            except Exception:
                continue
            match = next((t for t in labels if t.lower() == want), None)
            if not match:
                continue
            try:
                current = (sel.evaluate("s => (s.options[s.selectedIndex] || {}).text || ''") or "").strip()
            except Exception:
                current = ""
            if current.lower() == want:
                self._log(f"下拉已是：{option}")
                return True
            try:
                sel.select_option(label=match)
                self._log(f"下拉已选：{option}")
                self._sleep()
                return True
            except Exception as e:
                self._log(f"原生 select 选择 {option} 失败：{e}")
                break

        opened = False
        for hint in hints:
            for loc in (
                self.page.get_by_label(re.compile(hint, re.I)).first,
                self.page.get_by_role("combobox", name=re.compile(hint, re.I)).first,
                self.page.locator("s-select, [role='combobox']").filter(has_text=re.compile(hint, re.I)).first,
                self.page.get_by_text(hint, exact=False).first,
            ):
                if not self._visible(loc, timeout=700):
                    continue
                try:
                    loc.click(timeout=2500)
                    opened = True
                    self._sleep()
                    break
                except Exception:
                    continue
            if opened:
                break
        if not opened:
            return False
        opt = self.page.get_by_role("option", name=re.compile(rf"^{re.escape(option)}$", re.I)).first
        if not self._visible(opt, timeout=1500):
            opt = self.page.get_by_role("option", name=option, exact=True).first
        try:
            opt.click(timeout=3000, force=True)
            self._log(f"下拉已选：{option}")
            self._sleep()
            return True
        except Exception as e:
            self._log(f"选择 {option} 失败：{e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _default_rules_has_values(self) -> bool:
        url = self.page.url or ""
        if re.search(r"cancel-return-rules/\d+", url):
            return True
        if self._switch_is_on("Return rules") or self._switch_is_on("Cancellation rules"):
            return True
        try:
            selected = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('select')).map(
                    s => (s.options[s.selectedIndex] || {}).text || ''
                )"""
            )
            if any(re.search(r"day|minute|fulfilled|shipping", t or "", re.I) for t in (selected or [])):
                return True
        except Exception:
            pass
        return False

    def _save_default_rules_if_needed(self):
        """只有改过值才会出现顶部 Unsaved changes / Save；没有按钮就继续，不卡住。"""
        bar = self.page.get_by_text(re.compile(r"Unsaved changes", re.I)).first
        if not self._visible(bar, timeout=2500):
            self._log("没有 Save 按钮（未改动），继续后续流程")
            return
        save = self.page.get_by_role("button", name=re.compile(r"^Save$", re.I))
        btn = None
        try:
            if save.count():
                btn = save.last
        except Exception:
            btn = save.first
        if btn is None or not self._visible(btn, timeout=1500):
            self._log("看到未保存提示但找不到 Save，继续后续流程")
            return
        try:
            if btn.is_disabled():
                self._log("Save 不可点，继续后续流程")
                return
            btn.click(timeout=4000, force=True)
            self._log("已点 Default rules 的 Save")
            try:
                bar.wait_for(state="hidden", timeout=10000)
            except PlaywrightTimeout:
                pass
        except Exception as e:
            self._log(f"点 Save 失败，继续后续流程：{e}")

    def setup_return_rules(self):
        if not self._open_default_rules():
            raise RuntimeError("打不开 Default rules")
        try:
            self._turn_on_all_rule_switches()
            self._turn_on_switch("Return rules")
            self._turn_on_switch("Cancellation rules")
            self._select_dropdown(["Return window", "14 days", "30 days"], "30 days")
            self._select_dropdown(["Starting from"], "Delivery of item")
            self._set_checkbox("Extend to account for weekends or holidays", True)
            self._select_dropdown(["Return shipping"], "Free return shipping")
            self._set_checkbox("Charge restocking fee", False)
            self._select_dropdown(
                ["Cancellation window", "until fulfilled", "Until item is fulfilled", "Cancel"],
                "15 minutes",
            )
            self._select_radio("Collections")
        except Exception as e:
            self._log(f"填写 Default rules 出错，仍继续：{e}")
        self._save_default_rules_if_needed()
        self._log("退货/取消规则已处理")
        self._sleep()

    def _policy_row_status(self, item_id: str) -> str:
        row = self.page.locator(f"#{item_id}")
        if not self._visible(row, timeout=1500):
            return "missing"
        try:
            text = row.inner_text()
        except Exception:
            return "unknown"
        if re.search(r"\bAutomated\b", text):
            return "automated"
        if "No policy set" in text:
            return "empty"
        if "Review policy" in text:
            return "review"
        return "set"

    def _open_policy(self, name: str) -> bool:
        path = self.POLICY_PATHS.get(name)
        if not path:
            self._log(f"未知政策：{name}")
            return False
        slug = path.rsplit("/", 1)[-1]
        self._goto_admin(path, dismiss=False)
        try:
            self.page.wait_for_url(re.compile(rf"/settings/legal/{re.escape(slug)}"), timeout=15000)
        except PlaywrightTimeout:
            self._log(f"政策 URL 未跳转：{self.page.url}")
            return False
        heading = self.page.get_by_text(name, exact=True).first
        insert = self.page.get_by_role("button", name=re.compile(r"Insert template", re.I)).first
        cancel = self.page.get_by_role("button", name=re.compile(r"^Cancel$", re.I)).first
        publish = self.page.get_by_role("button", name=re.compile(r"^Publish$", re.I)).first
        save = self.page.get_by_role("button", name=re.compile(r"^Save$", re.I)).first
        for loc, ms in ((heading, 8000), (insert, 3000), (cancel, 3000), (publish, 2000), (save, 2000)):
            if self._visible(loc, timeout=ms):
                return True
        self._log(f"政策编辑页未出现：{name} {self.page.url}")
        return False

    def _click_cancel(self) -> bool:
        """政策弹窗有内容时点 Cancel 关闭，不要 Save。"""
        loc = self.page.get_by_role("button", name=re.compile(r"^Cancel$", re.I))
        btn = loc.last if loc.count() else None
        if btn is None or not self._visible(btn, timeout=2000):
            self._log("找不到 Cancel，改关窗口")
            self._close_dialog()
            return False
        try:
            btn.click(timeout=4000)
            self._log("已点 Cancel 关闭政策窗口")
            self._sleep()
            try:
                btn.wait_for(state="hidden", timeout=8000)
            except PlaywrightTimeout:
                pass
            return True
        except Exception as e:
            self._log(f"点击 Cancel 失败：{e}")
            self._close_dialog()
            return False

    def _has_policy_content(self) -> bool:
        text = (self._read_editor_text() or "").strip()
        if len(text) >= 30:
            return True
        insert = self.page.get_by_role("button", name=re.compile(r"Insert template", re.I)).first
        try:
            if insert.is_visible(timeout=600) and insert.is_disabled():
                return True
        except Exception:
            pass
        return False

    def _policy_editor(self):
        for sel in (
            '[contenteditable="true"]',
            "textarea",
            'iframe[title*="policy" i]',
            ".tox-edit-area iframe",
            "iframe.tox-edit-area__iframe",
        ):
            loc = self.page.locator(sel).first
            if self._visible(loc, timeout=800):
                return loc
        return None

    def _read_editor_text(self) -> str:
        loc = self._policy_editor()
        if not loc:
            return ""
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "iframe":
                return loc.content_frame.locator("body").inner_text()
            if tag == "textarea":
                return loc.input_value()
            return loc.inner_text()
        except Exception:
            return ""

    def _set_editor_text(self, text: str) -> bool:
        html = "".join(
            f"<p>{line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</p>"
            for line in (text or "").split("\n")
        )
        try:
            via_tiny = self.page.evaluate(
                """(html) => {
                    const eds = (window.tinymce && tinymce.editors) ? Object.values(tinymce.editors) : [];
                    const ed = (window.tinymce && tinymce.activeEditor) || (eds.length ? eds[0] : null);
                    if (!ed) return false;
                    ed.focus();
                    ed.setContent(html);
                    ed.fire('change');
                    ed.fire('input');
                    if (ed.save) ed.save();
                    return true;
                }""",
                html,
            )
            if via_tiny:
                self._dirty_editor()
                return True
        except Exception:
            pass
        loc = self._policy_editor()
        if not loc:
            return False
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "iframe":
                body = loc.content_frame.locator("body")
                body.click()
                body.evaluate(
                    """(el, t) => {
                        el.innerHTML = t.split('\\n').map(x => '<p>'+x+'</p>').join('');
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                    }""",
                    text,
                )
                self._dirty_editor()
                return True
            if tag == "textarea":
                loc.fill(text)
                return True
            loc.click()
            loc.evaluate(
                "(el, t) => { el.innerText = t; el.dispatchEvent(new Event('input', {bubbles:true})); }",
                text,
            )
            self._dirty_editor()
            return True
        except Exception as e:
            self._log(f"写入政策编辑器失败：{e}")
            return False

    def _dirty_editor(self):
        loc = self._policy_editor()
        if not loc:
            return
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            target = loc.content_frame.locator("body") if tag == "iframe" else loc
            target.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(" ")
            self.page.keyboard.press("Backspace")
        except Exception:
            pass

    def _publish_btn(self):
        return self.page.get_by_role("button", name=re.compile(r"^Publish$", re.I)).first

    def _publish_enabled(self) -> bool:
        loc = self._publish_btn()
        try:
            if not loc.is_visible(timeout=800):
                return False
            return not loc.is_disabled()
        except Exception:
            return False

    def _click_publish(self) -> bool:
        loc = self._publish_btn()
        try:
            loc.wait_for(state="visible", timeout=5000)
            for _ in range(12):
                if not loc.is_disabled():
                    break
                self.page.wait_for_timeout(300)
            loc.click(timeout=5000, force=True)
            self._log("已 Publish 政策")
            self._sleep()
            return True
        except Exception as e:
            self._log(f"Publish 失败：{e}")
            return False

    def _strip_vat_lines(self, raw: str) -> str:
        lines = [
            line
            for line in (raw or "").splitlines()
            if not re.search(r"^\s*(VAT number|Trade number)\s*:", line, re.I)
        ]
        return "\n".join(lines).strip()

    def _extract_green_contact(self, raw: str) -> str:
        """只保留联系信息绿框四行：Trade name / Phone / Email / Physical address。"""
        keep = re.compile(
            r"^(Trade name|Phone number|Email|Physical address)\s*:",
            re.I,
        )
        kept = [line.rstrip() for line in (raw or "").splitlines() if keep.match(line.strip())]
        return "\n".join(kept).strip() or self._contact_block()

    def _contact_still_has_red_lines(self, raw: str) -> bool:
        return bool(re.search(r"^\s*(VAT number|Trade number)\s*:", raw or "", re.I | re.M))

    def _replace_tos_contact_tail(self, raw: str, contact_block: str) -> str:
        """图2：保留 SECTION 25 前文，只替换最后几行占位符为联系信息绿框。"""
        block = (contact_block or "").strip()
        if not raw:
            return block
        if re.search(r"Our contact information is posted below:", raw, re.I):
            return re.sub(
                r"(Our contact information is posted below:\s*)[\s\S]*$",
                r"\1" + block + "\n",
                raw,
                count=1,
                flags=re.I,
            )
        if re.search(r"\[INSERT TRADING NAME\]", raw, re.I):
            return re.sub(
                r"\[INSERT TRADING NAME\][\s\S]*?(?:\[INSERT VAT NUMBER\][^\n]*)?",
                block,
                raw,
                count=1,
                flags=re.I,
            )
        return raw.rstrip() + "\n\n" + block + "\n"

    def _click_save_or_publish(self) -> bool:
        self._dirty_editor()
        for _ in range(20):
            for loc in (
                self.page.get_by_role("button", name=re.compile(r"^Save$", re.I)).first,
                self._publish_btn(),
            ):
                try:
                    if not loc.is_visible(timeout=400):
                        continue
                    if loc.is_disabled():
                        loc.click(timeout=2500, force=True)
                    else:
                        loc.click(timeout=4000, force=True)
                    self._log("已点击 Save / Publish")
                    self._sleep()
                    return True
                except Exception:
                    continue
            self.page.wait_for_timeout(250)
        return False

    def _policy_already_ok(self, kind: str) -> bool:
        raw = (self._read_editor_text() or "").strip()
        shop = (self.bot.shop_name or "").strip()
        mail = (self.bot.email or "").strip()
        if re.search(r"sculpfun", raw, re.I):
            return False
        if kind == "contact":
            return bool(
                shop
                and f"Trade name: {shop}" in raw
                and mail
                and mail in raw
                and not self._contact_still_has_red_lines(raw)
            )
        if kind in ("refund", "privacy", "tos", "shipping"):
            if len(raw) < 80:
                return False
            branded = (shop and shop.lower() in raw.lower()) or (mail and mail.lower() in raw.lower())
            if kind == "tos" and branded:
                return "Trade name:" in raw
            return branded
        if not self._has_policy_content():
            return False
        return True

    def _insert_template(self) -> bool:
        ok = self._click(["Insert template", "插入模板"], timeout=4000)
        self._sleep()
        return ok

    def _ensure_policy(self, name: str, kind: str, fill_fn):
        self._log(f"打开政策窗口：{name}")
        if not self._open_policy(name):
            raise RuntimeError(f"打不开 {name}")
        extra = None
        for _ in range(12):
            if self._policy_editor() is not None or self._has_policy_content():
                break
            self.page.wait_for_timeout(250)
        if self._policy_already_ok(kind):
            self._log(f"{name} 已填好，点 Cancel 关闭")
            if kind == "contact":
                extra = self._extract_green_contact(self._read_editor_text())
            self._click_cancel()
            return extra
        if fill_fn is None:
            self._log(f"{name} 无需填写，点 Cancel")
            self._click_cancel()
            return extra
        extra = fill_fn()
        if extra and kind == "contact":
            self._log(f"已记下联系信息，准备写入服务条款")
        if not self._click_save_or_publish():
            raise RuntimeError(f"{name} 保存/发布失败")
        self._sleep()
        return extra

    def _open_sidekick(self) -> bool:
        box = self.page.get_by_placeholder(re.compile(r"Ask anything", re.I))
        try:
            if box.first.is_visible(timeout=800):
                return True
        except Exception:
            pass
        candidates = [
            self.page.locator('button[aria-label*="Sidekick" i]'),
            self.page.locator('button[aria-label*="assistant" i]'),
            self.page.get_by_role("button", name=re.compile(r"Sidekick", re.I)),
            self.page.locator('header button, [class*="TopBar"] button').last,
        ]
        for loc in candidates:
            try:
                btn = loc.first
                if not btn.is_visible(timeout=600):
                    continue
                btn.click(timeout=3000)
                self._sleep()
                if self.page.get_by_placeholder(re.compile(r"Ask anything", re.I)).first.is_visible(timeout=4000):
                    self._log("已打开右上角 Sidekick")
                    return True
            except Exception:
                continue
        self._log("打不开右上角 Sidekick")
        return False

    def _ask_sidekick(self, prompt: str, wait_ms=40000) -> str:
        if not self._open_sidekick():
            return ""
        box = self.page.get_by_placeholder(re.compile(r"Ask anything", re.I)).first
        box.click()
        box.fill(prompt)
        box.press("Enter")
        self._log("已向 Sidekick 发送请求，等待回复...")
        self.page.wait_for_timeout(wait_ms)
        try:
            panel = self.page.get_by_placeholder(re.compile(r"Ask anything", re.I)).locator(
                "xpath=ancestor::aside|ancestor::*[contains(@class,'Panel') or contains(@class,'conversation')][1]"
            )
            text = panel.inner_text(timeout=3000)
            if text and len(text) > 80:
                return text
        except Exception:
            pass
        try:
            return self.page.locator("body").inner_text()[-5000:]
        except Exception:
            return ""

    def _extract_policy_body(self, raw: str) -> str:
        if not raw:
            return ""
        text = raw.strip()
        for marker in ("Shipping Policy", "Legal Notice", "Legal notice", "Shipping policy"):
            idx = text.lower().rfind(marker.lower())
            if idx >= 0:
                chunk = text[idx:]
                if len(chunk) > 120:
                    return chunk[:6000]
        if len(text) > 120:
            return text[-3500:]
        return ""

    def _default_policy(self, name: str) -> str:
        shop = self.bot.shop_name
        mail = self.bot.email
        if "Shipping" in name:
            return (
                f"Shipping Policy for {shop}\n\n"
                f"Thank you for shopping at {shop}. We ship orders after payment is confirmed. "
                "Delivery times vary by destination and carrier. You will receive tracking information by email when your order ships. "
                f"Questions about shipping can be sent to {mail}."
            )
        if "sale" in name.lower():
            return (
                f"Terms of Sale for {shop}\n\n"
                f"By placing an order with {shop}, you agree to these terms of sale. "
                f"Questions can be sent to {mail}."
            )
        return (
            f"Legal Notice for {shop}\n\n"
            f"{shop} operates this online store. For legal or privacy inquiries, contact {mail}. "
            + (f"Our business address is {self._full_address()}. " if self._full_address() else "")
            + "Please review our other store policies for returns, shipping, and terms of service."
        )

    def setup_written_policies(self):
        contact_text = self._contact_block()
        notes = []

        def fill_from_file(policy_name: str):
            filename = self.POLICY_FILES[policy_name]
            text = self._load_branded_policy(filename)
            if policy_name == "Terms of service":
                text = text.rstrip() + "\n\n" + contact_text
            self._log(f"{policy_name} 使用 信息/{filename}，已替换店名/邮箱")
            if not self._set_editor_text(text):
                raise RuntimeError(f"{policy_name} 写入编辑器失败")
            return None

        def fill_contact():
            self._log(f"联系信息按账号填写：\n{contact_text}")
            if not self._set_editor_text(contact_text):
                raise RuntimeError("联系信息写入编辑器失败")
            return contact_text

        def fill_from_excel(policy_name: str):
            from data_manager import POLICY_COL, pick_policy_text

            excel = self.bot.config.get("settings", "excel_path")
            col = POLICY_COL.get(policy_name, "")
            override = (self.data.get(col) or "").strip() if col else ""
            text = pick_policy_text(
                excel, policy_name, self.bot.shop_name, self.bot.email, override
            ) or self._default_policy(policy_name)
            self._log(f"{policy_name} 使用表格文案，长度 {len(text)}")
            if not self._set_editor_text(text):
                raise RuntimeError(f"{policy_name} 写入编辑器失败")
            return None

        self._goto_admin("settings/legal")
        # 先确认列表已加载，再判断可选入口是否存在，避免把加载失败当成无需填写。
        self.page.locator('a[href$="/settings/legal/refund"]').first.wait_for(
            state="visible", timeout=30000
        )
        sale_available = self.page.locator(
            'a[href$="/settings/legal/terms-of-sale"]'
        ).count() > 0 or self.page.get_by_role(
            "link", name=re.compile(r"^Terms of sale$", re.I)
        ).count() > 0
        try:
            self.page.locator("#written-policies").wait_for(state="visible", timeout=15000)
            for item_id, label in (
                ("refund", "Return and refund"),
                ("privacy", "Privacy"),
                ("terms-of-service", "Terms of service"),
                ("shipping", "Shipping"),
                ("contact-information", "Contact"),
                ("legal-notice", "Legal notice"),
                ("terms-of-sale", "Terms of sale"),
            ):
                self._log(f"列表状态 {label}：{self._policy_row_status(item_id)}")
        except Exception:
            pass

        checks = [
            ("1/7 Return and refund policy", "Return and refund policy", "refund", lambda: fill_from_file("Return and refund policy")),
            ("2/7 Privacy policy", "Privacy policy", "privacy", lambda: fill_from_file("Privacy policy")),
            ("3/7 Contact information", "Contact information", "contact", fill_contact),
            ("4/7 Terms of service", "Terms of service", "tos", lambda: fill_from_file("Terms of service")),
            ("5/7 Shipping policy", "Shipping policy", "shipping", lambda: fill_from_file("Shipping policy")),
            ("6/7 Legal notice", "Legal notice", "generated", lambda: fill_from_excel("Legal notice")),
            ("7/7 Terms of sale", "Terms of sale", "sale", lambda: fill_from_excel("Terms of sale")),
        ]
        for label, name, kind, fn in checks:
            if kind == "sale" and not sale_available:
                if (self.data.get("销售条款") or "").strip():
                    raise RuntimeError("表格已填写销售条款，但当前店铺政策列表没有 Terms of sale 入口")
                note = "当前店铺无 Terms of sale 入口，已跳过该项"
                notes.append(note)
                self._log(note)
                continue
            self._log(label)
            got = self._ensure_policy(name, kind, fn)
            if kind == "contact" and got:
                contact_text = got
        return notes

    def _theme_banner_done(self, editor) -> bool:
        thumbs = editor.locator('[class*="Thumbnail"] img, [class*="thumbnail"] img')
        try:
            if thumbs.count() >= 1:
                return True
        except Exception:
            pass
        return False

    def setup_theme(self, banner_path: str):
        self._goto_admin("themes")
        self._dismiss_modals()
        if not self._click(["Edit theme", "Customize", "编辑主题", "自定义"], timeout=6000):
            raise RuntimeError("找不到 Edit theme")
        self._sleep()
        self.page.wait_for_timeout(4000)

        editor = self.page
        for frame in self.page.frames:
            if "editor" in (frame.url or "").lower() or "theme" in (frame.url or "").lower():
                editor = frame
                break

        for name in ("Hero", "Image banner", "Slideshow", "Banner"):
            try:
                loc = editor.get_by_text(name, exact=False).first
                if loc.is_visible(timeout=800):
                    loc.click(timeout=2000)
                    self._sleep()
                    break
            except Exception:
                continue

        if self._theme_banner_done(editor):
            self._log("主题横幅已有图片，退出编辑器")
            self._exit_theme_editor(editor)
            return

        if banner_path and Path(banner_path).is_file():
            self._upload_banner(editor, banner_path)
        else:
            self._try_theme_ai(editor)

        saved = False
        try:
            editor.get_by_role("button", name=re.compile(r"^Save$", re.I)).click(timeout=5000)
            self._log("主题已保存")
            saved = True
        except Exception:
            saved = self._click(["Save", "保存"])
        if not saved:
            self._log("未点到 Save，可能没有改动")
        self._sleep()
        self._exit_theme_editor(editor)

    def _exit_theme_editor(self, editor):
        for loc in (
            editor.get_by_role("button", name=re.compile(r"Exit|Back", re.I)).first,
            self.page.get_by_role("button", name=re.compile(r"Exit|Back", re.I)).first,
            self.page.locator('[aria-label*="Exit" i], [aria-label*="Back" i]').first,
        ):
            try:
                if loc.is_visible(timeout=800):
                    loc.click(timeout=2000)
                    self._sleep()
                    return
            except Exception:
                continue

    def _upload_banner(self, editor, banner_path: str):
        try:
            inp = editor.locator('input[type="file"]').first
            inp.set_input_files(str(Path(banner_path).resolve()), timeout=8000)
            self._log(f"已上传横幅：{banner_path}")
            return
        except Exception:
            pass
        try:
            with self.page.expect_file_chooser(timeout=8000) as fc:
                editor.get_by_role(
                    "button", name=re.compile(r"Change|Select image|Add image|Upload|Select", re.I)
                ).first.click()
            fc.value.set_files(str(Path(banner_path).resolve()))
            self._log(f"已上传横幅：{banner_path}")
        except Exception as e:
            self._log(f"横幅上传未成功，请在主题编辑器里手动添加：{e}")

    def _try_theme_ai(self, editor):
        prompt = f"Create a clean homepage banner related to our products for {self.bot.shop_name}"
        asked = self._ask_sidekick(
            f"Help me write homepage banner headline and subheading for store {self.bot.shop_name}."
        )
        if asked:
            self._log("已用 Sidekick 生成横幅文案，请在主题编辑器里确认。")
            return
        for name in ("Ask for changes", "Generate"):
            try:
                btn = editor.get_by_text(name, exact=False).first
                if btn.is_visible(timeout=800):
                    btn.click(timeout=2000)
                    box = editor.locator("textarea, [contenteditable='true']").last
                    if box.is_visible(timeout=2000):
                        box.fill(prompt)
                        editor.get_by_role("button", name=re.compile(r"Send|Generate|Apply", re.I)).first.click(
                            timeout=3000
                        )
                        return
            except Exception:
                continue
        self._log("未提供横幅图片，主题编辑到此为止。")
