"""Cliproxy short-term ISP API, as documented in the dashboard's API tab.

Extraction consumes the user's IP quota. Never retry an extraction automatically.
"""
import json
import re
from urllib.parse import quote, quote_plus

import requests

from web_storage import now

MIN_EXTRACTION_PORT = 443
MAX_EXTRACTION_PORT = 3000


class ExtractionUncertain(RuntimeError):
    pass


class ExtractionRejected(RuntimeError):
    pass


def provider_error(data, key):
    """Keep useful provider diagnostics without exposing auth or proxy credentials."""
    message = next((data[field] for field in ("msg", "message", "error", "detail")
                    if isinstance(data.get(field), str) and data[field].strip()), "")
    for secret in (key, quote(key, safe=""), quote_plus(key)):
        if secret:
            message = message.replace(secret, "[已隐藏]")
    message = re.sub(r"https?://\S+", "[链接已隐藏]", message, flags=re.I)
    message = re.sub(r"\b[A-Za-z0-9.-]+:\d{1,5}:[^\s:]+:[^\s]+", "[代理已隐藏]", message)
    message = re.sub(r"(?i)\b(key|token|password|passwd|pwd|authorization)\s*[:=]\s*[^\s,;]+",
                     r"\1=[已隐藏]", message)
    message = " ".join(message.split())[:400]
    code = data.get("code")
    code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
    if not re.fullmatch(r"[-A-Za-z0-9_]{1,24}", code) or code == key:
        code = ""
    prefix = "Cliproxy 拒绝提取" + ("（错误码 {}）".format(code) if code else "")
    return prefix + "：" + (message or "请在其网站检查密钥、IP 余额和所选国家库存")


def parse_proxy(value):
    """Only accept the documented type=2 (host:port:user:password) format."""
    parts = str(value).strip().split(":", 3)
    if len(parts) != 4:
        raise ValueError("代理格式应为 host:port:user:password")
    host, port, user, password = parts
    if (not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or not port.isdigit()
            or not 1 <= int(port) <= 65535 or not user or not password
            or any(c.isspace() for c in user + password) or len(user + password) > 1000):
        raise ValueError("代理地址、端口或账号密码无效")
    return {"proxy_soft": "other", "proxy_type": "socks5", "proxy_host": host,
            "proxy_port": str(int(port)), "proxy_user": user, "proxy_password": password}


class CliproxyClient:
    ENDPOINT = "https://webipapi.cliproxy.com/api/getIpInfo"

    def __init__(self, key):
        if not key or re.search(r"\s", key):
            raise ValueError("请在连接设置中填写 Cliproxy 短效 IP 的 API Key")
        self.key = key
        self.session = requests.Session()
        # Fetching an IP must not depend on a separate Windows/system proxy.
        # The extracted proxy is used only by its AdsPower browser environment.
        self.session.trust_env = False

    def extract(self, country, port):
        if isinstance(port, bool) or not isinstance(port, int) or not MIN_EXTRACTION_PORT <= port <= MAX_EXTRACTION_PORT:
            raise ValueError("Cliproxy 提取端口必须在 443–3000 之间")
        country = "GB" if country == "uk" else country.upper()
        params = {"key": self.key, "port": int(port), "num": 1, "country": country,
                  "state": "", "type": 2, "format": "n"}
        try:
            # No redirect or retry: a repeated GET could consume a second IP.
            with self.session.get(self.ENDPOINT, params=params, timeout=(10, 60),
                                  allow_redirects=False, stream=True) as response:
                raw = bytearray()
                for chunk in response.iter_content(4096):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ExtractionUncertain("Cliproxy 返回内容过大，提取结果待确认")
                if response.status_code != 200:
                    raise ExtractionUncertain("Cliproxy 返回 HTTP {}，提取结果待确认".format(response.status_code))
                text = raw.decode("utf-8-sig").strip()
        except requests.RequestException:
            # requests exceptions include the URL (and API key); never surface them.
            raise ExtractionUncertain("Cliproxy 连接中断或超时，提取结果待确认") from None
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        if isinstance(data, dict):
            if data.get("success") is False or ("code" in data and str(data["code"]) not in ("0", "200")):
                raise ExtractionRejected(provider_error(data, self.key))
            data = data.get("data")
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        try:
            if not isinstance(data, str) or len(data.splitlines()) != 1:
                raise ValueError()
            proxy = parse_proxy(data)
        except ValueError:
            raise ExtractionUncertain("Cliproxy 未返回可识别的单条代理，请在其网站确认本次提取结果") from None
        return {"config": proxy, "country": country, "fetched_at": now(), "requested_port": port}
