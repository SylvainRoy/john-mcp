from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from john_agent import gmail_auth
from john_agent.gmail_client import GMAIL_COMPOSE_SCOPE, GMAIL_ENV, GMAIL_SCOPE


class FakeCredentials:
    def __init__(self, refresh_token='synthetic-refresh', scopes=None):
        self.refresh_token = refresh_token
        self.scopes = scopes if scopes is not None else [GMAIL_SCOPE]


class FakeFlow:
    last = None

    def __init__(self):
        self.config = None
        self.kwargs = None
        self.oauth2session = None
        FakeFlow.last = self

    @classmethod
    def from_client_config(cls, config, scopes, autogenerate_code_verifier):
        cls.last_config = config
        cls.last_scopes = scopes
        cls.last_pkce = autogenerate_code_verifier
        return cls()

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return FakeCredentials()


def run_main(monkeypatch, argv, tmp_path, *, isatty=True, flow=FakeFlow,
             secrets=('cid', 'csec')):
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(gmail_auth, 'InstalledAppFlow', flow)
    monkeypatch.setattr(gmail_auth.getpass, 'getpass', lambda prompt: next(secrets_iter))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: isatty)
    secrets_iter = iter(secrets)
    try:
        gmail_auth.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_happy_path_writes_private_json(tmp_path, monkeypatch, capsys):
    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)], tmp_path)
    assert code == 0
    data = json.loads(out.read_text())
    assert data == {
        'type': 'authorized_user',
        'client_id': 'cid',
        'client_secret': 'csec',
        'refresh_token': 'synthetic-refresh',
        'scopes': [GMAIL_SCOPE],
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert FakeFlow.last_scopes == [GMAIL_SCOPE]
    assert FakeFlow.last_pkce is True
    assert FakeFlow.last_config['installed']['redirect_uris'] == ['http://localhost']
    assert FakeFlow.last_config['installed']['token_uri'].endswith('oauth2.googleapis.com/token')
    kwargs = FakeFlow.last.kwargs
    assert kwargs['host'] == '127.0.0.1'
    assert kwargs['port'] == 0
    assert kwargs['access_type'] == 'offline'
    assert kwargs['prompt'] == 'consent'
    captured = capsys.readouterr()
    assert 'synthetic-refresh' not in captured.out
    assert 'synthetic-refresh' not in captured.err
    assert 'csec' not in captured.out


def test_existing_path_not_overwritten_no_prompt(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    out.write_text('keep me')
    prompted = []
    monkeypatch.setattr(sys, 'argv', ['john-gmail-auth', '--output', str(out)])
    monkeypatch.setattr(gmail_auth, 'InstalledAppFlow', FakeFlow)
    monkeypatch.setattr(gmail_auth.getpass, 'getpass',
                        lambda p: prompted.append(1) or 'x')
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    with pytest.raises(SystemExit) as exc:
        gmail_auth.main()
    assert exc.value.code != 0
    assert out.read_text() == 'keep me'
    assert prompted == []


def test_symlink_path_not_overwritten(tmp_path, monkeypatch):
    target = tmp_path / 'real.json'
    target.write_text('keep')
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(link)], tmp_path)
    assert code != 0
    assert target.read_text() == 'keep'


def test_missing_parent_rejected(tmp_path, monkeypatch):
    out = tmp_path / 'nope' / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)], tmp_path)
    assert code != 0
    assert not out.exists()


def test_noninteractive_rejected(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, isatty=False)
    assert code != 0
    assert not out.exists()


def test_missing_refresh_token_rejected(tmp_path, monkeypatch):
    class NoRefresh(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials(refresh_token=None)

    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=NoRefresh)
    assert code != 0
    assert not out.exists()


def test_broader_scopes_rejected(tmp_path, monkeypatch):
    class Broad(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials(scopes=[GMAIL_SCOPE, 'https://mail.google.com/'])

    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=Broad)
    assert code != 0
    assert not out.exists()


def test_flow_error_sanitized(tmp_path, monkeypatch, capsys):
    class Boom(FakeFlow):
        def run_local_server(self, **kwargs):
            raise RuntimeError('SECRET_SENTINEL provider detail')

    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=Boom)
    assert code != 0
    captured = capsys.readouterr()
    assert 'SECRET_SENTINEL' not in captured.out
    assert 'SECRET_SENTINEL' not in captured.err
    assert not out.exists()


def test_requested_readonly_granted_broader_fails(tmp_path, monkeypatch):
    class Broader(FakeFlow):
        def run_local_server(self, **kwargs):
            c = FakeCredentials()
            c.granted_scopes = [GMAIL_SCOPE, 'https://mail.google.com/']
            return c

    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=Broader)
    assert code != 0
    assert not out.exists()


@pytest.mark.parametrize('granted', [None, [], [GMAIL_SCOPE]])
def test_granted_scope_variants_succeed(tmp_path, monkeypatch, granted):
    class G(FakeFlow):
        def run_local_server(self, **kwargs):
            c = FakeCredentials()
            c.granted_scopes = granted
            return c

    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=G)
    assert code == 0
    assert json.loads(out.read_text())['scopes'] == [GMAIL_SCOPE]


@pytest.mark.parametrize('exc', [EOFError(), KeyboardInterrupt()])
def test_getpass_interruption_sanitized(tmp_path, monkeypatch, capsys, exc):
    out = tmp_path / 'creds.json'
    monkeypatch.setattr(sys, 'argv', ['john-gmail-auth', '--output', str(out)])
    monkeypatch.setattr(gmail_auth.getpass, 'getpass',
                        lambda p: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    with pytest.raises(SystemExit):
        gmail_auth.main()
    captured = capsys.readouterr()
    assert 'EOF' not in captured.err and 'KeyboardInterrupt' not in captured.err
    assert not out.exists()


def test_oversized_client_secret_rejected(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, secrets=('cid', 's' * (20 * 1024)))
    assert code != 0
    assert not out.exists()


def test_write_failure_removes_only_own_partial(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'

    class FailDump(FakeFlow):
        def run_local_server(self, **kwargs):
            return FakeCredentials()

    monkeypatch.setattr(json, 'dump', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)],
                    tmp_path, flow=FailDump)
    assert code != 0
    assert not out.exists()


def test_write_failure_keeps_replaced_path(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    real_fdopen = os.fdopen

    def racy_fdopen(fd, mode='r', *a, **k):
        os.close(fd)
        out.unlink(missing_ok=True)
        out.write_text('replacement')
        raise RuntimeError('mid-write race')

    monkeypatch.setattr(gmail_auth.os, 'fdopen', racy_fdopen)
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)], tmp_path)
    assert code != 0
    assert out.read_text() == 'replacement'


def test_success_print_broken_pipe_keeps_file(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    monkeypatch.setattr('builtins.print',
                        lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError()))
    code = run_main(monkeypatch, ['john-gmail-auth', '--output', str(out)], tmp_path)
    assert code == 0
    data = json.loads(out.read_text())
    assert data['type'] == 'authorized_user'
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def _write_auth_config(tmp_path, data, name='cfg.json'):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


AUTH_BASE = {
    'version': 1,
    'corpus': {'markdown_root': '/abs/md'},
    'gmail': {'enabled': False},
}


def run_config_auth(monkeypatch, cfg_path, *, flow=FakeFlow, getpass_map=None):
    """Run main with --config; getpass_map maps prompt->value (None = never call)."""
    monkeypatch.setattr(sys, 'argv', ['john-gmail-auth', '--config', str(cfg_path)])
    monkeypatch.setattr(gmail_auth, 'InstalledAppFlow', flow)
    prompts = []
    if getpass_map is None:
        monkeypatch.setattr(gmail_auth.getpass, 'getpass',
                            lambda p: (_ for _ in ()).throw(AssertionError('prompted')))
    else:
        it = iter(getpass_map)

        def gp(prompt):
            prompts.append(prompt)
            return next(it)

        monkeypatch.setattr(gmail_auth.getpass, 'getpass', gp)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    try:
        gmail_auth.main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, prompts


def test_config_flow_uses_configured_client(tmp_path, monkeypatch, capsys):
    data = dict(AUTH_BASE)
    data['gmail'] = {'enabled': False, 'credentials': {
        'client_id': 'cid', 'client_secret': 'csec', 'refresh_token': None}}
    cfg = _write_auth_config(tmp_path, data)
    code, _ = run_config_auth(monkeypatch, cfg)
    assert code == 0
    saved = json.loads(cfg.read_text())
    assert saved['gmail']['credentials']['refresh_token'] == 'synthetic-refresh'
    assert saved['gmail']['enabled'] is False
    assert saved['corpus'] == {'markdown_root': '/abs/md'}
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert 'synthetic-refresh' not in captured.out + captured.err
    assert 'csec' not in captured.out + captured.err
    assert 'saved to the configuration' in captured.out


def test_config_partial_null_prompts_for_missing(tmp_path, monkeypatch):
    data = dict(AUTH_BASE)
    data['gmail'] = {'enabled': True, 'credentials': {
        'client_id': 'cid', 'client_secret': None, 'refresh_token': None}}
    cfg = _write_auth_config(tmp_path, data)
    code, prompts = run_config_auth(monkeypatch, cfg, getpass_map=['csec'])
    assert code == 0
    assert len(prompts) == 1
    saved = json.loads(cfg.read_text())
    assert saved['gmail']['credentials']['client_secret'] == 'csec'


def test_config_env_style_updates_same_location(tmp_path, monkeypatch):
    existing = json.dumps({
        'type': 'authorized_user', 'client_id': 'cid', 'client_secret': 'csec',
        'refresh_token': 'old', 'scopes': [GMAIL_SCOPE]})
    data = dict(AUTH_BASE)
    data['env'] = {GMAIL_ENV: existing}
    cfg = _write_auth_config(tmp_path, data)
    code, _ = run_config_auth(monkeypatch, cfg)
    assert code == 0
    saved = json.loads(cfg.read_text())
    new_creds = json.loads(saved['env'][GMAIL_ENV])
    assert new_creds['refresh_token'] == 'synthetic-refresh'
    assert 'credentials' not in saved.get('gmail', {})


def test_config_concurrent_edit_keeps_original(tmp_path, monkeypatch):
    data = dict(AUTH_BASE)
    data['gmail'] = {'credentials': {'client_id': 'cid', 'client_secret': 'csec'}}
    cfg = _write_auth_config(tmp_path, data)
    original = cfg.read_bytes()

    class Editor(FakeFlow):
        def run_local_server(self, **kwargs):
            cfg.write_text(json.dumps(dict(AUTH_BASE, corpus={'markdown_root': '/other'})))
            return FakeCredentials()

    code, _ = run_config_auth(monkeypatch, cfg, flow=Editor)
    assert code != 0
    after = cfg.read_text()
    assert 'synthetic-refresh' not in after
    assert json.loads(after)['corpus']['markdown_root'] == '/other'
    assert not list(tmp_path.glob('.john-mcp-*.tmp'))


def test_config_replace_failure_keeps_original(tmp_path, monkeypatch):
    data = dict(AUTH_BASE)
    data['gmail'] = {'credentials': {'client_id': 'cid', 'client_secret': 'csec'}}
    cfg = _write_auth_config(tmp_path, data)
    original = cfg.read_bytes()
    monkeypatch.setattr(gmail_auth.os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('MARKER')))
    code, _ = run_config_auth(monkeypatch, cfg)
    assert code != 0
    assert cfg.read_bytes() == original
    assert not list(tmp_path.glob('.john-mcp-*.tmp'))


def test_config_symlink_rejected_before_consent(tmp_path, monkeypatch):
    target = tmp_path / 'real.json'
    target.write_text(json.dumps(AUTH_BASE))
    target.chmod(0o600)
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    used = []

    class Spy(FakeFlow):
        def run_local_server(self, **kwargs):
            used.append(1)
            return FakeCredentials()

    code, _ = run_config_auth(monkeypatch, link, flow=Spy)
    assert code != 0
    assert used == []
    assert target.read_text() == json.dumps(AUTH_BASE)


@pytest.mark.parametrize('raw', [
    '[' * 2000 + '"MARKER_SECRET"' + ']' * 2000,
    json.dumps({'client_id': 'cid', 'client_secret': 'csec',
                'padding': 'MARKER_SECRET' * 6000}),
])
def test_config_env_credentials_rejected_before_consent(tmp_path, monkeypatch, capsys, raw):
    data = dict(AUTH_BASE)
    data['env'] = {GMAIL_ENV: raw}
    cfg = _write_auth_config(tmp_path, data)
    original = cfg.read_bytes()
    used = []

    class Spy(FakeFlow):
        @classmethod
        def from_client_config(cls, *args, **kwargs):
            used.append(True)
            return super().from_client_config(*args, **kwargs)

    code, _ = run_config_auth(monkeypatch, cfg, flow=Spy)
    assert code != 0
    assert used == []
    assert cfg.read_bytes() == original
    captured = capsys.readouterr()
    assert 'MARKER_SECRET' not in captured.out + captured.err
    assert 'Traceback' not in captured.out + captured.err


def test_config_missing_file_rejected(tmp_path, monkeypatch):
    code, _ = run_config_auth(monkeypatch, tmp_path / 'none.json')
    assert code != 0


def test_real_installed_app_flow_accepts_config():
    flow = gmail_auth.InstalledAppFlow.from_client_config(
        {'installed': {'client_id': 'synthetic.apps.googleusercontent.com',
                       'client_secret': 'synthetic-secret',
                       'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                       'token_uri': 'https://oauth2.googleapis.com/token',
                       'redirect_uris': ['http://localhost']}},
        scopes=[GMAIL_SCOPE], autogenerate_code_verifier=True,
    )
    try:
        session = flow.oauth2session
        assert session is not None
        url, _ = flow.authorization_url()
        assert 'code_challenge' in url
        assert 'scope=' in url
    finally:
        try:
            flow.oauth2session.close()
        except Exception:
            pass


def test_help_works_offline():
    import subprocess

    env = {k: v for k, v in os.environ.items()
           if 'GMAIL' not in k and 'API_KEY' not in k}
    proc = subprocess.run(
        [sys.executable, '-m', 'john_agent.gmail_auth', '--help'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert '--output' in proc.stdout
    assert 'never sent' in proc.stdout


class DraftFlow(FakeFlow):
    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return FakeCredentials(scopes=[GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE])


def test_drafts_flag_requests_and_saves_both_scopes(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch,
                    ['john-gmail-auth', '--output', str(out), '--drafts'],
                    tmp_path, flow=DraftFlow)
    assert code == 0
    assert DraftFlow.last_scopes == [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE]
    data = json.loads(out.read_text())
    assert data['scopes'] == [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE]


def test_drafts_flag_rejects_readonly_grant(tmp_path, monkeypatch):
    out = tmp_path / 'creds.json'
    code = run_main(monkeypatch,
                    ['john-gmail-auth', '--output', str(out), '--drafts'],
                    tmp_path, flow=FakeFlow)
    assert code != 0
    assert not out.exists()


def test_drafts_config_flow_saves_compose_scopes(tmp_path, monkeypatch):
    data = dict(AUTH_BASE)
    data['gmail'] = {'enabled': True, 'drafts': True, 'credentials': {
        'client_id': 'cid', 'client_secret': 'csec', 'refresh_token': None}}
    cfg = _write_auth_config(tmp_path, data)
    monkeypatch.setattr(sys, 'argv',
                        ['john-gmail-auth', '--config', str(cfg), '--drafts'])
    monkeypatch.setattr(gmail_auth, 'InstalledAppFlow', DraftFlow)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    gmail_auth.main()
    saved = json.loads(cfg.read_text())
    assert saved['gmail']['credentials']['scopes'] == [
        GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE]
