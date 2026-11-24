"""Deterministic interview context compilation (S02A).

The model view is derived from the frozen run snapshot, not from live business rows:

- the full history is still the authority (`validate_decision` runs on it), and
- this module only decides what may enter the model request.

Two contracts live here:

1. **Mode isolation.** A formal interview may read this session's questions, answers,
   coverage and the current topic's references. It may not read cross-session coaching
   advice, past answer keys or historical weakness summaries; coaching may read the same
   account/goal/role history that is not retracted. The last check happens here, so an old
   snapshot that already froze cross-session rows still cannot reach the model.
2. **Stable clues instead of free text.** When an early question leaves the recent window,
   the model still gets its topic, question/answer/observation IDs and whether it was
   assisted. It never gets a model-written summary of them.

No model call, no database access, no clock: the same snapshot always compiles to the same
view (`context_version` is part of the output).
"""
from __future__ import annotations

import json

from .catalog import topic
from .contracts import question_coverage

CONTEXT_VERSION = 'interview-context-v1'
# Protected: never trimmed, whatever the budget. The current answer and the confirmed facts
# are the evidence the model must judge; trimming them would change the judgement silently.
PROTECTED_KEYS = ('policy', 'facts', 'answer', 'current_question', 'assisted_topics')
RECENT_QUESTIONS = 6
RECENT_OBSERVATIONS = 6
QUESTION_TEXT_LIMIT = 50
OBSERVATION_TEXT_LIMITS = (('summary', 90), ('answer_quote', 60))
# F07Z-167 Phase 2: the earliest answered rounds leave the recent window but must stay
# traceable by id. Anchors carry a bounded excerpt of the user's own answer, never a summary.
EARLY_ANCHORS = 3
ANCHOR_TEXT_LIMIT = 120
ANSWER_READ_LIMIT = 400


def mode_of(scope):
    return (scope.get('policy') or {}).get('mode') or 'interview'


# The server tags the two sources explicitly when it freezes the run payload. An entry with no
# recognisable tag was already frozen into *this* run, so it is treated as current-session data;
# only the explicit cross-session marker is what mode isolation removes.
CROSS_SESSION_ORIGIN = 'prior_observation_to_retest'
THIS_SESSION_ORIGIN = 'this_session_observation'


def cross_session(scope):
    """Observations the server explicitly froze from another session of this goal/role."""
    return [item for item in scope.get('prior_observations') or []
            if item.get('origin') == CROSS_SESSION_ORIGIN]


def this_session(scope):
    return [item for item in scope.get('prior_observations') or []
            if item.get('origin') != CROSS_SESSION_ORIGIN]


def _history_view(scope, truncated):
    history = []
    for question in (scope['question_history'] or [])[-RECENT_QUESTIONS:]:
        copied = dict(question)
        if isinstance(copied.get('text'), str) and len(copied['text']) > QUESTION_TEXT_LIMIT:
            copied['text'] = copied['text'][:QUESTION_TEXT_LIMIT]
            truncated.append('question_history.' + str(question.get('id')) + '.text')
        history.append(copied)
    if len(scope['question_history'] or []) > RECENT_QUESTIONS:
        truncated.append('question_history.latest_6_only')
    return history


def _observation_view(observations, truncated, prefix):
    memory = []
    for item in observations[-RECENT_OBSERVATIONS:]:
        copied = dict(item)
        for key, limit in OBSERVATION_TEXT_LIMITS:
            if isinstance(copied.get(key), str) and len(copied[key]) > limit:
                copied[key] = copied[key][:limit]
                truncated.append(prefix + '.' + str(copied.get('id')) + '.' + key)
        memory.append(copied)
    if len(observations) > RECENT_OBSERVATIONS:
        truncated.append(prefix + '.latest_6_only')
    return memory


def early_anchors(scope, truncated=None):
    """The first answered rounds, reduced to ids plus a bounded excerpt of the original answer.

    The anchor is what lets round ten point back at round one after the recent window moved on;
    `read_answer` can then return more of that exact answer, still by id and still this session.
    """
    anchors = []
    for item in (scope.get('answers') or [])[:EARLY_ANCHORS]:
        text = str(item.get('text') or '')
        anchor = {'answer_id': item.get('id'), 'question_id': item.get('question_id'),
                  'topic_id': item.get('topic_id'), 'assisted': bool(item.get('assisted')),
                  'text': text[:ANCHOR_TEXT_LIMIT]}
        if len(text) > ANCHOR_TEXT_LIMIT:
            anchor['text_truncated'] = True
            if truncated is not None:
                truncated.append('early_anchors.' + str(item.get('id')) + '.text')
        anchors.append(anchor)
    return anchors


def read_answer(scope, identity):
    """Return a bounded excerpt of one original answer in this session.

    Unknown ids are refused rather than guessed: the id must match an answer event frozen into
    this run snapshot, so a model cannot pull another session's or a fabricated answer.
    """
    for item in scope.get('answers') or []:
        if str(item.get('id')) == str(identity):
            text = str(item.get('text') or '')
            return {'answer_id': item.get('id'), 'question_id': item.get('question_id'),
                    'topic_id': item.get('topic_id'), 'assisted': bool(item.get('assisted')),
                    'text': text[:ANSWER_READ_LIMIT],
                    'truncated': len(text) > ANSWER_READ_LIMIT}
    raise KeyError('answer_not_in_session')


def working_state(scope, mode=None):
    """What the model must know even after the early turns left the recent window.

    Every clue keeps the IDs that lead back to the original question/answer/observation; it
    carries no free-form summary, so the model cannot invent detail the server never stored.
    """
    history = scope.get('question_history') or []
    asked_topics = list(dict.fromkeys(question.get('topic_id') for question in history if question.get('topic_id')))
    mode = mode or mode_of(scope)
    # A clue about another session's weakness is cross-session knowledge: coaching may reuse
    # it, a formal interview may not (its own session's clues stay).
    source = this_session(scope) if mode != 'coaching' else (
        (scope.get('prior_observations') or []) + this_session(scope))
    unresolved = []
    for item in source:
        if item.get('label') != 'needs_probe':
            continue
        clue = {'topic_id': item.get('topic_id'), 'observation_id': item.get('id'),
                'answer_id': item.get('answer_id'), 'origin': item.get('origin'),
                'assisted': bool(item.get('assisted'))}
        for optional in ('question_id', 'pack_version', 'source_session_id'):
            if item.get(optional):
                clue[optional] = item[optional]
        if clue not in unresolved:
            unresolved.append(clue)
    return {
        'asked_topics': asked_topics,
        'asked_question_ids': [question.get('id') for question in history if question.get('id')],
        'question_count': len(history),
        'remaining_questions': (scope.get('policy') or {}).get('max_questions', 0) - len(history),
        'coverage': question_coverage(scope),
        'assisted_topics': list(scope.get('assisted_topics') or []),
        'unresolved_clues': unresolved,
        'deadline_at': (scope.get('deadline_at') or None),
        # F07Z-167: structured long-round progress; skipped rounds are counted as asked and never
        # as wrong, and the profile/remaining time tell the model how much session is left.
        'skipped_question_ids': list(scope.get('skipped_question_ids') or []),
        'profile': (scope.get('policy') or {}).get('profile'),
        'remaining_seconds': scope.get('remaining_seconds'),
    }


def compile_interview_context(scope, budget_bytes=None):
    """Return the deterministic model view for this frozen snapshot.

    `budget_bytes` (optional) is a soft budget for the serialised view. When it does not fit,
    loose material is dropped in a fixed order - cross-session evidence first, then old
    observation narration, then old question text - and every cut is recorded. The current
    answer, the confirmed facts and the current question are never dropped.
    """
    truncated = []
    view = {key: scope[key] for key in ('policy', 'facts', 'current_question', 'answer', 'assisted_topics')}
    view['context_version'] = CONTEXT_VERSION
    view['mode'] = mode_of(scope)

    view['prior_written_results'] = [
        {key: item[key] for key in ('id', 'session_id', 'source_id', 'goal_version', 'topic_id', 'outcome',
                                    'qualification', 'policy') if key in item}
        for item in scope.get('prior_written_results') or []]
    for item, original in zip(view['prior_written_results'], scope.get('prior_written_results') or []):
        if original.get('comparison'):
            item['comparison'] = original['comparison']
        if original.get('application'):
            item['application'] = original['application']
    if scope.get('material_alignment'):
        view['material_alignment'] = scope['material_alignment']
    if scope.get('question_banks'):
        view['question_banks'] = scope['question_banks']
    view['application'] = scope.get('application')
    # 新增业务 1: candidate order for the interviewer (ids + titles only). The order comes from the
    # existing lexical ranking; the model still reads references itself and may ignore the list.
    context = scope.get('training_context') or {}
    if scope.get('jd_coverage'):
        from .jd_coverage import next_topics
        uncovered = next_topics(scope['jd_coverage'], scope.get('question_history') or [])
        view['unasked_jd_requirements'] = [
            {'quote': row['quote'], 'topic_ids': list(row['candidates'])[:3]} for row in uncovered[:4]
        ]
    if scope.get('pack', {}).get('topics'):
        from ..question_banks.ranking import suggest_topics
        picked = suggest_topics(scope['pack']['topics'], jd_text=context.get('jd_text', ''),
                                direction=context.get('direction', ''), limit=5)
        if picked:
            view['suggested_topics'] = picked
        generated = [{'id':tid,'title':item['title'],
                      **({'has_public_reference': True} if item.get('public_reference') else {})}
                     for tid,item in scope['pack']['topics'].items() if item.get('generated')]
        if generated:
            view['generated_candidates'] = generated
        from ..question_banks.ranking import preferred_private_ids
        private = preferred_private_ids(scope['pack']['topics'], jd_text=context.get('jd_text', ''),
                                        direction=context.get('direction', ''))
        if private:
            titles = scope['pack']['topics']
            view['preferred_candidates'] = [{'id': tid, 'title': str(titles[tid].get('title') or tid),
                                             'source': 'private_bank',
                                             'has_reference': bool(titles[tid].get('has_reference'))}
                                            for tid in private]
            # Plan §2.1.2: the role-pack backups are listed next to the private candidates so a
            # fallback can be judged by comparison. Ids and titles only, three at most.
            backups = [tid for tid in scope['pack']['topic_order']
                       if not titles[tid].get('generated')
                       and str(((titles[tid].get('bank_item') or {}).get('origin') or '')) != 'private']
            if backups:
                view['alternatives'] = [{'id': tid, 'title': str(titles[tid].get('title') or tid),
                                         'source': 'role_pack'} for tid in backups[:3]]
    # 新增业务 1 · B: offer one strategy the server picked deterministically. The interviewer may
    # take it or ignore it; a live run showed it does not reach for the corpus on its own.
    if scope.get('strategies') and (scope.get('answer') or scope.get('facts')):
        from .project_strategy import StrategyCard, choose_strategy
        cards = [StrategyCard(id=c['id'], title=c['title'],
                              applies_to=tuple(c.get('applies_to') or ()),
                              trigger=tuple(c.get('trigger') or ()),
                              dimensions=tuple(c.get('dimensions') or ()),
                              example_question=c.get('example_question') or '')
                 for c in scope['strategies']]
        anchor = ((scope.get('answer') or {}).get('text') or ''
                  or ' '.join(f.get('quote', '') for f in scope.get('facts') or []))
        jd = (scope.get('target') or {}).get('jd_text') or ''
        picked = choose_strategy(cards, anchor_text=anchor, jd_text=jd, dimension='verification',
                                 allow=bool(scope['policy'].get('allow_project_questions', True)))
        if picked:
            view['suggested_strategy'] = {'id': picked.id, 'title': picked.title,
                                          'example_question': picked.example_question}

    target = dict(scope['target'])
    for key, limit in (('text', 300), ('requirements', 300), ('title', 120)):
        if isinstance(target.get(key), str) and len(target[key]) > limit:
            target[key] = target[key][:limit]
            truncated.append('target.' + key)
    view['target'] = target

    view['question_history'] = _history_view(scope, truncated)
    view['early_anchors'] = early_anchors(scope, truncated)
    own = this_session(scope)
    view['prior_observations'] = _observation_view(own, truncated, 'prior_observations')

    cross = cross_session(scope)
    if mode_of(scope) == 'coaching':
        view['prior_evidence'] = _observation_view(cross, truncated, 'prior_evidence')
    else:
        # Formal interview: cross-session coaching/weakness material is not readable here,
        # even when an older snapshot already froze it into the run payload.
        view['prior_evidence'] = []
        if cross:
            truncated.append('prior_evidence.mode_isolated')
    # Kept as a top-level key for compatibility: the system prompt and existing tests read
    # `question_coverage` directly; the same value also lives in `working_state`.
    view['question_coverage'] = question_coverage(scope)
    view['working_state'] = working_state(scope, mode_of(scope))

    if len(truncated) > 10:
        truncated = truncated[:10] + ['truncated_more']
    view['model_view_truncation'] = truncated
    view['original_answer_and_confirmed_statements_truncated'] = False

    if budget_bytes is not None:
        _fit(view, budget_bytes)
    return view


def _size(view):
    return len(json.dumps(view, ensure_ascii=False, separators=(',', ':')).encode())


def _fit(view, budget_bytes):
    """Drop loose material until the serialised view fits. Protected keys are never touched."""
    for cut in ('prior_evidence', 'prior_written_results', 'question_banks', 'material_alignment'):
        if _size(view) <= budget_bytes:
            return
        if view.get(cut):
            view[cut] = [] if isinstance(view[cut], list) else None
            view['model_view_truncation'].append('budget.' + cut)
    if _size(view) <= budget_bytes:
        return
    for item in view['prior_observations']:
        for key, limit in OBSERVATION_TEXT_LIMITS:
            if _size(view) <= budget_bytes:
                return
            if isinstance(item.get(key), str) and len(item[key]) > 30:
                item[key] = item[key][:30]
                view['model_view_truncation'].append('budget.prior_observations.' + key)
    for item in view['question_history']:
        if _size(view) <= budget_bytes:
            return
        if isinstance(item.get('text'), str) and len(item['text']) > 12:
            item['text'] = item['text'][:12]
            view['model_view_truncation'].append('budget.question_history.text')
    # Nothing else is dropped: if the protected set alone exceeds the budget the caller must
    # surface a readable failure instead of silently truncating the current answer.
    view['model_view_truncation'].append('budget.exhausted' if _size(view) > budget_bytes else 'budget.fitted')


def scope_view(scope):
    """Backward-compatible alias used by tests and older call sites."""
    return compile_interview_context(scope)


def current_topic_view(scope):
    """The current topic's prepared payload, with project references narrowed to the fact."""
    current = scope.get('current_question')
    if not current:
        return None
    prepared = topic(scope, current['topic_id'])
    if current['topic_id'] == 'project':
        prepared = {**prepared, 'references': [ref for ref in prepared['references']
                                               if ref['id'] == current.get('fact_id')]}
    return prepared
