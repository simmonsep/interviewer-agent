"""Deterministic segmentation (F07Z-34).

External review F07Z-33 chose stable source segment IDs over "the model retypes the line
and we check it is close": the model answers only *which section a line belongs to*. These
tests pin the two halves of that contract — the IDs are reproducible from the source text,
and every rejection is deterministic rather than a similarity judgement.
"""
import pytest

from interview_agent.materials.segments import (MAX_SEGMENTS, is_heading, pre_heading_ids,
                                                resolve_sections, scope_segments, segments_of)


def scope(text, source_id='source-1'):
    return {'draft_style': 'rewrite', 'sources': [{'id': source_id, 'text': text}]}


def test_segments_are_numbered_in_source_order_and_reproducible():
    text = '教育经历\n2014.09 - 2019.07  本科   某大学\n技能\nNode / Vue'
    first = segments_of(text)
    assert [s['id'] if 'id' in s else s['line'] for s in first] == [1, 2, 3, 4]
    assert first[1]['text'] == '2014.09 - 2019.07  本科   某大学'   # inner spacing preserved
    assert [s['id'] for s in scope_segments(scope(text))] == ['S001', 'S002', 'S003', 'S004']
    assert scope_segments(scope(text)) == scope_segments(scope(text))


def test_blank_lines_are_skipped_so_ids_stay_stable():
    a = scope_segments(scope('技能\n\nNode\n'))
    b = scope_segments(scope('技能\nNode'))
    assert [s['text'] for s in a] == [s['text'] for s in b] == ['技能', 'Node']


def test_ids_keep_running_across_multiple_sources():
    two = {'draft_style': 'rewrite',
           'sources': [{'id': 'a', 'text': '技能\nNode'}, {'id': 'b', 'text': '项目经历\n即时聊天'}]}
    segments = scope_segments(two)
    assert [s['id'] for s in segments] == ['S001', 'S002', 'S003', 'S004']
    assert [s['source_id'] for s in segments] == ['a', 'a', 'b', 'b']


@pytest.mark.parametrize('line', ['教育经历', '  工作经历  ', '# 项目经历', '- 技能', '技能：', 'Education', 'skills'])
def test_section_headings_are_told_apart_but_content_rows_are_not(line):
    assert is_heading(line) is True


def test_a_skill_stack_heading_opens_the_skills_block():
    """F07Z-126: resume 4 wrote 技能栈. It was not in HEADING_WORDS, so its rows were told
    they lived under 项目经历 while the final backstop demanded a skills section."""
    segments = scope_segments(scope('项目经历\n某平台\n技能栈\n前端：Vue3\n'))
    rows = [(s['text'], s['heading'], s['block']) for s in segments]
    assert rows == [('项目经历', True, 'projects'), ('某平台', False, 'projects'),
                    ('技能栈', True, 'skills'), ('前端：Vue3', False, 'skills')]


@pytest.mark.parametrize('line', ['技能：Node / Vue', '2014.09 - 2019.07 本科 某大学',
                                  '技能', '即时聊天系统 2017.08 - 2017.08'])
def test_content_rows_are_not_headings_except_the_bare_marker(line):
    # '技能' alone is the heading; every row carrying content stays a normal segment.
    assert is_heading(line) is (line == '技能')


def test_resolution_copies_the_original_line_and_sorts_by_source_order():
    # F07Z-100: a line under one heading can no longer be filed under another (decision D),
    # so the ordering check uses two lines of the same block.
    segments = scope_segments(scope('教育经历\n2014.09 - 2019.07  本科   某大学\n'
                                    '2022.09 - 2024.06  硕士   某大学\n工作经历\n2018.10 某公司'))
    resolved = resolve_sections(segments, {'education': ['S002'], 'experience': ['S005']})
    assert resolved['education'] == ['2014.09 - 2019.07  本科   某大学']
    assert resolved['experience'] == ['2018.10 某公司']
    shuffled = resolve_sections(segments, {'education': ['S003', 'S002'], 'experience': []})
    assert shuffled['education'] == ['2014.09 - 2019.07  本科   某大学',
                                     '2022.09 - 2024.06  硕士   某大学']


def test_a_heading_can_never_be_used_as_an_entry():
    segments = scope_segments(scope('教育经历\n2014.09 本科 某大学'))
    with pytest.raises(ValueError, match='education_heading_not_allowed'):
        resolve_sections(segments, {'education': ['S001']})


def test_an_unknown_id_is_rejected_not_guessed():
    segments = scope_segments(scope('技能\nNode'))
    with pytest.raises(ValueError, match='skills_segment_not_in_scope'):
        resolve_sections(segments, {'skills': ['S999']})


def test_the_same_line_cannot_fill_two_sections():
    segments = scope_segments(scope('技能\nNode'))
    with pytest.raises(ValueError, match='segment_assigned_twice'):
        resolve_sections(segments, {'skills': ['S002'], 'projects': ['S002']})


def test_segment_count_is_capped_so_ids_keep_their_width():
    text = '\n'.join(f'line {index}' for index in range(MAX_SEGMENTS + 1))
    with pytest.raises(ValueError, match='material_scope_too_many_segments'):
        scope_segments(scope(text))

def test_header_block_is_every_body_line_before_the_first_heading():
    text = '求职意向：Agent\n研二在读｜2027.06 毕业\n教育经历\n2022.09 本科 某大学'
    segments = scope_segments(scope(text))
    assert pre_heading_ids(segments) == ['S001', 'S002']


def test_a_heading_free_resume_has_no_header_block():
    """Without a heading there is no way to tell header from content (F07Z-49).

    F07Z-100 keeps this for a plain note; a heading-free *resume* that declares its sections
    inline ("教育经历：...") does get a header block, see
    `test_material_structure_gate.py::test_heading_free_resume_keeps_header_and_places_bullets`.
    """
    segments = scope_segments(scope('五年经验的后端开发者\n负责订单服务重构'))
    assert pre_heading_ids(segments) == []


def test_compatibility_ideograph_headings_are_normalised():
    text='教育经历\n2014.09 某大学\n⼯作经历\n2018.10 某公司\n个⼈技能\nNode'
    segments=segments_of(text)
    assert [s['text'] for s in segments if s['heading']]==['教育经历','工作经历','个人技能']
    assert '⼯' not in '\n'.join(s['text'] for s in segments)
