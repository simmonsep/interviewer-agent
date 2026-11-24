"""F07Z-117: long material is accepted and condensed in one stage.

Ruling: no second compression agent; the rewrite batch window grows up to six batches, and
`read_source` only sends the first batch. Material past the budget is truncated and reported.
"""
import json
from uuid import uuid4

import pytest

from interview_agent.materials.segments import (narrative_batches, plan_batches,
                                                pre_heading_ids, request_ceiling,
                                                rewrite_observation, scope_segments)
from test_material_repository import (material_context, process, source, start, view)
from test_postgres import arun, pg_settings  # noqa: F401  (fixture re-exported)
from test_worker import worker_settings  # noqa: F401  (fixture re-exported)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def long_text(chars, heading='工作经历'):
    lines = [heading]
    index = 0
    while sum(len(line) for line in lines) < chars:
        index += 1
        lines.append(f'2018.{index % 12 + 1:02d} 某公司 后端开发实习生 负责订单查询接口与缓存改造，'
                     f'使用 Java 与 Redis，接口延迟下降 30%（第 {index} 段详细描述）')
    return '\n'.join(lines)


def segments_of(text):
    scope = {'draft_style': 'rewrite', 'sources': [{'id': 's1', 'name': 'r', 'text': text,
                                                    'content_sha256': 'x'}], 'facts': []}
    segments = scope_segments(scope)
    header = set(pre_heading_ids(segments))
    narrative = [s for s in segments if s['id'] not in header]
    return scope['sources'][0], narrative


def test_short_material_keeps_the_original_eight_segment_batches():
    _, narrative = segments_of('\n'.join('line %d' % n for n in range(48)))
    assert plan_batches(narrative)[0] == narrative_batches(narrative)
    assert plan_batches(narrative)[1] == []


def test_long_material_fits_six_batches_and_twelve_requests():
    for chars in (4000, 6000, 8000, 12000):
        source_row, narrative = segments_of(long_text(chars))
        batches, truncated = plan_batches(narrative)
        assert len(batches) <= 6, (chars, len(batches))
        assert request_ceiling(len(batches)) <= 12, (chars, request_ceiling(len(batches)))
        observation = rewrite_observation(source_row, narrative, batches)
        payload = json.dumps(observation, ensure_ascii=False).encode()
        assert len(payload) < 16384, (chars, len(payload))
        assert [s['id'] for s in observation['segments']] == batches[0]
        if chars >= 12000:
            assert truncated, '12000 characters should exceed the 120-segment budget'


def test_single_source_over_twenty_thousand_characters_is_rejected(material_context):
    c, g = material_context
    response = source(c, g, text=long_text(20001))
    # The DoS ceiling survives the product limit: 20,001 characters is not accepted.
    assert response.status_code == 400, response.text


def test_aggregate_scope_over_twenty_thousand_characters_is_rejected(material_context):
    c, g = material_context
    first = source(c, g, text=long_text(12000))
    assert first.status_code == 201, first.text
    second = source(c, g, text=long_text(12000), revision=1)
    assert second.status_code == 201, second.text
    # Two valid 12,000-character sources still exceed the 20,000 ceiling when read together.
    response = start(c, g, 2, mode='draft', draft_style='rewrite',
                     source_ids=[first.json()['source_id'], second.json()['source_id']])
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'material_scope_too_large'


def test_rewrite_observation_states_the_batch_protocol_and_follows_the_cursor():
    source_row, narrative = segments_of(long_text(8000))
    batches, _ = plan_batches(narrative)
    assert len(batches) == 6
    first = rewrite_observation(source_row, narrative, batches)
    second = rewrite_observation(source_row, narrative, batches, 1)
    # The live run saw `total_segments: 50` with nine IDs and gave up; the envelope now says
    # which batch this is and that the next one arrives after submit_rewrite.
    assert first['batch'] == 1 and first['total_batches'] == 6
    assert second['batch'] == 2 and second['total_batches'] == 6
    assert [s['id'] for s in first['segments']] == batches[0]
    assert [s['id'] for s in second['segments']] == batches[1]
    assert 'read_source' in first['rule'] and '1/6' in first['rule']
    assert first['total_segments'] == len(narrative)


def test_over_budget_material_is_truncated_not_rejected(material_context, worker_settings):
    c, g = material_context
    text = long_text(13000)
    response = source(c, g, text=text)
    assert response.status_code == 201, response.text
    assert start(c, g, 1, mode='draft', draft_style='rewrite').status_code == 202
    arun(process(worker_settings, provider='scripted'))
    run = view(c, g)['runs'][0]
    assert run['status'] == 'SUCCEEDED', run
    payload = view(c, g)['drafts'][0]['payload']
    assert payload['truncated_segment_ids'], 'truncation must be reported in the payload'
    # Segments the model never saw must not be counted against the draft's coverage.
    assert payload['coverage']['unaccounted'] == 0
    assert payload['candidate_status'] == 'editable_suggestion'


def test_eight_thousand_characters_rewrite_without_unaccounted_segments(material_context,
                                                                        worker_settings):
    c, g = material_context
    response = source(c, g, text=long_text(8000))
    assert response.status_code == 201, response.text
    assert start(c, g, 1, mode='draft', draft_style='rewrite').status_code == 202
    arun(process(worker_settings, provider='scripted'))
    run = view(c, g)['runs'][0]
    assert run['status'] == 'SUCCEEDED', run
    draft = view(c, g)['drafts'][0]
    assert not draft['payload']['truncated_segment_ids']
    assert draft['payload']['coverage']['unaccounted'] == 0
