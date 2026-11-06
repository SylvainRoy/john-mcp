# Configuration

`john-mcp` is configured with one explicit JSON file plus optional command-line
overrides. No config file is discovered or read automatically.

## Create a template

```sh
uv run --frozen john-mcp --init-config /absolute/path/to/john-mcp.json
```

`--init-config` must be used alone. It creates a new mode-0600 file inside an
existing directory and never overwrites an existing path (including dangling
symlinks). Edit the placeholders afterwards; `john-mcp.example.json` shows the
full schema:

```json
{
  "version": 1,
  "corpus": {
    "markdown_root": "/absolute/path/to/markdown",
    "chroma_path": null,
    "collection": "documents"
  },
  "embedding": {
    "provider": "gemini",
    "model": "gemini-embedding-001",
    "api_key": null
  },
  "gmail": {
    "enabled": false,
    "credentials": {
      "client_id": null,
      "client_secret": null,
      "refresh_token": null
    }
  },
  "env": {}
}
```

Start the server with an explicit path:

```sh
uv run --frozen john-mcp --config /absolute/path/to/john-mcp.json
```

For Devin, the `john` entry only needs the executable and configuration path:

```json
{
  "command": "/absolute/path/to/john-mcp/.venv/bin/john-mcp",
  "args": ["--config", "/absolute/path/to/john-mcp.json"]
}
```

Use this inside the existing `mcpServers` object, preserving other servers.
The complete example is `.devin/mcp_config.example.json`.

## Precedence

Each setting resolves to the first source that defines it — presence, not
truthiness, decides:

1. Explicit CLI flag (`--collection`, `--embedding`, `--gemini-model`,
   `--gemini-api-key`, `--gmail`/`--no-gmail`, `--gmail-credentials`,
   `--markdown-root`, `--chroma-path`/`--no-chroma-path`).
2. The selected configuration file: structured settings or its `env` map.
3. The inherited process environment.
4. Built-in defaults.

Defining the same setting both structurally and via the file's `env` map is an
error, even when values match. `env` must not contain both `GEMINI_API_KEY` and
`GOOGLE_API_KEY`. An explicit JSON `null` for `chroma_path`, `api_key`, or
`gmail.credentials` deliberately suppresses environment fallback — it does not
mean "unset, look elsewhere". An empty `env` alias value for an API key likewise
means "no key" and blocks inheritance.

| Setting | JSON | env alias(es) | Default |
| --- | --- | --- | --- |
| markdown_root | corpus.markdown_root | JOHN_MARKDOWN_ROOT | required |
| chroma_path | corpus.chroma_path | JOHN_CHROMA_PATH | null |
| collection | corpus.collection | JOHN_COLLECTION | documents |
| embedding | embedding.provider | JOHN_EMBEDDING | gemini |
| gemini_model | embedding.model | JOHN_GEMINI_MODEL | gemini-embedding-001 |
| gemini_api_key | embedding.api_key | GEMINI_API_KEY, GOOGLE_API_KEY | null |
| gmail | gmail.enabled | JOHN_GMAIL | false |
| gmail_credentials | gmail.credentials | JOHN_GMAIL_CREDENTIALS | null |

Booleans accept true/false/1/0/yes/no/on/off (case-insensitive) from `env` or
the inherited environment; JSON `gmail.enabled` must be a real boolean.

Relative paths in the file (including `env` path values) resolve against the
config directory; CLI and inherited-environment paths resolve against the
process working directory. `~` is expanded; `${...}` is not interpolated.

Arbitrary `env` entries are applied to `os.environ` once during normal server
startup — after the configuration resolves — and never during `--help`,
`--init-config`, `--check-config`, or `john-gmail-auth`. John passes structured
API keys and Gmail credentials explicitly to their components. The embedding
library uses an internal environment variable when it initializes Gemini.

Changes take effect on MCP server restart; there is no config reload.

## Validation

```sh
uv run --frozen john-mcp --config /absolute/path/to/john-mcp.json --check-config
```

`--check-config` performs local checks only: it parses and resolves the file,
validates Gmail credentials when `gmail.enabled` is set, and requires a Gemini
API key when a Chroma index is configured with the Gemini provider. It does not
construct readers or embedding clients, apply `env`, contact Google, or check
that corpus/index paths exist.

Config files must be regular, non-symlink files. When a file contains a
nonempty API key, any non-null Gmail credential field, or any `env` entries, it
must be owned by you and have no group/other permissions (mode 0600 on POSIX).
Files holding only non-secret settings may be 0644. Never commit live
credentials; keep real configs outside the checkout (`john-mcp.json` and
`john-mcp.local.json` at the repo root are gitignored).

## Gmail setup

Enable the Gmail API in a Google Cloud project and create an OAuth Desktop app
client. Then run the helper yourself in an interactive terminal:

```sh
uv run --frozen john-gmail-auth --config /absolute/path/to/john-mcp.json
```

It reuses the client ID/secret from the file and prompts for missing values.
Browser consent requests only Gmail read-only access. The helper saves the
resulting authorized-user JSON into `gmail.credentials`, or the existing
`env.JOHN_GMAIL_CREDENTIALS` entry. It preserves other settings, writes atomically
with mode 0600, and refuses to replace a file that changed during consent.
Set `gmail.enabled` to `true`, validate the configuration, and restart the
server to enable Gmail.

The older `john-gmail-auth --output /private/path/gmail-credentials.json` command
still creates a standalone credential file. Its JSON can be supplied through
`JOHN_GMAIL_CREDENTIALS`.

External OAuth apps in Testing mode issue Gmail refresh tokens that expire
after seven days; rerun the helper when reauthorization is needed.

## Startup laziness

Normal startup reads and validates the explicitly selected configuration file,
including embedded credentials. Invalid configuration syntax or field types
are rejected at startup. `--help` does not read that file.

Startup and tool discovery do not read corpus contents, open the Chroma
collection, initialize embeddings, or contact Google. Gmail authorization and
token refresh happen on the first Gmail operation. Missing credentials or an
expired grant do not prevent corpus tools from being used.
