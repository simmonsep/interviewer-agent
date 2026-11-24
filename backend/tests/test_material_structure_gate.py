"""F07Z-100: the structural gates ruled by the decision-maker (A + D + E).

- D: a line under one heading cannot be filed into another section, and the refusal arrives in
  the same batch envelope the model can act on.
- A heading-free resume (inline prefixes) keeps its header block and places bullets by the
  nearest preceding structured anchor.
- Rendering no longer escapes `. - +` inside bullet lines.
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from interview_agent.materials.agent import run_material_agent
from interview_agent.materials.contracts import (MaterialResult, RewriteItem, escaped,
                                                 render_draft)
from interview_agent.materials.segments import (placement, pre_heading_ids,
                                                resolve_sections, scope_segments)
from interview_agent.runtime.types import AssistantResponse, ToolCall


def rewrite_scope(text):
    sid = str(uuid4())
    return {'mode': 'draft', 'draft_style': 'rewrite', 'goal_version': 1,
            'material_revision': 1, 'target': {'kind': 'direction', 'direction': 'Java 后端'},
            'sources': [{'id': sid, 'name': '简历.md', 'text': text,
                         'content_sha256': 'test'}], 'facts': []}, sid


def test_structured_assignment_cannot_cross_its_block():
    scope, _ = rewrite_scope('工作经历\n2019.03 某公司\n1. 官网项目\n- 做了甲\n技能\n- Python\n')
    segments = scope_segments(scope)
    by_text = lambda prefix: next(s['id'] for s in segments if s['text'].startswith(prefix))
    title = by_text('1. 官网项目')
    with pytest.raises(ValueError, match='projects_cross_block'):
        resolve_sections(segments, {'education': [], 'experience': [], 'projects': [title],
                                    'skills': []})


def test_cross_block_submission_returns_batch_incomplete():
    scope, sid = rewrite_scope('工作经历\n2019.03 某公司\n1. 官网项目\n- 做了甲\n技能\n- Python\n')
    segments = scope_segments(scope)
    pick = lambda prefix: next(s['id'] for s in segments if s['text'].startswith(prefix)
                               and not s['heading'])
    dated, title, bullet, skill = pick('2019.03'), pick('1. 官网项目'), pick('- 做了甲'), pick('- Python')

    def submit(projects=(), experience=(), items=(), skills=()):
        return {'items': [{'text': text, 'source_ids': [sid], 'change_note': '整理',
                           'evidence_segments': [evidence]} for text, evidence in items],
                'education': [], 'experience': list(experience), 'projects': list(projects),
                'skills': list(skills), 'suggestions': []}

    class Crosser:
        def __init__(self):
            self.n = 0
            self.seen = []

        async def complete(self, messages, specs):
            self.seen.append(messages[-1].text or '')
            self.n += 1
            if self.n == 1:
                name, args = 'read_material_scope', {}
            elif self.n == 2:
                name, args = 'read_target', {}
            elif self.n == 3:
                name, args = 'read_source', {'source_id': sid}
            elif self.n == 4:
                name, args = 'submit_rewrite', submit(projects=[dated, title],
                                                      items=[('做了甲', bullet)], skills=[skill])
            else:
                name, args = 'submit_rewrite', submit(experience=[dated, title],
                                                      items=[('做了甲', bullet)], skills=[skill])
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    provider = Crosser()
    result = asyncio.run(run_material_agent(scope, provider, asyncio.Event()))
    refusal = provider.seen[4]
    assert '"reason": "batch_incomplete"' in refusal, refusal
    assert title in refusal and '不能放进' in refusal, refusal
    assert provider.n == 5
    assert result.structured_sections['projects'] == []
    assert set(result.structured_sections['experience']) == {dated, title}


def test_heading_free_resume_keeps_header_and_places_bullets():
    scope, _ = rewrite_scope(
        '张三 · 求职意向：Java 后端开发实习\n'
        '教育经历：某大学 计算机 本科 2023-2027\n'
        '实习经历：某公司 后端实习生\n'
        '- 负责订单查询接口\n'
        '- 用 Redis 缓存热点商品\n'
        '项目经历：校园二手平台\n'
        '- 负责订单服务\n')
    segments = scope_segments(scope)
    ids = {s['text']: s['id'] for s in segments}
    assert pre_heading_ids(segments) == [ids['张三 · 求职意向：Java 后端开发实习']]
    items = [SimpleNamespace(evidence_segments=[ids['- 负责订单查询接口']]),
             SimpleNamespace(evidence_segments=[ids['- 用 Redis 缓存热点商品']]),
             SimpleNamespace(evidence_segments=[ids['- 负责订单服务']])]
    structured = {'basics': [ids['张三 · 求职意向：Java 后端开发实习']],
                  'education': [ids['教育经历：某大学 计算机 本科 2023-2027']],
                  'experience': [ids['实习经历：某公司 后端实习生']],
                  'projects': [ids['项目经历：校园二手平台']]}
    grouped = placement(segments, items, structured)
    assert grouped['experience'] == [0, 1]
    assert grouped['projects'] == [2]
    assert grouped['basics'] == []


SYNTHETIC = ('张三 · 求职意向：Java 后端开发实习\n'
             '教育经历：某大学 计算机科学与技术 本科 2023.09 - 2027.06\n'
             '实习经历：2025.07 - 2025.10 某电商公司 后端开发实习生\n'
             '- 负责订单查询接口，用 Java 17 与 Spring Boot 重写慢查询，P95 从 1.2s 降到 300ms。\n'
             '- 用 Redis 缓存热点商品，命中率约 85%，大促期间数据库 QPS 下降 40%。\n'
             '项目经历：校园二手交易平台 2024.03 - 2024.09\n'
             '- 负责基于 Spring Cloud 的订单服务，使用 MySQL 与 MyBatis-Plus，接口平均延迟 80ms。\n'
             '- 用 RabbitMQ 处理超时未支付订单，消息积压告警降到 0。\n'
             '技能：Java、Spring Boot、MySQL、Redis、RabbitMQ、Git\n')


def test_rendered_heading_free_draft_keeps_header_and_work_bullets():
    """The nine-line synthetic resume that lost its first line and mis-filed two bullets."""
    scope, sid = rewrite_scope(SYNTHETIC)
    segments = scope_segments(scope)
    text_of = {s['id']: s['text'] for s in segments}
    structured = {'basics': ['S001'], 'education': ['S002'], 'experience': ['S003'],
                  'projects': ['S006'], 'skills': ['S009']}
    result = MaterialResult(action='draft',
        basics=[text_of['S001']], education=[text_of['S002']], experience=[text_of['S003']],
        projects=[text_of['S006']], skills=[text_of['S009']], suggestions=[],
        rewrite_items=[RewriteItem(text=text_of[sid_].lstrip('- '), source_ids=[sid],
                                   change_note='整理', evidence_segments=[sid_])
                       for sid_ in ('S004', 'S005', 'S007', 'S008')],
        structured_sections=structured,
        structured_segment_ids=['S001', 'S002', 'S003', 'S006', 'S009'])
    markdown, _ = render_draft(scope, result)
    basics, rest = markdown.split('# 教育经历')
    work, rest = rest.split('# 项目经历')
    projects = rest.split('# 技能')[0]
    assert '张三' in basics
    assert '负责订单查询接口' in work and '用 Redis 缓存热点商品' in work
    assert '负责订单查询接口' not in projects and '用 Redis 缓存热点商品' not in projects


def test_out_of_order_structured_map_keeps_source_order():
    scope, sid = rewrite_scope('工作经历\n2018.10 甲公司\n2018.05 乙公司\n- 做了甲\n')
    segments = scope_segments(scope)
    text_of = {s['id']: s['text'] for s in segments}
    result = MaterialResult(action='draft', basics=[], education=[],
                            experience=[text_of['S002'], text_of['S003']],
                            projects=[], skills=[], suggestions=[],
                            rewrite_items=[RewriteItem(text='做了甲', source_ids=[sid],
                                                       change_note='整理',
                                                       evidence_segments=['S004'])],
                            structured_sections={'education': [], 'experience': ['S003', 'S002'],
                                                 'projects': [], 'skills': []},
                            structured_segment_ids=['S003', 'S002'])
    markdown, _ = render_draft(scope, result)
    work = markdown.split('# 工作经历')[1].split('# 技能')[0]
    assert work.index('2018.10') < work.index('2018.05'), work


def test_escape_keeps_plain_punctuation_and_escapes_markdown():
    assert escaped('2014.09 - 2019.07') == '2014.09 - 2019.07'
    assert escaped('Vue + FullPage + Element') == 'Vue + FullPage + Element'
    assert escaped('demo.user@example.com') == 'demo.user@example.com'
    assert escaped('a*b_c') == 'a\\*b\\_c'
    assert escaped('x | y') == 'x \\| y'


def test_rendered_lines_carry_no_backslash_escapes():
    """Validation 3 of the ruling: plain punctuation must survive into the exported markdown."""
    scope, sid = rewrite_scope('工作经历\n2014.09 - 2019.07  某公司\n'
                               '- Vue + FullPage + Element\n- demo.user@example.com\n')
    segments = scope_segments(scope)
    text_of = {s['id']: s['text'] for s in segments}
    result = MaterialResult(action='draft', basics=[], education=[],
                            experience=[text_of['S002']], projects=[], skills=[],
                            suggestions=[],
                            rewrite_items=[RewriteItem(text=text_of[sid_].lstrip('- '),
                                                       source_ids=[sid], change_note='整理',
                                                       evidence_segments=[sid_])
                                           for sid_ in ('S003', 'S004')],
                            structured_sections={'education': [], 'experience': ['S002'],
                                                 'projects': [], 'skills': []},
                            structured_segment_ids=['S002'])
    markdown, _ = render_draft(scope, result)
    assert '2014.09 - 2019.07' in markdown
    assert 'Vue + FullPage + Element' in markdown
    assert 'demo.user@example.com' in markdown
    assert '\\' not in markdown, markdown


def test_empty_section_is_not_rendered_as_a_bare_heading():
    scope, sid = rewrite_scope('工作经历\n2018.10 甲公司\n- 做了甲\n')
    segments = scope_segments(scope)
    text_of = {s['id']: s['text'] for s in segments}
    result = MaterialResult(action='draft', basics=[], education=[],
                            experience=[text_of['S002']], projects=[], skills=[],
                            suggestions=[],
                            rewrite_items=[RewriteItem(text='做了甲', source_ids=[sid],
                                                       change_note='整理',
                                                       evidence_segments=['S003'])],
                            structured_sections={'education': [], 'experience': ['S002'],
                                                 'projects': [], 'skills': []},
                            structured_segment_ids=['S002'])
    markdown, _ = render_draft(scope, result)
    assert '# 项目经历' not in markdown, markdown
    assert '# 工作经历' in markdown


def test_material_policy_allows_twelve_attempts_and_interview_stays_four(project_root):
    from interview_agent.billing.config import DEFAULT_LIVE_POLICY
    from interview_agent.interviews.billing import InterviewPolicy
    from interview_agent.materials.billing import MaterialPolicy
    material = MaterialPolicy.load(project_root / 'configs/ja04-deepseek-flash.json')
    interview = InterviewPolicy.load(project_root / 'configs/ja05-deepseek-flash.json')
    assert material.max_operation_attempts == 12
    assert interview.max_operation_attempts == 4
    assert DEFAULT_LIVE_POLICY['max_operation_attempts'] == 4
