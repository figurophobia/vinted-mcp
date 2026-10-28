"""vinted_mcp: typed Vinted web-API client, CLI and MCP server.

Public surface: ``Vinted`` (client), ``Item`` (compact listing),
``build_client`` / ``import_browser`` / ``import_cookie_text`` /
``session_info`` (session helpers), ``ApiError`` / ``Response``.
Checkout and payment are intentionally not implemented.
"""

from .config import VERSION as __version__
from .http import ApiError, Response, TransportError, UnknownOutcomeError
from .ids import item_id, user_id
from .models import Item
from .client import Vinted, condition_id
from .session import SessionError, build_client, import_browser, import_cookie_text, session_info

__all__ = [
    "__version__", "ApiError", "Item", "Response", "SessionError", "TransportError", "UnknownOutcomeError",
    "Vinted", "build_client", "condition_id", "import_browser", "import_cookie_text", "item_id", "session_info",
    "user_id",
]
