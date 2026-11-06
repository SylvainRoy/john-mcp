from __future__ import annotations

import json
import threading
import urllib.parse

import httpx
import pytest

from john_agent.gmail_client import (
    API_ROOT,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_ENV,
    GMAIL_SCOPE,
    TOKEN_URL,
    GmailClient,
    GmailError,
)

CREDS = {
    'type': 'authorized_user',
    'client_id': 'synthetic.apps.googleusercontent.com',
    'client_secret': 'synthetic-secret',
    'refresh_token': 'synthetic-refresh',
    'scopes': [GMAIL_SCOPE],
}
COMPOSE_CREDS = {**CREDS, 'scopes': [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE]}


def make_client(handler, *, credentials=CREDS, clock=None):
    requests = []
    sleeps = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    http = httpx.Client(transport=transport, follow_redirects=False, trust_env=False, timeout=10.0)
    client = GmailClient(
        credentials=credentials,
        http_client=http,
        sleep=sleeps.append,
        clock=clock or (lambda: 1000.0),
    )
    return client, requests, sleeps


def token_response(**extra):
    body = {'access_token': 'fake-access-token', 'expires_in': 3600, 'scope': GMAIL_SCOPE}
    body.update(extra)
    return httpx.Response(200, json=body)


def ok(payload):
    return httpx.Response(200, json=payload)


def test_lazy_env_not_read_until_first_call(monkeypatch):
    client, requests, _ = make_client(lambda r: ok({}), credentials=None)
    monkeypatch.delenv(GMAIL_ENV, raising=False)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'not_configured'
    assert requests == []


def test_env_credentials_parsed_lazily(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return ok({'emailAddress': 'user@example.com'})

    client, requests, _ = make_client(handler, credentials=None)
    monkeypatch.setenv(GMAIL_ENV, json.dumps(CREDS))
    profile = client.get_profile()
    assert profile['emailAddress'] == 'user@example.com'
    token_req = requests[0]
    assert str(token_req.url).startswith(TOKEN_URL)
    form = urllib.parse.parse_qs(token_req.content.decode())
    assert form['grant_type'] == ['refresh_token']
    assert form['refresh_token'] == ['synthetic-refresh']
    get_req = requests[1]
    assert get_req.headers['Authorization'] == 'Bearer fake-access-token'
    assert 'synthetic-secret' not in str(get_req.url)
    assert 'synthetic-refresh' not in str(get_req.url)


@pytest.mark.parametrize('bad', [
    {'type': 'authorized_user', 'client_id': '', 'client_secret': 'x', 'refresh_token': 'y',
     'scopes': [GMAIL_SCOPE]},
    {'type': 'service_account', 'client_id': 'x', 'client_secret': 'y', 'refresh_token': 'z',
     'scopes': [GMAIL_SCOPE]},
    {'type': 'authorized_user', 'client_id': 'x', 'client_secret': 'y', 'refresh_token': 'z',
     'scopes': ['https://mail.google.com/']},
    {'type': 'authorized_user', 'client_id': 'x', 'client_secret': 'y', 'refresh_token': 'z',
     'scopes': [GMAIL_SCOPE, 'extra']},
    {'type': 'authorized_user', 'client_id': 'x', 'client_secret': 'y', 'refresh_token': 'z',
     'scopes': [GMAIL_COMPOSE_SCOPE]},
    {'type': 'authorized_user', 'client_id': 'x', 'client_secret': 'y', 'refresh_token': 'z',
     'scopes': [GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, 'https://mail.google.com/']},
])
def test_invalid_credentials_rejected(monkeypatch, bad):
    client, requests, _ = make_client(lambda r: ok({}), credentials=None)
    monkeypatch.setenv(GMAIL_ENV, json.dumps(bad))
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_credentials'
    assert requests == []


def test_malformed_env_json_rejected(monkeypatch):
    client, requests, _ = make_client(lambda r: ok({}), credentials=None)
    monkeypatch.setenv(GMAIL_ENV, '{nope')
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_credentials'
    assert requests == []


def test_oversized_env_rejected(monkeypatch):
    client, requests, _ = make_client(lambda r: ok({}), credentials=None)
    monkeypatch.setenv(GMAIL_ENV, ' ' * (65 * 1024))
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_credentials'
    assert requests == []


def test_access_token_cached_until_expiry():
    clock = [1000.0]
    token_calls = []

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            token_calls.append(request)
            return token_response()
        return ok({'emailAddress': 'a@b.c'})

    client, requests, _ = make_client(handler, clock=lambda: clock[0])
    client.get_profile()
    clock[0] += 1800
    client.get_profile()
    assert len(token_calls) == 1
    clock[0] += 1800
    client.get_profile()
    assert len(token_calls) == 2


def test_401_forces_single_refresh_and_retry():
    state = {'gets': 0, 'tokens': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            state['tokens'] += 1
            return token_response(access_token=f'tok{state["tokens"]}')
        state['gets'] += 1
        if request.headers['Authorization'] == 'Bearer tok1':
            return httpx.Response(401, json={'error': {'code': 401}})
        return ok({'emailAddress': 'a@b.c'})

    client, requests, _ = make_client(handler)
    assert client.get_profile()['emailAddress'] == 'a@b.c'
    assert state['tokens'] == 2


def test_repeated_401_is_authorization_required():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(401, json={'error': {'code': 401}})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'authorization_required'


def test_429_and_503_retry_bounds_and_sleep():
    state = {'gets': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        state['gets'] += 1
        if state['gets'] < 3:
            return httpx.Response(503, json={})
        return ok({'ok': True})

    client, _, sleeps = make_client(handler)
    assert client.get_profile() == {'ok': True}
    assert sleeps == [0.25, 0.5]


def test_retry_exhaustion_unavailable():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(503, json={})

    client, requests, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'unavailable'
    assert sum(1 for r in requests if r.url.host != 'oauth2.googleapis.com') == 3


def test_retry_after_caps_at_two_seconds():
    state = {'gets': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        state['gets'] += 1
        if state['gets'] == 1:
            return httpx.Response(429, json={}, headers={'Retry-After': '30'})
        return ok({'ok': True})

    client, _, sleeps = make_client(handler)
    client.get_profile()
    assert sleeps == [2.0]


def test_403_reasons_distinction():
    def quota(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(403, json={'error': {'errors': [{'reason': 'userRateLimitExceeded'}]}})

    client, _, _ = make_client(quota)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'rate_limited'

    def denied(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(403, json={'error': {'errors': [{'reason': 'insufficientPermissions'}]}})

    client, _, _ = make_client(denied)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'permission_denied'


def test_404_not_found():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(404, json={})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_message('abc123')
    assert exc.value.code == 'not_found'


def test_token_400_authorization_required():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return httpx.Response(400, json={'error': 'invalid_grant'})
        return ok({})

    client, requests, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'authorization_required'
    assert len(requests) == 1


def test_malformed_json_and_bad_token_shape():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return httpx.Response(200, content=b'{broken')
        return ok({})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'

    def handler2(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response(expires_in=-5)
        return ok({})

    client, _, _ = make_client(handler2)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'


def test_wrong_token_scope_rejected():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response(scope='https://mail.google.com/')
        return ok({})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'


def test_stream_overflow_too_large():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(200, content=b' ' * (300 * 1024))

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'too_large'


def test_redirects_not_followed():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return httpx.Response(302, headers={'location': 'https://evil.example/x'})

    client, requests, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'
    assert all('evil.example' not in str(r.url) for r in requests)


def test_unsafe_ids_never_reach_transport():
    client, requests, _ = make_client(lambda r: ok({}))
    for bad in ['../x', 'a/b', 'a?b', '', 'x' * 300, 'id with space']:
        with pytest.raises(GmailError) as exc:
            client.get_message(bad)
        assert exc.value.code == 'invalid_input'
    assert requests == []
    with pytest.raises(GmailError) as exc:
        client.get_attachment('abc', '')
    assert exc.value.code == 'invalid_input'


def test_attachment_id_quoted():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return ok({'data': 'AAAA'})

    client, requests, _ = make_client(handler)
    client.get_attachment('msg1', 'att id/with+chars?')
    raw_path = requests[-1].url.raw_path.decode('ascii')
    assert raw_path.startswith('/gmail/v1/users/me/messages/msg1/attachments/')
    assert 'with+chars' not in raw_path
    assert '%' in raw_path
    assert '?' not in raw_path.split('attachments/')[1]


def test_list_messages_params():
    captured = []

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        captured.append(dict(urllib.parse.parse_qsl(request.url.query.decode())))
        return ok({'messages': []})

    client, _, _ = make_client(handler)
    client.list_messages('from:a', 7, 'tok', True)
    params = captured[0]
    assert params['q'] == 'from:a'
    assert params['maxResults'] == '7'
    assert params['includeSpamTrash'] == 'true'
    assert params['pageToken'] == 'tok'


def test_metadata_get_uses_metadata_headers():
    captured = []

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        captured.append(urllib.parse.parse_qsl(request.url.query.decode()))
        return ok({'id': 'm1'})

    client, _, _ = make_client(handler)
    client.get_message('m1', format='metadata')
    params = captured[0]
    assert ('format', 'metadata') in params
    headers = [v for k, v in params if k == 'metadataHeaders']
    assert headers == ['From', 'To', 'Subject', 'Date', 'Message-ID']


def test_no_secrets_in_info_logs(caplog):
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return ok({'emailAddress': 'a@b.c'})

    client, _, _ = make_client(handler)
    with caplog.at_level('INFO'):
        client.list_messages('secret query text', 5, None, False)
    for marker in ('synthetic-secret', 'synthetic-refresh', 'secret query text', 'fake-access-token'):
        assert marker not in caplog.text


def test_concurrent_refresh_single_lock():
    tokens = []

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            tokens.append(request)
            return token_response()
        return ok({'ok': True})

    client, _, _ = make_client(handler)
    errors = []

    def work():
        try:
            client.get_profile()
        except GmailError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(tokens) == 1


class _BigStream(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        yield b' ' * (70 * 1024)
        raise AssertionError('consumed beyond cap')

    def close(self):
        self.closed = True


def test_token_response_not_eagerly_buffered():
    stream = _BigStream()

    def handler(request):
        return httpx.Response(200, stream=stream)

    client, requests, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'too_large'
    assert stream.iterations == 1
    assert stream.closed is True
    assert len(requests) == 1


def test_token_error_response_not_eagerly_buffered():
    stream = _BigStream()

    def handler(request):
        return httpx.Response(400, stream=stream)

    client, requests, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'authorization_required'
    assert stream.iterations == 1
    assert stream.closed is True
    assert len(requests) == 1


def test_token_503_retries_then_succeeds():
    state = {'posts': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            state['posts'] += 1
            if state['posts'] == 1:
                return httpx.Response(503, json={})
            return token_response()
        return ok({'emailAddress': 'a@b.c'})

    client, _, sleeps = make_client(handler)
    assert client.get_profile()['emailAddress'] == 'a@b.c'
    assert state['posts'] == 2
    assert sleeps == [0.25]


def test_token_transport_timeout_exhaustion_unavailable():
    def handler(request):
        raise httpx.ReadTimeout('slow', request=request)

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'unavailable'


class _FailOnceStream(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield b'{'
        raise httpx.ReadError('mid-stream failure')

    def close(self):
        self.closed = True


def test_api_stream_read_error_retries():
    state = {'gets': 0}
    stream = _FailOnceStream()

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        state['gets'] += 1
        if state['gets'] == 1:
            return httpx.Response(200, stream=stream)
        return ok({'ok': True})

    client, _, _ = make_client(handler)
    assert client.get_profile() == {'ok': True}
    assert state['gets'] == 2
    assert stream.closed is True


def test_401_forced_refresh_outage_stays_unavailable():
    state = {'posts': 0, 'gets': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            state['posts'] += 1
            if state['posts'] == 1:
                return token_response()
            return httpx.Response(503, json={})
        state['gets'] += 1
        return httpx.Response(401, json={'error': {'code': 401}})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'unavailable'
    assert state['posts'] == 4


def test_token_redirect_not_followed_with_redirect_client():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == 'oauth2.googleapis.com':
            return httpx.Response(302, headers={'location': 'https://evil.example/t'})
        return ok({})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, follow_redirects=True, timeout=10.0)
    client = GmailClient(credentials=CREDS, http_client=http,
                         sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'
    assert all('evil.example' not in str(r.url) for r in requests)


def test_use_environment_false_ignores_env(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return ok({})

    monkeypatch.setenv(GMAIL_ENV, json.dumps(CREDS))
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)
    client = GmailClient(credentials=None, use_environment=False, http_client=http)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'not_configured'
    assert requests == []


def test_injected_json_string_credentials(monkeypatch):
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response()
        return ok({'emailAddress': 'a@b.c'})

    client, requests, _ = make_client(handler, credentials=json.dumps(CREDS))
    assert client.get_profile()['emailAddress'] == 'a@b.c'


def test_validate_credentials_pure_and_sanitized():
    from john_agent.gmail_client import validate_credentials

    assert validate_credentials(json.dumps(CREDS))['client_id'] == \
        'synthetic.apps.googleusercontent.com'
    assert validate_credentials(dict(CREDS))['refresh_token'] == 'synthetic-refresh'
    for bad in ('MARKER\ud800unpaired', '{"client_id": "x\x00y", "type": "authorized_user"',
                '[' * 20000, json.dumps({**CREDS, 'client_id': 'MARKER\x00BAD'})):
        with pytest.raises(GmailError) as exc:
            validate_credentials(bad)
        assert 'MARKER' not in str(exc.value)
        assert exc.value.code in ('invalid_credentials', 'not_configured')
    with pytest.raises(GmailError) as exc:
        validate_credentials('   ')
    assert exc.value.code == 'not_configured'
    with pytest.raises(GmailError) as exc:
        validate_credentials(None)
    assert exc.value.code == 'not_configured'


def test_compose_credentials_accepted_and_exposed():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response(scope=f'{GMAIL_SCOPE} {GMAIL_COMPOSE_SCOPE}')
        return ok({'emailAddress': 'a@b.c'})

    client, _, _ = make_client(handler, credentials=COMPOSE_CREDS)
    assert client.get_profile()['emailAddress'] == 'a@b.c'
    assert client.scopes == frozenset({GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE})


def test_compose_refresh_requires_full_scope_set():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response(scope=GMAIL_SCOPE)
        return ok({})

    client, _, _ = make_client(handler, credentials=COMPOSE_CREDS)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'


def test_readonly_refresh_rejects_compose_scope():
    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            return token_response(scope=f'{GMAIL_SCOPE} {GMAIL_COMPOSE_SCOPE}')
        return ok({})

    client, _, _ = make_client(handler)
    with pytest.raises(GmailError) as exc:
        client.get_profile()
    assert exc.value.code == 'invalid_response'


def test_create_draft_posts_json_and_retries_401():
    state = {'posts': 0, 'tokens': 0}

    def handler(request):
        if request.url.host == 'oauth2.googleapis.com':
            state['tokens'] += 1
            return token_response(
                scope=f'{GMAIL_SCOPE} {GMAIL_COMPOSE_SCOPE}',
                access_token=f'tok{state["tokens"]}')
        state['posts'] += 1
        if request.headers['Authorization'] == 'Bearer tok1':
            return httpx.Response(401, json={'error': {'code': 401}})
        return ok({'id': 'd1', 'message': {'id': 'm9', 'threadId': 't9'}})

    client, requests, _ = make_client(handler, credentials=COMPOSE_CREDS)
    out = client.create_draft({'raw': 'abc', 'threadId': 't9'})
    assert out['id'] == 'd1'
    draft_reqs = [r for r in requests if r.url.path == '/gmail/v1/users/me/drafts']
    assert len(draft_reqs) == 2
    assert all(r.method == 'POST' for r in draft_reqs)
    assert json.loads(draft_reqs[-1].content) == {'message': {'raw': 'abc', 'threadId': 't9'}}
    assert state['tokens'] == 2


def test_create_draft_validates_message_shape():
    client, requests, _ = make_client(lambda r: ok({}), credentials=COMPOSE_CREDS)
    for bad in (None, 'x', {}, {'raw': 5}):
        with pytest.raises(GmailError) as exc:
            client.create_draft(bad)
        assert exc.value.code == 'invalid_input'
    assert requests == []


def test_client_never_sends():
    import inspect

    import john_agent.gmail_client as module

    assert [n for n in dir(GmailClient) if 'send' in n.lower()] == []
    source = inspect.getsource(module)
    assert '/send' not in source
    assert 'messages.send' not in source
    assert 'drafts.send' not in source


def test_close_idempotent():
    client, _, _ = make_client(lambda r: ok({}))
    client.close()
    client.close()
