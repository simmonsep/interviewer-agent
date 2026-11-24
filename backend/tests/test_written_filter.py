"""F07Z-91: build a set from question kinds, optionally at random.

These tests pin the filtered-pool path: the session contains only the requested kinds, a pool
that cannot fill the requested count is refused, and a random pick records the pool size and the
seed it used.
"""
import re

import pytest

from test_postgres import client_for, pg_settings, signup, query
from test_written_question_sets import base, post

pytestmark = [pytest.mark.postgres]

WRITTEN = '00000000-0000-4000-8000-000000000003'   # 3 items: single, multiple, short_answer
APTITUDE = '00000000-0000-4000-8000-000000000004'   # 20 single_choice
KNOWLEDGE = '00000000-0000-4000-8000-000000000001'  # 4 knowledge (java-backend only)


def filtered(c, g, body, **kwargs):
    return post(c, base(g) + '/written',
        {'expected_version': 1, 'role': 'java-backend', **body, **kwargs})


def detail_of(c, g, response):
    assert response.status_code == 202, response.text
    return c.get(base(g) + '/written/' + response.json()['id']).json()


def items_of(payload):
    return [q['bank_item']['item_id'] for q in payload['questions']]


def kinds_of(payload):
    return [q['kind'] for q in payload['questions']]


@pytest.fixture
def goal(pg_settings):
    with client_for(pg_settings[0]) as c:
        c.headers['Origin'] = pg_settings[0].origin
        uid = signup(c)['user']['id']
        g = post(c, '/api/career-goals/', {'message': '按题型组卷',
            'conditions': {'direction': 'Java后端'}}).json()
        yield c, g
    query(pg_settings[1], 'DELETE FROM auth.users WHERE id=%s RETURNING id', (uid,))


def test_a_kind_filter_builds_the_set_from_that_kind(goal):
    c, g = goal
    payload = detail_of(c, g, filtered(c, g, {'question_banks': [{'bank_id': WRITTEN, 'version': 1}],
        'item_kinds': ['short_answer'], 'max_questions': 1}))
    assert items_of(payload) == ['idempotency-design']
    basis = payload['selection_basis']
    assert basis['policy'] == 'filtered_first_items_v1'
    assert basis['item_kinds'] == ['short_answer']
    assert basis['available_questions'] == 1
    assert basis['selected_questions'] == 1


def test_a_random_pick_samples_the_filtered_pool_and_records_the_seed(goal):
    c, g = goal
    payload = detail_of(c, g, filtered(c, g, {'question_banks': [{'bank_id': APTITUDE, 'version': 1}],
        'item_kinds': ['single_choice'], 'shuffle': True, 'max_questions': 4}))
    picked = items_of(payload)
    assert len(picked) == 4 and len(set(picked)) == 4
    assert set(kinds_of(payload)) == {'single_choice'}
    basis = payload['selection_basis']
    assert basis['policy'] == 'filtered_random_items_v1'
    assert basis['available_questions'] == 20
    assert re.fullmatch(r'[0-9a-f]{32}', basis['seed']), basis


def test_shuffle_without_kinds_uses_the_whole_bank_pool(goal):
    c, g = goal
    payload = detail_of(c, g, filtered(c, g, {'question_banks': [{'bank_id': APTITUDE, 'version': 1}],
        'shuffle': True, 'max_questions': 2}))
    assert len(items_of(payload)) == 2
    assert payload['selection_basis']['policy'] == 'random_items_from_bank_v1'
    assert payload['selection_basis']['seed']


def test_a_kind_filter_caps_at_what_the_bank_has(goal):
    c, g = goal
    payload = detail_of(c, g, filtered(c, g, {'question_banks': [{'bank_id': WRITTEN, 'version': 1}],
        'item_kinds': ['multiple_choice'], 'max_questions': 2}))
    assert items_of(payload) == ['bank-admission']
    assert payload['selection_basis']['selected_questions'] == 1
    assert payload['selection_basis']['available_questions'] == 1


def test_a_random_pick_larger_than_the_pool_is_refused(goal):
    c, g = goal
    too_few = filtered(c, g, {'question_banks': [{'bank_id': WRITTEN, 'version': 1}],
        'item_kinds': ['multiple_choice'], 'shuffle': True, 'max_questions': 2})
    assert too_few.status_code == 400
    assert too_few.json()['code'] == 'written_selection_too_few_items'
    # The named bank has no item of that kind at all - a different refusal than "too few".
    no_kind = filtered(c, g, {'question_banks': [{'bank_id': KNOWLEDGE, 'version': 1}],
        'item_kinds': ['single_choice'], 'max_questions': 1})
    assert no_kind.status_code == 400
    assert no_kind.json()['code'] == 'question_bank_kind_not_supported'


def test_conflicting_or_invalid_filters_are_schema_errors(goal):
    c, g = goal
    common = {'expected_version': 1, 'role': 'java-backend',
              'question_banks': [{'bank_id': WRITTEN, 'version': 1}], 'max_questions': 1}
    # Contract refusals come back as 400 (the API reports model-validator refusals under one
    # generic code); only a value that is not a known kind is a schema error (422).
    both = post(c, base(g) + '/written', {**common, 'item_ids': ['missing-reference'],
        'item_kinds': ['single_choice']})
    assert both.status_code == 400
    with_shuffle = post(c, base(g) + '/written', {**common, 'item_ids': ['missing-reference'],
        'shuffle': True})
    assert with_shuffle.status_code == 400
    empty = post(c, base(g) + '/written', {**common, 'item_kinds': []})
    assert empty.status_code == 400
    unknown = post(c, base(g) + '/written', {**common, 'item_kinds': ['essay']})
    assert unknown.status_code == 400
    duplicated = post(c, base(g) + '/written', {**common,
        'item_kinds': ['single_choice', 'single_choice']})
    assert duplicated.status_code == 400
