"""Draft-only Gmail composition. Drafts are created but never sent."""

from __future__ import annotations

import base64
import mimetypes
import threading
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable

from john_agent.gmail import _check_message_id, _headers_of
from john_agent.gmail_client import (
    GMAIL_COMPOSE_SCOPE,
    GmailClient,
    GmailError,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_FIELD_BYTES = 16 * 1024
_MAX_BODY_BYTES = 1024 * 1024
_STUBS = {'.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform', '.gmap', '.gsite',
          '.gshortcut'}


class GmailDrafter:
    def __init__(self, *, client_factory: Callable[[], GmailClient] | None = None,
                 reader=None, attachment_roots=(), source_root=None):
        self._client_factory = client_factory or GmailClient
        self._reader = reader
        self.roots = [Path(p).resolve() for p in attachment_roots]
        self.source_root = Path(source_root).resolve() if source_root else None
        self._client: GmailClient | None = None
        self._account_id: str | None = None
        self._lock = threading.RLock()

    def _get_client(self) -> GmailClient:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory()
            scopes = getattr(self._client, 'scopes', None)
            if isinstance(scopes, frozenset) and GMAIL_COMPOSE_SCOPE not in scopes:
                raise GmailError('drafts_disabled')
            return self._client

    def _get_account(self, client: GmailClient) -> str:
        with self._lock:
            if self._account_id is None:
                profile = client.get_profile()
                email = profile.get('emailAddress')
                if not isinstance(email, str) or not email or len(email) > 320:
                    raise GmailError('invalid_response')
                self._account_id = email
            return self._account_id

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._account_id = None

    @staticmethod
    def _contained(root: Path, name: Any) -> Path:
        if not isinstance(name, str) or not name:
            raise GmailError('attachment_rejected')
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise GmailError('attachment_rejected')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise GmailError('attachment_rejected')
        return path

    def _attachments(self, attachments, document_attachments) -> list[tuple[str, bytes]]:
        paths = []
        for name in attachments or []:
            for root in self.roots:
                try:
                    path = self._contained(root, name)
                    break
                except GmailError:
                    continue
            else:
                raise GmailError('attachment_rejected')
            paths.append(path)
        if document_attachments:
            if not isinstance(document_attachments, list):
                raise GmailError('invalid_input')
            if self.source_root is None or self._reader is None:
                raise GmailError('attachment_rejected')
            ids, offset = set(), 0
            while True:
                page = self._reader.list_documents(offset=offset, limit=100)
                for doc in page.get('documents', []):
                    doc_id = doc.get('document_id') if isinstance(doc, dict) else None
                    if isinstance(doc_id, str):
                        ids.add(doc_id)
                offset = page.get('next_offset')
                if offset is None:
                    break
            for name in document_attachments:
                if not isinstance(name, str) or name not in ids:
                    raise GmailError('attachment_rejected')
                if Path(name).suffix.lower() in _STUBS:
                    raise GmailError('attachment_rejected')
                paths.append(self._contained(self.source_root, name))
        result, total = [], 0
        for path in paths:
            if total + path.stat().st_size > MAX_ATTACHMENT_BYTES:
                raise GmailError('too_large')
            with path.open('rb') as stream:
                data = stream.read(MAX_ATTACHMENT_BYTES - total + 1)
            total += len(data)
            if total > MAX_ATTACHMENT_BYTES:
                raise GmailError('too_large')
            result.append((path.name, data))
        return result

    @staticmethod
    def _check_header(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or '\x00' in value or '\r' in value \
                or '\n' in value \
                or len(value.encode('utf-8', errors='ignore')) > _MAX_FIELD_BYTES:
            raise GmailError('invalid_response')
        return value

    @staticmethod
    def _check_field(name: str, value: Any, *, required: bool) -> str | None:
        if value is None:
            if required:
                raise GmailError('invalid_input')
            return None
        if not isinstance(value, str) or not value \
                or len(value.encode('utf-8', errors='ignore')) > _MAX_FIELD_BYTES \
                or '\x00' in value or '\r' in value or '\n' in value:
            raise GmailError('invalid_input')
        return value

    def _draft(self, to, subject, body, cc, bcc, attachments, document_attachments,
               thread_id=None, in_reply_to=None, references=None):
        client = self._get_client()
        msg = EmailMessage()
        for key, value in [('To', to), ('Subject', subject), ('Cc', cc), ('Bcc', bcc),
                           ('In-Reply-To', in_reply_to), ('References', references)]:
            if value:
                msg[key] = value
        msg.set_content(body)
        for name, data in self._attachments(attachments, document_attachments):
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            maintype, subtype = mime.split('/', 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        message: dict[str, Any] = {
            'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')}
        if thread_id:
            message['threadId'] = thread_id
        draft = client.create_draft(message)
        draft_message = draft.get('message')
        if not isinstance(draft.get('id'), str) or not isinstance(draft_message, dict) \
                or not isinstance(draft_message.get('id'), str):
            raise GmailError('invalid_response')
        thread = draft_message.get('threadId')
        return {'draft_id': draft['id'], 'message_id': draft_message['id'],
                'thread_id': thread if isinstance(thread, str) else None,
                'gmail_link': 'https://mail.google.com/mail/u/0/#drafts',
                'status': 'NOT sent. Review and send manually in Gmail.'}

    def create_draft(self, to: str, subject: str, body: str, cc: str | None = None,
                     bcc: str | None = None, attachments: list[str] | None = None,
                     document_attachments: list[str] | None = None) -> dict[str, Any]:
        to = self._check_field('to', to, required=True)
        subject = self._check_field('subject', subject, required=True)
        cc = self._check_field('cc', cc, required=False)
        bcc = self._check_field('bcc', bcc, required=False)
        if not isinstance(body, str) or len(body.encode('utf-8', errors='ignore')) > _MAX_BODY_BYTES:
            raise GmailError('invalid_input')
        return self._draft(to, subject, body, cc, bcc, attachments, document_attachments)

    def create_reply_draft(self, message_id: str, body: str, reply_all: bool = False,
                           cc: str | None = None, bcc: str | None = None,
                           attachments: list[str] | None = None,
                           document_attachments: list[str] | None = None) -> dict[str, Any]:
        _check_message_id(message_id)
        if not isinstance(body, str) or len(body.encode('utf-8', errors='ignore')) > _MAX_BODY_BYTES:
            raise GmailError('invalid_input')
        if not isinstance(reply_all, bool):
            raise GmailError('invalid_input')
        cc = self._check_field('cc', cc, required=False)
        bcc = self._check_field('bcc', bcc, required=False)
        client = self._get_client()
        original = client.get_message(message_id, format='full')
        if original.get('id') != message_id:
            raise GmailError('invalid_response')
        headers = _headers_of(original.get('payload') or {})
        to = self._check_header(headers.get('reply-to') or headers.get('from', ''))
        if reply_all:
            own = self._get_account(client).lower()
            seen = {own}

            def unique(values):
                result = []
                for _, address in getaddresses(values):
                    if address and address.lower() not in seen:
                        seen.add(address.lower())
                        result.append(address)
                return ', '.join(result)

            to = unique([to, headers.get('to', '')])
            cc = unique([headers.get('cc', ''), cc or ''])
        to = self._check_header(to)
        cc = self._check_header(cc)
        if not to:
            raise GmailError('invalid_response')
        subject = self._check_header(headers.get('subject', ''))
        if not subject.lower().startswith('re:'):
            subject = 'Re: ' + subject
        message_id_header = self._check_header(headers.get('message-id'))
        references = self._check_header(' '.join(
            part for part in (headers.get('references'), message_id_header) if part))
        return self._draft(to, subject, body, cc, bcc, attachments,
                           document_attachments, original.get('threadId'),
                           message_id_header, references or None)
