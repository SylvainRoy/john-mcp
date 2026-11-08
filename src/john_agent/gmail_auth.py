"""Interactive OAuth setup helper for the optional Gmail tools."""

from __future__ import annotations

import copy
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from john_agent.config import (
    ConfigError,
    SafeArgumentParser,
    load_config,
    credentials_location,
    update_config,
)
from john_agent.gmail_client import (
    GMAIL_COMPOSE_SCOPE,
    CALENDAR_SCOPE,
    CALENDAR_EVENTS_SCOPE,
    CONTACTS_SCOPE,
    OTHER_CONTACTS_SCOPE,
    CONTACTS_WRITE_SCOPE,
    GMAIL_ENV,
    GMAIL_SCOPE,
    TOKEN_URL,
)

_SUCCESS = (
    'Authorization complete. Load the written JSON into the JOHN_GOOGLE_CREDENTIALS '
    'environment variable through your launcher or secret manager.'
)
_SUCCESS_CONFIG = (
    'Authorization saved to the configuration. Enable the desired Gmail/Calendar/Contacts tools and restart the '
    'MCP server to use it.'
)
_FAILURE = 'Gmail authorization setup failed; check inputs and try again.'
_MAX_FIELD = 16 * 1024
_MAX_JSON = 64 * 1024


def _quiet_library_loggers() -> None:
    for name in ('google_auth_oauthlib', 'google.auth', 'requests', 'urllib3',
                 'requests_oauthlib', 'oauthlib'):
        logging.getLogger(name).setLevel(logging.WARNING)


def _write_credentials(output: Path, payload: dict) -> None:
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created_stat = os.fstat(fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            current = output.lstat()
            if (current.st_dev, current.st_ino) == (created_stat.st_dev, created_stat.st_ino):
                output.unlink()
        except OSError:
            pass
        raise


def _configured_client(config_file, which: str) -> str | None:
    section, key = credentials_location(config_file.data)
    creds = config_file.data.get(section, {}).get(key)
    if isinstance(creds, dict):
        value = creds.get(which)
        if isinstance(value, str) and value:
            return value
    raw = creds if section == 'env' else None
    if isinstance(raw, str):
        try:
            if len(raw.encode('utf-8')) > _MAX_JSON:
                raise ConfigError('Configured Gmail credentials are invalid.')
            if not raw.strip():
                return None
            parsed = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ConfigError('Configured Gmail credentials are invalid.') from None
        if not isinstance(parsed, dict):
            raise ConfigError('Configured Gmail credentials are invalid.')
        value = parsed.get(which)
        if isinstance(value, str) and value:
            return value
    return None


def main() -> None:
    parser = SafeArgumentParser(
        prog=Path(sys.argv[0]).name,
        description='Interactive shared Google OAuth setup for optional Gmail, Calendar and Contacts tools.',
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--config', type=Path)
    parser.add_argument('--drafts', action='store_true',
                        help='Also authorize the Gmail compose scope so the optional '
                             'draft tools can create drafts; drafts are never sent.')
    parser.add_argument('--calendar', action='store_true', help='Authorize Calendar read-only access.')
    parser.add_argument('--calendar-write', action='store_true', help='Authorize Calendar read and event writes without guest notifications.')
    parser.add_argument('--contacts', action='store_true', help='Authorize read-only contacts and other contacts.')
    parser.add_argument('--contacts-write', action='store_true', help='Also authorize contact creation and updates; never deletion.')
    parser.add_argument('--no-gmail', action='store_true', help='Omit Gmail scopes; requires a Calendar or Contacts flag.')
    args = parser.parse_args()

    if args.no_gmail and (not (args.calendar or args.calendar_write or args.contacts or args.contacts_write) or args.drafts):
        parser.error('--no-gmail requires Calendar or Contacts and cannot be used with --drafts.')
    scopes = [] if args.no_gmail else ([GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE] if args.drafts else [GMAIL_SCOPE])
    if args.calendar or args.calendar_write:
        scopes.append(CALENDAR_SCOPE)
    if args.calendar_write:
        scopes.append(CALENDAR_EVENTS_SCOPE)
    if args.contacts or args.contacts_write:
        scopes.extend([CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE])
    if args.contacts_write:
        scopes.append(CONTACTS_WRITE_SCOPE)
    required = set(scopes)

    _quiet_library_loggers()
    output: Path | None = args.output
    config_path: Path | None = args.config

    def fail() -> None:
        print(_FAILURE, file=sys.stderr)
        raise SystemExit(1)

    if not sys.stdin.isatty():
        fail()

    config_file = None
    if config_path is not None:
        try:
            config_file = load_config(config_path)
        except ConfigError:
            fail()
    else:
        parent = output.parent
        if not parent.is_dir():
            fail()
        if os.path.lexists(output):
            fail()

    configured_id = configured_secret = None
    if config_file is not None:
        try:
            configured_id = _configured_client(config_file, 'client_id')
            configured_secret = _configured_client(config_file, 'client_secret')
        except ConfigError:
            fail()

    flow = None
    try:
        client_id = configured_id or getpass.getpass('Google OAuth client ID: ')
        client_secret = configured_secret or getpass.getpass('Google OAuth client secret: ')
        if (
            not client_id or not client_secret
            or len(client_id.encode('utf-8')) > _MAX_FIELD
            or len(client_secret.encode('utf-8')) > _MAX_FIELD
        ):
            fail()

        flow = InstalledAppFlow.from_client_config(
            {'installed': {'client_id': client_id, 'client_secret': client_secret,
                           'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                           'token_uri': TOKEN_URL, 'redirect_uris': ['http://localhost']}},
            scopes=scopes, autogenerate_code_verifier=True,
        )
        credentials = flow.run_local_server(
            host='127.0.0.1', port=0, timeout_seconds=300,
            access_type='offline', prompt='consent', include_granted_scopes='false',
            authorization_prompt_message='',
            success_message='Authorization completed. You can close this window.',
        )
        refresh_token = getattr(credentials, 'refresh_token', None)
        requested = set(getattr(credentials, 'scopes', None) or [])
        granted = set(getattr(credentials, 'granted_scopes', None) or requested)
        if (
            not isinstance(refresh_token, str) or not refresh_token
            or len(refresh_token.encode('utf-8')) > _MAX_FIELD
            or requested != required or granted != required
        ):
            fail()
        payload = {
            'type': 'authorized_user',
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
            'scopes': scopes,
        }
        if len(json.dumps(payload).encode('utf-8')) > _MAX_JSON:
            fail()
        if config_file is not None:
            updated = copy.deepcopy(config_file.data)
            section, key = credentials_location(updated)
            updated.setdefault(section, {})[key] = json.dumps(payload) if section == 'env' else payload
            try:
                update_config(config_file, updated)
            except ConfigError:
                fail()
            try:
                print(_SUCCESS_CONFIG)
            except BrokenPipeError:
                pass
        else:
            _write_credentials(output, payload)
            try:
                print(_SUCCESS)
            except BrokenPipeError:
                pass
    except SystemExit:
        raise
    except BaseException:
        fail()
    finally:
        session = getattr(flow, 'oauth2session', None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
