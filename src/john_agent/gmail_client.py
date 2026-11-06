"""Fixed-origin Gmail REST transport with lazy authorized-user credentials."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
GMAIL_COMPOSE_SCOPE = 'https://www.googleapis.com/auth/gmail.compose'
ALLOWED_SCOPE_SETS = frozenset({
    frozenset({GMAIL_SCOPE}),
    frozenset({GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE}),
})
GMAIL_ENV = 'JOHN_GMAIL_CREDENTIALS'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API_ROOT = 'https://gmail.googleapis.com/gmail/v1/users/me'

ERROR_MESSAGES = {
    'not_configured': 'Configure gmail.credentials or JOHN_GMAIL_CREDENTIALS after running john-gmail-auth.',
    'invalid_credentials': 'Gmail credentials must be authorized-user JSON with only the Gmail read-only scope, or read-only plus compose for drafts.',
    'authorization_required': 'Gmail authorization is missing, expired, or revoked; run the separate OAuth setup again.',
    'permission_denied': 'Gmail read-only access was not granted or is not available.',
    'rate_limited': 'Gmail temporarily limited requests; try again later.',
    'unavailable': 'Gmail is temporarily unavailable; try again later.',
    'not_found': 'The requested Gmail message or attachment is no longer available.',
    'invalid_input': 'Check the Gmail query, identifiers, pagination, and requested limits.',
    'invalid_response': 'Gmail returned an invalid or unsupported response.',
    'too_large': 'The message or attachment exceeds the supported processing limits.',
    'unsupported_attachment': 'Only text-based PDF attachments can be read.',
    'invalid_pdf': 'The attachment could not be read as a PDF.',
    'password_required': 'The PDF is encrypted; password-protected PDFs are not supported.',
    'no_extractable_text': 'The PDF has no extractable text; OCR is not enabled.',
    'extraction_timeout': 'PDF text extraction exceeded its time limit.',
    'drafts_disabled': 'Draft tools require credentials authorized with the Gmail compose scope; rerun john-gmail-auth --drafts.',
    'attachment_rejected': 'Attachments must be regular files inside a configured attachment root or source root.',
}

_MESSAGE_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
_ENV_JSON_CAP = 64 * 1024
_FIELD_CAP = 16 * 1024
_TOKEN_CAP = 64 * 1024
_SMALL_CAP = 256 * 1024
_MESSAGE_CAP = 32 * 1024 * 1024
_ATTACHMENT_CAP = 15 * 1024 * 1024
_TOKEN_MARGIN = 60.0
_MAX_ATTEMPTS = 3
_RETRY_SLEEPS = (0.25, 0.5)
_RETRY_AFTER_CAP = 2.0
_RATE_LIMIT_REASONS = {'rateLimitExceeded', 'userRateLimitExceeded'}
_METADATA_HEADERS = ('From', 'To', 'Subject', 'Date', 'Message-ID')


class GmailError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Gmail error [{code}]: {ERROR_MESSAGES[code]}')


def _error_reasons(payload: Any) -> set[str]:
    reasons: set[str] = set()
    if not isinstance(payload, dict):
        return reasons
    error = payload.get('error')
    if not isinstance(error, dict):
        return reasons
    errors = error.get('errors')
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and isinstance(item.get('reason'), str):
                reasons.add(item['reason'])
    return reasons


def validate_credentials(value: Any) -> dict[str, str]:
    if value is None:
        raise GmailError('not_configured')
    parsed = value
    if isinstance(parsed, str):
        try:
            if len(parsed.encode('utf-8')) > _ENV_JSON_CAP:
                raise GmailError('invalid_credentials')
        except UnicodeError:
            raise GmailError('invalid_credentials') from None
        if not parsed.strip():
            raise GmailError('not_configured')
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
            raise GmailError('invalid_credentials') from None
    if not isinstance(parsed, dict) or parsed.get('type') != 'authorized_user':
        raise GmailError('invalid_credentials')
    fields: dict[str, str] = {}
    for key in ('client_id', 'client_secret', 'refresh_token'):
        field_value = parsed.get(key)
        if not isinstance(field_value, str) or not field_value or '\x00' in field_value:
            raise GmailError('invalid_credentials')
        try:
            if len(field_value.encode('utf-8')) > _FIELD_CAP:
                raise GmailError('invalid_credentials')
        except UnicodeError:
            raise GmailError('invalid_credentials') from None
        fields[key] = field_value
    scopes = parsed.get('scopes')
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes) \
            or frozenset(scopes) not in ALLOWED_SCOPE_SETS:
        raise GmailError('invalid_credentials')
    fields['scopes'] = frozenset(scopes)
    return fields


class GmailClient:
    def __init__(self, *, credentials=None, use_environment: bool = True,
                 http_client=None, sleep=time.sleep, clock=time.monotonic):
        self._injected_credentials = credentials
        self._use_environment = use_environment
        self._http = http_client
        self._sleep = sleep
        self._clock = clock
        self._credentials: dict[str, Any] | None = None
        self._access_token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.RLock()
        for name in ('httpx', 'httpcore'):
            logging.getLogger(name).setLevel(logging.WARNING)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0)
        return self._http

    def _load_credentials(self) -> dict[str, Any]:
        value = self._injected_credentials
        if value is None and self._use_environment:
            value = os.environ.get(GMAIL_ENV)
        self._credentials = validate_credentials(value)
        return self._credentials

    def _get_credentials(self) -> dict[str, Any]:
        if self._credentials is None:
            return self._load_credentials()
        return self._credentials

    @property
    def scopes(self) -> frozenset:
        return self._get_credentials()['scopes']

    def _refresh(self, *, force: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if not force and self._access_token and now < self._token_expiry:
                return self._access_token
            creds = self._get_credentials()
            payload = self._perform_json(
                'POST', TOKEN_URL, cap=_TOKEN_CAP, token_request=True,
                data={'grant_type': 'refresh_token', 'client_id': creds['client_id'],
                      'client_secret': creds['client_secret'], 'refresh_token': creds['refresh_token']},
            )
            token = payload.get('access_token')
            expires = payload.get('expires_in')
            if not isinstance(token, str) or not token:
                raise GmailError('invalid_response')
            if isinstance(expires, bool) or not isinstance(expires, int) or expires <= 0:
                raise GmailError('invalid_response')
            scope = payload.get('scope')
            if scope is not None:
                if not isinstance(scope, str) or set(scope.split()) != creds['scopes']:
                    raise GmailError('invalid_response')
            self._access_token = token
            self._token_expiry = self._clock() + expires - _TOKEN_MARGIN
            return token

    def _read_json(self, response: httpx.Response, cap: int) -> dict[str, Any]:
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > cap:
                raise GmailError('too_large')
            chunks.append(chunk)
        try:
            parsed = json.loads(b''.join(chunks))
        except (json.JSONDecodeError, ValueError):
            raise GmailError('invalid_response') from None
        if not isinstance(parsed, dict):
            raise GmailError('invalid_response')
        return parsed

    def _perform_json(self, method: str, url: str, *, cap: int,
                      params=None, data=None, json=None, headers=None,
                      token_request: bool = False) -> dict[str, Any]:
        attempts = 0
        while attempts < _MAX_ATTEMPTS:
            attempts += 1
            retry_code = None
            retry_after = None
            try:
                with self._client().stream(
                    method, url, params=params, data=data, json=json, headers=headers,
                    follow_redirects=False, timeout=10.0,
                ) as response:
                    status = response.status_code
                    if status == 200:
                        return self._read_json(response, cap)
                    if status < 400:
                        raise GmailError('invalid_response')
                    try:
                        payload = self._read_json(response, min(cap, _SMALL_CAP))
                    except GmailError:
                        payload = {}
                    retry_after = response.headers.get('Retry-After')
                    if token_request:
                        if status in (400, 401, 403):
                            raise GmailError('authorization_required')
                    else:
                        if status == 401:
                            raise GmailError('authorization_required')
                        if status == 404:
                            raise GmailError('not_found')
                        if status == 400:
                            raise GmailError('invalid_input')
                        if status == 403 and not (
                            _error_reasons(payload) & _RATE_LIMIT_REASONS
                        ):
                            raise GmailError('permission_denied')
                    if status == 429:
                        retry_code = 'rate_limited'
                    elif status in (403, 500, 502, 503, 504):
                        retry_code = 'rate_limited' if status == 403 else 'unavailable'
                    elif token_request:
                        raise GmailError('unavailable')
                    else:
                        raise GmailError('invalid_response')
            except httpx.HTTPError:
                retry_code = 'unavailable'
            if retry_code is not None:
                if attempts >= _MAX_ATTEMPTS:
                    raise GmailError(retry_code)
                delay = _RETRY_SLEEPS[min(attempts - 1, len(_RETRY_SLEEPS) - 1)]
                if retry_after:
                    try:
                        delay = max(delay, min(float(retry_after), _RETRY_AFTER_CAP))
                    except ValueError:
                        pass
                self._sleep(delay)
        raise GmailError('unavailable')

    def _request_json(self, path: str, params: list[tuple[str, str]], cap: int,
                      method: str = 'GET', json=None) -> dict[str, Any]:
        token = self._refresh()
        try:
            return self._perform_json(method, API_ROOT + path, cap=cap, params=params,
                                      json=json, headers={'Authorization': f'Bearer {token}'})
        except GmailError as exc:
            if exc.code != 'authorization_required':
                raise
        token = self._refresh(force=True)
        return self._perform_json(method, API_ROOT + path, cap=cap, params=params,
                                  json=json, headers={'Authorization': f'Bearer {token}'})

    def get_profile(self) -> dict[str, Any]:
        return self._request_json('/profile', [], _SMALL_CAP)

    def create_draft(self, message: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(message, dict) or not isinstance(message.get('raw'), str):
            raise GmailError('invalid_input')
        return self._request_json('/drafts', [], _SMALL_CAP,
                                  method='POST', json={'message': message})

    def list_messages(self, query: str, limit: int, page_token: str | None, include_spam_trash: bool) -> dict[str, Any]:
        params = [
            ('q', query),
            ('maxResults', str(limit)),
            ('includeSpamTrash', 'true' if include_spam_trash else 'false'),
        ]
        if page_token:
            params.append(('pageToken', page_token))
        return self._request_json('/messages', params, _SMALL_CAP)

    def _check_message_id(self, message_id: str) -> None:
        if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
            raise GmailError('invalid_input')

    def get_message(self, message_id: str, format: str = 'full') -> dict[str, Any]:
        self._check_message_id(message_id)
        if format == 'metadata':
            params = [('format', 'metadata')]
            params += [('metadataHeaders', h) for h in _METADATA_HEADERS]
            return self._request_json(f'/messages/{message_id}', params, _SMALL_CAP)
        if format != 'full':
            raise GmailError('invalid_input')
        return self._request_json(f'/messages/{message_id}', [('format', 'full')], _MESSAGE_CAP)

    def get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        self._check_message_id(message_id)
        if not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 4096:
            raise GmailError('invalid_input')
        safe_id = quote(attachment_id, safe='')
        return self._request_json(f'/messages/{message_id}/attachments/{safe_id}', [], _ATTACHMENT_CAP)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
