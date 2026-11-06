import itertools
import json
import stat
import sys

import pytest

from john_agent import gmail_auth
from john_agent.config import ConfigError, check_settings, load_config, resolve_settings
from john_agent.gmail_client import (
    GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE,
    validate_credentials, GmailError,
)
from test_gmail_auth import FakeCredentials, FakeFlow, run_main


def creds(scopes):
    return {'type': 'authorized_user', 'client_id': 'fake-id', 'client_secret': 'fake-secret',
            'refresh_token': 'fake-refresh', 'scopes': list(scopes)}


def config_file(tmp_path, data):
    path = tmp_path / 'synthetic.json'
    path.write_text(json.dumps({'corpus': {'markdown_root': '/fake/md'}, **data}))
    path.chmod(0o600)
    return load_config(path)


ALL_SCOPES = [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE]
SCOPE_SETS = [set(s for s, included in zip(ALL_SCOPES, mask) if included)
              for mask in itertools.product((False, True), repeat=4)]


@pytest.mark.parametrize('scopes', SCOPE_SETS)
def test_scope_validation(scopes):
    valid = (GMAIL_COMPOSE_SCOPE not in scopes or GMAIL_SCOPE in scopes) and (
        CALENDAR_EVENTS_SCOPE not in scopes or CALENDAR_SCOPE in scopes)
    if valid:
        assert validate_credentials(creds(scopes))['scopes'] == scopes
    else:
        with pytest.raises(GmailError):
            validate_credentials(creds(scopes))


@pytest.mark.parametrize('scopes', [set(), {GMAIL_SCOPE}, {GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE},
    {CALENDAR_SCOPE}, {CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE}, set(ALL_SCOPES)])
@pytest.mark.parametrize('features,required', [
    ({'gmail': True}, {GMAIL_SCOPE}),
    ({'gmail': True, 'gmail_drafts': True}, {GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE}),
    ({'calendar': True}, {CALENDAR_SCOPE}),
    ({'calendar': True, 'calendar_write': True}, {CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE}),
    ({'calendar': True, 'gmail': True}, {GMAIL_SCOPE, CALENDAR_SCOPE}),
])
def test_feature_matrix(scopes, features, required):
    settings = resolve_settings({'markdown_root': '/fake/md', 'google_credentials': creds(scopes), **features}, environ={})
    if scopes >= required:
        check_settings(settings)
    else:
        with pytest.raises(ConfigError):
            check_settings(settings)


@pytest.mark.parametrize('section', ['google', 'gmail'])
@pytest.mark.parametrize('env_alias', ['JOHN_GOOGLE_CREDENTIALS', 'JOHN_GMAIL_CREDENTIALS'])
def test_alias_precedence(tmp_path, section, env_alias):
    file_creds = creds([CALENDAR_SCOPE])
    config = config_file(tmp_path, {section: {'credentials': file_creds}, 'calendar': {'enabled': True}})
    settings = resolve_settings({}, config, {env_alias: json.dumps(creds([GMAIL_SCOPE]))})
    assert settings.google_credentials['scopes'] == [CALENDAR_SCOPE]
    assert not settings.gmail and settings.calendar
    check_settings(settings)
    for key in ('google_credentials', 'gmail_credentials'):
        settings = resolve_settings({key: creds(ALL_SCOPES)}, config, {})
        assert set(settings.google_credentials['scopes']) == set(ALL_SCOPES)
    config = config_file(tmp_path, {section: {'credentials': None}})
    assert resolve_settings({}, config, {env_alias: json.dumps(file_creds)}).google_credentials is None


@pytest.mark.parametrize('data', [
    {'google': {'credentials': None}, 'gmail': {'credentials': None}},
    {'google': {'credentials': None}, 'env': {'JOHN_GMAIL_CREDENTIALS': ''}},
    {'gmail': {'credentials': None}, 'env': {'JOHN_GOOGLE_CREDENTIALS': ''}},
    {'env': {'JOHN_GOOGLE_CREDENTIALS': '', 'JOHN_GMAIL_CREDENTIALS': ''}},
])
def test_file_credential_alias_conflicts(tmp_path, data):
    with pytest.raises(ConfigError):
        config_file(tmp_path, data)


def test_cli_and_environment_alias_conflicts():
    with pytest.raises(ConfigError):
        resolve_settings({'markdown_root': '/fake', 'google_credentials': None, 'gmail_credentials': None}, environ={})
    with pytest.raises(ConfigError):
        resolve_settings({'markdown_root': '/fake'}, environ={'JOHN_GOOGLE_CREDENTIALS': '', 'JOHN_GMAIL_CREDENTIALS': ''})


def test_calendar_flags_and_types(tmp_path):
    settings = resolve_settings({'markdown_root': '/fake'}, environ={'JOHN_CALENDAR': 'yes', 'JOHN_CALENDAR_WRITE': 'on'})
    assert settings.calendar and settings.calendar_write
    with pytest.raises(ConfigError, match='requires calendar.enabled'):
        resolve_settings({'markdown_root': '/fake', 'calendar_write': True}, environ={})
    with pytest.raises(ConfigError, match='boolean'):
        config_file(tmp_path, {'calendar': {'enabled': 'yes'}})
    config = config_file(tmp_path, {'calendar': {'enabled': True, 'write': True}})
    settings = resolve_settings({'calendar': False, 'calendar_write': False}, config, {})
    assert not settings.calendar and not settings.calendar_write


@pytest.mark.parametrize('flags,scopes', [
    (['--calendar'], [GMAIL_SCOPE, CALENDAR_SCOPE]),
    (['--calendar-write'], [GMAIL_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE]),
    (['--calendar', '--drafts'], [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_SCOPE]),
    (['--calendar', '--no-gmail'], [CALENDAR_SCOPE]),
    (['--calendar-write', '--no-gmail'], [CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE]),
])
def test_helper_scope_selection(tmp_path, monkeypatch, flags, scopes):
    class Flow(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials(scopes=scopes)
    out = tmp_path / 'synthetic.json'
    code = run_main(monkeypatch, ['john-google-auth', '--output', str(out), *flags], tmp_path, flow=Flow)
    assert code == 0
    assert Flow.last_scopes == scopes
    assert json.loads(out.read_text())['scopes'] == scopes


def test_no_gmail_requires_calendar(tmp_path, monkeypatch):
    assert run_main(monkeypatch, ['john-google-auth', '--output', str(tmp_path / 'out'), '--no-gmail'], tmp_path) != 0


@pytest.mark.parametrize('location', [('google', 'credentials'), ('gmail', 'credentials'),
                                     ('env', 'JOHN_GOOGLE_CREDENTIALS'), ('env', 'JOHN_GMAIL_CREDENTIALS'), None])
def test_auth_preserves_credential_location(tmp_path, monkeypatch, location):
    data = {'calendar': {'enabled': False}}
    if location:
        section, key = location
        payload = creds([CALENDAR_SCOPE])
        data[section] = {key: json.dumps(payload) if section == 'env' else payload}
    config = config_file(tmp_path, data)
    class Flow(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials(scopes=[CALENDAR_SCOPE])
    assert run_main(monkeypatch, ['john-google-auth', '--config', str(config.path), '--calendar', '--no-gmail'], tmp_path, flow=Flow) == 0
    saved = json.loads(config.path.read_text())
    section, key = location or ('google', 'credentials')
    payload = saved[section][key]
    payload = json.loads(payload) if section == 'env' else payload
    assert payload['scopes'] == [CALENDAR_SCOPE]
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600
    assert saved['calendar'] == {'enabled': False}


def test_startup_rejects_mismatched_scope_locally(tmp_path, monkeypatch, capsys):
    from john_agent import server

    config = config_file(tmp_path, {'google': {'credentials': creds([GMAIL_SCOPE])},
                                   'calendar': {'enabled': True}})
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(config.path)])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert 'calendar.readonly' in capsys.readouterr().err


@pytest.mark.parametrize('gmail', [False, True])
def test_startup_calendar_wiring_shared_tokens(tmp_path, monkeypatch, gmail):
    from john_agent import server

    scopes = [CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE] + ([GMAIL_SCOPE] if gmail else [])
    config = config_file(tmp_path, {'google': {'credentials': creds(scopes)},
                                   'calendar': {'enabled': True, 'write': True},
                                   'gmail': {'enabled': gmail}})
    captured = {}

    class FakeServer:
        def run(self):
            pass

    def capture(reader, **kwargs):
        captured.update(kwargs)
        return FakeServer()

    monkeypatch.setattr(server, 'create_server', capture)
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(config.path)])
    server.main()
    assert captured['calendar_write']
    assert captured['calendar_client'].tokens is captured['google_client'].tokens
    assert (captured['gmail_reader'] is not None) is gmail
    if gmail:
        assert captured['gmail_reader']._client_factory() is captured['google_client']
