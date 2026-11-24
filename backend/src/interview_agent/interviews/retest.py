"""Deterministic retest suggestions for a finished session (收尾 01, step 1).

Why this module exists: a finished interview already records *what* was missing, but the
review screen could only say "训练观察待新情景核验". The user needs to know which question
to practise and why. The suggestion therefore points back at the stored observation and
takes the question text from the question pack this session already froze.

No model call and no new table: every emitted field is copied from the observation or from
the frozen pack, never inferred from the answer text.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import catalog

# Only `needs_probe` is "we saw a concrete gap and a reference exists". `inconclusive` means
# there was not enough signal, and `supported_in_this_answer` is not a weakness at all, so
# neither may become a targeted drill.
ELIGIBLE_LABEL = 'needs_probe'

# Fields the review may show. Anything else would be an inference the caller cannot attribute.
FIELDS = ('observation_id', 'answer_id', 'topic_id', 'topic_title', 'question',
          'reference_url', 'pack_version', 'assisted', 'reason')


def frozen_topic(topic_id: str, pack: dict[str, Any] | None) -> dict[str, Any] | None:
    """The frozen pack item for this topic id, or the authored catalog entry.

    Selected question banks key topics by a bank-scoped id (`b-...`), but the frozen item
    carries the authored content (title/url/retest) and its `bank_item.item_id` names the
    built-in topic. Both routes return the same authored text - we never compose a question.
    """
    item = ((pack or {}).get('topics') or {}).get(topic_id)
    if isinstance(item, dict) and item.get('retest'):
        return {'title': item.get('title') or topic_id, 'retest': item['retest'],
                'url': item.get('url') or '', 'origin': 'frozen_pack'}
    identity = topic_id
    if isinstance(item, dict):
        ref = item.get('bank_item') or {}
        if ref.get('origin') == 'builtin' and ref.get('item_id'):
            identity = str(ref['item_id'])
    authored = catalog.TOPICS.get(identity)
    if authored:
        return {'title': authored['title'], 'retest': authored['retest'],
                'url': authored['url'], 'origin': 'authored_pack'}
    return None


def suggestions(observations: Iterable[dict[str, Any]],
                pack: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One suggestion per eligible technical observation, in the order given.

    A suggestion is emitted only when all of these hold:
      * the observation still needs a probe and was not corrected (撤回);
      * it is a technical topic, not the project deep-dive (that one is not a fixed question);
      * the session stored a reference (id + quote) for it;
      * the frozen pack (or the authored catalog) ships a `retest` for that topic.
    """
    out: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for observation in observations:
        if observation.get('label') != ELIGIBLE_LABEL:
            continue
        if observation.get('correction'):
            continue
        topic_id = str(observation.get('topic_id') or '')
        if not topic_id or topic_id == 'project':
            continue
        item = frozen_topic(topic_id, pack)
        if not item:
            continue
        if not (observation.get('evidence_id') and str(observation.get('evidence_quote') or '').strip()):
            continue
        # One drill per topic: a base and its probe answer can both be flagged, and repeating the
        # same authored retest twice would look like two different exercises.
        if topic_id in seen_topics:
            continue
        seen_topics.add(topic_id)
        out.append({
            'observation_id': observation.get('id'),
            'answer_id': observation.get('answer_id'),
            'topic_id': topic_id,
            'topic_title': item['title'],
            # A new scenario for the same topic, not the original question repeated.
            'question': item['retest'],
            'reference_url': item['url'],
            'pack_version': observation.get('pack_version') or (pack or {}).get('version') or catalog.VERSION,
            'assisted': bool(observation.get('assisted')),
            'reason': 'needs_probe_with_reference',
        })
    return out
