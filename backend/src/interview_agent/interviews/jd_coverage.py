"""Bound the job requirements and link frozen candidates to what was actually asked."""
import re

from interview_agent.connectors.interview_experiences import experience_wiki_topic
from interview_agent.connectors.public_reference import explicit_topic
from interview_agent.question_banks.ranking import GENERIC, keywords

from .generated_questions import question_topics

MAX_REQUIREMENTS = 12
MAX_QUOTE = 120


def requirements(text, topics):
    quotes = dict.fromkeys(
        part.strip()[:MAX_QUOTE].rstrip()
        for part in re.split(r'[\r\n。；;、，,]+|(?<=\.)\s+', text)
        if keywords(part.strip()[:MAX_QUOTE]) - GENERIC
    )
    rows = [{'id': f'r{index}', 'quote': quote, 'candidates': {}}
            for index, quote in enumerate(list(quotes)[:MAX_REQUIREMENTS], 1)]
    query = [(row, keywords(row['quote']) - GENERIC) for row in rows]
    for topic_id, topic in topics.items():
        terms = keywords(f"{topic['title']} {topic['base']}") - GENERIC
        scores = [(terms & required, row) for row, required in query]
        if not scores:
            break
        best = max(len(common) for common, _ in scores)
        matches = [(common, row) for common, row in scores if len(common) == best]
        if len(matches) == 1:
            common, row = matches[0]
            if len(common) < 2 and not any(term.isascii() and len(term) >= 3 for term in common):
                continue
            origin = (topic.get('bank_item') or {}).get('origin')
            row['candidates'][topic_id] = 'private_bank' if origin == 'private' else 'role_pack'
    return rows


def include_generated(rows, questions, target_kind):
    """Join only unambiguous input anchors and keep older generated topics unchanged."""
    result = [{**row, 'candidates': dict(row['candidates'])} for row in rows]
    for topic_id, topic in question_topics(questions, target_kind).items():
        if wiki := topic.get('wiki'):
            topic_key_for = experience_wiki_topic if topic.get('experience_reference') else explicit_topic
            matches = [row for row in result if topic_key_for(row['quote']) == wiki['topic_key']]
            source = 'wiki'
        else:
            anchor = topic['generated']['target_quote']
            matches = [row for row in result if anchor and row['quote'] == anchor]
            if not matches:
                matches = [row for row in result if anchor and anchor in row['quote']]
            source = 'jd_generated'
        if len(matches) == 1:
            matches[0]['candidates'][topic_id] = source
    return result


def project(rows, questions=()):
    asked = [question['topic_id'] for question in questions]
    return [{'id': row['id'], 'quote': row['quote'],
             'planned_sources': list(dict.fromkeys(row['candidates'].values())),
             'asked_count': sum(topic_id in row['candidates'] for topic_id in asked)}
            for row in rows]


def next_topics(rows, questions):
    asked = {question['topic_id'] for question in questions}
    return [row for row in rows if row['candidates'] and asked.isdisjoint(row['candidates'])]
