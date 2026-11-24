"""Replay one synthetic interview decision across three frozen candidate sets."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from interview_agent.contracts.common import Contract
from interview_agent.interviews.agent import InterviewFailure, ScriptedInterviewer, interview
from interview_agent.interviews.catalog import pack
from interview_agent.interviews.contracts import question_payload
from interview_agent.interviews.generated_questions import WIKI_SOURCE, question_topics

from .__main__ import provenance, write
from .reviews import item_hash
from .suite import read_jsonl, sha

ARMS = ('A', 'B', 'C')
PREFIX = 'replay-prefix'


class WikiCandidate(Contract):
    status: Literal['active', 'pending', 'conflict']
    title: str
    question: str


class Case(Contract):
    id: str
    family: Literal['retrieval', 'memory', 'tools']
    shape: Literal['concrete', 'vague', 'unverified', 'conflict']
    jd: str
    question: str
    answer: str
    probe: str
    baseline: str
    wiki: WikiCandidate


class Oracle(Contract):
    id: str
    wiki_offered: bool
    wiki_used_in_script: bool
    review_focus: str


def load_suite(directory):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    hashes = {name: sha(root / name) for name in ('cases.jsonl', 'oracles.jsonl')}
    if manifest['sha256'] != hashes:
        raise ValueError('wiki_replay_checksum_mismatch')
    cases = read_jsonl(root / 'cases.jsonl', Case)
    oracle_rows = read_jsonl(root / 'oracles.jsonl', Oracle)
    oracles = {oracle.id: oracle for oracle in oracle_rows}
    ids = {case.id for case in cases}
    if (len(cases) != len(ids) or len(cases) != manifest['cases']
            or len(oracle_rows) != len(oracles) or ids != oracles.keys()):
        raise ValueError('wiki_replay_case_oracle_mismatch')
    return manifest, cases, oracles


def _candidate(text, title, source_hint, wiki_id=None):
    item = {'title': title, 'question': text, 'probe': '', 'target_quote': '',
            'kind': 'scenario', 'difficulty': 'intermediate'}
    if source_hint:
        # A synthetic provenance marker. It only exposes the availability flag to the agent.
        item['public_reference'] = {'id': 'synthetic-source'}
    if wiki_id:
        item['wiki'] = {'id': wiki_id, 'version': 1, 'topic_key': 'synthetic-wiki'}
    return question_topics([item], 'user_jd')


def make_scope(case, arm):
    if arm not in ARMS:
        raise ValueError('wiki_replay_unknown_arm')
    role = 'ai-application'
    base = pack(role)
    first = {'id': str(uuid5(NAMESPACE_URL, case.id + ':question')), 'topic_id': PREFIX,
             'question_id': PREFIX + '-base', 'kind': 'base', 'text': case.question,
             'assisted': False, 'fact_id': None}
    prefix = {'title': '本轮已问', 'base': case.question, 'probe': case.probe,
              'reference': '讨论本题给定场景及决策依据。', 'url': '', 'retest': ''}
    common = _candidate(case.baseline, '继续同一技术主题', source_hint=arm != 'A')
    wiki = _candidate(case.wiki.question, case.wiki.title, source_hint=True, wiki_id=case.id) \
        if arm == 'C' and case.wiki.status == 'active' else {}
    topics = {PREFIX: prefix, **wiki, **common, **base['topics']}
    frozen_pack = {**base, 'topics': topics, 'topic_order': list(topics)}
    return {
        'policy': {'role': role, 'mode': 'interview', 'version': 'ja05-policy-v1', 'max_questions': 4},
        'pack': frozen_pack, 'target': {'kind': 'user_jd', 'text': case.jd},
        'training_context': {'jd_text': case.jd}, 'facts': [], 'prior_observations': [],
        'question_history': [first], 'current_question': first, 'assisted_topics': [],
        'answer': {'id': str(uuid5(NAMESPACE_URL, case.id + ':answer')), 'text': case.answer,
                   'assisted': False, 'question_id': first['id'], 'topic_id': PREFIX},
    }


async def replay(case, arm):
    scope = make_scope(case, arm)
    attempts = 0
    reads = []

    async def admit():
        nonlocal attempts
        attempts += 1

    class RecordedScript(ScriptedInterviewer):
        async def complete(self, messages, specs):
            response = await super().complete(messages, specs)
            for call in response.tool_calls:
                if call.name == 'read_topics':
                    reads.extend(call.arguments['topic_ids'])
            return response

    wiki_ids = {key for key, value in scope['pack']['topics'].items() if value.get('wiki')}
    result = {'arm': arm, 'input_sha256': item_hash(scope), 'wiki_offered': bool(wiki_ids),
              'wiki_eligibility': case.wiki.status if arm == 'C' else 'control'}
    try:
        decision = await interview(scope, RecordedScript(scope, admit), asyncio.Event())
        question = question_payload(scope, decision) if decision.action == 'ask_question' else None
        result.update(status='ok', action=decision.action, question=question, trace=decision.tool_log,
                      wiki_used=bool(question and question.get('source') == WIKI_SOURCE))
    except InterviewFailure as error:
        result.update(status='failed', error_code=error.code, diagnostic=error.diagnostic, wiki_used=False)
    return {**result, 'provider_steps': attempts, 'topic_reads': reads,
            'wiki_read': bool(wiki_ids.intersection(reads)), 'model_http_calls': 0}


def review_layout(version, case_ids):
    shuffled = sorted(case_ids, key=lambda identity: item_hash([version, identity, 'review']))
    return {
        identity: ('B', 'C') if index % 2 == 0 else ('C', 'B')
        for index, identity in enumerate(shuffled)
    }


async def evaluate(manifest, cases, oracles):
    rows = []
    reviews = []
    layout = review_layout(manifest['version'], [case.id for case in cases])
    for case in cases:
        arms = {arm: await replay(case, arm) for arm in ARMS}
        b, c = arms['B'], arms['C']
        all_ok = all(arm['status'] == 'ok' and arm['question'] for arm in arms.values())
        changed = all_ok and b['question']['text'] != c['question']['text']
        oracle = oracles[case.id]
        passed = (bool(all_ok) and arms['A']['question']['text'] == b['question']['text']
                  and c['wiki_offered'] == oracle.wiki_offered
                  and c['wiki_used'] == oracle.wiki_used_in_script
                  and all(arm['question']['kind'] != 'project' for arm in arms.values()))
        if c['status'] != 'ok':
            outcome = 'decision_failed'
        elif not c['wiki_offered']:
            outcome = 'withheld_' + c['wiki_eligibility']
        elif c['wiki_used']:
            outcome = 'used'
        else:
            outcome = 'read_not_used' if c['wiki_read'] else 'offered_not_read'
        pair = [arms[arm] for arm in layout[case.id]]
        rows.append({'case_id': case.id, 'family': case.family, 'arms': arms,
                     'protocol_passed': passed, 'wiki_outcome': outcome,
                     'question_changed_b_to_c': bool(changed), 'oracle_focus': oracle.review_focus,
                     'review_arm_order': [arm['arm'] for arm in pair]})
        reviews.append({'case_id': case.id, 'jd': case.jd, 'previous_question': case.question,
                        'answer': case.answer,
                        'question_x': (pair[0].get('question') or {}).get('text'),
                        'question_y': (pair[1].get('question') or {}).get('text'),
                        'decision': 'pending', 'reason': ''})
    summary = {'version': manifest['version'], 'mode': 'scripted_protocol', 'cases': len(rows),
               'protocol_passed': sum(row['protocol_passed'] for row in rows),
               'wiki_offered': sum(row['arms']['C']['wiki_offered'] for row in rows),
               'wiki_read': sum(row['arms']['C']['wiki_read'] for row in rows),
               'wiki_used': sum(row['arms']['C']['wiki_used'] for row in rows),
               'wiki_outcomes': dict(Counter(row['wiki_outcome'] for row in rows)),
               'question_changed_b_to_c': sum(row['question_changed_b_to_c'] for row in rows),
               'model_http_calls': 0, 'semantic_review_total': 0}
    return {'summary': summary, 'cases': rows}, reviews


def review_markdown(reviews, *, source='固定脚本'):
    sections = ['# 逐题对照表', '', f'以下题目均由{source}选出。请先独立评价 X 和 Y，再填写结论。', '',
                '- 相关性：是否同时贴合岗位、上一题和用户回答？',
                '- 难度：回答具体时是否推进一个可验证的细节？回答含糊时是否先用一个小情景澄清？',
                '- 边界：是否把未知说成事实、虚构个人经历、要求接受错误前提或直接泄露答案？',
                '- 结论：选 X、Y、平或无效。只有两题都合格且难分高下才填平。没有可比较的合格题填无效。', '']
    for row in reviews:
        sections.extend([f"## {row['case_id']}", '', f"岗位 · {row['jd']}",
                         f"上一题 · {row['previous_question']}", f"回答 · {row['answer']}", '',
                         f"- X · {row['question_x'] or '没有有效题目'}",
                         f"- Y · {row['question_y'] or '没有有效题目'}",
                         '- X 的难度与边界 · ', '- Y 的难度与边界 · ',
                         '- 结论 · 待审', '- 理由 · ', ''])
    return '\n'.join(sections)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    manifest, cases, oracles = load_suite(args.suite)
    report, reviews = asyncio.run(evaluate(manifest, cases, oracles))
    args.out.mkdir(parents=True, exist_ok=False)
    write(args.out / 'report.json', report)
    (args.out / 'review-pairs.md').write_text(review_markdown(reviews), encoding='utf-8')
    write(args.out / 'provenance.json', {'suite': manifest, 'run': provenance(Path.cwd())})
    print(json.dumps(report['summary'], ensure_ascii=False))
    return 0 if report['summary']['protocol_passed'] == len(cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
