import configparser
import hashlib
import json
import queue
import re
import secrets
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from adspower_client import AdsPowerClient, AdsPowerAPIError
from cliproxy_client import CliproxyClient, ExtractionRejected, parse_proxy, MIN_EXTRACTION_PORT, MAX_EXTRACTION_PORT
from bot import ShopifyBot
from mailbox_pool import parse_mailboxes, mailbox_usage
from profile_source import COUNTRIES, fetch_profile
from shopify_admin import admin_store_base
from web_storage import Store, now


ROOT = Path(__file__).resolve().parent
ACTIVE = {"queued", "preparing", "running", "waiting", "stopping", "paused"}
PROFILE_FIELDS = ("name", "first_name", "last_name", "address", "city", "province", "postal_code", "phone", "region")


class Cancelled(BaseException):
    pass


def proxy_port_seed(value):
    """Migrate the original, unsupported 20000-based counter into the API range."""
    try:
        port = int(value)
    except (ValueError, TypeError):
        port = MIN_EXTRACTION_PORT
    return port if MIN_EXTRACTION_PORT <= port <= MAX_EXTRACTION_PORT else MIN_EXTRACTION_PORT


def local_api_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("AdsPower 地址必须是本机 HTTP 地址，例如 http://127.0.0.1:50325")
    try:
        if not parsed.port:
            raise ValueError()
    except ValueError:
        raise ValueError("请填写有效的 AdsPower 端口")
    return value.rstrip("/")


def public_job(job):
    payload = job["payload"]
    result = job["result"]
    url = result.get("admin_url") or payload.get("admin_url", "")
    if not url.startswith("https://admin.shopify.com/store/"):
        url = ""
    return {
        "id": job["id"], "state": job["state"], "stage": job["stage"],
        "created": job["created"], "updated": job["updated"],
        "shop_name": payload["shop_name"], "email": payload["email"],
        "country": payload["country"], "profile_id": payload.get("profile_id", ""),
        "profile": payload.get("profile"), "prompt": job["prompt"],
        "prompt_kind": payload.get("prompt_kind", "continue"),
        "error": result.get("error", ""), "setup_notes": result.get("setup_notes", ""),
        "auto_skipped": bool(result.get("auto_skipped")),
        "auto_retry_count": result.get("auto_retry_count", 0),
        "admin_url": url, "has_password": bool(payload.get("shopify_password")),
        "creation_uncertain": payload.get("creation_uncertain", False),
        "batch_id": payload.get("batch_id", ""), "batch_index": payload.get("batch_index", 0),
        "proxy_source": payload.get("proxy_source", "saved"),
        "proxy_address": "{}:{}".format(payload["proxy_lease"]["config"]["proxy_host"], payload["proxy_lease"]["config"]["proxy_port"]) if payload.get("proxy_lease") else "",
        "proxy_fetched_at": (payload.get("proxy_lease") or {}).get("fetched_at", ""),
        "extraction_uncertain": payload.get("extraction_uncertain", False),
    }


class Tasks:
    def __init__(self, db_path=None, start_worker=True):
        self.store = Store(db_path or ROOT / "local_data" / "shopflow.sqlite3")
        self.queue = queue.Queue()
        self.controls = {}
        self.lock = threading.RLock()
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self._worker, name="shopflow-worker", daemon=True)
            self.worker.start()

    def config(self):
        config = configparser.ConfigParser()
        config.read(str(ROOT / "config.ini"), encoding="utf-8")
        for section in ("settings", "adspower"):
            if not config.has_section(section):
                config.add_section(section)
        config.set("settings", "excel_path", str(ROOT / config.get("settings", "excel_path", fallback="stores.xlsx")))
        config.set("settings", "manual_verify_fallback", "true")
        config.set("settings", "close_browser_after", "false")
        saved = self.store.get_settings()
        config.set("adspower", "api_base", local_api_url(saved.get("api_base") or config.get("adspower", "api_base", fallback="http://127.0.0.1:50325")))
        if "api_key" in saved:
            config.set("adspower", "api_key", saved["api_key"])
        return config

    def settings(self):
        config = self.config()
        saved = self.store.get_settings()
        return {
            "api_base": config.get("adspower", "api_base"),
            "has_api_key": bool(config.get("adspower", "api_key", fallback="")),
            "default_country": saved.get("default_country", "es"),
            "proxy_id": saved.get("proxy_id", ""), "group_id": saved.get("group_id", "0"),
            "proxy_source": saved.get("proxy_source", "cliproxy" if saved.get("cliproxy_key") else "saved"),
            "has_cliproxy_key": bool(saved.get("cliproxy_key")),
            "cliproxy_next_port": proxy_port_seed(saved.get("cliproxy_next_port")),
        }

    def save_settings(self, data):
        with self.lock:
            return self._save_settings(data)

    def _save_settings(self, data):
        old = self.store.get_settings()
        old["cliproxy_next_port"] = proxy_port_seed(old.get("cliproxy_next_port"))
        old["api_base"] = local_api_url(str(data.get("api_base", self.settings()["api_base"])))
        if data.get("api_key"):
            old["api_key"] = str(data["api_key"])[:500]
        if data.get("clear_api_key"):
            old["api_key"] = ""
        if data.get("cliproxy_key"):
            key = str(data["cliproxy_key"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,500}", key):
                raise ValueError("Cliproxy API Key 格式无效，请从短效 IP 页面的 API 栏复制")
            old["cliproxy_key"] = key
        if data.get("clear_cliproxy_key"):
            old["cliproxy_key"] = ""
        if "proxy_source" in data:
            if data["proxy_source"] not in ("saved", "cliproxy"):
                raise ValueError("无效的代理来源")
            old["proxy_source"] = data["proxy_source"]
        if "cliproxy_next_port" in data and int(data["cliproxy_next_port"]) != self.settings()["cliproxy_next_port"]:
            if any(j["state"] in ACTIVE for j in self.store.jobs(limit=None)):
                raise ValueError("有任务进行中，暂时不能修改提取起始端口")
            port = int(data["cliproxy_next_port"])
            if not MIN_EXTRACTION_PORT <= port <= MAX_EXTRACTION_PORT:
                raise ValueError("起始端口应在 443–3000 之间")
            old["cliproxy_next_port"] = port
        for key in ("proxy_id", "group_id"):
            if key in data:
                old[key] = str(data[key])[:100]
        if data.get("default_country") in COUNTRIES:
            old["default_country"] = data["default_country"]
        self.store.save_settings(old)
        return self.settings()

    def ads(self):
        config = self.config()
        return AdsPowerClient(config.get("adspower", "api_base"), config.get("adspower", "api_key", fallback=""))

    def connection(self):
        ads = self.ads()
        ads.check_ready()
        profiles = []
        for page in range(1, 101):
            batch = ads.list_profiles(page=page)
            profiles.extend({"id": p["profile_id"], "name": p.get("name") or p["profile_id"]} for p in batch)
            if len(batch) < 100:
                break
        proxies, warning = [], ""
        try:
            for page in range(1, 101):
                batch = ads.list_proxies(page=page).get("list", [])
                proxies.extend({"id": str(p["proxy_id"]), "label": p.get("remark") or
                                "{}://{}:{}".format(p.get("type", ""), p.get("host", ""), p.get("port", ""))} for p in batch)
                if len(batch) < 200:
                    break
        except Exception:
            warning = "AdsPower 已连接，但代理列表读取失败；可使用本机网络或已有环境。"
        return {"connected": True, "profiles": profiles, "proxies": proxies, "warning": warning}

    def mailbox_inventory(self):
        with self.lock:
            jobs = self.store.jobs(limit=None)
            accounts = [{"id": m["id"], "email": m["email"], "seq": m["seq"], "created": m["created"],
                         **mailbox_usage(m["email"], jobs, m)} for m in self.store.mailboxes()]
            return {"mailboxes": accounts, "counts": {state: sum(m["state"] == state for m in accounts)
                    for state in ("unused", "reserved", "in_use", "used")}}

    def import_mailboxes(self, text):
        records, summary = parse_mailboxes(text)
        with self.lock:
            return {**summary, **self.store.import_mailboxes(records), **self.mailbox_inventory()}

    def set_mailbox_state(self, mailbox_id, state):
        if state not in ("used", "unused"):
            raise ValueError("邮箱状态只能改为已使用或未使用")
        with self.lock:
            mailbox = self.store.mailbox(mailbox_id=mailbox_id)
            jobs = self.store.jobs(limit=None)
            usage = mailbox_usage(mailbox["email"], jobs, mailbox)
            ending = any(j["id"] in self.controls and j["payload"]["email"].casefold() == mailbox["email"].casefold() for j in jobs)
            if usage["state"] in ("reserved", "in_use") or ending:
                raise ValueError("邮箱正被任务占用，请先结束或取消关联任务后再修改")
            if usage["state"] != state:
                self.store.set_mailbox_state(mailbox_id, state)
            return self.mailbox_inventory()

    def prepare_payload(self, data, jobs=None):
        country = data.get("country", "es")
        if country not in COUNTRIES:
            raise ValueError("请选择支持的国家")
        email = str(data.get("email", "")).strip()
        mailbox = self.store.mailbox(mailbox_id=str(data.get("mailbox_id", "")), email=email)
        if mailbox:
            email = mailbox["email"]
            if mailbox_usage(email, jobs if jobs is not None else self.store.jobs(limit=None), mailbox)["state"] != "unused":
                raise ValueError("该邮箱已预留或已使用，请查看关联任务；重试请使用原任务")
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("请填写可收信的邮箱")
        shop = str(data.get("shop_name", "")).strip() or "store-{}-{}".format(country, secrets.token_hex(3))
        if len(shop) > 80 or any(ord(c) < 32 for c in shop):
            raise ValueError("店铺名应为 1–80 个可见字符")
        settings = self.settings()
        profile = None
        if data.get("profile"):
            incoming = data["profile"]
            if not isinstance(incoming, dict) or incoming.get("country") != country:
                raise ValueError("资料国家与任务国家不一致，请重新获取资料")
            profile = {k: str(incoming.get(k, "")).strip()[:300] for k in PROFILE_FIELDS}
            if not all(profile[k] for k in ("name", "address", "city", "postal_code", "phone")):
                raise ValueError("姓名、地址、城市、邮编和电话不能为空")
            profile.update(country=country, country_name=COUNTRIES[country]["name"],
                           synthetic=bool(incoming.get("synthetic", True)),
                           source_url="https://toolqd.com/fakename-" + country)
        password = str(data.get("shopify_password") or ("Shp!" + secrets.token_urlsafe(15)))
        if not 8 <= len(password) <= 200:
            raise ValueError("Shopify 密码至少需要 8 位")
        payload = {
            "country": country, "email": email, "shop_name": shop, "profile": profile,
            "profile_id": str(data.get("profile_id", ""))[:100],
            "proxy_id": str(data.get("proxy_id", settings["proxy_id"]))[:100],
            "group_id": settings["group_id"], "shopify_password": password,
            "email_password": mailbox["password"] if mailbox else str(data.get("email_password", ""))[:500],
            "mailbox_id": mailbox["id"] if mailbox else "", "execution_started": "",
            "mailbox_usage_revision": mailbox.get("usage_revision", 0) if mailbox else 0,
            "imap_server": str(data.get("imap_server", "")).strip()[:250],
            "product_csv": str(data.get("product_csv", "")).strip()[:500],
        }
        source = data.get("proxy_source", settings["proxy_source"])
        if source not in ("saved", "cliproxy"):
            raise ValueError("无效的代理来源")
        # Selecting an existing environment preserves its configured proxy.
        if payload["profile_id"]:
            source = "saved"
        if source == "cliproxy" and not settings["has_cliproxy_key"]:
            raise ValueError("请先在连接设置保存 Cliproxy 短效 IP 的 API Key")
        payload["proxy_source"] = source
        return payload

    def check_email_available(self, email, jobs):
        mailbox = self.store.mailbox(email=email)
        for job in jobs:
            if job["payload"]["email"].casefold() == email.casefold() and (
                job["state"] in ACTIVE or (job["state"] == "success" and not mailbox)
            ):
                raise ValueError("此邮箱已有进行中或完成的任务，请在任务列表查看")

    def submit(self, data):
        job_id = str(uuid.UUID(str(data.get("request_id") or uuid.uuid4())))
        with self.lock:
            try:
                existing = self.store.get(job_id)
                same = (existing["payload"].get("mailbox_id") == data["mailbox_id"] if data.get("mailbox_id")
                        else existing["payload"]["email"] == str(data.get("email", "")).strip())
                if not same:
                    raise ValueError("该请求编号已被使用")
                return public_job(existing)
            except KeyError:
                pass
            payload = self.prepare_payload(data)
            self.check_email_available(payload["email"], self.store.jobs(limit=None))
            self.store.add(job_id, payload)
            self._enqueue(job_id)
            return public_job(self.store.get(job_id))

    def submit_batch(self, data):
        batch_id = str(uuid.UUID(str(data.get("request_id") or uuid.uuid4())))
        use_library = data.get("email_source") == "library"
        raw = str(data.get("emails", "")).strip()
        lines = [(i, line.strip()) for i, line in enumerate(raw.splitlines(), 1) if line.strip()]
        if use_library:
            value = str(data.get("mailbox_count", ""))
            if not value.isdigit() or not 1 <= int(value) <= 100:
                raise ValueError("每批请选择 1–100 个邮箱")
            requested_count = int(value)
        elif not 1 <= len(lines) <= 100:
            raise ValueError("每批请输入 1–100 行邮箱")
        if data.get("profile_id") or data.get("profile"):
            raise ValueError("批量任务会分别获取资料并创建环境，不能共用同一环境或联系人资料")
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in data.items() if k != "request_id"},
                                                sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        with self.lock:
            try:
                existing = self.store.batch(batch_id)
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("该批次编号已用于其他内容，请重新提交")
                return {"batch_id": batch_id, "jobs": [public_job(self.store.get(j)) for j in existing["job_ids"]]}
            except KeyError:
                pass
            entries, seen = [], set()
            jobs = self.store.jobs(limit=None)
            pool = []
            if use_library:
                available = [m for m in self.mailbox_inventory()["mailboxes"] if m["state"] == "unused"]
                if len(available) < requested_count:
                    raise ValueError("未使用邮箱不足：需要 {} 个，当前可用 {} 个".format(requested_count, len(available)))
                pool = available[:requested_count]
                lines = [(i, m["email"]) for i, m in enumerate(pool, 1)]
            for index, (line_number, line) in enumerate(lines, 1):
                parts = [v.strip() for v in (line.split("----") if "----" in line else line.split("\t"))]
                if len(parts) > 3:
                    raise ValueError("第 {} 行格式错误：邮箱----邮箱应用密码----店铺名".format(line_number))
                incoming = {**data, "email": parts[0], "email_password": parts[1] if len(parts) > 1 else "",
                            "shop_name": parts[2] if len(parts) > 2 else "", "profile": None, "profile_id": "",
                            "mailbox_id": pool[index - 1]["id"] if use_library else ""}
                try:
                    payload = self.prepare_payload(incoming, jobs=jobs)
                    self.check_email_available(payload["email"], jobs)
                    if payload["email"].casefold() in seen:
                        raise ValueError("邮箱在本批次重复")
                except ValueError as exc:
                    raise ValueError("第 {} 行：{}".format(line_number, exc)) from None
                seen.add(payload["email"].casefold())
                payload.update(batch_id=batch_id, batch_index=index)
                entries.append((str(uuid.uuid5(uuid.UUID(batch_id), str(index))), payload))
            self.store.add_batch(batch_id, fingerprint, entries)
            for job_id, _ in entries:
                self._enqueue(job_id)
            return {"batch_id": batch_id, "jobs": [public_job(self.store.get(j)) for j, _ in entries]}

    def batch_action(self, batch_id, action):
        if action not in ("cancel", "retry"):
            raise ValueError("无效的批次操作")
        changed, skipped = 0, 0
        with self.lock:
            for job_id in self.store.batch(batch_id)["job_ids"]:
                state = self.store.get(job_id)["state"]
                eligible = state in ACTIVE if action == "cancel" else state in ("failed", "cancelled", "interrupted")
                if not eligible:
                    continue
                try:
                    getattr(self, action)(job_id)
                    changed += 1
                except ValueError:
                    skipped += 1
        return {"changed": changed, "skipped": skipped}

    def _enqueue(self, job_id):
        blocker = self.store.queue_blocker()
        if blocker and blocker != job_id:
            self.store.update(job_id, state="paused", stage="队列暂停，尚未提取下一条 IP")
            return
        self.controls[job_id] = {"cancel": threading.Event(), "resume": threading.Event(), "answer": ""}
        self.queue.put(job_id)

    def queue_status(self):
        with self.lock:
            blocker = self.store.queue_blocker()
            if not blocker:
                return {"paused": False}
            job = self.store.get(blocker)
            return {"paused": True, "blocked_job_id": blocker, "shop_name": job["payload"]["shop_name"],
                    "state": job["state"], "can_continue": job["state"] not in ACTIVE and blocker not in self.controls}

    def _pause_queue(self, job_id):
        """Called by the sole worker under self.lock, before it can take another job."""
        self.store.set_queue_blocker(job_id)
        # Remove pending work before allowing retries to be enqueued. This avoids a
        # blocked FIFO entry sitting ahead of the current job's retry.
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
            else:
                self.queue.task_done()
        for job in self.store.jobs(limit=None):
            if job["id"] == job_id:
                continue
            if job["state"] == "queued":
                self.store.update(job["id"], state="paused", stage="队列暂停，尚未提取下一条 IP")
                self.controls.pop(job["id"], None)
            elif job["state"] == "stopping":
                self.store.update(job["id"], state="cancelled", stage="已停止", prompt="")
                self.controls.pop(job["id"], None)

    def _release_queue(self):
        self.store.set_queue_blocker("")
        # Restore submission order, including the row order within a batch.
        for job in reversed(self.store.jobs(limit=None)):
            if job["state"] == "paused":
                self.store.update(job["id"], state="queued", stage="等待上一条完成后再取 IP")
                self._enqueue(job["id"])

    def continue_queue(self, blocked_job_id):
        with self.lock:
            status = self.queue_status()
            if not status["paused"]:
                raise ValueError("队列没有暂停")
            if status["blocked_job_id"] != blocked_job_id:
                raise ValueError("队列等待的任务已变化，请刷新后再操作")
            if not status["can_continue"]:
                raise ValueError("当前任务仍在处理，请等待它结束")
            self.log(status["blocked_job_id"], "已手动跳过此任务，允许队列继续逐条处理")
            self._release_queue()
            return self.queue_status()

    def retry(self, job_id):
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] not in ("failed", "cancelled", "interrupted"):
                raise ValueError("仅失败、停止或中断的任务可以重试")
            if job_id in self.controls:
                raise ValueError("上一次任务正在结束，请稍后重试")
            for other in self.store.jobs(limit=None):
                if other["id"] != job_id and other["state"] in ACTIVE and other["payload"]["email"].casefold() == job["payload"]["email"].casefold():
                    raise ValueError("此邮箱还有其他进行中的任务")
            mailbox = self.store.mailbox(email=job["payload"]["email"])
            if mailbox:
                others = [j for j in self.store.jobs(limit=None) if j["id"] != job_id]
                # A manual used mark controls new allocation, not retrying the
                # original task. Newer task usage still blocks an old retry.
                if mailbox_usage(job["payload"]["email"], others, {**mailbox, "usage_state": "unused"})["state"] != "unused":
                    raise ValueError("该邮箱已被其他任务预留或使用，不能重复分配")
                job["payload"]["mailbox_usage_revision"] = mailbox.get("usage_revision", 0)
            self.store.update(job_id, state="queued", stage="等待重试", prompt="", result={}, payload=job["payload"])
            self._enqueue(job_id)
        return public_job(self.store.get(job_id))

    def cancel(self, job_id):
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] in ("paused", "interrupted"):
                self.store.update(job_id, state="cancelled", stage="已停止", prompt="")
                return
            if job["state"] not in ACTIVE:
                raise ValueError("任务已结束")
            control = self.controls.get(job_id)
            if control:
                control["cancel"].set()
                control["resume"].set()
            self.store.update(job_id, state="stopping", stage="正在停止", prompt="")

    def resume(self, job_id, answer):
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] != "waiting" or job_id not in self.controls:
                raise ValueError("当前任务没有等待输入")
            answer = str(answer).strip()
            if job["payload"].get("prompt_kind") == "otp" and answer and not re.fullmatch(r"\d{6}", answer):
                raise ValueError("验证码应为 6 位数字")
            self.controls[job_id]["answer"] = answer
            self.controls[job_id]["resume"].set()

    def check_cancel(self, job_id):
        if self.controls[job_id]["cancel"].is_set():
            raise Cancelled()

    def redact(self, job_id, message):
        payload = self.store.get(job_id)["payload"]
        message = str(message)
        proxy = (payload.get("proxy_lease") or {}).get("config", {})
        for secret in (payload.get("shopify_password"), payload.get("email_password"),
                       self.config().get("adspower", "api_key", fallback=""),
                       self.store.get_settings().get("cliproxy_key"), proxy.get("proxy_user"), proxy.get("proxy_password")):
            if secret:
                message = message.replace(secret, "[已隐藏]")
        message = re.sub(r"wss?://[^\s]+", "[浏览器已连接]", message)
        message = re.sub(r"(验证码[：:]\s*)\d{6}", r"\1[已隐藏]", message)
        message = re.sub(r"([?&]key=)[^&\s]+", r"\1[已隐藏]", message)
        return message

    def log(self, job_id, message):
        self.store.log(job_id, self.redact(job_id, message))

    def ask(self, job_id, message, kind):
        control = self.controls[job_id]
        control["answer"] = ""
        control["resume"].clear()
        payload = self.store.get(job_id)["payload"]
        payload["prompt_kind"] = kind
        self.store.update(job_id, state="waiting", stage="等待邮箱验证码" if kind == "otp" else "等待你在浏览器完成", prompt=message, payload=payload)
        self.log(job_id, message)
        while not control["resume"].wait(0.5):
            self.check_cancel(job_id)
        self.check_cancel(job_id)
        self.store.update(job_id, state="running", stage="继续处理", prompt="")
        return control["answer"]

    def _worker(self):
        while True:
            job_id = self.queue.get()
            try:
                if job_id is None:
                    return
                self.execute(job_id)
            finally:
                self.queue.task_done()

    def _available_proxy_port(self, saved):
        occupied = set()
        for job in self.store.jobs(limit=None):
            payload = job["payload"]
            lease = payload.get("proxy_lease") or {}
            if lease.get("requested_port") is not None:
                occupied.add(lease["requested_port"])
            if payload.get("extraction_uncertain") and payload.get("extraction_port") is not None:
                occupied.add(payload["extraction_port"])
        start = proxy_port_seed(saved.get("cliproxy_next_port"))
        count = MAX_EXTRACTION_PORT - MIN_EXTRACTION_PORT + 1
        for offset in range(count):
            port = MIN_EXTRACTION_PORT + (start - MIN_EXTRACTION_PORT + offset) % count
            if port not in occupied:
                return port
        raise ValueError("Cliproxy 提取端口已全部占用（443–3000），本次未提取 IP")

    def prepare_proxy(self, job_id, payload):
        if payload.get("proxy_source") != "cliproxy" or payload.get("proxy_lease"):
            return
        if payload.get("extraction_uncertain"):
            raise RuntimeError("上次提取 IP 的结果未确认。请到 Cliproxy 查看，复制该条代理后在任务详情补录；不会自动重复提取。")
        with self.lock:
            saved = self.store.get_settings()
            client = CliproxyClient(saved.get("cliproxy_key", ""))
            port = self._available_proxy_port(saved)
            saved["cliproxy_next_port"] = port + 1 if port < MAX_EXTRACTION_PORT else MIN_EXTRACTION_PORT
            self.store.save_settings(saved)
            payload["extraction_port"] = port
            payload["extraction_uncertain"] = True
            self.store.update(job_id, payload=payload, stage="提取所选国家的短效 IP")
        self.check_cancel(job_id)
        self.log(job_id, "正在从 Cliproxy 提取 {} 的 1 条短效 IP...".format(COUNTRIES[payload["country"]]["name"]))
        try:
            payload["proxy_lease"] = client.extract(payload["country"], port)
        except ExtractionRejected:
            payload["extraction_uncertain"] = False
            self.store.update(job_id, payload=payload)
            raise
        payload["extraction_uncertain"] = False
        payload["proxy_bound"] = False
        self.store.update(job_id, payload=payload)
        self.log(job_id, "短效 IP 已提取并加密保存，接下来写入 AdsPower")

    def execute(self, job_id):
        # Keep both attempts in the same worker slot, before the next task can
        # generate contacts, extract a proxy, or create a browser environment.
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] not in ("queued", "stopping") or job_id not in self.controls:
                return
        started = False
        try:
            for attempt in range(2):
                self.check_cancel(job_id)
                started = True
                try:
                    result = dict(self._execute_attempt(job_id))
                except Exception as exc:
                    result = {"status": "failed", "error": "处理失败：" + str(exc)}
                self.check_cancel(job_id)
                # Passwords belong only in the encrypted payload.
                result.pop("password", None)
                for key in ("error", "setup_notes"):
                    if key in result:
                        result[key] = self.redact(job_id, result[key])
                result["auto_retry_count"] = attempt
                if result.get("status") == "success":
                    with self.lock:
                        self.check_cancel(job_id)
                        self.store.update(job_id, state="success", stage="全部完成", result=result, prompt="")
                    break
                self.log(job_id, "本次任务失败：" + (result.get("error") or result.get("setup_notes") or "未返回成功结果"))
                if attempt == 0:
                    with self.lock:
                        self.check_cancel(job_id)
                        self.store.update(job_id, state="preparing", stage="自动重试（1/1）", result=result, prompt="")
                    self.log(job_id, "自动重试（1/1）：沿用已保存的 IP、环境和任务进度，重试完成后再处理下一条")
                    continue
                result["auto_skipped"] = True
                with self.lock:
                    self.check_cancel(job_id)
                    self.store.update(job_id, state="failed", stage="重试 1 次仍失败，已自动跳过", result=result, prompt="")
                    self.log(job_id, "自动重试 1 次后仍失败，已标记并保留任务进度；继续下一条任务")
        except Cancelled:
            self.store.update(job_id, state="cancelled", stage="已停止", prompt="", result={})
            self.log(job_id, "任务已停止，已有环境和资料已保留")
        finally:
            with self.lock:
                self.controls.pop(job_id, None)
                finished = self.store.get(job_id)
                state = finished["state"]
                if state == "success" or (state == "failed" and finished["result"].get("auto_skipped")):
                    if self.store.queue_blocker() == job_id:
                        self._release_queue()
                elif started and state == "cancelled":
                    self._pause_queue(job_id)

    def _execute_attempt(self, job_id):
        # Reload checkpoints on every attempt; uncertain external operations
        # retain their existing guards against duplicate extraction/creation.
        payload = self.store.get(job_id)["payload"]
        if not payload.get("execution_started"):
            payload["execution_started"] = now()
            self.store.update(job_id, payload=payload)
        self.store.update(job_id, state="preparing", stage="准备联系人资料")
        if payload.get("profile") is None:
            self.log(job_id, "正在从起点工具获取所选国家的虚拟联系人资料...")
            payload["profile"] = fetch_profile(payload["country"])
            self.store.update(job_id, payload=payload)
        self.check_cancel(job_id)
        ads = self.ads()
        ads.check_ready()
        self.check_cancel(job_id)
        self.prepare_proxy(job_id, payload)
        self.check_cancel(job_id)
        if not payload.get("profile_id"):
            self.store.update(job_id, stage="创建 AdsPower 环境")
            marker = "shopflow:" + job_id
            if payload.get("creation_uncertain"):
                recovered = ads.find_task_profile(marker)
                if not recovered:
                    raise RuntimeError("上次创建环境的结果未确认。请先在 AdsPower 检查，找到后在任务详情绑定环境；不会重复创建。")
                payload["profile_id"] = recovered
            else:
                # 先持久化创建意图；即使连接断开，也不会在重试时重复创建。
                payload["creation_uncertain"] = True
                self.store.update(job_id, payload=payload)
                try:
                    proxy_options = {"proxy_config": payload["proxy_lease"]["config"]} if payload.get("proxy_lease") else {}
                    payload["profile_id"] = ads.create_profile(payload["shop_name"], payload["email"], marker,
                                                              payload["proxy_id"], payload["group_id"], **proxy_options)
                    payload["proxy_bound"] = bool(payload.get("proxy_lease"))
                except AdsPowerAPIError:
                    payload["creation_uncertain"] = False
                    self.store.update(job_id, payload=payload)
                    raise
            payload["creation_uncertain"] = False
            self.store.update(job_id, payload=payload)
            self.log(job_id, "环境已创建并保存 ID：" + payload["profile_id"])
        else:
            self.log(job_id, "沿用 AdsPower 环境：" + payload["profile_id"])
        if payload.get("proxy_lease") and not payload.get("proxy_bound"):
            self.store.update(job_id, stage="更新环境代理")
            ads.update_proxy(payload["profile_id"], payload["proxy_lease"]["config"])
            payload["proxy_bound"] = True
            self.store.update(job_id, payload=payload)
        self.check_cancel(job_id)
        profile = payload["profile"]
        data = {
            "AdsPower环境ID": payload["profile_id"], "店铺名": payload["shop_name"],
            "邮箱": payload["email"], "邮箱密码": payload["email_password"],
            "Shopify密码": payload["shopify_password"], "IMAP服务器": payload["imap_server"],
            "联系人姓名": profile["name"], "国家": COUNTRIES[payload["country"]]["name"],
            "联系人名": profile.get("first_name", ""), "联系人姓": profile.get("last_name", ""),
            "地址": profile["address"], "城市": profile["city"], "州/省": profile.get("province", ""),
            "邮编": profile["postal_code"], "电话": profile["phone"],
            "产品CSV": payload["product_csv"], "店铺后台": payload.get("admin_url", ""),
            "_setup_completed": list(payload.get("setup_completed", [])),
        }
        original_start = ads.start

        def on_wait(message):
            self.check_cancel(job_id)
            self.store.update(job_id, stage="等待浏览器内核下载")
            self.log(job_id, message)

        ads.start = lambda profile_id: original_start(profile_id, on_wait=on_wait)
        self.store.update(job_id, state="running", stage="启动浏览器并处理店铺")
        config = self.config()
        # Tasks.execute owns the one task retry; do not multiply browser retries.
        config.set("settings", "browser_recovery_attempts", "0")
        if payload.get("batch_id"):
            config.set("settings", "close_browser_after", "true")
        bot = WebBot(config, data, ads, self, job_id)
        return bot.register()

    def bind_profile(self, job_id, profile_id):
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] in ACTIVE:
                raise ValueError("请先停止任务")
            profile_id = str(profile_id).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", profile_id):
                raise ValueError("请选择有效环境")
            job["payload"]["profile_id"] = profile_id
            job["payload"]["creation_uncertain"] = False
            self.store.update(job_id, payload=job["payload"])

    def change_proxy(self, job_id, value=None):
        with self.lock:
            job = self.store.get(job_id)
            if job["state"] not in ("failed", "cancelled", "interrupted") or job_id in self.controls:
                raise ValueError("请等待任务停止后再更换代理")
            payload = job["payload"]
            if payload.get("proxy_source") != "cliproxy":
                raise ValueError("此任务未使用 Cliproxy 自动提取")
            if payload.get("creation_uncertain"):
                raise ValueError("请先确认并绑定 AdsPower 环境，再更换代理")
            if value is not None:
                payload["proxy_lease"] = {"config": parse_proxy(value), "country": payload["country"].upper(), "fetched_at": now()}
            else:
                payload.pop("proxy_lease", None)
            payload["extraction_uncertain"] = False
            payload["proxy_bound"] = False
            self.store.update(job_id, payload=payload)
            self.log(job_id, "已补录代理，下次重试会使用此代理" if value is not None else "已选择重新提取 IP，下次重试会消耗 1 条 IP 额度")
        return self.retry(job_id)


class WebBot(ShopifyBot):
    def __init__(self, config, data, ads, tasks, job_id):
        super().__init__(config, data, ads)
        self.tasks, self.job_id = tasks, job_id

    def _log(self, message):
        self.tasks.check_cancel(self.job_id)
        self.tasks.log(self.job_id, message)

    def _check_cancel(self):
        self.tasks.check_cancel(self.job_id)

    def _remember_admin(self):
        base = super()._remember_admin()
        if base:
            self.tasks.check_cancel(self.job_id)
            payload = self.tasks.store.get(self.job_id)["payload"]
            payload["admin_url"] = base
            self.tasks.store.update(self.job_id, payload=payload)
        return base

    def _sleep(self, short=False):
        self.tasks.check_cancel(self.job_id)
        duration = 0.6 if short else self.config.getfloat("settings", "min_wait", fallback=1.5)
        if self.tasks.controls[self.job_id]["cancel"].wait(duration):
            raise Cancelled()

    def _ask_user(self, message, kind="continue"):
        return self.tasks.ask(self.job_id, message, kind)

    def _checkpoint_setup(self, step):
        super()._checkpoint_setup(step)
        self.tasks.check_cancel(self.job_id)
        payload = self.tasks.store.get(self.job_id)["payload"]
        payload["setup_completed"] = list(self.data["_setup_completed"])
        self.tasks.store.update(self.job_id, payload=payload)

    def _ensure_admin(self):
        super()._ensure_admin()
        payload = self.tasks.store.get(self.job_id)["payload"]
        if not admin_store_base(self.admin_url):
            raise RuntimeError("未确认真实店铺后台地址，已暂停后续设置")
        payload["admin_url"] = self.admin_url
        self.tasks.store.update(self.job_id, payload=payload, stage="完善店铺资料与政策")
