"""Live read-only Gmail reader: bounded search, MIME body, and PDF evidence."""

from __future__ import annotations

import base64
import binascii
import email.message
import hashlib
import re
import threading
from datetime import datetime, timezone
from email.header import decode_header
from html.parser import HTMLParser
from typing import Any, Callable

from john_agent.gmail_client import GmailClient, GmailError
from john_agent.pdf_text import PdfTextError, extract_pdf_pages

_MESSAGE_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_HEADER_NAMES = {'from': 'from', 'to': 'to', 'subject': 'subject', 'date': 'date', 'message-id': 'message-id'}
_MAX_DEPTH = 32
_MAX_PARTS = 512
_MAX_BODY_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 1_000_000
_MAX_BODY_FETCHES = 32
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_PDF_PAGES = 100
_HEADER_CAP = 8192
_SKIP_TAGS = {'script', 'style', 'head', 'noscript', 'template'}
_BREAK_TAGS = {'br', 'p', 'div', 'li', 'tr', 'table', 'ul', 'ol', 'blockquote',
               'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'section', 'article', 'header', 'footer'}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_header_value(value: str) -> tuple[str, bool]:
    try:
        parts = decode_header(value)
    except Exception:
        return str(value)[:_HEADER_CAP], len(str(value)) > _HEADER_CAP
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            try:
                out.append(data.decode(charset or 'utf-8', errors='replace'))
            except (LookupError, ValueError):
                out.append(data.decode('utf-8', errors='replace'))
        else:
            out.append(data)
    text = ''.join(out)
    if len(text) > _HEADER_CAP:
        return text[:_HEADER_CAP], True
    return text, False


def _b64url_decode(data: str, decoded_cap: int) -> bytes:
    if not isinstance(data, str) or len(data) > (decoded_cap * 4 // 3) + 8:
        raise GmailError('too_large')
    padded = data + '=' * (-len(data) % 4)
    try:
        raw = base64.b64decode(padded.encode('ascii'), altchars=b'-_', validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise GmailError('invalid_response') from None
    if len(raw) > decoded_cap:
        raise GmailError('too_large')
    return raw


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.chunks.append('\n')

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BREAK_TAGS:
            self.chunks.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)

    def text(self) -> str:
        raw = ''.join(self.chunks)
        lines = [ln.strip() for ln in raw.split('\n')]
        out = []
        blank = 0
        for ln in lines:
            if ln:
                out.append(ln)
                blank = 0
            else:
                blank += 1
                if blank == 1 and out:
                    out.append('')
        return '\n'.join(out).strip('\n').replace('\xa0', ' ')


def _html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return parser.text()


def _headers_of(part: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    headers = part.get('headers')
    if headers is None:
        return out
    if not isinstance(headers, list):
        raise GmailError('invalid_response')
    for h in headers:
        if not isinstance(h, dict) or not isinstance(h.get('name'), str):
            raise GmailError('invalid_response')
        value = h.get('value')
        if value is None:
            value = ''
        if not isinstance(value, str):
            raise GmailError('invalid_response')
        out.setdefault(h['name'].lower(), value)
    return out


def _charset_of(part: dict[str, Any]) -> str | None:
    content_type = _headers_of(part).get('content-type', '')
    if not content_type:
        return None
    message = email.message.Message()
    message['content-type'] = content_type
    return message.get_content_charset()


def _excerpt(text: str, offset: int, max_chars: int) -> tuple[dict[str, Any], int | None]:
    end = min(offset + max_chars, len(text))
    excerpt = {
        'text': text[offset:end],
        'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'start_char': offset,
        'end_char': end,
        'start_line': text.count('\n', 0, offset) + 1,
        'end_line': text.count('\n', 0, max(offset, end - 1)) + 1,
    }
    next_offset = end if end < len(text) else None
    return excerpt, next_offset


def _check_int(value: Any, lo: int, hi: int | None, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GmailError('invalid_input')
    if value < lo or (hi is not None and value > hi):
        raise GmailError('invalid_input')
    return value


def _check_message_id(message_id: Any) -> str:
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        raise GmailError('invalid_input')
    return message_id


class GmailReader:
    def __init__(self, *, client_factory: Callable[[], GmailClient] | None = None,
                 pdf_extractor: Callable[[bytes], list[str]] = extract_pdf_pages):
        self._client_factory = client_factory or GmailClient
        self._pdf_extractor = pdf_extractor
        self._client: GmailClient | None = None
        self._account_id: str | None = None
        self._lock = threading.RLock()

    def _get_client(self) -> GmailClient:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _get_account(self) -> str:
        with self._lock:
            if self._account_id is None:
                profile = self._get_client().get_profile()
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

    def _message_metadata(self, message: dict[str, Any], warnings: list) -> dict[str, Any]:
        account_id = self._get_account()
        headers: dict[str, str] = {}
        raw_headers = _headers_of(message.get('payload') or {})
        for wire, key in _HEADER_NAMES.items():
            value, truncated = _decode_header_value(raw_headers.get(wire, ''))
            headers[key] = value
            if truncated:
                warnings.append({'code': 'header_truncated', 'header': key})
        message_id = message.get('id')
        if not isinstance(message_id, str) or not message_id:
            raise GmailError('invalid_response')
        return {
            'source': 'gmail',
            'account_id': account_id,
            'message_id': message_id,
            'thread_id': message.get('threadId'),
            'citation_id': f'gmail:{account_id}:{message_id}',
            'headers': headers,
            'internal_date_ms': message.get('internalDate'),
        }

    def search_emails(self, query: str, limit: int = 10, page_token: str | None = None,
                      include_spam_trash: bool = False) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise GmailError('invalid_input')
        query = query.strip()
        _check_int(limit, 1, 50, 'limit')
        if page_token is not None and (not isinstance(page_token, str) or not page_token or len(page_token) > 4096):
            raise GmailError('invalid_input')
        if not isinstance(include_spam_trash, bool):
            raise GmailError('invalid_input')

        client = self._get_client()
        listing = client.list_messages(query, limit, page_token, include_spam_trash)
        entries = listing.get('messages') or []
        if not isinstance(entries, list) or len(entries) > limit:
            raise GmailError('invalid_response')
        warnings: list = []
        results = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
                raise GmailError('invalid_response')
            mid = entry['id']
            try:
                message = client.get_message(mid, format='metadata')
            except GmailError as exc:
                if exc.code == 'not_found':
                    warnings.append({'code': 'message_unavailable', 'message_id': mid})
                    continue
                raise
            if message.get('id') != mid:
                raise GmailError('invalid_response')
            results.append(self._message_metadata(message, warnings))
        next_token = listing.get('nextPageToken')
        estimate = listing.get('resultSizeEstimate')
        if estimate is not None and (
            isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0
        ):
            raise GmailError('invalid_response')
        return {
            'source': 'gmail',
            'account_id': self._get_account(),
            'query': query,
            'include_spam_trash': include_spam_trash,
            'results': results,
            'next_page_token': next_token if isinstance(next_token, str) else None,
            'result_size_estimate': estimate,
            'retrieved_at': _now(),
            'warnings': warnings,
        }

    def _fetch_body_bytes(self, client: GmailClient, message_id: str, part: dict[str, Any],
                          state: dict[str, Any], cap: int) -> bytes:
        body = part.get('body') or {}
        if not isinstance(body, dict):
            raise GmailError('invalid_response')
        declared = body.get('size')
        if declared is not None:
            if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                raise GmailError('invalid_response')
            if declared > cap:
                raise GmailError('too_large')
        data = body.get('data')
        if isinstance(data, str) and data:
            return _b64url_decode(data, cap)
        if data is not None and not isinstance(data, str):
            raise GmailError('invalid_response')
        attachment_id = body.get('attachmentId')
        if isinstance(attachment_id, str) and attachment_id:
            state['fetches'] += 1
            if state['fetches'] > _MAX_BODY_FETCHES:
                raise GmailError('too_large')
            payload = client.get_attachment(message_id, attachment_id)
            fetched = payload.get('data')
            if not isinstance(fetched, str):
                raise GmailError('invalid_response')
            return _b64url_decode(fetched, cap)
        if attachment_id is not None and not isinstance(attachment_id, str):
            raise GmailError('invalid_response')
        return b''

    def _part_text(self, client: GmailClient, message_id: str, part: dict[str, Any],
                   mime: str, state: dict[str, Any], warnings: list) -> str:
        remaining = _MAX_BODY_BYTES - state['body_bytes']
        raw = self._fetch_body_bytes(client, message_id, part, state, remaining)
        state['body_bytes'] += len(raw)
        if state['body_bytes'] > _MAX_BODY_BYTES:
            raise GmailError('too_large')
        charset = _charset_of(part)
        if charset:
            try:
                text = raw.decode(charset, errors='replace')
            except LookupError:
                warnings.append({'code': 'unknown_charset', 'charset': charset[:64]})
                text = raw.decode('utf-8', errors='replace')
        else:
            text = raw.decode('utf-8', errors='replace')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if mime == 'text/html':
            text = _html_to_text(text)
        if len(text) > _MAX_TEXT_CHARS:
            raise GmailError('too_large')
        return text

    def _is_attachment(self, part: dict[str, Any]) -> bool:
        mime = (part.get('mimeType') or '').lower()
        headers = _headers_of(part)
        disposition = headers.get('content-disposition', '').split(';', 1)[0].strip().lower()
        return (
            bool(part.get('filename'))
            or disposition == 'attachment'
            or mime == 'message/rfc822'
        )

    def _contains_plain(self, part: dict[str, Any]) -> bool:
        if self._is_attachment(part):
            return False
        mime = (part.get('mimeType') or '').lower()
        if mime in ('', 'text/plain'):
            return True
        return mime.startswith('multipart/') and any(
            self._contains_plain(child) for child in (part.get('parts') or [])
        )

    def _body_text(self, part: dict[str, Any], client: GmailClient, message_id: str,
                   state: dict[str, Any], warnings: list) -> str:
        if self._is_attachment(part):
            return ''
        mime = (part.get('mimeType') or '').lower()
        children = part.get('parts') or []
        if mime == 'multipart/alternative':
            for child in sorted(children, key=lambda child: not self._contains_plain(child)):
                text = self._body_text(child, client, message_id, state, warnings)
                if text.strip():
                    return text
            return ''
        if mime.startswith('multipart/'):
            fragments = [self._body_text(child, client, message_id, state, warnings)
                         for child in children]
            return '\n\n'.join(text for text in fragments if text)
        if mime in ('', 'text/plain', 'text/html'):
            return self._part_text(client, message_id, part, mime or 'text/plain', state, warnings)
        return ''

    def _walk(self, part: dict[str, Any], depth: int, state: dict[str, Any],
              force_attach: bool = False) -> None:
        state['count'] += 1
        if depth > _MAX_DEPTH or state['count'] > _MAX_PARTS:
            raise GmailError('invalid_response')
        if not isinstance(part, dict):
            raise GmailError('invalid_response')
        part_id = part.get('partId')
        if part_id is not None and (
            not isinstance(part_id, str) or len(part_id) > 256 or _CONTROL_RE.search(part_id)
        ):
            raise GmailError('invalid_response')
        if part.get('mimeType') is not None and not isinstance(part.get('mimeType'), str):
            raise GmailError('invalid_response')
        if part.get('filename') is not None and not isinstance(part.get('filename'), str):
            raise GmailError('invalid_response')
        _headers_of(part)
        if part.get('body') is not None and not isinstance(part.get('body'), dict):
            raise GmailError('invalid_response')
        children = part.get('parts')
        if children is not None and not isinstance(children, list):
            raise GmailError('invalid_response')
        if force_attach or self._is_attachment(part):
            self._register_attachment(part, state)
            for child in children or []:
                self._walk(child, depth + 1, state, force_attach=True)
            return
        mime = (part.get('mimeType') or '').lower()
        if mime.startswith('multipart/'):
            for child in children or []:
                self._walk(child, depth + 1, state)
            return
        if mime in ('text/plain', 'text/html') or not mime:
            return
        self._register_attachment(part, state)

    def _register_attachment(self, part: dict[str, Any], state: dict[str, Any]) -> None:
        part_id = part.get('partId') or ''
        if part_id in state['attachment_ids']:
            raise GmailError('invalid_response')
        state['attachment_ids'].add(part_id)
        state['attachments'].append(part)

    def _analyze(self, message: dict[str, Any], warnings: list, *,
                 include_body: bool = True) -> dict[str, Any]:
        client = self._get_client()
        message_id = message.get('id')
        if not isinstance(message_id, str) or not message_id:
            raise GmailError('invalid_response')
        payload = message.get('payload')
        if not isinstance(payload, dict):
            raise GmailError('invalid_response')
        state = {'count': 0, 'fetches': 0, 'body_bytes': 0,
                 'attachments': [], 'attachment_ids': set()}
        self._walk(payload, 0, state)
        body_text = ''
        if include_body:
            body_text = self._body_text(payload, client, message_id, state, warnings)
            if len(body_text) > _MAX_TEXT_CHARS:
                raise GmailError('too_large')
            if not body_text.strip():
                warnings.append({'code': 'no_body_text'})
        attachments = []
        for part in state['attachments']:
            part_id = part.get('partId') or ''
            mime = (part.get('mimeType') or '').lower()
            filename = part.get('filename') or ''
            part_body = part.get('body') or {}
            size = part_body.get('size') if isinstance(part_body, dict) else None
            supported = mime == 'application/pdf' or filename.lower().endswith('.pdf')
            if len(filename) > 1024:
                warnings.append({'code': 'filename_truncated', 'part_id': part_id})
                filename = filename[:1024]
            attachments.append({
                'part_id': part_id,
                'filename': filename,
                'mime_type': mime,
                'size_bytes': size if isinstance(size, int) and not isinstance(size, bool) else None,
                'supported': supported,
            })
        return {'text': body_text, 'attachments': attachments,
                'parts': state['attachments'], 'payload': payload}

    def read_email(self, message_id: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        _check_message_id(message_id)
        _check_int(offset, 0, None, 'offset')
        _check_int(max_chars, 1, 24000, 'max_chars')
        client = self._get_client()
        message = client.get_message(message_id, format='full')
        if message.get('id') != message_id:
            raise GmailError('invalid_response')
        warnings: list = []
        metadata = self._message_metadata(message, warnings)
        analyzed = self._analyze(message, warnings)
        text = analyzed['text']
        total_chars = len(text)
        if offset > total_chars:
            raise GmailError('invalid_input')
        excerpt, next_offset = _excerpt(text, offset, max_chars)
        return {
            **metadata,
            'retrieved_at': _now(),
            'attachments': analyzed['attachments'],
            'warnings': warnings,
            'total_chars': total_chars,
            'next_offset': next_offset,
            'excerpt': excerpt,
        }

    def read_email_attachment(self, message_id: str, part_id: str, offset: int = 0,
                              max_chars: int = 12000) -> dict[str, Any]:
        _check_message_id(message_id)
        if not isinstance(part_id, str) or len(part_id) > 256 or _CONTROL_RE.search(part_id):
            raise GmailError('invalid_input')
        _check_int(offset, 0, None, 'offset')
        _check_int(max_chars, 1, 24000, 'max_chars')
        client = self._get_client()
        message = client.get_message(message_id, format='full')
        if message.get('id') != message_id:
            raise GmailError('invalid_response')
        warnings: list = []
        metadata = self._message_metadata(message, warnings)
        inventory_warnings: list = []
        analyzed = self._analyze(message, inventory_warnings, include_body=False)
        target = None
        for part in analyzed['parts']:
            if (part.get('partId') or '') == part_id:
                target = part
                break
        if target is None:
            raise GmailError('invalid_input')
        selected_attachment = None
        for entry in analyzed['attachments']:
            if entry['part_id'] == part_id:
                selected_attachment = entry
                break
        if selected_attachment is None:
            raise GmailError('invalid_response')
        warnings.extend(w for w in inventory_warnings if w.get('part_id') == part_id)
        mime = (target.get('mimeType') or '').lower()
        filename = target.get('filename') or ''
        if not (mime == 'application/pdf' or filename.lower().endswith('.pdf')):
            raise GmailError('unsupported_attachment')
        body = target.get('body') or {}
        declared = body.get('size')
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > _MAX_ATTACHMENT_BYTES:
            raise GmailError('too_large')
        raw = self._fetch_body_bytes(client, message_id, target, {'fetches': 0}, _MAX_ATTACHMENT_BYTES)
        if not raw[:1024].startswith(b'%PDF-'):
            raise GmailError('invalid_pdf')
        try:
            pages = self._pdf_extractor(raw)
        except PdfTextError as exc:
            raise GmailError(exc.code) from None
        if not isinstance(pages, list) or len(pages) > _MAX_PDF_PAGES:
            raise GmailError('invalid_response')
        for page in pages:
            if not isinstance(page, str):
                raise GmailError('invalid_response')
        pages = [p.replace('\r\n', '\n').replace('\r', '\n') for p in pages]
        text = '\n\n'.join(pages)
        if len(text) > _MAX_TEXT_CHARS:
            raise GmailError('too_large')
        empty_pages = [i + 1 for i, p in enumerate(pages) if not p.strip()]
        if len(empty_pages) == len(pages) and pages:
            raise GmailError('no_extractable_text')
        if not pages or not text.strip():
            raise GmailError('no_extractable_text')
        if empty_pages:
            warnings.append({'code': 'pages_without_text', 'pages': empty_pages})
        total_chars = len(text)
        if offset > total_chars:
            raise GmailError('invalid_input')
        excerpt, next_offset = _excerpt(text, offset, max_chars)
        starts = []
        pos = 0
        for p in pages:
            starts.append(pos)
            pos += len(p) + 2
        page_ranges = []
        for i, p in enumerate(pages):
            if not p.strip():
                continue
            p_start, p_end = starts[i], starts[i] + len(p)
            lo, hi = max(offset, p_start), min(excerpt['end_char'], p_end)
            if lo < hi:
                page_ranges.append({'page': i + 1, 'start_char': lo, 'end_char': hi})
        excerpt['page_ranges'] = page_ranges
        return {
            **metadata,
            'citation_id': f'{metadata["citation_id"]}:part:{part_id or "root"}',
            'attachment': {key: selected_attachment[key] for key in
                           ('part_id', 'filename', 'mime_type', 'size_bytes')},
            'attachment_sha256': hashlib.sha256(raw).hexdigest(),
            'retrieved_at': _now(),
            'excerpt': excerpt,
            'total_chars': total_chars,
            'next_offset': next_offset,
            'warnings': warnings,
        }
