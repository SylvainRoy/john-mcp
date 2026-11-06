import asyncio
import base64
import datetime
import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response
from mcp.shared.memory import create_connected_server_and_client_session
from mdtodb import CorpusReader

from john_agent.gmail import GmailClient, GmailError
from john_agent.server import create_server
from test_server import make_tree


class FakeService:
    def __init__(self):
        self.created = []
        self.result = {}
        self.message = {'id': 'original', 'threadId': 'thread', 'snippet': 'snippet', 'payload': {
            'headers': [{'name': k, 'value': v} for k, v in {
                'From': 'sender@example.com', 'Reply-To': 'reply@example.com',
                'To': 'me@example.com, other@example.com', 'Cc': 'third@example.com',
                'Subject': 'Hello', 'Message-ID': '<original>', 'References': '<older>'}.items()],
            'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'original body').decode()}}}

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def get(self, **kwargs):
        self.result = self.message
        return self

    def list(self, **kwargs):
        self.result = {'messages': [{'id': 'original'}]}
        return self

    def getProfile(self, **kwargs):
        self.result = {'emailAddress': 'me@example.com'}
        return self

    def create(self, **kwargs):
        self.created.append(kwargs['body']['message'])
        self.result = {'id': 'draft', 'message': {'id': 'new', 'threadId': 'thread'}}
        return self

    def execute(self):
        return self.result


@pytest.fixture
def client(tmp_path):
    reader = CorpusReader(make_tree(tmp_path, {'file.pdf': 'text', 'stub.gdoc': 'stub'}))
    return GmailClient(reader, service=FakeService(), attachment_roots=[tmp_path], source_root=tmp_path)


def parsed(client):
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(client.service.created[-1]['raw']))


def test_new_draft_mime(client, tmp_path):
    (tmp_path / 'file.pdf').write_bytes(b'%PDF synthetic')
    result = client.create_draft('to@example.com', 'subject', 'body', cc='cc@example.com',
                                 bcc='bcc@example.com', attachments=['file.pdf'])
    msg = parsed(client)
    assert msg['To'] == 'to@example.com'
    assert msg['Cc'] == 'cc@example.com'
    assert msg['Bcc'] == 'bcc@example.com'
    assert msg['Subject'] == 'subject'
    assert msg.get_body().get_content() == 'body\n'
    attachment = list(msg.iter_attachments())[0]
    assert attachment.get_payload(decode=True) == b'%PDF synthetic'
    assert attachment.get_content_type() == 'application/pdf'
    assert 'threadId' not in client.service.created[-1]
    assert result['draft_id'] == 'draft' and 'NOT sent' in result['status']


def test_reply(client):
    client.create_reply_draft('original', 'response', reply_all=True, cc='extra@example.com')
    msg = parsed(client)
    assert msg['To'] == 'reply@example.com, other@example.com'
    assert msg['Cc'] == 'third@example.com, extra@example.com'
    assert msg['In-Reply-To'] == '<original>'
    assert msg['References'] == '<older> <original>'
    assert msg['Subject'] == 'Re: Hello'
    assert msg.get_content() == 'response\n'
    assert client.service.created[-1]['threadId'] == 'thread'
    client.service.message['payload']['headers'] = [{'name': 'From', 'value': 'from@example.com'},
                                                  {'name': 'Subject', 'value': 'RE: Existing'}]
    client.create_reply_draft('original', '')
    assert parsed(client)['To'] == 'from@example.com'
    assert parsed(client)['Subject'] == 'RE: Existing'


@pytest.mark.parametrize('name', ['../outside', '/etc/passwd', 'missing'])
def test_escape(client, name):
    with pytest.raises(GmailError, match='allowed attachment root'):
        client.create_draft('a@b', 's', '', attachments=[name])
    assert not client.service.created


def test_symlink_and_no_root(client, tmp_path):
    outside = tmp_path.parent / 'outside'
    outside.write_bytes(b'fake')
    (tmp_path / 'link').symlink_to(outside)
    with pytest.raises(GmailError):
        client.create_draft('a@b', 's', '', attachments=['link'])
    client.roots = []
    with pytest.raises(GmailError):
        client.create_draft('a@b', 's', '', attachments=['file.pdf'])


def test_size_cap(client, tmp_path):
    with (tmp_path / 'large').open('wb') as f:
        f.truncate(25 * 1024 * 1024 + 1)
    with pytest.raises(GmailError, match='25 MB'):
        client.create_draft('a@b', 's', '', attachments=['large'])
    assert not client.service.created


def test_document_attachments(client, tmp_path):
    (tmp_path / 'file.pdf').write_bytes(b'original not markdown')
    client.create_draft('a@b', 's', '', document_attachments=['file.pdf'])
    assert list(parsed(client).iter_attachments())[0].get_payload(decode=True) == b'original not markdown'
    for name, error in [('unknown.pdf', 'Unknown'), ('stub.gdoc', 'stubs')]:
        with pytest.raises(GmailError, match=error):
            client.create_draft('a@b', 's', '', document_attachments=[name])
    client.source_root = None
    with pytest.raises(GmailError, match='source-root'):
        client.create_draft('a@b', 's', '', document_attachments=['file.pdf'])


def test_read_search_empty_html(client):
    assert client.search_emails('test')['emails'][0]['subject'] == 'Hello'
    assert client.read_email('original', 4)['body'] == 'orig'
    assert client.read_email('original', 4)['truncated']
    client.service.message['payload'] = {'parts': [
        {'mimeType': 'text/html', 'body': {'data': base64.urlsafe_b64encode(b'<p>Hello</p>').decode()}},
        {'filename': 'file.pdf', 'body': {'size': 10, 'attachmentId': 'not-downloaded'}}]}
    data = client.read_email('original')
    assert data['body'] == 'Hello'
    assert data['attachments'] == [{'name': 'file.pdf', 'size': 10}]
    client.service.message['payload'] = {}
    assert client.read_email('original')['body'] == ''
    for method, args in [(client.read_email, ('original', 0)), (client.search_emails, ('q', 101))]:
        with pytest.raises(GmailError):
            method(*args)


def test_registration_and_sanitization(client):
    async def go():
        async with create_connected_server_and_client_session(create_server(client.reader, client)) as session:
            tools = (await session.list_tools()).tools
            assert len(tools) == 9
            assert all('send' not in t.name.lower() for t in tools)
            for t in tools:
                if t.name in ('create_draft', 'create_reply_draft'):
                    assert not t.annotations.readOnlyHint
                    assert not t.annotations.destructiveHint
                    assert not t.annotations.idempotentHint
                    assert t.annotations.openWorldHint
            res = await session.call_tool('create_draft', {'to': 'a@b', 'subject': 's', 'body': ''})
            assert not res.isError
            def boom(**kwargs):
                raise HttpError(Response({'status': '403'}), b'credential SECRET path')
            client.service.get = boom
            res = await session.call_tool('read_email', {'message_id': 'original'})
            assert res.isError and 'SECRET' not in str(res.content)
    asyncio.run(go())


def test_lazy_factory(client):
    called = []
    lazy = GmailClient(client.reader, service_factory=lambda: called.append(True) or client.service)
    create_server(client.reader, lazy)
    assert not called
    lazy.read_email('original')
    assert called == [True]


def test_no_delivery_source_guard():
    for path in (Path(__file__).parents[1] / 'src').rglob('*.py'):
        source = path.read_text()
        assert not re.search(r'\.send\s*\(', source)
        assert not re.search(r'(?:drafts|messages)\s*\([^)]*\)\s*\.send\s*\(', source)
        assert not re.search(r'\b(?:drafts|messages)\s*\.send\s*\(', source)


def test_token_refresh_persisted(client, tmp_path, monkeypatch):
    from google.oauth2.credentials import Credentials
    import googleapiclient.discovery

    token = tmp_path / 'synthetic-token.json'
    token.write_text(json.dumps({'token': 'fake', 'refresh_token': 'fake-refresh',
                                'client_id': 'fake-client', 'client_secret': 'fake-secret',
                                'token_uri': 'https://example.invalid/token',
                                'expiry': '2000-01-01T00:00:00Z'}))
    captured = []

    def refresh(creds, request):
        creds.token = 'fake-refreshed'
        creds.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)

    def build(*args, **kwargs):
        captured.append(kwargs['credentials'])
        return client.service

    monkeypatch.setattr(Credentials, 'refresh', refresh)
    monkeypatch.setattr(googleapiclient.discovery, 'build', build)
    lazy = GmailClient(client.reader, token=token)
    lazy.read_email('original')
    assert json.loads(token.read_text())['token'] == 'fake-refreshed'
    assert token.stat().st_mode & 0o777 == 0o600
    captured[0].token = 'stale-fake'
    captured[0].refresh(None)
    assert json.loads(token.read_text())['token'] == 'fake-refreshed'
