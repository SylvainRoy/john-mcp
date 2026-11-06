import asyncio
import copy
import json
from datetime import datetime, timedelta

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mdtodb import CorpusReader

from john_agent.calendar import CalendarClient, CalendarError
from john_agent.gmail import GmailReader
from john_agent.gmail_client import GmailClient, TOKEN_URL, GMAIL_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE
from john_agent.server import create_server


CREDS = {'type': 'authorized_user', 'client_id': 'fake-id', 'client_secret': 'fake-secret',
         'refresh_token': 'fake-refresh', 'scopes': [GMAIL_SCOPE, CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE]}
EVENT = {'id': 'event', 'etag': '"etag"', 'summary': 'Meeting',
         'start': {'dateTime': '2026-11-10T10:00:00Z', 'timeZone': 'UTC'},
         'end': {'dateTime': '2026-11-10T11:00:00Z', 'timeZone': 'UTC'},
         'organizer': {'self': True, 'email': 'me@example.com'}, 'status': 'confirmed'}


class API:
    def __init__(self):
        self.requests = []
        self.event = copy.deepcopy(EVENT)
        self.calendar = {'id': 'primary', 'summary': 'Personal', 'primary': True,
                         'accessRole': 'owner', 'timeZone': 'Europe/Paris'}
        self.write_status = 200
        self.error = None
        self.items = None

    def __call__(self, request):
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={'access_token': 'fake-access', 'expires_in': 3600})
        if self.error:
            return httpx.Response(self.error, json={'error': {'message': 'SECRET raw body'}})
        path = request.url.path
        if path.endswith('/profile'):
            return httpx.Response(200, json={'emailAddress': 'me@example.com'})
        if request.method != 'GET':
            return httpx.Response(self.write_status, json=self.event) if self.write_status != 204 else httpx.Response(204)
        if path.endswith('/calendarList'):
            return httpx.Response(200, json={'items': [self.calendar]})
        if '/calendarList/' in path:
            return httpx.Response(200, json=self.calendar)
        if path.endswith('/events'):
            return httpx.Response(200, json={'items': self.items if self.items is not None else [self.event], 'nextPageToken': 'next'})
        return httpx.Response(200, json=self.event)


@pytest.fixture
def setup():
    api = API()
    http = httpx.Client(transport=httpx.MockTransport(api))
    google = GmailClient(credentials=CREDS, use_environment=False, http_client=http)
    calendar = CalendarClient(token_provider=google.tokens, http_client=http)
    yield calendar, google, api
    calendar.close()
    google.close()


def writes(api):
    return [r for r in api.requests if r.method in ('POST', 'PATCH', 'DELETE') and str(r.url) != TOKEN_URL]


def test_shared_refresh_and_lazy(setup):
    calendar, google, api = setup
    assert not api.requests
    assert google.get_profile()['emailAddress'] == 'me@example.com'
    assert calendar.list_calendars()['calendars'][0] == {
        'id': 'primary', 'summary': 'Personal', 'primary': True,
        'access_role': 'owner', 'time_zone': 'Europe/Paris'}
    assert len([r for r in api.requests if str(r.url) == TOKEN_URL]) == 1
    assert all(r.headers['authorization'] == 'Bearer fake-access' for r in api.requests if str(r.url) != TOKEN_URL)


def test_calendar_refresh_shared_back_to_gmail(setup):
    calendar, google, api = setup
    now = [1000.0]
    google.tokens._clock = lambda: now[0]
    calendar.read_event('event')
    google.get_profile()
    now[0] += 3600
    calendar.read_event('event')
    google.get_profile()
    assert len([r for r in api.requests if str(r.url) == TOKEN_URL]) == 2


def test_list_default_and_explicit_window(setup):
    calendar, _, api = setup
    data = calendar.list_events()
    params = api.requests[-1].url.params
    assert params['singleEvents'] == 'true' and params['orderBy'] == 'startTime'
    assert datetime.fromisoformat(params['timeMax']) - datetime.fromisoformat(params['timeMin']) == timedelta(days=30)
    assert data['events'][0]['etag'] == '"etag"' and data['next_page_token'] == 'next'
    calendar.list_events('custom@example.com', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 'meeting', 1, 'page')
    assert api.requests[-1].url.params['q'] == 'meeting'
    assert api.requests[-1].url.params['pageToken'] == 'page'
    api.items = []
    assert calendar.list_events()['events'] == []


def test_read_bounded(setup):
    calendar, _, api = setup
    api.event.update(description='x' * 13000, attendees=[{'displayName': 'untrusted', 'email': 'a@b'}] * 101,
                     recurrence=['RRULE:FREQ=DAILY'], reminders={'useDefault': True})
    data = calendar.read_event('event')
    assert len(data['description']) == 12000 and data['description_truncated']
    assert len(data['attendees']) == 100 and data['attendees_truncated']
    assert data['recurrence'] == ['RRULE:FREQ=DAILY']
    assert data['organizer']['self'] is True and data['reminders'] == {'useDefault': True}


def test_all_writes_are_silent_conditional_patch_only(setup):
    calendar, _, api = setup
    calendar.create_event('Meeting', '2026-11-10T10:00:00Z', '2026-11-10T11:00:00Z',
                          attendees=['guest@example.com'], recurrence=['RRULE:FREQ=DAILY'],
                          reminders={'use_default': False, 'overrides': [{'method': 'popup', 'minutes': 10}]})
    body = json.loads(writes(api)[0].content)
    assert body['start']['timeZone'] == 'Europe/Paris'
    assert body['attendees'] == [{'email': 'guest@example.com'}]
    assert body['reminders'] == {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 10}]}
    calendar.update_event('event', '"etag"', summary='New')
    assert json.loads(writes(api)[-1].content) == {'summary': 'New'}
    api.write_status = 204
    assert calendar.delete_event('event', '"etag"')['deleted']
    assert [r.method for r in writes(api)] == ['POST', 'PATCH', 'DELETE']
    for request in writes(api):
        assert request.url.params['sendUpdates'] == 'none'
        if request.method != 'POST':
            assert request.headers['If-Match'] == '"etag"'


def test_all_day_and_partial_time_patch(setup):
    calendar, _, api = setup
    calendar.create_event('Day', '2026-11-10', '2026-11-11')
    assert json.loads(writes(api)[-1].content)['start'] == {'date': '2026-11-10'}
    calendar.update_event('event', '"etag"', start='2026-11-10T10:30:00Z')
    body = json.loads(writes(api)[-1].content)
    assert set(body) == {'start'}
    assert body['start']['dateTime'] == '2026-11-10T10:30:00Z'


@pytest.mark.parametrize('start,end', [
    ('2026-11-10', '2026-11-10'), ('2026-11-11', '2026-11-10'),
    ('2026-11-10', '2026-11-11T00:00:00Z'), ('bad', 'bad'),
    ('2026-02-30', '2026-03-01'), ('2026-11-10T10:00:00', '2026-11-10T11:00:00'),
    ('2026-11-10T11:00:00Z', '2026-11-10T10:00:00Z'),
    ('', ''), (None, None),
])
def test_invalid_time_never_writes(setup, start, end):
    calendar, _, api = setup
    with pytest.raises(CalendarError, match='Check Calendar'):
        calendar.create_event('Bad', start, end)
    assert not writes(api)


@pytest.mark.parametrize('fields', [
    {'time_zone': 'Not/AZone'}, {'attendees': ['invalid']},
    {'reminders': {'use_default': False, 'overrides': [{'method': 'sms', 'minutes': 1}]}},
    {'reminders': {'use_default': True, 'overrides': [{'method': 'email', 'minutes': 1}]}},
    {'reminders': {'use_default': False, 'overrides': [{'method': 'email', 'minutes': -1}]}},
    {'recurrence': ['BEGIN:VEVENT']},
])
def test_invalid_fields(setup, fields):
    calendar, _, api = setup
    with pytest.raises(CalendarError):
        calendar.create_event('Bad', '2026-11-10T10:00:00Z', '2026-11-10T11:00:00Z', **fields)
    assert not writes(api)


@pytest.mark.parametrize('kwargs', [{'time_min': ''}, {'time_min': '2026-01-01'},
                                   {'limit': 0}, {'limit': 101}, {'limit': 1.5}])
def test_invalid_list_window_or_limit(setup, kwargs):
    calendar, _, api = setup
    with pytest.raises(CalendarError):
        calendar.list_events(**kwargs)
    assert not api.requests


@pytest.mark.parametrize('method', ['update', 'delete'])
@pytest.mark.parametrize('case,scope,error', [
    ('master', 'instance', 'series master'), ('instance', 'series', 'recurring_event_id'),
    ('single', 'series', 'master id'), ('organizer', 'instance', 'organizer.self'),
    ('role', 'instance', 'accessRole'),
])
def test_guards(setup, method, case, scope, error):
    calendar, _, api = setup
    if case == 'master':
        api.event['recurrence'] = ['RRULE:FREQ=DAILY']
    if case == 'instance':
        api.event['recurringEventId'] = 'master'
    if case == 'organizer':
        api.event['organizer']['self'] = False
    if case == 'role':
        api.calendar['accessRole'] = 'reader'
    with pytest.raises(CalendarError, match=error):
        if method == 'update':
            calendar.update_event('event', '"etag"', scope=scope, summary='New')
        else:
            calendar.delete_event('event', '"etag"', scope=scope)
    assert not writes(api)


def test_create_role_refusal_and_series_success(setup):
    calendar, _, api = setup
    api.calendar['accessRole'] = 'freeBusyReader'
    with pytest.raises(CalendarError, match='accessRole'):
        calendar.create_event('Day', '2026-11-10', '2026-11-11')
    assert not writes(api)
    api.calendar['accessRole'] = 'writer'
    api.event['recurrence'] = ['RRULE:FREQ=DAILY']
    calendar.update_event('master', '"master-etag"', scope='series', summary='Series')
    assert writes(api)[-1].url.path.endswith('/events/master')
    assert writes(api)[-1].headers['if-match'] == '"master-etag"'
    calendar.delete_event('master', '"master-etag"', scope='series')


@pytest.mark.parametrize('method', ['update', 'delete'])
def test_412_is_clear(setup, method):
    calendar, _, api = setup
    api.write_status = 412
    with pytest.raises(CalendarError, match='event changed since it was read; re-read and retry'):
        if method == 'update':
            calendar.update_event('event', '"old"', summary='New')
        else:
            calendar.delete_event('event', '"old"')


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429, 500, 302])
def test_api_errors_safe(setup, status):
    calendar, _, api = setup
    api.error = status
    with pytest.raises(CalendarError) as exc:
        calendar.read_event('event')
    assert 'SECRET' not in str(exc.value) and 'fake' not in str(exc.value)


@pytest.mark.parametrize('gmail,enabled,write', [(False, False, False), (True, False, False),
    (False, True, False), (False, True, True), (True, True, False), (True, True, True)])
def test_tool_registration_and_roundtrip(setup, tmp_path, gmail, enabled, write):
    calendar, google, api = setup
    async def go():
        server = create_server(CorpusReader(tmp_path),
                               gmail_reader=GmailReader(client_factory=lambda: google) if gmail else None,
                               calendar_client=calendar if enabled else None, calendar_write=write)
        async with create_connected_server_and_client_session(server) as session:
            tools = (await session.list_tools()).tools
            assert len(tools) == 5 + 3 * gmail + (3 + 3 * write) * enabled
            assert not api.requests
            for t in tools:
                assert 'send' not in t.name.lower()
                if t.name.endswith('_event') or t.name in ('list_calendars', 'list_events'):
                    writing = t.name in ('create_event', 'update_event', 'delete_event')
                    assert t.annotations.openWorldHint
                    assert t.annotations.readOnlyHint is not writing
                    assert t.annotations.idempotentHint is not writing
                    assert t.annotations.destructiveHint is (t.name in ('update_event', 'delete_event'))
                    assert 'untrusted evidence' in t.description
            if enabled:
                result = await session.call_tool('read_event', {'event_id': 'event'})
                assert not result.isError and result.structuredContent['etag'] == '"etag"'
                if write:
                    result = await session.call_tool('update_event', {'event_id': 'event', 'etag': '"etag"', 'summary': 'Updated'})
                    assert not result.isError
                api.error = 403
                result = await session.call_tool('read_event', {'event_id': 'event'})
                assert result.isError and 'SECRET' not in str(result.content)
    asyncio.run(go())
