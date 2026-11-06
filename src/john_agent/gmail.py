"""Lazy, draft-only Gmail access. No delivery operations are exposed."""

from __future__ import annotations

import base64
import mimetypes
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

SCOPES = ['https://www.googleapis.com/auth/gmail.compose',
          'https://www.googleapis.com/auth/gmail.readonly']
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
STUBS = {'.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform', '.gmap', '.gsite', '.gshortcut'}


class GmailError(Exception):
    """An error safe to return to an MCP caller."""


def login(credentials: Path, token: Path) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    auth = InstalledAppFlow.from_client_secrets_file(str(credentials), SCOPES)
    creds = auth.run_local_server(port=0)
    _write_token(token, creds)


def _write_token(token, creds):
    import os

    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(creds.to_json())


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


class GmailClient:
    def __init__(self, reader, *, credentials=None, token=None, service=None,
                 service_factory=None, attachment_roots=(), source_root=None):
        self.reader = reader
        self.credentials = credentials
        self.token = token
        self._service = service
        self._factory = service_factory
        self.roots = [Path(p).resolve() for p in attachment_roots]
        self.source_root = Path(source_root).resolve() if source_root else None

    @property
    def service(self):
        if self._service is None:
            if self._factory:
                self._service = self._factory()
            else:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                token_path = self.token

                class PersistedCredentials(Credentials):
                    def refresh(self, request):
                        super().refresh(request)
                        _write_token(token_path, self)

                creds = PersistedCredentials.from_authorized_user_file(str(self.token), SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                if not creds.valid:
                    raise GmailError('Gmail authorization required; run gmail-login.')
                self._service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        return self._service

    def _get(self, message_id):
        return self.service.users().messages().get(userId='me', id=message_id, format='full').execute()

    @staticmethod
    def _headers(message):
        return {h['name'].lower(): h['value'] for h in message.get('payload', {}).get('headers', [])}

    def search_emails(self, query: str, limit: int = 10) -> dict:
        if not 1 <= limit <= 100:
            raise GmailError('limit must be between 1 and 100.')
        page = self.service.users().messages().list(userId='me', q=query, maxResults=limit).execute()
        results = []
        for item in page.get('messages', []):
            msg = self._get(item['id'])
            headers = self._headers(msg)
            results.append(dict(id=msg['id'], thread_id=msg.get('threadId'),
                                snippet=msg.get('snippet', ''),
                                **{key: headers.get(key, '') for key in ('from', 'to', 'subject', 'date')}))
        return {'emails': results}

    def read_email(self, message_id: str, max_chars: int = 12000) -> dict:
        if not 1 <= max_chars <= 100000:
            raise GmailError('max_chars must be between 1 and 100000.')
        msg = self._get(message_id)
        plain, html, attachments = [], [], []

        def visit(part):
            if part.get('filename'):
                attachments.append({'name': part['filename'], 'size': part.get('body', {}).get('size', 0)})
                return
            data = part.get('body', {}).get('data')
            if data and part.get('mimeType') in ('text/plain', 'text/html'):
                decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
                (plain if part['mimeType'] == 'text/plain' else html).append(decoded)
            for child in part.get('parts', []):
                visit(child)

        visit(msg.get('payload', {}))
        text = '\n'.join(plain)
        if not plain:
            parser = _HTMLText()
            parser.feed('\n'.join(html))
            text = ' '.join(parser.text)
        return {'id': msg['id'], 'thread_id': msg.get('threadId'), 'headers': self._headers(msg),
                'body': text[:max_chars], 'truncated': len(text) > max_chars, 'attachments': attachments}

    @staticmethod
    def _contained(root, name):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise GmailError('Attachment must be a relative path inside an allowed root.')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise GmailError('Attachment is not a regular file inside an allowed root.')
        return path

    def _attachments(self, attachments, document_attachments):
        paths = []
        for name in attachments or []:
            for root in self.roots:
                try:
                    path = self._contained(root, name)
                    break
                except GmailError:
                    continue
            else:
                raise GmailError('Attachment is not a regular file inside an allowed attachment root.')
            paths.append(path)
        if document_attachments:
            if self.source_root is None:
                raise GmailError('Document attachments require --source-root.')
            ids, offset = set(), 0
            while True:
                page = self.reader.list_documents(offset=offset, limit=100)
                ids.update(doc['document_id'] for doc in page['documents'])
                offset = page['next_offset']
                if offset is None:
                    break
            for name in document_attachments:
                if name not in ids:
                    raise GmailError('Unknown document attachment id.')
                if Path(name).suffix.lower() in STUBS:
                    raise GmailError('Google Drive stubs cannot be attached; supply a local exported file.')
                paths.append(self._contained(self.source_root, name))
        result, total = [], 0
        for path in paths:
            if total + path.stat().st_size > MAX_ATTACHMENT_BYTES:
                raise GmailError('Total attachments exceed the 25 MB size limit.')
            with path.open('rb') as stream:
                data = stream.read(MAX_ATTACHMENT_BYTES - total + 1)
            total += len(data)
            if total > MAX_ATTACHMENT_BYTES:
                raise GmailError('Total attachments exceed the 25 MB size limit.')
            result.append((path.name, data))
        return result

    def _draft(self, to, subject, body, cc, bcc, attachments, document_attachments,
               thread_id=None, in_reply_to=None, references=None):
        msg = EmailMessage()
        for key, value in [('To', to), ('Subject', subject), ('Cc', cc), ('Bcc', bcc),
                           ('In-Reply-To', in_reply_to), ('References', references)]:
            if value:
                msg[key] = value
        msg.set_content(body)
        for name, data in self._attachments(attachments, document_attachments):
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            main, sub = mime.split('/', 1)
            msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
        message = {'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')}
        if thread_id:
            message['threadId'] = thread_id
        draft = self.service.users().drafts().create(userId='me', body={'message': message}).execute()
        return {'draft_id': draft['id'], 'message_id': draft['message']['id'],
                'thread_id': draft['message'].get('threadId'),
                'gmail_link': 'https://mail.google.com/mail/u/0/#drafts',
                'status': 'NOT sent. Review and send manually in Gmail.'}

    def create_draft(self, to: str, subject: str, body: str, cc: str | None = None,
                     bcc: str | None = None, attachments: list[str] | None = None,
                     document_attachments: list[str] | None = None) -> dict:
        return self._draft(to, subject, body, cc, bcc, attachments, document_attachments)

    def create_reply_draft(self, message_id: str, body: str, reply_all: bool = False,
                           cc: str | None = None, bcc: str | None = None,
                           attachments: list[str] | None = None,
                           document_attachments: list[str] | None = None) -> dict:
        original = self._get(message_id)
        headers = self._headers(original)
        to = headers.get('reply-to') or headers.get('from', '')
        if reply_all:
            own = self.service.users().getProfile(userId='me').execute()['emailAddress'].lower()
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
        subject = headers.get('subject', '')
        if not subject.lower().startswith('re:'):
            subject = 'Re: ' + subject
        message_id_header = headers.get('message-id')
        references = ' '.join(filter(None, [headers.get('references'), message_id_header]))
        return self._draft(to, subject, body, cc, bcc, attachments, document_attachments,
                           original.get('threadId'), message_id_header, references)
