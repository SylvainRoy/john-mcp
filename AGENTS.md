# Project information

- This project exposes user-initiated, read-only document tools over MCP stdio. It has no autonomous agent loop, scheduler, or external action tools.
- Reuse `mdtodb` and `pdftomd` as Python libraries only. Do not invoke their CLIs.
- The `mdtodb` dependency is pinned to an immutable GitHub commit in `pyproject.toml` and `uv.lock`; no sibling checkout is required. Chroma compatibility remains pinned to 1.5.9.
- Never read credential files or run tests against the user's real document repository, Chroma database, or Google accounts. Use synthetic temporary fixtures and fake embeddings.
- Never create, prune, reindex, migrate, or rename the user's source/index data from the MCP tool layer.
- Install the locked environment with `uv sync --frozen --python 3.13`.
- Run this project's tests with `uv run --frozen pytest`.
- Retrieval-library tests live in the separate `mdtodb` repository. This project's tests exercise its pinned dependency.
- Inspect startup options with `uv run --frozen john-mcp --help`.
- The non-active client configuration example is `.devin/mcp_config.example.json`. Replace its `/absolute/path/to/...` placeholders with your own locations. Credentials belong in the client process environment, never in committed configuration.
