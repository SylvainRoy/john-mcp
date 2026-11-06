# Project information

- This project exposes user-initiated, read-only document tools and optional draft-only Gmail tools over MCP stdio. It has no autonomous agent loop, scheduler, or send/delivery tools. Gmail drafts are never sent: users review and send manually in Gmail. Never add delivery operations or send tools.
- Reuse `mdtodb` and `pdftomd` as Python libraries only. Do not invoke their CLIs.
- The `mdtodb` dependency is pinned to an immutable GitHub commit in `pyproject.toml` and `uv.lock`; no sibling checkout is required. Chroma compatibility remains pinned to 1.5.9.
- Never read credential files or run tests against the user's real document repository, Chroma database, or Google accounts. Use synthetic temporary fixtures and fake embeddings.
- Never create, prune, reindex, migrate, or rename the user's source/index data from the MCP tool layer.
- Install the locked environment with `uv sync --frozen --python 3.13`.
- Run this project's tests with `uv run --frozen pytest`.
- Retrieval-library tests live in the separate `mdtodb` repository. This project's tests exercise its pinned dependency.
- Inspect startup options with `uv run --frozen john-mcp --help`.
- The non-active client configuration example is `.devin/mcp_config.example.json`. It launches John with `--config /absolute/path/to/john-mcp.json`; replace its placeholders with your own locations. Keep live credentials in a private configuration file or the process environment, never in committed configuration.
- The workspace checkout directory is `john-mcp`; the Python distribution remains `john-agent` and the import package remains `john_agent`.

## Configuration

- Create a template with `uv run --frozen john-mcp --init-config /absolute/private/path/john-mcp.json`. The parent directory must exist; the command creates a mode-0600 file and refuses to overwrite any existing path.
- Settings resolve in this order: explicit CLI options, the selected JSON file, inherited environment variables, built-in defaults. No configuration file is discovered automatically. See `CONFIGURATION.md` and `john-mcp.example.json` for the schema and environment aliases.
- Relative paths in the file resolve against its directory. Optional `null` values suppress environment fallback; duplicate definitions in structured settings and the file's `env` map are rejected.
- Validate locally with `uv run --frozen john-mcp --config /absolute/private/path/john-mcp.json --check-config`. This does not contact Google or inspect corpus/index contents. Restart the server after configuration changes.
- The server reads the explicitly selected configuration at startup, including any embedded secrets, but never rewrites it. Files containing secrets or `env` entries must be owned by the current user with no group/other permissions on POSIX.

## Gmail tools

- Gmail is opt-in through `gmail.enabled`, `--gmail`, or `JOHN_GMAIL`; `--no-gmail` overrides the file and environment. When disabled, only the five corpus tools are registered. The three additional tools are `search_emails`, `read_email`, and `read_email_attachment`.
- Gmail uses one personal account, native Gmail search, and the `https://www.googleapis.com/auth/gmail.readonly` scope. It never sends mail, changes labels/read state, imports documents, or updates indexes. Spam and Trash are excluded from searches unless explicitly requested.
- Put `client_id`, `client_secret`, and `refresh_token` in `gmail.credentials`. Structured credentials use the authorized-user type and Gmail read-only scope; other types or scopes are rejected. `JOHN_GMAIL_CREDENTIALS` and the explicit `--gmail-credentials` CLI override accept complete authorized-user JSON. Credentials are selected as a whole object, never merged between sources. Do not put credential values in MCP tool arguments or committed configuration.
- For first-time setup, enable the Gmail API in a Google Cloud project and create an OAuth Desktop app client. Run `uv run --frozen john-gmail-auth --config /absolute/private/path/john-mcp.json` yourself in an interactive terminal. It reuses configured client settings, prompts for missing values, performs browser consent with a loopback callback, and atomically saves credentials into the same file with mode 0600 while preserving other settings. Set `gmail.enabled` to `true` and restart the MCP server afterwards.
- The older `john-gmail-auth --output /absolute/private/path/gmail-credentials.json` flow remains supported; it creates a separate file without overwriting an existing path, whose JSON can be supplied through `JOHN_GMAIL_CREDENTIALS`. Startup and tool discovery do not contact Google or refresh access tokens. Do not paste credentials into chat or logs; never run real OAuth consent in automated tests.
- Google external OAuth apps in Testing issue Gmail refresh tokens that expire after seven days. Handle personal-use publishing/consent settings deliberately and rerun the separate setup when reauthorization is needed; public distribution requires a separate OAuth-policy review.
- Draft creation is opt-in through `gmail.drafts`, `--gmail-drafts`, or `JOHN_GMAIL_DRAFTS`, and requires `gmail.enabled` plus credentials authorized with the `gmail.compose` scope (run `john-gmail-auth --drafts`). It registers `create_draft` and `create_reply_draft`, which only create drafts — they never send mail. `gmail.attachment_roots` (`--attachment-root`, `JOHN_GMAIL_ATTACHMENT_ROOTS`) bounds local file attachments; `gmail.source_root` (`--source-root`, `JOHN_GMAIL_SOURCE_ROOT`) enables manifest `document_id` attachments. Attachments stay inside those roots, reject Google Drive stubs, and are capped at 25 MiB total.
- Email and PDF contents are transient. PDFs are limited to 10 MiB, 100 pages, and 1,000,000 extracted characters, with a 15-second local worker timeout. The worker receives no credential environment. OCR, cloud conversion, Office attachments, remote-link fetching, and persistent mail indexing are not supported.
- Gmail tests use fake HTTP transports, synthetic MIME messages/PDFs, and blocked network connections. Run the complete verification with `uv run --frozen pytest`; inspect setup help with `uv run --frozen john-gmail-auth --help`.
- Dependency updates must preserve the existing `uv.lock` `exclude-newer` cutoff unless a change is explicitly approved.
