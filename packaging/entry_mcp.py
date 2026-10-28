"""PyInstaller entry point for the ``vinted-mcp`` executable."""
from vinted_mcp.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
