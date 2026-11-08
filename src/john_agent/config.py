"""Single-file JSON configuration for the john-mcp server."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from john_agent.gmail_client import (
    ALLOWED_SCOPE_SETS,
    CALENDAR_SCOPE,
    CALENDAR_EVENTS_SCOPE,
    CONTACTS_SCOPE,
    OTHER_CONTACTS_SCOPE,
    CONTACTS_WRITE_SCOPE,
    GOOGLE_ENV,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_ENV,
    GMAIL_SCOPE,
    GmailError,
    validate_credentials,
)

_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_CRED_FIELD = 16 * 1024
_ENV_NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

_MISSING = object()
_SPECS = {
    'markdown_root': ('corpus', 'markdown_root', ('JOHN_MARKDOWN_ROOT',), _MISSING),
    'chroma_path': ('corpus', 'chroma_path', ('JOHN_CHROMA_PATH',), None),
    'collection': ('corpus', 'collection', ('JOHN_COLLECTION',), 'documents'),
    'embedding': ('embedding', 'provider', ('JOHN_EMBEDDING',), 'gemini'),
    'gemini_model': ('embedding', 'model', ('JOHN_GEMINI_MODEL',), 'gemini-embedding-001'),
    'gemini_api_key': ('embedding', 'api_key', ('GEMINI_API_KEY', 'GOOGLE_API_KEY'), None),
    'gmail': ('gmail', 'enabled', ('JOHN_GMAIL',), False),
    'gmail_credentials': ('google', 'credentials', (GOOGLE_ENV, GMAIL_ENV), None),
    'calendar': ('calendar', 'enabled', ('JOHN_CALENDAR',), False),
    'calendar_write': ('calendar', 'write', ('JOHN_CALENDAR_WRITE',), False),
    'contacts': ('contacts', 'enabled', ('JOHN_CONTACTS',), False),
    'contacts_write': ('contacts', 'write', ('JOHN_CONTACTS_WRITE',), False),
    'gmail_drafts': ('gmail', 'drafts', ('JOHN_GMAIL_DRAFTS',), False),
    'gmail_attachment_roots': ('gmail', 'attachment_roots',
                               ('JOHN_GMAIL_ATTACHMENT_ROOTS',), []),
    'gmail_source_root': ('gmail', 'source_root', ('JOHN_GMAIL_SOURCE_ROOT',), None),
}

CONFIG_TEMPLATE = {
    'version': 1,
    'corpus': {'markdown_root': '/absolute/path/to/markdown', 'chroma_path': None,
               'collection': 'documents'},
    'embedding': {'provider': 'gemini', 'model': 'gemini-embedding-001', 'api_key': None},
    'gmail': {'enabled': False, 'drafts': False, 'attachment_roots': [],
              'source_root': None},
    'calendar': {'enabled': False, 'write': False},
    'contacts': {'enabled': False, 'write': False},
    'google': {'credentials': {
        'client_id': None, 'client_secret': None, 'refresh_token': None}},
    'env': {},
}

_SECTION_KEYS = {
    'google': {'credentials'},
    'calendar': {'enabled', 'write'},
    'contacts': {'enabled', 'write'},
    'corpus': {'markdown_root', 'chroma_path', 'collection'},
    'embedding': {'provider', 'model', 'api_key'},
    'gmail': {'enabled', 'credentials', 'drafts', 'attachment_roots', 'source_root'},
}
_CRED_FIELDS = ('client_id', 'client_secret', 'refresh_token')
_CRED_ALLOWED = set(_CRED_FIELDS) | {'type', 'scopes'}
_TOP_KEYS = {'version', 'corpus', 'embedding', 'gmail', 'google', 'calendar', 'contacts', 'env'}
_BOOL_TRUE = {'true', '1', 'yes', 'on'}
_BOOL_FALSE = {'false', '0', 'no', 'off'}


class ConfigError(ValueError):
    """A configuration error safe to display without values."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        super().error('Invalid command-line arguments; use --help for options.')


@dataclass(frozen=True)
class ConfigFile:
    path: Path
    data: dict[str, Any] = field(repr=False)
    content: bytes = field(repr=False)
    file_stat: os.stat_result = field(repr=False)


@dataclass(frozen=True)
class Settings:
    markdown_root: Path
    chroma_path: Path | None
    collection: str
    embedding: str
    gemini_model: str
    gemini_api_key: str | None = field(repr=False)
    gmail: bool
    gmail_credentials: dict[str, Any] | str | None = field(repr=False)
    env: dict[str, str] = field(repr=False)
    gmail_drafts: bool = False
    gmail_attachment_roots: list[Path] = field(default_factory=list)
    gmail_source_root: Path | None = None
    calendar: bool = False
    calendar_write: bool = False
    contacts: bool = False
    contacts_write: bool = False

    @property
    def google_credentials(self):
        return self.gmail_credentials


def _reject_constant(value: str) -> None:
    raise ConfigError('Configuration contains unsupported numeric constants.')


def _reject_duplicates(pairs: list) -> dict:
    seen = set()
    out = {}
    for key, value in pairs:
        if key in seen:
            raise ConfigError('Configuration contains a duplicated key.')
        seen.add(key)
        out[key] = value
    return out


def _nonempty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ConfigError(f'{name} must be a nonempty string without NUL.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ConfigError(f'{name} must contain valid Unicode.') from None
    return value


def _validate_credentials_schema(creds: Any) -> None:
    if creds is None:
        return
    if not isinstance(creds, dict):
        raise ConfigError('gmail.credentials must be an object or null.')
    if not set(creds) <= _CRED_ALLOWED:
        raise ConfigError('gmail.credentials contains unsupported keys.')
    for name in _CRED_FIELDS:
        value = creds.get(name)
        if value is not None:
            _nonempty_string(f'gmail.credentials.{name}', value)
            if len(value.encode('utf-8')) > _MAX_CRED_FIELD:
                raise ConfigError('gmail.credentials fields exceed the supported size.')
    if 'type' in creds and creds['type'] != 'authorized_user':
        raise ConfigError('gmail.credentials type must be authorized_user.')
    if 'scopes' in creds:
        scopes = creds['scopes']
        if not isinstance(scopes, list) \
                or not all(isinstance(s, str) for s in scopes) \
                or frozenset(scopes) not in ALLOWED_SCOPE_SETS:
            raise ConfigError(
                'Google credentials scopes must use supported Gmail/Calendar/Contacts scopes; '
                'compose/events/contacts write require their respective read-only scopes.')


def credentials_location(data):
    """Return the unique credential location, preserving legacy config layouts."""
    locations = [(section, 'credentials') for section in ('google', 'gmail')
                 if 'credentials' in (data.get(section) or {})]
    locations += [('env', key) for key in (GOOGLE_ENV, GMAIL_ENV)
                  if key in (data.get('env') or {})]
    if len(locations) > 1:
        raise ConfigError('Google credentials are defined more than once in the same source.')
    return locations[0] if locations else ('google', 'credentials')


def _validate_schema(data: dict[str, Any]) -> None:
    if not set(data) <= _TOP_KEYS:
        raise ConfigError('Configuration contains unsupported keys.')
    version = data.get('version', 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigError('Configuration version must be integer 1.')
    for section, allowed in _SECTION_KEYS.items():
        if section in data and not isinstance(data[section], dict):
            raise ConfigError('Configuration sections must be objects.')
        body = data.get(section)
        if isinstance(body, dict) and not set(body) <= allowed:
            raise ConfigError('Configuration section contains unsupported keys.')
    corpus = data.get('corpus') or {}
    if 'markdown_root' in corpus:
        _nonempty_string('corpus.markdown_root', corpus['markdown_root'])
    if 'chroma_path' in corpus and corpus['chroma_path'] is not None:
        _nonempty_string('corpus.chroma_path', corpus['chroma_path'])
    if 'collection' in corpus:
        _nonempty_string('corpus.collection', corpus['collection'])
    embedding = data.get('embedding') or {}
    if 'provider' in embedding and embedding['provider'] not in ('gemini', 'default'):
        raise ConfigError('embedding.provider must be gemini or default.')
    if 'model' in embedding:
        _nonempty_string('embedding.model', embedding['model'])
    if 'api_key' in embedding and embedding['api_key'] is not None:
        _nonempty_string('embedding.api_key', embedding['api_key'])
    gmail = data.get('gmail') or {}
    if 'enabled' in gmail and not isinstance(gmail['enabled'], bool):
        raise ConfigError('gmail.enabled must be a boolean.')
    if 'drafts' in gmail and not isinstance(gmail['drafts'], bool):
        raise ConfigError('gmail.drafts must be a boolean.')
    if 'attachment_roots' in gmail and gmail['attachment_roots'] is not None:
        roots = gmail['attachment_roots']
        if not isinstance(roots, list):
            raise ConfigError('gmail.attachment_roots must be a list of paths or null.')
        for root in roots:
            _nonempty_string('gmail.attachment_roots', root)
    if 'source_root' in gmail and gmail['source_root'] is not None:
        _nonempty_string('gmail.source_root', gmail['source_root'])
    if 'credentials' in gmail:
        _validate_credentials_schema(gmail['credentials'])
    google = data.get('google') or {}
    if 'credentials' in google:
        _validate_credentials_schema(google['credentials'])
    for section in ('calendar', 'contacts'):
        for key, value in (data.get(section) or {}).items():
            if not isinstance(value, bool):
                raise ConfigError(f'{section}.{key} must be a boolean.')
    if 'env' in data and not isinstance(data['env'], dict):
        raise ConfigError('env must be an object mapping names to strings.')
    env = data.get('env') or {}
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name) \
                or '\x00' in name:
            raise ConfigError('env contains an invalid variable name.')
        if not isinstance(value, str) or '\x00' in value:
            raise ConfigError('env values must be strings without NUL.')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ConfigError('env values must contain valid Unicode.') from None
    for name, (section, key, aliases, _default) in _SPECS.items():
        structural = key in (data.get(section) or {})
        aliased = any(alias in env for alias in aliases)
        if structural and aliased:
            raise ConfigError(
                'A setting is defined both structurally and in env; keep one source.')
    if 'GEMINI_API_KEY' in env and 'GOOGLE_API_KEY' in env:
        raise ConfigError('env must not define both Gemini API key aliases.')
    credentials_location(data)


def _has_secret_content(data: dict[str, Any]) -> bool:
    embedding = data.get('embedding') or {}
    if isinstance(embedding.get('api_key'), str) and embedding['api_key']:
        return True
    for section in ('gmail', 'google'):
        creds = (data.get(section) or {}).get('credentials')
        if isinstance(creds, dict) and any(v is not None for v in creds.values()):
            return True
    env = data.get('env') or {}
    return bool(env)


def _check_permissions(path: Path, file_stat: os.stat_result, data: dict[str, Any]) -> None:
    if os.name != 'posix':
        return
    if not _has_secret_content(data):
        return
    if file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077:
        raise ConfigError(
            'Configuration contains secrets and must be owned by you with mode 0600.')


def load_config(path: Path) -> ConfigFile:
    path = Path(os.path.abspath(Path(path).expanduser()))
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_NONBLOCK'):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ConfigError('Configuration file cannot be opened for reading.') from None
    try:
        try:
            file_stat = os.fstat(fd)
            link_stat = os.lstat(path)
        except OSError:
            raise ConfigError('Configuration path cannot be inspected.') from None
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfigError('Configuration path is not a regular file.')
        if stat.S_ISLNK(link_stat.st_mode) or (
            link_stat.st_dev, link_stat.st_ino
        ) != (file_stat.st_dev, file_stat.st_ino):
            raise ConfigError('Configuration path must not be a symbolic link.')
        chunks = []
        total = 0
        try:
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_CONFIG_BYTES:
                    raise ConfigError('Configuration file exceeds the supported size limit.')
                chunks.append(chunk)
        except OSError:
            raise ConfigError('Configuration file cannot be read.') from None
        content = b''.join(chunks)
    finally:
        os.close(fd)
    try:
        data = json.loads(
            content, parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicates)
    except ConfigError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise ConfigError('Configuration file is not valid JSON.') from None
    if not isinstance(data, dict):
        raise ConfigError('Configuration root must be an object.')
    _validate_schema(data)
    _check_permissions(path, file_stat, data)
    return ConfigFile(path=path, data=data, content=content, file_stat=file_stat)


def _to_path(name: str, value: Any, from_file: bool, config_dir: Path | None) -> Path:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ConfigError(f'{name} must be a nonempty path string.')
    raw = Path(os.path.expanduser(value))
    if raw.is_absolute():
        return raw
    if from_file and config_dir is not None:
        return Path(os.path.abspath(config_dir / raw))
    return Path(os.path.abspath(raw))


def _to_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
    raise ConfigError(f'{name} must be a boolean.')


def _normalize_credentials(value: Any) -> dict[str, Any] | str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        declared = value.get('scopes')
        scopes = sorted(declared) if isinstance(declared, list) else [GMAIL_SCOPE]
        return {
            'type': 'authorized_user',
            'scopes': scopes,
            'client_id': value.get('client_id'),
            'client_secret': value.get('client_secret'),
            'refresh_token': value.get('refresh_token'),
        }
    raise ConfigError('gmail.credentials must be an object, string, or null.')


def _to_path_list(name: str, value: Any, from_file: bool,
                  config_dir: Path | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, str):
        if '\x00' in value:
            raise ConfigError(f'{name} must not contain NUL.')
        value = [item for item in value.split(os.pathsep) if item]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f'{name} must be a path list or a {os.pathsep}-joined string.')
    return [_to_path(name, item, from_file, config_dir) for item in value]


def resolve_settings(overrides: Mapping[str, Any], config: ConfigFile | None = None,
                     environ: Mapping[str, str] | None = None) -> Settings:
    if environ is None:
        environ = os.environ
    file_data = config.data if config is not None else {}
    overrides = dict(overrides)
    if 'google_credentials' in overrides:
        if 'gmail_credentials' in overrides:
            raise ConfigError('Google credentials are defined more than once in CLI options.')
        overrides['gmail_credentials'] = overrides.pop('google_credentials')
    if GOOGLE_ENV in environ and GMAIL_ENV in environ:
        raise ConfigError('Google credential environment aliases conflict.')
    cred_section, cred_key = credentials_location(file_data)
    file_env = file_data.get('env') or {}
    config_dir = config.path.parent if config is not None else None
    selected: dict[str, tuple[Any, bool]] = {}
    for name, (section, key, aliases, default) in _SPECS.items():
        if name == 'gmail_credentials':
            section, key = cred_section, cred_key
        if name in overrides:
            value, from_file = overrides[name], False
        elif key in file_data.get(section, {}):
            value, from_file = file_data[section][key], True
        elif any(alias in file_env for alias in aliases):
            value, from_file = next(file_env[a] for a in aliases if a in file_env), True
        elif name == 'gemini_api_key' and any(alias in environ for alias in aliases):
            value, from_file = next(
                (environ[a] for a in aliases if a in environ and environ[a]), None), False
        elif any(alias in environ for alias in aliases):
            value, from_file = next(environ[a] for a in aliases if a in environ), False
        else:
            value, from_file = default, False
        selected[name] = (value, from_file)

    def get(name):
        return selected[name]

    markdown_root = get('markdown_root')
    if markdown_root[0] is _MISSING:
        raise ConfigError('markdown_root is required via CLI, configuration, or environment.')
    gemini_api_key = get('gemini_api_key')[0]
    if gemini_api_key is not None:
        if not isinstance(gemini_api_key, str) or '\x00' in gemini_api_key:
            raise ConfigError('gemini_api_key must be a string or null.')
        if not gemini_api_key:
            gemini_api_key = None
    embedding = _nonempty_string('embedding', get('embedding')[0])
    if embedding not in ('gemini', 'default'):
        raise ConfigError('embedding must be gemini or default.')
    chroma_raw = get('chroma_path')
    gmail = _to_bool('gmail', get('gmail')[0])
    gmail_drafts = _to_bool('gmail_drafts', get('gmail_drafts')[0])
    if gmail_drafts and not gmail:
        raise ConfigError('gmail.drafts requires gmail.enabled.')
    calendar = _to_bool('calendar', get('calendar')[0])
    calendar_write = _to_bool('calendar_write', get('calendar_write')[0])
    if calendar_write and not calendar:
        raise ConfigError('calendar.write requires calendar.enabled.')
    contacts = _to_bool('contacts', get('contacts')[0])
    contacts_write = _to_bool('contacts_write', get('contacts_write')[0])
    if contacts_write and not contacts:
        raise ConfigError('contacts.write requires contacts.enabled.')
    roots_raw = get('gmail_attachment_roots')
    source_raw = get('gmail_source_root')
    return Settings(
        markdown_root=_to_path('markdown_root', markdown_root[0], markdown_root[1], config_dir),
        chroma_path=None if chroma_raw[0] is None else _to_path(
            'chroma_path', chroma_raw[0], chroma_raw[1], config_dir),
        collection=_nonempty_string('collection', get('collection')[0]),
        embedding=embedding,
        gemini_model=_nonempty_string('gemini_model', get('gemini_model')[0]),
        gemini_api_key=gemini_api_key,
        gmail=gmail,
        calendar=calendar,
        calendar_write=calendar_write,
        contacts=contacts,
        contacts_write=contacts_write,
        gmail_credentials=_normalize_credentials(get('gmail_credentials')[0]),
        gmail_drafts=gmail_drafts,
        gmail_attachment_roots=_to_path_list(
            'gmail_attachment_roots', roots_raw[0], roots_raw[1], config_dir),
        gmail_source_root=None if source_raw[0] is None else _to_path(
            'gmail_source_root', source_raw[0], source_raw[1], config_dir),
        env=dict(file_env),
    )


def check_settings(settings: Settings) -> None:
    if settings.chroma_path is not None and settings.embedding == 'gemini' \
            and not settings.gemini_api_key:
        raise ConfigError('A Gemini API key is required when Chroma is configured.')
    check_google_settings(settings)


def check_google_settings(settings: Settings) -> None:
    if settings.gmail or settings.calendar or settings.contacts:
        try:
            credentials = validate_credentials(settings.gmail_credentials)
        except GmailError as exc:
            raise ConfigError(str(exc)) from None
        if settings.gmail and GMAIL_SCOPE not in credentials['scopes']:
            raise ConfigError('gmail.enabled requires gmail.readonly scope.')
        if settings.calendar and CALENDAR_SCOPE not in credentials['scopes']:
            raise ConfigError('calendar.enabled requires calendar.readonly scope.')
        if settings.calendar_write and CALENDAR_EVENTS_SCOPE not in credentials['scopes']:
            raise ConfigError('calendar.write requires calendar.events scope.')
        if settings.contacts and not {CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE} <= credentials['scopes']:
            raise ConfigError('contacts.enabled requires contacts.readonly and contacts.other.readonly scopes.')
        if settings.contacts_write and CONTACTS_WRITE_SCOPE not in credentials['scopes']:
            raise ConfigError('contacts.write requires contacts scope.')
        if settings.gmail_drafts and GMAIL_COMPOSE_SCOPE not in credentials['scopes']:
            raise ConfigError(
                'gmail.drafts requires credentials authorized with the Gmail '
                'compose scope; rerun john-gmail-auth --drafts.')


def init_config(path: Path) -> None:
    path = Path(os.path.abspath(Path(path).expanduser()))
    serialized = (json.dumps(CONFIG_TEMPLATE, indent=2) + '\n').encode('utf-8')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise ConfigError('Cannot create a new configuration file at that path.') from None
    created_stat = os.fstat(fd)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (created_stat.st_dev, created_stat.st_ino):
                path.unlink()
        except OSError:
            pass
        raise ConfigError('Configuration file could not be written.') from None


def update_config(original: ConfigFile, data: dict[str, Any]) -> None:
    _validate_schema(data)
    serialized = (json.dumps(data, indent=2) + '\n').encode('utf-8')
    if len(serialized) > _MAX_CONFIG_BYTES:
        raise ConfigError('Updated configuration exceeds the supported size limit.')
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=original.path.parent, prefix='.john-mcp-', suffix='.tmp')
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fd = None
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            current = load_config(original.path)
        except ConfigError:
            raise ConfigError('Configuration file changed during update.') from None
        expected = original.file_stat
        current_stat = current.file_stat
        if (
            current_stat.st_dev, current_stat.st_ino, current_stat.st_size,
            current_stat.st_mtime_ns,
        ) != (
            expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns,
        ) or current.content != original.content:
            raise ConfigError('Configuration file changed during update.')
        os.replace(tmp_name, original.path)
        tmp_name = None
    except ConfigError:
        raise
    except OSError:
        raise ConfigError('Configuration file could not be updated.') from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
