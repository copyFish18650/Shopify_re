import time
import requests


class AdsPowerAPIError(RuntimeError):
    """AdsPower explicitly rejected an operation; it did not create a profile."""


class AdsPowerClient:
    """连接本机 AdsPower 本地 API，启动/关闭环境窗口。"""

    def __init__(self, api_base: str, api_key: str = ""):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        # Local API traffic must never inherit Windows or environment proxies.
        self.session.trust_env = False
        self.session.headers["Content-Type"] = "application/json"
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def _get(self, path, **kwargs):
        return self.session.get(self.api_base + path, timeout=60, **kwargs)

    def _post(self, path, **kwargs):
        return self.session.post(self.api_base + path, timeout=60, **kwargs)

    def check_ready(self):
        try:
            resp = self._get("/status")
            data = resp.json()
        except Exception as e:
            raise RuntimeError(
                f"无法连接 AdsPower 本地 API（{self.api_base}）。"
                f"请先打开 AdsPower 客户端，并在「设置 -> 本地API」确认地址。原始错误：{e}"
            ) from e
        if data.get("code") not in (0, None) and str(data.get("msg", "")).lower() not in ("success", ""):
            if data.get("code") != 0:
                raise RuntimeError(f"AdsPower API 状态异常：{data}")

    def _api_data(self, path, payload):
        response = self._post(path, json=payload)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise AdsPowerAPIError(str(result.get("msg") or "AdsPower API 返回失败"))
        return result.get("data") or {}

    def list_profiles(self, page=1, limit=100):
        return self._api_data("/api/v2/browser-profile/list", {"page": page, "limit": limit}).get("list", [])

    def list_proxies(self, page=1):
        return self._api_data("/api/v2/proxy-list/list", {"page": page, "limit": 200})

    def list_groups(self):
        response = self._get("/api/v1/group/list", params={"page": 1, "page_size": 2000})
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise AdsPowerAPIError(str(result.get("msg") or "读取 AdsPower 分组失败"))
        return (result.get("data") or {}).get("list", [])

    def find_task_profile(self, marker):
        for page in range(1, 101):
            profiles = self.list_profiles(page=page)
            for profile in profiles:
                if profile.get("remark") == marker:
                    return profile.get("profile_id")
            if len(profiles) < 100:
                break
        return None

    def create_profile(self, name, email, marker, proxy_id="", group_id="0", proxy_config=None):
        payload = {
            "name": name[:100], "group_id": group_id or "0", "remark": marker,
            "platform": "shopify.com", "username": email,
            "fingerprint_config": {
                "automatic_timezone": "1", "language_switch": "0", "language": ["en-US", "en"],
                "page_language_switch": "0", "page_language": "en-US",
                "browser_kernel_config": {"type": "chrome", "version": "ua_auto"},
                # AdsPower's unrestricted UA pool also includes Android/iOS.
                # Those profiles start in device emulation, changing layout
                # and mouse coordinates. This workflow uses desktop windows.
                "random_ua": {"ua_browser": ["chrome"], "ua_system_version": ["Windows 10", "Windows 11"]},
                "screen_resolution": "none",
            },
        }
        if proxy_config:
            payload["user_proxy_config"] = proxy_config
        elif proxy_id:
            payload["proxyid"] = proxy_id
        else:
            payload["user_proxy_config"] = {"proxy_soft": "no_proxy"}
        try:
            data = self._api_data("/api/v2/browser-profile/create", payload)
        except AdsPowerAPIError as exc:
            # Some AdsPower teams reject the documented ungrouped ID 0.
            # Only an explicit rejection permits another create request.
            message = str(exc).lower()
            if payload["group_id"] != "0" or "group" not in message or not any(word in message for word in ("deleted", "archived")):
                raise
            groups = self.list_groups()
            fallback = next((group for name in ("shopflow", "shopify_register") for group in groups
                             if str(group.get("group_name", "")).casefold() == name and group.get("group_id")), None)
            if not fallback:
                raise AdsPowerAPIError("AdsPower 默认分组不可用，请在连接设置填写有效的店铺分组 ID") from None
            payload["group_id"] = str(fallback["group_id"])
            data = self._api_data("/api/v2/browser-profile/create", payload)
        if not data.get("profile_id"):
            raise RuntimeError("AdsPower 未返回环境 ID，请刷新环境列表确认是否已创建")
        return str(data["profile_id"])

    def update_proxy(self, profile_id, proxy_config):
        self._api_data("/api/v2/browser-profile/update", {
            "profile_id": profile_id, "user_proxy_config": proxy_config,
            "fingerprint_config": {"automatic_timezone": "1"},
        })

    def start(self, profile_id: str, wait_timeout=300, on_wait=None) -> str:
        """启动环境，返回 Playwright 用的 CDP websocket 地址。"""
        time.sleep(1.1)
        ws, err = self._start_v2(profile_id)
        deadline = time.monotonic() + wait_timeout
        while not ws and err and (
            any(s in err.lower() for s in ("is updating", "waiting for download"))
            or ("not ready" in err.lower() and "download" in err.lower())
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("AdsPower 浏览器内核仍在下载，请下载完成后重试（会沿用当前环境）")
            message = "AdsPower 正在下载浏览器内核，稍后自动重试..."
            if on_wait:
                on_wait(message)
            else:
                print(message, flush=True)
            time.sleep(5)
            ws, err = self._start_v2(profile_id)
        if ws:
            return ws
        if err and "not found" not in err.lower() and "404" not in err:
            raise RuntimeError(
                f"AdsPower 启动环境 {profile_id} 失败：{err}。"
                "若提示代理相关，请先在该环境里把代理测通。"
            )
        ws, err = self._start_v1(profile_id)
        if ws:
            return ws
        raise RuntimeError(f"启动 AdsPower 环境失败，profile_id={profile_id}，原因：{err}")

    def stop(self, profile_id: str):
        time.sleep(1.1)
        try:
            resp = self._post("/api/v2/browser-profile/stop", json={"profile_id": profile_id})
            data = resp.json()
            if data.get("code") == 0:
                return
        except Exception:
            pass
        try:
            self._get("/api/v1/browser/stop", params={"user_id": profile_id})
        except Exception as e:
            print(f"关闭 AdsPower 环境时出错（可忽略）：{e}")

    def _start_v2(self, profile_id: str):
        try:
            resp = self._post(
                "/api/v2/browser-profile/start",
                json={
                    "profile_id": profile_id,
                    "last_opened_tabs": "0",
                    "proxy_detection": "1",
                    "headless": "0",
                },
            )
            data = resp.json()
        except Exception as e:
            return None, str(e)
        if data.get("code") == 0:
            return (data.get("data") or {}).get("ws", {}).get("puppeteer"), None
        msg = data.get("msg") or str(data)
        print(f"AdsPower v2 启动返回：{data}")
        return None, msg

    def _start_v1(self, profile_id: str):
        try:
            resp = self._get("/api/v1/browser/start", params={"user_id": profile_id, "open_tabs": 0})
            data = resp.json()
        except Exception as e:
            return None, str(e)
        if data.get("code") == 0:
            return (data.get("data") or {}).get("ws", {}).get("puppeteer"), None
        msg = data.get("msg") or str(data)
        print(f"AdsPower v1 启动返回：{data}")
        return None, msg
