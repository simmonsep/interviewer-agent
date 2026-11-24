"""新增业务 1 · A3: ordering is reviewable, deterministic and scope-safe.

The acceptance for this slice is the ordering property, not the model's behaviour: the same bank
must reorder when the JD or direction changes, and a miss must never broaden the scope.
"""
from interview_agent.question_banks.ranking import keywords, rank, suggest_topics

ITEMS = [
    {'item_id': 'thread-memory', 'kind': 'knowledge', 'roles': ['ai-application'],
     'title': 'LangGraph会话状态与长期记忆', 'question': 'state、checkpointer 和 store 的职责？',
     'reference': 'checkpointer 按 thread_id 保存检查点', 'tags': ['Python', 'LangGraph', 'memory']},
    {'item_id': 'tool-retry', 'kind': 'knowledge', 'roles': ['ai-application'],
     'title': '工具调用的重试与幂等', 'question': '工具调用超时后怎样安全重试？',
     'reference': '幂等键与去重', 'tags': ['工具调用', '幂等']},
    {'item_id': 'sql-index', 'kind': 'knowledge', 'roles': ['java-backend'],
     'title': '慢查询与索引选择', 'question': '一条慢 SQL 你会怎样定位？',
     'reference': '执行计划', 'tags': ['PostgreSQL', '索引']},
    {'item_id': 'choice-1', 'kind': 'single_choice', 'roles': ['ai-application'],
     'title': '选择题不该被当八股问', 'question': 'A/B/C', 'tags': []},
]


def test_langgraph_jd_puts_the_langgraph_item_first():
    ordered = rank(ITEMS, jd_text='熟悉 Python 与 LangGraph，做过 Agent 记忆与恢复',
                   direction='AI 应用开发实习')
    assert [item['item_id'] for item in ordered][0] == 'thread-memory'
    assert 'choice-1' not in [item['item_id'] for item in ordered], 'choice items are not askable'


def test_switching_the_jd_reorders_the_same_bank():
    ordered = rank(ITEMS, jd_text='工具调用超时、重试与幂等', direction='Agent 工程')
    assert [item['item_id'] for item in ordered][0] == 'tool-retry'


def test_asked_items_are_removed_and_nothing_is_invented():
    ordered = rank(ITEMS, jd_text='LangGraph', asked=['thread-memory'])
    assert 'thread-memory' not in [item['item_id'] for item in ordered]
    assert len(ordered) == 2  # tool-retry + sql-index; choice item filtered by kind


def test_no_match_keeps_the_bank_order_and_adds_nothing():
    ordered = rank(ITEMS, jd_text='量子计算与编译器')
    assert [item['item_id'] for item in ordered] == ['thread-memory', 'tool-retry', 'sql-index']
    assert all(set(item) == set(ITEMS[index]) for index, item in enumerate(ordered))


def test_keywords_mix_ascii_terms_and_cjk_bigrams():
    terms = keywords('LangGraph 工具调用重试')
    assert 'langgraph' in terms and 'check' not in terms
    assert {'工具', '调用', '重试'} <= terms, 'Chinese text has no spaces, so bigrams carry it'
    # A single CJK character produces no bigram and must not crash.
    assert keywords('的') == set()


def test_suggest_topics_returns_ids_and_titles_only():
    topics = {
        'agent-tools': {'title': '工具参数与权限', 'base': '工具调用要注意什么？', 'reference': '权限与预算'},
        'memory': {'title': '状态与恢复', 'base': '进程重启后怎样继续？', 'reference': 'checkpointer 按 thread_id'},
        'sql': {'title': '慢查询', 'base': '一条慢 SQL 怎么查？', 'reference': '执行计划'},
    }
    picked = suggest_topics(topics, jd_text='熟悉 LangGraph 的 checkpointer 与 thread_id', limit=3)
    assert picked[0]['id'] == 'memory'
    assert set(picked[0]) == {'id', 'title'}, 'no references or questions may be sent here'


def test_suggest_topics_without_a_query_keeps_the_pack_order():
    topics = {'a': {'title': 'A', 'base': 'x'}, 'b': {'title': 'B', 'base': 'y'}}
    assert [item['id'] for item in suggest_topics(topics, limit=2)] == ['a', 'b']
