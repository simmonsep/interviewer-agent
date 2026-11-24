"""F07Z-159 对照 1：同一兼容题库、两个不同的 JD，脚本协议下首题是否随 JD 变化。

这不是"应该怎样"的断言，而是把现状记录下来：排序还没接进候选视图，脚本协议按题目包顺序出题。
"""
import os
from pathlib import Path

import pytest

from interview_agent.evaluation.interview_session import SessionCase, execute_session
from interview_agent.worker.config import WorkerSettings

from test_postgres import pg_settings  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def case_with(jd_text: str) -> SessionCase:
    return SessionCase.model_validate({
        'case_id': 'IS15', 'family_id': 'target-variation', 'split': 'development',
        'provenance': 'project_authored_synthetic', 'review': 'pending_human_review',
        'role': 'ai-application', 'mode': 'interview', 'message': '帮我准备技术面试练习。',
        'jd_text': jd_text, 'source_text': '', 'confirm_count': 0,
        'max_questions': 2, 'stop_after_questions': None, 'pair_id': None,
        'idempotency_probe': False,
        'candidate': {'allowed_kinds': ['base', 'probe', 'retest'],
                      'allowed_topics': ['agent-tools', 'sql-isolation', 'redis-lock', 'http-idempotency'],
                      'rules': [{'topic_id': topic, 'kind': None, 'branch': 'substantive',
                                 'answer': '我做过这类实现，具体是读写与并发控制。'}
                                for topic in ('agent-tools', 'sql-isolation', 'redis-lock', 'http-idempotency')]},
    })


@pytest.fixture(scope='session')
def worker_settings():
    return WorkerSettings.load(os.environ['PY_INTERVIEW_WORKER_TEST_CONFIG'])


@pytest.mark.postgres
@pytest.mark.redis
def test_two_jds_record_the_first_question_each(worker_settings, pg_settings):
    runs = {}
    for label, jd in (
        ('langgraph', 'AI 应用开发实习：熟悉 LangGraph 状态恢复、checkpointer 与 thread_id。'),
        ('tools', 'AI 应用开发实习：熟悉 Python 工具调用治理与权限边界。'),
    ):
        result = execute_session(case_with(jd), worker_settings, pg_settings[1], ROOT)
        output = result.get('output') or {}
        first = next((turn for turn in output.get('turns') or [] if turn.get('question_text')),
                     (output.get('turns') or [None])[0] if output.get('turns') else None)
        runs[label] = {'status': result.get('status'), 'pack': output.get('pack_version'),
                       'first_topic': first.get('topic_id') if first else None,
                       'first_text': (first.get('question_text') or '')[:60] if first else None,
                       'jd': jd}
        print('RUN', label, runs[label])
    assert all(run['status'] == 'completed' for run in runs.values()), runs
    # The observation under test: with the current scripted path the JD does not change selection.
    print('SAME_FIRST', runs['langgraph']['first_topic'] == runs['tools']['first_topic'])
