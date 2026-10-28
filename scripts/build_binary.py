"""Build the standalone executables with PyInstaller and smoke-test them.

Usage: ``python scripts/build_binary.py`` (needs ``pip install -e '.[build]'``).
Executables land in ``dist/bin/``; the MCP binary is checked with a real
``initialize`` + ``tools/list`` exchange over stdio.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "bin"
EXE = ".exe" if platform.system() == "Windows" else ""


def build() -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
         str(ROOT / "packaging" / "vinted.spec")],
        check=True,
    )


def smoke_mcp(binary: Path) -> int:
    """Run ``initialize`` + ``tools/list`` over stdio; return the tool count.

    stdin stays open until the answer arrives: closing it early lets the
    server shut down before it has replied.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [str(binary), "--session", str(ROOT / "build" / "empty-session")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    timer = threading.Timer(120, proc.kill)
    timer.start()
    try:
        assert proc.stdin and proc.stdout
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == 2:
                return len(msg["result"]["tools"])
    finally:
        timer.cancel()
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=30)
    raise SystemExit(f"no tools/list answer from {binary.name}:\n{proc.stderr.read() if proc.stderr else ''}")


def main() -> int:
    build()
    (ROOT / "build" / "empty-session").mkdir(parents=True, exist_ok=True)
    mcp_bin, cli_bin = DIST / f"vinted-mcp{EXE}", DIST / f"vinted{EXE}"
    version = subprocess.run([str(mcp_bin), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    print(f"{version}: {smoke_mcp(mcp_bin)} tools")
    subprocess.run([str(cli_bin), "--help"], capture_output=True, check=True)
    print(f"{cli_bin.name}: --help OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
