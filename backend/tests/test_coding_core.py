import asyncio
from copy import deepcopy
import json
import pytest
from interview_agent.career.repository import digest
from interview_agent.coding.catalog import task, public_task, TASKS
from interview_agent.coding.workspace import Workspace, file_hash
from interview_agent.coding.agent import assist, final_grade, ScriptedCodingDemo, CodingFailure, validate_result
from interview_agent.execution.java_workspace import validate_sources
from interview_agent.runtime.types import AssistantResponse, ToolCall


class FakeExecutor:
    """Protocol double; real compilation is separately exercised by the demo."""
    def __init__(self, solutions): self.solutions = solutions; self.calls = []
    async def grade(self, files, cases, lost=None):
        self.calls.append(deepcopy(cases))
        accepted = any(solution in files.values() for solution in self.solutions.values())
        verdict = 'ACCEPTED' if accepted else 'WRONG_ANSWER'
        return {'verdict': verdict, 'cases': [{'id': c['id'], 'visibility': c['visibility'], 'verdict': verdict} for c in cases],
                'evidence': {'workspace_sha256': digest(files)}}


@pytest.fixture
def solutions(project_root):
    return json.loads((project_root / 'fixtures/ja07/scripted-solutions.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('identity', list(TASKS))
def test_frozen_catalog_public_projection_and_isolated_copies(identity):
    t = task(identity); view = public_task(t)
    assert len(view['samples']) == 1 and 'cases' not in view and 'files' not in view
    assert len(t['cases']) == 4 and len({c['id'] for c in t['cases']}) == 4
    t['files']['Main.java'] = 'tampered'
    assert task(identity)['files']['Main.java'] != 'tampered'


@pytest.mark.parametrize('path', ['../Main.java', '/tmp/Main.java', 'C:\\secret', 'Main.java', 'README.md', 'tests.java'])
def test_readonly_and_unregistered_edits_do_not_change_workspace(path):
    w = Workspace(task('ttl-boundary')); original = w.snapshot()
    with pytest.raises(ValueError): w.replace(path, 'x', 'evil', actor='assistant')
    assert w.snapshot() == original


def test_hash_compare_and_swap_and_actor_attribution(solutions):
    w = Workspace(task('ttl-boundary')); p = 'ExpiringCache.java'; before = w.read(p)
    with pytest.raises(ValueError, match='file_version_conflict'): w.replace(p, 'stale', 'x', actor='user')
    w.replace(p, before['sha256'], solutions['ttl-boundary'], actor='user')
    assert w.edits[0]['actor'] == 'user' and '--- a/ExpiringCache.java' in w.edits[0]['diff']
    assert w.edits[0]['before_sha256'] == before['sha256'] and w.sha256 != digest(w.initial)
    snapshot = w.snapshot(); snapshot['files'][p] = 'external mutation'
    assert w.files[p] == solutions['ttl-boundary']
    assert not w.replace(p, file_hash(w.files[p]), w.files[p], actor='assistant')['changed']
    assert len(w.edits) == 1


@pytest.mark.parametrize('text', ['', ' ', '\x00', '中' * 3000])
def test_invalid_content_does_not_mutate_snapshot(text):
    w = Workspace(task('ttl-boundary')); p = 'ExpiringCache.java'
    with pytest.raises(ValueError): w.replace(p, file_hash(w.files[p]), text, actor='assistant')
    assert w.revision == 1


def test_fixed_source_protocol_rejects_arbitrary_paths_and_limits():
    for files in ({'../Main.java': 'x'}, {'Main.java': 'x', 'Other.java': 'y'}, {'Main.java': 'x' * 8193}):
        with pytest.raises(ValueError): validate_sources(files)


@pytest.mark.parametrize('identity', list(TASKS))
def test_scripted_tool_loop_observes_failure_edits_retests_then_outer_grades(identity, solutions):
    async def run():
        t = task(identity); ex = FakeExecutor(solutions); provider = ScriptedCodingDemo(t, solutions[identity]); stop = asyncio.Event()
        c = await assist(t, provider, ex, stop)
        assert [r['result']['verdict'] for r in c['tests']] == ['WRONG_ANSWER', 'ACCEPTED']
        assert c['model_responses'] == 7 and len(c['snapshot']['edits']) == 1 and c['assisted']
        assert all(case['visibility'] == 'public' for call in ex.calls for case in call)
        report = await final_grade(t, c, ex, stop)
        assert len(ex.calls[-1]) == 4 and report['result']['verdict'] == 'ACCEPTED'
        assert not report['ability_certified']
    asyncio.run(run())


class Actions:
    def __init__(self, actions): self.actions = actions; self.n = 0; self.observations = []
    async def complete(self, messages, tools):
        if messages[-1].role == 'tool': self.observations.append(json.loads(messages[-1].text))
        name, args = self.actions[min(self.n, len(self.actions) - 1)]; self.n += 1
        return AssistantResponse(tool_calls=[ToolCall(id='call-' + str(self.n), name=name, arguments=args)])


def test_cannot_claim_test_success_without_execution():
    async def run():
        t = task('ttl-boundary'); w = Workspace(t)
        provider = Actions([('read_task', {}), ('finish_coding', {'workspace_sha256': w.sha256, 'status': 'verified', 'explanation': 'I passed all tests'})])
        with pytest.raises(CodingFailure, match='coding_model_limit'):
            await assist(t, provider, FakeExecutor({}), asyncio.Event())
        assert any(o.get('error') == 'current_passing_tests_required' for o in provider.observations)
    asyncio.run(run())


def test_forbidden_tool_and_edit_before_read_are_rejected():
    async def run():
        t = task('ttl-boundary'); p = 'ExpiringCache.java'
        provider = Actions([('read_task', {}), ('shell', {'command': 'cat hidden'}),
                            ('replace_file', {'path': p, 'expected_sha256': file_hash(t['files'][p]), 'text': 'x'})])
        with pytest.raises(CodingFailure): await assist(t, provider, FakeExecutor({}), asyncio.Event())
        assert any(o.get('error') == 'coding_tool_not_allowed' for o in provider.observations)
        assert any(o.get('error') == 'read_current_file_before_edit' for o in provider.observations)
        assert not any('zero-ttl' in str(o) for o in provider.observations)
    asyncio.run(run())


def test_public_execution_budget_admitted_before_executor_call():
    async def run():
        ex = FakeExecutor({}); provider = Actions([('read_task', {}), ('run_public_tests', {})])
        with pytest.raises(CodingFailure): await assist(task('ttl-boundary'), provider, ex, asyncio.Event())
        assert len(ex.calls) == 3
        assert any(o.get('error') == 'coding_public_test_limit' for o in provider.observations)
    asyncio.run(run())


def test_edit_after_passing_tests_invalidates_verified_claim(solutions):
    async def run():
        t = task('ttl-boundary'); w = Workspace(t); p = t['editable'][0]
        w.replace(p, file_hash(w.files[p]), solutions[t['id']], actor='user')
        broken = solutions[t['id']].replace('<= now', '< now')
        class StaleClaim(Actions):
            async def complete(self, messages, specs):
                if self.n == 4:
                    self.actions[4] = ('finish_coding', {'workspace_sha256': w.sha256, 'status': 'verified', 'explanation': 'Earlier tests passed'})
                return await super().complete(messages, specs)
        provider = StaleClaim([('read_task', {}), ('run_public_tests', {}), ('read_file', {'path': p}),
            ('replace_file', {'path': p, 'expected_sha256': file_hash(w.files[p]), 'text': broken}), ('finish_coding', {})])
        with pytest.raises(CodingFailure): await assist(t, provider, FakeExecutor(solutions), asyncio.Event(), workspace=w)
        assert any(o.get('error') == 'current_passing_tests_required' for o in provider.observations)
        assert w.edits[-1]['actor'] == 'assistant'
    asyncio.run(run())


def test_passing_verdict_requires_all_frozen_cases():
    t = task('ttl-boundary'); files = {k: v for k, v in t['files'].items() if k.endswith('.java')}
    result = {'verdict': 'ACCEPTED', 'cases': [], 'evidence': {'workspace_sha256': digest(files)}}
    with pytest.raises(ValueError, match='incomplete_coding_acceptance'): validate_result(files, t['cases'], result)


def test_final_controller_rejects_tampered_driver_and_result_binding(solutions):
    async def run():
        t = task('ttl-boundary'); ex = FakeExecutor(solutions); cancel = asyncio.Event()
        c = await assist(t, ScriptedCodingDemo(t, solutions[t['id']]), ex, cancel)
        c['snapshot']['files']['Main.java'] = 'spoof'
        c['snapshot']['workspace_sha256'] = digest(c['snapshot']['files'])
        with pytest.raises(ValueError, match='coding_readonly_file_changed'): await final_grade(t, c, ex, cancel)
        with pytest.raises(ValueError, match='coding_result_workspace_mismatch'):
            validate_result(t['files'], t['cases'], {'verdict': 'ACCEPTED', 'cases': [], 'evidence': {}})
    asyncio.run(run())


def test_cancellation_joins_inflight_executor():
    async def run():
        entered = asyncio.Event(); exited = asyncio.Event(); stop = asyncio.Event()
        class Waiting:
            async def grade(self, files, cases, lost):
                entered.set()
                try: await asyncio.sleep(100)
                finally: exited.set()
        provider = Actions([('read_task', {}), ('run_public_tests', {})])
        work = asyncio.create_task(assist(task('ttl-boundary'), provider, Waiting(), stop))
        await asyncio.wait_for(entered.wait(), 2); stop.set()
        with pytest.raises(CodingFailure, match='coding_cancelled_or_timeout'): await work
        assert exited.is_set()
    asyncio.run(run())


def test_first_edit_requires_observed_baseline_then_can_recover(solutions):
    async def run():
        t = task('ttl-boundary'); w = Workspace(t); p = t['editable'][0]
        edit = ('replace_file', {'path': p, 'expected_sha256': file_hash(w.files[p]), 'text': solutions[t['id']]})
        class Recovery(Actions):
            async def complete(self, messages, specs):
                if self.n == 3:
                    assert w.files[p] == t['files'][p] and not w.edits
                if self.n == 6:
                    self.actions[6] = ('finish_coding', {'workspace_sha256': w.sha256, 'status': 'verified', 'explanation': '已观察原代码失败，再修改并重测。'})
                return await super().complete(messages, specs)
        model = Recovery([('read_task', {}), ('read_file', {'path': p}), edit,
                          ('run_public_tests', {}), edit, ('run_public_tests', {}), ('finish_coding', {})])
        ex = FakeExecutor(solutions)
        candidate = await assist(t, model, ex, asyncio.Event(), workspace=w)
        assert any(o.get('error') == 'baseline_public_tests_required' for o in model.observations)
        assert [v['result']['verdict'] for v in candidate['tests']] == ['WRONG_ANSWER', 'ACCEPTED']
        assert len(w.edits) == 1 and len(ex.calls) == 2 and candidate['status'] == 'verified'
    asyncio.run(run())


def test_multi_tool_batch_has_no_effect_and_all_calls_receive_error(solutions):
    async def run():
        t = task('ttl-boundary'); w = Workspace(t); p = t['editable'][0]
        ex = FakeExecutor(solutions)
        class Model:
            n = 0
            async def complete(self, messages, specs):
                self.n += 1
                if self.n == 1: name, args = 'read_task', {}
                elif self.n == 2:
                    return AssistantResponse(tool_calls=[
                        ToolCall(id='batch-edit', name='replace_file', arguments={'path': p, 'expected_sha256': file_hash(w.files[p]), 'text': 'bad'}),
                        ToolCall(id='batch-run', name='run_public_tests', arguments={})])
                elif self.n == 3:
                    assert not w.edits and not ex.calls
                    assert {m.tool_call_id for m in messages[-2:]} == {'batch-edit', 'batch-run'}
                    assert all(m.is_error and json.loads(m.text)['error'] == 'one_coding_tool_required' for m in messages[-2:])
                    name, args = 'read_file', {'path': p}
                elif self.n == 4: name, args = 'run_public_tests', {}
                elif self.n == 5: name, args = 'replace_file', {'path': p, 'expected_sha256': file_hash(w.files[p]), 'text': solutions[t['id']]}
                elif self.n == 6: name, args = 'run_public_tests', {}
                else: name, args = 'finish_coding', {'workspace_sha256': w.sha256, 'status': 'verified', 'explanation': '修正工具协议后读取、测试和修复。'}
                return AssistantResponse(tool_calls=[ToolCall(id='single-' + str(self.n), name=name, arguments=args)])
        c = await assist(t, Model(), ex, asyncio.Event(), workspace=w)
        assert c['model_responses'] == 7 and len(c['snapshot']['edits']) == 1 and len(ex.calls) == 2
        assert [r['result']['verdict'] for r in c['tests']] == ['WRONG_ANSWER', 'ACCEPTED']
    asyncio.run(run())
