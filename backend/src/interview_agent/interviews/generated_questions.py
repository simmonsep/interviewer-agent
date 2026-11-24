"""JD practice questions proposed by the planner, with no verified answer key."""
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, ValidationError

from interview_agent.contracts.common import Contract

MAX_GENERATED_QUESTIONS = 6
GENERATED_SOURCE = 'jd_generated'
WIKI_SOURCE = 'wiki_reused'


class GeneratedQuestion(Contract):
    model_config = ConfigDict(strict=True, extra='ignore')

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    probe: Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)] = ''
    target_quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    kind: Literal['knowledge', 'scenario'] = 'scenario'
    difficulty: Literal['foundation', 'intermediate', 'advanced'] = 'intermediate'
    source_id: str = Field(default='', max_length=48)
    source_quote: str = Field(default='', max_length=300)
    experience_id: str = Field(default='', max_length=48)
    experience_quote: str = Field(default='', max_length=240)


def _question_key(text):
    return ''.join(character for character in text.casefold() if character.isalnum())


def accepted_questions(values, basis):
    """Retain bounded, distinct questions anchored in the target shown to the planner."""
    if not basis or not isinstance(values, list):
        return []
    seen = {_question_key(text) for text in basis.get('known_questions', basis.get('existing_questions', []))}
    accepted = []
    for value in values[:MAX_GENERATED_QUESTIONS]:
        if isinstance(value, dict):
            # A malformed optional citation must not discard a valid question.
            limits = {'source_id': 48, 'source_quote': 300, 'experience_id': 48, 'experience_quote': 240}
            value = {key: entry for key, entry in value.items()
                     if key not in limits or (isinstance(entry, str) and len(entry) <= limits[key])}
        try:
            item = GeneratedQuestion.model_validate(value)
        except ValidationError:
            continue
        anchor = _question_key(item.target_quote)
        key = _question_key(item.question)
        if not anchor or item.target_quote not in basis['text'] or not key or key in seen:
            continue
        seen.add(key)
        probe_key = _question_key(item.probe)
        if probe_key and probe_key not in seen:
            seen.add(probe_key)
        else:
            item = item.model_copy(update={'probe': ''})
        accepted.append(item)
    return accepted


def question_topics(questions, target_kind):
    topics = {}
    for item in questions:
        identity_fields = {key: value for key, value in item.items()
                           if key not in ('public_reference', 'experience_reference')}
        encoded = json.dumps(identity_fields, sort_keys=True, ensure_ascii=False).encode('utf-8')
        identity = 'g-' + sha256(encoded).hexdigest()[:32]
        topics[identity] = {
            'title': item['title'], 'base': item['question'], 'probe': item['probe'],
            'retest': '', 'url': '', 'reference': '', 'has_reference': False,
            'origin': GENERATED_SOURCE,
            'generated': {
                **{key: item[key] for key in ('kind', 'difficulty', 'target_quote')},
                'target_kind': target_kind,
            },
            **({'public_reference': item['public_reference']} if item.get('public_reference') else {}),
            **({'experience_reference': item['experience_reference']} if item.get('experience_reference') else {}),
            **({'wiki': item['wiki']} if item.get('wiki') else {}),
        }
    return topics
