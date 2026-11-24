"""Regression tests for the cost-safety helpers (no model calls, no database).

External review §25/§26: a small selector bug spent real money past the approved budget,
so selection and the budget guard must be testable without running a paid batch.
"""
import pytest
from types import SimpleNamespace
from interview_agent.evaluation.judgement import load_suite
from interview_agent.evaluation.judgement_live import RUN_BUDGET_NANO, select_cases

ROOT = __import__('pathlib').Path(__file__).resolve().parents[2]


def _cases():
    return load_suite(ROOT)[1]


def test_selector_reads_the_options_dict():
    """The original bug: `getattr(options, 'cases')` on a dict matched nothing."""
    cases = _cases()
    picked = select_cases(cases, ['abstention'], {'cases': ['ABS-001', 'ABS-002']})
    assert [case.id for case in picked] == ['ABS-001', 'ABS-002']
    # a dict without the key must not silently mean "everything"
    assert select_cases(cases, ['abstention'], {}) == [c for c in cases if c.slice == 'abstention']


def test_selector_rejects_unknown_ids():
    cases = _cases()
    with pytest.raises(ValueError, match='unknown_case_ids'):
        select_cases(cases, ['abstention'], {'cases': ['ABS-999']})


def test_selector_respects_limit_and_slice():
    cases = _cases()
    picked = select_cases(cases, ['observation'], {}, max_cases=3)
    assert len(picked) == 3 and all(case.slice == 'observation' for case in picked)


def test_selector_with_namespace_is_not_supported():
    """Documents the old shape: attribute access is why it failed. Dicts only now."""
    cases = _cases()
    with pytest.raises(AttributeError):
        select_cases(cases, ['abstention'], SimpleNamespace(cases=['ABS-001']))


def test_run_budget_cap_is_below_the_authorized_stage():
    from interview_agent.evaluation.judgement_live import MAX_CNY, MAX_NANO
    assert MAX_CNY == '0.45'                     # the stage the user actually authorized
    assert RUN_BUDGET_NANO == 450_000_000
    # A per-case authorization must exceed one reservation, or the first request of the
    # case is refused before it is ever sent.
    assert _policy().reservation_nano < MAX_NANO


def test_run_cap_is_cumulative_so_repeats_cannot_multiply_the_stage_budget():
    """Splitting a stage into runs must not multiply the worst case (F07Z-08)."""
    from interview_agent.evaluation.judgement import run_cap_nano
    assert run_cap_nano('0.45', '0.45') == 450_000_000
    # after a 0.117 run the next cap is what is left, not a fresh 0.45
    assert run_cap_nano('0.45', '0.333') == 333_000_000
    with pytest.raises(ValueError, match='run_budget_outside_stage_authorization'):
        run_cap_nano('0.45', '0.9')
    # a cap that cannot hold one reservation would start a run it must immediately stop
    with pytest.raises(ValueError, match='run_budget_leaves_no_room_for_one_reservation'):
        run_cap_nano('0.45', '0.10')


def test_budget_guard_stops_before_reservation_exceeds_run_budget():
    """The guard is a worst-case guard: no single request may push the run over the cap."""
    policy = _policy()
    reservation = policy.reservation_nano
    spent = 0
    admitted = 0
    while spent + reservation <= RUN_BUDGET_NANO:
        spent += reservation
        admitted += 1
    assert admitted >= 1 and spent <= RUN_BUDGET_NANO
    assert spent + reservation > RUN_BUDGET_NANO
    # What the whole 0.45 stage releases once one reservation is held back.
    assert RUN_BUDGET_NANO - reservation == 302_544_000



# ---------------------------------------------------------------- deterministic branch

def _record(has_reference, label, deterministic, raw=None):
    tool_log = ([{'tool': 'label_normalisation', 'raw_label': raw, 'effective_label': label,
                  'reason': 'reference_present'}] if raw else [])
    return {'case_id': 'X', 'slice': 'abstention', 'deterministic_label_branch': deterministic,
            'http_calls': 0 if deterministic else 3, 'accounted_nano': 0 if deterministic else 3000,
            'model_requests': 0 if deterministic else 3,
            'case': {'slice': 'abstention', 'input': {'policy': {'has_reference': has_reference}},
                     'expected': {'allowed_labels': ['needs_probe']}},
            'output': {'status': 'ok', 'decision': {'observation': {'label': label}},
                       'tool_log': tool_log},
            'grade': {'status': 'graded', 'label_correct': True, 'rationale_guard': 'hit',
                      'forbidden_conclusion': False, 'passed': True}}


def test_summarize_live_keeps_the_deterministic_branch_out_of_quality():
    """Required 2: the no-reference anchor must report 0 live requests and 0 benchmark cost."""
    from interview_agent.evaluation.judgement_live import summarize_live
    records = [_record(False, 'inconclusive', True), _record(True, 'needs_probe', False)]
    report = summarize_live(records, spent_http=3, spent_nano=3000, manifest={'version': '0.5.0'},
                            slices=['abstention'])
    assert report['quality']['trials'] == 1
    branch = report['deterministic_label_branch']
    assert branch['trials'] == 1
    assert branch['live_provider_requests'] == 0
    assert branch['benchmark_provider_cost_cny'] == 0
    assert branch['provider_mode'] == 'scripted'
    # The two facts must stay separate: production still calls the model for the action.
    assert report['production_behavior']['label_source'] == 'deterministic'
    assert report['production_behavior']['action_source'] == 'model'


def test_normalisation_metrics_separate_raw_from_effective():
    """Required 1: a model that keeps saying inconclusive must stay visible."""
    from interview_agent.evaluation.judgement import label_diagnostics
    records = [_record(True, 'needs_probe', False, raw='inconclusive'),
               _record(True, 'needs_probe', False)]
    stats = label_diagnostics(records)
    assert stats['raw_inconclusive_with_reference_count'] == 1
    assert stats['raw_inconclusive_with_reference_rate'] == 0.5
    assert stats['label_normalization_count'] == 1
    assert stats['label_normalization_rate'] == 0.5
    assert stats['raw_label_distribution'] == {'inconclusive': 1, 'needs_probe': 1}
    assert stats['effective_label_distribution'] == {'needs_probe': 2}
    # Review §22: the two accuracies must share one denominator, or the pair misleads.
    comparison = stats['judgement_comparison']
    assert comparison['trials'] == 2
    assert comparison['raw_label_correct'] == 1 and comparison['effective_label_correct'] == 2
    assert comparison['normalization_dependency_rate'] == 0.5


def test_raw_and_effective_accuracy_use_the_model_quality_population():
    """The no-reference anchor is excluded from both numbers, not just from the effective one."""
    from interview_agent.evaluation.judgement import label_diagnostics
    stats = label_diagnostics([_record(False, 'inconclusive', True)])
    assert stats['judgement_comparison']['trials'] == 0
    assert stats['judgement_comparison']['raw_label_accuracy'] is None


def test_a_live_call_on_the_deterministic_anchor_fails_the_grade():
    """If someone runs ABS-002 against the model, the contract is not demonstrated."""
    from interview_agent.evaluation.judgement import grade, load_suite
    case = next(c for c in _cases() if c.id == 'ABS-002')
    output = {'status': 'ok', 'deterministic_label_contract': 'violated:label=inconclusive,calls=3',
              'decision': {'observation': {'label': 'inconclusive', 'summary': '不作判断'}},
              'actions': []}
    result = grade(case, output)
    assert result['label_correct'] is True and result['passed'] is False
    assert result['flag_for_human_review'] is True
    ok = grade(case, {**output, 'deterministic_label_contract': 'ok'})
    assert ok['passed'] is True


# ---------------------------------------------------------------- budget enforcement point

def _policy():
    from interview_agent.interviews.billing import InterviewPolicy
    return InterviewPolicy.load(ROOT / 'configs/f07q-interviews.json')


def _request(policy, body_bytes=None):
    """A request body that passes `BudgetedTransport`'s shape check, so the test measures
    the reservation ordering and not a payload rejection."""
    import json as _json
    import httpx
    payload = {'model': policy.model, 'thinking': {'type': 'disabled'}, 'stream': False,
               'max_tokens': policy.max_output_tokens, 'messages': [{'role': 'user', 'content': 'x'}]}
    content = body_bytes or _json.dumps(payload).encode()
    return httpx.Request('POST', policy.endpoint, content=content)


def test_budget_refusal_happens_before_the_provider_request_is_sent():
    """External review §17/§18: prove the aggregate cap sits at the real request boundary.

    The refusal must not depend on `--cases`, case count or an average-cost estimate: the
    transport reserves first and only then hands the request to the inner transport.
    Scenario mirrors the review: already spent over budget, one more reservation would exceed.
    """
    import asyncio
    import json as _json
    import httpx
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.runtime.deepseek import BudgetedTransport

    policy = _policy()

    class Refusing:
        """Stands in for the ledger: first reservation is admitted, the next one is refused
        exactly as `InterviewLedger.reserve` does when `used + reservation > max_nano`."""

        def __init__(self):
            self.policy, self.reserved = policy, 0

        async def reserve(self, claim, context, payload_hash, payload_bytes):
            self.reserved += 1
            if self.reserved > 1:
                raise BillingUnavailable('model_budget_exhausted')
            return 'call-1'

        async def settle(self, *args, **kwargs):
            return True

    sent = []

    async def inner(request):
        sent.append(len(request.content))
        return httpx.Response(200, json={'model': 'offline', 'usage': {}, 'choices': []})

    ledger = Refusing()
    transport = BudgetedTransport(httpx.MockTransport(inner), ledger, claim=None, context={})

    async def scenario():
        first = await transport.handle_async_request(_request(policy))
        assert first.status_code == 200
        with pytest.raises(BillingUnavailable, match='model_budget_exhausted'):
            await transport.handle_async_request(_request(policy))

    asyncio.run(scenario())
    assert len(sent) == 1, 'the refused request must never reach the provider'
    assert ledger.reserved == 2


def test_budget_refusal_waits_for_the_payload_check_not_the_other_way_round():
    """An oversized body is refused even earlier, and also spends nothing."""
    import asyncio
    import httpx
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.runtime.deepseek import BudgetedTransport

    policy = _policy()

    class NeverReserves:
        policy = _policy()

        async def reserve(self, *args, **kwargs):
            raise AssertionError('an invalid payload must not reach the ledger')

        async def settle(self, *args, **kwargs):
            return True

    sent = []
    transport = BudgetedTransport(
        httpx.MockTransport(lambda request: sent.append(1)), NeverReserves(), claim=None, context={})
    oversized = _request(policy, body_bytes=b'x' * (policy.max_payload_bytes + 1))

    async def scenario():
        with pytest.raises(BillingUnavailable, match='model_payload_rejected'):
            await transport.handle_async_request(oversized)

    asyncio.run(scenario())
    assert not sent


