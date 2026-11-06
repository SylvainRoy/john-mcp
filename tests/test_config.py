from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from john_agent.config import (
    CONFIG_TEMPLATE,
    ConfigError,
    Settings,
    check_settings,
    init_config,
    load_config,
    resolve_settings,
    update_config,
)

GOOD_CREDS = {
    'client_id': 'cid', 'client_secret': 'csec', 'refresh_token': 'rtok',
}


def write_config(tmp_path: Path, data, mode: int = 0o600,
                 name: str = 'john-mcp.json') -> Path:
    path = tmp_path / name
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text)
    path.chmod(mode)
    return path


def base_config(**extra):
    data = {
        'version': 1,
        'corpus': {'markdown_root': '/abs/md'},
    }
    data.update(extra)
    return data


def resolve(overrides=None, config=None, environ=None):
    if environ is None:
        environ = {}
    return resolve_settings(overrides or {}, config, environ)


def test_defaults():
    settings = resolve({'markdown_root': '/tmp/md'})
    assert settings.markdown_root == Path('/tmp/md')
    assert settings.chroma_path is None
    assert settings.collection == 'documents'
    assert settings.embedding == 'gemini'
    assert settings.gemini_model == 'gemini-embedding-001'
    assert settings.gemini_api_key is None
    assert settings.gmail is False
    assert settings.gmail_credentials is None
    assert settings.env == {}


@pytest.mark.parametrize('name,cli,file_val,env_name,env_val,expected', [
    ('collection', 'cli-col', 'file-col', 'JOHN_COLLECTION', 'env-col', 'documents'),
    ('embedding', 'default', 'default', 'JOHN_EMBEDDING', 'default', 'gemini'),
    ('gemini_model', 'cli-m', 'file-m', 'JOHN_GEMINI_MODEL', 'env-m', 'gemini-embedding-001'),
])
def test_precedence(tmp_path, name, cli, file_val, env_name, env_val, expected):
    section = {'collection': 'corpus', 'embedding': 'embedding',
               'gemini_model': 'embedding'}[name]
    key = {'collection': 'collection', 'embedding': 'provider',
           'gemini_model': 'model'}[name]
    cfg = load_config(write_config(tmp_path, base_config(
        **{section: {key: file_val}})))
    cli_wins = resolve({name: cli, 'markdown_root': '/tmp/md'}, cfg,
                       {env_name: env_val})
    assert getattr(cli_wins, name) == cli
    file_wins = resolve({'markdown_root': '/tmp/md'}, cfg, {env_name: env_val})
    assert getattr(file_wins, name) == file_val
    env_wins = resolve({'markdown_root': '/tmp/md'}, None, {env_name: env_val})
    assert getattr(env_wins, name) == env_val
    default = resolve({'markdown_root': '/tmp/md'})
    assert getattr(default, name) == expected


def test_file_env_beats_inherited(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        env={'GOOGLE_API_KEY': 'file-key'})))
    settings = resolve({'markdown_root': '/tmp/md'}, cfg,
                       {'GEMINI_API_KEY': 'env-key'})
    assert settings.gemini_api_key == 'file-key'
    assert settings.env == {'GOOGLE_API_KEY': 'file-key'}


def test_file_env_alias_conflict(tmp_path):
    path = write_config(tmp_path, base_config(
        env={'GOOGLE_API_KEY': 'a', 'GEMINI_API_KEY': 'b'}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_file_null_suppresses_inherited(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        embedding={'api_key': None},
        corpus={'markdown_root': '/abs/md', 'chroma_path': None},
        gmail={'credentials': None})))
    env = {'GEMINI_API_KEY': 'env-key', 'JOHN_CHROMA_PATH': '/env/chroma',
           'JOHN_GMAIL_CREDENTIALS': 'env-creds'}
    settings = resolve({'markdown_root': '/tmp/md'}, cfg, env)
    assert settings.gemini_api_key is None
    assert settings.chroma_path is None
    assert settings.gmail_credentials is None


def test_file_env_empty_alias_suppresses_inherited(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        env={'GEMINI_API_KEY': ''})))
    settings = resolve({'markdown_root': '/tmp/md'}, cfg,
                       {'GEMINI_API_KEY': 'env-key'})
    assert settings.gemini_api_key is None


def test_inherited_gemini_beats_google():
    settings = resolve({'markdown_root': '/tmp/md'}, None,
                       {'GEMINI_API_KEY': 'g-key', 'GOOGLE_API_KEY': 'gg-key'})
    assert settings.gemini_api_key == 'g-key'
    settings = resolve({'markdown_root': '/tmp/md'}, None,
                       {'GEMINI_API_KEY': '', 'GOOGLE_API_KEY': 'gg-key'})
    assert settings.gemini_api_key == 'gg-key'


@pytest.mark.parametrize('env_val,expected', [
    ('0', False), ('false', False), ('No', False), ('off', False),
    ('1', True), ('TRUE', True), ('yes', True), ('on', True),
])
def test_env_boolean_parsing(env_val, expected):
    settings = resolve({'markdown_root': '/tmp/md'}, None, {'JOHN_GMAIL': env_val})
    assert settings.gmail is expected


def test_bool_env_rejects_junk():
    with pytest.raises(ConfigError):
        resolve({'markdown_root': '/tmp/md'}, None, {'JOHN_GMAIL': 'maybe'})


def test_json_numeric_bool_rejected(tmp_path):
    path = write_config(tmp_path, base_config(gmail={'enabled': 0}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_false_beats_file_true(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(gmail={'enabled': True})))
    settings = resolve({'gmail': False, 'markdown_root': '/tmp/md'}, cfg,
                       {'JOHN_GMAIL': 'true'})
    assert settings.gmail is False


def test_relative_paths_anchor(tmp_path, monkeypatch):
    cfg_dir = tmp_path / 'conf'
    cfg_dir.mkdir()
    path = cfg_dir / 'john-mcp.json'
    path.write_text(json.dumps(base_config(
        corpus={'markdown_root': 'md', 'chroma_path': 'chroma'})))
    path.chmod(0o600)
    cfg = load_config(path)
    settings = resolve({}, cfg, {})
    assert settings.markdown_root == Path(os.path.abspath(cfg_dir / 'md'))
    assert settings.chroma_path == Path(os.path.abspath(cfg_dir / 'chroma'))

    env_cfg_path = cfg_dir / 'env.json'
    env_cfg_path.write_text(json.dumps(base_config(
        env={'JOHN_CHROMA_PATH': 'env-chroma'})))
    env_cfg_path.chmod(0o600)
    settings = resolve({}, load_config(env_cfg_path), {})
    assert settings.chroma_path == Path(os.path.abspath(cfg_dir / 'env-chroma'))

    monkeypatch.chdir(tmp_path)
    settings = resolve({'markdown_root': 'rel-md'}, None,
                       {'JOHN_CHROMA_PATH': 'env-rel'})
    assert settings.markdown_root == Path(os.path.abspath(tmp_path / 'rel-md'))
    assert settings.chroma_path == Path(os.path.abspath(tmp_path / 'env-rel'))


def test_empty_environ_stays_empty():
    settings = resolve({'markdown_root': '/tmp/md'}, None, {})
    assert settings.env == {}
    assert settings.gemini_api_key is None


def test_duplicate_json_keys_rejected(tmp_path):
    path = write_config(tmp_path, '{"version": 1, "version": 2}')
    with pytest.raises(ConfigError):
        load_config(path)


def test_structural_and_env_alias_conflict(tmp_path):
    for data in (
        base_config(corpus={'markdown_root': '/abs/md'},
                    env={'JOHN_MARKDOWN_ROOT': '/abs/md'}),
        base_config(gmail={'enabled': True}, env={'JOHN_GMAIL': 'true'}),
    ):
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load_config(path)


@pytest.mark.parametrize('data', [
    '"just a string"',
    '[1,2]',
    '42',
    '{"version": true}',
    '{"version": 2}',
    '{"version": "1"}',
    '{"unknown_secret_key": "x"}',
    '{"corpus": null}',
    '{"env": null}',
    '{"corpus": "str"}',
    '{"corpus": {"markdown_root": 5}}',
    '{"corpus": {"markdown_root": ""}}',
    '{"corpus": {"markdown_root": null}}',
    '{"corpus": {"bogus": 1}}',
    '{"embedding": {"provider": "other"}}',
    '{"embedding": {"api_key": ""}}',
    '{"gmail": {"enabled": "yes"}}',
    '{"env": {"BAD-NAME": "x"}}',
    '{"env": {"OK": 1}}',
])
def test_schema_rejections(tmp_path, data):
    path = write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_nonfinite_json_rejected(tmp_path):
    for text in ('{"version": NaN}', '{"corpus": {"x": Infinity}}'):
        path = write_config(tmp_path, text)
        with pytest.raises(ConfigError):
            load_config(path)


def test_malformed_json_no_marker_echo(tmp_path):
    path = write_config(tmp_path, '{ "corpus": MARKER_SECRET_123')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert 'MARKER_SECRET_123' not in str(exc.value)


def test_unknown_key_not_echoed(tmp_path):
    path = write_config(tmp_path, '{"MYSTERY_KEY": "MYSTERY_VALUE"}')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert 'MYSTERY' not in str(exc.value)


@pytest.mark.parametrize('creds', [
    'a string',
    42,
    {'client_id': 'x', 'extra': 'y'},
    {'client_id': 'x', 'type': 'service_account'},
    {'client_id': 'x', 'type': None},
    {'client_id': 'x', 'scopes': None},
    {'client_id': 'x', 'scopes': ['https://mail.google.com/']},
    {'client_id': ''},
    {'client_id': 'x' * (20 * 1024)},
    {'client_id': 5},
])
def test_credential_schema_violations(tmp_path, creds):
    path = write_config(tmp_path, base_config(
        gmail={'credentials': creds}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_null_credential_placeholders_accepted(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': False, 'credentials': {
            'client_id': None, 'client_secret': None, 'refresh_token': None}})))
    settings = resolve({}, cfg, {})
    assert settings.gmail_credentials == {
        'type': 'authorized_user',
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
        'client_id': None, 'client_secret': None, 'refresh_token': None,
    }


def test_check_enabled_incomplete_credentials_fails(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'credentials': {'client_id': 'cid'}})))
    settings = resolve({}, cfg, {})
    with pytest.raises(ConfigError) as exc:
        check_settings(settings)
    assert 'cid' not in str(exc.value)


def test_check_chroma_gemini_requires_key(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        corpus={'markdown_root': '/abs/md', 'chroma_path': '/abs/chroma'})))
    settings = resolve({}, cfg, {})
    with pytest.raises(ConfigError):
        check_settings(settings)
    settings = resolve({}, cfg, {'GEMINI_API_KEY': 'k'})
    check_settings(settings)
    settings = resolve({'embedding': 'default'}, cfg, {})
    check_settings(settings)


def test_repr_hides_secrets(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        embedding={'api_key': 'MARKER_KEY'},
        gmail={'credentials': GOOD_CREDS})))
    settings = resolve({}, cfg, {})
    assert 'MARKER_KEY' not in repr(settings)
    assert 'csec' not in repr(settings)
    assert 'rtok' not in repr(cfg)


def test_symlink_and_nonregular_rejected(tmp_path):
    target = tmp_path / 'real.json'
    target.write_text('{}')
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    with pytest.raises(ConfigError):
        load_config(link)
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_oversize_config_rejected(tmp_path):
    path = write_config(tmp_path, '{"pad": "' + 'x' * (1024 * 1024) + '"}')
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permission checks')
def test_secret_files_require_0600(tmp_path):
    path = write_config(tmp_path, base_config(
        embedding={'api_key': 'k'}), mode=0o644)
    with pytest.raises(ConfigError):
        load_config(path)
    path.chmod(0o600)
    load_config(path)
    path = write_config(tmp_path, base_config(), mode=0o644, name='plain.json')
    load_config(path)
    path = write_config(tmp_path, base_config(env={'FOO': 'x'}),
                        mode=0o644, name='withenv.json')
    with pytest.raises(ConfigError):
        load_config(path)


def test_init_config(tmp_path):
    target = tmp_path / 'new.json'
    init_config(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    cfg = load_config(target)
    assert cfg.data == CONFIG_TEMPLATE
    creds = cfg.data['google']['credentials']
    assert all(v is None for v in creds.values())


def test_init_refuses_existing_and_dangling(tmp_path):
    existing = tmp_path / 'exists.json'
    existing.write_text('{"version":1}')
    with pytest.raises(ConfigError):
        init_config(existing)
    assert existing.read_text() == '{"version":1}'
    dangling = tmp_path / 'dangling.json'
    dangling.symlink_to(tmp_path / 'nowhere.json')
    with pytest.raises(ConfigError):
        init_config(dangling)
    assert not (tmp_path / 'nowhere.json').exists()
    with pytest.raises(ConfigError):
        init_config(tmp_path / 'missing' / 'x.json')


def test_init_write_failure_sanitized(tmp_path, monkeypatch):
    target = tmp_path / 'x.json'
    real_open = os.open

    def bad_fdopen(fd, *a, **k):
        os.close(fd)
        raise OSError('MARKER_SECRET_123')

    monkeypatch.setattr(os, 'fdopen', bad_fdopen)
    with pytest.raises(ConfigError) as exc:
        init_config(target)
    assert 'MARKER_SECRET_123' not in str(exc.value)
    assert not target.exists()


def with_collection(tmp_path, name, collection):
    data = base_config()
    data['corpus']['collection'] = collection
    return write_config(tmp_path, data, name=name)


def test_update_config_atomic(tmp_path):
    path = with_collection(tmp_path, 'john-mcp.json', 'old')
    cfg = load_config(path)
    data = json.loads(json.dumps(cfg.data))
    data['corpus']['collection'] = 'new'
    update_config(cfg, data)
    reloaded = load_config(path)
    assert reloaded.data['corpus']['collection'] == 'new'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_update_config_detects_concurrent_change(tmp_path):
    path = with_collection(tmp_path, 'john-mcp.json', 'old')
    cfg = load_config(path)
    changed = base_config()
    changed['corpus']['collection'] = 'changed'
    path.write_text(json.dumps(changed))
    data = json.loads(json.dumps(cfg.data))
    data['corpus']['collection'] = 'newer'
    before = {p.name for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError):
        update_config(cfg, data)
    assert json.loads(path.read_text())['corpus']['collection'] == 'changed'
    assert {p.name for p in tmp_path.iterdir()} == before


def test_update_config_replace_failure_keeps_original(tmp_path, monkeypatch):
    path = with_collection(tmp_path, 'john-mcp.json', 'documents')
    cfg = load_config(path)
    monkeypatch.setattr(os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('MARKER')))
    data = json.loads(json.dumps(cfg.data))
    data['corpus']['collection'] = 'newer'
    before = {p.name for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError) as exc:
        update_config(cfg, data)
    assert 'MARKER' not in str(exc.value)
    assert json.loads(path.read_text())['corpus']['collection'] == 'documents'
    assert {p.name for p in tmp_path.iterdir()} == before


def test_cli_wiring(tmp_path, monkeypatch):
    import john_agent.server as server
    from mdtodb import ReaderError

    captured = {}

    class FakeReader:
        def __init__(self, root, collection_factory=None, embedding_factory=None):
            captured['root'] = root
            captured['collection_factory'] = collection_factory
            captured['embedding_factory'] = embedding_factory

    class FakeServer:
        def run(self):
            captured['ran'] = True

    def fake_create(reader, gmail_reader=None, gmail_drafter=None, **kwargs):
        captured['gmail_reader'] = gmail_reader
        captured['gmail_drafter'] = gmail_drafter
        return FakeServer()

    embed_calls = []

    def fake_get_embedding(name, **options):
        embed_calls.append((name, options))
        return ('embed', 'model')

    monkeypatch.setattr(server, 'CorpusReader', FakeReader)
    monkeypatch.setattr(server, 'create_server', fake_create)
    monkeypatch.setattr(server, 'get_embedding', fake_get_embedding)
    monkeypatch.setattr(server.GmailReader, 'close', lambda self: None)

    path = write_config(tmp_path, base_config(
        corpus={'markdown_root': 'md', 'chroma_path': 'chroma'},
        embedding={'api_key': 'FILEKEY'},
        env={'JOHN_TEST_MARK': 'present'}))
    monkeypatch.setenv('GEMINI_API_KEY', 'ENVKEY')
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(path)])
    monkeypatch.delenv('JOHN_TEST_MARK', raising=False)
    server.main()
    assert captured['ran'] is True
    assert captured['root'] == Path(os.path.abspath(tmp_path / 'md'))
    assert os.environ.get('JOHN_TEST_MARK') == 'present'
    captured['embedding_factory']()
    assert embed_calls[-1][1]['api_key'] == 'FILEKEY'


def test_cli_null_key_blocks_env_fallback(tmp_path, monkeypatch):
    import john_agent.server as server
    from mdtodb import ReaderError

    captured = {}

    class FakeReader:
        def __init__(self, root, collection_factory=None, embedding_factory=None):
            captured['embedding_factory'] = embedding_factory

    class FakeServer:
        def run(self):
            pass

    monkeypatch.setattr(server, 'CorpusReader', FakeReader)
    monkeypatch.setattr(server, 'create_server', lambda r, gmail_reader=None, gmail_drafter=None, **kwargs: FakeServer())
    path = write_config(tmp_path, base_config(embedding={'api_key': None}))
    monkeypatch.setenv('GEMINI_API_KEY', 'ENVKEY')
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(path)])
    server.main()
    with pytest.raises(ReaderError):
        captured['embedding_factory']()


def test_cli_gmail_credentials_and_no_gmail(tmp_path, monkeypatch):
    import john_agent.server as server

    captured = {}

    class FakeReader:
        def __init__(self, *a, **k):
            pass

    class FakeServer:
        def run(self):
            pass

    def fake_create(reader, gmail_reader=None, gmail_drafter=None, **kwargs):
        captured['gmail_reader'] = gmail_reader
        captured['gmail_drafter'] = gmail_drafter
        return FakeServer()

    monkeypatch.setattr(server, 'CorpusReader', FakeReader)
    monkeypatch.setattr(server, 'create_server', fake_create)

    path = write_config(tmp_path, base_config(
        gmail={'enabled': True, 'credentials': GOOD_CREDS}))
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--config', str(path)])
    server.main()
    gmail_reader = captured['gmail_reader']
    assert gmail_reader is not None
    client = gmail_reader._client_factory()
    assert client._use_environment is False
    assert client._injected_credentials['refresh_token'] == 'rtok'

    captured.clear()
    monkeypatch.setattr(sys, 'argv',
                        ['john-mcp', '--config', str(path), '--no-gmail'])
    server.main()
    assert captured['gmail_reader'] is None


def test_check_config_does_not_apply_env(tmp_path, monkeypatch, capsys):
    import john_agent.server as server

    monkeypatch.setattr(server, 'CorpusReader',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    path = write_config(tmp_path, base_config(env={'JOHN_TEST_MARK': 'x'}))
    monkeypatch.delenv('JOHN_TEST_MARK', raising=False)
    monkeypatch.setattr(sys, 'argv',
                        ['john-mcp', '--config', str(path), '--check-config'])
    server.main()
    out = capsys.readouterr().out
    assert 'Configuration is valid' in out
    assert 'JOHN_TEST_MARK' not in os.environ


def test_check_config_invalid_credentials(tmp_path, monkeypatch):
    import john_agent.server as server

    path = write_config(tmp_path, base_config(
        gmail={'enabled': True, 'credentials': {'client_id': 'MARKER_ONLY'}}))
    monkeypatch.setattr(sys, 'argv',
                        ['john-mcp', '--config', str(path), '--check-config'])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code != 0


def test_init_config_cli_and_conflicts(tmp_path, monkeypatch, capsys):
    import john_agent.server as server

    target = tmp_path / 'init.json'
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--init-config', str(target)])
    server.main()
    assert target.exists()
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--init-config', str(target)])
    with pytest.raises(SystemExit):
        server.main()
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--init-config',
                                      str(tmp_path / 'y.json'), '--gmail'])
    with pytest.raises(SystemExit):
        server.main()


def test_no_implicit_config_reads(tmp_path, monkeypatch):
    import john_agent.server as server

    captured = {}

    class FakeReader:
        def __init__(self, *a, **k):
            pass

    class FakeServer:
        def run(self):
            pass

    monkeypatch.setattr(server, 'CorpusReader', FakeReader)
    monkeypatch.setattr(server, 'create_server', lambda *a, **k: FakeServer())
    stray = tmp_path / 'john-mcp.json'
    stray.write_text('{"version":1}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['john-mcp', '--markdown-root', '/tmp/md'])
    server.main()


DRAFT_CREDS = {
    'client_id': 'cid', 'client_secret': 'csec', 'refresh_token': 'rtok',
    'scopes': ['https://www.googleapis.com/auth/gmail.readonly',
               'https://www.googleapis.com/auth/gmail.compose'],
}


def test_drafts_requires_gmail():
    with pytest.raises(ConfigError) as exc:
        resolve({'markdown_root': '/tmp/md', 'gmail_drafts': True})
    assert 'gmail.drafts requires gmail.enabled' in str(exc.value)


def test_drafts_precedence(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'drafts': True})))
    settings = resolve({'markdown_root': '/tmp/md'}, cfg,
                       {'JOHN_GMAIL_DRAFTS': 'false'})
    assert settings.gmail_drafts is True
    settings = resolve({'markdown_root': '/tmp/md'}, None,
                       {'JOHN_GMAIL': 'true', 'JOHN_GMAIL_DRAFTS': 'true'})
    assert settings.gmail_drafts is True
    settings = resolve({'markdown_root': '/tmp/md', 'gmail_drafts': False}, cfg, {})
    assert settings.gmail_drafts is False
    settings = resolve({'markdown_root': '/tmp/md'})
    assert settings.gmail_drafts is False


def test_attachment_roots_and_source_root_resolution(tmp_path, monkeypatch):
    cfg_dir = tmp_path / 'conf'
    cfg_dir.mkdir()
    path = cfg_dir / 'john-mcp.json'
    path.write_text(json.dumps(base_config(
        gmail={'enabled': True, 'drafts': True,
               'attachment_roots': ['files', '/abs/root'],
               'source_root': 'sources'})))
    path.chmod(0o600)
    settings = resolve({'markdown_root': '/tmp/md'}, load_config(path), {})
    assert settings.gmail_attachment_roots == [
        Path(os.path.abspath(cfg_dir / 'files')), Path('/abs/root')]
    assert settings.gmail_source_root == Path(os.path.abspath(cfg_dir / 'sources'))

    monkeypatch.chdir(tmp_path)
    joined = 'a' + os.pathsep + str(tmp_path / 'b')
    settings = resolve({'markdown_root': '/tmp/md', 'gmail': True}, None,
                       {'JOHN_GMAIL_ATTACHMENT_ROOTS': joined,
                        'JOHN_GMAIL_SOURCE_ROOT': 'srcenv'})
    assert settings.gmail_attachment_roots == [
        Path(os.path.abspath(tmp_path / 'a')), tmp_path / 'b']
    assert settings.gmail_source_root == Path(os.path.abspath(tmp_path / 'srcenv'))

    settings = resolve({'markdown_root': '/tmp/md', 'gmail': True,
                        'gmail_attachment_roots': ['/cli/root']}, None, {})
    assert settings.gmail_attachment_roots == [Path('/cli/root')]

    settings = resolve({'markdown_root': '/tmp/md', 'gmail': True})
    assert settings.gmail_attachment_roots == []
    assert settings.gmail_source_root is None


def test_attachment_roots_null_blocks_env(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'attachment_roots': None, 'source_root': None})))
    settings = resolve({'markdown_root': '/tmp/md'}, cfg,
                       {'JOHN_GMAIL_ATTACHMENT_ROOTS': '/env/root',
                        'JOHN_GMAIL_SOURCE_ROOT': '/env/src'})
    assert settings.gmail_attachment_roots == []
    assert settings.gmail_source_root is None


@pytest.mark.parametrize('gmail', [
    {'drafts': 'yes'},
    {'attachment_roots': '/notalist'},
    {'attachment_roots': ['ok', '']},
    {'attachment_roots': [5]},
    {'source_root': 7},
])
def test_gmail_drafts_schema_rejections(tmp_path, gmail):
    path = write_config(tmp_path, base_config(gmail=gmail))
    with pytest.raises(ConfigError):
        load_config(path)


def test_check_drafts_requires_compose_scope(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'drafts': True, 'credentials': GOOD_CREDS})))
    settings = resolve({}, cfg, {})
    with pytest.raises(ConfigError) as exc:
        check_settings(settings)
    assert 'john-gmail-auth --drafts' in str(exc.value)

    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'drafts': True, 'credentials': DRAFT_CREDS})))
    check_settings(resolve({}, cfg, {}))

    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'enabled': True, 'credentials': GOOD_CREDS})))
    check_settings(resolve({}, cfg, {}))


def test_credential_schema_compose_scope_accepted(tmp_path):
    cfg = load_config(write_config(tmp_path, base_config(
        gmail={'credentials': DRAFT_CREDS})))
    settings = resolve({}, cfg, {})
    assert set(settings.gmail_credentials['scopes']) == set(DRAFT_CREDS['scopes'])


def test_check_config_drafts_happy_path(tmp_path, monkeypatch, capsys):
    import john_agent.server as server

    path = write_config(tmp_path, base_config(
        gmail={'enabled': True, 'drafts': True, 'credentials': DRAFT_CREDS}))
    monkeypatch.setattr(sys, 'argv',
                        ['john-mcp', '--config', str(path), '--check-config'])
    server.main()
    assert 'Configuration is valid' in capsys.readouterr().out


def test_cli_drafts_wiring(tmp_path, monkeypatch):
    import john_agent.server as server

    captured = {}

    class FakeReader:
        def __init__(self, *a, **k):
            pass

    class FakeServer:
        def run(self):
            pass

    def fake_create(reader, gmail_reader=None, gmail_drafter=None, **kwargs):
        captured['gmail_reader'] = gmail_reader
        captured['gmail_drafter'] = gmail_drafter
        return FakeServer()

    monkeypatch.setattr(server, 'CorpusReader', FakeReader)
    monkeypatch.setattr(server, 'create_server', fake_create)
    path = write_config(tmp_path, base_config(
        gmail={'enabled': True, 'credentials': DRAFT_CREDS}))
    monkeypatch.setattr(sys, 'argv', [
        'john-mcp', '--config', str(path), '--gmail-drafts',
        '--attachment-root', '/cli/root', '--source-root', '/cli/src'])
    server.main()
    drafter = captured['gmail_drafter']
    assert drafter is not None
    assert drafter.roots == [Path('/cli/root').resolve()]
    assert drafter.source_root == Path('/cli/src').resolve()

    captured.clear()
    monkeypatch.setattr(sys, 'argv',
                        ['john-mcp', '--config', str(path)])
    server.main()
    assert captured['gmail_drafter'] is None
    assert captured['gmail_reader'] is not None
