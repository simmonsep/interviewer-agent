"""Targeted coaching from a review's retest suggestion (F07Z-165 A).

The client only names the source session and observation. The server re-reads both under the
same goal, proves the observation is still retestable, and freezes the authored question text
plus its provenance before the session exists. Nothing here calls a model.
"""
from interview_agent.storage.errors import ApiError

from .retest import suggestions


async def resolve(conn, goal_id, source_session_id, observation_id):
    source = await (await conn.execute(
        '''SELECT id,status,input_payload FROM app.career_interviews WHERE id=%s AND goal_id=%s''',
        (source_session_id, goal_id))).fetchone()
    if not source or source['status'] != 'COMPLETED':
        raise ApiError('focus_source_unavailable', 404)
    row = await (await conn.execute(
        '''SELECT o.*,c.reason AS correction
             FROM app.career_interview_observations o
             LEFT JOIN app.career_interview_corrections c ON c.observation_id=o.id
            WHERE o.id=%s AND o.session_id=%s AND o.goal_id=%s''',
        (observation_id, source_session_id, goal_id))).fetchone()
    if not row:
        raise ApiError('focus_observation_unavailable', 404)
    payload = row['payload'] or {}
    observation = {
        'id': str(row['id']), 'answer_id': str(row['answer_id']),
        'topic_id': payload.get('topic_id'), 'label': payload.get('label'),
        'correction': row['correction'],
        'evidence_id': payload.get('evidence_id'), 'evidence_quote': payload.get('evidence_quote'),
        'pack_version': payload.get('pack_version'), 'assisted': payload.get('assisted'),
    }
    pack = (source['input_payload'] or {}).get('pack')
    try:
        [suggestion] = suggestions([observation], pack)
    except ValueError:
        # Not eligible: supported/inconclusive, retracted, project, no reference, or no retest.
        raise ApiError('focus_observation_not_retestable', 409) from None
    return {
        'source_session_id': str(source['id']),
        'observation_id': str(row['id']),
        'source_answer_id': str(row['answer_id']),
        'topic_id': suggestion['topic_id'],
        'topic_title': suggestion['topic_title'],
        # topic() builds authored question ids as `<topic>-<kind>`.
        'question_id': suggestion['topic_id'] + '-retest',
        'question_text': suggestion['question'],
        'reference_url': suggestion.get('reference_url') or '',
        'source_pack_version': suggestion['pack_version'],
        'source_evidence_quote': observation['evidence_quote'],
        'source_evidence_id': observation['evidence_id'],
    }


def pack_with_focus(pack, focus):
    """Inject the focused topic as an authored one so every existing lookup keeps working."""
    topics = dict(pack.get('topics') or {})
    existing = dict(topics.get(focus['topic_id']) or {})
    item = {key: existing[key] for key in ('base', 'probe', 'reference', 'url', 'title', 'has_reference')
            if existing.get(key) is not None}
    item.update({
        'title': item.get('title') or focus['topic_title'],
        'retest': focus['question_text'],
        'reference': item.get('reference') or focus['source_evidence_quote'],
        'has_reference': True,
        'origin': 'retest_suggestion',
    })
    topics[focus['topic_id']] = item
    return {**pack, 'topics': topics}
