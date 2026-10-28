"""Session bootstrap: import browser cookies and build a signed-in client.

Vinted has no password login for Google/Apple accounts and protects the
OAuth endpoint with anti-bot checks, so a session is always taken from a
browser where you are already logged in:

- ``import_browser("chromium" | "chrome" | "brave" | "edge" | "firefox")``
  reads the cookie store directly (Linux/macOS Chromium cookies are decrypted
  with the OS keyring key, or the ``peanuts`` fallback).
- ``import_cookie_text(text)`` accepts a ``Cookie:`` header, a JSON map/list
  (e.g. a cookie-editor export) or a Netscape ``cookies.txt``.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from typing import Optional

from . import config as _cfg
from .client import Vinted
from .http import ApiError


class SessionError(ApiError):
    """No usable session was found."""


# --- cookie parsing ----------------------------------------------------------


def parse_cookie_text(text: str) -> dict[str, str]:
    """Parse a Cookie header, a JSON export or a Netscape cookies.txt."""
    t = text.strip()
    if t.lower().startswith("cookie:"):
        t = t[7:].strip()
    if t.startswith("{") or t.startswith("["):
        data = json.loads(t)
        if isinstance(data, dict):
            data = data.get("cookies", data)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {c["name"]: c["value"] for c in data if isinstance(c, dict) and "name" in c and "vinted" in c.get("domain", "vinted")}
    if "\t" in t:
        out = {}
        for line in t.splitlines():
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "vinted" in parts[0]:
                out[parts[5]] = parts[6]
        return out
    out = {}
    for part in t.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _keep(cookies: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in cookies.items() if k in _cfg.KEEP_COOKIES and v}


# --- browser stores ------------------------------------------------------------

#: Browser profile used by ``vinted login --wizard`` (its own Vinted session).
WIZARD_PROFILE = os.path.join(_cfg.CONFIG_DIR, "browser-profile")

_CHROMIUM_DIRS = {
    "chromium": ["~/.config/chromium", "~/Library/Application Support/Chromium"],
    "chrome": ["~/.config/google-chrome", "~/Library/Application Support/Google/Chrome"],
    "brave": ["~/.config/BraveSoftware/Brave-Browser", "~/Library/Application Support/BraveSoftware/Brave-Browser"],
    "edge": ["~/.config/microsoft-edge", "~/Library/Application Support/Microsoft Edge"],
    "vivaldi": ["~/.config/vivaldi", "~/Library/Application Support/Vivaldi"],
}

_KEYRING_LABELS = {
    "chromium": ("Chromium Safe Storage", "Chromium"),
    "chrome": ("Chrome Safe Storage", "Chrome"),
    "brave": ("Brave Safe Storage", "Brave"),
    "edge": ("Microsoft Edge Safe Storage", "Microsoft Edge"),
    "vivaldi": ("Vivaldi Safe Storage", "Vivaldi"),
}


def _copy_db(path: str) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    shutil.copyfile(path, tmp)
    return tmp


def _chromium_cookie_files(browser: str, profile: Optional[str]) -> list[str]:
    files: list[str] = []
    for base in ([WIZARD_PROFILE] if browser == "wizard" else _CHROMIUM_DIRS[browser]):
        base = os.path.expanduser(base)
        profiles = [profile] if profile else ["Default", "Profile *"]
        for prof in profiles:
            for d in glob.glob(os.path.join(base, prof)):
                for name in ("Network/Cookies", "Cookies"):
                    p = os.path.join(d, name)
                    if os.path.isfile(p):
                        files.append(p)
                        break
    return files


def _chromium_password(browser: str) -> list[bytes]:
    """Candidate AES key passwords: OS keyring secret, then the ``peanuts`` default."""
    label, account = _KEYRING_LABELS.get(browser, _KEYRING_LABELS["chromium"])
    out: list[bytes] = []
    if sys.platform == "darwin":
        import subprocess

        try:
            pw = subprocess.run(["security", "find-generic-password", "-w", "-s", label],
                                capture_output=True, text=True, timeout=20).stdout.strip()
            if pw:
                out.append(pw.encode())
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            import secretstorage  # type: ignore

            bus = secretstorage.dbus_init()
            for it in secretstorage.get_default_collection(bus).get_all_items():
                if it.get_label() == label:
                    out.append(it.get_secret())
        except Exception:  # keyring absent/locked: fall back to the default key
            pass
    out.append(b"peanuts")
    return out


def _decrypt_chromium(blob: bytes, keys: list[bytes], host: str) -> Optional[str]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if blob[:3] not in (b"v10", b"v11"):
        return None
    host_hash = hashlib.sha256(host.encode()).digest()
    for key in keys:
        try:
            dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
            raw = dec.update(blob[3:]) + dec.finalize()
            pad = raw[-1]
            if not 1 <= pad <= 16:
                continue
            raw = raw[:-pad]
            if raw[:32] == host_hash:  # Chromium >= 130 prefixes the host hash
                raw = raw[32:]
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    return None


def chromium_cookies(browser: str = "chromium", domain: Optional[str] = None,
                     profile: Optional[str] = None) -> dict[str, str]:
    """Read and decrypt the vinted.<tld> cookies of a Chromium-family browser (Linux/macOS)."""
    if browser not in _CHROMIUM_DIRS and browser != "wizard":
        raise SessionError(f"unsupported browser {browser!r}; use one of {', '.join(_CHROMIUM_DIRS)} or firefox")
    if sys.platform == "win32":
        raise SessionError("Windows cookie stores are app-bound encrypted; use --cookies-file instead")
    tld = domain or _cfg.DOMAIN
    files = _chromium_cookie_files(browser, profile)
    if not files:
        raise SessionError(f"no {browser} cookie database found")
    pw = _chromium_password(browser)
    iterations = 1003 if sys.platform == "darwin" else 1
    keys = [hashlib.pbkdf2_hmac("sha1", p, b"saltysalt", iterations, 16) for p in pw]
    best: dict[str, str] = {}
    for f in files:
        tmp = _copy_db(f)
        try:
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "SELECT host_key, name, value, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (f"%vinted.{tld}",),
            ).fetchall()
            con.close()
        finally:
            os.unlink(tmp)
        got: dict[str, str] = {}
        for host, name, value, enc in rows:
            v = value or (_decrypt_chromium(enc, keys, host) if enc else None)
            if v:
                got[name] = v
        if "refresh_token_web" in got and "refresh_token_web" not in best:
            best = got
        elif not best:
            best = got
    return best


def firefox_cookies(domain: Optional[str] = None, profile: Optional[str] = None) -> dict[str, str]:
    tld = domain or _cfg.DOMAIN
    roots = ["~/.mozilla/firefox", "~/Library/Application Support/Firefox/Profiles", "~/.zen", "~/.librewolf"]
    files = []
    for r in roots:
        files += glob.glob(os.path.join(os.path.expanduser(r), profile or "*", "cookies.sqlite"))
    files.sort(key=os.path.getmtime, reverse=True)
    for f in files:
        tmp = _copy_db(f)
        try:
            con = sqlite3.connect(tmp)
            rows = con.execute("SELECT name, value FROM moz_cookies WHERE host LIKE ?", (f"%vinted.{tld}",)).fetchall()
            con.close()
        finally:
            os.unlink(tmp)
        got = dict(rows)
        if got.get("refresh_token_web"):
            got["__profile__"] = os.path.basename(os.path.dirname(f))
            return got
    return {}


def import_browser(browser: str, domain: Optional[str] = None, profile: Optional[str] = None,
                   path: Optional[str] = None) -> dict:
    """Copy a logged-in browser session into the local session file."""
    cookies = read_browser_cookies(browser, domain, profile)
    return _store(cookies, domain, path, source=browser, profile=cookies.pop("__profile__", profile))


def read_browser_cookies(browser: str, domain: Optional[str] = None, profile: Optional[str] = None) -> dict[str, str]:
    if browser == "firefox":
        return firefox_cookies(domain, profile)
    return chromium_cookies(browser, domain, profile)


def import_cookie_text(text: str, domain: Optional[str] = None, path: Optional[str] = None) -> dict:
    return _store(parse_cookie_text(text), domain, path, source="cookies")


def _store(cookies: dict[str, str], domain: Optional[str], path: Optional[str], source: str,
           profile: Optional[str] = None) -> dict:
    kept = _keep(cookies)
    if not kept.get("refresh_token_web") and not kept.get("access_token_web"):
        raise SessionError(
            "no Vinted session cookies found (access_token_web / refresh_token_web). "
            "Log in to vinted in that browser first."
        )
    if kept.get("access_token_web") and not _jwt(kept["access_token_web"]).get("sub"):
        raise SessionError("that browser holds an anonymous (logged-out) Vinted session; log in there first")
    session = {"domain": domain or _cfg.DOMAIN, "source": source, "cookies": kept}
    if profile:
        session["profile"] = profile
    _cfg.save_session(session, path)
    return session


# --- login wizard ---------------------------------------------------------------


def login_wizard(domain: Optional[str] = None, path: Optional[str] = None, timeout_s: int = 600,
                 executable: Optional[str] = None) -> dict:
    """Open a dedicated browser, let the user log in, and store that session.

    The session belongs to its own browser profile, so it does not share a
    refresh token with your everyday browser (Vinted revokes a session when an
    already-rotated refresh token is reused, which happens when two clients
    refresh the same session). Needs ``pip install playwright``.
    """
    import time

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - optional dependency
        raise SessionError("the wizard needs playwright: pip install 'vinted-mcp[browser]'") from e
    tld = domain or _cfg.DOMAIN
    exe = executable or next((p for p in ("/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser")
                              if os.path.exists(p)), None)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            WIZARD_PROFILE, headless=False, executable_path=exe, locale=_cfg.locale(tld),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://www.vinted.{tld}/member/signup/select_type?ref_url=%2F")
        print("Log in to Vinted in the opened window; it closes by itself once you are in.", file=sys.stderr)
        deadline = time.monotonic() + timeout_s
        cookies: dict[str, str] = {}
        while time.monotonic() < deadline:
            cookies = {c["name"]: c["value"] for c in ctx.cookies(f"https://www.vinted.{tld}")}
            # Anonymous visitors get access/refresh tokens too; only a token with a user (``sub``) means logged in.
            if cookies.get("refresh_token_web") and _jwt(cookies.get("access_token_web")).get("sub"):
                page.wait_for_timeout(2000)
                cookies = {c["name"]: c["value"] for c in ctx.cookies(f"https://www.vinted.{tld}")}
                break
            page.wait_for_timeout(1000)
        ctx.close()
    if not cookies.get("refresh_token_web"):
        raise SessionError("timed out waiting for the Vinted login")
    return _store(cookies, tld, path, source="wizard")


# --- client construction -------------------------------------------------------


def build_client(required: bool = True, path: Optional[str] = None) -> Vinted:
    """Return a :class:`Vinted` client from ``VINTED_COOKIES`` or the stored session.

    Refreshed tokens are written back to the session file. Raises
    :class:`SessionError` when ``required`` and no session exists.
    """
    env = os.environ.get("VINTED_COOKIES")
    if env:
        cookies = _keep(parse_cookie_text(env))
        return Vinted(cookies, domain=os.environ.get("VINTED_DOMAIN"))
    p = path or _cfg.SESSION_PATH
    session = _cfg.load_session(p)
    cookies = session.get("cookies") or {}
    if required and not (cookies.get("refresh_token_web") or cookies.get("access_token_web")):
        raise SessionError("not logged in. run: vinted login --browser chromium  (or --cookies-file cookies.txt)")
    domain = session.get("domain") or _cfg.DOMAIN

    def persist(client: Vinted) -> None:
        cur = _cfg.load_session(p)
        cur["domain"] = domain
        cur["cookies"] = _keep({**(cur.get("cookies") or {}), **client.cookies()})
        _cfg.save_session(cur, p)

    client = Vinted(cookies, domain=domain, persist_fn=persist if cookies else None)
    source = session.get("source")
    if source in _BROWSER_SOURCES:
        client.http.on_unauthorized = lambda: recover_session(client, source, domain, session.get("profile"))
    return client


_BROWSER_SOURCES = set(_CHROMIUM_DIRS) | {"firefox", "wizard"}


def _jwt(token: Optional[str]) -> dict:
    import base64

    try:
        return json.loads(base64.urlsafe_b64decode((token or "").split(".")[1] + "=="))
    except (IndexError, ValueError):
        return {}


def _usable(cookies: dict[str, str], user_id: Optional[str], margin: int = 60) -> bool:
    """A logged-in (``sub`` present), unexpired access token, for the same user when known."""
    import time

    payload = _jwt(cookies.get("access_token_web"))
    if not payload.get("sub") or payload.get("exp", 0) - time.time() < margin:
        return False
    return user_id is None or str(payload["sub"]) == str(user_id)


def recover_session(client: Vinted, source: str, domain: str, profile: Optional[str] = None) -> bool:
    """Get valid tokens back after a 401, without fighting the source browser.

    Vinted rotates the refresh token on every refresh and kills the session
    when a rotated token is replayed, and the source browser refreshes the
    same session on its own. So: (1) if the browser already holds a newer,
    valid access token, adopt it (no refresh at all); (2) otherwise refresh
    with our token; (3) if that fails and the browser has a different refresh
    token (it rotated first), adopt it and refresh with that one.
    """
    mine = client.cookies()
    user_id = _jwt(mine.get("access_token_web")).get("sub") or _jwt(mine.get("refresh_token_web")).get("sub")
    try:
        fresh = _keep(read_browser_cookies(source, domain, profile))
    except (SessionError, OSError, sqlite3.Error):
        fresh = {}

    def adopt(c: dict[str, str]) -> None:
        for name in _cfg.AUTH_COOKIES:
            if c.get(name):
                client.http.set_cookie(name, c[name])
        client._persist()

    if fresh.get("access_token_web") != mine.get("access_token_web") and _usable(fresh, user_id):
        adopt(fresh)
        return True
    if client.refresh():
        return True
    if fresh.get("refresh_token_web") and fresh["refresh_token_web"] != mine.get("refresh_token_web"):
        adopt(fresh)
        return client.refresh()
    return False


def session_info(path: Optional[str] = None) -> dict:
    """Describe the stored session without any network call."""
    import base64
    import time

    session = _cfg.load_session(path)
    cookies = session.get("cookies") or {}
    info = {
        "logged_in": bool(cookies.get("refresh_token_web") or cookies.get("access_token_web")),
        "domain": session.get("domain") or _cfg.DOMAIN,
        "source": session.get("source"),
        "has_refresh_token": bool(cookies.get("refresh_token_web")),
    }
    for name in ("access_token_web", "refresh_token_web"):
        tok = cookies.get(name) or ""
        try:
            payload = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
            info[name.replace("_web", "") + "_expires_in_s"] = int(payload.get("exp", 0) - time.time())
            if name == "access_token_web" and payload.get("sub"):
                info["user_id"] = payload["sub"]
        except (IndexError, ValueError):
            pass
    return info


__all__ = ["SessionError", "build_client", "import_browser", "import_cookie_text", "login_wizard", "parse_cookie_text",
           "read_browser_cookies", "recover_session", "session_info"]
