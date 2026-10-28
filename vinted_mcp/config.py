"""Vinted constants, hosts and local session storage."""

from __future__ import annotations

import json
import os
from typing import Any

VERSION = "0.1.0"


#: Country domain (``es``, ``fr``, ``it``, ``de``, ``pt``...). Selects the
#: www and api hosts; a session is only valid on the domain it was created on.
DOMAIN = os.environ.get("VINTED_DOMAIN", "es")


def web_base(domain: str | None = None) -> str:
    """``https://www.vinted.<tld>``: SSR pages and the auth endpoints."""
    return f"https://www.vinted.{domain or DOMAIN}"


def core_base(domain: str | None = None) -> str:
    """Legacy REST API (``/api/v2``): users, items, uploads, transactions."""
    return f"{web_base(domain)}/api/v2"


def edge_base(domain: str | None = None) -> str:
    """Service API host: catalogue search, messaging, notifications, wallet."""
    return f"https://api.vinted.{domain or DOMAIN}"


#: The web bundle sends this constant as ``X-CSRF-Token`` on every API call.
#: It is baked into the JS build and has been stable; override with
#: ``VINTED_CSRF_TOKEN`` if Vinted rotates it.
CSRF_TOKEN = os.environ.get("VINTED_CSRF_TOKEN", "75f6c9fa-dc8e-4e52-a000-e09dd4084b3e")


#: TLS/HTTP2 fingerprint used by curl_cffi; Vinted sits behind Cloudflare and
#: DataDome, which reject plain Python clients.
IMPERSONATE = os.environ.get("VINTED_IMPERSONATE", "chrome")


LOCALES = {"es": "es-ES", "fr": "fr-FR", "it": "it-IT", "de": "de-DE", "pt": "pt-PT", "nl": "nl-NL", "be": "fr-BE",
           "co.uk": "en-GB", "com": "en-US", "pl": "pl-PL", "lt": "lt-LT", "cz": "cs-CZ", "at": "de-AT"}


def locale(domain: str | None = None) -> str:
    return LOCALES.get(domain or DOMAIN, "en-GB")


CONFIG_DIR = os.environ.get("VINTED_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "vinted-mcp")


SESSION_PATH = os.path.join(CONFIG_DIR, "session.json")


#: Cookies that make up a usable session. ``access_token_web`` is a short-lived
#: JWT; ``refresh_token_web`` lets :meth:`Vinted.refresh` mint a new one.
AUTH_COOKIES = ("access_token_web", "refresh_token_web")


#: Cookies worth keeping besides the auth pair (anti-bot clearance, anon id).
KEEP_COOKIES = AUTH_COOKIES + (
    "_vinted_fr_session", "anon_id", "datadome", "cf_clearance", "__cf_bm", "v_udt", "v_sid", "v_uid",
)


#: Item condition (``status``) ids, as used by search filters and uploads.
CONDITIONS = {
    "new_with_tags": 6,
    "new_without_tags": 1,
    "very_good": 2,
    "good": 3,
    "satisfactory": 4,
}


#: Search ``order`` values accepted by the catalogue.
ORDERS = ("relevance", "newest_first", "price_low_to_high", "price_high_to_low")


def load_session(path: str | None = None) -> dict[str, Any]:
    """Return the stored session ``{"domain": ..., "cookies": {...}}`` (empty if none)."""
    p = path or SESSION_PATH
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "cookies" not in data:  # bare cookie map
        data = {"cookies": data}
    return data


def save_session(session: dict[str, Any], path: str | None = None) -> None:
    """Write the session file with owner-only permissions."""
    p = path or SESSION_PATH
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=1)
    os.replace(tmp, p)
