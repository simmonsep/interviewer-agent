"""B1: structured fields are explicit, and sections are rendered by code.

External review (F07Z-24): education and skills are facts the source already states, so the
model must not be the only path by which they survive. These tests pin the contract:
  * an empty `education`/`skills` is rejected when the source clearly has such a block;
  * the renderer owns the section headings and order.
"""
from uuid import uuid4

import pytest

from interview_agent.materials.contracts import (MaterialResult, RewriteArgs, render_draft,
                                                 validate_result)


SOURCE_ID = str(uuid4())


def scope(text):
    return {'mode': 'draft', 'draft_style': 'rewrite',
            'sources': [{'id': SOURCE_ID, 'name': '简历.md', 'text': text}],
            'target': {'kind': 'user_jd', 'text': '前端开发', 'content_sha256': 'x'}}


def item(text='参与订单服务改造。'):
    return {'text': text, 'source_ids': [SOURCE_ID], 'change_note': '重排表达。'}


def args(**overrides):
    body = {'action': 'draft', 'rewrite_items': [item()], 'education': [], 'experience': [],
            'projects': [], 'skills': []}
    body.update(overrides)
    return body


RESUME = ('教育经历\n2014.09 - 2019.07 本科 某大学 计算机科学与技术\n'
          '工作经历\n2018.10 - 2019.02 某公司 前端开发\n'
          '项目经历\n2017.08 即时聊天系统\n'
          '技能\nNode / Vue / MySQL\n')


def test_section_keys_are_optional_per_batch_but_required_once_merged():
    """A batch may only touch one section (F07Z-46); the merged result still refuses gaps.

    Requiring all four keys on every submit made the model fail with "Field required"
    whenever it worked on a single section, which the long sample exposed.
    """
    RewriteArgs.model_validate({'items': [item()]})
    args_without_education = args(education=[], skills=['Node / Vue / MySQL'])
    with pytest.raises(ValueError, match='education_section_missing'):
        validate_result(scope(RESUME), args_without_education)


def test_empty_education_is_rejected_when_the_source_states_one():
    with pytest.raises(ValueError, match='education_section_missing'):
        validate_result(scope(RESUME), args())


def test_empty_skills_is_rejected_when_the_source_states_them():
    with pytest.raises(ValueError, match='skills_section_missing'):
        validate_result(scope(RESUME), args(education=['2014.09 - 2019.07 本科 某大学'],
                                           experience=['2018.10 - 2019.02 某公司 前端开发实习生'],
                                           projects=['2017.08 即时聊天系统']))


def test_a_source_without_those_blocks_may_leave_them_empty():
    result = validate_result(scope('负责缓存改造，接口响应时间从约 200ms 降到 80ms。'), args())
    assert result.education == [] and result.skills == []


def test_a_skill_word_inside_another_block_does_not_demand_a_skills_section():
    """F07Z-126: the demand follows declared blocks, not marker words anywhere in the text.

    Resume 4 mentioned 技能 inside a project block while 技能栈 was not recognised as a heading.
    The old backstop then required `skills` although the batch rules forbade filing those rows
    there - an unsatisfiable pair that ended with no draft.
    """
    text = '项目经历\n2025.02-2025.04 智能知识库平台\n前端：Vue3，含技能培训模块。\n'
    result = validate_result(scope(text), args(projects=['2025.02-2025.04 智能知识库平台']))
    assert result.skills == []


def test_renderer_owns_the_sections_and_their_order():
    result = validate_result(scope(RESUME), args(education=['2014.09 - 2019.07 本科 某大学'],
                                                 experience=['2018.10 - 2019.02 某公司 前端开发实习生'],
                                                 projects=['2017.08 即时聊天系统'],
                                                 skills=['Node / Vue / MySQL']))
    markdown, payload = render_draft(scope(RESUME), result)
    assert markdown.index('# 教育经历') < markdown.index('# 项目经历') < markdown.index('# 技能')
    # the renderer escapes markdown punctuation, so match the payload verbatim instead
    assert '2014' in markdown and 'MySQL' in markdown
    assert payload['education'] == ['2014.09 - 2019.07 本科 某大学']
    assert payload['skills'] == ['Node / Vue / MySQL']


def test_structured_fields_are_not_allowed_on_other_actions():
    body = MaterialResult(action='ask_user', question='补充一下项目？', education=['x'])
    with pytest.raises(ValueError, match='rewrite_only'):
        validate_result(scope(RESUME), body)


def test_empty_experience_is_rejected_when_the_source_states_work_history():
    with pytest.raises(ValueError, match='experience_section_missing'):
        validate_result(scope(RESUME), args(education=['2014.09 - 2019.07 本科 某大学'],
                                            projects=['2017.08 即时聊天系统'],
                                            skills=['Node / Vue / MySQL']))


def test_work_history_is_rendered_by_code_between_education_and_projects():
    result = validate_result(scope(RESUME),
                             args(education=['2014.09 - 2019.07 本科 某大学'],
                                  experience=['2018.10 - 2019.02 某公司 前端开发实习生'],
                                  projects=['2017.08 即时聊天系统'],
                                  skills=['Node / Vue / MySQL']))
    markdown, payload = render_draft(scope(RESUME), result)
    assert (markdown.index('# 教育经历') < markdown.index('# 工作经历')
            < markdown.index('# 项目经历') < markdown.index('# 技能'))
    assert '2018' in markdown and '2019' in markdown
    assert payload['experience'] == ['2018.10 - 2019.02 某公司 前端开发实习生']


def test_experience_is_also_forbidden_on_other_actions():
    body = MaterialResult(action='ask_user', question='补充一下？',
                          experience=['2018.10 - 2019.02 某公司'])
    with pytest.raises(ValueError, match='rewrite_only'):
        validate_result(scope(RESUME), body)


def test_empty_projects_is_rejected_when_the_source_has_a_project_section():
    with pytest.raises(ValueError, match='projects_section_missing'):
        validate_result(scope(RESUME), args(education=['2014.09 - 2019.07 本科 某大学'],
                                            experience=['2018.10 - 2019.02 某公司 前端开发'],
                                            skills=['Node / Vue / MySQL']))


def test_structured_projects_render_under_the_existing_project_heading():
    result = validate_result(scope(RESUME),
                             args(education=['2014.09 - 2019.07 本科 某大学'],
                                  experience=['2018.10 - 2019.02 某公司 前端开发'],
                                  projects=['2017.08 即时聊天系统'],
                                  skills=['Node / Vue / MySQL']))
    markdown, payload = render_draft(scope(RESUME), result)
    project = markdown.split('# 项目经历')[1].split('# 技能')[0]
    # the structured line comes before the narrative bullet, under the same heading
    assert '2017' in project and project.index('2017') < project.index('参与订单服务改造')
    assert payload['projects'] == ['2017.08 即时聊天系统']
