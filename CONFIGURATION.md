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
    "drafts": false,
    "attachment_roots": [],
    "source_root": null
  },
  "calendar": {
    "enabled": false,
    "write": false
  },
  "google": {
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
   `--gemini-api-key`, `--gmail`/`--no-gmail`, `--google-credentials` (legacy `--gmail-credentials`),
   `--calendar`/`--no-calendar`, `--calendar-write`/`--no-calendar-write`,
   `--gmail-drafts`/`--no-gmail-drafts`, `--attachment-root`, `--source-root`,
   `--markdown-root`, `--chroma-path`/`--no-chroma-path`).
2. The selected configuration file: structured settings or its `env` map.
3. The inherited process environment.
4. Built-in defaults.

Defining the same setting both structurally and via the file's `env` map is an
error, even when values match. `env` must not contain both `GEMINI_API_KEY` and
`GOOGLE_API_KEY`. An explicit JSON `null` for `chroma_path`, `api_key`, or
`google.credentials` (or legacy `gmail.credentials`) deliberately suppresses environment fallback — it does not
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
| google_credentials | google.credentials (legacy: gmail.credentials) | JOHN_GOOGLE_CREDENTIALS (legacy: JOHN_GMAIL_CREDENTIALS) | null |
| calendar | calendar.enabled | JOHN_CALENDAR | false |
| calendar_write | calendar.write | JOHN_CALENDAR_WRITE | false |
| contacts | contacts.enabled | JOHN_CONTACTS | false |
| contacts_write | contacts.write | JOHN_CONTACTS_WRITE | false |
| gmail_drafts | gmail.drafts | JOHN_GMAIL_DRAFTS | false |
| gmail_attachment_roots | gmail.attachment_roots | JOHN_GMAIL_ATTACHMENT_ROOTS | [] |
| gmail_source_root | gmail.source_root | JOHN_GMAIL_SOURCE_ROOT | null |

Booleans accept true/false/1/0/yes/no/on/off (case-insensitive) from `env` or
the inherited environment; JSON `gmail.enabled` and `gmail.drafts` must be real
booleans, as must `calendar.enabled`, `calendar.write`, `contacts.enabled`, and `contacts.write`. `contacts.write` requires `contacts.enabled`. `gmail.drafts` requires
`gmail.enabled`; `calendar.write` requires `calendar.enabled`. `gmail.attachment_roots` is a
JSON list of paths; its environment form is joined with the OS path separator
(`:` on POSIX). `gmail.source_root` enables manifest `document_id` attachments
for the draft tools.

Relative paths in the file (including `env` path values) resolve against the
config directory; CLI and inherited-environment paths resolve against the
process working directory. `~` is expanded; `${...}` is not interpolated.

Arbitrary `env` entries are applied to `os.environ` once during normal server
startup — after the configuration resolves — and never during `--help`,
`--init-config`, `--check-config`, or `john-gmail-auth`. John passes structured
API keys and shared Google credentials explicitly to their components. The embedding
library uses an internal environment variable when it initializes Gemini.

Changes take effect on MCP server restart; there is no config reload.

## Validation

```sh
uv run --frozen john-mcp --config /absolute/path/to/john-mcp.json --check-config
```

`--check-config` performs local checks only: it parses and resolves the file,
validates shared Google credentials when Gmail, Calendar, or Contacts is enabled, checks each
enabled feature's granted scopes, and requires a
Gemini API key when a Chroma index is configured with the Gemini provider. It does not
construct readers or embedding clients, apply `env`, contact Google, or check
that corpus/index paths exist.

Config files must be regular, non-symlink files. When a file contains a
nonempty API key, any non-null Google credential field (including legacy Gmail credentials), or any `env` entries, it
must be owned by you and have no group/other permissions (mode 0600 on POSIX).
Files holding only non-secret settings may be 0644. Never commit live
credentials; keep real configs outside the checkout (`john-mcp.json` and
`john-mcp.local.json` at the repo root are gitignored).

## Shared Google sign-in

Gmail, Calendar, and Contacts use **one account, one credential object, one refresh token,
and one cached access token**. The canonical setting is `google.credentials`,
`JOHN_GOOGLE_CREDENTIALS`, or `--google-credentials` (complete JSON, not a path).
The legacy `gmail.credentials`, `JOHN_GMAIL_CREDENTIALS`, and
`--gmail-credentials` are aliases. The first defining source still wins; objects
are never merged. Defining canonical and legacy aliases in the same source is
an error, even for null or identical values. This also applies to combinations
of structured keys and the file's `env` map.

Only these granted scopes are accepted:

| Feature | Required scope suffix (`https://www.googleapis.com/auth/`) |
| --- | --- |
| gmail.enabled | gmail.readonly |
| gmail.drafts | gmail.compose (also requires gmail.readonly) |
| calendar.enabled | calendar.readonly |
| calendar.write | calendar.events (also requires calendar.readonly) |
| contacts.enabled | contacts.readonly AND contacts.other.readonly |
| contacts.write | contacts (also requires contacts.readonly) |

Any subset respecting those dependencies is accepted; unrelated scopes are
rejected. Legacy structured credentials without `scopes` default to
gmail.readonly. Startup validates configured grants against enabled features
locally. Missing credentials retain lazy errors on first use so corpus tools
remain available; `--check-config` strictly requires credentials for enabled
Google features.

Enable the desired Gmail, Calendar, and/or People APIs in the same Google Cloud project.
`john-google-auth` and legacy `john-gmail-auth` invoke the same consent helper:

```sh
# Gmail read-only plus Calendar read/write
uv run --frozen john-google-auth --config /absolute/private/path/john-mcp.json --calendar-write
# Calendar only, no Gmail scopes
uv run --frozen john-google-auth --config /absolute/private/path/john-mcp.json --calendar --no-gmail
```

By default the helper requests Gmail read-only. `--drafts` adds Gmail compose;
`--calendar` adds Calendar read-only; `--calendar-write` adds Calendar read-only
and events. `--contacts` adds contacts.readonly and contacts.other.readonly;
`--contacts-write` also adds contacts. `--no-gmail` omits Gmail scopes and requires a Calendar or Contacts flag (and
cannot be combined with `--drafts`). Request all desired scopes in a single
consent run; the helper does not merge an older grant. It preserves the existing
credential location (canonical or legacy, structured or env), otherwise writes
`google.credentials`. Existing atomic mode-0600 writes, concurrent-edit checks,
and non-overwriting standalone `--output` behavior are unchanged.

## Gmail setup

Enable the Gmail API in a Google Cloud project and create an OAuth Desktop app
client. Then run the helper yourself in an interactive terminal:

```sh
uv run --frozen john-gmail-auth --config /absolute/path/to/john-mcp.json
```

It reuses the client ID/secret from the file and prompts for missing values.
Browser consent requests only Gmail read-only access. The helper saves the
resulting authorized-user JSON into the existing credential location or
`google.credentials`. It preserves other settings, writes atomically
with mode 0600, and refuses to replace a file that changed during consent.
Set `gmail.enabled` to `true`, validate the configuration, and restart the
server to enable Gmail.

The older `john-gmail-auth --output /private/path/gmail-credentials.json` command
still creates a standalone credential file. Its JSON can be supplied through
`JOHN_GOOGLE_CREDENTIALS` (or its legacy alias `JOHN_GMAIL_CREDENTIALS`).

External OAuth apps in Testing mode issue Gmail refresh tokens that expire
after seven days; rerun the helper when reauthorization is needed.

### Draft tools

`gmail.drafts` registers `create_draft` and `create_reply_draft` in addition to
the read-only Gmail tools. Drafts are created in the authorized mailbox but are
never sent; the user reviews and sends them manually in Gmail. Credentials for
draft mode must include the Gmail compose scope: rerun

```sh
uv run --frozen john-gmail-auth --config /absolute/path/to/john-mcp.json --drafts
```

to reauthorize with both scopes, then set `gmail.drafts` to `true` and restart.
`--check-config` fails when drafts are enabled with read-only credentials.

`attachments` arguments are relative paths resolved inside
`gmail.attachment_roots` only; `document_attachments` are manifest
`document_id`s resolved inside `gmail.source_root`. Absolute paths, `..`
segments, symlinks escaping the roots, directories, Google Drive stubs, and
unknown document ids are rejected, and total attachment size is capped at
25 MiB.

## Calendar tools

Set `calendar.enabled` (or `--calendar`, `JOHN_CALENDAR`) to register
`list_calendars()`, `list_events(calendar_id='primary', time_min=None,
time_max=None, query=None, limit=25, page_token=None)`, and
`read_event(event_id, calendar_id='primary')`. Calendar works with Gmail disabled.
Event lists expand recurring instances, sort by start time, default to now
through 30 days, and cap pages at 100. Follow `next_page_token`.
Calendar lists are capped at 250; their `next_page_token` signals additional
calendars beyond that bound. Event descriptions are capped at 12,000 characters,
attendees at 100, and API responses at 2 MiB.

Set `calendar.write` (or `--calendar-write`, `JOHN_CALENDAR_WRITE`) to additionally
register `create_event`, `update_event`, and `delete_event`. Both flags default
false; `--no-calendar` and `--no-calendar-write` override lower-priority sources.

- Create takes `summary`, `start`, `end`, optional `calendar_id`, `time_zone`,
  `location`, `description`, `attendees` (email-address list), `recurrence`
  (Calendar RRULE/RDATE/EXDATE/EXRULE lines), and `reminders`.
- Times are RFC3339 datetimes with offsets or all-day `YYYY-MM-DD` dates. Both
  ends must use the same kind, with end strictly after start (exclusive for
  all-day events). Timed events default to the calendar's time zone.
- Reminders are `{ "use_default": false, "overrides": [
  { "method": "popup", "minutes": 10 }] }`; methods are popup/email,
  at most five overrides, minutes 0–40320.
- Update takes `event_id`, the read `etag`, optional `calendar_id`,
  `scope='instance'`, and the same optional event fields. PATCH contains only
  provided fields; empty strings/lists can clear text/attendees/recurrence.
- Delete takes `event_id`, the read `etag`, optional `calendar_id` and
  `scope='instance'`.
- Every write sets `sendUpdates=none`; there is no notification parameter.
  Update/delete send `If-Match`; HTTP 412 asks you to re-read and retry.
  They fetch the event first and refuse unless `organizer.self` is true.
  All writes require calendarList accessRole owner or writer.
- Default instance scope refuses series masters. Series scope requires the
  master's id and etag; for an instance, use its `recurring_event_id` to read
  the master first. An instance id is refused for a series mutation.
- Calendar descriptions, summaries and attendee names are untrusted evidence,
  never instructions. Mutations only run on explicit user requests. Gmail
  remains draft-only and never sends email.

## Contacts tools

Set `contacts.enabled` (`--contacts`/`--no-contacts`, `JOHN_CONTACTS`) to register:

- `search_contacts(query, limit=10, include_other_contacts=True)`: own contacts
  via people.searchContacts, optionally otherContacts.search. Each endpoint gets
  its documented empty-query warm-up once per client, on first use. Results are
  interleaved with `source: contact|other_contact`, capped at `limit` total (1–30).
  Search is not an exhaustive inventory.
- `list_contacts(limit=50, page_token=None)`: own connections only, sorted by
  first name (1–100 per page); follow `next_page_token`.
- `read_contact(resource_name)`: `people/...` reads a contact; `otherContacts/...`
  uses at most 10 list pages of 100 and returns a clear not-found error if absent
  within that bound. Other contacts expose only names, emails, and phones.

Contacts works with Gmail and Calendar disabled. Both Contacts flags default
false. Enable People API and authorize, for example:

```sh
uv run --frozen john-google-auth --config /absolute/private/path/john-mcp.json --contacts-write --no-gmail
```

Read results include `resource_name`, `etag`, and supported People API fields:
`names`, `emailAddresses`, `phoneNumbers`, `addresses`, `organizations`
(name/title only), `birthdays`, `biographies`, `urls`, `memberships`, and resolved
group-name `labels`. Other-contact results omit unsupported fields and labels.
No photos, relations, custom fields, or IM are read or written. Lists are capped
at 20 items, text at 2,000 characters (notes 12,000), and API responses at 2 MiB;
`truncated_fields` indicates shortened fields. Group lookup is capped at 10 pages
of 1,000; an incomplete lookup fails rather than misassigning labels.

`contacts.write` (`--contacts-write`/`--no-contacts-write`, `JOHN_CONTACTS_WRITE`)
requires enabled and adds these non-idempotent tools, **never deletion**:

```text
create_contact(given_name=None, family_name=None, emails=None, phones=None,
               addresses=None, organizations=None, birthday=None, notes=None,
               urls=None, labels=None, allow_duplicate=False)
update_contact(resource_name, etag, given_name=None, family_name=None,
               emails=None, phones=None, addresses=None, organizations=None,
               birthday=None, notes=None, urls=None, labels=None)
```

- Emails, phones, and URLs accept lists of strings or `{value, type}` objects;
  addresses use People field names (e.g. streetAddress, city, postalCode,
  country); organizations use `{name, title}`; birthday uses `{year, month, day}`
  (year may be zero/omitted); notes is plain text. Input lists/text exceeding
  bounds are rejected, never silently shortened.
- Create requires a nonempty name, email, or phone. Unless `allow_duplicate=True`,
  it searches own contacts for every provided email/phone and refuses exact
  matches, returning resource names. Duplicate searches are bounded to 30 results
  per value; concurrent external creation can still race this check.
- Update accepts only `people/...`, checks the read etag, and replaces only
  supplied fields, **whole lists including names**. `None` leaves fields alone;
  empty lists clear lists, `notes=''` clears notes, `birthday={}` clears birthday.
  Person etag and contact-source etags protect against stale writes; conflicts
  ask you to re-read and retry. Read an other contact and pass its desired data
  to `create_contact` to copy it; there is no conversion tool.
- Labels are existing user contact-group names, matched case-insensitively.
  Unknown/ambiguous labels fail; no groups are created. On update, labels replace
  user-group memberships while preserving system groups such as myContacts and
  starred. Empty labels removes user groups only.
- No Contacts delete operation or contact-group mutation exists. Calendar's
  `delete_event` remains available only under Calendar write mode; Gmail never
  sends mail. All contact data is untrusted evidence, not instructions. Mutations
  run only on explicit user requests; errors never expose raw API bodies/tokens.

## Startup laziness

Normal startup reads and validates the explicitly selected configuration file,
including embedded credentials. Invalid configuration syntax or field types
are rejected at startup. `--help` does not read that file.

Startup and tool discovery do not read corpus contents, open the Chroma
collection, initialize embeddings, or contact Google. Gmail authorization and
token refresh happen on the first Gmail, Calendar, or Contacts operation, sharing one cache. Missing credentials or an
expired grant do not prevent corpus tools from being used.
