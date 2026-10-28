"""Check distribution contents for accidental credentials and missing license."""
from pathlib import Path
import sys
import tarfile
import zipfile

root = Path(sys.argv[1])
archives = list(root.glob("*.whl")) + list(root.glob("*.tar.gz"))
assert archives, "no distribution archives"
for archive in archives:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as fh:
            names = fh.namelist()
    else:
        with tarfile.open(archive) as fh:
            names = fh.getnames()
    forbidden = {"cookies.txt", "credentials.json", "session.json", ".env"}
    assert not any(Path(n).name in forbidden for n in names), f"private file in {archive}"
    assert any(Path(n).name == "LICENSE" for n in names), f"missing license in {archive}"
    assert any(n.endswith("vinted_mcp/mcp_server.py") for n in names), f"missing MCP server in {archive}"
    print(f"{archive.name}: package contents OK")
