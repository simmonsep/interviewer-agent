import asyncio
from pathlib import Path
import shutil

import pytest

from interview_agent.evaluation.wiki_replay import evaluate, load_suite, make_scope, review_markdown

SUITE = Path(__file__).resolve().parents[2] / 'benchmarks/wiki-replay-v0.1'
HOLDOUT = SUITE.with_name('wiki-replay-holdout-v0.1')


def test_frozen_wiki_replay_is_deterministic_and_keeps_constraints():
    manifest, cases, oracles = load_suite(SUITE)
    for case in cases:
        b, c = make_scope(case, 'B'), make_scope(case, 'C')
        assert {k: v for k, v in b.items() if k != 'pack'} == {k: v for k, v in c.items() if k != 'pack'}
        common = b['pack']['topics']
        assert {k: c['pack']['topics'][k] for k in common} == common
        assert set(c['pack']['topics']) - set(common) or b == c
    report, reviews = asyncio.run(evaluate(manifest, cases, oracles))
    assert (report, reviews) == asyncio.run(evaluate(manifest, cases, oracles))
    summary = report['summary']
    assert summary['protocol_passed'] == 12
    assert summary['wiki_offered'] == 6 and summary['wiki_used'] == 3
    assert summary['wiki_outcomes'] == {'used': 3, 'offered_not_read': 3,
                                        'withheld_pending': 3, 'withheld_conflict': 3}
    assert summary['model_http_calls'] == summary['semantic_review_total'] == 0
    assert all(row['decision'] == 'pending' for row in reviews)
    blind = review_markdown(reviews)
    assert '待审' in blind and 'offered_not_read' not in blind


def test_changed_fixture_is_refused(tmp_path):
    target = tmp_path / 'suite'
    shutil.copytree(SUITE, target)
    with (target / 'cases.jsonl').open('ab') as file:
        file.write(b'\n')
    with pytest.raises(ValueError, match='wiki_replay_checksum_mismatch'):
        load_suite(target)


def test_holdout_replays_new_cases_without_putting_oracles_in_the_review():
    _, development, _ = load_suite(SUITE)
    manifest, cases, oracles = load_suite(HOLDOUT)
    assert manifest['split'] == 'holdout'
    assert {case.id for case in cases}.isdisjoint(case.id for case in development)
    for field in ('jd', 'question', 'answer'):
        assert {getattr(case, field) for case in cases}.isdisjoint(getattr(case, field) for case in development)
    assert len({(case.family, case.shape) for case in cases}) == len(cases) == 12

    report, reviews = asyncio.run(evaluate(manifest, cases, oracles))
    assert report['summary']['protocol_passed'] == 12
    assert report['summary']['semantic_review_total'] == 0
    assert sum(row['review_arm_order'] == ['B', 'C'] for row in report['cases']) == 6
    blind = review_markdown(reviews)
    assert 'X 的难度与边界' in blind and 'Y 的难度与边界' in blind
    assert all(oracle.review_focus not in blind for oracle in oracles.values())
