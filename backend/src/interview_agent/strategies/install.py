"""Validate one user strategy document: declarative cards, no questions with answers."""
from interview_agent.storage.errors import ApiError

from .contracts import DIMENSIONS, MAX_CARDS, MAX_TEXT, SLUG


def _text(value, code, limit, *, allow_empty=False):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ApiError(code, 400)
    text = value.strip()
    if (not text and not allow_empty) or len(text) > limit:
        raise ApiError(code, 400)
    if any(ord(char) < 32 and char not in '\n\t' for char in text):
        raise ApiError(code, 400)
    return text


def _list(value, code, limit=6, item_limit=30):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ApiError(code, 400)
    return [_text(item, code, item_limit) for item in value]


def parse_document(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ApiError('strategy_document_invalid', 400)
    if document.get('schema_version') != 1:
        raise ApiError('strategy_schema_version_unsupported', 400)
    name = _text(document.get('name'), 'strategy_name_invalid', 80)
    set_id = document.get('id')
    if set_id is not None:
        if not isinstance(set_id, str) or not SLUG.match(set_id):
            raise ApiError('strategy_set_id_invalid', 400)
    role = document.get('role')
    if role not in ('java-backend', 'ai-application'):
        raise ApiError('strategy_role_invalid', 400)
    cards = document.get('cards')
    if not isinstance(cards, list) or not 1 <= len(cards) <= MAX_CARDS:
        raise ApiError('strategy_cards_invalid', 400)

    normalized = []
    seen = set()
    total = 0
    for raw in cards:
        if not isinstance(raw, dict):
            raise ApiError('strategy_cards_invalid', 400)
        card_id = raw.get('id')
        if not isinstance(card_id, str) or not SLUG.match(card_id):
            raise ApiError('strategy_id_invalid', 400)
        if card_id in seen:
            raise ApiError('strategy_id_duplicate', 400)
        seen.add(card_id)
        dimensions = raw.get('dimensions') or list(DIMENSIONS)
        if (not isinstance(dimensions, list) or not dimensions
                or any(item not in DIMENSIONS for item in dimensions)):
            raise ApiError('strategy_dimensions_invalid', 400)
        card = {
            'id': card_id,
            'title': _text(raw.get('title'), 'strategy_title_invalid', 40),
            'applies_to': _list(raw.get('applies_to'), 'strategy_applies_to_invalid'),
            'trigger': _list(raw.get('trigger'), 'strategy_trigger_invalid'),
            'dimensions': list(dict.fromkeys(dimensions)),
            'example_question': _text(raw.get('example_question'), 'strategy_example_invalid',
                                      220, allow_empty=True),
            'source_url': _text(raw.get('source_url'), 'strategy_source_invalid', 300,
                                allow_empty=True),
            'source_note': _text(raw.get('source_note'), 'strategy_source_invalid', 120,
                                 allow_empty=True),
        }
        total += sum(len(str(value)) for value in card.values() if isinstance(value, str))
        normalized.append(card)
    if total > MAX_TEXT:
        raise ApiError('strategy_document_too_large', 400)

    # A user card must not silently shadow a preinstalled one: the frozen list would then carry two
    # cards with the same id and `strategy_id` could not tell them apart.
    from interview_agent.interviews.project_strategy import cards_for
    builtin = {card.id for card in cards_for(role)}
    for card in normalized:
        if card['id'] in builtin:
            raise ApiError('strategy_id_conflicts_builtin', 400)

    return {'schema_version': 1, 'name': name,
            **({'id': set_id} if set_id is not None else {}),
            'role': role, 'cards': normalized,
            'source': 'user_imported_declarative'}
