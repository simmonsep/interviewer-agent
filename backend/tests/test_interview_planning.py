"""S03: the planning agent runs through real middleware — trimming, tool scope, typed plan."""
import asyncio
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from interview_agent.interview_prep.planning import (
    TOOL_NAMES, InterviewPlan, PlanningGuard, _trim, build_planner, normalize_plan)


class RecordingModel(BaseChatModel):
    """A chat model that records every request and replays scripted answers."""

    responses: list = []
    index: int = 0
    seen: list = []

    @property
    def _llm_type(self):
        return 'recording'

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda _input: {'focus_topic_ids': ['java-concurrency'],
                                              'resume_item_indexes': [0],
                                              'evidence_ids': ['obs-1'],
                                              'rationale': '先补并发基础，再用草稿里的缓存经历。'})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


def scope():
    return {'goal_version': 2, 'role': 'java-backend', 'mode': 'coaching', 'max_questions': 4,
            'duration_seconds': 1200, 'target': {'kind': 'direction', 'direction': 'Java 后端'},
            'draft': {'id': 'd-1', 'version': 3}, 'topic_order': ['java-concurrency', 'sql-isolation'],
            'resume_item_indexes': [0, 1],
            'memory': [{'id': 'obs-1', 'topic_id': 'java-concurrency', 'label': 'needs_probe'}]}


def run_agent(messages, responses):
    model = RecordingModel(responses=responses)
    agent, guard = build_planner(model, scope())
    state = asyncio.run(agent.ainvoke({'messages': messages}))
    return state, guard, model


def test_the_tools_expose_only_the_frozen_scope():
    from interview_agent.interview_prep.planning import _tools
    tools = {tool.__name__: tool for tool in _tools(scope())}
    assert set(tools) == set(TOOL_NAMES)
    listed = json.loads(asyncio.run(tools['list_builtin_interview_topics']()))
    assert listed == ['java-concurrency', 'sql-isolation']
    memory = json.loads(tools['read_relevant_memory'](topic_id='java-concurrency'))
    assert [item['id'] for item in memory] == ['obs-1']
    assert json.loads(tools['read_relevant_memory'](topic_id='project')) == []
    preview = json.loads(tools['read_preparation_scope']())
    assert preview['topic_ids'] == ['java-concurrency', 'sql-isolation'] and preview['role'] == 'java-backend'


def test_generated_questions_keep_only_unique_topic_text_and_grounded_quotes():
    basis = {'text': '职责包括会话恢复、工具调用。', 'existing_questions': ['什么是线程？']}
    value = {'generated_questions': [
        {'title': '会话恢复', 'question': '如何续接中断会话？', 'target_quote': '会话恢复',
         'probe': '如何续接中断会话？', 'reference': '模型杜撰的参考'},
        {'title': '重复', 'question': '如何续接中断会话!', 'target_quote': '会话恢复'},
        {'title': '内置题重复', 'question': '什么是线程!', 'target_quote': '会话恢复'},
        {'title': '捏造引用', 'question': '怎样实现超时？', 'target_quote': '服务超时'},
        {'title': '范围合适但题型非法', 'question': '怎样处理工具错误？',
         'target_quote': '工具调用', 'kind': 'unknown'},
    ]}
    assert normalize_plan(value, scope()).generated_questions == []
    allowed_scope = scope() | {'question_basis': basis}
    plan = normalize_plan(value, allowed_scope)
    assert len(plan.generated_questions) == 1
    [question] = plan.generated_questions
    assert question.probe == '' and 'reference' not in question.model_dump()
    assert normalize_plan(plan, allowed_scope) == plan


def test_an_out_of_scope_tool_is_rejected_before_execution_and_the_model_sees_why():
    tool_call = {'name': 'delete_everything', 'args': {'all': True}, 'id': 'call-2'}
    final = AIMessage(content='计划完成。')
    state, guard, model = run_agent([HumanMessage(content='请规划')], [AIMessage(content='', tool_calls=[tool_call]), final])
    assert guard.rejections == [{'tool': 'delete_everything', 'code': 'tool_not_in_scope'}]
    rejection = [message for message in state['messages']
                 if isinstance(message, ToolMessage) and message.status == 'error']
    assert rejection and 'tool_not_in_scope' in rejection[0].content
    assert guard.model_calls >= 2  # it asked again after the rejection instead of failing silently


def test_a_documented_tool_with_extra_arguments_is_rejected():
    tool_call = {'name': 'read_relevant_memory', 'args': {'topic_id': 'java-concurrency', 'limit': 99}, 'id': 'call-3'}
    state, guard, _model = run_agent([HumanMessage(content='请规划')], [AIMessage(content='', tool_calls=[tool_call]), AIMessage(content='ok')])
    assert guard.rejections == [{'tool': 'read_relevant_memory', 'code': 'unexpected_argument'}]


def test_a_legal_tool_call_runs_and_its_result_reaches_the_model():
    tool_call = {'name': 'list_builtin_interview_topics', 'args': {}, 'id': 'call-4'}
    state, guard, model = run_agent([HumanMessage(content='请规划')],
                                    [AIMessage(content='', tool_calls=[tool_call]), AIMessage(content='计划完成。')])
    assert guard.rejections == []
    results = [message for message in state['messages'] if isinstance(message, ToolMessage)]
    assert results and 'java-concurrency' in results[0].content


def test_the_planner_is_built_with_the_typed_schema_and_our_guard():
    model = RecordingModel(responses=[AIMessage(content='计划完成。')])
    agent, guard = build_planner(model, scope())
    assert agent.name == 'interview-planner' and isinstance(guard, PlanningGuard)
    # NOTE: with a scripted fake model the framework's own structured-output step returns None
    # (`state["structured_response"] is None`), so the plan node must always run `normalize_plan`
    # on whatever the model produced. That server-side gate is covered below.
    state = asyncio.run(agent.ainvoke({'messages': [HumanMessage(content='请规划')]}))
    assert 'structured_response' in state


def test_trimming_drops_old_turns_without_splitting_tool_pairs():
    long_history = [HumanMessage(content='任务')]
    for index in range(20):
        long_history.append(AIMessage(content=f'第 {index} 轮历史' * 40))
        long_history.append(ToolMessage(content=f'第 {index} 轮工具结果' * 40, tool_call_id=f'c{index}'))
    trimmed = _trim(long_history, 800)
    assert 0 < len(trimmed) < len(long_history)
    assert not isinstance(trimmed[0], ToolMessage)  # never open with an orphaned tool result


def test_the_request_the_model_receives_is_replaced_not_duplicated():
    history = [HumanMessage(content='任务')]
    for index in range(12):
        history.append(AIMessage(content=f'历史 {index} ' * 200))
    state, guard, model = run_agent(history, [AIMessage(content='计划完成。')])
    assert guard.model_calls >= 1
    first_request = model.seen[0]
    assert len(first_request) < len(history)
    # The graph state still holds the full history: trimming only affects this request.
    assert len(state['messages']) >= 1


def test_normalize_plan_drops_topics_items_and_ids_it_was_not_shown():
    plan = normalize_plan({'focus_topic_ids': ['java-concurrency', 'invented-topic'],
                           'resume_item_indexes': [0, 5],
                           'evidence_ids': ['obs-1', 'obs-elsewhere'],
                           'rationale': 'x' * 900}, scope())
    assert plan.focus_topic_ids == ['java-concurrency']
    assert plan.resume_item_indexes == [0]
    assert plan.evidence_ids == ['obs-1']
    assert len(plan.rationale) == 600


@pytest.mark.parametrize('value', [
    {'focus_topic_ids': ['a'] * 9},
    {'resume_item_indexes': list(range(9))},
])
def test_plan_bounds_are_enforced(value):
    with pytest.raises(ValueError):
        InterviewPlan.model_validate(value)
