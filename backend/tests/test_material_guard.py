"""Candidate material guard: unit tests on synthetic shapes, not on one model's output.

The real scoring against the eight C2-labelled rewrites is a private artifact
(`.private/f07x/material-guard-score.json`); these tests pin the *rule semantics* so the
guard cannot silently drift into a word blacklist.
"""
import pytest

from interview_agent.evaluation.material_guard import ALL_FAMILIES, codes, scan

SOURCE = '参与订单服务的缓存改造，负责缓存与数据库一致性'


def test_a_rewrite_that_only_restates_the_source_is_not_flagged():
    assert codes('参与订单服务缓存改造，负责缓存与数据库一致性', SOURCE) == []


def test_outcome_family_requires_the_assertion_to_be_absent_from_the_source():
    assert 'unsupported_outcome' not in codes('负责缓存与数据库一致性', SOURCE)
    assert 'unsupported_outcome' in codes(
        '负责缓存与数据库一致性，保障系统稳定运行', SOURCE, ('outcome',))
    # if the source already claims it, the rewrite is entitled to keep it
    assert 'unsupported_outcome' not in codes(
        '保障系统稳定运行', '负责缓存改造，保障系统稳定运行', ('outcome',))


def test_frequency_family_only_fires_when_the_source_is_silent():
    assert 'unsupported_precision' in codes('日常维护对账脚本', '维护对账脚本', ('frequency',))
    assert 'unsupported_precision' not in codes('日常维护对账脚本', '日常维护对账脚本', ('frequency',))


def test_responsibility_family_is_relative_to_the_source_wording():
    # source already says 负责, so 负责 in the rewrite is not an escalation
    assert 'responsibility_inflation' not in codes(
        '负责缓存与数据库一致性', SOURCE, ('responsibility',))
    assert 'responsibility_inflation' in codes(
        '承接订单相关事件的流程处理', '参与订单事件处理', ('responsibility',))


def test_activity_family_catches_the_flagrant_shape():
    assert 'unsupported_activity' in codes(
        '负责缓存与数据库一致性方案的设计与落地', SOURCE, ('activity',))


def test_activity_family_cannot_tell_paraphrase_from_inflation():
    """Documented limitation, measured on real output: the same rule that catches
    "设计与落地" also flags a legitimate 重构→改造 paraphrase. Keep it visible."""
    assert 'unsupported_activity' in codes('完成导出链路的改造工作', '负责报表导出模块的重构',
                                           ('activity',))
    assert 'unsupported_activity' in codes('接口性能优化', '平均响应时间从 200ms 降到 80ms',
                                           ('activity',))


def test_every_hit_is_explainable():
    hits = scan('负责缓存与数据库一致性方案的设计与落地，保障系统稳定运行', SOURCE)
    assert hits and all({'family', 'code', 'term', 'why'} <= set(hit) for hit in hits)
    assert {hit['family'] for hit in hits} <= set(ALL_FAMILIES)


@pytest.mark.parametrize('families', [('outcome',), ('frequency',), ('responsibility',),
                                      ('outcome', 'frequency', 'responsibility')])
def test_partial_families_never_fire_on_a_pure_restatement(families):
    assert not scan('参与订单服务缓存改造，负责缓存与数据库一致性', SOURCE, families)


def test_role_marker_family_catches_a_dropped_stance():
    """Found in grounding-v3 / grounding-d output: "参与X" rewritten as the bare activity."""
    assert 'responsibility_inflation' in codes(
        '订单服务缓存改造：负责缓存与数据库一致性', SOURCE, ('role_marker',))
    assert 'responsibility_inflation' not in codes(
        '参与订单服务缓存改造，负责缓存与数据库一致性', SOURCE, ('role_marker',))


def test_role_marker_family_only_applies_when_the_source_limited_the_stance():
    assert codes('负责订单服务缓存改造', '负责订单服务的缓存改造', ('role_marker',)) == []

