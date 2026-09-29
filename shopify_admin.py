"""Validate store admin addresses without guessing handles from display names."""
import re
from urllib.parse import urlsplit


def admin_store_base(url):
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.netloc.lower() != "admin.shopify.com":
        return ""
    match = re.match(r"^/store/([a-z0-9][a-z0-9-]*)(?:/|$)", parts.path, re.I)
    return "https://admin.shopify.com/store/" + match.group(1) if match else ""
