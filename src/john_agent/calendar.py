"""Bounded Calendar v3 access with silent, conditional event writes."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from john_agent.gmail_client import GmailError

API_ROOT = 'https://www.googleapis.com/calendar/v3'
_CAP = 2 * 1024 * 1024
_ERRORS = {
    'invalid_input': 'Check Calendar identifiers, times, limits, and event fields.',
    'unavailable': 'Calendar is temporarily unavailable; try again later.',
    'authorization': 'Calendar authorization is unavailable; rerun john-google-auth.',
    'permission': 'Calendar access was not granted or is unavailable.',
    'not_found': 'The requested calendar or event is no longer available.',
    'changed': 'event changed since it was read; re-read and retry',
    'invalid_response': 'Calendar returned an invalid or unsupported response.',
    'role': 'Writes require calendarList accessRole owner or writer.',
    'organizer': 'Only events whose organizer.self is true may be changed or removed.',
    'instance': "This is a series master; use an instance id or scope='series'.",
    'series': "Use recurring_event_id (the series master id) and its etag for scope='series'.",
}


class CalendarError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code])


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024 \
            or value in ('.', '..') or any(ord(c) < 32 for c in value):
        raise CalendarError('invalid_input')
    return quote(value, safe='')


def _time(value):
    if not isinstance(value, str):
        raise CalendarError('invalid_input')
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return 'date', date.fromisoformat(value)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return 'dateTime', parsed
    except ValueError:
        raise CalendarError('invalid_input') from None


def _time_pair(start, end):
    kind, first = _time(start)
    other, last = _time(end)
    if kind != other or last <= first:
        raise CalendarError('invalid_input')
    return kind


class CalendarClient:
    def __init__(self, *, token_provider, http_client=None):
        self.tokens = token_provider
        self._http = http_client

    def _request(self, method, path, *, params=None, body=None, etag=None):
        if self._http is None:
            self._http = httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0)
        for attempt in range(2):
            try:
                token = self.tokens.refresh(force=bool(attempt))
                headers = {'Authorization': f'Bearer {token}'}
                if etag is not None:
                    headers['If-Match'] = etag
                with self._http.stream(method, API_ROOT + path, params=params, json=body,
                                       headers=headers, follow_redirects=False, timeout=10.0) as response:
                    status = response.status_code
                    if status == 401 and not attempt:
                        continue
                    if status == 412:
                        raise CalendarError('changed')
                    if status in (401, 403):
                        raise CalendarError('authorization' if status == 401 else 'permission')
                    if status == 404:
                        raise CalendarError('not_found')
                    if status == 400:
                        raise CalendarError('invalid_input')
                    if status == 429 or status >= 500:
                        raise CalendarError('unavailable')
                    if status == 204 and method == 'DELETE':
                        return {}
                    if status not in (200, 201):
                        raise CalendarError('invalid_response')
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _CAP:
                            raise CalendarError('invalid_response')
                        chunks.append(chunk)
                    result = json.loads(b''.join(chunks))
                    if not isinstance(result, dict):
                        raise CalendarError('invalid_response')
                    return result
            except GmailError:
                raise CalendarError('authorization') from None
            except httpx.HTTPError:
                raise CalendarError('unavailable') from None
            except (ValueError, UnicodeError):
                raise CalendarError('invalid_response') from None
        raise CalendarError('authorization')

    def _calendar(self, calendar_id):
        return self._request('GET', '/users/me/calendarList/' + _identifier(calendar_id))

    @staticmethod
    def _calendar_info(item):
        return {'id': item.get('id'), 'summary': str(item.get('summary', ''))[:1000],
                'primary': item.get('primary', False), 'access_role': item.get('accessRole'),
                'time_zone': item.get('timeZone')}

    @staticmethod
    def _event(item, full=False):
        result = {key: item.get(api) for key, api in {
            'id': 'id', 'etag': 'etag', 'summary': 'summary', 'start': 'start', 'end': 'end',
            'location': 'location', 'status': 'status', 'recurring_event_id': 'recurringEventId',
            'html_link': 'htmlLink'}.items()}
        result['summary'] = str(result['summary'] or '')[:1000]
        result['organizer_self'] = item.get('organizer', {}).get('self', False)
        if full:
            result = {**item, **result}
            description = str(item.get('description', ''))
            attendees = item.get('attendees', [])
            result.update(description=description[:12000], description_truncated=len(description) > 12000,
                          attendees=attendees[:100], attendees_truncated=len(attendees) > 100,
                          recurrence=item.get('recurrence'), organizer=item.get('organizer'),
                          reminders=item.get('reminders'))
        return result

    def list_calendars(self) -> dict[str, Any]:
        data = self._request('GET', '/users/me/calendarList', params={'maxResults': '250'})
        return {'calendars': [self._calendar_info(item) for item in data.get('items', [])[:250]],
                'next_page_token': data.get('nextPageToken')}

    def list_events(self, calendar_id: str = 'primary', time_min: str | None = None,
                    time_max: str | None = None, query: str | None = None, limit: int = 25,
                    page_token: str | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CalendarError('invalid_input')
        now = datetime.now(timezone.utc)
        time_min = now.isoformat() if time_min is None else time_min
        time_max = (_time(time_min)[1] + timedelta(days=30)).isoformat() if time_max is None else time_max
        if _time_pair(time_min, time_max) != 'dateTime':
            raise CalendarError('invalid_input')
        params = {'singleEvents': 'true', 'orderBy': 'startTime', 'timeMin': time_min,
                  'timeMax': time_max, 'maxResults': str(limit)}
        if query is not None:
            if len(query) > 4096:
                raise CalendarError('invalid_input')
            params['q'] = query
        if page_token is not None:
            if len(page_token) > 4096:
                raise CalendarError('invalid_input')
            params['pageToken'] = page_token
        data = self._request('GET', '/calendars/' + _identifier(calendar_id) + '/events', params=params)
        return {'events': [self._event(item) for item in data.get('items', [])[:limit]],
                'next_page_token': data.get('nextPageToken')}

    def _get(self, event_id, calendar_id):
        return self._request('GET', self._path(calendar_id, event_id))

    @staticmethod
    def _path(calendar_id, event_id=None):
        return '/calendars/' + _identifier(calendar_id) + '/events' + (
            '/' + _identifier(event_id) if event_id is not None else '')

    def read_event(self, event_id: str, calendar_id: str = 'primary') -> dict[str, Any]:
        return self._event(self._get(event_id, calendar_id), full=True)

    def _writable_calendar(self, calendar_id):
        calendar = self._calendar(calendar_id)
        if calendar.get('accessRole') not in ('owner', 'writer'):
            raise CalendarError('role')
        return calendar

    def _change_guard(self, event_id, etag, calendar_id, scope):
        if scope not in ('instance', 'series') or not isinstance(etag, str) or not etag or etag == '*' \
                or len(etag) > 1024 or any(ord(c) < 32 for c in etag):
            raise CalendarError('invalid_input')
        calendar = self._writable_calendar(calendar_id)
        event = self._get(event_id, calendar_id)
        if event.get('organizer', {}).get('self') is not True:
            raise CalendarError('organizer')
        if scope == 'instance' and event.get('recurrence'):
            raise CalendarError('instance')
        if scope == 'series' and (event.get('recurringEventId') or not event.get('recurrence')):
            raise CalendarError('series')
        return calendar, event

    @staticmethod
    def _body(calendar, summary, start, end, time_zone, location, description,
              attendees, recurrence, reminders, original=None):
        result = {}
        for key, value, cap in [('summary', summary, 1000), ('location', location, 2000),
                                ('description', description, 12000)]:
            if value is not None:
                if not isinstance(value, str) or len(value) > cap:
                    raise CalendarError('invalid_input')
                result[key] = value
        if start is not None or end is not None or time_zone is not None:
            original = original or {}
            old_start, old_end = original.get('start', {}), original.get('end', {})
            first = start if start is not None else old_start.get('dateTime', old_start.get('date'))
            last = end if end is not None else old_end.get('dateTime', old_end.get('date'))
            kind = _time_pair(first, last)
            zone = time_zone or calendar.get('timeZone')
            if kind == 'dateTime':
                try:
                    ZoneInfo(zone)
                except (ZoneInfoNotFoundError, TypeError, ValueError):
                    raise CalendarError('invalid_input') from None
            for key, value, supplied in [('start', first, start), ('end', last, end)]:
                if supplied is not None or time_zone is not None:
                    result[key] = {kind: value}
                    if kind == 'dateTime':
                        result[key]['timeZone'] = zone
        if attendees is not None:
            if len(attendees) > 100 or not all(isinstance(a, str) and len(a) <= 320
                                             and re.fullmatch(r'[^\s@]+@[^\s@]+', a) for a in attendees):
                raise CalendarError('invalid_input')
            result['attendees'] = [{'email': a} for a in attendees]
        if recurrence is not None:
            if len(recurrence) > 20 or not all(isinstance(r, str) and len(r) <= 2000
                                              and r.startswith(('RRULE:', 'RDATE:', 'EXDATE:', 'EXRULE:'))
                                              and '\n' not in r and '\r' not in r for r in recurrence):
                raise CalendarError('invalid_input')
            result['recurrence'] = recurrence
        if reminders is not None:
            if not isinstance(reminders, dict) or not set(reminders) <= {'use_default', 'overrides'} \
                    or not isinstance(reminders.get('use_default'), bool):
                raise CalendarError('invalid_input')
            overrides = reminders.get('overrides', [])
            if not isinstance(overrides, list) or len(overrides) > 5 \
                    or (reminders['use_default'] and overrides):
                raise CalendarError('invalid_input')
            for override in overrides:
                if not isinstance(override, dict) or set(override) != {'method', 'minutes'} \
                        or override['method'] not in ('popup', 'email') \
                        or isinstance(override['minutes'], bool) or not isinstance(override['minutes'], int) \
                        or not 0 <= override['minutes'] <= 40320:
                    raise CalendarError('invalid_input')
            result['reminders'] = {'useDefault': reminders['use_default'], 'overrides': overrides}
        return result

    def create_event(self, summary: str, start: str, end: str, calendar_id: str = 'primary',
                     time_zone: str | None = None, location: str | None = None,
                     description: str | None = None, attendees: list[str] | None = None,
                     recurrence: list[str] | None = None, reminders: dict | None = None) -> dict[str, Any]:
        _time_pair(start, end)
        calendar = self._writable_calendar(calendar_id)
        body = self._body(calendar, summary, start, end, time_zone, location, description,
                          attendees, recurrence, reminders)
        return self._event(self._request('POST', self._path(calendar_id),
                                        params={'sendUpdates': 'none'}, body=body), full=True)

    def update_event(self, event_id: str, etag: str, calendar_id: str = 'primary',
                     scope: Literal['instance', 'series'] = 'instance', summary: str | None = None,
                     start: str | None = None, end: str | None = None, time_zone: str | None = None,
                     location: str | None = None, description: str | None = None,
                     attendees: list[str] | None = None, recurrence: list[str] | None = None,
                     reminders: dict | None = None) -> dict[str, Any]:
        calendar, original = self._change_guard(event_id, etag, calendar_id, scope)
        body = self._body(calendar, summary, start, end, time_zone, location, description,
                          attendees, recurrence, reminders, original)
        if not body:
            raise CalendarError('invalid_input')
        return self._event(self._request('PATCH', self._path(calendar_id, event_id),
                                        params={'sendUpdates': 'none'}, body=body, etag=etag), full=True)

    def delete_event(self, event_id: str, etag: str, calendar_id: str = 'primary',
                     scope: Literal['instance', 'series'] = 'instance') -> dict[str, Any]:
        self._change_guard(event_id, etag, calendar_id, scope)
        self._request('DELETE', self._path(calendar_id, event_id),
                      params={'sendUpdates': 'none'}, etag=etag)
        return {'id': event_id, 'deleted': True}

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None
