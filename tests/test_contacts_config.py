import itertools
import json
import sys

import pytest

from john_agent import server
from john_agent.config import ConfigError, check_settings, resolve_settings
from john_agent.gmail_client import (
    CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE, CONTACTS_WRITE_SCOPE,
    GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE,
    GmailError, validate_credentials,
)
from test_google_config import config_file, creds
from test_gmail_auth import FakeCredentials, FakeFlow, run_main


ALL_SCOPES = [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE,
              CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE, CONTACTS_WRITE_SCOPE]
CONTACTS_READ = [CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE]


@pytest.mark.parametrize('mask', list(itertools.product((False, True), repeat=7)))
def test_combined_scope_dependencies(mask):
    scopes = {s for s, included in zip(ALL_SCOPES, mask) if included}
    valid = all(write not in scopes or read in scopes for write, read in (
        (GMAIL_COMPOSE_SCOPE, GMAIL_SCOPE), (CALENDAR_EVENTS_SCOPE, CALENDAR_SCOPE),
        (CONTACTS_WRITE_SCOPE, CONTACTS_SCOPE)))
    if valid:
        assert validate_credentials(creds(scopes))['scopes'] == scopes
    else:
        with pytest.raises(GmailError):
            validate_credentials(creds(scopes))


@pytest.mark.parametrize('scopes', [[], [GMAIL_SCOPE], [CONTACTS_SCOPE], [OTHER_CONTACTS_SCOPE],
    CONTACTS_READ, [CONTACTS_SCOPE, CONTACTS_WRITE_SCOPE], [*CONTACTS_READ, CONTACTS_WRITE_SCOPE], ALL_SCOPES])
@pytest.mark.parametrize('write', [False, True])
def test_contacts_feature_scopes(scopes, write):
    settings = resolve_settings({'markdown_root': '/fake', 'contacts': True,
        'contacts_write': write, 'google_credentials': creds(scopes)}, environ={})
    assert not settings.gmail and not settings.calendar
    required = set(CONTACTS_READ + ([CONTACTS_WRITE_SCOPE] if write else []))
    if required <= set(scopes):
        check_settings(settings)
    else:
        with pytest.raises(ConfigError):
            check_settings(settings)


def test_flags_types_and_precedence(tmp_path):
    settings = resolve_settings({'markdown_root': '/fake'}, environ={})
    assert not settings.contacts and not settings.contacts_write
    settings = resolve_settings({'markdown_root': '/fake'}, environ={
        'JOHN_CONTACTS': 'yes', 'JOHN_CONTACTS_WRITE': 'on'})
    assert settings.contacts and settings.contacts_write
    with pytest.raises(ConfigError, match='requires contacts.enabled'):
        resolve_settings({'markdown_root': '/fake', 'contacts_write': True}, environ={})
    for field in ('enabled', 'write'):
        with pytest.raises(ConfigError, match='boolean'):
            config_file(tmp_path, {'contacts': {field: 'yes'}})
    config = config_file(tmp_path, {'contacts': {'enabled': True, 'write': True}})
    settings = resolve_settings({'contacts': False, 'contacts_write': False}, config, {})
    assert not settings.contacts and not settings.contacts_write
    with pytest.raises(ConfigError):
        config_file(tmp_path, {'contacts': {'enabled': True}, 'env': {'JOHN_CONTACTS': 'true'}})


@pytest.mark.parametrize('flags,scopes', [
    (['--contacts'], [GMAIL_SCOPE, *CONTACTS_READ]),
    (['--contacts-write'], [GMAIL_SCOPE, *CONTACTS_READ, CONTACTS_WRITE_SCOPE]),
    (['--contacts', '--no-gmail'], CONTACTS_READ),
    (['--contacts-write', '--no-gmail'], [*CONTACTS_READ, CONTACTS_WRITE_SCOPE]),
    (['--contacts', '--calendar-write', '--no-gmail'], [CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE, *CONTACTS_READ]),
    (['--contacts-write', '--drafts'], [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, *CONTACTS_READ, CONTACTS_WRITE_SCOPE]),
])
def test_auth_selection(tmp_path, monkeypatch, flags, scopes):
    class Flow(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials(scopes=scopes)
    out = tmp_path / 'synthetic.json'
    assert run_main(monkeypatch, ['john-google-auth', '--output', str(out), *flags], tmp_path, flow=Flow) == 0
    assert Flow.last_scopes == scopes
    assert json.loads(out.read_text())['scopes'] == scopes


def test_no_gmail_drafts_conflict(tmp_path, monkeypatch):
    assert run_main(monkeypatch, ['john-google-auth', '--output', str(tmp_path / 'out'),
                                 '--contacts', '--no-gmail', '--drafts'], tmp_path) != 0


@pytest.mark.parametrize('mode', ['startup', 'check', 'cli', 'disabled'])
def test_contacts_startup_and_cli(tmp_path, monkeypatch, capsys, mode):
    config = config_file(tmp_path, {'contacts': {'enabled': True, 'write': True},
        'google': {'credentials': creds([*CONTACTS_READ, CONTACTS_WRITE_SCOPE])}})
    captured = {}
    class FakeServer:
        def run(self):
            pass
    def capture(reader, **kwargs):
        captured.update(kwargs)
        return FakeServer()
    monkeypatch.setattr(server, 'create_server', capture)
    extra = {'startup': [], 'check': ['--check-config'],
             'cli': ['--contacts', '--contacts-write'],
             'disabled': ['--no-contacts', '--no-contacts-write']}[mode]
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(config.path), *extra])
    server.main()
    if mode == 'check':
        assert 'Configuration is valid' in capsys.readouterr().out
        assert not captured
    elif mode == 'disabled':
        assert captured['contacts_client'] is None and not captured['contacts_write']
    else:
        assert captured['contacts_write']
        assert captured['contacts_client'].tokens is captured['google_client'].tokens
        assert captured['gmail_reader'] is None and captured['calendar_client'] is None


@pytest.mark.parametrize('check', [False, True])
def test_scope_startup_rejection(tmp_path, monkeypatch, capsys, check):
    config = config_file(tmp_path, {'contacts': {'enabled': True},
        'google': {'credentials': creds([CONTACTS_SCOPE])}})
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(config.path),
                                     *(['--check-config'] if check else [])])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert 'contacts.other.readonly' in capsys.readouterr().err
