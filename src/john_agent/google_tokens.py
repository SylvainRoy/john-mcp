"""One lazy, thread-safe access-token cache shared by Google API clients."""

import threading
import time


class GoogleTokens:
    def __init__(self, load_credentials, request, clock=time.monotonic):
        self._load = load_credentials
        self._request = request
        self._clock = clock
        self._token = None
        self._expiry = 0.0
        self._lock = threading.RLock()

    @property
    def scopes(self):
        return self._load()['scopes']

    def refresh(self, *, force=False):
        from john_agent.gmail_client import GmailError, TOKEN_URL

        with self._lock:
            if not force and self._token and self._clock() < self._expiry:
                return self._token
            creds = self._load()
            payload = self._request(
                'POST', TOKEN_URL, cap=64 * 1024, token_request=True,
                data={'grant_type': 'refresh_token', 'client_id': creds['client_id'],
                      'client_secret': creds['client_secret'], 'refresh_token': creds['refresh_token']})
            token, expires = payload.get('access_token'), payload.get('expires_in')
            if not isinstance(token, str) or not token or isinstance(expires, bool) \
                    or not isinstance(expires, int) or expires <= 0:
                raise GmailError('invalid_response')
            scope = payload.get('scope')
            if scope is not None and (not isinstance(scope, str) or set(scope.split()) != creds['scopes']):
                raise GmailError('invalid_response')
            self._token = token
            self._expiry = self._clock() + expires - 60.0
            return token
