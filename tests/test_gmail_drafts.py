from __future__ import annotations

import base64
import email.parser
import email.policy
from pathlib import Path

import pytest

import john_agent.gmail_drafts as gmail_drafts
from john_agent.gmail_client import GMAIL_COMPOSE_SCOPE, GMAIL_SCOPE, GmailError
from john_agent.gmail_drafts import GmailDrafter

ACCOUNT = 'me@example.com'
BOTH_SCOPES = frozenset({GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE})


class FakeClient:
    def __init__(self, scopes=BOTH_SCOPES):
        self.calls = []
        self.messages = {}
        self.drafts = []
        self.scopes = scopes
        self.closed = False

    def get_profile(self):
        self.calls.append(('profile',))
        return {'emailAddress': ACCOUNT}

    def get_message(self, message_id, format='full'):
        self.calls.append(('get_message', message_id, format))
        return self.messages[message_id]

    def create_draft(self, message):
        self.calls.append(('create_draft', message))
        self.drafts.append(message)
        return {'id': 'd1', 'message': {'id': 'm9', 'threadId': message.get('threadId')}}

    def close(self):
        self.closed = True


class FakeReader:
    def __init__(self, ids):
        self.ids = list(ids)
        self.offsets = []

    def list_documents(self, offset=0, limit=100):
        self.offsets.append(offset)
        docs = [{'document_id': i} for i in self.ids[offset:offset + limit]]
        next_offset = offset + limit if offset + limit < len(self.ids) else None
        return {'documents': docs, 'next_offset': next_offset}


def make_message(mid, headers, thread='t1'):
    return {'id': mid, 'threadId': thread, 'internalDate': '1',
            'payload': {'partId': '', 'mimeType': 'text/plain',
                        'headers': [{'name': n, 'value': v} for n, v in headers],
                        'body': {'size': 0}}}


ORIGINAL = make_message('m1', [
    ('From', 'Alice <alice@example.com>'),
    ('Reply-To', 'reply-alice@example.com'),
    ('To', f'{ACCOUNT}, bob@example.com'),
    ('Cc', 'carol@example.com'),
    ('Subject', 'Quarterly report'),
    ('Message-ID', '<orig@x>'),
    ('References', '<older@x>'),
])


def drafter_for(client, roots=(), source_root=None, reader=None):
    return GmailDrafter(client_factory=lambda: client, reader=reader,
                        attachment_roots=roots, source_root=source_root)


def decode_raw(message):
    raw = base64.urlsafe_b64decode(message['raw'] + '=' * (-len(message['raw']) % 4))
    return email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)


def test_create_draft_builds_plain_mime_and_result():
    client = FakeClient()
    out = drafter_for(client).create_draft(
        'to@example.com', 'Hello subject', 'line one\nline two',
        cc='cc@example.com', bcc='bcc@example.com')
    assert ('create_draft', client.drafts[0]) in client.calls
    msg = decode_raw(client.drafts[0])
    assert msg['To'] == 'to@example.com'
    assert msg['Subject'] == 'Hello subject'
    assert msg['Cc'] == 'cc@example.com'
    assert msg['Bcc'] == 'bcc@example.com'
    assert msg.get_body(('plain',)).get_content() == 'line one\nline two\n'
    assert 'threadId' not in client.drafts[0]
    assert out['draft_id'] == 'd1'
    assert out['message_id'] == 'm9'
    assert out['gmail_link'] == 'https://mail.google.com/mail/u/0/#drafts'
    assert 'NOT sent' in out['status']


def test_create_draft_attaches_root_file(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'note.txt').write_bytes(b'attachment bytes')
    client = FakeClient()
    out = drafter_for(client, roots=[root]).create_draft(
        'to@example.com', 's', 'body', attachments=['note.txt'])
    assert out['draft_id'] == 'd1'
    msg = decode_raw(client.drafts[0])
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == 'note.txt'
    assert attachments[0].get_payload(decode=True) == b'attachment bytes'


def test_reply_draft_headers_and_thread():
    client = FakeClient()
    client.messages['m1'] = ORIGINAL
    out = drafter_for(client).create_reply_draft('m1', 'reply body')
    sent = client.drafts[0]
    assert sent['threadId'] == 't1'
    msg = decode_raw(sent)
    assert msg['To'] == 'reply-alice@example.com'
    assert msg['Subject'] == 'Re: Quarterly report'
    assert msg['In-Reply-To'] == '<orig@x>'
    assert msg['References'] == '<older@x> <orig@x>'
    assert out['thread_id'] == 't1'


def test_reply_all_dedupes_own_address_and_merges_cc():
    client = FakeClient()
    client.messages['m1'] = ORIGINAL
    drafter_for(client).create_reply_draft(
        'm1', 'body', reply_all=True, cc='extra@example.com')
    msg = decode_raw(client.drafts[0])
    assert msg['To'] == 'reply-alice@example.com, bob@example.com'
    assert msg['Cc'] == 'carol@example.com, extra@example.com'
    assert ACCOUNT not in msg['To']
    assert ACCOUNT not in msg['Cc']


def test_reply_subject_already_re_not_doubled():
    client = FakeClient()
    client.messages['m1'] = make_message('m1', [
        ('From', 'a@example.com'), ('Subject', 'Re: ping'), ('Message-ID', '<x@y>')])
    drafter_for(client).create_reply_draft('m1', 'b')
    assert decode_raw(client.drafts[0])['Subject'] == 'Re: ping'


def test_document_attachment_via_manifest(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'doc.pdf').write_bytes(b'%PDF-fake')
    reader = FakeReader(['docs/doc.pdf'])
    (source / 'docs').mkdir()
    (source / 'docs' / 'doc.pdf').write_bytes(b'%PDF-fake')
    client = FakeClient()
    drafter_for(client, source_root=source, reader=reader).create_draft(
        'to@example.com', 's', 'b', document_attachments=['docs/doc.pdf'])
    msg = decode_raw(client.drafts[0])
    attachments = list(msg.iter_attachments())
    assert attachments[0].get_filename() == 'doc.pdf'
    assert reader.offsets == [0]


@pytest.mark.parametrize('name', [
    '/abs/path/file.txt', '../outside.txt', 'a/../../b.txt', '',
])
def test_attachment_traversal_rejected(tmp_path, name):
    root = tmp_path / 'root'
    root.mkdir()
    client = FakeClient()
    with pytest.raises(GmailError) as exc:
        drafter_for(client, roots=[root]).create_draft(
            't@x.c', 's', 'b', attachments=[name])
    assert exc.value.code == 'attachment_rejected'


def test_attachment_symlink_escape_and_directory_rejected(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret')
    (root / 'link.txt').symlink_to(outside)
    (root / 'subdir').mkdir()
    client = FakeClient()
    for name in ('link.txt', 'subdir', 'missing.txt'):
        with pytest.raises(GmailError) as exc:
            drafter_for(client, roots=[root]).create_draft(
                't@x.c', 's', 'b', attachments=[name])
        assert exc.value.code == 'attachment_rejected'
    assert client.drafts == []


def test_attachment_no_configured_root_rejected():
    client = FakeClient()
    with pytest.raises(GmailError) as exc:
        drafter_for(client).create_draft('t@x.c', 's', 'b', attachments=['f.txt'])
    assert exc.value.code == 'attachment_rejected'


def test_document_attachment_rejections(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'sheet.gsheet').write_text('stub')
    reader = FakeReader(['sheet.gsheet', 'real.pdf'])
    client = FakeClient()
    drafter = drafter_for(client, source_root=source, reader=reader)
    for doc_id in ('unknown.pdf', 'sheet.gsheet', 'real.pdf'):
        with pytest.raises(GmailError) as exc:
            drafter.create_draft('t@x.c', 's', 'b',
                                 document_attachments=[doc_id])
        assert exc.value.code == 'attachment_rejected'


def test_document_attachment_requires_source_root():
    reader = FakeReader(['doc.pdf'])
    client = FakeClient()
    with pytest.raises(GmailError) as exc:
        drafter_for(client, reader=reader).create_draft(
            't@x.c', 's', 'b', document_attachments=['doc.pdf'])
    assert exc.value.code == 'attachment_rejected'


def test_attachment_total_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_drafts, 'MAX_ATTACHMENT_BYTES', 10)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'big.bin').write_bytes(b'x' * 20)
    client = FakeClient()
    with pytest.raises(GmailError) as exc:
        drafter_for(client, roots=[root]).create_draft(
            't@x.c', 's', 'b', attachments=['big.bin'])
    assert exc.value.code == 'too_large'


def test_error_messages_never_echo_paths(tmp_path):
    root = tmp_path / 'MARKERROOT'
    root.mkdir()
    outside = tmp_path / 'MARKERFILE.txt'
    outside.write_text('data')
    (root / 'MARKERLINK.txt').symlink_to(outside)
    client = FakeClient()
    drafter = drafter_for(client, roots=[root])
    for name in ('MARKERLINK.txt', str(outside), '../MARKERFILE.txt'):
        with pytest.raises(GmailError) as exc:
            drafter.create_draft('t@x.c', 's', 'b', attachments=[name])
        assert 'MARKER' not in str(exc.value)


def test_input_validation():
    client = FakeClient()
    drafter = drafter_for(client)
    for kwargs in (
        {'to': '', 'subject': 's', 'body': 'b'},
        {'to': 't@x.c', 'subject': '', 'body': 'b'},
        {'to': 't@x.c', 'subject': 's', 'body': 'b', 'cc': 'bad\nline'},
        {'to': 5, 'subject': 's', 'body': 'b'},
    ):
        with pytest.raises(GmailError) as exc:
            drafter.create_draft(**kwargs)
        assert exc.value.code == 'invalid_input'
    assert client.drafts == []
    with pytest.raises(GmailError) as exc:
        drafter.create_reply_draft('../bad', 'b')
    assert exc.value.code == 'invalid_input'
    with pytest.raises(GmailError) as exc:
        drafter.create_reply_draft('m1', 'b', reply_all='yes')
    assert exc.value.code == 'invalid_input'


def test_readonly_credentials_rejected():
    client = FakeClient(scopes=frozenset({GMAIL_SCOPE}))
    with pytest.raises(GmailError) as exc:
        drafter_for(client).create_draft('t@x.c', 's', 'b')
    assert exc.value.code == 'drafts_disabled'
    assert client.drafts == []


def test_close_closes_client():
    client = FakeClient()
    drafter = drafter_for(client)
    drafter.create_draft('t@x.c', 's', 'b')
    drafter.close()
    assert client.closed is True
    drafter.close()
