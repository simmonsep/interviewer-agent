"""Practice mistakes as interview topics (F07Z-59 / P3.3b).

The product never reads the practice site, so a topic built here contains only what the
user typed: site, title, outcome, their own note, and an optional pasted statement. No
reference answer exists, which is why these topics carry `has_reference: False` and
`assessment: 'none'`: the answer is stored and never scored. Coaching may explain an
approach, but the observation label stays `inconclusive` — the product does not judge
algorithm questions (F07H).
"""

PREFIX = 'pm-'  # 3 + 36 = 39 chars: `topic_id` is capped at 40 (contracts.py:107).
MAX_TOPICS = 2

OUTCOME_TEXT = {'SOLVED': '做出来了', 'PARTIAL': '做出一部分',
                'UNSOLVED': '没做出来', 'REVIEWED': '只复盘了思路'}


def topic_from_record(row: dict) -> tuple[str, dict]:
    """Build one pack topic from a practice record. Deterministic: no model involved."""
    identity = PREFIX + str(row['id'])
    outcome = OUTCOME_TEXT.get(row['outcome'], row['outcome'])
    statement = (row['statement'] or '').strip()
    note = (row['note'] or '').strip()
    parts = []
    if statement:
        parts.append('用户粘贴的题面：' + statement)
    if note:
        parts.append('用户复盘：' + note)
    reference = '；'.join(parts) or f'用户只记录了「{row["title"]}」，没有题面或复盘。'
    base = (f'你在{row["site_label"]}做过「{row["title"]}」（{row["practiced_on"]}，{outcome}）。'
            '请讲一下当时的思路、时间与空间复杂度，以及你会补充哪些边界条件。')
    return identity, {
        'title': '练习错题 · ' + row['title'],
        'url': row['url'],
        'reference': reference,
        'base': base,
        'probe': '如果输入规模放大十倍，或者出现空输入，你的方案还成立吗？请指出失效的地方。',
        'retest': '请完整再讲一遍你现在的思路，并说明这次会怎么避免上次的问题。',
        'has_reference': False,
        'assessment': 'none',
        'origin': 'practice_record',
        'practice': {'record_id': str(row['id']), 'site_label': row['site_label'],
                     'url': row['url'], 'practiced_on': row['practiced_on'].isoformat(),
                     'outcome': row['outcome']},
    }


async def mistake_topics(conn, goal_id: str, limit: int = MAX_TOPICS) -> dict[str, dict]:
    """Latest unsolved/partial records for this goal, newest first.

    Only those two outcomes: a solved question is not a mistake, and a `REVIEWED` record is
    the user saying they did not attempt it. Deleting a record removes it from future
    sessions, because the snapshot is built at session creation.
    """
    rows = await (await conn.execute(
        "SELECT id,site_label,title,url,practiced_on,outcome,note,statement "
        "FROM app.career_practice_records "
        "WHERE goal_id=%s::uuid AND outcome IN ('UNSOLVED','PARTIAL') "
        "ORDER BY practiced_on DESC, created_at DESC LIMIT %s",
        (goal_id, limit))).fetchall()
    topics: dict[str, dict] = {}
    for row in rows:
        identity, item = topic_from_record(dict(row))
        topics[identity] = item
    return topics
