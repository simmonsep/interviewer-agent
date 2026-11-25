import asyncio

import httpx
import pytest

from interview_agent.connectors.public_reference import CATALOG, PreparationReference, explicit_topic

TOPIC = 'langgraph-interrupts'
PASSAGE = 'A node restarts from the beginning when an interrupted execution resumes.'
MARKDOWN = '# Interrupts\n\n' + PASSAGE


def test_an_explicit_langgraph_requirement_routes_to_the_official_topic():
    assert explicit_topic('会话检查点与恢复（LangGraph）') == 'langgraph-checkpoints'
    assert explicit_topic('用 LangGraph 实现人工确认和进程恢复') == TOPIC
    assert explicit_topic('实现写工具幂等和会话恢复') is None
    assert explicit_topic('熟悉 LangGraph 与 Python') is None


@pytest.mark.parametrize('requested,selected', [
    ('LangGraph 的 checkpointer 和 Store 有什么区别', 'langgraph-stores'),
    ('LangGraph的Store与长期记忆', 'langgraph-stores'),
    ('LangGraph 通过 PostgresStore 实现偏好持久化', 'langgraph-stores'),
    ('LangChain 的结构化输出与工具策略', 'langchain-structured-output'),
    ('采用 LangChain ToolStrategy 处理模型输出', 'langchain-structured-output'),
    ('有项目长期记忆和结构化输出经验', None),
    ('LangGraph 中读取 datastore 里的指标', None),
])
def test_new_official_topics_require_specific_framework_and_concept(requested, selected):
    assert explicit_topic(requested) == selected


def test_catalog_lookup_is_one_fixed_get_with_replayable_source():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=MARKDOWN)

    async def scenario():
        reader = PreparationReference(lambda: True, transport=httpx.MockTransport(handler))
        assert await reader.lookup('https://unlisted.invalid/?private=1') == {'status': 'unknown_topic'}
        result = await reader.lookup(TOPIC)
        assert result['source']['excerpt'] == PASSAGE
        assert result['source']['url'] == CATALOG[TOPIC].url
        assert reader.citation(TOPIC, PASSAGE)['quote'] == PASSAGE
        assert len(reader.citation(TOPIC, PASSAGE)['content_sha256']) == 64
        assert reader.citation(TOPIC, 'Fabricated text with a fabricated answer') is None
        assert reader.citation('langgraph-checkpoints', PASSAGE) is None
        assert await reader.lookup(TOPIC) == {'status': 'lookup_limit'}

    asyncio.run(scenario())
    assert [(call.method, str(call.url), call.content) for call in calls] == [('GET', CATALOG[TOPIC].url, b'')]
    assert 'authorization' not in calls[0].headers and 'cookie' not in calls[0].headers


def test_revoked_setting_prevents_any_transport_call():
    def unexpected(_):
        pytest.fail('disabled connector reached HTTP')

    reader = PreparationReference(lambda: False, transport=httpx.MockTransport(unexpected))
    assert asyncio.run(reader.lookup(TOPIC)) == {'status': 'disabled'}


@pytest.mark.parametrize('outcome,expected', [
    ('timeout', 'unavailable'),
    ('redirect', 'unavailable'),
    ('not_found', 'unavailable'),
    ('html', 'unavailable'),
    ('large', 'unavailable'),
    ('empty', 'no_passage'),
    ('code', 'no_passage'),
    ('ambiguous', 'no_passage'),
])
def test_lookup_failures_do_not_create_evidence(outcome, expected):
    def handler(request):
        if outcome == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        if outcome == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://unlisted.invalid/'})
        if outcome == 'not_found':
            return httpx.Response(404)
        if outcome == 'html':
            return httpx.Response(200, text='<title>Interrupts</title>',
                                  headers={'Content-Type': 'text/html'})
        body = {
            'large': MARKDOWN + '\n' + 'x' * (128 * 1024),
            'empty': '# Interrupts\n\nSome other content',
            'code': '# Interrupts\n\n```python\n' + PASSAGE + '\n```',
            'ambiguous': MARKDOWN + '\n' + 'Documentation that says a ' + PASSAGE[2:],
        }[outcome]
        return httpx.Response(200, text=body)

    reader = PreparationReference(lambda: True, transport=httpx.MockTransport(handler))
    assert asyncio.run(reader.lookup(TOPIC)) == {'status': expected}
    assert reader.citation(TOPIC, PASSAGE) is None
