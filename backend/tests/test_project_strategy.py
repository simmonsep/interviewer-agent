"""新增业务 1 · 节点 B: strategy selection is deterministic and can always fall back.

Covers the plan's acceptance points that do not need a model: same resume with different answers
leads to different probe angles; a card without an anchor is refused; the existing template stays
the fallback when nothing matches.
"""
from interview_agent.interviews.project_strategy import (
    StrategyCard, anchors_are_grounded, choose_strategy)

LOCK = StrategyCard(
    id='lock-verification', title='锁的验证与失效边界',
    applies_to=('Redis', '锁'), trigger=('SETNX', '过期时间'),
    dimensions=('verification', 'failure'),
    example_question='锁租期到了但业务还没跑完，新持有者接手后旧执行者会怎样？')
TX = StrategyCard(
    id='tx-tradeoff', title='事务边界的取舍',
    applies_to=('PostgreSQL', '事务'), trigger=('事务', '隔离'),
    dimensions=('tradeoff', 'contribution'),
    example_question='这段事务为什么这样切边界？换一种切法会付出什么代价？')


def test_same_resume_different_answers_take_different_angles():
    redis_answer = '我负责缓存层，用 SETNX 加过期时间做锁，释放时比较持有者标识。'
    sql_answer = '我负责订单写入，事务里先写订单再写明细，隔离级别用的是默认。'
    first = choose_strategy([LOCK, TX], anchor_text=redis_answer, dimension='verification')
    second = choose_strategy([LOCK, TX], anchor_text=sql_answer, dimension='tradeoff')
    assert first and first.id == 'lock-verification'
    assert second and second.id == 'tx-tradeoff'
    assert first.id != second.id


def test_no_match_falls_back_to_the_existing_template():
    assert choose_strategy([LOCK, TX], anchor_text='我做过一个前端页面。', dimension='verification') is None


def test_dimension_filter_excludes_cards_that_do_not_cover_it():
    only_tradeoff = StrategyCard(id='x', title='x', trigger=('SETNX',), dimensions=('tradeoff',))
    assert choose_strategy([only_tradeoff], anchor_text='用 SETNX 做锁', dimension='verification') is None


def test_project_questions_turned_off_means_no_strategy():
    assert choose_strategy([LOCK], anchor_text='用 SETNX 做锁', dimension='verification', allow=False) is None


def test_jd_only_match_still_works_but_scores_lower_than_an_anchor_match():
    anchor = choose_strategy([LOCK, TX], anchor_text='用 SETNX 加过期时间', jd_text='熟悉 Redis',
                             dimension='verification')
    jd_only = choose_strategy([LOCK, TX], anchor_text='', jd_text='熟悉 Redis',
                              dimension='verification')
    assert anchor and anchor.id == 'lock-verification'
    assert jd_only and jd_only.id == 'lock-verification'


def test_a_card_without_an_anchor_is_refused():
    assert anchors_are_grounded(LOCK, fact_ids=[], answer_text='') is False
    assert anchors_are_grounded(LOCK, fact_ids=['fact-1']) is True
    assert anchors_are_grounded(LOCK, fact_ids=[], answer_text='我在订单服务里做过') is True
    assert anchors_are_grounded(None, fact_ids=['fact-1']) is False
