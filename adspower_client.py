import time
import requests


class AdsPowerClient:
    """连接本机 AdsPower 本地 API，启动/关闭环境窗口。"""

    def __init__(self, api_base: str, api_key: str = ""):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
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

    def start(self, profile_id: str) -> str:
        """启动环境，返回 Playwright 用的 CDP websocket 地址。"""
        time.sleep(1.1)
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
