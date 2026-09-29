"""Fetch only the requested contact fields from toolqd's server-rendered page."""
import json
import re
import secrets
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests


COUNTRIES = {
    "es": {"label": "西班牙", "name": "Spain", "dial": "34"},
    "fr": {"label": "法国", "name": "France", "dial": "33"},
    "de": {"label": "德国", "name": "Germany", "dial": "49"},
    "it": {"label": "意大利", "name": "Italy", "dial": "39"},
    "uk": {"label": "英国", "name": "United Kingdom", "dial": "44"},
    "us": {"label": "美国", "name": "United States", "dial": "1"},
    "ca": {"label": "加拿大", "name": "Canada", "dial": "1"},
}


class NuxtDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NUXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def parse_profile(html, country):
    if country not in COUNTRIES:
        raise ValueError("暂不支持该国家")
    parser = NuxtDataParser()
    parser.feed(html)
    try:
        items = json.loads("".join(parser.parts))
        person = next(x for x in items if isinstance(x, dict) and
                      {"givenname", "surname", "streetaddress", "zipcode"} <= x.keys())

        def value(key):
            index = person.get(key)
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(items):
                return ""
            raw = items[index]
            return str(raw).strip() if isinstance(raw, (str, int)) else ""

        actual_country = value("country").lower()
        if actual_country not in ({"uk", "gb"} if country == "uk" else {country}):
            raise ValueError("资料国家与所选国家不一致")
        dial = value("telephonecountrycode") or COUNTRIES[country]["dial"]
        phone = re.sub(r"\D", "", value("telephonenumber"))
        if country in ("fr", "de", "uk"):
            phone = phone.lstrip("0")
        if country in ("us", "ca") and len(phone) == 11 and phone.startswith("1"):
            phone = phone[1:]
        profile = {
            "name": " ".join(filter(None, [value("givenname"), value("middleinitial"), value("surname")])),
            "first_name": " ".join(filter(None, [value("givenname"), value("middleinitial")])),
            "last_name": value("surname"),
            "address": value("streetaddress"), "city": value("city"),
            "province": value("state") if country in ("es", "us", "ca", "it") else "",
            "region": value("statefull"), "postal_code": value("zipcode"),
            "phone": "+" + dial + phone if phone else "", "country": country,
            "country_name": COUNTRIES[country]["name"],
            "source_url": "https://toolqd.com/fakename-" + country,
            "synthetic": True, "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if not all(profile[k] for k in ("name", "address", "city", "postal_code", "phone")):
            raise ValueError("资料字段不完整")
        return profile
    except (ValueError, TypeError, StopIteration, KeyError) as exc:
        raise ValueError("资料网站返回内容不完整或格式已改变，请重试或手动填写") from exc


def fetch_profile(country):
    if country not in COUNTRIES:
        raise ValueError("暂不支持该国家")
    # Contact lookup uses the local connection, independently of browser proxies.
    # Requests otherwise picks up Windows registry proxies as well as env vars.
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                "https://toolqd.com/fakename-" + country,
                params={"_": secrets.token_hex(8)},
                headers={"User-Agent": "ShopFlow-Local/1.0", "Cache-Control": "no-cache"},
                timeout=(10, 25),
            )
            response.raise_for_status()
    except requests.Timeout:
        raise RuntimeError("获取起点工具资料超时，请稍后重试。") from None
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "异常"
        raise RuntimeError("起点工具返回 HTTP {}，请稍后重试。".format(status)) from None
    except requests.RequestException:
        raise RuntimeError("无法连接起点工具，请检查本机网络后重试。") from None
    if len(response.content) > 2_000_000:
        raise ValueError("资料网站响应过大")
    return parse_profile(response.content.decode("utf-8"), country)
