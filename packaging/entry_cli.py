"""PyInstaller entry point for the ``vinted`` CLI executable."""
from vinted_mcp.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
