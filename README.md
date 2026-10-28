# vinted-mcp

An [MCP](https://modelcontextprotocol.io) server for Vinted, written in Python:
let an AI agent search listings, read item and seller details, favourite,
follow, chat with sellers and buyers, negotiate offers, read orders and the
wallet, and manage your own listings (publish, edit, hide, delete, drafts).

The same package ships the `vinted` CLI and the typed `vinted_mcp` Python
client; all three share one API client. It talks to the internal API of the
Vinted web app (no browser automation). **Checkout and payment are not
implemented on purpose: buying stays manual.**

Sibling project of [wallapop-mcp](https://github.com/figurophobia/wallapop-mcp),
with the same layout so both can be unified later.

## Install

Python 3.10+.

```sh
pip install -e .            # from this checkout (deps: mcp, curl_cffi, cryptography, secretstorage)
```

`curl_cffi` is required: Vinted sits behind Cloudflare + DataDome and rejects
plain Python HTTP clients, so requests go out with a Chrome TLS fingerprint.

## Log in

Vinted accounts are often Google/Apple-only and the OAuth endpoint is
bot-protected, so the session is imported from a browser where you are already
logged in:

```sh
vinted login --wizard                  # recommended: log in once in a dedicated window (pip install -e '.[browser]')
vinted login --browser chromium        # or chrome | brave | edge | vivaldi | firefox  [--profile "Profile 1"]
vinted login --cookies-file cookies.txt  # Cookie header, JSON export or Netscape cookies.txt ('-' = stdin)
vinted session                          # offline: domain, token expiry
```

The session lives in `~/.config/vinted-mcp/session.json` (mode 600; override
with `VINTED_CONFIG_DIR`, or pass `VINTED_COOKIES` in the environment). The
access token is short-lived (~1 h); the client refreshes it with the refresh
token (valid ~7 days, renewed on each refresh) and writes it back. Other
countries: `--domain fr` / `VINTED_DOMAIN=fr` (a session is only valid on its
own domain).

> **Session recovery.** Vinted rotates the refresh token on every refresh and
> revokes the whole session if an already-rotated token is replayed. A session
> imported with `--browser` is shared with that browser, so on a `401` the
> client first re-reads the browser's cookies and adopts a newer valid token
> (no refresh, no conflict), then refreshes with its own token, and finally
> retries with the browser's rotated refresh token. `--wizard` gives the
> library a session of its own and avoids sharing altogether.

## Use it from an MCP client

```json
{
  "mcpServers": {
    "vinted": { "command": "vinted-mcp", "args": ["--allow-writes"] }
  }
}
```

Writes are hidden unless `--allow-writes` is passed. 43 tools (25 read, 18
write); full reference and agent flows in [AGENTS.md](AGENTS.md).

## CLI

```sh
vinted search "nintendo 3ds" --catalog 3025 --condition very_good,good --max-price 90 --order price_low_to_high
vinted categories --suggest "nintendo 3ds"          # where items like this are listed
vinted listing-options 3025                          # required attributes + package sizes
vinted show 1234567890                              # item details
vinted message "¿Sigue disponible?" --item 1234567891
vinted offer send 1234567892 --price 36
vinted publish ~/fotos/3ds --title "Nintendo 3DS" --price 120 --catalog 3025 --condition good \
    --brand-id 77600 --brand Nintendo --package-size 8 --attr video_game_platform=1265
vinted edit 1234567893 --price 110
vinted hide 1234567893            # --show to unhide
vinted delete 1234567893
```

## Library

```python
from vinted_mcp import build_client

v = build_client()
for item in v.search_items("nintendo 3ds", catalog_ids=[3025], conditions=["good"], price_to=80):
    print(item.price, item.title, item.url)
```

## Develop

```sh
uv venv && uv pip install -e '.[test]'
.venv/bin/python -m pytest        # offline, no account needed
```

Endpoint research is in [docs/ENDPOINTS.md](docs/ENDPOINTS.md).

## Status

Experimental, unofficial. The endpoints come from the web app and can change.
Writes may have partial or unknown outcomes; inspect state before retrying.
Vinted removes listings it considers duplicates of one already online (and may
restrict publishing after repeated duplicates), so never list the same item
twice.

MIT license. Vinted and its trademarks belong to their respective owners; this
project is not affiliated with Vinted.
