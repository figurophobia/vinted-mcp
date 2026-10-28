# PyInstaller spec: single-file executables for the MCP server and the CLI.
#
#   python scripts/build_binary.py     (builds into dist/bin/ and smoke-tests)
#
# Produces ``vinted-mcp`` (stdio MCP server) and ``vinted`` (CLI).
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

HERE = SPECPATH  # noqa: F821 (injected by PyInstaller)

# The mcp SDK imports parts of itself lazily; bundle it whole except its
# own ``mcp`` developer CLI (needs typer, unused here).
hiddenimports = collect_submodules("vinted_mcp") + collect_submodules(
    "mcp", filter=lambda name: not name.startswith("mcp.cli")
)
datas = copy_metadata("mcp")
# curl_cffi ships a native libcurl-impersonate; bundle its binaries and data.
curl_datas, curl_binaries, curl_hidden = collect_all("curl_cffi")
datas += curl_datas
hiddenimports += curl_hidden
excludes = ["playwright", "tkinter", "pytest"]


def build(entry, name):
    a = Analysis(  # noqa: F821
        [f"{HERE}/{entry}"],
        pathex=[f"{HERE}/.."],
        datas=datas,
        binaries=curl_binaries,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )
    pyz = PYZ(a.pure)  # noqa: F821
    return EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=name,
        console=True,
        strip=False,
        upx=False,
    )


build("entry_mcp.py", "vinted-mcp")
build("entry_cli.py", "vinted")
