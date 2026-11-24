"""Coverage is a different question from grounding, and only part of it is deterministic."""
from interview_agent.evaluation.material_coverage import (
    coverage, source_lines, structural_tokens)

RESUME = """教育经历
2014.09 - 2019.07  本科  某大学  计算机科学与技术  专业排名 20%~50%
工作经历
2018.10 - 2019.02  某公司  前端开发实习生
负责订单服务缓存改造，接口平均响应时间从约 200ms 降到 80ms
技能
Node / Vue / React / MySQL / Webpack"""


def output(education=True, skills=True, body=True):
    blocks = []
    if education:
        blocks.append('# 教育经历\n\n- 2014.09 - 2019.07 本科 某大学 专业排名 20%~50%')
    if body:
        blocks.append('# 项目经历\n\n- 2018.10 - 2019.02 某公司 前端开发实习生\n- 参与订单服务缓存改造，接口平均响应时间从约 200ms 降到 80ms')
    if skills:
        blocks.append('# 技能\n\n- Node / Vue / React / MySQL / Webpack')
    return '\n\n'.join(blocks)


def test_complete_output_has_no_structural_gap():
    result = coverage(RESUME, output())
    assert result['passed'] is True
    assert result['structural']['missing'] == []
    assert result['lines']['covered'] == result['lines']['total']


def test_dropped_education_is_caught_by_structural_tokens():
    result = coverage(RESUME, output(education=False))
    assert result['passed'] is False
    missing = ' '.join(result['structural']['missing'])
    assert '2014' in missing and '20%' in missing
    assert any('本科' in item['line'] for item in result['lines']['dropped'])


def test_dropped_skills_are_caught_by_tech_tokens():
    result = coverage(RESUME, output(skills=False))
    assert result['passed'] is False
    missing = [token.lower() for token in result['structural']['missing']]
    assert 'react' in missing and 'mysql' in missing


def test_rewording_is_not_penalised_when_the_facts_survive():
    reworded = ('# 项目经历\n\n- 2018.10 - 2019.02 某公司 前端开发实习生\n- 缓存改造由你参与，接口平均响应时间从约 200ms 降到 80ms\n\n'
                '# 教育经历\n\n- 2014.09 - 2019.07 本科 某大学 计算机科学与技术 专业排名 20%~50%\n\n'
                '# 技能\n\n- Node、Vue、React、MySQL、Webpack')
    assert coverage(RESUME, reworded)['passed'] is True


def test_headings_and_fragments_are_not_counted_as_content():
    lines = source_lines(RESUME)
    assert all(line not in ('教育经历', '工作经历', '技能') for line in lines)
    assert lines


def test_markdown_escapes_do_not_break_token_matching():
    escaped = output().replace('2014.09', '2014\\.09').replace('200ms', '200ms')
    assert coverage(RESUME, escaped)['passed'] is True


def test_report_never_claims_semantic_validity():
    result = coverage(RESUME, output())
    assert result['semantic_validity'] == 'not_established'
    assert result['lines']['note'].startswith('diagnostic only')
