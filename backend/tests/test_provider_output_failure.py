"""F07Z-26: a rejected provider response must be attributable without storing its content.

Before this, `invalid_tool_calls` and `reasoning_content` collapsed into one
`invalid_provider_output`, so a real failure could not be diagnosed (F07Z-21/22).
"""
import asyncio
from types import SimpleNamespace

import pytest

from interview_agent.runtime.deepseek import provider_output_failure, salvage_tool_call
from interview_agent.runtime.types import ScopeError


def response(**overrides):
    body = {'invalid_tool_calls': [], 'additional_kwargs': {}, 'tool_calls': [], 'content': '',
            'response_metadata': {}, 'usage_metadata': {}}
    body.update(overrides)
    return SimpleNamespace(**body)


def test_invalid_tool_calls_and_reasoning_are_different_codes():
    tool_code, _ = provider_output_failure(
        response(invalid_tool_calls=[{'error': 'unparsable'}]))
    reasoning_code, _ = provider_output_failure(
        response(additional_kwargs={'reasoning_content': 'thinking'}))
    assert tool_code == 'invalid_provider_tool_calls'
    assert reasoning_code == 'invalid_provider_reasoning'
    assert tool_code != reasoning_code


def test_reasoning_wins_when_both_are_present():
    code, diagnostics = provider_output_failure(
        response(invalid_tool_calls=[{'error': 'x'}],
                 additional_kwargs={'reasoning_content': 'thinking'}))
    assert code == 'invalid_provider_reasoning' and diagnostics['branch'] == 'reasoning_content'


def test_diagnostics_describe_shape_only():
    content = '这是一份简历的正文，包含隐私内容'
    code, diagnostics = provider_output_failure(response(
        invalid_tool_calls=[{'error': 'bad json'}],
        tool_calls=[{'args': {'text': content}}], content=content,
        response_metadata={'finish_reason': 'length'},
        usage_metadata={'output_tokens': 2048}))
    assert code == 'invalid_provider_tool_calls'
    assert diagnostics['finish_reason'] == 'length'
    assert diagnostics['output_tokens'] == 2048
    assert diagnostics['invalid_tool_call_count'] == 1
    assert diagnostics['max_arg_chars'] > 0
    assert diagnostics['content_chars'] == len(content)
    # nothing from the model output is copied into diagnostics
    assert content not in str(diagnostics)
    assert 'args' not in diagnostics


def test_each_rejected_call_reports_its_shape_without_its_text(tmp_path, monkeypatch):
    """F07Z-34: an unparsable tool call must still say how big it was, or the next step is
    another paid run with no new information."""
    payload = '{"education": ["S001"] ,, }'
    code, diagnostics = provider_output_failure(response(
        invalid_tool_calls=[{'name': 'submit_rewrite', 'args': payload, 'error': 'Expecting value'},
                            {'name': 'submit_rewrite', 'args': None, 'error': 'bad'}]))
    assert code == 'invalid_provider_tool_calls'
    assert diagnostics['invalid_calls'] == [{'arg_chars': len(payload), 'error': 'Expecting value'},
                                            {'arg_chars': -1, 'error': 'bad'}]
    assert payload not in str(diagnostics)


def test_the_raw_capture_is_off_unless_a_private_path_is_given(tmp_path, monkeypatch):
    payload = '{"education": ["S001"] ,, }'
    monkeypatch.delenv('INTERVIEW_PROVIDER_DUMP', raising=False)
    provider_output_failure(response(invalid_tool_calls=[{'args': payload, 'error': 'x'}]))
    assert list(tmp_path.iterdir()) == []

    target = tmp_path / 'dump.jsonl'
    monkeypatch.setenv('INTERVIEW_PROVIDER_DUMP', str(target))
    provider_output_failure(response(invalid_tool_calls=[{'name': 'submit_rewrite',
                                                          'args': payload, 'error': 'x'}]))
    written = target.read_text(encoding='utf-8').strip()
    assert 'submit_rewrite' in written and 'S001' in written


def test_a_complete_tool_call_with_trailing_junk_is_salvaged():
    """Real F07Z-40 rejection: one extra closing brace killed a paid run."""
    assert salvage_tool_call('{"items": [], "skills": ["S001"]}}') == {'items': [], 'skills': ['S001']}
    assert salvage_tool_call('{"a": 1}') is None          # normal parse, nothing to repair
    assert salvage_tool_call('{"a": ') is None            # truncated stays invalid
    assert salvage_tool_call('[1]}') is None              # not an object
    assert salvage_tool_call(None) is None


def test_scope_error_carries_diagnostics_and_defaults_to_empty():
    error = ScopeError('invalid_provider_reasoning', diagnostics={'branch': 'reasoning_content'})
    assert error.code == 'invalid_provider_reasoning'
    assert error.diagnostics['branch'] == 'reasoning_content'
    assert ScopeError('x').diagnostics == {}


def test_material_agent_records_the_provider_diagnostics():
    from interview_agent.materials.agent import run_material_agent

    class Broken:
        async def complete(self, messages, tools):
            raise ScopeError('invalid_provider_reasoning',
                             diagnostics={'branch': 'reasoning_content', 'output_tokens': 2048})

    scope = {'mode': 'draft', 'draft_style': 'rewrite',
             'sources': [{'id': '6f1c2b7e-0000-4000-8000-000000000001', 'name': 'x',
                          'text': '负责缓存改造'}],
             'target': {'kind': 'user_jd', 'text': 'jd', 'content_sha256': 'y'},
             'facts': []}

    async def run():
        with pytest.raises(Exception) as raised:
            await run_material_agent(scope, Broken(), asyncio.Event())
        return raised.value

    error = asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    assert error.code == 'invalid_provider_reasoning'
    assert error.diagnostic['provider']['branch'] == 'reasoning_content'
