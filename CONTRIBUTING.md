# Contributing

Keep changes focused and describe the observable behavior they fix. Bug reports should include the command or MCP tool, version, sanitized input, expected result and actual result. Never include session files, tokens, cookies or private messages.

## Checks

Python 3.10+.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test,build]'
.venv/bin/python -m pytest
.venv/bin/python -m build && .venv/bin/python scripts/check_package.py dist
.venv/bin/python scripts/build_binary.py      # PyInstaller executables in dist/bin/
```

Releases: bump `VERSION` in `vinted_mcp/config.py`, then push a `vX.Y.Z` tag.
The release workflow tests, publishes to PyPI (trusted publishing) and attaches
Linux/macOS/Windows executables to the GitHub release.

Add regression tests for changed API behavior using local HTTP servers or synthetic fixtures. The regular test suite must not contact Vinted or read a developer's session. Label any manually verified live behavior separately.

Keep stdout reserved for the MCP server's stdio transport. Bound new network responses and outputs. Treat mutations as non-retryable unless the upstream service provides a verified idempotency mechanism. Preserve created resource IDs after partial failures.

Use the endpoint research as evidence, not as proof that an endpoint still works. Document agent-facing changes in [AGENTS.md](AGENTS.md) and endpoints in [docs/ENDPOINTS.md](docs/ENDPOINTS.md).

AI-assisted changes are welcome when reviewed and tested by the contributor. Do not commit agent transcripts, private prompts, worktree paths or generated planning logs.
