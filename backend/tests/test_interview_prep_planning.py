"""S03b: the live planner is bounded by the server gate and by the ledger (no unpaid request)."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from interview_agent.interview_prep import checkpointer
from interview_agent.interview_prep.billing import PreparationLedger
from interview_agent.billing.ledger import authorize
from interview_agent.interview_prep.planner import LivePlanner, PlanningFailed, _plan_object
from interview_agent.interview_prep.planning import InterviewPlan
from interview_agent.interview_prep.repository import PreparationRepository
from interview_agent.interview_prep.worker import worker_for
from interview_agent.interviews.billing import InterviewPolicy
from interview_agent.worker.queue import OutboxDispatcher
from test_interview_planning import scope
from test_interview_prep_repository import base, checkpoints, confirm, context, create, view
from test_postgres import arun, pg_settings, query
from test_worker import resources, worker_settings  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


class FakePlanner:
    """Stands in for the LangChain agent: returns a plan, or raises a stable failure."""

    def __init__(self, result=None, error=None):
        # NB: the attribute must not be called plan, it would shadow the method below.
        self.result, self.error, self.closed, self.calls = result, error, False, 0

    async def plan(self, frozen):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self):
        self.closed = True


def plan_of(**overrides):
    value = {'focus_topic_ids': ['java-backend-base'], 'resume_item_indexes': [],
             'evidence_ids': [], 'rationale': '先补基础。'}
    return InterviewPlan.model_validate(value | overrides)


async def once(settings, planner_factory=None):
    async with resources(settings) as (old, queue):
        repo = PreparationRepository(old.pool)
        await OutboxDispatcher(repo, queue).dispatch()
        worker = worker_for(repo, queue, pool=old.pool,
                            saver_factory=lambda: checkpointer.saver(settings.database),
                            planner_factory=planner_factory)
        return await worker.once()


def test_a_planner_plan_is_merged_and_sanitised_by_the_server(context, worker_settings):
    client, goal, _uid = context
    planner = FakePlanner(result=plan_of(focus_topic_ids=['java-concurrency', 'invented-topic'],
                                       evidence_ids=['made-up-id']))
    prep = create(client, goal).json()
    assert arun(once(worker_settings, lambda claim: planner))
    planned = view(client, goal, prep['id'])
    assert planned['status'] == 'WAITING_FOR_USER', {k: planned[k] for k in ('status', 'error_code')}
    assert planned['plan']['planner'] == 'langchain-agent'
    assert planned['plan']['focus_topic_ids'] == ['java-concurrency']  # the invented one is dropped
    assert planned['plan']['evidence_ids'] == []
    assert planned['plan']['rationale'] == '先补基础。'
    assert planner.calls == 1 and planner.closed is True


def test_a_planner_failure_fails_the_preparation_with_a_stable_code(context, worker_settings):
    client, goal, _uid = context
    planner = FakePlanner(error=PlanningFailed('planning_no_plan', {'model_calls': 2}))
    prep = create(client, goal).json()
    assert arun(once(worker_settings, lambda claim: planner))
    failed = view(client, goal, prep['id'])
    assert failed['status'] == 'FAILED' and failed['error_code'] == 'planning_no_plan'
    assert planner.closed is True


def test_the_default_path_is_still_deterministic(context, worker_settings):
    client, goal, _uid = context
    prep = create(client, goal).json()
    assert arun(once(worker_settings))
    planned = view(client, goal, prep['id'])
    assert planned['status'] == 'WAITING_FOR_USER'
    assert planned['plan']['version'] == 'prep-plan-v1'
    assert 'planner' not in planned['plan'] or planned['plan'].get('planner') != 'langchain-agent'


def test_a_ledger_refusal_sends_no_request_at_all(context, worker_settings, pg_settings, tmp_path):
    client, goal, _uid = context
    prep = create(client, goal).json()
    policy = InterviewPolicy.load(__import__('pathlib').Path('configs/ja05-deepseek-flash.json'))
    key = tmp_path / 'key.env'
    key.write_text('DEEPSEEK_API_KEY=offline-not-real\n', encoding='utf-8')
    attempts = []

    def handler(request):  # would be a paid call if the ledger let it through
        attempts.append(request)
        return httpx.Response(200, json={})

    async def scenario():
        async with resources(worker_settings) as (old, queue):
            repo = PreparationRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            from interview_agent.worker.queue import parse_message
            message = await queue.receive('ledger-probe-' + uuid4().hex)
            claim = await repo.claim(parse_message(message[1]))
            assert claim is not None
            planner = LivePlanner(old.pool, repo, claim, policy, f'not-authorized-{uuid4().hex}', key,
                                  inner_factory=lambda: httpx.MockTransport(handler))
            from interview_agent.billing.ledger import BillingUnavailable
            with pytest.raises(BillingUnavailable, match='model_authorization_unavailable'):
                await planner.plan({})
            return claim

    arun(scenario())
    assert attempts == []  # nothing left the process


def test_a_string_credential_path_is_accepted_and_a_missing_one_is_a_billing_refusal(tmp_path):
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.interview_prep.planner import LivePlanner

    key = tmp_path / "key.env"
    key.write_text("DEEPSEEK_API_KEY=offline-not-real\n", encoding="utf-8")
    planner = LivePlanner(pool=None, repository=None, claim=None, policy=None,
                          authorization="unit", credentials=str(key))  # argparse hands over a str
    assert planner._key() == "offline-not-real"
    missing = LivePlanner(pool=None, repository=None, claim=None, policy=None,
                          authorization="unit", credentials=str(tmp_path / "nope.env"))
    with pytest.raises(BillingUnavailable, match="model_credentials_unavailable"):
        missing._key()


def test_the_live_planner_sends_no_response_format_field(project_root):
    """DeepSeek rejects `response_format` with HTTP 400 (seen live on 2026-11-16), so the live
    planner must ask for JSON in the prompt and parse it. This pins that payload shape."""
    import httpx
    from interview_agent.interview_prep.planning import build_planner
    from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
    from interview_agent.interviews.billing import InterviewPolicy

    bodies = []

    class Ledger:
        policy = InterviewPolicy.load(project_root / "configs/ja05-deepseek-flash.json")

        async def reserve(self, *args, **kwargs):
            return "call-1"

        async def settle(self, *args, **kwargs):
            return True

    async def scenario():
        frozen = scope()
        provider = DeepSeekProvider(
            "offline-not-real",
            BudgetedTransport(httpx.MockTransport(
                lambda request: bodies.append(json.loads(request.content)) or httpx.Response(200, json={})),
                Ledger(), None, frozen))
        try:
            agent, guard = build_planner(provider.model, frozen, structured=False)
            try:
                await agent.ainvoke({"messages": [{"role": "user", "content": "plan"}]})
            except Exception:
                pass  # the fake body is not a chat completion; we only care about the request
            return guard
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert bodies, "no request captured"
    assert "response_format" not in bodies[0] and bodies[0].get("tools")


COMPLETION_USAGE = {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
                    "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 40}


def completion(policy, text):
    return {"id": "offline-plan", "object": "chat.completion", "created": 1, "model": policy.model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": COMPLETION_USAGE}


def run_mock_plan(settings, owner, client, goal, tmp_path, text, *, requests=None,
                  reference_lookup=None, lookup_topic=None, experience_lookup=None, **creation):
    """The real worker path with a mock provider: no network, no money, real SQL."""
    from pathlib import Path

    policy = InterviewPolicy.load(Path("configs/ja05-deepseek-flash.json"))
    key = tmp_path / "key.env"
    key.write_text("DEEPSEEK_API_KEY=offline-not-real\n", encoding="utf-8")
    identity = f"test-prep-{uuid4().hex}"
    arun(authorize(owner, policy, identity, 200_000_000))
    response = create(client, goal, **creation)
    assert response.status_code == 202, response.text
    prep = response.json()

    calls = [('read_preparation_scope', {})]
    if lookup_topic is not None:
        calls.append(('lookup_official_reference', {'topic_id': lookup_topic}))

    def handler(request):
        if requests is not None:
            requests.append(json.loads(request.content))
            if len(requests) <= len(calls):
                name, args = calls[len(requests) - 1]
                tool = completion(policy, "")
                tool["choices"][0].update(finish_reason="tool_calls", message={
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": f"planner-call-{len(requests)}", "type": "function", "function": {
                            "name": name, "arguments": json.dumps(args)}}]})
                return httpx.Response(200, json=tool)
        return httpx.Response(200, json=completion(policy, text(requests) if callable(text) else text))

    async def scenario():
        async with resources(settings) as (old, queue):
            repo = PreparationRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            worker = worker_for(repo, queue, pool=old.pool,
                                saver_factory=lambda: checkpointer.saver(settings.database),
                                planner_factory=lambda claim: LivePlanner(
                                    old.pool, repo, claim, policy, identity, key,
                                    inner_factory=lambda: httpx.MockTransport(handler),
                                    reference_lookup=reference_lookup, experience_lookup=experience_lookup))
            return await worker.once()

    try:
        assert arun(scenario())
        return view(client, goal, prep["id"])
    finally:
        query(owner, "DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id", (identity,))
        query(owner, "DELETE FROM app.model_authorizations WHERE id=%s RETURNING id", (identity,))


def test_empty_account_jd_questions_reach_the_frozen_session_and_report(
        context, worker_settings, pg_settings, tmp_path):
    from test_interview_prep_repository import interviews, post
    from test_interview_repository import answer, process, question, view as session_view

    client, _, _uid = context
    jd = "负责多轮 Agent 的会话恢复；工具执行；离线评测；多模态图像保真度。"
    goal_response = post(client, "/api/career-goals/", {
        "message": "按 JD 练习完整面试", "jd_text": jd, "conditions": {"direction": "Agent 开发"}})
    assert goal_response.status_code == 202, goal_response.text
    goal = goal_response.json()
    prompts = {
        "会话恢复": "服务重启后，如何让同一个会话继续等待人工输入？",
        "工具执行": "调用外部写入工具成功后进程崩溃，重放时如何避免重复写入？",
        "离线评测": "离线评测如何区分工具选错与工具执行失败？",
    }
    body = {
        "rationale": "准备题目：" + next(iter(prompts.values())),
        "generated_questions": [
            {"title": quote, "target_quote": quote, "question": question_text,
             "reference": "未核对的模型说法", "url": "https://example.com/claimed-reference"}
            for quote, question_text in prompts.items()],
    }
    requests = []
    owner = pg_settings[1]
    prep = run_mock_plan(worker_settings, owner, client, goal, tmp_path,
                         json.dumps(body, ensure_ascii=False), requests=requests,
                         role="ai-application", profile="full", use_resume=False)
    assert prep["status"] == "WAITING_FOR_USER", prep["error_code"]
    assert len(requests) == 2
    delivered = [json.loads(m["content"]) for m in requests[1]["messages"] if m["role"] == "tool"]
    assert delivered[0]["jd_or_direction"] == jd and delivered[0]["existing_questions"]
    assert len(delivered[0]["jd_requirements"]) == 4
    assert prep["plan"]["generated_question_count"] == len(prompts)
    assert "generated_questions" not in prep["plan"]
    assert all(text not in json.dumps(prep, ensure_ascii=False) for text in prompts.values())
    assert interviews(owner, goal["id"]) == []

    key = uuid4().hex
    approval = confirm(client, goal, prep, key=key)
    replay = confirm(client, goal, prep, key=key)
    assert approval.status_code == replay.status_code == 200
    assert approval.json()["status"] == "PENDING"
    assert replay.json()["version"] == approval.json()["version"]
    assert arun(once(worker_settings))
    opened = view(client, goal, prep["id"])
    assert opened["status"] == "SUCCEEDED", opened.get("error_code")
    assert len(interviews(owner, goal["id"])) == 1
    session = {"id": opened["session_id"]}
    arun(process(worker_settings))
    session = session_view(client, goal, session)
    first = question(session)["payload"]
    assert first["source"] == "jd_generated" and first["has_reference"] is False
    assert first["text"] in prompts.values() and first["topic_id"].startswith("g-")
    assert first["generated"]["target_quote"] in jd
    assert first["generated"]["target_kind"] == "user_jd"
    assert session_view(client, goal, session)["events"] == session["events"]

    for _ in range(12):
        if session["status"] == "COMPLETED":
            break
        submitted = answer(client, goal, session, "不清楚，我会先验证这一步的状态和恢复边界。")
        assert submitted.status_code == 202, submitted.text
        arun(process(worker_settings))
        session = session_view(client, goal, session)
        assert session["status"] in ("WAITING_FOR_USER", "COMPLETED"), session.get("error_code")
    assert session["status"] == "COMPLETED"
    generated = [event for event in session["events"] if event["kind"] == "question"
                 and event["payload"].get("source") == "jd_generated"]
    assert session["report"]["generated_questions"] == len(generated) > 0
    assert session["report"]["questions"] >= 10
    coverage = {item["quote"]: item for item in session["report"]["jd_coverage"]}
    assert all(coverage[quote]["asked_count"] > 0 for quote in
               ("负责多轮 Agent 的会话恢复", "工具执行", "离线评测"))
    assert coverage["多模态图像保真度"]["planned_sources"] == []
    assert coverage["多模态图像保真度"]["asked_count"] == 0
    assert session["observations"][0]["label"] == "inconclusive"
    assert "claimed-reference" not in json.dumps(session)


@pytest.mark.parametrize("usable_plan", [True, False], ids=["private-plus-generated", "generation-fallback"])
def test_full_preparation_preserves_explicit_private_priority(context, worker_settings, usable_plan):
    from test_interview_repository import question, process, view as session_view
    from test_interview_wiki import goal_for
    from test_question_banks import document, imported, selected

    client, _, _uid = context
    goal = goal_for(client, "并发排查；量化向量索引召回损失。")
    private_question = "做并发排查时，怎样把异常执行顺序固化成回归用例？"
    data = document(private_question)
    data["items"][0]["roles"] = ["ai-application"]
    bank = imported(client, data)
    creation = create(client, goal, role="ai-application", profile="full", use_resume=False,
                      question_banks=selected(bank))
    assert creation.status_code == 202, creation.text
    fake = FakePlanner(
        result=plan_of(generated_questions=[{
            "title": "向量召回损失", "target_quote": "量化向量索引召回损失",
            "question": "怎样用精确近邻结果作为基线判断向量索引是否漏召回？",
        }]) if usable_plan else None,
        error=None if usable_plan else PlanningFailed("planning_no_plan", {"model_calls": 2}),
    )
    assert arun(once(worker_settings, lambda claim: fake))
    prepared = view(client, goal, creation.json()["id"])
    assert prepared["status"] == "WAITING_FOR_USER", prepared.get("error_code")
    plan = prepared["plan"]
    assert plan["selected_bank_count"] == 1
    assert plan["generated_question_count"] == int(usable_plan)
    assert plan.get("generation_fallback") == (None if usable_plan else "planning_no_plan")
    assert plan["jd_coverage"][0]["planned_sources"] == ["private_bank"]
    assert plan["jd_coverage"][1]["planned_sources"] == (["jd_generated"] if usable_plan else [])
    assert private_question not in json.dumps(prepared, ensure_ascii=False)
    assert confirm(client, goal, prepared).status_code == 200
    assert arun(once(worker_settings))
    opened = view(client, goal, prepared["id"])
    assert opened["status"] == "SUCCEEDED", opened.get("error_code")
    arun(process(worker_settings))
    session = session_view(client, goal, {"id": opened["session_id"]})
    assert session["status"] == "WAITING_FOR_USER", session.get("run")
    assert question(session)["payload"]["text"] == private_question
    assert session["question_banks"][0]["bank_id"] == bank["bank_id"]


def test_full_preparation_tracks_user_direction_without_inventing_a_jd(context, worker_settings):
    from test_interview_repository import question, process, view as session_view
    from test_interview_wiki import goal_for

    client, _, _uid = context
    goal = goal_for(client, "")
    creation = create(client, goal, role="ai-application", profile="full", use_resume=False)
    assert creation.status_code == 202, creation.text
    fake = FakePlanner(result=plan_of(generated_questions=[{
        "title": "AI 应用开发", "target_quote": "AI 应用开发",
        "question": "一个 AI 助手持续回答无关内容，你会怎样设计诊断步骤？",
    }]))
    assert arun(once(worker_settings, lambda claim: fake))
    prepared = view(client, goal, creation.json()["id"])
    assert prepared["plan"]["target"]["kind"] == "direction"
    assert [item["quote"] for item in prepared["plan"]["jd_coverage"]] == ["AI 应用开发"]
    assert confirm(client, goal, prepared).status_code == 200
    assert arun(once(worker_settings))
    opened = view(client, goal, prepared["id"])
    assert opened["status"] == "SUCCEEDED", opened.get("error_code")
    arun(process(worker_settings))
    first = question(session_view(client, goal, {"id": opened["session_id"]}))["payload"]
    assert first["generated"]["target_kind"] == "direction"


@pytest.mark.parametrize('has_passage,automatic,case', [
    (True, False, 'interrupts'), (True, True, 'interrupts'), (False, True, 'interrupts'),
    (True, True, 'stores'), (True, True, 'structured-output'),
])
def test_preparation_public_reference_is_attached_only_after_exact_read(
        context, worker_settings, pg_settings, tmp_path, has_passage, automatic, case):
    from interview_agent.connectors.public_reference import CATALOG, PreparationReference
    from test_interview_prep_repository import post
    from test_interview_repository import process, question, view as session_view
    from test_preparation_reference import PASSAGE, TOPIC

    client, _, _uid = context
    cases = {
        'interrupts': (TOPIC, 'LangGraph' if automatic else 'Agent', '中断恢复', PASSAGE),
        'stores': ('langgraph-stores', 'LangGraph', '跨线程记忆',
                   'Compile the graph with both the checkpointer and the store to share preferences.'),
        'structured-output': ('langchain-structured-output', 'LangChain', '结构化输出',
                              "For models that don't support native structured output, use a tool-backed schema."),
    }
    topic, framework, anchor, passage = cases[case]
    jd = f'负责 {framework} {anchor}与 SECRET-JD-PLACEHOLDER。'
    goal = post(client, '/api/career-goals/', {'message': '做 JD 训练', 'jd_text': jd}).json()
    doc_requests = []

    def fetch(request):
        doc_requests.append(request)
        return httpx.Response(200, text=f'# Documentation\n\n{passage}' if has_passage else '# No usable passage')

    lookup = PreparationReference(lambda: True, transport=httpx.MockTransport(fetch))
    questions = [
        {'title': '设计取舍', 'target_quote': anchor, 'question': f'面对{anchor}的要求，你会怎样验证自己的设计？',
         'source_id': topic, 'source_quote': passage, 'url': 'https://fabricated.invalid/'},
        {'title': '识别来源', 'target_quote': anchor, 'question': f'哪些信号可以说明{anchor}的设计没有生效？',
         'source_id': topic, 'source_quote': 'Fabricated evidence from the model output.'},
    ]
    model_requests = []
    prep = run_mock_plan(worker_settings, pg_settings[1], client, goal, tmp_path,
                         json.dumps({'generated_questions': questions}), requests=model_requests,
                         reference_lookup=lookup, lookup_topic=None if automatic else topic,
                         role='ai-application', profile='full', use_resume=False)
    assert prep['status'] == 'WAITING_FOR_USER', prep['error_code']
    assert len(model_requests) == (2 if automatic else 3)
    if automatic:
        assert all(t['function']['name'] != 'lookup_official_reference' for t in model_requests[0]['tools'])
        delivered = [json.loads(m['content']) for m in model_requests[1]['messages'] if m['role'] == 'tool']
        assert delivered[0]['official_reference']['status'] == ('retrieved' if has_passage else 'no_passage')
    else:
        assert not model_requests[-1].get('tools')
    assert [(str(req.url), req.content) for req in doc_requests] == [(CATALOG[topic].url, b'')]
    assert prep['plan']['public_reference_status'] == ('retrieved' if has_passage else 'no_passage')
    assert prep['plan']['public_reference_count'] == int(has_passage)
    assert passage not in str(prep)
    assert confirm(client, goal, prep).status_code == 200
    assert arun(once(worker_settings))
    session_id = view(client, goal, prep['id'])['session_id']
    arun(process(worker_settings))
    session = session_view(client, goal, {'id': session_id})
    assert question(session)['payload']['source'] == 'jd_generated'
    assert question(session)['payload']['has_reference'] is False
    assert passage not in str(session) and session['report'] is None
    ended = post(client, base(goal) + f'/interviews/{session_id}/stop',
                 {'expected_version': session['version']}).json()
    if has_passage:
        [selected] = ended['report']['public_references']
        assert selected['question_title'] == questions[0]['title'] and selected['quote'] == passage
        assert selected['url'] == CATALOG[topic].url
    else:
        assert 'public_references' not in ended['report']
    assert 'Fabricated evidence' not in str(ended) and 'fabricated.invalid' not in str(ended)


def test_a_fenced_code_block_is_read_as_the_plan(context, worker_settings, pg_settings, tmp_path):
    """2026-11-16: the live model sometimes wraps its JSON in fences. That is still a plan."""
    client, goal, _uid = context
    body = {"focus_topic_ids": ["java-concurrency"], "resume_item_indexes": [],
            "evidence_ids": [], "rationale": "先补并发。"}
    text = "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
    row = run_mock_plan(worker_settings, pg_settings[1], client, goal, tmp_path, text)
    assert row["status"] == "WAITING_FOR_USER", row["error_code"]
    assert row["plan"]["focus_topic_ids"] == ["java-concurrency"]


def test_a_wrong_plan_shape_is_a_stable_planning_outcome(context, worker_settings, pg_settings, tmp_path):
    """A malformed plan must not look like a crash (`planning_crashed`)."""
    client, goal, _uid = context
    text = json.dumps({"focus_topic_ids": "java-concurrency", "rationale": "x"}, ensure_ascii=False)
    row = run_mock_plan(worker_settings, pg_settings[1], client, goal, tmp_path, text)
    assert row["status"] == "FAILED"
    assert row["error_code"] == "planning_invalid_plan", row["error_code"]
    detail = query(pg_settings[1],
                   "SELECT error_detail FROM app.career_interview_preparations WHERE id=%s",
                   (row["id"],))[0]["error_detail"]
    assert "focus_topic_ids" in json.dumps(detail)


def test_plan_object_ignores_prose_but_rejects_garbage():
    assert _plan_object('好的：{"focus_topic_ids": []}') == {"focus_topic_ids": []}
    assert _plan_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _plan_object("no json here") is None
    assert _plan_object("[1, 2]") is None


def test_the_last_permitted_request_can_only_answer(project_root):
    """S03c3: the ledger refuses a fourth call for one preparation, so the last request the
    budget allows must be spent answering, not on one more read. Tools are removed there."""
    import httpx
    from interview_agent.interview_prep.billing import MAX_PREPARATION_REQUESTS
    from interview_agent.interview_prep.planning import PlanningGuard, build_planner
    from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider

    policy = InterviewPolicy.load(project_root / "configs/ja05-deepseek-flash.json")
    bodies = []

    class Ledger:
        pass

    ledger = Ledger()
    ledger.policy = policy

    async def reserve(*args, **kwargs):
        return f"call-{len(bodies)}"

    async def settle(*args, **kwargs):
        return True

    ledger.reserve, ledger.settle = reserve, settle
    call = {"id": "call-1", "type": "function",
            "function": {"name": "read_preparation_scope", "arguments": "{}"}}

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) < MAX_PREPARATION_REQUESTS:
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps({"focus_topic_ids": []})}
            finish = "stop"
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                                         "model": policy.model, "choices": [{"index": 0,
                                         "finish_reason": finish, "message": message}],
                                         "usage": COMPLETION_USAGE})

    async def scenario():
        provider = DeepSeekProvider(
            "offline-not-real",
            BudgetedTransport(httpx.MockTransport(handler), ledger, None, scope()))
        try:
            guard = PlanningGuard(max_model_calls=MAX_PREPARATION_REQUESTS)
            agent, guard = build_planner(provider.model, scope(), structured=False,
                                         middleware=guard)
            await agent.ainvoke({"messages": [{"role": "user", "content": "plan"}]})
            return guard
        finally:
            await provider.aclose()

    guard = arun(scenario())
    assert guard.model_calls == MAX_PREPARATION_REQUESTS == len(bodies)
    assert all(body.get("tools") for body in bodies[:-1])
    assert "tools" not in bodies[-1]  # the answer round cannot call a tool
    assert guard.final_round is True


async def _settle_history(pool, identity, count):
    """Write `count` already-settled planning calls, as earlier preparations would have."""
    async with pool.connection() as conn, conn.transaction():
        for _ in range(count):
            await conn.execute(
                "INSERT INTO app.model_http_calls(id,authorization_id,operation_id,job_id,attempt,"
                "fencing_token,input_hash,payload_hash,payload_bytes,reserved_nano,state,accounted_nano,"
                "raw_usage,http_status,purpose) "
                "VALUES(%s,%s,%s,%s,1,1,%s,%s,10,147456000,'KNOWN',1,'{}'::jsonb,200,"
                "'career_interview_prep')",
                (str(uuid4()), identity, str(uuid4()), str(uuid4()), '0' * 64, '0' * 64))


def test_planning_has_its_own_fuse_instead_of_the_interview_twelve(context, worker_settings, pg_settings):
    """2026-11-16 live: 12 requests were spent while 0.187 CNY was unspent, so a fifth preparation
    was refused. Planning must keep going until the money gate says stop."""
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.interview_prep.billing import (MAX_PREPARATION_HTTP_ATTEMPTS,
                                                        PreparationLedger)
    from interview_agent.worker.queue import parse_message

    client, goal, _uid = context
    policy = InterviewPolicy.load(__import__('pathlib').Path('configs/ja05-deepseek-flash.json'))
    identity = f'test-fuse-{uuid4().hex}'
    arun(authorize(pg_settings[1], policy, identity, 200_000_000))
    prep = create(client, goal).json()
    results = {}

    async def scenario():
        async with resources(worker_settings) as (old, queue):
            repo = PreparationRepository(old.pool)
            await OutboxDispatcher(repo, queue).dispatch()
            message = await queue.receive('fuse-' + uuid4().hex)
            claim = await repo.claim(parse_message(message[1]))
            frozen = await repo.frozen_inputs(await repo.context(claim))
            await repo.freeze(claim, frozen)  # reserve compares against the frozen snapshot
            ledger = PreparationLedger(old.pool, policy, identity, repo)
            await _settle_history(old.pool, identity, 12)  # one full interview session's fuse
            results['first'] = await ledger.reserve(claim, frozen, 'a' * 64, 100)
            # Settle it as the transport does, so the next attempt is not blocked by an
            # outstanding reservation for the same operation.
            await ledger.settle(results['first'], http_status=200,
                                usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                                       'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 10},
                                response_model=policy.model, response_meta={})
            await _settle_history(old.pool, identity, MAX_PREPARATION_HTTP_ATTEMPTS - 13)
            try:
                await ledger.reserve(claim, frozen, 'b' * 64, 100)
            except BillingUnavailable as error:
                results['refused'] = (error.code, error.limit)

    try:
        arun(scenario())
        assert isinstance(results['first'], str) and len(results['first']) == 36
        assert results['refused'] == ('model_budget_exhausted', 'authorization_http_limit')
    finally:
        query(pg_settings[1], 'DELETE FROM app.model_http_calls WHERE authorization_id=%s RETURNING id',
              (identity,))
        query(pg_settings[1], 'DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',
              (identity,))
