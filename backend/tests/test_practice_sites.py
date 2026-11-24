"""F07Z-57: practice records and the external-site entry.

The product records what the user did on 牛客/力扣 by hand; it does not judge and does not
read the site. These tests pin the boundary: the approved URL list, the owner scope, the
append-only record shape, and idempotent replay.
"""
import json
import uuid
from datetime import date, timedelta

import pytest

from interview_agent.desktop import practice_sites
from test_postgres import client_for, pg_settings, signup
from test_worker import worker_settings
from uuid import uuid4

pytestmark = [pytest.mark.postgres]


@pytest.fixture
def practice_context(pg_settings):
    with client_for(pg_settings[0]) as client:
        client.headers['Origin'] = pg_settings[0].origin
        user = signup(client)['user']['id']
        goal = client.post('/api/career-goals/', json={'message': '算法练习验收'},
                           headers={'Idempotency-Key': uuid4().hex}).json()
        yield client, goal
        from test_postgres import query
        query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (user,))


def test_site_list_is_read_only_and_names_both_sites(practice_context):
    client, _ = practice_context
    payload = client.get('/api/desktop/practice-sites').json()
    ids = [item['id'] for item in payload['items']]
    assert ids == ['nowcoder', 'leetcode', 'luogu', 'acwing', 'hackerrank', 'acmcoder',
                   'lanqiao', 'patest', 'codeforces', 'atcoder', 'leetcode-global']
    assert all(item.get('blurb') for item in payload['items'])
    assert all(url['url'].startswith('https://') for item in payload['items'] for url in item['urls'])
    assert '不代替登录' in payload['note']
    assert payload['outcomes'] == ['SOLVED', 'PARTIAL', 'UNSOLVED', 'REVIEWED']
    assert payload['checked_on'] == '2026-11-13'


def test_open_only_accepts_configured_urls_and_calls_the_launcher_once(practice_context, monkeypatch):
    client, _ = practice_context
    calls = []
    monkeypatch.setattr(practice_sites, '_launch', lambda url, profile: calls.append((url, profile)))
    approved = 'https://leetcode.cn/problemset/'
    assert client.post('/api/desktop/practice-sites/open',
                       json={'site_id': 'leetcode', 'url': approved}).status_code == 200
    assert [url for url, _ in calls] == [approved]
    refused = client.post('/api/desktop/practice-sites/open',
                          json={'site_id': 'leetcode', 'url': 'https://example.com/'})
    assert refused.status_code == 400 and refused.json()['code'] == 'practice_url_not_allowed'
    unknown = client.post('/api/desktop/practice-sites/open',
                          json={'site_id': 'no-such-site', 'url': approved})
    assert unknown.status_code == 404 and unknown.json()['code'] == 'practice_site_not_found'


def test_record_round_trip_is_owner_scoped_and_idempotent(practice_context, pg_settings):
    client, goal = practice_context
    body = {'request_id': 'practice-1', 'site_id': 'nowcoder',
            'url': 'https://www.nowcoder.com/exam/oj', 'title': '两数之和',
            'practiced_on': date.today().isoformat(), 'outcome': 'SOLVED', 'note': '哈希表一次过',
            'statement': '给定 nums 与 target，返回两个下标。'}
    created = client.post(f"/api/career-goals/{goal['id']}/practice-records", json=body)
    assert created.status_code == 201 and created.json()['created'] is True, created.text
    replay = client.post(f"/api/career-goals/{goal['id']}/practice-records", json=body)
    assert replay.status_code == 200 and replay.json()['created'] is False

    listing = client.get(f"/api/career-goals/{goal['id']}/practice-records").json()
    assert len(listing['items']) == 1
    item = listing['items'][0]
    assert item['site_label'] == '牛客' and item['outcome'] == 'SOLVED' and item['note'] == '哈希表一次过'
    assert item['statement'] == '给定 nums 与 target，返回两个下标。'

    with client_for(pg_settings[0]) as other:
        other.headers['Origin'] = pg_settings[0].origin
        stranger = signup(other)['user']['id']
        assert other.get(f"/api/career-goals/{goal['id']}/practice-records").status_code == 404
        assert other.post(f"/api/career-goals/{goal['id']}/practice-records", json=body).status_code == 404
        from test_postgres import query
        query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (stranger,))


def test_delete_removes_only_the_owners_record(practice_context, pg_settings):
    client, goal = practice_context
    body = {'request_id': 'practice-del', 'site_id': 'luogu',
            'url': 'https://www.luogu.com.cn/problem/list', 'title': '前缀和',
            'practiced_on': date.today().isoformat(), 'outcome': 'REVIEWED', 'note': ''}
    record_id = client.post(f"/api/career-goals/{goal['id']}/practice-records", json=body).json()['record_id']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin'] = pg_settings[0].origin
        stranger = signup(other)['user']['id']
        assert other.delete(f"/api/career-goals/{goal['id']}/practice-records/{record_id}").status_code == 404
        from test_postgres import query
        query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (stranger,))
    deleted = client.delete(f"/api/career-goals/{goal['id']}/practice-records/{record_id}")
    assert deleted.status_code == 200 and deleted.json()['deleted'] is True
    assert client.delete(f"/api/career-goals/{goal['id']}/practice-records/{record_id}").status_code == 404
    assert client.get(f"/api/career-goals/{goal['id']}/practice-records").json()['items'] == []


def test_record_validation_rejects_future_date_and_foreign_url(practice_context):
    client, goal = practice_context
    base = {'request_id': 'practice-2', 'site_id': 'leetcode',
            'url': 'https://leetcode.cn/problemset/', 'title': '合并区间',
            'practiced_on': date.today().isoformat(), 'outcome': 'PARTIAL', 'note': ''}
    future = client.post(f"/api/career-goals/{goal['id']}/practice-records",
                         json={**base, 'practiced_on': (date.today() + timedelta(days=1)).isoformat()})
    assert future.status_code == 400 and future.json()['code'] == 'practice_date_in_future'
    foreign = client.post(f"/api/career-goals/{goal['id']}/practice-records",
                          json={**base, 'url': 'https://example.com/x'})
    assert foreign.status_code == 400 and foreign.json()['code'] == 'practice_url_not_allowed'
    listing = client.get(f"/api/career-goals/{goal['id']}/practice-records")
    assert listing.status_code == 200, listing.text
    assert listing.json()['items'] == []
