"""Bounded People API contact reads and non-deleting mutations."""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from typing import Any

import httpx

from john_agent.gmail_client import GmailError

API_ROOT = 'https://people.googleapis.com/v1'
MAX_PAGES = 10
MAX_ITEMS = 20
NOTE_CAP = 12000
_RESPONSE_CAP = 2 * 1024 * 1024
_RESOURCE = re.compile(r'(people|otherContacts)/[A-Za-z0-9_-]{1,256}\Z')
_FIELDS = {
    'names': ('givenName', 'familyName', 'displayName'),
    'emailAddresses': ('value', 'type', 'formattedType'),
    'phoneNumbers': ('value', 'type', 'formattedType', 'canonicalForm'),
    'addresses': ('formattedValue', 'streetAddress', 'extendedAddress', 'poBox', 'city',
                  'region', 'postalCode', 'country', 'countryCode', 'type', 'formattedType'),
    'organizations': ('name', 'title'),
    'birthdays': ('date', 'text'),
    'biographies': ('value', 'contentType'),
    'urls': ('value', 'type', 'formattedType'),
    'memberships': ('contactGroupMembership',),
}
READ_MASK = ','.join(_FIELDS)
_OTHER_MASK = 'names,emailAddresses,phoneNumbers'
_ERRORS = {
    'invalid_input': 'Check contact identifiers, field values, pagination, and requested limits.',
    'invalid_response': 'Contacts returned an invalid or unsupported response.',
    'authorization': 'Contacts authorization is unavailable; rerun john-google-auth with the required scopes.',
    'permission': 'Contacts access was not granted or is unavailable.',
    'unavailable': 'Contacts are temporarily unavailable; try again later.',
    'not_found': 'Contact not found.',
    'lookup_limit': 'Contact not found within the bounded other-contacts lookup (10 pages).',
    'changed': 'contact changed since it was read; re-read and retry',
    'other_contact': 'Other contacts cannot be updated; use create_contact with the data you read.',
    'label': 'Unknown or ambiguous user contact label; no labels were created.',
    'groups_limit': 'Contact group lookup exceeded its page limit; no labels were created.',
}


class ContactsError(Exception):
    def __init__(self, code, matches=None):
        self.code = code
        self.matching_resource_names = matches or []
        message = (_ERRORS[code] if code != 'duplicate' else
                   'Matching contacts already exist: ' + ', '.join(self.matching_resource_names)
                   + '. Review them or explicitly set allow_duplicate=True.')
        super().__init__(message)


def _text(value, cap=2000):
    if not isinstance(value, str) or len(value) > cap or '\x00' in value:
        raise ContactsError('invalid_input')
    return value


def _resource(value):
    if not isinstance(value, str) or not _RESOURCE.fullmatch(value):
        raise ContactsError('invalid_input')
    return value


class ContactsClient:
    def __init__(self, *, token_provider, http_client=None):
        self.tokens = token_provider
        self._http = http_client
        self._lock = threading.RLock()
        self._warmed = set()

    def _request(self, method, path, *, params=None, body=None):
        if self._http is None:
            self._http = httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0)
        for attempt in range(2):
            try:
                token = self.tokens.refresh(force=bool(attempt))
                with self._http.stream(method, API_ROOT + path, params=params, json=body,
                                       headers={'Authorization': f'Bearer {token}'},
                                       follow_redirects=False, timeout=10.0) as response:
                    status = response.status_code
                    if status == 401 and not attempt:
                        continue
                    if status == 412:
                        raise ContactsError('changed')
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _RESPONSE_CAP:
                            raise ContactsError('invalid_response')
                        chunks.append(chunk)
                    try:
                        payload = json.loads(b''.join(chunks))
                    except (ValueError, UnicodeError):
                        payload = None
                    if status == 400:
                        error = payload.get('error', {}) if isinstance(payload, dict) else {}
                        errors = error.get('errors', []) if isinstance(error, dict) else []
                        errors = errors if isinstance(errors, list) else []
                        stale = isinstance(error, dict) and error.get('status') == 'FAILED_PRECONDITION'
                        stale = stale or any(isinstance(e, dict) and e.get('reason') in
                                             ('failedPrecondition', 'FAILED_PRECONDITION') for e in errors)
                        raise ContactsError('changed' if stale else 'invalid_input')
                    if status in (401, 403):
                        raise ContactsError('authorization' if status == 401 else 'permission')
                    if status == 404:
                        raise ContactsError('not_found')
                    if status == 429 or status >= 500:
                        raise ContactsError('unavailable')
                    if status not in (200, 201) or not isinstance(payload, dict):
                        raise ContactsError('invalid_response')
                    return payload
            except GmailError:
                raise ContactsError('authorization') from None
            except httpx.HTTPError:
                raise ContactsError('unavailable') from None
        raise ContactsError('authorization')

    def _groups(self):
        groups, token = {}, None
        for _ in range(MAX_PAGES):
            params = {'pageSize': '1000', 'groupFields': 'name,groupType'}
            if token:
                params['pageToken'] = token
            page = self._request('GET', '/contactGroups', params=params)
            for group in page.get('contactGroups', [])[:1000]:
                resource = group.get('resourceName')
                if isinstance(resource, str) and re.fullmatch(r'contactGroups/[A-Za-z0-9_-]{1,256}', resource):
                    groups[resource] = group
            token = page.get('nextPageToken')
            if not token:
                return groups
        raise ContactsError('groups_limit')

    @staticmethod
    def _person(person, groups=None, other=False):
        resource = _resource(person.get('resourceName'))
        result = {'resource_name': resource, 'etag': str(person.get('etag', ''))[:1024],
                  'source': 'other_contact' if other else 'contact'}
        truncated = []
        for field, allowed in _FIELDS.items():
            if other and field not in ('names', 'emailAddresses', 'phoneNumbers'):
                continue
            values = person.get(field, [])
            if not isinstance(values, list):
                raise ContactsError('invalid_response')
            if len(values) > MAX_ITEMS:
                truncated.append(field)
            result[field] = []
            for item in values[:MAX_ITEMS]:
                if not isinstance(item, dict):
                    raise ContactsError('invalid_response')
                mapped = {}
                for key in allowed:
                    value = item.get(key)
                    if isinstance(value, str):
                        cap = NOTE_CAP if field == 'biographies' else 2000
                        mapped[key] = value[:cap]
                        if len(value) > cap:
                            truncated.append(field)
                    elif key == 'date' and isinstance(value, dict):
                        mapped[key] = {k: value[k] for k in ('year', 'month', 'day')
                                       if isinstance(value.get(k), int) and not isinstance(value[k], bool)}
                    elif key == 'contactGroupMembership' and isinstance(value, dict):
                        mapped[key] = {k: str(value[k])[:1024] for k in
                                       ('contactGroupResourceName', 'contactGroupId') if k in value}
                result[field].append(mapped)
        if not other:
            result['labels'] = [str(groups.get(m.get('contactGroupMembership', {}).get(
                'contactGroupResourceName'), {}).get('name', ''))[:2000]
                for m in result['memberships']]
        result['truncated_fields'] = sorted(set(truncated))
        return result

    def _search(self, query, limit, other=False):
        path = '/otherContacts:search' if other else '/people:searchContacts'
        params = {'query': query, 'pageSize': str(limit), 'readMask': _OTHER_MASK if other else READ_MASK}
        with self._lock:
            if path not in self._warmed:
                self._request('GET', path, params={**params, 'query': ''})
                self._warmed.add(path)
            page = self._request('GET', path, params=params)
        return [r['person'] for r in page.get('results', [])[:limit] if isinstance(r, dict) and 'person' in r]

    def search_contacts(self, query: str, limit: int = 10,
                        include_other_contacts: bool = True) -> dict[str, Any]:
        if not _text(query, 1000).strip() or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ContactsError('invalid_input')
        own = self._search(query, limit)
        others = self._search(query, limit, other=True) if include_other_contacts else []
        groups = self._groups() if own else {}
        results = []
        for i in range(limit):
            if i < len(own):
                results.append(self._person(own[i], groups))
            if i < len(others):
                results.append(self._person(others[i], other=True))
        return {'contacts': results[:limit]}

    def list_contacts(self, limit: int = 50, page_token: str | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContactsError('invalid_input')
        params = {'pageSize': str(limit), 'personFields': READ_MASK, 'sortOrder': 'FIRST_NAME_ASCENDING'}
        if page_token is not None:
            params['pageToken'] = _text(page_token, 4096)
        page = self._request('GET', '/people/me/connections', params=params)
        people = page.get('connections', [])[:limit]
        groups = self._groups() if people else {}
        return {'contacts': [self._person(p, groups) for p in people],
                'next_page_token': page.get('nextPageToken')}

    def _get(self, resource_name):
        return self._request('GET', '/' + _resource(resource_name),
                             params={'personFields': READ_MASK + ',metadata',
                                     'sources': 'READ_SOURCE_TYPE_CONTACT'})

    def read_contact(self, resource_name: str) -> dict[str, Any]:
        _resource(resource_name)
        if resource_name.startswith('otherContacts/'):
            token = None
            for _ in range(MAX_PAGES):
                params = {'pageSize': '100', 'readMask': _OTHER_MASK}
                if token:
                    params['pageToken'] = token
                page = self._request('GET', '/otherContacts', params=params)
                for person in page.get('otherContacts', [])[:100]:
                    if person.get('resourceName') == resource_name:
                        return self._person(person, other=True)
                token = page.get('nextPageToken')
                if not token:
                    raise ContactsError('not_found')
            raise ContactsError('lookup_limit')
        return self._person(self._get(resource_name), self._groups())

    @staticmethod
    def _body(given_name, family_name, emails, phones, addresses, organizations,
              birthday, notes, urls):
        body = {}
        if given_name is not None or family_name is not None:
            name = {}
            for key, value in [('givenName', given_name), ('familyName', family_name)]:
                if value is not None:
                    name[key] = _text(value)
            body['names'] = [name] if any(name.values()) else []
        for key, values in [('emailAddresses', emails), ('phoneNumbers', phones),
                            ('addresses', addresses), ('organizations', organizations), ('urls', urls)]:
            if values is None:
                continue
            if not isinstance(values, list) or len(values) > MAX_ITEMS:
                raise ContactsError('invalid_input')
            allowed = set(_FIELDS[key]) - {'formattedType', 'canonicalForm'}
            body[key] = []
            for value in values:
                if isinstance(value, str) and key in ('emailAddresses', 'phoneNumbers', 'urls'):
                    value = {'value': value}
                if not isinstance(value, dict) or not value or not set(value) <= allowed:
                    raise ContactsError('invalid_input')
                item = {k: _text(v) for k, v in value.items()}
                if key in ('emailAddresses', 'phoneNumbers', 'urls') and not item.get('value', '').strip():
                    raise ContactsError('invalid_input')
                body[key].append(item)
        if birthday is not None:
            if not isinstance(birthday, dict) or not set(birthday) <= {'year', 'month', 'day'}:
                raise ContactsError('invalid_input')
            if birthday:
                year, month, day = (birthday.get(k, 0) for k in ('year', 'month', 'day'))
                if any(isinstance(n, bool) or not isinstance(n, int) for n in (year, month, day)):
                    raise ContactsError('invalid_input')
                try:
                    date(year or 2000, month, day)
                except ValueError:
                    raise ContactsError('invalid_input') from None
            body['birthdays'] = [{'date': birthday}] if birthday else []
        if notes is not None:
            notes = _text(notes, NOTE_CAP)
            body['biographies'] = [{'value': notes, 'contentType': 'TEXT_PLAIN'}] if notes else []
        return body

    def _memberships(self, labels, groups, original=None):
        if not isinstance(labels, list) or len(labels) > MAX_ITEMS:
            raise ContactsError('invalid_input')
        selected = []
        for label in labels:
            label = _text(label).casefold()
            matches = [resource for resource, g in groups.items()
                       if g.get('groupType') == 'USER_CONTACT_GROUP' and str(g.get('name', '')).casefold() == label]
            if len(matches) != 1:
                raise ContactsError('label')
            selected.append(matches[0])
        for membership in (original or {}).get('memberships', []):
            resource = membership.get('contactGroupMembership', {}).get('contactGroupResourceName')
            if resource and (resource not in groups or groups[resource].get('groupType') != 'USER_CONTACT_GROUP'):
                selected.append(resource)
        return [{'contactGroupMembership': {'contactGroupResourceName': r}} for r in dict.fromkeys(selected)]

    def _duplicates(self, body):
        matches = set()
        for key in ('emailAddresses', 'phoneNumbers'):
            for item in body.get(key, []):
                value = item['value']
                normalize = (lambda s: s.strip().casefold()) if key == 'emailAddresses' else (lambda s: re.sub(r'\D', '', s))
                needle = normalize(value)
                if not needle:
                    raise ContactsError('invalid_input')
                for person in self._search(value, 30):
                    if any(normalize(p.get('canonicalForm') or p.get('value', '')) == needle
                           for p in person.get(key, [])):
                        matches.add(_resource(person.get('resourceName')))
        if matches:
            raise ContactsError('duplicate', sorted(matches))

    def create_contact(self, given_name: str | None = None, family_name: str | None = None,
                       emails: list[str | dict[str, str]] | None = None,
                       phones: list[str | dict[str, str]] | None = None,
                       addresses: list[dict[str, str]] | None = None,
                       organizations: list[dict[str, str]] | None = None,
                       birthday: dict[str, int] | None = None, notes: str | None = None,
                       urls: list[str | dict[str, str]] | None = None,
                       labels: list[str] | None = None, allow_duplicate: bool = False) -> dict[str, Any]:
        if not isinstance(allow_duplicate, bool):
            raise ContactsError('invalid_input')
        with self._lock:
            body = self._body(given_name, family_name, emails, phones, addresses, organizations, birthday, notes, urls)
            if not any(str(v).strip() for n in body.get('names', []) for v in n.values()) \
                    and not body.get('emailAddresses') and not body.get('phoneNumbers'):
                raise ContactsError('invalid_input')
            groups = self._groups()
            if labels is not None:
                memberships = self._memberships(labels, groups)
                if memberships:
                    body['memberships'] = memberships
            if not allow_duplicate:
                self._duplicates(body)
            person = self._request('POST', '/people:createContact', params={'personFields': READ_MASK}, body=body)
            return self._person(person, groups)

    def update_contact(self, resource_name: str, etag: str, given_name: str | None = None,
                       family_name: str | None = None, emails: list[str | dict[str, str]] | None = None,
                       phones: list[str | dict[str, str]] | None = None,
                       addresses: list[dict[str, str]] | None = None,
                       organizations: list[dict[str, str]] | None = None,
                       birthday: dict[str, int] | None = None, notes: str | None = None,
                       urls: list[str | dict[str, str]] | None = None,
                       labels: list[str] | None = None) -> dict[str, Any]:
        _resource(resource_name)
        if resource_name.startswith('otherContacts/'):
            raise ContactsError('other_contact')
        if not _text(etag, 1024):
            raise ContactsError('invalid_input')
        with self._lock:
            body = self._body(given_name, family_name, emails, phones, addresses, organizations, birthday, notes, urls)
            original = self._get(resource_name)
            if original.get('etag') != etag:
                raise ContactsError('changed')
            groups = self._groups()
            if labels is not None:
                body['memberships'] = self._memberships(labels, groups, original)
            if not body:
                raise ContactsError('invalid_input')
            mask = ','.join(body)
            sources = [{k: s[k] for k in ('type', 'id', 'etag') if k in s}
                       for s in original.get('metadata', {}).get('sources', []) if s.get('type') == 'CONTACT']
            if not sources or any(not s.get('etag') or not s.get('id') for s in sources):
                raise ContactsError('invalid_response')
            body.update(etag=etag, metadata={'sources': sources})
            person = self._request('PATCH', '/' + resource_name + ':updateContact',
                                   params={'updatePersonFields': mask, 'personFields': READ_MASK}, body=body)
            return self._person(person, groups)

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None
