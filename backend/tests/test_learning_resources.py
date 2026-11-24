"""F07Z-86: the learning-resource list and the user's own links.

What is pinned here: the shipped list is addresses only (no fetching), the file holds only user
entries, duplicate addresses are refused, `javascript:`/`file:`/credentials-in-URL are refused,
and a shipped entry cannot be deleted through the user endpoint.
"""
import json
import uuid

import pytest

from test_postgres import client_for, pg_settings, signup

pytestmark = [pytest.mark.postgres]

ADDRESS = '/api/desktop/learning-resources'


@pytest.fixture
def resource_context(pg_settings, tmp_path, monkeypatch):
    store = tmp_path / 'learning-resources.json'
    monkeypatch.setenv('INTERVIEW_LEARNING_RESOURCES', str(store))
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        yield client, store
        from test_postgres import query
        query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


def test_shipped_list_is_verified_addresses_and_says_so(resource_context):
    client, store = resource_context
    payload = client.get(ADDRESS).json()
    items = payload['items']
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids)) >= 12
    assert all(item['url'].startswith('https://') for item in items)
    assert all(item['name'] and item['blurb'] for item in items)
    assert all(item['source'] == 'default' for item in items)
    assert payload['checked_on'] == '2026-11-15'
    # The page must not imply that a user link was checked.
    assert '没有核对' in payload['note']
    # Reading the list never writes the user's file.
    assert not store.exists()


def test_user_links_persist_and_defaults_stay_put(resource_context):
    client, store = resource_context
    created = client.post(ADDRESS, json={'name': '我的一门课',
                                         'url': 'https://example.edu/course/intro'})
    assert created.status_code == 201
    items = created.json()['items']
    mine = [item for item in items if item['source'] == 'user']
    assert [item['name'] for item in mine] == ['我的一门课']
    assert mine[0]['id'].startswith('u-')
    assert mine[0]['topic'] == '我添加的'
    assert store.is_file()
    stored = json.loads(store.read_text(encoding='utf-8'))
    assert [item['url'] for item in stored['items']] == ['https://example.edu/course/intro']

    # A shipped entry is still there, and cannot be deleted through the user endpoint.
    first_default = next(item for item in items if item['source'] == 'default')
    refused = client.delete(f'{ADDRESS}/{first_default["id"]}')
    assert refused.status_code == 404
    assert refused.json()['code'] == 'learning_resource_not_found'

    removed = client.delete(f'{ADDRESS}/{mine[0]["id"]}')
    assert removed.status_code == 200
    assert not [item for item in removed.json()['items'] if item['source'] == 'user']
    assert json.loads(store.read_text(encoding='utf-8'))['items'] == []


def test_duplicate_and_unsafe_addresses_are_refused(resource_context):
    client, _ = resource_context
    client.post(ADDRESS, json={'name': 'dup', 'url': 'https://example.org/learn/'})
    again = client.post(ADDRESS, json={'name': 'dup again', 'url': 'https://Example.org/learn'})
    assert again.status_code == 409 and again.json()['code'] == 'learning_resource_exists'

    shipped = client.post(ADDRESS, json={'name': 'shipped again',
                                         'url': 'https://developer.mozilla.org/zh-CN'})
    assert shipped.status_code == 409

    for bad, code in [
        ('javascript:alert(1)', 'learning_url_scheme_not_allowed'),
        ('file:///C:/Windows/win.ini', 'learning_url_scheme_not_allowed'),
        ('https://user:secret@example.com/', 'learning_url_credentials_not_allowed'),
        ('https:///', 'learning_url_host_missing'),
    ]:
        refused = client.post(ADDRESS, json={'name': 'bad', 'url': bad})
        assert refused.status_code == 400, bad
        assert refused.json()['code'] == code, bad

    blank = client.post(ADDRESS, json={'name': '   ', 'url': 'https://example.net/'})
    assert blank.status_code == 400

    too_long = client.post(ADDRESS, json={'name': 'x' * 61, 'url': 'https://example.net/'})
    assert too_long.status_code in (400, 422)


def test_list_requires_a_session(pg_settings):
    with client_for(pg_settings[0]) as client:
        assert client.get(ADDRESS).status_code == 401
