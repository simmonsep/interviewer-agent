"""F07Z-89: choosing the questions, not just how many.

The written session used to accept "this bank, this role, at most N questions" and take the
first N. These tests pin the explicit-selection path: exactly the requested items, in the
requested order, and a clear refusal when an item does not exist or cannot be answered by that
role.
"""
import pytest

from test_postgres import client_for, pg_settings, signup, query
from test_written_question_sets import base, post

pytestmark = [pytest.mark.postgres]

# 0000…0001 is the built-in java-backend bank; 0000…0004 is the aptitude bank.
JAVA = '00000000-0000-4000-8000-000000000001'
APTITUDE = '00000000-0000-4000-8000-000000000004'


def selected(c, g, items, bank=JAVA, **kwargs):
    return post(c, base(g) + '/written', {'expected_version': 1, 'role': 'java-backend',
        'question_banks': [{'bank_id': bank, 'version': 1}],
        'item_ids': items, 'max_questions': len(items), **kwargs})


def items_of(c, g, s):
    return [q['bank_item']['item_id'] for q in
            c.get(base(g) + '/written/' + s['id']).json()['questions']]


@pytest.fixture
def goal(pg_settings):
    with client_for(pg_settings[0]) as c:
        c.headers['Origin'] = pg_settings[0].origin
        uid = signup(c)['user']['id']
        g = post(c, '/api/career-goals/', {'message': '选定题目的笔试演练',
            'conditions': {'direction': 'Java后端'}}).json()
        yield c, g
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def test_explicit_items_are_used_in_the_requested_order(goal):
    c, g = goal
    response = selected(c, g, ['redis-lock', 'sql-isolation'])
    assert response.status_code == 202, response.text
    session = response.json()
    assert items_of(c, g, session) == ['redis-lock', 'sql-isolation']
    payload = c.get(base(g) + '/written/' + session['id']).json()
    basis = payload['selection_basis']
    assert basis['policy'] == 'explicit_items_in_user_order'
    assert basis['selected_questions'] == 2


def test_the_same_request_key_returns_the_same_session(goal):
    c, g = goal
    body = {'expected_version': 1, 'role': 'java-backend',
            'question_banks': [{'bank_id': JAVA, 'version': 1}],
            'item_ids': ['redis-lock'], 'max_questions': 1}
    first = post(c, base(g) + '/written', body, 'explicit-replay')
    second = post(c, base(g) + '/written', body, 'explicit-replay')
    # A replay reports the same session with 200; the first creation used 202.
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()['id'] == second.json()['id']


def test_unknown_item_and_ambiguous_requests_are_refused(goal):
    c, g = goal
    unknown = selected(c, g, ['no-such-item'])
    assert unknown.status_code == 400 and unknown.json()['code'] == 'written_item_not_found'
    # A count that disagrees with the selection would make the request mean two things.
    mismatched = post(c, base(g) + '/written', {'expected_version': 1, 'role': 'java-backend',
        'question_banks': [{'bank_id': JAVA, 'version': 1}],
        'item_ids': ['redis-lock', 'sql-isolation'], 'max_questions': 5})
    assert mismatched.status_code == 400
    duplicated = post(c, base(g) + '/written', {'expected_version': 1, 'role': 'java-backend',
        'question_banks': [{'bank_id': JAVA, 'version': 1}],
        'item_ids': ['redis-lock', 'redis-lock'], 'max_questions': 2})
    assert duplicated.status_code == 400
    without_bank = post(c, base(g) + '/written', {'expected_version': 1,
        'item_ids': ['redis-lock'], 'max_questions': 1})
    assert without_bank.status_code == 400
    empty = selected(c, g, [])
    assert empty.status_code == 400


def test_items_outside_the_named_bank_are_not_reachable(goal):
    c, g = goal
    # `project-idempotency` lives in the private example bank, which this request did not name.
    outside = selected(c, g, ['project-idempotency'])
    assert outside.status_code == 400 and outside.json()['code'] == 'written_item_not_found'
    # Another built-in bank is fine as long as the item belongs to it and supports the role.
    aptitude = selected(c, g, ['math-work-days'], bank=APTITUDE)
    assert aptitude.status_code == 202, aptitude.text
    assert items_of(c, g, aptitude.json()) == ['math-work-days']
